"""Logical QueryEngine ownership, identity, and lifecycle invariants."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.commands import CommandBus
from agent_builder_v2.context import ContextCompiler, ContextPlan, ModelProfile
from agent_builder_v2.contracts import EventEnvelope, StartRunCommand
from agent_builder_v2.query_engine import (
    QueryContextUnavailableError,
    QueryEngine,
    QueryEngineOwnershipError,
    QueryEngineRegistry,
    QueryEngineRetiredError,
    QueryReplayCursorError,
    QueryReplayUnavailableError,
    QueryRunNotRetainedError,
)
from agent_builder_v2.replay import (
    DurableReplay,
    ReplayGap,
    RunIdentity,
    project_durable_run,
)
from agent_builder_v2.sessions import (
    Conversation,
    ConversationConflictError,
    ConversationDeleteResult,
    ConversationNotFoundError,
    ConversationSummary,
)
from agent_builder_v2.state import JournalCorruptionError
from agent_builder_v2.tools import prototype_tool_specs, toolset_digest


PLAN_DIGEST = "a" * 64


def _started_payload(
    context_plan: dict[str, object] | None = None,
) -> dict[str, object]:
    if context_plan is None:
        context_plan = {
            "plan_id": f"context-{PLAN_DIGEST[:24]}",
            "digest": PLAN_DIGEST,
            "toolset_digest": toolset_digest(prototype_tool_specs()),
            "section_count": 3,
            "history_message_count": 0,
            "included_history_message_count": 0,
            "omitted_history_message_count": 0,
            "history_source_digest": "b" * 64,
            "windowing_strategy": "full",
            "estimated_input_tokens": 1_024,
            "native_context_tokens": 262_144,
            "operational_context_tokens": 32_768,
            "input_budget_tokens": 30_720,
            "compact_at_tokens": 24_576,
            "compact_target_tokens": 18_432,
            "output_reserve_tokens": 2_048,
            "template_reserve_tokens": 256,
            "estimator": "utf8-bytes-upper-bound-v1",
        }
    return {
        "prototype": True,
        "model": "qwen3.5:2b",
        "visible_tools": ["builtin/echo"],
        "sandbox": "harness-v2-worker-v1",
        "context_plan": dict(context_plan),
    }


def _completed_payload() -> dict[str, object]:
    return {
        "reason": "end_turn",
        "model_iterations": 1,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "last_input_tokens": 100,
            "complete": True,
        },
    }


def _conversation(conversation_id: str, *, title: str = "test") -> Conversation:
    return Conversation(
        conversation_id=conversation_id,
        agent_id=PROTOTYPE_AGENT_ID,
        title=title,
        created_at="2026-07-18T00:00:00.000Z",
        updated_at="2026-07-18T00:00:00.000Z",
        revision=0,
        active_run_id=None,
        turns=(),
    )


@dataclass(frozen=True, slots=True)
class _RunRecord:
    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    context_plan: ContextPlan | None
    events: tuple[EventEnvelope, ...]


def _context_plan(message: str) -> ContextPlan:
    return ContextCompiler().compile(
        message,
        model_profile=ModelProfile(
            provider="ollama",
            model="qwen3.5:2b",
            model_digest="c" * 64,
            native_context_tokens=262_144,
            operational_context_tokens=32_768,
            max_output_tokens=2_048,
            profile_source="test-profile",
        ),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )


class _RunService:
    """Small authority double; the QueryEngine must not mirror this state."""

    def __init__(self) -> None:
        self.capsule = SimpleNamespace(agent_id=PROTOTYPE_AGENT_ID)
        self.conversations: dict[str, Conversation] = {}
        self.runs: dict[str, _RunRecord] = {}
        self.events: dict[str, tuple[EventEnvelope, ...]] = {}
        self.durable_events: dict[str, tuple[EventEnvelope, ...]] = {}
        self.run_identities: dict[str, RunIdentity] = {}
        self.started: list[StartRunCommand] = []
        self.cancelled: list[str] = []
        self.streamed: list[tuple[str, int]] = []
        self.resolved: list[str] = []
        self.replayed: list[tuple[str, int, int, RunIdentity]] = []
        self.snapshot_only = False
        self.replay_error: BaseException | None = None
        self.get_conversation_calls: list[str] = []
        self.get_calls = 0
        self.evict_on_get_call: int | None = None
        self.foreign_on_get_call: int | None = None
        self._next_conversation = 1
        self._next_run = 1

    async def create_conversation(self, title: str = "新会话") -> Conversation:
        conversation_id = f"{self._next_conversation:032x}"
        self._next_conversation += 1
        value = _conversation(conversation_id, title=title)
        self.conversations[conversation_id] = value
        return value

    async def list_conversations(self) -> tuple[ConversationSummary, ...]:
        return tuple(
            ConversationSummary(
                conversation_id=value.conversation_id,
                agent_id=value.agent_id,
                title=value.title,
                created_at=value.created_at,
                updated_at=value.updated_at,
                revision=value.revision,
                active_run_id=value.active_run_id,
                turn_count=len(value.turns),
                completed_turn_count=0,
                last_run_id=None,
            )
            for value in self.conversations.values()
        )

    async def get_conversation(self, conversation_id: str) -> Conversation:
        self.get_conversation_calls.append(conversation_id)
        try:
            return self.conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError("conversation not found") from exc

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        deleted = self.conversations.pop(conversation_id, None)
        return ConversationDeleteResult(
            deleted=deleted is not None,
            deleted_turns=0,
            deleted_events=0,
        )

    async def start(self, command: StartRunCommand) -> _RunRecord:
        command.validate()
        conversation_id = command.conversation_id
        if conversation_id is None:
            conversation = await self.create_conversation()
            conversation_id = conversation.conversation_id
        elif conversation_id not in self.conversations:
            raise ConversationNotFoundError("conversation not found")
        self.started.append(command)
        index = self._next_run
        self._next_run += 1
        turn_id = f"{index + 10_000:032x}"
        run_id = f"{index + 20_000:032x}"
        plan = _context_plan(command.message)
        started_event = EventEnvelope(
            event_id=f"{index + 40_000:032x}",
            agent_id=command.agent_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            seq=1,
            occurred_at="2026-07-18T00:00:00.000Z",
            kind="run.started",
            durability="durable",
            payload=_started_payload(plan.public_metadata()),
        )
        record = _RunRecord(
            agent_id=command.agent_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            context_plan=plan,
            events=(started_event,),
        )
        self.runs[record.run_id] = record
        identity = RunIdentity(
            record.agent_id,
            record.conversation_id,
            record.turn_id,
            record.run_id,
        )
        self.run_identities[record.run_id] = identity
        self.events[record.run_id] = (
            EventEnvelope(
                event_id=f"{index + 30_000:032x}",
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=1,
                occurred_at="2026-07-18T00:00:00.000Z",
                kind="run.completed",
                durability="durable",
                payload={"reason": "end_turn", "model_iterations": 1},
            ),
        )
        self.durable_events[record.run_id] = (
            started_event,
            EventEnvelope(
                event_id=f"{index + 50_000:032x}",
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=2,
                occurred_at="2026-07-18T00:00:00.001Z",
                kind="run.completed",
                durability="durable",
                payload=_completed_payload(),
            ),
        )
        return record

    def get(self, run_id: str) -> _RunRecord:
        self.get_calls += 1
        if self.evict_on_get_call == self.get_calls:
            self.runs.pop(run_id, None)
        if self.foreign_on_get_call == self.get_calls and run_id in self.runs:
            self.runs[run_id] = replace(
                self.runs[run_id], agent_id="foreign-agent"
            )
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise KeyError("run not found") from exc

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def stream(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[EventEnvelope | None]:
        self.streamed.append((run_id, after))
        for event in self.events[run_id]:
            if event.seq > after:
                yield event

    async def resolve_run_identity(self, run_id: str) -> RunIdentity:
        self.resolved.append(run_id)
        try:
            return self.run_identities[run_id]
        except KeyError as exc:
            raise KeyError("run not found") from exc

    async def replay_run(
        self,
        run_id: str,
        *,
        after: int,
        limit: int,
        expected_identity: RunIdentity,
    ) -> DurableReplay:
        self.replayed.append((run_id, after, limit, expected_identity))
        if self.replay_error is not None:
            raise self.replay_error
        if self.run_identities.get(run_id) != expected_identity:
            raise KeyError("run not found")
        events = self.durable_events[run_id]
        snapshot, gaps = project_durable_run(events)
        if after > snapshot.through_seq:
            raise ValueError("replay cursor is newer than the durable Run")
        if self.snapshot_only:
            return DurableReplay(
                identity=expected_identity,
                availability="snapshot_only",
                oldest_cursor=snapshot.through_seq,
                latest_cursor=snapshot.through_seq,
                next_cursor=snapshot.through_seq,
                has_more=False,
                events=(),
                gaps=(
                    ()
                    if after == snapshot.through_seq
                    else (
                        ReplayGap(
                            after + 1,
                            snapshot.through_seq,
                            "retention",
                        ),
                    )
                ),
                snapshot=snapshot,
            )
        page = tuple(event for event in events if event.seq > after)[:limit]
        next_cursor = page[-1].seq if page else after
        return DurableReplay(
            identity=expected_identity,
            availability="complete",
            oldest_cursor=0,
            latest_cursor=snapshot.through_seq,
            next_cursor=next_cursor,
            has_more=next_cursor < snapshot.through_seq,
            events=page,
            gaps=tuple(
                gap
                for gap in gaps
                if gap.to_seq > after and gap.from_seq <= next_cursor
            ),
            snapshot=snapshot,
        )


class _CancellationSafeDeleteRunService(_RunService):
    """Model RunService's owned, shielded durable delete operation."""

    def __init__(self) -> None:
        super().__init__()
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        async def owned_delete() -> ConversationDeleteResult:
            self.delete_started.set()
            await self.allow_delete.wait()
            return await super(
                _CancellationSafeDeleteRunService, self
            ).delete_conversation(conversation_id)

        operation = asyncio.create_task(owned_delete())
        return await asyncio.shield(operation)


class _SingleActiveRunService(_RunService):
    async def start(self, command: StartRunCommand) -> _RunRecord:
        assert command.conversation_id is not None
        conversation = await self.get_conversation(command.conversation_id)
        if conversation.active_run_id is not None:
            raise ConversationConflictError(
                "conversation already has an active Run"
            )
        record = await super().start(command)
        self.conversations[conversation.conversation_id] = replace(
            conversation, active_run_id=record.run_id, revision=1
        )
        return record


class _GatedStartRunService(_SingleActiveRunService):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.allow_start = asyncio.Event()

    async def start(self, command: StartRunCommand) -> _RunRecord:
        self.start_entered.set()
        await self.allow_start.wait()
        return await super().start(command)


class _GatedInternRegistry(QueryEngineRegistry):
    def __init__(self, service: _RunService) -> None:
        super().__init__(service, PROTOTYPE_AGENT_ID)  # type: ignore[arg-type]
        self.intern_started = asyncio.Event()
        self.allow_intern = asyncio.Event()

    async def _intern(self, conversation_id: str) -> QueryEngine:
        self.intern_started.set()
        await self.allow_intern.wait()
        return await super()._intern(conversation_id)


def test_registry_reuses_one_logical_engine_per_conversation() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("persistent session")

        engines = await asyncio.gather(
            *(
                registry.for_conversation(conversation.conversation_id)
                for _ in range(32)
            )
        )

        assert all(engine is engines[0] for engine in engines)
        assert engines[0].agent_id == PROTOTYPE_AGENT_ID
        assert engines[0].conversation_id == conversation.conversation_id
        assert await engines[0].restore() == conversation

    asyncio.run(scenario())


def test_engine_submission_is_bound_to_its_conversation() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        first = await registry.create_conversation("first")
        second = await registry.create_conversation("second")
        engine = await registry.for_conversation(first.conversation_id)

        record = await engine.submit_message("stay in the first conversation")

        assert record.conversation_id == first.conversation_id
        assert service.started[-1] == StartRunCommand(
            agent_id=PROTOTYPE_AGENT_ID,
            conversation_id=first.conversation_id,
            message="stay in the first conversation",
        )
        assert record.conversation_id != second.conversation_id

    asyncio.run(scenario())


def test_command_bus_routes_start_and_cancel_through_query_engine() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        commands = CommandBus(registry)
        conversation = await registry.create_conversation("command boundary")

        handle = await commands.start(
            StartRunCommand(
                agent_id=PROTOTYPE_AGENT_ID,
                conversation_id=conversation.conversation_id,
                message="typed command",
            )
        )
        assert await registry.get_run(handle.run_id) == handle
        events = [
            event
            async for event in registry.stream(handle.run_id)
            if event is not None
        ]
        await commands.cancel(handle.run_id)

        assert service.started[-1].conversation_id == conversation.conversation_id
        assert [event.kind for event in events] == ["run.completed"]
        assert service.cancelled == [handle.run_id]

    asyncio.run(scenario())


def test_retained_context_inspection_is_owned_fresh_and_fail_closed() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        first = await registry.create_conversation("first")
        second = await registry.create_conversation("second")
        first_engine = await registry.for_conversation(first.conversation_id)
        second_engine = await registry.for_conversation(second.conversation_id)
        admitted = await first_engine.submit_message("operator-secret-71")

        inspection = await registry.inspect_retained_context(admitted.run_id)
        payload = inspection.to_dict()
        assert payload["identity"] == admitted.to_dict()
        assert payload["availability"] == "exact"
        assert [section["id"] for section in payload["sections"]] == [
            "platform.contract",
            "agent.instructions",
            "turn.user",
        ]
        assert all("content" not in section for section in payload["sections"])
        assert "operator-secret-71" not in str(payload)

        payload["identity"]["run_id"] = "changed"
        payload["sections"][0]["id"] = "changed"
        fresh = (await registry.inspect_retained_context(admitted.run_id)).to_dict()
        assert fresh["identity"]["run_id"] == admitted.run_id
        assert fresh["sections"][0]["id"] == "platform.contract"
        assert [
            section["content_digest"] for section in fresh["sections"]
        ] == [
            section["content_digest"] for section in inspection.to_dict()["sections"]
        ]

        restarted_registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        restarted = (
            await restarted_registry.inspect_retained_context(admitted.run_id)
        ).to_dict()
        assert [
            section["content_digest"] for section in restarted["sections"]
        ] != [
            section["content_digest"] for section in fresh["sections"]
        ]

        with pytest.raises(QueryEngineOwnershipError):
            await second_engine.inspect_context(
                admitted.run_id,
                durable_identity=service.run_identities[admitted.run_id],
                content_digest_key=b"x" * 32,
            )

        other = await second_engine.submit_message("different valid plan")
        original = service.runs[admitted.run_id]
        service.runs[admitted.run_id] = replace(
            original,
            context_plan=service.runs[other.run_id].context_plan,
        )
        with pytest.raises(QueryContextUnavailableError, match="unavailable"):
            await registry.inspect_retained_context(admitted.run_id)

        service.runs[admitted.run_id] = replace(
            original, context_plan=None
        )
        with pytest.raises(QueryContextUnavailableError, match="unavailable"):
            await registry.inspect_retained_context(admitted.run_id)

        service.runs.clear()
        with pytest.raises(QueryRunNotRetainedError):
            await registry.inspect_retained_context(admitted.run_id)

    asyncio.run(scenario())


def test_retained_context_second_lookup_eviction_and_foreign_record_diverge() -> None:
    async def scenario() -> None:
        evicted_service = _RunService()
        evicted_registry = QueryEngineRegistry(
            evicted_service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await evicted_registry.create_conversation("eviction race")
        admitted = await evicted_registry.submit(
            StartRunCommand(
                agent_id=PROTOTYPE_AGENT_ID,
                conversation_id=conversation.conversation_id,
                message="evict between retained lookups",
            )
        )
        evicted_service.evict_on_get_call = 2

        with pytest.raises(QueryRunNotRetainedError, match="not retained"):
            await evicted_registry.inspect_retained_context(admitted.run_id)

        foreign_service = _RunService()
        foreign_registry = QueryEngineRegistry(
            foreign_service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await foreign_registry.create_conversation("foreign race")
        admitted = await foreign_registry.submit(
            StartRunCommand(
                agent_id=PROTOTYPE_AGENT_ID,
                conversation_id=conversation.conversation_id,
                message="replace with foreign identity",
            )
        )
        foreign_service.foreign_on_get_call = 2

        with pytest.raises(QueryEngineOwnershipError, match="not found"):
            await foreign_registry.inspect_retained_context(admitted.run_id)

    asyncio.run(scenario())


def test_durable_run_resolution_and_replay_do_not_require_live_record() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("durable replay")
        engine = await registry.for_conversation(conversation.conversation_id)
        admitted = await engine.submit_message("persist this Run")

        # Model a Gateway restart/Run retention eviction.  Durable identity and
        # semantic events remain available, but ``RunService.get`` cannot work.
        service.runs.clear()

        resolved = await registry.resolve_run_identity(admitted.run_id)
        first = await registry.replay(admitted.run_id, after=0, limit=1)
        second = await registry.replay(
            admitted.run_id, after=first.next_cursor, limit=1
        )

        assert resolved == admitted
        assert [event.seq for event in first.events] == [1]
        assert first.has_more is True
        assert [event.seq for event in second.events] == [2]
        assert second.has_more is False
        assert second.snapshot.complete is True
        assert service.resolved == [admitted.run_id] * 3
        assert [value[1:3] for value in service.replayed] == [(0, 1), (1, 1)]

    asyncio.run(scenario())


def test_durable_replay_enforces_engine_identity_and_cursor() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        first = await registry.create_conversation("first")
        second = await registry.create_conversation("second")
        first_engine = await registry.for_conversation(first.conversation_id)
        second_engine = await registry.for_conversation(second.conversation_id)
        admitted = await first_engine.submit_message("owned durable Run")
        identity = await service.resolve_run_identity(admitted.run_id)

        with pytest.raises(QueryEngineOwnershipError):
            await second_engine.replay(identity, after=0, limit=1)
        assert service.replayed == []

        with pytest.raises(QueryReplayCursorError):
            await registry.replay(admitted.run_id, after=3, limit=1)

        resolved_before = len(service.resolved)
        with pytest.raises(ValueError, match="invalid replay cursor"):
            await registry.replay(admitted.run_id, after=-1, limit=1)
        with pytest.raises(ValueError, match="invalid replay limit"):
            await registry.replay(admitted.run_id, after=0, limit=0)
        assert len(service.resolved) == resolved_before

    asyncio.run(scenario())


def test_durable_replay_accepts_identity_bound_snapshot_only_page() -> None:
    async def scenario() -> None:
        service = _RunService()
        service.snapshot_only = True
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("retained snapshot")
        admitted = await (
            await registry.for_conversation(conversation.conversation_id)
        ).submit_message("retain only the projection")
        service.runs.clear()

        replay = await registry.replay(admitted.run_id, after=0, limit=1)

        assert replay.availability == "snapshot_only"
        assert replay.events == ()
        assert replay.next_cursor == replay.latest_cursor == 2
        assert replay.gaps == (ReplayGap(1, 2, "retention"),)
        assert replay.snapshot.identity.run_id == admitted.run_id

    asyncio.run(scenario())


def test_durable_replay_maps_corrupt_or_tombstoned_journal_to_unavailable() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await service.create_conversation("unavailable replay")
        engine = await registry.for_conversation(conversation.conversation_id)
        admitted = await engine.submit_message("create durable identity")
        service.runs.clear()
        service.replay_error = JournalCorruptionError(
            "managed Run events are unavailable"
        )

        with pytest.raises(QueryReplayUnavailableError, match="unavailable"):
            await registry.replay(admitted.run_id, after=0, limit=1)

    asyncio.run(scenario())


def test_same_conversation_concurrent_submit_accepts_only_one_turn() -> None:
    async def scenario() -> None:
        service = _SingleActiveRunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("single active")
        engine = await registry.for_conversation(conversation.conversation_id)

        results = await asyncio.gather(
            engine.submit_message("first"),
            engine.submit_message("second"),
            return_exceptions=True,
        )

        assert sum(not isinstance(value, BaseException) for value in results) == 1
        assert sum(
            isinstance(value, ConversationConflictError) for value in results
        ) == 1
        assert len(service.started) == 1
        restored = await engine.restore()
        assert restored.active_run_id in service.runs

    asyncio.run(scenario())


def test_second_submit_fails_fast_while_first_admission_is_preparing() -> None:
    async def scenario() -> None:
        service = _GatedStartRunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("gated admission")
        engine = await registry.for_conversation(conversation.conversation_id)

        first = asyncio.create_task(engine.submit_message("first"))
        await asyncio.wait_for(service.start_entered.wait(), timeout=0.25)

        with pytest.raises(ConversationConflictError, match="in progress"):
            await asyncio.wait_for(
                engine.submit_message("must not queue"), timeout=0.05
            )
        assert service.started == []

        service.allow_start.set()
        admitted = await asyncio.wait_for(first, timeout=0.25)
        assert admitted.conversation_id == conversation.conversation_id
        assert [command.message for command in service.started] == ["first"]

    asyncio.run(scenario())


def test_engine_rejects_cross_conversation_run_control_and_streaming() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        first = await registry.create_conversation("first")
        second = await registry.create_conversation("second")
        first_engine = await registry.for_conversation(first.conversation_id)
        second_engine = await registry.for_conversation(second.conversation_id)
        record = await first_engine.submit_message("owned by first")

        with pytest.raises(QueryEngineOwnershipError):
            await second_engine.cancel_run(record.run_id)

        with pytest.raises(QueryEngineOwnershipError):
            async for _event in second_engine.stream(record.run_id):
                pass

        assert service.cancelled == []
        assert service.streamed == []

    asyncio.run(scenario())


def test_cancel_active_turn_uses_the_durable_conversation_pointer() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("interrupt")
        engine = await registry.for_conversation(conversation.conversation_id)
        handle = await engine.submit_message("long turn")
        service.conversations[conversation.conversation_id] = replace(
            conversation, active_run_id=handle.run_id, revision=1
        )

        assert await engine.cancel_active_turn() == handle.run_id
        assert service.cancelled == [handle.run_id]

        service.conversations[conversation.conversation_id] = replace(
            conversation, active_run_id=None, revision=2
        )
        assert await engine.cancel_active_turn() is None
        assert service.cancelled == [handle.run_id]

    asyncio.run(scenario())


def test_delete_retires_old_handle_and_removes_it_from_the_registry() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("delete me")
        old_engine = await registry.for_conversation(conversation.conversation_id)

        result = await registry.delete_conversation(conversation.conversation_id)

        assert result.deleted is True
        with pytest.raises(QueryEngineRetiredError):
            await old_engine.restore()
        with pytest.raises(QueryEngineRetiredError):
            await old_engine.submit_message("must not resurrect deleted state")

        replacement = replace(conversation, title="replacement", revision=1)
        service.conversations[conversation.conversation_id] = replacement
        new_engine = await registry.for_conversation(conversation.conversation_id)

        assert new_engine is not old_engine
        assert await new_engine.restore() == replacement

    asyncio.run(scenario())


def test_cancelled_delete_still_retires_and_evicts_after_durable_commit() -> None:
    async def scenario() -> None:
        service = _CancellationSafeDeleteRunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("cancel delete")
        engine = await registry.for_conversation(conversation.conversation_id)

        deletion = asyncio.create_task(engine.delete())
        await service.delete_started.wait()
        deletion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deletion

        retry = asyncio.create_task(
            registry.delete_conversation(conversation.conversation_id)
        )
        await asyncio.sleep(0)
        service.allow_delete.set()
        assert (await retry).deleted is True
        for _attempt in range(20):
            if engine.retired and registry.cached_engine_count == 0:
                break
            await asyncio.sleep(0)

        assert conversation.conversation_id not in service.conversations
        assert engine.retired is True
        assert registry.cached_engine_count == 0
        with pytest.raises(QueryEngineRetiredError):
            await engine.submit_message("must remain deleted")

    asyncio.run(scenario())


def test_concurrent_delete_joins_one_owned_operation() -> None:
    async def scenario() -> None:
        service = _CancellationSafeDeleteRunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("double delete")

        first = asyncio.create_task(
            registry.delete_conversation(conversation.conversation_id)
        )
        await service.delete_started.wait()
        second = asyncio.create_task(
            registry.delete_conversation(conversation.conversation_id)
        )
        await asyncio.sleep(0)
        service.allow_delete.set()
        results = await asyncio.gather(first, second)

        assert [result.deleted for result in results] == [True, True]
        assert registry.cached_engine_count == 0

    asyncio.run(scenario())


def test_restore_reads_authoritative_state_instead_of_caching_transcript() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("revision zero")
        engine = await registry.for_conversation(conversation.conversation_id)

        first = await engine.restore()
        service.conversations[conversation.conversation_id] = replace(
            conversation,
            title="revision one",
            revision=1,
            updated_at="2026-07-18T00:00:01.000Z",
        )
        second = await engine.restore()

        assert first.revision == 0
        assert second.revision == 1
        assert second.title == "revision one"
        assert service.get_conversation_calls.count(conversation.conversation_id) >= 2

    asyncio.run(scenario())


def test_cancelled_create_cleans_durable_state_before_registry_close() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = _GatedInternRegistry(service)

        creation = asyncio.create_task(
            registry.create_conversation("abandoned create")
        )
        await registry.intern_started.wait()
        assert len(service.conversations) == 1
        creation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creation
        await registry._drain_lifecycle_tasks()

        assert service.conversations == {}
        assert registry.cached_engine_count == 0

    asyncio.run(scenario())


def test_cancelled_legacy_admission_requests_run_cancellation() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = _GatedInternRegistry(service)

        submission = asyncio.create_task(
            registry.submit(
                StartRunCommand(
                    agent_id=PROTOTYPE_AGENT_ID,
                    message="compatibility route",
                )
            )
        )
        await registry.intern_started.wait()
        run_id = next(iter(service.runs))
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
        await registry._drain_lifecycle_tasks()

        assert service.cancelled == [run_id]
        assert registry.cached_engine_count == 0

    asyncio.run(scenario())


def test_registry_close_retires_every_old_handle() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID
        )  # type: ignore[arg-type]
        conversation = await registry.create_conversation("shutdown")
        engine = await registry.for_conversation(conversation.conversation_id)

        await registry.close()

        assert registry.cached_engine_count == 0
        with pytest.raises(QueryEngineRetiredError):
            await engine.restore()
        with pytest.raises(RuntimeError, match="closed"):
            await registry.for_conversation(conversation.conversation_id)

    asyncio.run(scenario())


def test_query_engine_cache_fails_closed_at_conversation_store_capacity() -> None:
    async def scenario() -> None:
        service = _RunService()
        registry = QueryEngineRegistry(
            service, PROTOTYPE_AGENT_ID, capacity=2
        )  # type: ignore[arg-type]
        conversation_ids = [
            f"{index + 1:032x}" for index in range(3)
        ]
        service.conversations.update(
            (conversation_id, _conversation(conversation_id))
            for conversation_id in conversation_ids
        )

        await asyncio.gather(
            *(registry.for_conversation(value) for value in conversation_ids[:-1])
        )
        assert registry.cached_engine_count == 2
        with pytest.raises(RuntimeError, match="capacity"):
            await registry.for_conversation(conversation_ids[-1])
        assert registry.cached_engine_count == 2

    asyncio.run(scenario())

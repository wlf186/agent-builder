"""Negative-security and authenticated-flow tests for the Web Gateway."""

from __future__ import annotations

from dataclasses import dataclass, replace
import asyncio
import base64
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from agent_builder_v2.auth import SessionService
from agent_builder_v2.agents import AgentRegistry
from agent_builder_v2.capsule import (
    PROTOTYPE_AGENT_ID,
    SYSTEM_AGENT_DISPLAY_NAME,
)
from agent_builder_v2.commands import CommandBus
from agent_builder_v2.context import ContextCompiler, ContextPlan, ModelProfile
from agent_builder_v2.context_audit import ContextRevealPolicy
from agent_builder_v2.extensions import ExtensionCatalog
from agent_builder_v2.contracts import EventEnvelope, StartRunCommand
from agent_builder_v2.model_catalog import default_model_catalog
from agent_builder_v2.query_engine import QueryEngineRegistry
from agent_builder_v2.replay import (
    DurableReplay,
    ReplayGap,
    RunIdentity,
    project_durable_run,
)
from agent_builder_v2.research import ResearchEnvironmentRecord
from agent_builder_v2.web import (
    CSRF_COOKIE,
    MAX_JSON_BODY_BYTES,
    SESSION_COOKIE,
    LoginLimiter,
    _rename_runtime_conversation,
    create_app,
)
from agent_builder_v2.sessions import (
    Conversation,
    ConversationConflictError,
    ConversationDeleteResult,
    ConversationNotFoundError,
    ConversationPage,
    ConversationStoreUnavailableError,
    ConversationTurnCapacityError,
    ConversationSummary,
    ConversationTurn,
    PermissionRecord,
    TurnTerminalSummary,
)
from agent_builder_v2.skills import SkillRecord
from agent_builder_v2.tools import prototype_tool_specs, toolset_digest
from agent_builder_v2.workspace_context import WorkspaceContextError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:20815"
SAME_ORIGIN = {"origin": BASE_URL}
PROJECT_TOKEN = "b" * 64
RUN_ID = "1" * 32
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


@dataclass(frozen=True)
class _RunRecord:
    agent_id: str = PROTOTYPE_AGENT_ID
    conversation_id: str = "2" * 32
    turn_id: str = "3" * 32
    run_id: str = RUN_ID
    context_plan: ContextPlan | None = None
    events: tuple[EventEnvelope, ...] = ()


@dataclass(frozen=True)
class _TaskRecord:
    task_id: str = "d" * 32
    state: str = "queued"

    def public_metadata(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "agent_id": PROTOTYPE_AGENT_ID,
            "capsule_generation": 1,
            "conversation_id": "2" * 32,
            "turn_id": "3" * 32,
            "parent_run_id": RUN_ID,
            "command_id": "runtime-compile",
            "state": self.state,
            "result": None,
            "result_digest": None,
            "error_code": None,
            "output_bytes": 0,
            "notification_count": 1,
            "created_at": "2026-07-20T00:00:00.000Z",
            "started_at": None,
            "finished_at": None,
            "updated_at": "2026-07-20T00:00:00.000Z",
        }


def _context_plan(
    message: str, agent_id: str = PROTOTYPE_AGENT_ID
) -> ContextPlan:
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
        agent_id=agent_id,
        capsule_generation=1,
    )


class _Commands(CommandBus):
    def __init__(self, query_engines: QueryEngineRegistry) -> None:
        super().__init__(query_engines)
        self.started: list[Any] = []

    async def start(self, command: StartRunCommand) -> Any:
        self.started.append(command)
        return await super().start(command)


class _LifecycleManager:
    """Boundary-test lifecycle fence; execution is injected separately."""

    def __init__(self) -> None:
        self.draining: set[str] = set()
        self.runtimes: dict[str, object] = {}
        self.research_environments: dict[str, ResearchEnvironmentRecord] = {}
        catalog = default_model_catalog()
        profile = _context_plan("catalog").model_profile
        qualification = SimpleNamespace(
            catalog_model_id="qwen3.5:2b",
            model_profile=profile,
        )
        self.model_broker = SimpleNamespace(
            catalog=catalog,
            qualifications=(qualification,),
        )

    def register(
        self,
        agent_id: str,
        run_service: object,
        query_engines: QueryEngineRegistry,
        commands: CommandBus,
    ) -> None:
        self.runtimes[agent_id] = SimpleNamespace(
            agent_id=agent_id,
            generation=1,
            run_service=run_service,
            query_engines=query_engines,
            commands=commands,
        )

    async def for_agent(self, agent_id: str) -> object:
        if agent_id in self.draining:
            raise RuntimeError("Agent runtime is draining")
        try:
            return self.runtimes[agent_id]
        except KeyError as exc:
            raise KeyError("Agent not found") from exc

    async def begin_drain(self, agent_id: str) -> None:
        self.draining.add(agent_id)

    async def end_drain(self, agent_id: str) -> None:
        self.draining.discard(agent_id)

    async def research_environment_status(
        self, agent_id: str
    ) -> ResearchEnvironmentRecord | None:
        if agent_id not in self.runtimes:
            raise KeyError("Agent not found")
        return self.research_environments.get(agent_id)

    async def install_research_environment(
        self, agent_id: str
    ) -> ResearchEnvironmentRecord:
        if agent_id not in self.runtimes:
            raise KeyError("Agent not found")
        record = ResearchEnvironmentRecord(
            "research-documents",
            "1",
            ("pypdf==6.14.2", "python-docx==1.2.0"),
            "a" * 64,
            "2026-07-21T00:00:00.000Z",
        )
        self.research_environments[agent_id] = record
        return record

    async def delete_research_environment(self, agent_id: str) -> None:
        if agent_id not in self.runtimes:
            raise KeyError("Agent not found")
        self.research_environments.pop(agent_id, None)


class _RunService:
    capsule = object()
    model_qualification = SimpleNamespace(model="qwen3.5:2b")
    sandbox_qualification = object()
    extension_executor = SimpleNamespace(catalog=ExtensionCatalog.empty())

    def __init__(self, agent_id: str = PROTOTYPE_AGENT_ID) -> None:
        self.agent_id = agent_id
        self.conversations: dict[str, Conversation] = {}
        self.runs: dict[str, _RunRecord] = {}
        self.run_identities: dict[str, RunIdentity] = {}
        self.durable_events: dict[str, tuple[EventEnvelope, ...]] = {}
        self.cancelled: list[str] = []
        self.streamed: list[tuple[str, int]] = []
        self.replayed: list[tuple[str, int, int]] = []
        self.resolve_calls = 0
        self.delete_identity_on_resolve_call: int | None = None
        self.get_calls = 0
        self.evict_on_get_call: int | None = None
        self.foreign_on_get_call: int | None = None
        self.snapshot_only = False
        self.replay_error: BaseException | None = None
        self.permissions: dict[str, PermissionRecord] = {}
        self.tasks: dict[str, _TaskRecord] = {}
        self.skills: dict[str, SkillRecord] = {}
        self.preparations: dict[str, dict[str, object]] = {}
        self.preparation_cancellations: list[str] = []
        self.get_conversation_calls = 0
        self.get_conversation_page_calls = 0

    async def create_conversation(self, title: str) -> Conversation:
        conversation = Conversation(
            conversation_id="4" * 32,
            agent_id=self.agent_id,
            title=title,
            created_at="2026-07-18T00:00:00.000Z",
            updated_at="2026-07-18T00:00:00.000Z",
            revision=0,
            active_run_id=None,
            turns=(),
        )
        self.conversations[conversation.conversation_id] = conversation
        return conversation

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
                completed_turn_count=sum(
                    turn.status == "completed" for turn in value.turns
                ),
                last_run_id=value.turns[-1].run_id if value.turns else None,
            )
            for value in self.conversations.values()
        )

    async def get_conversation(self, conversation_id: str) -> Conversation:
        self.get_conversation_calls += 1
        try:
            return self.conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError("conversation not found") from exc

    async def get_conversation_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before_position: int | None = None,
        expected_revision: int | None = None,
    ) -> ConversationPage:
        self.get_conversation_page_calls += 1
        try:
            current = self.conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError("conversation not found") from exc
        if expected_revision is not None and current.revision != expected_revision:
            raise ConversationConflictError("stale page revision")
        if before_position is not None and (
            not any(turn.position == before_position for turn in current.turns)
            or not any(turn.position < before_position for turn in current.turns)
        ):
            raise ConversationConflictError("stale page boundary")
        eligible = tuple(
            turn
            for turn in current.turns
            if before_position is None or turn.position < before_position
        )
        summary = ConversationSummary(
            conversation_id=current.conversation_id,
            agent_id=current.agent_id,
            title=current.title,
            created_at=current.created_at,
            updated_at=current.updated_at,
            revision=current.revision,
            active_run_id=current.active_run_id,
            turn_count=len(current.turns),
            completed_turn_count=sum(
                turn.status == "completed" for turn in current.turns
            ),
            last_run_id=current.turns[-1].run_id if current.turns else None,
        )
        return ConversationPage(
            summary=summary,
            turns=eligible[-limit:],
            limit=limit,
            eligible_turn_count=len(eligible),
            before_position=before_position,
        )

    async def rename_conversation(
        self,
        conversation_id: str,
        title: str,
        *,
        expected_revision: int,
    ) -> Conversation:
        try:
            current = self.conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError("conversation not found") from exc
        if not title.strip() or len(title.encode("utf-8")) > 256:
            raise ValueError("invalid title")
        if current.revision != expected_revision:
            raise ConversationConflictError("stale rename")
        renamed = replace(
            current,
            title=title,
            revision=current.revision + (title != current.title),
            updated_at="2026-07-21T00:00:02.000Z",
        )
        self.conversations[conversation_id] = renamed
        return renamed

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        value = self.conversations.pop(conversation_id, None)
        return ConversationDeleteResult(
            deleted=value is not None,
            deleted_turns=len(value.turns) if value is not None else 0,
            deleted_events=0,
        )

    async def create_conversation_continuation(
        self, source_conversation_id: str, *, title: str = "续接会话"
    ) -> tuple[Conversation, int, int]:
        if source_conversation_id not in self.conversations:
            raise ConversationNotFoundError("conversation not found")
        conversation = Conversation(
            conversation_id="6" * 32,
            agent_id=self.agent_id,
            title=title,
            created_at="2026-07-21T00:00:00.000Z",
            updated_at="2026-07-21T00:00:00.000Z",
            revision=0,
            active_run_id=None,
            turns=(),
        )
        self.conversations[conversation.conversation_id] = conversation
        return conversation, 2, 3

    async def next_turn_preview(
        self, conversation_id: str, *, model_id: str | None = None
    ) -> dict[str, object]:
        if conversation_id not in self.conversations:
            raise ConversationNotFoundError("conversation not found")
        return {
            "version": "next-turn-preview-v1",
            "agent_id": self.agent_id,
            "conversation_id": conversation_id,
            "conversation_revision": self.conversations[conversation_id].revision,
            "model_id": model_id or "qwen3.5:2b",
            "availability": "available",
            "basis": "provider-calibrated-context-v1",
            "fixed_context_tokens": 1_024,
            "fixed_context_error_margin_tokens": 64,
            "safe_user_tokens": 20_000,
            "single_message_byte_limit": 8_192,
        }

    async def preparation_status(
        self, conversation_id: str
    ) -> dict[str, object] | None:
        if conversation_id not in self.conversations:
            raise ConversationNotFoundError("conversation not found")
        return self.preparations.get(conversation_id)

    async def cancel_preparation(
        self, conversation_id: str, operation_id: str
    ) -> dict[str, object]:
        if conversation_id not in self.conversations:
            raise ConversationNotFoundError("conversation not found")
        preparation = self.preparations.get(conversation_id)
        if preparation is None:
            return {
                "version": "run-preparation-cancel-v1",
                "state": "idle",
                "target": None,
            }
        if preparation.get("operation_id") != operation_id:
            return {
                "version": "run-preparation-cancel-v1",
                "state": "stale",
                "target": None,
            }
        self.preparation_cancellations.append(conversation_id)
        preparation["state"] = "cancelling"
        return {
            "version": "run-preparation-cancel-v1",
            "state": "cancellation_requested",
            "target": "preparation",
        }

    async def start(self, command: StartRunCommand) -> _RunRecord:
        command.validate()
        if command.agent_id != self.agent_id:
            raise ValueError("unknown Agent")
        conversation_id = command.conversation_id or "2" * 32
        if conversation_id not in self.conversations:
            if command.conversation_id is not None:
                raise ConversationNotFoundError("conversation not found")
            self.conversations[conversation_id] = Conversation(
                conversation_id=conversation_id,
                agent_id=self.agent_id,
                title="新会话",
                created_at="2026-07-18T00:00:00.000Z",
                updated_at="2026-07-18T00:00:00.000Z",
                revision=0,
                active_run_id=None,
                turns=(),
            )
        plan = _context_plan(command.message, self.agent_id)
        started_event = EventEnvelope(
            event_id="7" * 32,
            agent_id=self.agent_id,
            conversation_id=conversation_id,
            turn_id="3" * 32,
            run_id=RUN_ID,
            seq=1,
            occurred_at="2026-07-18T00:00:00.000Z",
            kind="run.started",
            durability="durable",
            payload=_started_payload(plan.public_metadata()),
        )
        record = _RunRecord(
            agent_id=self.agent_id,
            conversation_id=conversation_id,
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
        self.durable_events[record.run_id] = (
            started_event,
            EventEnvelope(
                event_id="8" * 32,
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=2,
                occurred_at="2026-07-18T00:00:00.001Z",
                kind="assistant.block.started",
                durability="durable",
                payload={"block_id": "answer", "block_type": "content"},
            ),
            EventEnvelope(
                event_id="9" * 32,
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=3,
                occurred_at="2026-07-18T00:00:00.002Z",
                kind="assistant.block.finished",
                durability="durable",
                payload={"block_id": "answer", "content": "durable answer"},
            ),
            EventEnvelope(
                event_id="a" * 32,
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=4,
                occurred_at="2026-07-18T00:00:00.003Z",
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

    async def list_permission_requests(
        self, *, pending_only: bool = True
    ) -> tuple[PermissionRecord, ...]:
        return tuple(
            value
            for value in self.permissions.values()
            if not pending_only or value.status == "pending"
        )

    async def resolve_permission_request(
        self, permission_id: str, decision: str
    ) -> PermissionRecord:
        try:
            current = self.permissions[permission_id]
        except KeyError as exc:
            raise KeyError("permission not found") from exc
        if current.status != "pending":
            raise ConversationConflictError("permission already resolved")
        resolved = replace(
            current,
            status="approved" if decision == "approve" else "denied",
            resolved_at="2026-07-20T00:00:01.000Z",
            resolution_source="operator",
        )
        self.permissions[permission_id] = resolved
        return resolved

    async def capability_audit_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 128
    ) -> tuple[object, ...]:
        if run_id not in self.run_identities:
            raise KeyError("run not found")
        return ()

    async def submit_background_task(
        self, run_id: str, arguments: dict[str, str] | None = None
    ) -> _TaskRecord:
        if run_id not in self.run_identities:
            raise KeyError("run not found")
        task = _TaskRecord()
        self.tasks[task.task_id] = task
        return task

    async def list_background_tasks(self) -> tuple[_TaskRecord, ...]:
        return tuple(self.tasks.values())

    async def get_background_task(self, task_id: str) -> _TaskRecord:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise KeyError("Task not found") from exc

    async def background_task_notifications(self, task_id: str) -> tuple[object, ...]:
        if task_id not in self.tasks:
            raise KeyError("Task not found")
        return (
            SimpleNamespace(
                sequence=1,
                kind="task.queued",
                payload={"state": "queued"},
                payload_digest="e" * 64,
                created_at="2026-07-20T00:00:00.000Z",
            ),
        )

    async def cancel_background_task(self, task_id: str) -> _TaskRecord:
        current = await self.get_background_task(task_id)
        cancelled = replace(current, state="cancelled")
        self.tasks[task_id] = cancelled
        return cancelled

    async def list_skills(self) -> tuple[SkillRecord, ...]:
        return tuple(self.skills.values())

    async def install_skill(self, raw: bytes, expected_digest: str) -> SkillRecord:
        assert raw == b"test-skill-archive"
        assert expected_digest == hashlib.sha256(raw).hexdigest()
        record = SkillRecord(
            skill_id="b" * 32,
            version="1.0.0",
            display_name="Web Skill",
            package_digest=expected_digest,
            content_digest="c" * 64,
            capabilities_json="[]",
            installed_at="2026-07-20T00:00:00.000Z",
            updated_at="2026-07-20T00:00:00.000Z",
        )
        self.skills[record.skill_id] = record
        return record

    async def delete_skill(self, skill_id: str) -> None:
        if self.skills.pop(skill_id, None) is None:
            raise KeyError("Skill not found")

    async def stream(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[EventEnvelope | None]:
        self.streamed.append((run_id, after))
        record = self.get(run_id)
        event = EventEnvelope(
            event_id="6" * 32,
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            seq=1,
            occurred_at="2026-07-18T00:00:01.000Z",
            kind="run.completed",
            durability="durable",
            payload={"reason": "end_turn", "model_iterations": 1},
        )
        if event.seq > after:
            yield event

    async def resolve_run_identity(self, run_id: str) -> RunIdentity:
        self.resolve_calls += 1
        if self.delete_identity_on_resolve_call == self.resolve_calls:
            self.run_identities.pop(run_id, None)
            raise KeyError("run not found")
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
        self.replayed.append((run_id, after, limit))
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


@pytest.fixture
def web_client() -> tuple[TestClient, _Commands]:
    app = create_app(REPOSITORY_ROOT)
    run_service = _RunService()
    app.state.sessions = SessionService(PROJECT_TOKEN)
    app.state.run_service = run_service
    query_engines = QueryEngineRegistry(
        run_service, PROTOTYPE_AGENT_ID
    )  # type: ignore[arg-type]
    commands = _Commands(query_engines)
    runtime_manager = _LifecycleManager()
    runtime_manager.register(
        PROTOTYPE_AGENT_ID, run_service, query_engines, commands
    )
    app.state.runtime_manager = runtime_manager
    app.state.query_engines = query_engines
    app.state.commands = commands
    app.state.login_limiter = LoginLimiter()
    app.state.cookie_secure = False
    # Deliberately do not enter TestClient as a context manager: the production
    # lifespan provisions a real Capsule, while these boundary tests inject all
    # route collaborators explicitly and perform no project writes.
    client = TestClient(app, base_url=BASE_URL)
    try:
        yield client, commands
    finally:
        client.close()


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "127.0.0.1@attacker.example",
        "127.0.0.1:0",
        "[::1",
    ],
)
def test_invalid_host_is_rejected_with_security_headers(
    web_client: tuple[TestClient, _Commands], host: str
) -> None:
    client, _commands = web_client

    response = client.get("/health", headers={"host": host})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid Host header"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_health_reports_qualified_runtime_dependencies(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "release": "0.2.0",
        "prototype": True,
        "agent_ready": True,
        "model": "qwen3.5:2b",
        "sandbox": "landlock+seccomp",
    }


def test_extension_catalog_is_authenticated_and_hides_endpoints(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    assert client.get("/api/extensions").status_code == 401
    client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    response = client.get("/api/extensions")
    assert response.status_code == 200
    assert response.json() == {"extensions": []}
    assert "endpoint" not in response.text


def test_skill_install_and_delete_are_authenticated_and_csrf_bound(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    agent = PROTOTYPE_AGENT_ID
    assert client.get(f"/api/agents/{agent}/skills").status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    raw = b"test-skill-archive"
    body = {
        "archive_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert client.post(
        f"/api/agents/{agent}/skills", json=body, headers=SAME_ORIGIN
    ).status_code == 403
    installed = client.post(
        f"/api/agents/{agent}/skills",
        json=body,
        headers={**SAME_ORIGIN, "x-csrf-token": csrf},
    )
    assert installed.status_code == 201
    skill_id = installed.json()["skill_id"]
    assert "archive" not in installed.text
    assert client.get(f"/api/agents/{agent}/skills").json()["skills"][0][
        "skill_id"
    ] == skill_id
    assert client.delete(
        f"/api/agents/{agent}/skills/{skill_id}",
        headers={**SAME_ORIGIN, "x-csrf-token": csrf},
    ).status_code == 204
    assert client.get(f"/api/agents/{agent}/skills").json()["skills"] == []


def test_research_environment_is_agent_scoped_authenticated_and_csrf_bound(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    endpoint = f"/api/agents/{PROTOTYPE_AGENT_ID}/research-environment"
    assert client.get(endpoint).status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}
    assert client.get(endpoint).json() == {
        "agent_id": PROTOTYPE_AGENT_ID,
        "installed": False,
        "environment": None,
    }
    assert client.post(endpoint, json={}, headers=SAME_ORIGIN).status_code == 403
    installed = client.post(endpoint, json={}, headers=mutation)
    assert installed.status_code == 200
    assert installed.json()["installed"] is True
    assert installed.json()["environment"]["reuse_scope"] == (
        "agent-generation-across-conversations"
    )
    assert "endpoint" not in installed.text
    assert client.get(endpoint).json()["installed"] is True
    assert client.delete(endpoint, headers=mutation).status_code == 204
    assert client.get(endpoint).json()["installed"] is False
    assert client.get(
        f"/api/agents/{'f' * 32}/research-environment"
    ).status_code == 404


def test_timeline_exposes_complete_events_as_inert_text(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    index = client.get("/")
    script = client.get("/assets/app.js")

    assert index.status_code == 200
    assert 'id="event-detail-dialog"' in index.text
    assert 'aria-labelledby="event-detail-title"' in index.text
    assert 'id="replay-workbench"' in index.text
    assert 'id="sequence-lane-header"' in index.text
    assert 'id="turn-conversation-panel"' in index.text
    assert 'id="command-help-list"' in index.text
    assert 'id="command-result-json"' in index.text
    assert 'id="permission-list"' in index.text
    assert 'id="event-inspector"' in index.text
    assert 'data-inspector-tab="business"' in index.text
    assert 'data-inspector-tab="envelope"' in index.text
    assert "<canvas" not in index.text.lower()
    assert script.status_code == 200
    assert "JSON.stringify(envelope, null, 2)" in script.text
    assert "eventDetailJson.textContent =" in script.text
    assert "commandResultJson.textContent =" in script.text
    assert "preview.textContent = permission.preview" in script.text
    assert "pollPendingPermissions(runContext)" in script.text
    assert ".innerHTML" not in script.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "http://localhost:20815"},
        {"origin": "https://127.0.0.1:20815"},
        {"origin": "http://127.0.0.1:20816"},
        {"origin": "http://127.0.0.1:20815/path"},
    ],
)
def test_state_change_requires_exact_same_origin(
    web_client: tuple[TestClient, _Commands], headers: dict[str, str]
) -> None:
    client, _commands = web_client

    response = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=headers
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin rejected"}
    assert SESSION_COOKIE not in client.cookies


def test_login_csrf_run_and_logout_session_flow(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, commands = web_client

    assert client.get("/api/auth/status").json() == {"authenticated": False}
    rejected = client.post(
        "/api/auth/login", json={"token": "0" * 64}, headers=SAME_ORIGIN
    )
    assert rejected.status_code == 401
    assert "set-cookie" not in rejected.headers

    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )

    assert login.status_code == 200
    assert PROJECT_TOKEN not in login.text
    csrf_token = login.json()["csrf_token"]
    assert client.cookies.get(SESSION_COOKIE)
    assert client.cookies.get(CSRF_COOKIE) == csrf_token
    set_cookies = login.headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    assert all("HttpOnly" in value for value in set_cookies)
    assert all("SameSite=strict" in value for value in set_cookies)

    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json()["csrf_token"] == csrf_token
    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json()["authenticated"] is True
    assert status.json()["csrf_token"] == csrf_token

    missing_csrf = client.post(
        "/api/runs", json={"message": "hello"}, headers=SAME_ORIGIN
    )
    assert missing_csrf.status_code == 403
    wrong_csrf = client.post(
        "/api/runs",
        json={"message": "hello"},
        headers={**SAME_ORIGIN, "x-csrf-token": "wrong-token"},
    )
    assert wrong_csrf.status_code == 403
    assert commands.started == []

    accepted = client.post(
        "/api/runs",
        json={"message": "hello"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    )
    assert accepted.status_code == 202
    assert accepted.json()["run_id"] == RUN_ID
    assert accepted.json()["events_url"] == f"/api/runs/{RUN_ID}/events"
    assert len(commands.started) == 1
    assert commands.started[0].message == "hello"

    logout = client.post(
        "/api/auth/logout",
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    )
    assert logout.status_code == 204
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_slash_commands_are_csrf_bound_and_never_become_model_turns(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, commands = web_client
    assert client.get("/api/commands").status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}
    registry = client.get("/api/commands")
    assert registry.status_code == 200
    assert [item["command_id"] for item in registry.json()["commands"]] == [
        "cancel", "clear", "compact", "context", "model", "permissions", "status"
    ]
    created = client.post(
        "/api/sessions", json={"title": "slash-boundary"}, headers=mutation
    ).json()
    session_id = created["session_id"]
    before = len(commands.started)

    missing_csrf = client.post(
        f"/api/sessions/{session_id}/commands",
        json={"command": "/status"},
        headers=SAME_ORIGIN,
    )
    assert missing_csrf.status_code == 403
    inline = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"message": "/status"},
        headers=mutation,
    )
    assert inline.status_code == 200
    value = inline.json()
    assert value["kind"] == "slash_command_result"
    assert value["command_id"] == "status"
    assert value["model_invoked"] is False
    assert value["turn_created"] is False
    assert len(commands.started) == before
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["messages"] == []

    compact = client.post(
        f"/api/sessions/{session_id}/commands",
        json={"command": "/compact"},
        headers=mutation,
    )
    assert compact.status_code == 200
    assert compact.json()["ui_effect"] == {"compact_next_turn": True}
    assert client.post(
        f"/api/sessions/{session_id}/runs",
        json={"message": "/status", "model_id": "qwen3.5:2b"},
        headers=mutation,
    ).status_code == 400
    assert client.post(
        f"/api/sessions/{session_id}/commands",
        json={"command": "/unknown"},
        headers=mutation,
    ).status_code == 400

    cleared = client.post(
        f"/api/sessions/{session_id}/commands",
        json={"command": "/clear"},
        headers=mutation,
    )
    assert cleared.status_code == 200
    assert cleared.json()["result"]["deleted"] is True
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_authenticated_model_catalog_is_content_bounded_and_selectable(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, commands = web_client
    assert client.get("/api/models").status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]

    listed = client.get("/api/models")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["default_model_id"] == "qwen3.5:2b"
    assert [item["model_id"] for item in payload["models"]] == ["qwen3.5:2b"]
    assert payload["models"][0]["profile_digest"]
    assert "iollama" not in listed.text
    assert "11434" not in listed.text

    started = client.post(
        "/api/runs",
        json={
            "message": "use catalog selection",
            "model_id": "qwen3.5:2b",
            "compact": True,
        },
        headers={**SAME_ORIGIN, "x-csrf-token": csrf},
    )
    assert started.status_code == 202
    assert commands.started[-1].model_id == "qwen3.5:2b"
    assert commands.started[-1].compact is True

    rejected = client.post(
        "/api/runs",
        json={
            "message": "reject endpoint injection",
            "model_id": "http://attacker.invalid/model",
        },
        headers={**SAME_ORIGIN, "x-csrf-token": csrf},
    )
    assert rejected.status_code == 400


def test_background_task_api_is_agent_scoped_bounded_and_csrf_protected(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}
    created = client.post(
        "/api/runs", json={"message": "parent"}, headers=mutation
    )
    assert created.status_code == 202

    endpoint = f"/api/agents/{PROTOTYPE_AGENT_ID}/runs/{RUN_ID}/tasks"
    assert client.post(
        endpoint,
        json={"command_id": "runtime-compile"},
        headers=SAME_ORIGIN,
    ).status_code == 403
    assert client.post(
        endpoint,
        json={"command_id": "../../bin/sh"},
        headers=mutation,
    ).status_code == 400
    submitted = client.post(
        endpoint,
        json={"command_id": "runtime-compile"},
        headers=mutation,
    )
    assert submitted.status_code == 202
    task_id = submitted.json()["task_id"]
    assert submitted.json()["parent_run_id"] == RUN_ID
    assert submitted.json()["state"] == "queued"

    listed = client.get(f"/api/agents/{PROTOTYPE_AGENT_ID}/tasks")
    assert [item["task_id"] for item in listed.json()["tasks"]] == [task_id]
    detail = client.get(f"/api/agents/{PROTOTYPE_AGENT_ID}/tasks/{task_id}")
    assert detail.json()["command_id"] == "runtime-compile"
    notices = client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/tasks/{task_id}/notifications"
    )
    assert notices.json()["notifications"][0]["kind"] == "task.queued"
    cancelled = client.post(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/tasks/{task_id}/cancel",
        headers=mutation,
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelled"
    assert client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/tasks/{'f' * 32}"
    ).status_code == 404

def test_authenticated_agent_lifecycle_api_is_csrf_protected_and_isolated(
    web_client: tuple[TestClient, _Commands], tmp_path: Path
) -> None:
    client, _commands = web_client
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    client.app.state.agent_registry = registry
    try:
        login = client.post(
            "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
        )
        csrf = login.json()["csrf_token"]
        mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}
        listed = client.get("/api/agents")
        assert listed.status_code == 200
        assert listed.json()["agents"][0]["agent_id"] == PROTOTYPE_AGENT_ID
        assert listed.json()["agents"][0]["display_name"] == SYSTEM_AGENT_DISPLAY_NAME
        assert client.post(
            "/api/agents", json={"display_name": "No CSRF"}, headers=SAME_ORIGIN
        ).status_code == 403
        created = client.post(
            "/api/agents",
            json={"display_name": "API Agent"},
            headers=mutation,
        )
        assert created.status_code == 201
        agent_id = created.json()["agent_id"]
        assert created.json()["generation"] == 1
        assert client.get(f"/api/agents/{agent_id}").status_code == 200
        assert client.patch(
            f"/api/agents/{agent_id}",
            json={"display_name": "Renamed API Agent"},
            headers=SAME_ORIGIN,
        ).status_code == 403
        renamed = client.patch(
            f"/api/agents/{agent_id}",
            json={"display_name": "Renamed API Agent"},
            headers=mutation,
        )
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "Renamed API Agent"
        assert renamed.json()["generation"] == 1
        protected_rename = client.patch(
            f"/api/agents/{PROTOTYPE_AGENT_ID}",
            json={"display_name": "Renamed system"},
            headers=mutation,
        )
        assert protected_rename.status_code == 409
        assert protected_rename.json()["detail"] == (
            "the system Agent cannot be renamed"
        )
        upgraded = client.post(
            f"/api/agents/{agent_id}/upgrade",
            json={},
            headers=mutation,
        )
        assert upgraded.status_code == 200
        assert upgraded.json()["generation"] == 2
        assert upgraded.json()["display_name"] == "Renamed API Agent"
        protected = client.delete(
            f"/api/agents/{PROTOTYPE_AGENT_ID}", headers=mutation
        )
        assert protected.status_code == 409
        assert protected.json()["detail"] == "the system Agent cannot be deleted"
        assert client.delete(f"/api/agents/{agent_id}", headers=mutation).status_code == 204
        assert client.get(f"/api/agents/{agent_id}").status_code == 404
    finally:
        registry.close()


def test_agent_scoped_session_run_stream_and_context_do_not_cross_agents(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    agent_id = "a" * 32
    service = _RunService(agent_id)
    engines = QueryEngineRegistry(service, agent_id)  # type: ignore[arg-type]
    commands = _Commands(engines)
    manager = client.app.state.runtime_manager
    manager.register(agent_id, service, engines, commands)
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}

    created = client.post(
        f"/api/agents/{agent_id}/sessions",
        json={"title": "Scoped"},
        headers=mutation,
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    renamed_session = client.patch(
        f"/api/agents/{agent_id}/sessions/{session_id}",
        json={"title": "Scoped renamed", "revision": created.json()["revision"]},
        headers=mutation,
    )
    assert renamed_session.status_code == 200
    assert renamed_session.json()["title"] == "Scoped renamed"
    scoped_detail = client.get(
        f"/api/agents/{agent_id}/sessions/{session_id}?limit=1"
    )
    assert scoped_detail.status_code == 200
    assert scoped_detail.json()["page"]["limit"] == 1
    started = client.post(
        f"/api/agents/{agent_id}/sessions/{session_id}/runs",
        json={"message": "isolated", "model_id": "qwen3.5:2b", "compact": True},
        headers=mutation,
    )
    assert started.status_code == 202
    assert started.json()["agent_id"] == agent_id
    assert commands.started[-1].model_id == "qwen3.5:2b"
    assert commands.started[-1].compact is True
    assert started.json()["events_url"].startswith(
        f"/api/agents/{agent_id}/runs/"
    )
    streamed = client.get(started.json()["events_url"])
    assert streamed.status_code == 200
    assert '"agent_id":"' + agent_id + '"' in streamed.text
    context = client.get(f"/api/agents/{agent_id}/runs/{RUN_ID}/context")
    assert context.status_code == 200
    assert context.json()["identity"]["agent_id"] == agent_id

    assert client.get(f"/api/runs/{RUN_ID}/context").status_code == 404
    assert client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/sessions/{session_id}"
    ).status_code == 404
    assert client.post(
        f"/api/agents/{agent_id}/sessions/{session_id}/runs",
        json={"message": "invalid", "compact": "yes"},
        headers=mutation,
    ).status_code == 400
    assert client.post(
        f"/api/agents/{agent_id}/sessions/{session_id}/runs",
        json={"message": "invalid", "semantic_summary": True},
        headers=mutation,
    ).status_code == 400


def test_permission_approval_is_authenticated_csrf_bound_and_operator_safe(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    service = client.app.state.run_service
    permission_id = "d" * 32
    service.permissions[permission_id] = PermissionRecord(
        permission_id=permission_id,
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        conversation_id="2" * 32,
        turn_id="3" * 32,
        run_id=RUN_ID,
        call_id="call-permission-1",
        capability_id="file/write",
        toolset_digest="a" * 64,
        policy_digest="b" * 64,
        arguments_digest="c" * 64,
        preview="Write workspace/example.txt (5 UTF-8 bytes)",
        preview_digest="e" * 64,
        policy_decision="ask",
        status="pending",
        expires_at_milliseconds=1_800_000_060_000,
        created_at="2026-07-20T00:00:00.000Z",
        resolved_at=None,
        resolution_source=None,
    )
    service.run_identities[RUN_ID] = RunIdentity(
        PROTOTYPE_AGENT_ID, "2" * 32, "3" * 32, RUN_ID
    )

    collection = f"/api/agents/{PROTOTYPE_AGENT_ID}/permissions"
    member = f"{collection}/{permission_id}"
    assert client.get(collection).status_code == 401
    assert client.post(collection, json={}, headers=SAME_ORIGIN).status_code == 405

    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}
    listed = client.get(collection)
    assert listed.status_code == 200
    projection = listed.json()["permissions"][0]
    assert projection["preview"] == "Write workspace/example.txt (5 UTF-8 bytes)"
    assert "arguments" not in projection
    assert "result" not in projection
    audit = client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/runs/{RUN_ID}/capability-audit"
    )
    assert audit.status_code == 200
    assert audit.json()["events"] == []
    assert client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/runs/{RUN_ID}/capability-audit?limit=129"
    ).status_code == 400

    assert client.post(
        member, json={"decision": "approve"}, headers=SAME_ORIGIN
    ).status_code == 403
    assert client.post(
        member, json={"decision": "invent"}, headers=mutation
    ).status_code == 400
    assert client.post(
        member,
        json={"decision": "approve", "internal_approval": True},
        headers=mutation,
    ).status_code == 400
    approved = client.post(
        member, json={"decision": "approve"}, headers=mutation
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["resolution_source"] == "operator"
    assert client.post(
        member, json={"decision": "deny"}, headers=mutation
    ).status_code == 409
    assert client.get(collection).json() == {"permissions": []}
    assert client.get(
        f"/api/agents/{'f' * 32}/permissions"
    ).status_code == 404
    assert client.post(
        f"{collection}/not-an-id",
        json={"decision": "approve"},
        headers=mutation,
    ).status_code == 404


def test_operator_chosen_access_token_can_create_a_web_session(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    operator_token = "operator-token_2026"
    client.app.state.sessions = SessionService(operator_token)

    rejected = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert rejected.status_code == 401
    accepted = client.post(
        "/api/auth/login", json={"token": operator_token}, headers=SAME_ORIGIN
    )
    assert accepted.status_code == 200
    assert operator_token not in accepted.text


def test_authenticated_conversation_create_restore_multiturn_and_delete_api(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, commands = web_client
    assert client.get("/api/sessions").status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}

    created = client.post(
        "/api/sessions",
        json={"title": "持续会话"},
        headers=mutation_headers,
    )
    assert created.status_code == 201
    conversation_id = created.json()["session_id"]
    assert created.json()["state"] == "idle"

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert [item["session_id"] for item in listed.json()["sessions"]] == [
        conversation_id
    ]

    run = client.post(
        f"/api/sessions/{conversation_id}/runs",
        json={"message": "第一轮"},
        headers=mutation_headers,
    )
    assert run.status_code == 202
    assert run.json() == {
        "run_id": RUN_ID,
        "session_id": conversation_id,
        "events_url": f"/api/runs/{RUN_ID}/events",
    }
    assert commands.started[-1].conversation_id == conversation_id

    service = client.app.state.run_service
    service.conversations[conversation_id] = Conversation(
        conversation_id=conversation_id,
        agent_id=PROTOTYPE_AGENT_ID,
        title="持续会话",
        created_at="2026-07-18T00:00:00.000Z",
        updated_at="2026-07-18T00:00:00.500Z",
        revision=1,
        active_run_id=RUN_ID,
        turns=(
            ConversationTurn(
                turn_id="5" * 32,
                conversation_id=conversation_id,
                run_id=RUN_ID,
                position=1,
                status="running",
                user_content="第一轮",
                assistant_content=None,
                created_at="2026-07-18T00:00:00.100Z",
                updated_at="2026-07-18T00:00:00.100Z",
            ),
        ),
    )
    running = client.get(f"/api/sessions/{conversation_id}")
    assert running.status_code == 200
    assert running.json()["session"]["state"] == "running"
    assert running.json()["messages"] == [
        {
            "message_id": running.json()["messages"][0]["message_id"],
            "role": "user",
            "content": "第一轮",
            "created_at": "2026-07-18T00:00:00.100Z",
            "turn_id": "5" * 32,
            "run_id": RUN_ID,
            "turn_position": 1,
            "turn_status": "running",
        }
    ]

    service.conversations[conversation_id] = Conversation(
        conversation_id=conversation_id,
        agent_id=PROTOTYPE_AGENT_ID,
        title="持续会话",
        created_at="2026-07-18T00:00:00.000Z",
        updated_at="2026-07-18T00:00:01.000Z",
        revision=2,
        active_run_id=None,
        turns=(
            ConversationTurn(
                turn_id="5" * 32,
                conversation_id=conversation_id,
                run_id=RUN_ID,
                position=1,
                status="completed",
                user_content="第一轮",
                assistant_content="第一轮回答",
                created_at="2026-07-18T00:00:00.100Z",
                updated_at="2026-07-18T00:00:01.000Z",
            ),
        ),
    )
    restored = client.get(f"/api/sessions/{conversation_id}")
    assert restored.status_code == 200
    assert [message["role"] for message in restored.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert restored.json()["messages"][1]["content"] == "第一轮回答"
    assert {
        message["turn_status"] for message in restored.json()["messages"]
    } == {"completed"}

    missing_csrf = client.delete(
        f"/api/sessions/{conversation_id}", headers=SAME_ORIGIN
    )
    assert missing_csrf.status_code == 403
    deleted = client.delete(
        f"/api/sessions/{conversation_id}", headers=mutation_headers
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/sessions/{conversation_id}").status_code == 404


def test_preview_is_authenticated_no_store_and_continuation_is_explicit(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    assert client.get(
        f"/api/sessions/{'4' * 32}/context-preview"
    ).status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
    created = client.post(
        "/api/sessions", json={"title": "source"}, headers=mutation_headers
    )
    source_id = created.json()["session_id"]

    preview = client.get(
        f"/api/sessions/{source_id}/context-preview?model_id=qwen3.5%3A2b"
    )
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert preview.json()["conversation_id"] == source_id
    assert preview.json()["single_message_byte_limit"] == 8_192
    assert "prompt" not in preview.text.lower()
    assert client.get(
        f"/api/sessions/{source_id}/context-preview?unexpected=1"
    ).status_code == 400

    assert client.post(
        f"/api/sessions/{source_id}/continue", json={"title": "next"},
        headers=SAME_ORIGIN,
    ).status_code == 403
    continued = client.post(
        f"/api/sessions/{source_id}/continue", json={"title": "next"},
        headers=mutation_headers,
    )
    assert continued.status_code == 201
    assert continued.json()["session_id"] == "6" * 32
    assert continued.json()["continuation"] == {
        "source_session_id": source_id,
        "included_completed_turns": 2,
        "omitted_completed_turns": 3,
        "authority": "untrusted_conversation_data",
    }
    assert continued.json()["relationship"] == {
        "version": "conversation-relationship-v1",
        "type": "continue",
        "source_session_id": source_id,
        "source_preserved": True,
        "branch_point": "completed_head",
        "context_transfer": {
            "type": "bounded_completed_turn_projection",
            "included_completed_turns": 2,
            "omitted_completed_turns": 3,
            "authority": "untrusted_conversation_data",
        },
    }
    branched = client.post(
        f"/api/sessions/{source_id}/continue",
        json={"mode": "branch"},
        headers=mutation_headers,
    )
    assert branched.status_code == 201
    assert branched.json()["title"] == "分支会话"
    assert branched.json()["relationship"]["type"] == "branch"
    invalid_mode = client.post(
        f"/api/sessions/{source_id}/continue",
        json={"mode": "copy"},
        headers=mutation_headers,
    )
    assert invalid_mode.status_code == 400
    assert invalid_mode.json()["detail"]["code"] == (
        "invalid_session_relationship"
    )
    assert client.get(f"/api/sessions/{source_id}").status_code == 200


def test_preparation_progress_is_authenticated_bounded_and_agent_scoped(
    web_client: tuple[TestClient, _Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _commands = web_client
    conversation_id = "4" * 32
    assert client.get(
        f"/api/sessions/{conversation_id}/preparation"
    ).status_code == 401

    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert login.status_code == 200
    mutation_headers = {
        **SAME_ORIGIN,
        "x-csrf-token": login.json()["csrf_token"],
    }
    service = client.app.state.run_service
    created = client.post(
        "/api/sessions",
        json={"title": "preparation"},
        headers=mutation_headers,
    )
    assert created.status_code == 201
    conversation_id = created.json()["session_id"]

    idle = client.get(f"/api/sessions/{conversation_id}/preparation")
    assert idle.status_code == 200
    assert idle.headers["cache-control"] == "no-store"
    assert idle.json() == {
        "version": "run-preparation-v1",
        "state": "idle",
        "operation_id": None,
        "stage": None,
        "elapsed_ms": 0,
    }

    operation_id = "7" * 32
    service.preparations[conversation_id] = {
        "version": "run-preparation-v1",
        "state": "preparing",
        "operation_id": operation_id,
        "stage": "summarizing_history",
        "elapsed_ms": 1_234,
    }
    preparing = client.get(f"/api/sessions/{conversation_id}/preparation")
    assert preparing.status_code == 200
    assert preparing.json() == service.preparations[conversation_id]
    assert "prompt" not in preparing.text.lower()
    assert "message" not in preparing.text.lower()
    cancel_path = f"/api/sessions/{conversation_id}/preparation/cancel"
    assert client.post(
        cancel_path, json={"operation_id": operation_id}
    ).status_code == 403
    cancelled = client.post(
        cancel_path,
        json={"operation_id": operation_id},
        headers=mutation_headers,
    )
    assert cancelled.status_code == 202
    assert cancelled.headers["cache-control"] == "no-store"
    assert cancelled.json() == {
        "version": "run-preparation-cancel-v1",
        "state": "cancellation_requested",
        "target": "preparation",
    }
    assert service.preparation_cancellations == [conversation_id]
    stale = client.post(
        cancel_path,
        json={"operation_id": "6" * 32},
        headers=mutation_headers,
    )
    assert stale.status_code == 202
    assert stale.json() == {
        "version": "run-preparation-cancel-v1",
        "state": "stale",
        "target": None,
    }
    assert client.post(
        cancel_path, json={"unexpected": True}, headers=mutation_headers
    ).status_code == 400
    assert client.post(
        f"{cancel_path}?unexpected=1",
        json={"operation_id": operation_id},
        headers=mutation_headers,
    ).status_code == 404
    service.preparations.pop(conversation_id)
    idle_cancel = client.post(
        cancel_path,
        json={"operation_id": operation_id},
        headers=mutation_headers,
    )
    assert idle_cancel.status_code == 202
    assert idle_cancel.json() == {
        "version": "run-preparation-cancel-v1",
        "state": "idle",
        "target": None,
    }
    assert client.post(
        f"/api/sessions/{'f' * 32}/preparation/cancel",
        json={"operation_id": operation_id},
        headers=mutation_headers,
    ).status_code == 404
    assert client.get(
        f"/api/sessions/{conversation_id}/preparation?unexpected=1"
    ).status_code == 404
    assert client.get(
        f"/api/sessions/{'f' * 32}/preparation"
    ).status_code == 404

    agent_id = "b" * 32
    agent_service = _RunService(agent_id)
    engines = QueryEngineRegistry(agent_service, agent_id)  # type: ignore[arg-type]
    commands = _Commands(engines)
    client.app.state.runtime_manager.register(agent_id, agent_service, engines, commands)
    agent_created = client.post(
        f"/api/agents/{agent_id}/sessions",
        json={"title": "agent preparation"},
        headers=mutation_headers,
    )
    agent_conversation_id = agent_created.json()["session_id"]
    agent_operation_id = "8" * 32
    agent_service.preparations[agent_conversation_id] = {
        "version": "run-preparation-v1",
        "state": "preparing",
        "operation_id": agent_operation_id,
        "stage": "admitting_run",
        "elapsed_ms": 17,
    }
    scoped = client.get(
        f"/api/agents/{agent_id}/sessions/{agent_conversation_id}/preparation"
    )
    assert scoped.status_code == 200
    assert scoped.json()["stage"] == "admitting_run"
    agent_cancelled = client.post(
        f"/api/agents/{agent_id}/sessions/{agent_conversation_id}/preparation/cancel",
        json={"operation_id": agent_operation_id},
        headers=mutation_headers,
    )
    assert agent_cancelled.status_code == 202
    assert agent_cancelled.json()["target"] == "preparation"
    assert agent_service.preparation_cancellations == [agent_conversation_id]
    assert client.post(
        f"/api/agents/{'c' * 32}/sessions/"
        f"{agent_conversation_id}/preparation/cancel",
        json={"operation_id": agent_operation_id},
        headers=mutation_headers,
    ).status_code == 404
    service.preparations[agent_conversation_id] = {
        "version": "run-preparation-v1",
        "state": "preparing",
        "operation_id": operation_id,
        "stage": "summarizing_history",
        "elapsed_ms": 1_234,
    }
    root_scoped = client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/sessions/"
        f"{agent_conversation_id}/preparation"
    )
    assert root_scoped.status_code == 200
    assert root_scoped.json()["stage"] == "summarizing_history"

    async def unavailable(
        _conversation_id: str, _operation_id: str
    ) -> dict[str, object]:
        raise ConversationStoreUnavailableError("injected unavailable store")

    monkeypatch.setattr(agent_service, "cancel_preparation", unavailable)
    unavailable_response = client.post(
        f"/api/agents/{agent_id}/sessions/"
        f"{agent_conversation_id}/preparation/cancel",
        json={"operation_id": agent_operation_id},
        headers=mutation_headers,
    )
    assert unavailable_response.status_code == 503
    assert unavailable_response.json() == {
        "detail": "preparation cancellation is unavailable"
    }


def test_session_rename_is_csrf_bound_typed_and_persistent_in_detail(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    mutation = {**SAME_ORIGIN, "x-csrf-token": login.json()["csrf_token"]}
    created = client.post(
        "/api/sessions", json={"title": "before"}, headers=mutation
    )
    conversation_id = created.json()["session_id"]

    assert client.patch(
        f"/api/sessions/{conversation_id}",
        json={"title": "after", "revision": created.json()["revision"]},
        headers=SAME_ORIGIN,
    ).status_code == 403
    renamed = client.patch(
        f"/api/sessions/{conversation_id}",
        json={"title": "研究会话", "revision": created.json()["revision"]},
        headers=mutation,
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "研究会话"
    assert renamed.json()["revision"] == created.json()["revision"] + 1
    assert client.get(
        f"/api/sessions/{conversation_id}"
    ).json()["session"]["title"] == "研究会话"

    missing_revision = client.patch(
        f"/api/sessions/{conversation_id}",
        json={"title": "missing CAS"},
        headers=mutation,
    )
    assert missing_revision.status_code == 400
    assert missing_revision.json()["detail"]["code"] == "invalid_session_rename"
    stale = client.patch(
        f"/api/sessions/{conversation_id}",
        json={"title": "stale", "revision": created.json()["revision"]},
        headers=mutation,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "session_rename_conflict"

    invalid = client.patch(
        f"/api/sessions/{conversation_id}",
        json={"title": "  ", "revision": renamed.json()["revision"]},
        headers=mutation,
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == {
        "code": "invalid_session_title",
        "message": "title must be a non-empty string within the limit",
    }
    missing = client.patch(
        f"/api/sessions/{'f' * 32}",
        json={"title": "hidden", "revision": 0},
        headers=mutation,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "session_not_found"


def test_web_rename_helper_never_penetrates_runtime_store(tmp_path: Path) -> None:
    from agent_builder_v2.sessions import ConversationStore

    data_root = tmp_path / PROTOTYPE_AGENT_ID
    data_root.mkdir(mode=0o700)
    store = ConversationStore(data_root / "state.sqlite", PROTOTYPE_AGENT_ID)
    conversation = store.create_conversation("store title")

    class RuntimeBoundary:
        def __init__(self) -> None:
            self.conversations = store
            self.calls: list[tuple[str, str, int]] = []

        async def rename_conversation(
            self,
            conversation_id: str,
            title: str,
            *,
            expected_revision: int,
        ) -> Conversation:
            self.calls.append((conversation_id, title, expected_revision))
            return replace(
                conversation,
                title="service title",
                revision=expected_revision + 1,
            )

    runtime = RuntimeBoundary()
    try:
        renamed = asyncio.run(
            _rename_runtime_conversation(
                runtime,
                conversation.conversation_id,
                "requested title",
                expected_revision=conversation.revision,
            )
        )
        assert renamed.title == "service title"
        assert runtime.calls == [
            (
                conversation.conversation_id,
                "requested title",
                conversation.revision,
            )
        ]
        assert store.get_conversation(conversation.conversation_id).title == (
            "store title"
        )
    finally:
        store.close()


def test_session_detail_uses_stable_latest_turn_pagination_and_safe_terminal(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    mutation = {**SAME_ORIGIN, "x-csrf-token": login.json()["csrf_token"]}
    created = client.post(
        "/api/sessions", json={"title": "long"}, headers=mutation
    )
    conversation_id = created.json()["session_id"]
    service = client.app.state.run_service

    turns = tuple(
        ConversationTurn(
            turn_id=f"{1_000 + position:032x}",
            conversation_id=conversation_id,
            run_id=f"{2_000 + position:032x}",
            position=position,
            status="failed" if position == 40 else "completed",
            user_content=f"user-{position}",
            assistant_content=(None if position == 40 else f"assistant-{position}"),
            created_at=f"2026-07-21T00:{position // 60:02d}:{position % 60:02d}.000Z",
            updated_at=f"2026-07-21T00:{position // 60:02d}:{position % 60:02d}.500Z",
            terminal=(
                TurnTerminalSummary(
                    "model_first_frame_timeout", "model", True, 120_000
                )
                if position == 40
                else None
            ),
        )
        for position in range(1, 71)
    )
    service.conversations[conversation_id] = replace(
        service.conversations[conversation_id],
        revision=140,
        turns=turns,
    )

    latest = client.get(f"/api/sessions/{conversation_id}")
    assert latest.status_code == 200
    assert service.get_conversation_page_calls == 1
    assert service.get_conversation_calls == 0
    page = latest.json()["page"]
    assert page == {
        "version": "turn-page-v2",
        "limit": 32,
        "before_cursor": None,
        "returned_turns": 32,
        "total_turns": 70,
        "oldest_position": 39,
        "newest_position": 70,
        "has_older": True,
        "has_newer": False,
        "next_before_cursor": page["next_before_cursor"],
    }
    cursor = page["next_before_cursor"]
    assert isinstance(cursor, str)
    assert cursor != "39"
    assert 64 < len(cursor) <= 256
    assert latest.json()["messages"][0]["content"] == "user-39"
    failed = next(
        item for item in latest.json()["messages"]
        if item["turn_status"] == "failed"
    )
    assert failed["terminal"] == {
        "version": "turn-terminal-v1",
        "code": "model_first_frame_timeout",
        "stage": "model",
        "retryable": True,
        "duration_ms": 120_000,
    }
    assert "message" not in failed["terminal"]

    older_url = f"/api/sessions/{conversation_id}?limit=10&before={cursor}"
    older = client.get(older_url)
    assert [
        item["content"] for item in older.json()["messages"] if item["role"] == "user"
    ] == [f"user-{position}" for position in range(29, 39)]
    assert older.json()["page"]["before_cursor"] == cursor
    assert isinstance(older.json()["page"]["next_before_cursor"], str)

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    for invalid_cursor in (tampered, "39"):
        invalid = client.get(
            f"/api/sessions/{conversation_id}?before={invalid_cursor}"
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "invalid_session_cursor"

    other_id = "5" * 32
    service.conversations[other_id] = replace(
        service.conversations[conversation_id],
        conversation_id=other_id,
        turns=tuple(replace(turn, conversation_id=other_id) for turn in turns),
    )
    cross_conversation = client.get(
        f"/api/sessions/{other_id}?before={cursor}"
    )
    assert cross_conversation.status_code == 400
    assert cross_conversation.json()["detail"]["code"] == (
        "invalid_session_cursor"
    )

    service.conversations[conversation_id] = replace(
        service.conversations[conversation_id],
        revision=141,
        turns=(*turns, replace(turns[-1], position=71, turn_id="a" * 32)),
    )
    revision_drift = client.get(older_url)
    assert revision_drift.status_code == 400
    assert revision_drift.json()["detail"]["code"] == "invalid_session_cursor"

    service.conversations[conversation_id] = replace(
        service.conversations[conversation_id], revision=140, turns=turns
    )
    delete_cursor = client.get(
        f"/api/sessions/{conversation_id}"
    ).json()["page"]["next_before_cursor"]
    deleted = service.conversations.pop(conversation_id)
    deletion_drift = client.get(
        f"/api/sessions/{conversation_id}?before={delete_cursor}"
    )
    assert deletion_drift.status_code == 400
    assert deletion_drift.json()["detail"]["code"] == "invalid_session_cursor"

    service.conversations[conversation_id] = deleted
    restart_cursor = client.get(
        f"/api/sessions/{conversation_id}"
    ).json()["page"]["next_before_cursor"]
    client.app.state.session_cursor_key = b"r" * 32
    restart_drift = client.get(
        f"/api/sessions/{conversation_id}?before={restart_cursor}"
    )
    assert restart_drift.status_code == 400
    assert restart_drift.json()["detail"]["code"] == "invalid_session_cursor"

    invalid = client.get(f"/api/sessions/{conversation_id}?limit=65")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_session_page"


def test_turn_capacity_has_a_stable_api_error_before_run_creation(
    web_client: tuple[TestClient, _Commands], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    headers = {**SAME_ORIGIN, "x-csrf-token": login.json()["csrf_token"]}
    created = client.post(
        "/api/sessions", json={"title": "full"}, headers=headers
    )
    conversation_id = created.json()["session_id"]

    async def reject(_command: StartRunCommand) -> object:
        raise ConversationTurnCapacityError("turn capacity is exhausted")

    monkeypatch.setattr(commands, "start", reject)
    response = client.post(
        f"/api/sessions/{conversation_id}/runs",
        json={"message": "must not run"},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "conversation_turn_capacity_exhausted",
            "message": "conversation turn capacity is exhausted",
            "turn_limit": 128,
        }
    }


def test_authenticated_sse_and_cancel_use_query_engine_ownership(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
    started = client.post(
        "/api/runs",
        json={"message": "query engine route"},
        headers=mutation_headers,
    )
    assert started.status_code == 202

    events = client.get(started.json()["events_url"])
    assert events.status_code == 200
    assert "event: run.completed" in events.text
    assert f'"run_id":"{RUN_ID}"' in events.text
    assert client.app.state.run_service.streamed == [(RUN_ID, 0)]
    assert client.app.state.run_service.replayed == []

    cancelled = client.post(
        f"/api/runs/{RUN_ID}/cancel", headers=mutation_headers
    )
    assert cancelled.status_code == 202
    assert cancelled.json() == {"accepted": True}
    assert client.app.state.run_service.cancelled == [RUN_ID]


def test_authenticated_durable_replay_pages_without_live_run_record(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    started = client.post(
        "/api/runs",
        json={"message": "durable replay"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    )
    assert started.status_code == 202
    client.app.state.run_service.runs.clear()

    first = client.get(f"/api/runs/{RUN_ID}/replay?after=0&limit=2")
    second = client.get(f"/api/runs/{RUN_ID}/replay?after=2&limit=2")

    assert first.status_code == 200
    assert first.json()["identity"] == {
        "agent_id": PROTOTYPE_AGENT_ID,
        "conversation_id": "2" * 32,
        "turn_id": "3" * 32,
        "run_id": RUN_ID,
    }
    assert first.json()["availability"] == "complete"
    assert first.json()["oldest_cursor"] == 0
    assert first.json()["latest_cursor"] == 4
    assert first.json()["next_cursor"] == 2
    assert first.json()["has_more"] is True
    assert [event["seq"] for event in first.json()["events"]] == [1, 2]
    assert first.json()["snapshot"]["through_seq"] == 4
    assert first.json()["snapshot"]["document"]["terminal"]["kind"] == (
        "run.completed"
    )
    assert second.status_code == 200
    assert second.json()["next_cursor"] == 4
    assert second.json()["has_more"] is False
    assert [event["seq"] for event in second.json()["events"]] == [3, 4]


def test_authenticated_retained_context_is_exact_no_store_and_withheld(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    secret = "operator-prompt-secret-83"
    assert client.post(
        "/api/runs",
        json={"message": secret},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["identity"] == {
        "agent_id": PROTOTYPE_AGENT_ID,
        "conversation_id": "2" * 32,
        "turn_id": "3" * 32,
        "run_id": RUN_ID,
    }
    assert payload["availability"] == "exact"
    assert payload["content_exposure"] == "withheld"
    assert payload["provider_message_count"] == 2
    assert "provider_messages" not in payload
    assert payload["renderer"]["version"] == "ordered-sections-v7"
    assert (
        payload["renderer"]["section_registry_version"]
            == "prompt-section-registry-v6"
    )
    assert payload["renderer"]["leading_system_sections_merged"] is True
    assert payload["renderer"]["leading_system_section_count"] == 2
    assert [section["id"] for section in payload["sections"]] == [
        "platform.contract",
        "agent.instructions",
        "turn.user",
    ]
    assert all("content" not in section for section in payload["sections"])
    assert all(
        "content_bytes" in section
        and "content_digest" in section
        and "dependency_digest" in section
        and "budget_tokens" in section
        and "truncation_reason" in section
        for section in payload["sections"]
    )
    turn_section = next(
        section for section in payload["sections"] if section["id"] == "turn.user"
    )
    digest_input = (
        b"agent-builder-context-section-inspection-v1\0"
        + secret.encode("utf-8")
    )
    assert turn_section["content_digest"] != hashlib.sha256(
        digest_input
    ).hexdigest()
    assert turn_section["content_digest"] != hmac.new(
        PROJECT_TOKEN.encode("utf-8"), digest_input, hashlib.sha256
    ).hexdigest()
    assert "keyed inspection digests" in payload["notice"]
    assert secret not in response.text

    elevated = client.get(f"/api/runs/{RUN_ID}/context?include_content=true")
    assert elevated.status_code == 400


def test_context_reveal_requires_independent_token_audits_and_redacts(
    web_client: tuple[TestClient, _Commands], tmp_path: Path
) -> None:
    client, _commands = web_client
    policy = ContextRevealPolicy(tmp_path, enabled=True)
    client.app.state.context_reveal = policy
    try:
        login = client.post(
            "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
        )
        csrf_token = login.json()["csrf_token"]
        secret = "token=0123456789abcdef0123456789abcdef"
        assert client.post(
            "/api/runs",
            json={"message": f"{secret} visible"},
            headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
        ).status_code == 202
        endpoint = f"/api/runs/{RUN_ID}/context/reveal"
        mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
        assert client.post(endpoint, headers=mutation_headers).status_code == 403
        token = tmp_path.joinpath(
            ".runtime", "secrets", "context-reveal-token"
        ).read_text(encoding="ascii").strip()
        response = client.post(
            endpoint,
            headers={
                **mutation_headers,
                "x-context-operator-token": token,
            },
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["content_exposure"] == "redacted_excerpt"
        assert len(payload["audit_id"]) == 32
        assert secret not in response.text
        assert "[REDACTED]" in response.text
        assert payload["sections"][0]["exposure"] == "withheld"
        assert payload["sections"][1]["exposure"] == "withheld"
    finally:
        policy.close()


def test_context_reveal_is_hidden_when_disabled_and_rejects_evicted_plan(
    web_client: tuple[TestClient, _Commands], tmp_path: Path
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
    assert client.post(
        "/api/runs",
        json={"message": "bounded reveal"},
        headers=mutation_headers,
    ).status_code == 202
    endpoint = f"/api/runs/{RUN_ID}/context/reveal"

    disabled = ContextRevealPolicy(tmp_path, enabled=False)
    client.app.state.context_reveal = disabled
    try:
        assert client.post(endpoint, headers=mutation_headers).status_code == 404
    finally:
        disabled.close()

    enabled = ContextRevealPolicy(tmp_path, enabled=True)
    client.app.state.context_reveal = enabled
    try:
        token = tmp_path.joinpath(
            ".runtime", "secrets", "context-reveal-token"
        ).read_text(encoding="ascii").strip()
        client.app.state.run_service.runs.clear()
        response = client.post(
            endpoint,
            headers={
                **mutation_headers,
                "x-context-operator-token": token,
            },
        )
        assert response.status_code == 409
        assert token not in response.text
    finally:
        enabled.close()


def test_historical_context_falls_back_to_validated_summary_only(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "evicted exact context"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    expected_context_plan = dict(
        service.durable_events[RUN_ID][0].payload["context_plan"]
    )
    service.runs.clear()
    service.snapshot_only = True

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["availability"] == "summary_only"
    assert payload["context_plan"] == expected_context_plan
    assert payload["provider_message_count"] is None
    assert payload["renderer"]["version"] is None
    assert payload["sections"] == []
    assert payload["content_exposure"] == "unavailable"
    assert "no longer resident" in payload["notice"]
    assert service.replayed == [(RUN_ID, 0, 1)]


def test_context_retention_race_falls_back_but_foreign_record_does_not(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "second lookup eviction"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.evict_on_get_call = 2

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 200
    assert response.json()["availability"] == "summary_only"
    assert service.replayed == [(RUN_ID, 0, 1)]


def test_context_second_lookup_foreign_record_stays_not_found(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "second lookup foreign replacement"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.foreign_on_get_call = 2

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}
    assert service.replayed == []


def test_context_route_maps_missing_conflict_and_unavailable_states(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.get(f"/api/runs/{RUN_ID}/context").status_code == 404
    assert client.post(
        "/api/runs",
        json={"message": "context failures"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service

    original = service.runs[RUN_ID]
    service.runs[RUN_ID] = replace(
        original, context_plan=_context_plan("different valid plan")
    )
    swapped_plan = client.get(f"/api/runs/{RUN_ID}/context")
    assert swapped_plan.status_code == 503
    assert swapped_plan.json() == {"detail": "run context is unavailable"}

    service.runs[RUN_ID] = replace(original, context_plan=None)
    unavailable_exact = client.get(f"/api/runs/{RUN_ID}/context")
    assert unavailable_exact.status_code == 503
    assert unavailable_exact.json() == {"detail": "run context is unavailable"}

    service.runs.clear()
    service.replay_error = ConversationConflictError("snapshot unavailable")
    conflict = client.get(f"/api/runs/{RUN_ID}/context")
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "run context is unavailable"}

    service.replay_error = ConversationStoreUnavailableError("store offline")
    unavailable_summary = client.get(f"/api/runs/{RUN_ID}/context")
    assert unavailable_summary.status_code == 503
    assert unavailable_summary.json() == {
        "detail": "run context is unavailable"
    }

    service.replay_error = None
    service.run_identities.clear()
    assert client.get(f"/api/runs/{RUN_ID}/context").status_code == 404
    assert client.get("/api/runs/not-a-run/context").status_code == 404


def test_sse_falls_back_to_paginated_durable_replay_without_terminal_repeat(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "long durable stream"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    identity = service.run_identities[RUN_ID]

    events = [
        EventEnvelope(
            event_id=f"{1_000:032x}",
            agent_id=identity.agent_id,
            conversation_id=identity.conversation_id,
            turn_id=identity.turn_id,
            run_id=identity.run_id,
            seq=1,
            occurred_at="2026-07-18T00:00:00.001Z",
            kind="run.started",
            durability="durable",
            payload=_started_payload(),
        )
    ]
    sequence = 2
    for index in range(65):
        block_id = f"block-{index}"
        events.append(
            EventEnvelope(
                event_id=f"{1_000 + sequence:032x}",
                agent_id=identity.agent_id,
                conversation_id=identity.conversation_id,
                turn_id=identity.turn_id,
                run_id=identity.run_id,
                seq=sequence,
                occurred_at=f"2026-07-18T00:00:00.{sequence:03d}Z",
                kind="assistant.block.started",
                durability="durable",
                payload={"block_id": block_id, "block_type": "content"},
            )
        )
        sequence += 1
        events.append(
            EventEnvelope(
                event_id=f"{1_000 + sequence:032x}",
                agent_id=identity.agent_id,
                conversation_id=identity.conversation_id,
                turn_id=identity.turn_id,
                run_id=identity.run_id,
                seq=sequence,
                occurred_at=f"2026-07-18T00:00:00.{sequence:03d}Z",
                kind="assistant.block.finished",
                durability="durable",
                payload={"block_id": block_id, "content": str(index)},
            )
        )
        sequence += 1
    events.append(
        EventEnvelope(
            event_id=f"{1_000 + sequence:032x}",
            agent_id=identity.agent_id,
            conversation_id=identity.conversation_id,
            turn_id=identity.turn_id,
            run_id=identity.run_id,
            seq=sequence,
            occurred_at=f"2026-07-18T00:00:00.{sequence:03d}Z",
            kind="run.completed",
            durability="durable",
            payload=_completed_payload(),
        )
    )
    terminal_seq = sequence
    service.durable_events[RUN_ID] = tuple(events)
    service.runs.clear()

    replayed = client.get(f"/api/runs/{RUN_ID}/events")

    assert replayed.status_code == 200
    assert replayed.text.count("event: run.completed") == 1
    assert f"id: {terminal_seq}\nevent: run.completed" in replayed.text
    assert service.streamed == []
    assert service.replayed[:2] == [
        (RUN_ID, 0, 128),
        (RUN_ID, 128, 128),
    ]

    acknowledged = client.get(
        f"/api/runs/{RUN_ID}/events",
        headers={"last-event-id": str(terminal_seq)},
    )
    assert acknowledged.status_code == 200
    assert "event: run.completed" not in acknowledged.text
    assert acknowledged.text == ""

    before_terminal = client.get(
        f"/api/runs/{RUN_ID}/events",
        headers={"last-event-id": str(terminal_seq - 1)},
    )
    assert before_terminal.text.count("event: run.completed") == 1


def test_sse_durable_replay_interleaves_explicit_gap_control_frame(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "durable gap"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    original = service.durable_events[RUN_ID]
    service.durable_events[RUN_ID] = (
        original[0],
        original[1],
        replace(original[2], seq=4),
        replace(original[3], seq=5),
    )
    service.runs.clear()

    response = client.get(f"/api/runs/{RUN_ID}/events")

    assert response.status_code == 200
    gap_frame = "id: 3\nevent: stream.gap"
    assert response.text.count("event: stream.gap") == 1
    assert '"from_seq":3,"to_seq":3' in response.text
    assert '"reason":"ephemeral_not_durable"' in response.text
    assert response.text.index("id: 2\n") < response.text.index(gap_frame)
    assert response.text.index(gap_frame) < response.text.index("id: 4\n")
    assert '"seq":4' in response.text
    assert response.text.count("event: run.completed") == 1
    assert service.streamed == []


def test_sse_snapshot_only_emits_once_then_respects_last_event_id(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "retained snapshot"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.snapshot_only = True
    service.runs.clear()

    first = client.get(f"/api/runs/{RUN_ID}/events")

    assert first.status_code == 200
    assert first.text.count("event: stream.gap") == 1
    assert "id: 4\nevent: stream.gap" not in first.text
    assert first.text.count("event: stream.snapshot") == 1
    assert "id: 4\nevent: stream.snapshot" in first.text
    assert first.text.index("event: stream.gap") < first.text.index(
        "id: 4\nevent: stream.snapshot"
    )
    assert '"control_version":"stream-control-v1"' in first.text
    assert '"through_seq":4' in first.text
    assert "event: run.completed" not in first.text
    assert service.streamed == []

    resumed = client.get(
        f"/api/runs/{RUN_ID}/events", headers={"last-event-id": "4"}
    )
    assert resumed.status_code == 200
    assert resumed.text == ""


def test_sse_durable_delete_race_ends_before_prefetched_identity_leaks(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "delete race"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.runs.clear()
    service.delete_identity_on_resolve_call = 2

    response = client.get(f"/api/runs/{RUN_ID}/events")

    assert response.status_code == 200
    assert response.text == ""
    assert RUN_ID not in response.text
    assert service.streamed == []
    assert service.replayed == [(RUN_ID, 0, 128)]


@pytest.mark.parametrize(
    "query",
    [
        "after=-1",
        "after=1000001",
        "after=not-a-number",
        "after=0&after=1",
        "limit=0",
        "limit=129",
        "limit=not-a-number",
        "unknown=1",
    ],
)
def test_replay_query_boundaries_fail_before_durable_lookup(
    web_client: tuple[TestClient, _Commands], query: str
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert login.status_code == 200

    response = client.get(f"/api/runs/{RUN_ID}/replay?{query}")

    assert response.status_code == 400


def test_replay_maps_missing_conflict_cursor_and_unavailable_states(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "error mapping"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service

    newer = client.get(f"/api/runs/{RUN_ID}/replay?after=5")
    assert newer.status_code == 416
    assert newer.json() == {
        "detail": "replay cursor is outside the durable Run"
    }

    service.replay_error = ConversationConflictError("snapshot unavailable")
    conflict = client.get(f"/api/runs/{RUN_ID}/replay")
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "run replay is unavailable"}

    service.replay_error = ConversationStoreUnavailableError("store offline")
    unavailable = client.get(f"/api/runs/{RUN_ID}/replay")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "durable replay is unavailable"}

    service.replay_error = None
    service.run_identities.clear()
    missing = client.get(f"/api/runs/{RUN_ID}/replay")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "run not found"}

    malformed = client.get("/api/runs/not-a-run/replay")
    assert malformed.status_code == 404


def test_failed_login_rate_limit_is_bounded_per_client(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    for _attempt in range(5):
        response = client.post(
            "/api/auth/login", json={"token": "0" * 64}, headers=SAME_ORIGIN
        )
        assert response.status_code == 401

    limited = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert SESSION_COOKIE not in client.cookies


def test_anonymous_event_and_cancel_routes_are_rejected(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    events = client.get(f"/api/runs/{RUN_ID}/events")
    replay = client.get(f"/api/runs/{RUN_ID}/replay")
    context = client.get(f"/api/runs/{RUN_ID}/context")
    cancel = client.post(
        f"/api/runs/{RUN_ID}/cancel",
        headers=SAME_ORIGIN,
    )

    assert events.status_code == 401
    assert replay.status_code == 401
    assert context.status_code == 401
    assert cancel.status_code == 403


def test_json_body_limit_accepts_boundary_and_rejects_overflow(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    prefix = b'{"token":"'
    suffix = b'"}'
    exact = prefix + b"x" * (MAX_JSON_BODY_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(exact) == MAX_JSON_BODY_BYTES

    at_limit = client.post(
        "/api/auth/login",
        content=exact,
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )
    assert at_limit.status_code == 401

    over_limit = client.post(
        "/api/auth/login",
        content=exact + b" ",
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )
    assert over_limit.status_code == 413
    assert over_limit.json() == {"detail": "request body too large"}


def test_streamed_json_body_cannot_bypass_limit_without_content_length(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    def chunks() -> Any:
        yield b'{"token":"'
        yield b"x" * MAX_JSON_BODY_BYTES
        yield b'"}'

    response = client.post(
        "/api/auth/login",
        content=chunks(),
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_json_routes_reject_wrong_media_type_and_allow_parameters(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    body = ('{"token":"' + "0" * 64 + '"}').encode()

    wrong_type = client.post(
        "/api/auth/login",
        content=body,
        headers={**SAME_ORIGIN, "content-type": "text/plain"},
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json() == {
        "detail": "Content-Type must be application/json"
    }

    parameterized_json = client.post(
        "/api/auth/login",
        content=body,
        headers={
            **SAME_ORIGIN,
            "content-type": "application/json; charset=utf-8",
        },
    )
    assert parameterized_json.status_code == 401


def test_negative_content_length_is_rejected_at_boundary(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    response = client.post(
        "/api/auth/login",
        content=b"{}",
        headers={
            **SAME_ORIGIN,
            "content-type": "application/json",
            "content-length": "-1",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid content length"}


def test_unsafe_workspace_context_is_a_bounded_conflict(
    web_client: tuple[TestClient, _Commands],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )

    async def reject(_command: StartRunCommand) -> object:
        raise WorkspaceContextError("internal path detail")

    monkeypatch.setattr(commands, "start", reject)
    response = client.post(
        "/api/runs",
        json={"message": "hello"},
        headers={
            **SAME_ORIGIN,
            "x-csrf-token": login.json()["csrf_token"],
        },
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Agent workspace context is unsafe"}
    assert "path" not in response.text

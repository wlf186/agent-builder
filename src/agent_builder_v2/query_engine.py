"""Conversation-scoped query orchestration for the greenfield Harness.

Claude Code keeps one QueryEngine per conversation and starts a new Turn for
each submitted message.  This prototype keeps the same logical ownership while
leaving mutable and durable state in :class:`ConversationStore` and Run
execution in :class:`RunService`.

The registry is an identity map, not another persistence layer.  A
``QueryEngine`` stores only its immutable identity, a short operation lock and
a retirement flag.  It never caches transcripts, events, ContextPlans, model
sessions or Worker state.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
import secrets
from typing import Any, Protocol

from .context import (
    CONTEXT_INSPECTION_KEY_BYTES,
    ContextPlan,
    ContextPlanError,
    ContextPlanInspection,
    PromptSectionReveal,
)
from .contracts import EventEnvelope, RESOURCE_ID, StartRunCommand
from .control import RunRecord
from .replay import (
    DurableReplay,
    MAX_REPLAY_PAGE,
    MAX_REPLAY_SEQUENCE,
    ProjectionSnapshot,
    ReplayGap,
    RunIdentity,
)
from .sessions import (
    MAX_CONVERSATIONS_PER_AGENT,
    Conversation,
    ConversationConflictError,
    ConversationDeleteResult,
    ConversationNotFoundError,
    ConversationStoreUnavailableError,
    ConversationSummary,
)
from .state import JournalCorruptionError, JournalUnavailableError


class QueryEngineOwnershipError(KeyError):
    """A Run does not belong to the QueryEngine used to address it."""


class QueryEngineRetiredError(ConversationNotFoundError):
    """A deleted or closed QueryEngine handle can no longer be used."""


class QueryReplayCursorError(ValueError):
    """A syntactically valid cursor is outside this durable Run."""


class QueryReplayUnavailableError(RuntimeError):
    """Durable replay could not be read or validated safely."""


class QueryContextUnavailableError(RuntimeError):
    """A retained Run has no trustworthy in-memory ContextPlan projection."""


class QueryRunNotRetainedError(LookupError):
    """A Run may exist durably but has no live RunRecord in this Gateway."""


def _validate_replay_request(after: int, limit: int) -> None:
    if (
        not isinstance(after, int)
        or isinstance(after, bool)
        or not 0 <= after <= MAX_REPLAY_SEQUENCE
    ):
        raise ValueError("invalid replay cursor")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_REPLAY_PAGE
    ):
        raise ValueError("invalid replay limit")


@dataclass(frozen=True, slots=True)
class QueryRunHandle:
    """Immutable identity returned across the QueryEngine boundary."""

    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str

    @classmethod
    def from_record(cls, record: RunRecord) -> QueryRunHandle:
        return cls(
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
        )

    @classmethod
    def from_identity(cls, identity: RunIdentity) -> QueryRunHandle:
        return cls(
            agent_id=identity.agent_id,
            conversation_id=identity.conversation_id,
            turn_id=identity.turn_id,
            run_id=identity.run_id,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class QueryContextInspection:
    """Fresh exact inspection of a retained, ownership-checked Run."""

    identity: QueryRunHandle
    plan: ContextPlanInspection
    availability: str = "exact"

    def __post_init__(self) -> None:
        if self.availability != "exact":
            raise ValueError("invalid retained context availability")

    def to_dict(self) -> dict[str, object]:
        payload = self.plan.to_dict()
        return {
            "identity": self.identity.to_dict(),
            "availability": self.availability,
            **payload,
        }


@dataclass(frozen=True, slots=True)
class QueryContextReveal:
    """Bounded redacted excerpts after independent operator authorization."""

    identity: QueryRunHandle
    sections: tuple[PromptSectionReveal, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_dict(),
            "availability": "exact",
            "content_exposure": "redacted_excerpt",
            "sections": [section.to_dict() for section in self.sections],
        }


class QueryRuntime(Protocol):
    """Narrow trusted runtime used by QueryEngine.

    ``RunService`` implements this protocol.  Keeping the protocol here makes
    the ownership boundary explicit without making the Control Plane depend on
    QueryEngine.
    """

    async def create_conversation(self, title: str = "新会话") -> Conversation: ...

    async def list_conversations(self) -> tuple[ConversationSummary, ...]: ...

    async def get_conversation(self, conversation_id: str) -> Conversation: ...

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult: ...

    async def start(self, command: StartRunCommand) -> RunRecord: ...

    def get(self, run_id: str) -> RunRecord: ...

    async def cancel(self, run_id: str) -> None: ...

    def stream(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[EventEnvelope | None]: ...

    async def resolve_run_identity(self, run_id: str) -> RunIdentity: ...

    async def replay_run(
        self,
        run_id: str,
        *,
        after: int,
        limit: int,
        expected_identity: RunIdentity,
    ) -> DurableReplay: ...


RetirementCallback = Callable[["QueryEngine"], Awaitable[None]]


class QueryEngine:
    """One logical conversation and its sequence of isolated Turns.

    The operation lock serializes only short admission, restore, cancellation
    and deletion operations in this Gateway process.  It is never held while a
    Run executes.  Durable active-Run and revision checks remain authoritative
    across threads and future processes.
    """

    def __init__(
        self,
        runtime: QueryRuntime,
        *,
        agent_id: str,
        conversation_id: str,
        on_retired: RetirementCallback,
    ) -> None:
        if not agent_id or len(agent_id) > 64:
            raise ValueError("invalid agent_id")
        if RESOURCE_ID.fullmatch(conversation_id) is None:
            raise ValueError("invalid conversation_id")
        self._runtime = runtime
        self._agent_id = agent_id
        self._conversation_id = conversation_id
        self._on_retired = on_retired
        self._operation_lock = asyncio.Lock()
        self._retired = False
        self._retiring = False
        self._delete_task: asyncio.Task[ConversationDeleteResult] | None = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def retired(self) -> bool:
        return self._retired

    def _ensure_live(self) -> None:
        if self._retired or self._retiring:
            raise QueryEngineRetiredError("query engine is retired")

    def _retire(self) -> None:
        self._retired = True

    def _track_delete(
        self, task: asyncio.Task[ConversationDeleteResult]
    ) -> None:
        self._delete_task = task

        def completed(value: asyncio.Task[ConversationDeleteResult]) -> None:
            if self._delete_task is value:
                self._delete_task = None
            if not value.cancelled():
                value.exception()

        task.add_done_callback(completed)

    async def _drain_delete(self) -> None:
        task = self._delete_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def _owned_record(
        self,
        run_id: str,
        *,
        distinguish_not_retained: bool = False,
    ) -> RunRecord:
        try:
            record = self._runtime.get(run_id)
        except KeyError as exc:
            if distinguish_not_retained:
                raise QueryRunNotRetainedError(
                    "run is not retained"
                ) from exc
            raise QueryEngineOwnershipError("run not found") from exc
        if (
            record.agent_id != self._agent_id
            or record.conversation_id != self._conversation_id
            or record.run_id != run_id
            or RESOURCE_ID.fullmatch(record.turn_id) is None
            or RESOURCE_ID.fullmatch(record.run_id) is None
        ):
            raise QueryEngineOwnershipError("run not found")
        return record

    def _handle(self, record: RunRecord) -> QueryRunHandle:
        if (
            record.agent_id != self._agent_id
            or record.conversation_id != self._conversation_id
        ):
            raise RuntimeError("trusted runtime returned a foreign Run")
        return QueryRunHandle.from_record(record)

    def _handle_identity(self, identity: RunIdentity) -> QueryRunHandle:
        if (
            identity.agent_id != self._agent_id
            or identity.conversation_id != self._conversation_id
            or RESOURCE_ID.fullmatch(identity.turn_id) is None
            or RESOURCE_ID.fullmatch(identity.run_id) is None
        ):
            raise QueryEngineOwnershipError("run not found")
        return QueryRunHandle.from_identity(identity)

    @staticmethod
    def _validated_replay(
        value: DurableReplay,
        *,
        identity: RunIdentity,
        after: int,
        limit: int,
    ) -> DurableReplay:
        if (
            not isinstance(value, DurableReplay)
            or value.identity != identity
            or not isinstance(value.snapshot, ProjectionSnapshot)
            or value.snapshot.identity != identity
            or value.availability
            not in {"complete", "partial", "snapshot_only", "unavailable"}
            or isinstance(value.oldest_cursor, bool)
            or isinstance(value.latest_cursor, bool)
            or isinstance(value.next_cursor, bool)
            or not isinstance(value.oldest_cursor, int)
            or not isinstance(value.latest_cursor, int)
            or not isinstance(value.next_cursor, int)
            or not isinstance(value.has_more, bool)
            or not 0 <= value.oldest_cursor <= value.latest_cursor <= MAX_REPLAY_SEQUENCE
            or not after <= value.next_cursor <= value.latest_cursor
            or value.has_more != (value.next_cursor < value.latest_cursor)
            or value.snapshot.through_seq != value.latest_cursor
            or not isinstance(value.events, tuple)
            or len(value.events) > limit
            or not isinstance(value.gaps, tuple)
            or any(not isinstance(gap, ReplayGap) for gap in value.gaps)
        ):
            raise QueryReplayUnavailableError("durable Run replay is invalid")
        previous = after
        for event in value.events:
            if (
                not isinstance(event, EventEnvelope)
                or event.agent_id != identity.agent_id
                or event.conversation_id != identity.conversation_id
                or event.turn_id != identity.turn_id
                or event.run_id != identity.run_id
                or event.seq <= previous
                or event.seq > value.next_cursor
            ):
                raise QueryReplayUnavailableError("durable Run replay is invalid")
            previous = event.seq
        previous_gap_end = after
        event_sequences = {event.seq for event in value.events}
        for gap in value.gaps:
            if (
                gap.to_seq <= after
                or gap.from_seq > value.next_cursor
                or gap.to_seq > value.next_cursor
                or gap.to_seq <= previous_gap_end
                or any(
                    gap.from_seq <= sequence <= gap.to_seq
                    for sequence in event_sequences
                )
            ):
                raise QueryReplayUnavailableError(
                    "durable Run replay is invalid"
                )
            previous_gap_end = gap.to_seq
        if value.events and value.events[-1].seq != value.next_cursor:
            raise QueryReplayUnavailableError("durable Run replay is invalid")
        if value.availability == "snapshot_only":
            expected_gaps = (
                ()
                if after >= value.latest_cursor
                else (
                    ReplayGap(
                        after + 1,
                        value.latest_cursor,
                        "retention",
                    ),
                )
            )
            if (
                value.events
                or value.oldest_cursor != value.latest_cursor
                or value.next_cursor != value.latest_cursor
                or value.has_more
                or value.gaps != expected_gaps
            ):
                raise QueryReplayUnavailableError(
                    "durable Run replay is invalid"
                )
        elif not value.events and value.next_cursor != after:
            raise QueryReplayUnavailableError("durable Run replay is invalid")
        elif not value.events and value.has_more:
            raise QueryReplayUnavailableError("durable Run replay made no progress")
        return value

    async def restore(self) -> Conversation:
        """Restore the latest durable projection; never resume an old Worker."""

        async with self._operation_lock:
            self._ensure_live()
            conversation = await self._runtime.get_conversation(
                self._conversation_id
            )
            if (
                conversation.agent_id != self._agent_id
                or conversation.conversation_id != self._conversation_id
            ):
                raise ConversationNotFoundError("conversation not found")
            return conversation

    async def submit_message(
        self, message: str, *, model_id: str | None = None, compact: bool = False
    ) -> QueryRunHandle:
        """Admit one new Turn while keeping the Run itself independently live."""

        async with self._operation_lock:
            self._ensure_live()
            record = await self._runtime.start(
                StartRunCommand(
                    agent_id=self._agent_id,
                    message=message,
                    conversation_id=self._conversation_id,
                    model_id=model_id,
                    compact=compact,
                )
            )
            try:
                return self._handle(record)
            except BaseException:
                # A trusted runtime identity violation must not leave execution
                # running after this boundary rejects the returned handle.
                await self._runtime.cancel(record.run_id)
                raise

    def get_run(self, run_id: str) -> QueryRunHandle:
        self._ensure_live()
        return self._handle(self._owned_record(run_id))

    async def inspect_context(
        self,
        run_id: str,
        *,
        durable_identity: RunIdentity,
        content_digest_key: bytes,
    ) -> QueryContextInspection:
        """Inspect a retained Run without caching or exposing prompt content."""

        async with self._operation_lock:
            self._ensure_live()
            if durable_identity.run_id != run_id:
                raise QueryEngineOwnershipError("run not found")
            durable_handle = self._handle_identity(durable_identity)
            record = self._owned_record(
                run_id, distinguish_not_retained=True
            )
            record_handle = self._handle(record)
            if record_handle != durable_handle:
                raise QueryEngineOwnershipError("run not found")
            plan = record.context_plan
            if not isinstance(plan, ContextPlan) or plan.agent_id != self._agent_id:
                raise QueryContextUnavailableError(
                    "retained Run context is unavailable"
                )
            events = getattr(record, "events", None)
            if not isinstance(events, (list, tuple)) or not events:
                raise QueryContextUnavailableError(
                    "retained Run context is unavailable"
                )
            started = events[0]
            if (
                not isinstance(started, EventEnvelope)
                or RESOURCE_ID.fullmatch(started.event_id) is None
                or not isinstance(started.seq, int)
                or isinstance(started.seq, bool)
                or started.seq != 1
                or started.kind != "run.started"
                or started.durability != "durable"
                or started.agent_id != durable_identity.agent_id
                or started.conversation_id != durable_identity.conversation_id
                or started.turn_id != durable_identity.turn_id
                or started.run_id != durable_identity.run_id
                or not isinstance(started.payload, dict)
                or started.payload.get("context_plan")
                != plan.public_metadata()
            ):
                raise QueryContextUnavailableError(
                    "retained Run context is unavailable"
                )
            try:
                projection = plan.operator_inspection(content_digest_key)
            except ContextPlanError as exc:
                raise QueryContextUnavailableError(
                    "retained Run context is unavailable"
                ) from exc
            return QueryContextInspection(
                identity=self._handle(record),
                plan=projection,
            )

    async def stream(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[EventEnvelope | None]:
        """Validate ownership, then transparently forward canonical events."""

        self._ensure_live()
        self._owned_record(run_id)
        async for event in self._runtime.stream(run_id, after):
            yield event

    async def replay(
        self,
        identity: RunIdentity,
        *,
        after: int,
        limit: int,
    ) -> DurableReplay:
        """Read one durable page without consulting a live ``RunRecord``."""

        self._ensure_live()
        _validate_replay_request(after, limit)
        handle = self._handle_identity(identity)
        try:
            value = await self._runtime.replay_run(
                handle.run_id,
                after=after,
                limit=limit,
                expected_identity=identity,
            )
        except (KeyError, ConversationNotFoundError) as exc:
            raise QueryEngineOwnershipError("run not found") from exc
        except ConversationConflictError:
            raise
        except ValueError as exc:
            raise QueryReplayCursorError(
                "replay cursor is outside the durable Run"
            ) from exc
        except (
            ConversationStoreUnavailableError,
            JournalCorruptionError,
            JournalUnavailableError,
        ) as exc:
            raise QueryReplayUnavailableError(
                "durable Run replay is unavailable"
            ) from exc
        return self._validated_replay(
            value,
            identity=identity,
            after=after,
            limit=limit,
        )

    async def cancel_run(self, run_id: str) -> None:
        async with self._operation_lock:
            self._ensure_live()
            self._owned_record(run_id)
            await self._runtime.cancel(run_id)

    async def cancel_active_turn(self) -> str | None:
        """Claude-style interrupt of this conversation's current Turn."""

        async with self._operation_lock:
            self._ensure_live()
            conversation = await self._runtime.get_conversation(
                self._conversation_id
            )
            if (
                conversation.agent_id != self._agent_id
                or conversation.conversation_id != self._conversation_id
            ):
                raise ConversationNotFoundError("conversation not found")
            run_id = conversation.active_run_id
            if run_id is None:
                return None
            self._owned_record(run_id)
            await self._runtime.cancel(run_id)
            return run_id

    async def delete(self) -> ConversationDeleteResult:
        """Delete durable state, then permanently invalidate this handle."""

        async with self._operation_lock:
            if self._retiring and self._delete_task is not None:
                operation = self._delete_task
            else:
                self._ensure_live()
                self._retiring = True
                operation = asyncio.create_task(
                    self._delete_owned(),
                    name=f"query-engine-delete-{self._conversation_id}",
                )
                self._track_delete(operation)
        return await asyncio.shield(operation)

    async def _delete_owned(self) -> ConversationDeleteResult:
        """Close the in-memory lifecycle even if the caller is cancelled."""

        try:
            result = await self._runtime.delete_conversation(
                self._conversation_id
            )
        except BaseException:
            self._retiring = False
            raise
        # A false result also proves that the durable identity is gone.
        self._retire()
        self._retiring = False
        await self._on_retired(self)
        return result


class QueryEngineRegistry:
    """Bounded per-Gateway identity map and QueryEngine application facade."""

    def __init__(
        self,
        runtime: QueryRuntime,
        agent_id: str,
        *,
        capacity: int = MAX_CONVERSATIONS_PER_AGENT,
    ) -> None:
        if not agent_id or len(agent_id) > 64:
            raise ValueError("invalid agent_id")
        if capacity <= 0 or capacity > MAX_CONVERSATIONS_PER_AGENT:
            raise ValueError("invalid QueryEngine registry capacity")
        self._runtime = runtime
        self._agent_id = agent_id
        self._capacity = capacity
        self._engines: dict[str, QueryEngine] = {}
        self._lock = asyncio.Lock()
        self._generation = 0
        self._closing = False
        self._lifecycle_tasks: set[asyncio.Task[Any]] = set()
        self._context_inspection_key = secrets.token_bytes(
            CONTEXT_INSPECTION_KEY_BYTES
        )

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def cached_engine_count(self) -> int:
        return len(self._engines)

    def _new_engine(self, conversation_id: str) -> QueryEngine:
        return QueryEngine(
            self._runtime,
            agent_id=self._agent_id,
            conversation_id=conversation_id,
            on_retired=self._retired,
        )

    def _track_lifecycle_task(self, task: asyncio.Task[Any]) -> None:
        self._lifecycle_tasks.add(task)

        def completed(value: asyncio.Task[Any]) -> None:
            self._lifecycle_tasks.discard(value)
            if not value.cancelled():
                value.exception()

        task.add_done_callback(completed)

    async def _drain_lifecycle_tasks(self) -> None:
        while self._lifecycle_tasks:
            await asyncio.gather(
                *tuple(self._lifecycle_tasks), return_exceptions=True
            )

    async def _cleanup_created_conversation(self, conversation_id: str) -> None:
        try:
            result = await self._runtime.delete_conversation(conversation_id)
        except ConversationConflictError:
            # Another authenticated request found the new Conversation and
            # admitted a Turn.  It is now visible durable state, not an empty
            # abandoned create, and must not be deleted underneath that Run.
            return
        if not result.deleted:
            return
        async with self._lock:
            engine = self._engines.pop(conversation_id, None)
            if engine is not None:
                engine._retire()
            self._generation += 1

    async def _cancel_abandoned_run(self, run_id: str) -> None:
        await self._runtime.cancel(run_id)

    async def _cached(self, conversation_id: str) -> QueryEngine | None:
        async with self._lock:
            if self._closing:
                raise RuntimeError("QueryEngine registry is closed")
            engine = self._engines.get(conversation_id)
            if engine is not None and engine.retired:
                del self._engines[conversation_id]
                self._generation += 1
                return None
            return engine

    async def _intern(self, conversation_id: str) -> QueryEngine:
        async with self._lock:
            if self._closing:
                raise RuntimeError("QueryEngine registry is closed")
            existing = self._engines.get(conversation_id)
            if existing is not None and not existing.retired:
                return existing
            if existing is not None:
                del self._engines[conversation_id]
            if len(self._engines) >= self._capacity:
                raise ConversationConflictError(
                    "QueryEngine registry capacity exhausted"
                )
            engine = self._new_engine(conversation_id)
            self._engines[conversation_id] = engine
            return engine

    async def _retired(self, engine: QueryEngine) -> None:
        async with self._lock:
            if self._engines.get(engine.conversation_id) is engine:
                del self._engines[engine.conversation_id]
                self._generation += 1

    async def for_conversation(self, conversation_id: str) -> QueryEngine:
        """Open one durable conversation and canonicalize its live Engine."""

        if RESOURCE_ID.fullmatch(conversation_id) is None:
            raise ConversationNotFoundError("conversation not found")
        while True:
            engine = await self._cached(conversation_id)
            if engine is not None:
                return engine
            async with self._lock:
                if self._closing:
                    raise RuntimeError("QueryEngine registry is closed")
                generation = self._generation
            conversation = await self._runtime.get_conversation(conversation_id)
            if (
                conversation.agent_id != self._agent_id
                or conversation.conversation_id != conversation_id
            ):
                raise ConversationNotFoundError("conversation not found")
            async with self._lock:
                if self._closing:
                    raise RuntimeError("QueryEngine registry is closed")
                if generation != self._generation:
                    continue
                existing = self._engines.get(conversation_id)
                if existing is not None and not existing.retired:
                    return existing
                if len(self._engines) >= self._capacity:
                    raise ConversationConflictError(
                        "QueryEngine registry capacity exhausted"
                    )
                engine = self._new_engine(conversation_id)
                self._engines[conversation_id] = engine
                return engine

    async def create_conversation(self, title: str = "新会话") -> Conversation:
        async with self._lock:
            if self._closing:
                raise RuntimeError("QueryEngine registry is closed")
            if len(self._engines) >= self._capacity:
                raise ConversationConflictError(
                    "QueryEngine registry capacity exhausted"
                )
        conversation = await self._runtime.create_conversation(title)
        if conversation.agent_id != self._agent_id:
            await self._runtime.delete_conversation(conversation.conversation_id)
            raise RuntimeError("trusted runtime returned a foreign conversation")
        try:
            await self._intern(conversation.conversation_id)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._cleanup_created_conversation(
                    conversation.conversation_id
                ),
                name=(
                    "query-engine-abandoned-create-"
                    f"{conversation.conversation_id}"
                ),
            )
            self._track_lifecycle_task(cleanup)
            raise
        except BaseException:
            await self._cleanup_created_conversation(
                conversation.conversation_id
            )
            raise
        return conversation

    async def list_conversations(self) -> tuple[ConversationSummary, ...]:
        conversations = await self._runtime.list_conversations()
        if any(value.agent_id != self._agent_id for value in conversations):
            raise RuntimeError("trusted runtime returned a foreign conversation")
        return conversations

    async def get_conversation(self, conversation_id: str) -> Conversation:
        engine = await self.for_conversation(conversation_id)
        return await engine.restore()

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        try:
            engine = await self.for_conversation(conversation_id)
        except ConversationNotFoundError:
            return ConversationDeleteResult(False, 0, 0)
        try:
            return await engine.delete()
        except ConversationNotFoundError:
            return ConversationDeleteResult(False, 0, 0)

    async def submit(self, command: StartRunCommand) -> QueryRunHandle:
        if not isinstance(command, StartRunCommand):
            raise TypeError("submit requires StartRunCommand")
        command.validate()
        if command.agent_id != self._agent_id:
            raise ValueError("unknown agent")
        if command.conversation_id is not None:
            engine = await self.for_conversation(command.conversation_id)
            return await engine.submit_message(
                command.message, model_id=command.model_id, compact=command.compact
            )

        # Compatibility endpoint: RunService retains its cancellation-safe
        # auto-create transaction; the resulting conversation is immediately
        # represented by a logical QueryEngine after successful admission.
        async with self._lock:
            if self._closing:
                raise RuntimeError("QueryEngine registry is closed")
            if len(self._engines) >= self._capacity:
                raise ConversationConflictError(
                    "QueryEngine registry capacity exhausted"
                )
        record = await self._runtime.start(command)
        if record.agent_id != self._agent_id:
            await self._runtime.cancel(record.run_id)
            raise RuntimeError("trusted runtime returned a foreign Run")
        try:
            engine = await self._intern(record.conversation_id)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._cancel_abandoned_run(record.run_id),
                name=f"query-engine-abandoned-run-{record.run_id}",
            )
            self._track_lifecycle_task(cleanup)
            raise
        except BaseException:
            await self._runtime.cancel(record.run_id)
            raise
        return engine._handle(record)

    async def _for_run(
        self, run_id: str, *, distinguish_not_retained: bool = False
    ) -> QueryEngine:
        while True:
            async with self._lock:
                if self._closing:
                    raise RuntimeError("QueryEngine registry is closed")
                generation = self._generation
            try:
                record = self._runtime.get(run_id)
            except KeyError as exc:
                if distinguish_not_retained:
                    raise QueryRunNotRetainedError(
                        "run is not retained"
                    ) from exc
                raise QueryEngineOwnershipError("run not found") from exc
            if (
                record.agent_id != self._agent_id
                or record.run_id != run_id
                or RESOURCE_ID.fullmatch(record.conversation_id) is None
                or RESOURCE_ID.fullmatch(record.turn_id) is None
                or RESOURCE_ID.fullmatch(record.run_id) is None
            ):
                raise QueryEngineOwnershipError("run not found")
            async with self._lock:
                if self._closing:
                    raise RuntimeError("QueryEngine registry is closed")
                if generation != self._generation:
                    continue
                engine = self._engines.get(record.conversation_id)
                if engine is not None and not engine.retired:
                    return engine
                if len(self._engines) >= self._capacity:
                    raise ConversationConflictError(
                        "QueryEngine registry capacity exhausted"
                    )
                engine = self._new_engine(record.conversation_id)
                self._engines[record.conversation_id] = engine
                return engine

    async def _resolve_durable_identity(self, run_id: str) -> RunIdentity:
        if RESOURCE_ID.fullmatch(run_id) is None:
            raise QueryEngineOwnershipError("run not found")
        try:
            identity = await self._runtime.resolve_run_identity(run_id)
        except (KeyError, ConversationNotFoundError) as exc:
            raise QueryEngineOwnershipError("run not found") from exc
        except ConversationStoreUnavailableError as exc:
            raise QueryReplayUnavailableError(
                "durable Run identity is unavailable"
            ) from exc
        if (
            not isinstance(identity, RunIdentity)
            or identity.agent_id != self._agent_id
            or RESOURCE_ID.fullmatch(identity.conversation_id) is None
            or RESOURCE_ID.fullmatch(identity.turn_id) is None
            or identity.run_id != run_id
        ):
            raise QueryEngineOwnershipError("run not found")
        return identity

    async def _for_durable_run(
        self, run_id: str
    ) -> tuple[QueryEngine, RunIdentity]:
        identity = await self._resolve_durable_identity(run_id)
        try:
            engine = await self.for_conversation(identity.conversation_id)
        except ConversationNotFoundError as exc:
            # A concurrent delete can remove the Conversation after identity
            # resolution.  Do not disclose the stale identity to the caller.
            raise QueryEngineOwnershipError("run not found") from exc
        engine._handle_identity(identity)
        return engine, identity

    async def get_run(self, run_id: str) -> QueryRunHandle:
        engine = await self._for_run(run_id)
        return engine.get_run(run_id)

    async def inspect_retained_context(
        self, run_id: str
    ) -> QueryContextInspection:
        """Inspect only a Run whose immutable ContextPlan remains in memory."""

        if RESOURCE_ID.fullmatch(run_id) is None:
            raise QueryEngineOwnershipError("run not found")
        engine = await self._for_run(run_id, distinguish_not_retained=True)
        try:
            identity = await self._resolve_durable_identity(run_id)
            return await engine.inspect_context(
                run_id,
                durable_identity=identity,
                content_digest_key=self._context_inspection_key,
            )
        except QueryEngineRetiredError as exc:
            raise QueryEngineOwnershipError("run not found") from exc
        except QueryReplayUnavailableError as exc:
            raise QueryContextUnavailableError(
                "retained Run durable identity is unavailable"
            ) from exc

    async def reveal_retained_context(self, run_id: str) -> QueryContextReveal:
        """Return bounded excerpts; authentication and audit remain Web-owned."""

        exact = await self.inspect_retained_context(run_id)
        engine = await self._for_run(run_id, distinguish_not_retained=True)
        record = engine._owned_record(run_id, distinguish_not_retained=True)
        plan = record.context_plan
        if (
            not isinstance(plan, ContextPlan)
            or engine._handle(record) != exact.identity
        ):
            raise QueryContextUnavailableError(
                "retained Run context is unavailable"
            )
        try:
            sections = plan.operator_redacted_reveal()
        except ContextPlanError as exc:
            raise QueryContextUnavailableError(
                "retained Run context is unavailable"
            ) from exc
        return QueryContextReveal(exact.identity, sections)

    async def resolve_run_identity(self, run_id: str) -> QueryRunHandle:
        """Resolve a durable Run without requiring a retained live record."""

        engine, identity = await self._for_durable_run(run_id)
        return engine._handle_identity(identity)

    async def replay(
        self,
        run_id: str,
        *,
        after: int,
        limit: int,
    ) -> DurableReplay:
        _validate_replay_request(after, limit)
        engine, identity = await self._for_durable_run(run_id)
        return await engine.replay(identity, after=after, limit=limit)

    async def stream(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[EventEnvelope | None]:
        engine = await self._for_run(run_id)
        async for event in engine.stream(run_id, after):
            yield event

    async def cancel(self, run_id: str) -> None:
        engine = await self._for_run(run_id)
        await engine.cancel_run(run_id)

    async def close(self) -> None:
        async with self._lock:
            if self._closing:
                return
            self._closing = True
            self._generation += 1
            engines = tuple(self._engines.values())
            self._engines.clear()
        for engine in engines:
            engine._retire()
        if engines:
            await asyncio.gather(
                *(engine._drain_delete() for engine in engines),
                return_exceptions=True,
            )
        await self._drain_lifecycle_tasks()

"""Explicit parent/child Run delegation with a bounded durable mailbox.

Delegation is deliberately a brokered capability, not an in-process graph edge.
The parent owns a durable Task and two bounded mailbox messages; the child owns
its own Conversation, Run, Worker, Capsule and sandbox.  No filesystem handle,
environment, transcript object or model credential crosses the boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import threading
from typing import Any

from .agents import AgentRegistry
from .capsule import SAFE_ID
from .contracts import StartRunCommand, new_id, utc_now
from .permissions import CapabilityRequest
from .tasks import (
    SUBAGENT_TASK_ID,
    TERMINAL_TASK_STATES,
    TaskError,
    TaskParentIdentity,
    TaskRecord,
    TaskStore,
)


MAX_DELEGATION_MESSAGE_BYTES = 4 * 1024
MAX_DELEGATION_RESULT_BYTES = 8 * 1024
MAX_MAILBOX_MESSAGES = 2
MAX_ACTIVE_DELEGATIONS = 2
MAX_DELEGATIONS_PER_PARENT_RUN = 1
DELEGATION_WALL_SECONDS = 45.0


class SubagentError(RuntimeError):
    """A delegation request or durable transition failed closed."""


def _canonical(value: object, maximum: int, field: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SubagentError(f"invalid {field}") from exc
    if len(encoded) > maximum:
        raise SubagentError(f"{field} exceeds its byte limit")
    return encoded.decode("utf-8")


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class SubagentLink:
    task_id: str
    parent_agent_id: str
    parent_conversation_id: str
    parent_turn_id: str
    parent_run_id: str
    child_agent_id: str
    child_conversation_id: str | None
    child_turn_id: str | None
    child_run_id: str | None
    state: str
    created_at: str
    updated_at: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "parent_agent_id": self.parent_agent_id,
            "parent_conversation_id": self.parent_conversation_id,
            "parent_turn_id": self.parent_turn_id,
            "parent_run_id": self.parent_run_id,
            "child_agent_id": self.child_agent_id,
            "child_conversation_id": self.child_conversation_id,
            "child_turn_id": self.child_turn_id,
            "child_run_id": self.child_run_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    task_id: str
    sequence: int
    direction: str
    source_agent_id: str
    target_agent_id: str
    content: str
    content_digest: str
    created_at: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "sequence": self.sequence,
            "direction": self.direction,
            "source_agent_id": self.source_agent_id,
            "target_agent_id": self.target_agent_id,
            "content": self.content,
            "content_digest": self.content_digest,
            "created_at": self.created_at,
        }


class SubagentStore:
    """Agent-local parent link and mailbox store in the Capsule database."""

    def __init__(self, database: Any, agent_id: str) -> None:
        if SAFE_ID.fullmatch(agent_id) is None:
            raise ValueError("invalid SubagentStore Agent identity")
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS subagent_links (
                task_id TEXT PRIMARY KEY,
                parent_agent_id TEXT NOT NULL,
                parent_conversation_id TEXT NOT NULL,
                parent_turn_id TEXT NOT NULL,
                parent_run_id TEXT NOT NULL,
                child_agent_id TEXT NOT NULL,
                child_conversation_id TEXT,
                child_turn_id TEXT,
                child_run_id TEXT,
                state TEXT NOT NULL CHECK (
                    state IN ('queued','running','completed','failed','cancelled','interrupted')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES background_tasks(task_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_subagent_links_parent
                ON subagent_links(parent_agent_id, parent_conversation_id, created_at);
            CREATE TABLE IF NOT EXISTS subagent_mailbox (
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 2),
                direction TEXT NOT NULL CHECK (
                    direction IN ('parent_to_child','child_to_parent')
                ),
                source_agent_id TEXT NOT NULL,
                target_agent_id TEXT NOT NULL,
                content TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence),
                FOREIGN KEY(task_id) REFERENCES subagent_links(task_id) ON DELETE CASCADE
            );
            """
        )
        self._connection.commit()

    @staticmethod
    def _link(row: sqlite3.Row) -> SubagentLink:
        return SubagentLink(**dict(row))

    def create(
        self,
        *,
        task_id: str,
        parent: TaskParentIdentity,
        child_agent_id: str,
        message: str,
    ) -> SubagentLink:
        if (
            parent.agent_id != self.agent_id
            or any(
                SAFE_ID.fullmatch(value) is None
                for value in (
                    task_id,
                    parent.conversation_id,
                    parent.turn_id,
                    parent.run_id,
                    child_agent_id,
                )
            )
            or child_agent_id == self.agent_id
            or not message.strip()
            or len(message.encode("utf-8")) > MAX_DELEGATION_MESSAGE_BYTES
        ):
            raise ValueError("invalid subagent link")
        now = utc_now()
        content_digest = _digest(
            b"agent-builder-subagent-message-v1", message.encode("utf-8")
        )
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO subagent_links VALUES (
                    ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'queued', ?, ?
                )""",
                (
                    task_id,
                    parent.agent_id,
                    parent.conversation_id,
                    parent.turn_id,
                    parent.run_id,
                    child_agent_id,
                    now,
                    now,
                ),
            )
            self._connection.execute(
                "INSERT INTO subagent_mailbox VALUES (?, 1, 'parent_to_child', ?, ?, ?, ?, ?)",
                (
                    task_id,
                    parent.agent_id,
                    child_agent_id,
                    message,
                    content_digest,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM subagent_links WHERE task_id=?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._link(row)

    def mark_running(
        self,
        task_id: str,
        *,
        child_conversation_id: str,
        child_turn_id: str,
        child_run_id: str,
    ) -> SubagentLink:
        if any(
            SAFE_ID.fullmatch(value) is None
            for value in (task_id, child_conversation_id, child_turn_id, child_run_id)
        ):
            raise ValueError("invalid child Run identity")
        now = utc_now()
        with self._lock, self._connection:
            changed = self._connection.execute(
                """UPDATE subagent_links SET state='running', child_conversation_id=?,
                    child_turn_id=?, child_run_id=?, updated_at=?
                    WHERE task_id=? AND parent_agent_id=? AND state='queued'""",
                (
                    child_conversation_id,
                    child_turn_id,
                    child_run_id,
                    now,
                    task_id,
                    self.agent_id,
                ),
            ).rowcount
            if changed != 1:
                raise SubagentError("subagent link cannot enter running")
            row = self._connection.execute(
                "SELECT * FROM subagent_links WHERE task_id=?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._link(row)

    def finish(self, task_id: str, state: str, answer: str | None = None) -> SubagentLink:
        if state not in TERMINAL_TASK_STATES:
            raise ValueError("invalid subagent terminal state")
        if answer is not None and len(answer.encode("utf-8")) > MAX_DELEGATION_RESULT_BYTES:
            raise ValueError("subagent answer exceeds its byte limit")
        now = utc_now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM subagent_links WHERE task_id=? AND parent_agent_id=?",
                (task_id, self.agent_id),
            ).fetchone()
            if row is None or row["state"] in TERMINAL_TASK_STATES:
                raise SubagentError("subagent link cannot enter terminal")
            if answer is not None:
                count = self._connection.execute(
                    "SELECT COUNT(*) FROM subagent_mailbox WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                if count >= MAX_MAILBOX_MESSAGES:
                    raise SubagentError("subagent mailbox capacity exhausted")
                digest = _digest(
                    b"agent-builder-subagent-message-v1", answer.encode("utf-8")
                )
                self._connection.execute(
                    "INSERT INTO subagent_mailbox VALUES (?, 2, 'child_to_parent', ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        row["child_agent_id"],
                        row["parent_agent_id"],
                        answer,
                        digest,
                        now,
                    ),
                )
            changed = self._connection.execute(
                """UPDATE subagent_links SET state=?, updated_at=?
                   WHERE task_id=? AND parent_agent_id=?
                     AND state IN ('queued','running')""",
                (state, now, task_id, self.agent_id),
            ).rowcount
            if changed != 1:
                raise SubagentError("subagent link cannot enter terminal")
            updated = self._connection.execute(
                "SELECT * FROM subagent_links WHERE task_id=?", (task_id,)
            ).fetchone()
        assert updated is not None
        return self._link(updated)

    def get(self, task_id: str) -> SubagentLink:
        if SAFE_ID.fullmatch(task_id) is None:
            raise KeyError("subagent link not found")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM subagent_links WHERE task_id=? AND parent_agent_id=?",
                (task_id, self.agent_id),
            ).fetchone()
        if row is None:
            raise KeyError("subagent link not found")
        return self._link(row)

    def list_for_conversation(self, conversation_id: str) -> tuple[SubagentLink, ...]:
        if SAFE_ID.fullmatch(conversation_id) is None:
            raise ValueError("invalid Conversation identity")
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM subagent_links
                   WHERE parent_agent_id=? AND parent_conversation_id=?
                   ORDER BY created_at, task_id""",
                (self.agent_id, conversation_id),
            ).fetchall()
        return tuple(self._link(row) for row in rows)

    def mailbox(self, task_id: str) -> tuple[MailboxMessage, ...]:
        self.get(task_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM subagent_mailbox WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return tuple(MailboxMessage(**dict(row)) for row in rows)

    def recover_incomplete(self) -> int:
        with self._lock:
            rows = self._connection.execute(
                """SELECT task_id FROM subagent_links WHERE parent_agent_id=?
                   AND state IN ('queued','running') ORDER BY task_id""",
                (self.agent_id,),
            ).fetchall()
        recovered = 0
        for row in rows:
            try:
                self.finish(row["task_id"], "interrupted")
            except SubagentError:
                continue
            recovered += 1
        return recovered

    def delete_conversation(self, conversation_id: str) -> int:
        with self._lock, self._connection:
            active = self._connection.execute(
                """SELECT COUNT(*) FROM subagent_links WHERE parent_agent_id=?
                   AND parent_conversation_id=? AND state IN ('queued','running')""",
                (self.agent_id, conversation_id),
            ).fetchone()[0]
            if active:
                raise SubagentError("Conversation has active delegations")
            return self._connection.execute(
                "DELETE FROM subagent_links WHERE parent_agent_id=? AND parent_conversation_id=?",
                (self.agent_id, conversation_id),
            ).rowcount

    def close(self) -> None:
        with self._lock:
            self._connection.close()


RuntimeProvider = Callable[[str], Awaitable[Any]]


class PreparedSubagentExecutor:
    """Sync capability seam that schedules the child lifecycle on the main loop."""

    executor_kind = "subagent-delegate-v1"

    def __init__(
        self,
        coordinator: "SubagentCoordinator",
        parent_service: Any,
        parent: TaskParentIdentity,
        child_agent_id: str,
        message: str,
    ) -> None:
        self._coordinator = coordinator
        self._parent_service = parent_service
        self._parent = parent
        self._child_agent_id = child_agent_id
        self._message = message
        binding = _canonical(
            {
                "parent_agent_id": parent.agent_id,
                "parent_run_id": parent.run_id,
                "child_agent_id": child_agent_id,
                "message_digest": _digest(
                    b"agent-builder-subagent-message-v1", message.encode("utf-8")
                ),
            },
            2048,
            "subagent executor binding",
        )
        self.identity_digest = _digest(
            b"agent-builder-subagent-executor-v1", binding.encode("utf-8")
        )

    def execute(
        self, request: CapabilityRequest, cancelled: Callable[[], bool]
    ) -> str:
        if request.context.run_id != self._parent.run_id or cancelled():
            raise SubagentError("delegation binding is stale")
        future = asyncio.run_coroutine_threadsafe(
            self._coordinator._delegate(
                self._parent_service,
                self._parent,
                self._child_agent_id,
                self._message,
            ),
            self._coordinator.loop,
        )
        while True:
            if cancelled():
                future.cancel()
                raise SubagentError("delegation cancelled")
            try:
                return future.result(timeout=0.05)
            except TimeoutError:
                continue


class SubagentCoordinator:
    """Global admission fence and lifecycle coordinator for child Runs."""

    def __init__(
        self,
        registry: AgentRegistry,
        runtime_provider: RuntimeProvider,
    ) -> None:
        self.registry = registry
        self._runtime_provider = runtime_provider
        self.loop: asyncio.AbstractEventLoop | None = None
        self._semaphore = asyncio.Semaphore(MAX_ACTIVE_DELEGATIONS)
        self._active_parents: set[str] = set()
        self._active_children: set[str] = set()
        self._active_tasks: dict[str, tuple[Any, str, str, str]] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.loop is not None and self.loop is not loop:
            raise RuntimeError("SubagentCoordinator loop changed")
        self.loop = loop

    def prepare(
        self,
        parent_service: Any,
        parent: TaskParentIdentity,
        arguments: dict[str, str | int | bool],
    ) -> tuple[dict[str, str], str, PreparedSubagentExecutor]:
        child_agent_id = arguments.get("child_agent_id")
        message = arguments.get("message")
        if (
            self.loop is None
            or self._closing
            or not isinstance(child_agent_id, str)
            or SAFE_ID.fullmatch(child_agent_id) is None
            or child_agent_id == parent.agent_id
            or not isinstance(message, str)
            or not message.strip()
            or len(message.encode("utf-8")) > MAX_DELEGATION_MESSAGE_BYTES
            or parent.run_id in self._active_children
        ):
            raise SubagentError("delegation request is not admissible")
        try:
            child = self.registry.get(child_agent_id)
        except KeyError as exc:
            raise SubagentError("child Agent is unavailable") from exc
        if child.state != "active":
            raise SubagentError("child Agent is unavailable")
        prepared = {"child_agent_id": child_agent_id, "message": message}
        preview = (
            f"Delegate one bounded message to isolated Agent {child_agent_id}; "
            "the child receives no parent filesystem or transcript authority"
        )
        return (
            prepared,
            preview,
            PreparedSubagentExecutor(
                self, parent_service, parent, child_agent_id, message
            ),
        )

    async def _delegate(
        self,
        parent_service: Any,
        parent: TaskParentIdentity,
        child_agent_id: str,
        message: str,
    ) -> str:
        if self._closing:
            raise SubagentError("subagent coordinator is closing")
        async with self._lock:
            if (
                parent.run_id in self._active_parents
                or parent.run_id in self._active_children
            ):
                raise SubagentError("delegation depth or parent concurrency exhausted")
            self._active_parents.add(parent.run_id)
        task_id = new_id()
        task_created = False
        link_created = False
        child_service: Any | None = None
        child_conversation_id: str | None = None
        child_run_id: str | None = None
        store: SubagentStore = parent_service.subagent_store
        task_store: TaskStore = parent_service.task_store
        if store is None or task_store is None:
            async with self._lock:
                self._active_parents.discard(parent.run_id)
            raise SubagentError("subagent persistence is unavailable")
        request = _canonical(
            {"child_agent_id": child_agent_id, "message": message},
            MAX_DELEGATION_MESSAGE_BYTES + 256,
            "delegation request",
        )
        try:
            async with self._semaphore:
                task = await asyncio.to_thread(
                    task_store.create,
                    task_id=task_id,
                    capsule_generation=parent_service.capsule.generation,
                    parent=parent,
                    command_id=SUBAGENT_TASK_ID,
                    executor_identity_digest=_digest(
                        b"agent-builder-subagent-task-executor-v1",
                        child_agent_id.encode("ascii"),
                    ),
                    request_digest=_digest(
                        b"agent-builder-subagent-task-request-v1",
                        request.encode("utf-8"),
                    ),
                )
                task_created = True
                await asyncio.to_thread(
                    store.create,
                    task_id=task_id,
                    parent=parent,
                    child_agent_id=child_agent_id,
                    message=message,
                )
                link_created = True
                await asyncio.to_thread(task_store.mark_running, task_id)

                child_runtime = await self._runtime_provider(child_agent_id)
                child_service = child_runtime.run_service
                conversation = await child_service.create_conversation(
                    f"Delegated from {parent.agent_id[:8]}"
                )
                child_conversation_id = conversation.conversation_id
                child_record = await child_service.start(
                    StartRunCommand(
                        agent_id=child_agent_id,
                        conversation_id=conversation.conversation_id,
                        message=message,
                    )
                )
                child_run_id = child_record.run_id
                await asyncio.to_thread(
                    store.mark_running,
                    task_id,
                    child_conversation_id=child_record.conversation_id,
                    child_turn_id=child_record.turn_id,
                    child_run_id=child_record.run_id,
                )
                async with self._lock:
                    self._active_children.add(child_record.run_id)
                    self._active_tasks[task_id] = (
                        child_service,
                        child_record.run_id,
                        parent.run_id,
                        parent.agent_id,
                    )

                async def wait_terminal() -> None:
                    while child_record.terminal_kind is None:
                        async with child_record.condition:
                            if child_record.terminal_kind is None:
                                await child_record.condition.wait()

                await asyncio.wait_for(wait_terminal(), DELEGATION_WALL_SECONDS)
                if child_record.terminal_kind != "run.completed":
                    raise SubagentError("child Run did not complete")
                child_conversation = await child_service.get_conversation(
                    child_record.conversation_id
                )
                child_turn = next(
                    (
                        turn
                        for turn in child_conversation.turns
                        if turn.run_id == child_record.run_id
                    ),
                    None,
                )
                if child_turn is None or child_turn.assistant_content is None:
                    raise SubagentError("child answer is unavailable")
                answer = child_turn.assistant_content
                encoded = answer.encode("utf-8")
                if len(encoded) > MAX_DELEGATION_RESULT_BYTES:
                    answer = encoded[:MAX_DELEGATION_RESULT_BYTES].decode(
                        "utf-8", "ignore"
                    )
                result = {
                    "task_id": task_id,
                    "child_agent_id": child_agent_id,
                    "child_conversation_id": child_record.conversation_id,
                    "child_run_id": child_record.run_id,
                    "answer": answer,
                }
                await asyncio.to_thread(store.finish, task_id, "completed", answer)
                await asyncio.to_thread(
                    task_store.finish,
                    task_id,
                    "completed",
                    result=result,
                )
                return _canonical(
                    result, MAX_DELEGATION_RESULT_BYTES + 1024, "delegation result"
                )
        except asyncio.CancelledError:
            if child_service is not None and child_run_id is not None:
                await child_service.cancel(child_run_id)
            await self._finish_failed(
                task_store,
                store,
                task_id,
                task_created,
                link_created,
                "cancelled",
                "cancelled",
            )
            raise
        except TimeoutError:
            if child_service is not None and child_run_id is not None:
                await child_service.cancel(child_run_id)
            await self._finish_failed(
                task_store,
                store,
                task_id,
                task_created,
                link_created,
                "failed",
                "deadline_exceeded",
            )
            raise SubagentError("child Run deadline exceeded") from None
        except BaseException as exc:
            if child_service is not None and child_run_id is not None:
                await child_service.cancel(child_run_id)
            await self._finish_failed(
                task_store,
                store,
                task_id,
                task_created,
                link_created,
                "failed",
                type(exc).__name__,
            )
            raise
        finally:
            async with self._lock:
                self._active_parents.discard(parent.run_id)
                if child_run_id is not None:
                    self._active_children.discard(child_run_id)
                self._active_tasks.pop(task_id, None)

    @staticmethod
    async def _finish_failed(
        task_store: TaskStore,
        store: SubagentStore,
        task_id: str,
        task_created: bool,
        link_created: bool,
        state: str,
        error_code: str,
    ) -> None:
        if link_created:
            try:
                link = await asyncio.to_thread(store.get, task_id)
                if link.state not in TERMINAL_TASK_STATES:
                    await asyncio.to_thread(store.finish, task_id, state)
            except BaseException:
                pass
        if task_created:
            try:
                task = await asyncio.to_thread(task_store.get, task_id)
                if task.state not in TERMINAL_TASK_STATES:
                    await asyncio.to_thread(
                        task_store.finish,
                        task_id,
                        state,
                        error_code=error_code[:64],
                    )
            except BaseException:
                pass

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            active = self._active_tasks.get(task_id)
        if active is None:
            return None
        child_service, child_run_id, _parent_run_id, _parent_agent_id = active
        await child_service.cancel(child_run_id)
        return None

    async def cleanup_conversation(self, parent_service: Any, conversation_id: str) -> None:
        store: SubagentStore | None = parent_service.subagent_store
        if store is None:
            return
        links = await asyncio.to_thread(store.list_for_conversation, conversation_id)
        for link in links:
            await self.cancel_task(link.task_id)
        # Let cancelled child Runs converge before requiring terminal links.
        for _ in range(200):
            current = await asyncio.to_thread(store.list_for_conversation, conversation_id)
            if all(link.state in TERMINAL_TASK_STATES for link in current):
                links = current
                break
            await asyncio.sleep(0.01)
        for link in links:
            if link.child_conversation_id is None:
                continue
            try:
                runtime = await self._runtime_provider(link.child_agent_id)
                await runtime.run_service.delete_conversation(
                    link.child_conversation_id
                )
            except (KeyError, RuntimeError):
                # A deleted/draining child owns no reusable authority.  Agent
                # deletion removes its whole Capsule generation.
                continue
        await asyncio.to_thread(store.delete_conversation, conversation_id)

    async def cancel_agent(self, agent_id: str) -> None:
        async with self._lock:
            active = tuple(
                (task_id, value)
                for task_id, value in self._active_tasks.items()
                if value[0].agent_id == agent_id or value[3] == agent_id
            )
        for _task_id, (service, run_id, _parent_run_id, _parent_agent_id) in active:
            await service.cancel(run_id)

    async def close(self) -> None:
        self._closing = True
        async with self._lock:
            active = tuple(self._active_tasks.values())
        for service, run_id, _parent_run_id, _parent_agent_id in active:
            await service.cancel(run_id)


__all__ = [
    "MailboxMessage",
    "PreparedSubagentExecutor",
    "SubagentCoordinator",
    "SubagentError",
    "SubagentLink",
    "SubagentStore",
]

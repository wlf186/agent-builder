"""Durable background Task state and bounded singleton execution manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Literal

from .capsule import AgentCapsule, CapsuleManager, SAFE_ID
from .command_exec import (
    BOUNDED_BASH_ID,
    COMMAND_ID,
    CommandExecutor,
    PreparedCommandExecutor,
)
from .contracts import new_id, utc_now


TaskState = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]
TERMINAL_TASK_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
MAX_TASKS_PER_AGENT = 128
MAX_ACTIVE_TASKS = 4
MAX_TASK_RESULT_BYTES = 16 * 1024
MAX_TASK_NOTIFICATIONS = 4
MAX_TASK_NOTIFICATION_BYTES = 4 * 1024
TASK_RETENTION_SECONDS = 7 * 24 * 60 * 60
SUBAGENT_TASK_ID = "agent-delegate"


class TaskError(RuntimeError):
    """A Task transition or durable record failed closed."""


@dataclass(frozen=True, slots=True)
class TaskParentIdentity:
    """Minimal durable parent identity without a QueryEngine dependency."""

    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    agent_id: str
    capsule_generation: int
    conversation_id: str
    turn_id: str
    parent_run_id: str
    command_id: str
    state: TaskState
    executor_identity_digest: str
    request_digest: str
    result_json: str | None
    result_digest: str | None
    error_code: str | None
    output_bytes: int
    notification_count: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    def public_metadata(self) -> dict[str, object]:
        result = json.loads(self.result_json) if self.result_json is not None else None
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "capsule_generation": self.capsule_generation,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "parent_run_id": self.parent_run_id,
            "command_id": self.command_id,
            "state": self.state,
            "result": result,
            "result_digest": self.result_digest,
            "error_code": self.error_code,
            "output_bytes": self.output_bytes,
            "notification_count": self.notification_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TaskNotification:
    task_id: str
    sequence: int
    kind: str
    payload: dict[str, object]
    payload_digest: str
    created_at: str


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + payload).hexdigest()


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
        raise TaskError(f"invalid {field}") from exc
    if len(encoded) > maximum:
        raise TaskError(f"{field} exceeds its byte limit")
    return encoded.decode("utf-8")


class TaskStore:
    """Single durable Task owner; output is committed only at semantic boundaries."""

    def __init__(self, database: Path, agent_id: str) -> None:
        if SAFE_ID.fullmatch(agent_id) is None:
            raise ValueError("invalid TaskStore Agent identity")
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS background_tasks (
                task_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                capsule_generation INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                parent_run_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('queued','running','completed','failed','cancelled','interrupted')
                ),
                executor_identity_digest TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                result_json TEXT,
                result_digest TEXT,
                error_code TEXT,
                output_bytes INTEGER NOT NULL DEFAULT 0,
                notification_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_background_tasks_parent
                ON background_tasks(agent_id, conversation_id, parent_run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_background_tasks_state
                ON background_tasks(agent_id, state, created_at);
            CREATE TABLE IF NOT EXISTS task_notifications (
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence),
                FOREIGN KEY(task_id) REFERENCES background_tasks(task_id) ON DELETE CASCADE
            );
            """
        )
        self._connection.commit()

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(**dict(row))

    def _notify(
        self,
        cursor: sqlite3.Cursor,
        task_id: str,
        kind: str,
        payload: dict[str, object],
        now: str,
    ) -> None:
        current = cursor.execute(
            "SELECT notification_count FROM background_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if current is None or current[0] >= MAX_TASK_NOTIFICATIONS:
            raise TaskError("Task notification capacity exhausted")
        sequence = current[0] + 1
        payload_json = _canonical(
            payload, MAX_TASK_NOTIFICATION_BYTES, "Task notification"
        )
        cursor.execute(
            "INSERT INTO task_notifications VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                sequence,
                kind,
                payload_json,
                _digest(b"agent-builder-task-notification-v1", payload_json.encode()),
                now,
            ),
        )
        cursor.execute(
            "UPDATE background_tasks SET notification_count = ? WHERE task_id = ?",
            (sequence, task_id),
        )

    def create(
        self,
        *,
        task_id: str,
        capsule_generation: int,
        parent: TaskParentIdentity,
        command_id: str,
        executor_identity_digest: str,
        request_digest: str,
    ) -> TaskRecord:
        if (
            SAFE_ID.fullmatch(task_id) is None
            or parent.agent_id != self.agent_id
            or any(SAFE_ID.fullmatch(value) is None for value in (
                parent.conversation_id, parent.turn_id, parent.run_id
            ))
            or command_id not in {COMMAND_ID, BOUNDED_BASH_ID, SUBAGENT_TASK_ID}
            or len(executor_identity_digest) != 64
            or len(request_digest) != 64
        ):
            raise ValueError("invalid background Task identity")
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.cursor()
            cursor.execute(
                """DELETE FROM background_tasks
                   WHERE agent_id = ?
                     AND state IN ('completed','failed','cancelled','interrupted')
                     AND julianday(finished_at) < julianday('now', '-7 days')""",
                (self.agent_id,),
            )
            total = cursor.execute(
                "SELECT COUNT(*) FROM background_tasks WHERE agent_id = ?",
                (self.agent_id,),
            ).fetchone()[0]
            active = cursor.execute(
                "SELECT COUNT(*) FROM background_tasks WHERE agent_id = ? AND state IN ('queued','running')",
                (self.agent_id,),
            ).fetchone()[0]
            if active >= MAX_ACTIVE_TASKS:
                raise TaskError("background Task concurrency exhausted")
            if total >= MAX_TASKS_PER_AGENT:
                victim = cursor.execute(
                    "SELECT task_id FROM background_tasks WHERE agent_id = ? AND state IN ('completed','failed','cancelled','interrupted') ORDER BY finished_at, task_id LIMIT 1",
                    (self.agent_id,),
                ).fetchone()
                if victim is None:
                    raise TaskError("background Task retention exhausted")
                cursor.execute("DELETE FROM background_tasks WHERE task_id = ?", (victim[0],))
            cursor.execute(
                """INSERT INTO background_tasks(
                    task_id, agent_id, capsule_generation, conversation_id, turn_id,
                    parent_run_id, command_id, state, executor_identity_digest,
                    request_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (
                    task_id, self.agent_id, capsule_generation,
                    parent.conversation_id, parent.turn_id, parent.run_id,
                    command_id, executor_identity_digest, request_digest, now, now,
                ),
            )
            self._notify(cursor, task_id, "task.queued", {"state": "queued"}, now)
            row = cursor.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)

    def mark_running(self, task_id: str) -> TaskRecord:
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.cursor()
            changed = cursor.execute(
                "UPDATE background_tasks SET state='running', started_at=?, updated_at=? WHERE task_id=? AND agent_id=? AND state='queued'",
                (now, now, task_id, self.agent_id),
            ).rowcount
            if changed != 1:
                raise TaskError("Task cannot enter running")
            self._notify(cursor, task_id, "task.running", {"state": "running"}, now)
            row = cursor.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)

    def finish(
        self,
        task_id: str,
        state: Literal["completed", "failed", "cancelled", "interrupted"],
        *,
        result: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> TaskRecord:
        if state not in TERMINAL_TASK_STATES:
            raise ValueError("invalid Task terminal state")
        result_json = None if result is None else _canonical(
            result, MAX_TASK_RESULT_BYTES, "Task result"
        )
        result_digest = None if result_json is None else _digest(
            b"agent-builder-task-result-v1", result_json.encode()
        )
        output_bytes = 0 if result_json is None else len(result_json.encode("utf-8"))
        if error_code is not None and (
            not error_code or len(error_code.encode("ascii", "ignore")) > 64
        ):
            raise ValueError("invalid Task error code")
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.cursor()
            changed = cursor.execute(
                """UPDATE background_tasks SET state=?, result_json=?, result_digest=?,
                    error_code=?, output_bytes=?, finished_at=?, updated_at=?
                    WHERE task_id=? AND agent_id=? AND state IN ('queued','running')""",
                (
                    state, result_json, result_digest, error_code, output_bytes,
                    now, now, task_id, self.agent_id,
                ),
            ).rowcount
            if changed != 1:
                raise TaskError("Task cannot enter terminal state")
            self._notify(
                cursor,
                task_id,
                f"task.{state}",
                {
                    "state": state,
                    "result_digest": result_digest,
                    "error_code": error_code,
                    "output_bytes": output_bytes,
                },
                now,
            )
            row = cursor.execute(
                "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._record(row)

    def recover_incomplete(self) -> int:
        with self._lock:
            rows = self._connection.execute(
                "SELECT task_id FROM background_tasks WHERE agent_id=? AND state IN ('queued','running') ORDER BY task_id",
                (self.agent_id,),
            ).fetchall()
        for row in rows:
            self.finish(row[0], "interrupted", error_code="gateway_restart")
        return len(rows)

    def get(self, task_id: str) -> TaskRecord:
        if SAFE_ID.fullmatch(task_id) is None:
            raise KeyError("Task not found")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=? AND agent_id=?",
                (task_id, self.agent_id),
            ).fetchone()
        if row is None:
            raise KeyError("Task not found")
        return self._record(row)

    def list(self, limit: int = MAX_TASKS_PER_AGENT) -> tuple[TaskRecord, ...]:
        if not 1 <= limit <= MAX_TASKS_PER_AGENT:
            raise ValueError("invalid Task list limit")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM background_tasks WHERE agent_id=? ORDER BY created_at DESC, task_id DESC LIMIT ?",
                (self.agent_id, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def notifications(self, task_id: str) -> tuple[TaskNotification, ...]:
        self.get(task_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_notifications WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return tuple(
            TaskNotification(
                task_id=row["task_id"],
                sequence=row["sequence"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                payload_digest=row["payload_digest"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def delete_conversation(self, conversation_id: str) -> int:
        with self._lock, self._connection:
            active = self._connection.execute(
                "SELECT COUNT(*) FROM background_tasks WHERE agent_id=? AND conversation_id=? AND state IN ('queued','running')",
                (self.agent_id, conversation_id),
            ).fetchone()[0]
            if active:
                raise TaskError("Conversation has active background Tasks")
            changed = self._connection.execute(
                "DELETE FROM background_tasks WHERE agent_id=? AND conversation_id=?",
                (self.agent_id, conversation_id),
            ).rowcount
        return changed

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class BackgroundTaskManager:
    """Run fixed commands beyond the parent Run without an implicit daemon."""

    def __init__(
        self,
        capsule: AgentCapsule,
        capsules: CapsuleManager,
        command_executor: CommandExecutor,
        store: TaskStore,
    ) -> None:
        self.capsule = capsule
        self._capsules = capsules
        self._commands = command_executor
        self.store = store
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    async def initialize(self) -> int:
        recovered = await asyncio.to_thread(self.store.recover_incomplete)
        await asyncio.to_thread(
            self._capsules.cleanup_orphan_task_roots, self.capsule
        )
        return recovered

    async def submit(
        self,
        parent: TaskParentIdentity,
        arguments: dict[str, str] | None = None,
    ) -> TaskRecord:
        async with self._lock:
            if self._closing:
                raise TaskError("Task manager is closing")
            task_id = new_id()
            task_root = await asyncio.to_thread(
                self._capsules.create_task_root, self.capsule, task_id
            )
            try:
                requested = arguments or {"command_id": COMMAND_ID}
                prepared, _preview, executor = self._commands.prepare(requested, task_root)
                command_id = str(prepared["command_id"])
                request_json = _canonical(prepared, MAX_TASK_RESULT_BYTES, "Task request")
                record = await asyncio.to_thread(
                    self.store.create,
                    task_id=task_id,
                    capsule_generation=self.capsule.generation,
                    parent=parent,
                    command_id=command_id,
                    executor_identity_digest=executor.identity_digest,
                    request_digest=_digest(
                        b"agent-builder-task-request-v1", request_json.encode()
                    ),
                )
            except BaseException:
                await asyncio.to_thread(
                    self._capsules.remove_task_root, self.capsule, task_id
                )
                raise
            cancelled = threading.Event()
            job = asyncio.create_task(
                self._run(task_id, task_root, executor, cancelled),
                name=f"agent-builder-task-{task_id}",
            )
            self._jobs[task_id] = job
            self._cancel[task_id] = cancelled
            job.add_done_callback(lambda _value, identity=task_id: self._retire(identity))
            return record

    def _retire(self, task_id: str) -> None:
        self._jobs.pop(task_id, None)
        self._cancel.pop(task_id, None)

    async def _run(
        self,
        task_id: str,
        task_root: Path,
        executor: PreparedCommandExecutor,
        cancelled: threading.Event,
    ) -> None:
        terminal: Literal["completed", "failed", "cancelled"] = "failed"
        result: dict[str, object] | None = None
        error_code: str | None = "command_failed"
        try:
            await asyncio.to_thread(self.store.mark_running, task_id)
            raw = await asyncio.to_thread(
                executor.execute_prepared, cancelled.is_set
            )
            value = json.loads(raw)
            if cancelled.is_set():
                terminal = "cancelled"
                error_code = "cancelled"
            elif value.get("exit_code") == 0:
                terminal = "completed"
                result = value
                error_code = None
            else:
                result = value
        except BaseException as exc:
            terminal = "cancelled" if cancelled.is_set() else "failed"
            error_code = "cancelled" if cancelled.is_set() else type(exc).__name__
        finally:
            try:
                await asyncio.to_thread(
                    self._capsules.remove_task_root, self.capsule, task_id
                )
            except BaseException:
                terminal = "failed"
                result = None
                error_code = "cleanup_failed"
            try:
                current = await asyncio.to_thread(self.store.get, task_id)
                if current.state not in TERMINAL_TASK_STATES:
                    await asyncio.to_thread(
                        self.store.finish,
                        task_id,
                        terminal,
                        result=result,
                        error_code=error_code,
                    )
            except BaseException:
                pass

    async def cancel(self, task_id: str) -> TaskRecord:
        async with self._lock:
            event = self._cancel.get(task_id)
            job = self._jobs.get(task_id)
            if event is None or job is None:
                return await asyncio.to_thread(self.store.get, task_id)
            event.set()
        await asyncio.gather(job, return_exceptions=True)
        return await asyncio.to_thread(self.store.get, task_id)

    async def cancel_conversation(self, conversation_id: str) -> None:
        records = await asyncio.to_thread(self.store.list)
        for record in records:
            if (
                record.conversation_id == conversation_id
                and record.state not in TERMINAL_TASK_STATES
            ):
                await self.cancel(record.task_id)

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            events = tuple(self._cancel.values())
            jobs = tuple(self._jobs.values())
            for event in events:
                event.set()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self.store.close()


__all__ = [
    "BackgroundTaskManager",
    "MAX_ACTIVE_TASKS",
    "MAX_TASKS_PER_AGENT",
    "TaskError",
    "TaskNotification",
    "TaskParentIdentity",
    "TaskRecord",
    "TaskStore",
    "TERMINAL_TASK_STATES",
]

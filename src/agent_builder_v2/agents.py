"""Persistent, recoverable Agent Capsule registry and lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading
from typing import Protocol
from uuid import uuid4

from .capsule import (
    AgentCapsule,
    CapsuleManager,
    PROTOTYPE_AGENT_ID,
    SAFE_ID,
    SYSTEM_AGENT_DISPLAY_NAME,
)


MAX_AGENTS = 100
_STATES = frozenset(
    {"provisioning", "active", "renaming", "upgrading", "deleting"}
)
_LEGACY_SYSTEM_AGENT_DISPLAY_NAME = "Harness V2 Prototype Agent"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class AgentRecord:
    agent_id: str
    display_name: str
    generation: int
    target_generation: int | None
    state: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if (
            SAFE_ID.fullmatch(self.agent_id) is None
            or not self.display_name.strip()
            or len(self.display_name.encode("utf-8")) > 128
            or not 1 <= self.generation <= 1_000_000_000
            or self.state not in _STATES
            or (
                self.state == "upgrading"
                and self.target_generation != self.generation + 1
            )
            or (self.state != "upgrading" and self.target_generation is not None)
        ):
            raise ValueError("invalid Agent registry record")

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "generation": self.generation,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AgentLifecycleHooks(Protocol):
    def drain(self, agent_id: str) -> None: ...

    def retire(self, agent_id: str) -> None: ...

    def residual_references(self, agent_id: str) -> tuple[str, ...]: ...


class _NoopHooks:
    def drain(self, agent_id: str) -> None:
        del agent_id

    def retire(self, agent_id: str) -> None:
        del agent_id

    def residual_references(self, agent_id: str) -> tuple[str, ...]:
        del agent_id
        return ()


class AgentRegistry:
    """SQLite authority; filesystem mutations converge from durable states."""

    def __init__(
        self,
        repository_root: Path,
        *,
        hooks: AgentLifecycleHooks | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.capsules = CapsuleManager(self.repository_root)
        self.hooks = hooks or _NoopHooks()
        self._lock = threading.RLock()
        data_root = self.repository_root / "data"
        data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(data_root, 0o700)
        self.path = data_root / "agent-registry.sqlite"
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                target_generation INTEGER,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> AgentRecord:
        return AgentRecord(
            agent_id=str(row[0]),
            display_name=str(row[1]),
            generation=int(row[2]),
            target_generation=(None if row[3] is None else int(row[3])),
            state=str(row[4]),
            created_at=str(row[5]),
            updated_at=str(row[6]),
        )

    def _get_locked(self, agent_id: str) -> AgentRecord:
        row = self._connection.execute(
            "SELECT agent_id,display_name,generation,target_generation,state,created_at,updated_at "
            "FROM agents WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Agent not found")
        return self._from_row(row)

    def initialize(self) -> None:
        with self._lock:
            try:
                prototype = self._get_locked(PROTOTYPE_AGENT_ID)
            except KeyError:
                timestamp = _now()
                self._connection.execute(
                    "INSERT INTO agents VALUES (?,?,?,?,?,?,?)",
                    (
                        PROTOTYPE_AGENT_ID,
                        SYSTEM_AGENT_DISPLAY_NAME,
                        1,
                        None,
                        "provisioning",
                        timestamp,
                        timestamp,
                    ),
                )
                self._connection.commit()
                prototype = self._get_locked(PROTOTYPE_AGENT_ID)
            recovered = self._recover_locked(prototype)
            if recovered is None:
                timestamp = _now()
                self._connection.execute(
                    "INSERT INTO agents VALUES (?,?,?,?,?,?,?)",
                    (
                        PROTOTYPE_AGENT_ID,
                        SYSTEM_AGENT_DISPLAY_NAME,
                        1,
                        None,
                        "provisioning",
                        timestamp,
                        timestamp,
                    ),
                )
                self._connection.commit()
                recovered = self._recover_locked(
                    self._get_locked(PROTOTYPE_AGENT_ID)
                )
                assert recovered is not None
            if recovered.display_name == _LEGACY_SYSTEM_AGENT_DISPLAY_NAME:
                prototype = self._set_state_locked(
                    recovered.agent_id,
                    state="upgrading",
                    generation=recovered.generation,
                    target_generation=recovered.generation + 1,
                    display_name=SYSTEM_AGENT_DISPLAY_NAME,
                )
                migrated = self._recover_locked(prototype)
                assert migrated is not None
            for record in self.list():
                if record.agent_id != PROTOTYPE_AGENT_ID and record.state != "active":
                    self._recover_locked(record)

    def _set_state_locked(
        self,
        agent_id: str,
        *,
        state: str,
        generation: int,
        target_generation: int | None,
        display_name: str,
    ) -> AgentRecord:
        if state not in _STATES:
            raise ValueError("invalid Agent state")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "UPDATE agents SET display_name=?,generation=?,target_generation=?,state=?,updated_at=? "
                "WHERE agent_id=?",
                (display_name, generation, target_generation, state, _now(), agent_id),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self._get_locked(agent_id)

    def _recover_locked(self, record: AgentRecord) -> AgentRecord | None:
        if record.state == "provisioning":
            self.capsules.ensure_agent(
                record.agent_id,
                display_name=record.display_name,
                generation=record.generation,
            )
            return self._set_state_locked(
                record.agent_id,
                state="active",
                generation=record.generation,
                target_generation=None,
                display_name=record.display_name,
            )
        if record.state == "renaming":
            current = self.capsules.load_agent(record.agent_id)
            if current.generation != record.generation:
                raise RuntimeError("Agent rename recovery found generation drift")
            current = self.capsules.rename_agent(
                current, display_name=record.display_name
            )
            return self._set_state_locked(
                record.agent_id,
                state="active",
                generation=current.generation,
                target_generation=None,
                display_name=current.display_name,
            )
        if record.state == "upgrading":
            current = self.capsules.load_agent(record.agent_id)
            if current.generation == record.generation:
                prepared = self.capsules.prepare_generation(
                    current, display_name=record.display_name
                )
                current = self.capsules.promote_generation(current, prepared)
            if current.generation != record.target_generation:
                raise RuntimeError("Agent upgrade recovery found generation drift")
            self.capsules.retire_generation(
                agent_id=record.agent_id,
                runtime_root=current.runtime_root,
                generation=record.generation,
            )
            return self._set_state_locked(
                record.agent_id,
                state="active",
                generation=current.generation,
                target_generation=None,
                display_name=current.display_name,
            )
        if record.state == "deleting":
            self._delete_files_locked(record)
            return None
        return record

    def create(self, display_name: str) -> AgentRecord:
        if (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name.encode("utf-8")) > 128
        ):
            raise ValueError("invalid Agent display name")
        with self._lock:
            count = int(self._connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0])
            if count >= MAX_AGENTS:
                raise RuntimeError("Agent capacity exhausted")
            agent_id = uuid4().hex
            timestamp = _now()
            self._connection.execute(
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?)",
                (agent_id, display_name, 1, None, "provisioning", timestamp, timestamp),
            )
            self._connection.commit()
            result = self._recover_locked(self._get_locked(agent_id))
            assert result is not None
            return result

    def list(self) -> tuple[AgentRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT agent_id,display_name,generation,target_generation,state,created_at,updated_at "
                "FROM agents ORDER BY created_at,agent_id LIMIT ?",
                (MAX_AGENTS + 1,),
            ).fetchall()
            if len(rows) > MAX_AGENTS:
                raise RuntimeError("Agent registry exceeded its bound")
            return tuple(self._from_row(row) for row in rows)

    def get(self, agent_id: str) -> AgentRecord:
        if SAFE_ID.fullmatch(agent_id) is None:
            raise KeyError("Agent not found")
        with self._lock:
            return self._get_locked(agent_id)

    def rename(self, agent_id: str, *, display_name: str) -> AgentRecord:
        if (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name.encode("utf-8")) > 128
        ):
            raise ValueError("invalid Agent display name")
        with self._lock:
            current = self._get_locked(agent_id)
            if current.state != "active":
                raise RuntimeError("Agent is not active")
            if current.display_name == display_name:
                return current
            record = self._set_state_locked(
                agent_id,
                state="renaming",
                generation=current.generation,
                target_generation=None,
                display_name=display_name,
            )
            result = self._recover_locked(record)
            assert result is not None
            return result

    def upgrade(self, agent_id: str, *, display_name: str | None = None) -> AgentRecord:
        with self._lock:
            current_record = self._get_locked(agent_id)
            if current_record.state != "active":
                raise RuntimeError("Agent is not active")
            self.hooks.drain(agent_id)
            if self.hooks.residual_references(agent_id):
                raise RuntimeError("Agent still has active runtime references")
            name = display_name if display_name is not None else current_record.display_name
            record = self._set_state_locked(
                agent_id,
                state="upgrading",
                generation=current_record.generation,
                target_generation=current_record.generation + 1,
                display_name=name,
            )
            result = self._recover_locked(record)
            assert result is not None
            return result

    def delete(self, agent_id: str) -> None:
        with self._lock:
            record = self._get_locked(agent_id)
            if record.state != "deleting":
                self.hooks.drain(agent_id)
                record = self._set_state_locked(
                    agent_id,
                    state="deleting",
                    generation=record.generation,
                    target_generation=None,
                    display_name=record.display_name,
                )
            self._delete_files_locked(record)

    def _delete_files_locked(self, record: AgentRecord) -> None:
        self.hooks.drain(record.agent_id)
        self.hooks.retire(record.agent_id)
        references = self.hooks.residual_references(record.agent_id)
        if references:
            raise RuntimeError("Agent residual references remain: " + ",".join(references))
        try:
            capsule = self.capsules.load_agent(record.agent_id)
        except FileNotFoundError:
            data_root = self.capsules.data_agents / record.agent_id
            runtime_root = self.capsules.runtime_agents / record.agent_id
            if data_root.exists() or runtime_root.exists():
                raise RuntimeError("Agent deletion left a partial Capsule")
        else:
            self.capsules.delete_agent(capsule)
        self._connection.execute("DELETE FROM agents WHERE agent_id=?", (record.agent_id,))
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "AgentLifecycleHooks",
    "AgentRecord",
    "AgentRegistry",
    "MAX_AGENTS",
]

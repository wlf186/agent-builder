"""Framework-neutral commands and events for the V2 walking skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


SCHEMA_VERSION = "2.0-prototype"
TERMINAL_KINDS = frozenset({"run.completed", "run.failed", "run.cancelled"})
MAX_MESSAGE_BYTES = 8_192
Durability = Literal["durable", "ephemeral"]


def new_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class StartRunCommand:
    agent_id: str
    message: str

    def validate(self) -> None:
        if not self.agent_id or len(self.agent_id) > 64:
            raise ValueError("invalid agent_id")
        if not self.message.strip():
            raise ValueError("message must not be empty")
        if len(self.message) > MAX_MESSAGE_BYTES or len(
            self.message.encode("utf-8")
        ) > MAX_MESSAGE_BYTES:
            raise ValueError("message exceeds 8192 UTF-8 bytes")


@dataclass(frozen=True)
class WorkerEvent:
    """An identity-free event emitted by an untrusted Run Worker."""

    kind: str
    durability: Durability
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "durability": self.durability,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    seq: int
    occurred_at: str
    kind: str
    durability: Durability
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "parent_run_id": None,
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "kind": self.kind,
            "durability": self.durability,
            "payload": self.payload,
        }

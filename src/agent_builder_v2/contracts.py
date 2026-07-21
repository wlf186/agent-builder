"""Framework-neutral commands and events for the runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Literal
from uuid import uuid4


SCHEMA_VERSION = "2.2-prototype"
TERMINAL_KINDS = frozenset({"run.completed", "run.failed", "run.cancelled"})
MAX_MESSAGE_BYTES = 8_192
MAX_MODEL_ITERATIONS = 8
MAX_TOOL_CALLS = 8
# The Control Plane reserves this complete live sequence band before releasing
# a Worker. Persisted managed-Run metadata must bind to this exact protocol
# value so recovery cannot reuse a cursor already observed by an SSE client.
RUN_CURSOR_RESERVED_THROUGH = 512
RESOURCE_ID = re.compile(r"^[a-f0-9]{32}$")
Durability = Literal["durable", "ephemeral"]


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Trusted per-Turn loop limits sent to the isolated Worker."""

    max_model_iterations: int
    max_tool_calls: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_model_iterations, int)
            or isinstance(self.max_model_iterations, bool)
            or not 1 <= self.max_model_iterations <= MAX_MODEL_ITERATIONS
            or not isinstance(self.max_tool_calls, int)
            or isinstance(self.max_tool_calls, bool)
            or not 0 <= self.max_tool_calls <= MAX_TOOL_CALLS
            or self.max_tool_calls >= self.max_model_iterations
        ):
            raise ValueError("invalid loop limits")

    @classmethod
    def from_dict(cls, value: object) -> LoopLimits:
        if not isinstance(value, dict) or set(value) != {
            "max_model_iterations",
            "max_tool_calls",
        }:
            raise ValueError("invalid loop limits")
        return cls(
            max_model_iterations=value["max_model_iterations"],
            max_tool_calls=value["max_tool_calls"],
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_model_iterations": self.max_model_iterations,
            "max_tool_calls": self.max_tool_calls,
        }


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
    conversation_id: str | None = None
    model_id: str | None = None
    compact: bool = False

    def validate(self) -> None:
        if not self.agent_id or len(self.agent_id) > 64:
            raise ValueError("invalid agent_id")
        if not self.message.strip():
            raise ValueError("message must not be empty")
        if len(self.message) > MAX_MESSAGE_BYTES or len(
            self.message.encode("utf-8")
        ) > MAX_MESSAGE_BYTES:
            raise ValueError("message exceeds 8192 UTF-8 bytes")
        if self.conversation_id is not None and not RESOURCE_ID.fullmatch(
            self.conversation_id
        ):
            raise ValueError("invalid conversation_id")
        if self.model_id is not None and (
            not isinstance(self.model_id, str)
            or re.fullmatch(r"[A-Za-z0-9._:/+-]{1,128}", self.model_id) is None
        ):
            raise ValueError("invalid model_id")
        if not isinstance(self.compact, bool):
            raise ValueError("invalid compact flag")


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

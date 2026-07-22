"""Versioned completed-Turn bundles used as canonical future model history."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal


COMPLETED_TURN_CONTEXT_VERSION = "completed-turn-context-v1"
MAX_CONTEXT_ITEMS_PER_TURN = 6
MAX_COMPLETED_TURN_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_TOOL_ARGUMENT_BYTES = 8 * 1024
MAX_CONTEXT_TOOL_RESULT_BYTES = 16 * 1024
ContextItemKind = Literal[
    "user", "assistant_tool_use", "tool_result_receipt", "assistant_final"
]

_ID = re.compile(r"^[a-f0-9]{32}$")
_AGENT_ID = re.compile(r"^[a-f0-9-]{32,64}$")
_TOOL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class CompletedTurnContextError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_digest(content: str) -> str:
    return hashlib.sha256(
        b"agent-builder-completed-context-content-v1\0" + content.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CompletedContextItem:
    item_index: int
    kind: ContextItemKind
    content: str
    content_digest: str
    call_id: str | None = None
    tool_id: str | None = None
    provider_name: str | None = None
    arguments: dict[str, str | int | bool] | None = None
    arguments_digest: str | None = None
    outcome: str | None = None
    original_bytes: int | None = None
    projection_reason: str | None = None
    projection_digest: str | None = None

    def __post_init__(self) -> None:
        try:
            encoded = self.content.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            raise CompletedTurnContextError("invalid context item content") from None
        if (
            not 0 <= self.item_index < MAX_CONTEXT_ITEMS_PER_TURN
            or self.kind not in {
                "user", "assistant_tool_use", "tool_result_receipt", "assistant_final"
            }
            or not isinstance(self.content, str)
            or (
                self.kind in {"user", "assistant_final"}
                and not self.content.strip()
            )
            or self.content_digest != _content_digest(self.content)
        ):
            raise CompletedTurnContextError("invalid completed context item")
        if self.kind in {"user", "assistant_final"}:
            # Pair-only rows written before completed-context-v1 may contain
            # the historical 16 KiB session-store assistant maximum. The
            # parent bundle narrows new complete_v1 answers to the Broker's
            # 12 KiB commit ceiling.
            limit = 8 * 1024 if self.kind == "user" else 16 * 1024
            if len(encoded) > limit or any(
                value is not None
                for value in (
                    self.call_id, self.tool_id, self.provider_name, self.arguments,
                    self.arguments_digest, self.outcome, self.original_bytes,
                    self.projection_reason, self.projection_digest,
                )
            ):
                raise CompletedTurnContextError("invalid plain context item")
        elif self.kind == "assistant_tool_use":
            if (
                self.content != ""
                or not isinstance(self.call_id, str)
                or _CALL_ID.fullmatch(self.call_id) is None
                or not isinstance(self.tool_id, str)
                or _TOOL_ID.fullmatch(self.tool_id) is None
                or not isinstance(self.provider_name, str)
                or _PROVIDER_NAME.fullmatch(self.provider_name) is None
                or not isinstance(self.arguments, dict)
                or len(_canonical(self.arguments)) > MAX_CONTEXT_TOOL_ARGUMENT_BYTES
                or self.arguments_digest
                != hashlib.sha256(
                    b"agent-builder-tool-arguments-v1\0" + _canonical(self.arguments)
                ).hexdigest()
                or any(
                    value is not None
                    for value in (
                        self.outcome, self.original_bytes, self.projection_reason,
                        self.projection_digest,
                    )
                )
            ):
                raise CompletedTurnContextError("invalid Tool-use context item")
        else:
            if (
                len(encoded) > MAX_CONTEXT_TOOL_RESULT_BYTES
                or not isinstance(self.call_id, str)
                or _CALL_ID.fullmatch(self.call_id) is None
                or not isinstance(self.tool_id, str)
                or _TOOL_ID.fullmatch(self.tool_id) is None
                or not isinstance(self.provider_name, str)
                or _PROVIDER_NAME.fullmatch(self.provider_name) is None
                or self.arguments is not None
                or self.arguments_digest is not None
                or self.outcome not in {"succeeded", "failed", "cancelled"}
                or not isinstance(self.original_bytes, int)
                or isinstance(self.original_bytes, bool)
                or not 0 <= self.original_bytes <= 65_536
                or self.projection_reason
                not in {"none", "provider_projection_limit", "context_headroom"}
                or not isinstance(self.projection_digest, str)
                or _DIGEST.fullmatch(self.projection_digest) is None
            ):
                raise CompletedTurnContextError("invalid Tool-result context item")

    @classmethod
    def plain(cls, index: int, kind: Literal["user", "assistant_final"], content: str) -> "CompletedContextItem":
        return cls(index, kind, content, _content_digest(content))

    @classmethod
    def tool_use(
        cls, index: int, *, call_id: str, tool_id: str, provider_name: str,
        arguments: dict[str, str | int | bool]
    ) -> "CompletedContextItem":
        encoded = _canonical(arguments)
        return cls(
            index, "assistant_tool_use", "", _content_digest(""), call_id,
            tool_id, provider_name, dict(arguments),
            hashlib.sha256(b"agent-builder-tool-arguments-v1\0" + encoded).hexdigest(),
        )

    @classmethod
    def tool_result(
        cls, index: int, *, call_id: str, tool_id: str, provider_name: str,
        content: str, outcome: str, original_bytes: int,
        projection_reason: str, projection_digest: str,
    ) -> "CompletedContextItem":
        return cls(
            index, "tool_result_receipt", content, _content_digest(content),
            call_id, tool_id, provider_name, None, None, outcome, original_bytes,
            projection_reason, projection_digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "item_index": self.item_index,
            "kind": self.kind,
            "content": self.content,
            "content_digest": self.content_digest,
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "provider_name": self.provider_name,
            "arguments": self.arguments,
            "arguments_digest": self.arguments_digest,
            "outcome": self.outcome,
            "original_bytes": self.original_bytes,
            "projection_reason": self.projection_reason,
            "projection_digest": self.projection_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CompletedContextItem":
        expected = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != expected:
            raise CompletedTurnContextError("invalid context item fields")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CompletedTurnContext:
    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    position: int
    model_profile_digest: str
    context_plan_digest: str
    items: tuple[CompletedContextItem, ...]
    history_fidelity: str = "complete_v1"
    version: str = COMPLETED_TURN_CONTEXT_VERSION

    def __post_init__(self) -> None:
        if (
            self.version != COMPLETED_TURN_CONTEXT_VERSION
            or _AGENT_ID.fullmatch(self.agent_id) is None
            or any(_ID.fullmatch(value) is None for value in (
                self.conversation_id, self.turn_id, self.run_id
            ))
            or not 1 <= self.position <= 128
            or _DIGEST.fullmatch(self.model_profile_digest) is None
            or _DIGEST.fullmatch(self.context_plan_digest) is None
            or self.history_fidelity not in {"complete_v1", "pair_only_legacy"}
            or not 2 <= len(self.items) <= MAX_CONTEXT_ITEMS_PER_TURN
            or tuple(item.item_index for item in self.items) != tuple(range(len(self.items)))
            or self.items[0].kind != "user"
            or self.items[-1].kind != "assistant_final"
            or len(_canonical(self.to_dict())) > MAX_COMPLETED_TURN_CONTEXT_BYTES
        ):
            raise CompletedTurnContextError("invalid completed Turn context")
        pending: dict[str, str] = {}
        for item in self.items[1:-1]:
            if item.kind == "assistant_tool_use":
                assert item.call_id is not None and item.tool_id is not None
                if item.call_id in pending:
                    raise CompletedTurnContextError("duplicate Tool call identity")
                pending[item.call_id] = item.tool_id
            elif item.kind == "tool_result_receipt":
                assert item.call_id is not None
                if pending.pop(item.call_id, None) != item.tool_id:
                    raise CompletedTurnContextError("Tool result order changed")
            else:
                raise CompletedTurnContextError("invalid completed Tool sequence")
        if pending:
            raise CompletedTurnContextError("completed Tool call has no result")
        if (
            self.history_fidelity == "complete_v1"
            and len(self.items[-1].content.encode("utf-8")) > 12 * 1024
        ):
            raise CompletedTurnContextError("completed assistant exceeds commit ceiling")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "position": self.position,
            "model_profile_digest": self.model_profile_digest,
            "context_plan_digest": self.context_plan_digest,
            "history_fidelity": self.history_fidelity,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, value: object) -> "CompletedTurnContext":
        expected = {
            "version", "agent_id", "conversation_id", "turn_id", "run_id",
            "position", "model_profile_digest", "context_plan_digest",
            "history_fidelity", "items",
        }
        if not isinstance(value, dict) or set(value) != expected or not isinstance(value["items"], list):
            raise CompletedTurnContextError("invalid completed Turn context fields")
        return cls(
            agent_id=value["agent_id"],  # type: ignore[arg-type]
            conversation_id=value["conversation_id"],  # type: ignore[arg-type]
            turn_id=value["turn_id"],  # type: ignore[arg-type]
            run_id=value["run_id"],  # type: ignore[arg-type]
            position=value["position"],  # type: ignore[arg-type]
            model_profile_digest=value["model_profile_digest"],  # type: ignore[arg-type]
            context_plan_digest=value["context_plan_digest"],  # type: ignore[arg-type]
            history_fidelity=value["history_fidelity"],  # type: ignore[arg-type]
            version=value["version"],  # type: ignore[arg-type]
            items=tuple(CompletedContextItem.from_dict(item) for item in value["items"]),
        )

    def provider_messages(self) -> tuple[dict[str, object], ...]:
        messages: list[dict[str, object]] = []
        for item in self.items:
            if item.kind == "user":
                messages.append({"role": "user", "content": item.content})
            elif item.kind == "assistant_tool_use":
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": item.call_id,
                        "function": {
                            "index": 0,
                            "name": item.provider_name,
                            "arguments": item.arguments,
                        },
                    }],
                })
            elif item.kind == "tool_result_receipt":
                messages.append({
                    "role": "tool",
                    "tool_name": item.provider_name,
                    "content": item.content,
                })
            else:
                messages.append({"role": "assistant", "content": item.content})
        return tuple(messages)


__all__ = [
    "COMPLETED_TURN_CONTEXT_VERSION", "CompletedContextItem",
    "CompletedTurnContext", "CompletedTurnContextError",
]

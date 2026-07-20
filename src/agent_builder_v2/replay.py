"""Strict, framework-neutral durable Run replay primitives.

The journal is a trust boundary.  This module decodes a complete bounded Run
before a caller can publish any part of it, and projects the durable semantic
subsequence without pretending that ephemeral token deltas survived restart.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Literal, Sequence

from .context import (
    MAX_CONTEXT_SECTIONS,
    MAX_HISTORY_MESSAGES,
    MAX_NATIVE_CONTEXT_TOKENS,
    MAX_OPERATIONAL_CONTEXT_TOKENS,
    MAX_PROVIDER_REQUEST_BYTES,
    MIN_OPERATIONAL_CONTEXT_TOKENS,
    PROVIDER_TEMPLATE_TOKEN_RESERVE,
    TOKEN_ESTIMATOR_ID,
)
from .contracts import (
    RUN_CURSOR_RESERVED_THROUGH,
    SCHEMA_VERSION,
    TERMINAL_KINDS,
    EventEnvelope,
)
from .tools import (
    PROTOTYPE_ECHO_SPEC_V1,
    ToolSpec,
    prototype_tool_specs,
    toolset_digest,
)


MAX_DURABLE_EVENT_BYTES = 65_536
MAX_REPLAY_EVENTS = RUN_CURSOR_RESERVED_THROUGH
MAX_REPLAY_BYTES = 256 * 1024
MAX_REPLAY_SEQUENCE = 1_000_000
MAX_REPLAY_PAGE = 128
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4_096
MAX_OBJECT_FIELDS = 128
MAX_ARRAY_ITEMS = 256
MAX_STRING_BYTES = 16_384
MAX_FIELD_NAME_BYTES = 128
MAX_SNAPSHOT_BYTES = 65_536
MAX_WORKER_TEXT_BYTES = 12_288
MAX_USAGE_TOKENS = 1_000_000_000
PROJECTION_VERSION = "run-ui-v2"
LEGACY_PROJECTION_VERSION = "run-ui-v1"
MODEL_BOUNDARY_FEATURE = "model-call-boundaries-v1"
MULTI_TOOL_LOOP_FEATURE = "sequential-multi-tool-v1"
SANDBOX_POLICY = "harness-v2-worker-v1"

_RESOURCE_ID = re.compile(r"^[a-f0-9]{32}$")
_AGENT_ID = re.compile(r"^[a-f0-9-]{32,64}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_WORKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_PLAN_ID = re.compile(r"^context-[a-f0-9]{24}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._:/+-]{1,128}$")
_TOOL_SPECS: dict[str, ToolSpec] = {
    spec.tool_id: spec for spec in prototype_tool_specs()
}
_VISIBLE_TOOL_IDS = tuple(spec.tool_id for spec in prototype_tool_specs())
_TOOLSET_DIGEST = toolset_digest(prototype_tool_specs())
_LEGACY_TOOLSET_DIGEST = toolset_digest((PROTOTYPE_ECHO_SPEC_V1,))
_DURABLE_KINDS = frozenset(
    {
        "run.started",
        "model.request.started",
        "model.response.finished",
        "assistant.block.started",
        "assistant.block.finished",
        "assistant.block.discarded",
        "tool.call.requested",
        "tool.call.started",
        "tool.call.finished",
        *TERMINAL_KINDS,
    }
)
GapReason = Literal[
    "ephemeral_not_durable", "retention", "journal_unavailable"
]
ReplayAvailability = Literal[
    "complete", "partial", "snapshot_only", "unavailable"
]


class ReplayCorruptionError(ValueError):
    """A purported canonical stream cannot be decoded or projected safely."""


@dataclass(frozen=True, slots=True)
class RunIdentity:
    agent_id: str
    conversation_id: str
    turn_id: str
    run_id: str

    @classmethod
    def from_event(cls, event: EventEnvelope) -> "RunIdentity":
        return cls(
            event.agent_id,
            event.conversation_id,
            event.turn_id,
            event.run_id,
        )


@dataclass(frozen=True, slots=True)
class ReplayGap:
    from_seq: int
    to_seq: int
    reason: GapReason

    def __post_init__(self) -> None:
        if (
            isinstance(self.from_seq, bool)
            or isinstance(self.to_seq, bool)
            or not 1 <= self.from_seq <= self.to_seq <= MAX_REPLAY_SEQUENCE
        ):
            raise ValueError("invalid replay gap")

    def to_dict(self) -> dict[str, object]:
        return {
            "from_seq": self.from_seq,
            "to_seq": self.to_seq,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    """A deterministic UI projection, encoded canonically and digest-bound."""

    identity: RunIdentity
    through_seq: int
    complete: bool
    document_json: str
    digest: str
    version: str = PROJECTION_VERSION

    @property
    def document(self) -> dict[str, object]:
        value = json.loads(self.document_json)
        assert isinstance(value, dict)
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "agent_id": self.identity.agent_id,
            "conversation_id": self.identity.conversation_id,
            "turn_id": self.identity.turn_id,
            "run_id": self.identity.run_id,
            "through_seq": self.through_seq,
            "complete": self.complete,
            "document": self.document,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class DurableReplay:
    identity: RunIdentity
    availability: ReplayAvailability
    oldest_cursor: int
    latest_cursor: int
    next_cursor: int
    has_more: bool
    events: tuple[EventEnvelope, ...]
    gaps: tuple[ReplayGap, ...]
    snapshot: ProjectionSnapshot


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len(pairs) > MAX_OBJECT_FIELDS:
        raise ReplayCorruptionError("JSON object has too many fields")
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayCorruptionError("JSON object has duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ReplayCorruptionError(f"invalid JSON constant: {value}")


def _validate_json_shape(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ReplayCorruptionError("JSON structure exceeds its replay limit")
        if isinstance(current, dict):
            if len(current) > MAX_OBJECT_FIELDS:
                raise ReplayCorruptionError("JSON object has too many fields")
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ReplayCorruptionError("JSON field name is not text")
                if len(key.encode("utf-8")) > MAX_FIELD_NAME_BYTES:
                    raise ReplayCorruptionError("JSON field name is too large")
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_ARRAY_ITEMS:
                raise ReplayCorruptionError("JSON array has too many items")
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_STRING_BYTES:
                raise ReplayCorruptionError("JSON string is too large")
        elif current is None or isinstance(current, bool):
            continue
        elif isinstance(current, int):
            if not -(2**63) <= current <= 2**63 - 1:
                raise ReplayCorruptionError("JSON integer is out of range")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ReplayCorruptionError("JSON number is not finite")
        else:
            raise ReplayCorruptionError("JSON contains an unsupported value")


def _decode_json(raw: bytes) -> dict[str, object]:
    if not 2 <= len(raw) <= MAX_DURABLE_EVENT_BYTES:
        raise ReplayCorruptionError("durable event has an invalid size")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        _validate_json_shape(value)
    except ReplayCorruptionError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ReplayCorruptionError("durable event is not strict bounded JSON") from exc
    if not isinstance(value, dict):
        raise ReplayCorruptionError("durable event is not a JSON object")
    return value


def decode_durable_event(
    raw: bytes,
    *,
    column_run_id: object,
    column_seq: object,
    column_kind: object,
    column_occurred_at: object,
) -> EventEnvelope:
    """Decode one row while binding duplicated JSON fields to SQL metadata."""

    value = _decode_json(raw)
    expected_keys = {
        "schema_version",
        "event_id",
        "agent_id",
        "conversation_id",
        "turn_id",
        "run_id",
        "parent_run_id",
        "seq",
        "occurred_at",
        "kind",
        "durability",
        "payload",
    }
    if set(value) != expected_keys:
        raise ReplayCorruptionError("durable event envelope fields are invalid")

    event_id = value.get("event_id")
    agent_id = value.get("agent_id")
    conversation_id = value.get("conversation_id")
    turn_id = value.get("turn_id")
    run_id = value.get("run_id")
    seq = value.get("seq")
    occurred_at = value.get("occurred_at")
    kind = value.get("kind")
    payload = value.get("payload")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(event_id, str)
        or _RESOURCE_ID.fullmatch(event_id) is None
        or not isinstance(agent_id, str)
        or _AGENT_ID.fullmatch(agent_id) is None
        or not isinstance(conversation_id, str)
        or _RESOURCE_ID.fullmatch(conversation_id) is None
        or not isinstance(turn_id, str)
        or _RESOURCE_ID.fullmatch(turn_id) is None
        or not isinstance(run_id, str)
        or _RESOURCE_ID.fullmatch(run_id) is None
        or value.get("parent_run_id") is not None
        or not isinstance(seq, int)
        or isinstance(seq, bool)
        or not 1 <= seq <= MAX_REPLAY_SEQUENCE
        or not isinstance(occurred_at, str)
        or _TIMESTAMP.fullmatch(occurred_at) is None
        or not isinstance(kind, str)
        or kind not in _DURABLE_KINDS
        or value.get("durability") != "durable"
        or not isinstance(payload, dict)
        or column_run_id != run_id
        or column_seq != seq
        or isinstance(column_seq, bool)
        or column_kind != kind
        or column_occurred_at != occurred_at
    ):
        raise ReplayCorruptionError("durable event envelope is inconsistent")

    return EventEnvelope(
        event_id=event_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        seq=seq,
        occurred_at=occurred_at,
        kind=kind,
        durability="durable",
        payload=payload,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _snapshot_digest(
    identity: RunIdentity,
    through_seq: int,
    complete: bool,
    document: dict[str, object],
    *,
    version: str = PROJECTION_VERSION,
) -> tuple[str, bytes]:
    unsigned = {
        "version": version,
        "agent_id": identity.agent_id,
        "conversation_id": identity.conversation_id,
        "turn_id": identity.turn_id,
        "run_id": identity.run_id,
        "through_seq": through_seq,
        "complete": complete,
        "document": document,
    }
    encoded = _canonical_json(unsigned).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), encoded


def encode_projection_snapshot(snapshot: ProjectionSnapshot) -> str:
    """Encode one digest-bound snapshot for durable storage."""

    return _canonical_json(snapshot.to_dict())


def decode_projection_snapshot(
    raw: bytes,
    *,
    expected_identity: RunIdentity,
    expected_through_seq: int,
) -> ProjectionSnapshot:
    """Strictly decode a persisted ``run-ui-v1`` projection."""

    value = _decode_json(raw)
    if set(value) != {
        "version",
        "agent_id",
        "conversation_id",
        "turn_id",
        "run_id",
        "through_seq",
        "complete",
        "document",
        "digest",
    }:
        raise ReplayCorruptionError("projection snapshot fields are invalid")
    identity = RunIdentity(
        value.get("agent_id"),  # type: ignore[arg-type]
        value.get("conversation_id"),  # type: ignore[arg-type]
        value.get("turn_id"),  # type: ignore[arg-type]
        value.get("run_id"),  # type: ignore[arg-type]
    )
    through_seq = value.get("through_seq")
    complete = value.get("complete")
    document = value.get("document")
    digest = value.get("digest")
    version = value.get("version")
    if (
        version not in {LEGACY_PROJECTION_VERSION, PROJECTION_VERSION}
        or identity != expected_identity
        or not isinstance(identity.agent_id, str)
        or _AGENT_ID.fullmatch(identity.agent_id) is None
        or not isinstance(identity.conversation_id, str)
        or _RESOURCE_ID.fullmatch(identity.conversation_id) is None
        or not isinstance(identity.turn_id, str)
        or _RESOURCE_ID.fullmatch(identity.turn_id) is None
        or not isinstance(identity.run_id, str)
        or _RESOURCE_ID.fullmatch(identity.run_id) is None
        or not isinstance(through_seq, int)
        or isinstance(through_seq, bool)
        or not 1 <= through_seq <= MAX_REPLAY_SEQUENCE
        or through_seq != expected_through_seq
        or not isinstance(complete, bool)
        or not isinstance(document, dict)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ReplayCorruptionError("projection snapshot is inconsistent")
    expected_digest, encoded_unsigned = _snapshot_digest(
        identity, through_seq, complete, document, version=str(version)
    )
    if len(encoded_unsigned) > MAX_SNAPSHOT_BYTES or digest != expected_digest:
        raise ReplayCorruptionError("projection snapshot digest is invalid")
    _validate_snapshot_document(
        document,
        through_seq=through_seq,
        complete=complete,
        version=str(version),
    )
    return ProjectionSnapshot(
        identity=identity,
        through_seq=through_seq,
        complete=complete,
        document_json=_canonical_json(document),
        digest=digest,
        version=str(version),
    )


def _exact_payload(
    event: EventEnvelope, fields: set[str]
) -> dict[str, object]:
    if set(event.payload) != fields:
        raise ReplayCorruptionError(f"invalid {event.kind} payload")
    return event.payload


def _worker_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _WORKER_ID.fullmatch(value) is None:
        raise ReplayCorruptionError(f"invalid {field}")
    return value


def _bounded_text(
    value: object,
    *,
    maximum_bytes: int,
    field: str,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ReplayCorruptionError(f"invalid {field}")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ReplayCorruptionError(f"invalid {field}") from exc
    if len(value) > maximum_bytes or encoded_size > maximum_bytes:
        raise ReplayCorruptionError(f"invalid {field}")
    return value


def _bounded_integer(
    value: object, *, minimum: int, maximum: int, field: str
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ReplayCorruptionError(f"invalid {field}")
    return value


def _validate_context_plan_metadata(value: object) -> dict[str, object]:
    expected = {
        "plan_id",
        "digest",
        "toolset_digest",
        "section_count",
        "history_message_count",
        "included_history_message_count",
        "omitted_history_message_count",
        "history_source_digest",
        "windowing_strategy",
        "estimated_input_tokens",
        "native_context_tokens",
        "operational_context_tokens",
        "input_budget_tokens",
        "compact_at_tokens",
        "compact_target_tokens",
        "output_reserve_tokens",
        "template_reserve_tokens",
        "estimator",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ReplayCorruptionError("invalid run.started context plan")

    plan_id = value.get("plan_id")
    digest = value.get("digest")
    history_digest = value.get("history_source_digest")
    if (
        not isinstance(plan_id, str)
        or _PLAN_ID.fullmatch(plan_id) is None
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or plan_id != f"context-{digest[:24]}"
        or value.get("toolset_digest")
        not in {_TOOLSET_DIGEST, _LEGACY_TOOLSET_DIGEST}
        or not isinstance(history_digest, str)
        or _DIGEST.fullmatch(history_digest) is None
    ):
        raise ReplayCorruptionError("invalid run.started context plan identity")

    section_count = _bounded_integer(
        value.get("section_count"),
        minimum=3,
        maximum=MAX_CONTEXT_SECTIONS,
        field="context section count",
    )
    history_count = _bounded_integer(
        value.get("history_message_count"),
        minimum=0,
        maximum=MAX_HISTORY_MESSAGES,
        field="history message count",
    )
    included_count = _bounded_integer(
        value.get("included_history_message_count"),
        minimum=0,
        maximum=MAX_HISTORY_MESSAGES,
        field="included history message count",
    )
    omitted_count = _bounded_integer(
        value.get("omitted_history_message_count"),
        minimum=0,
        maximum=MAX_HISTORY_MESSAGES,
        field="omitted history message count",
    )
    strategy = value.get("windowing_strategy")
    if (
        history_count % 2
        or included_count % 2
        or included_count > history_count
        or omitted_count != history_count - included_count
        or strategy not in {"full", "completed-turn-tail-v1"}
        or (strategy == "full" and included_count != history_count)
        or (
            strategy == "completed-turn-tail-v1"
            and included_count >= history_count
        )
            or not (
                3 + included_count + int(strategy == "completed-turn-tail-v1")
                <= section_count
                <= 6 + included_count + int(strategy == "completed-turn-tail-v1")
            )
    ):
        raise ReplayCorruptionError("invalid run.started history metadata")

    native_tokens = _bounded_integer(
        value.get("native_context_tokens"),
        minimum=MIN_OPERATIONAL_CONTEXT_TOKENS,
        maximum=MAX_NATIVE_CONTEXT_TOKENS,
        field="native context tokens",
    )
    operational_tokens = _bounded_integer(
        value.get("operational_context_tokens"),
        minimum=MIN_OPERATIONAL_CONTEXT_TOKENS,
        maximum=MAX_OPERATIONAL_CONTEXT_TOKENS,
        field="operational context tokens",
    )
    output_reserve = _bounded_integer(
        value.get("output_reserve_tokens"),
        minimum=1,
        maximum=MAX_OPERATIONAL_CONTEXT_TOKENS - 1,
        field="output reserve tokens",
    )
    input_budget = _bounded_integer(
        value.get("input_budget_tokens"),
        minimum=1_024,
        maximum=MAX_OPERATIONAL_CONTEXT_TOKENS - 1,
        field="input budget tokens",
    )
    estimated_tokens = _bounded_integer(
        value.get("estimated_input_tokens"),
        minimum=1,
        maximum=MAX_OPERATIONAL_CONTEXT_TOKENS,
        field="estimated input tokens",
    )
    if (
        native_tokens < operational_tokens
        or output_reserve >= operational_tokens
        or input_budget != operational_tokens - output_reserve
        or estimated_tokens > input_budget
        or value.get("compact_at_tokens") != max(1, input_budget * 80 // 100)
        or value.get("compact_target_tokens")
        != max(1, input_budget * 60 // 100)
        or value.get("template_reserve_tokens")
        != PROVIDER_TEMPLATE_TOKEN_RESERVE
        or value.get("estimator") != TOKEN_ESTIMATOR_ID
    ):
        raise ReplayCorruptionError("invalid run.started context budget")
    return value


def _validate_started_payload(payload: object) -> dict[str, object]:
    legacy_expected = {
        "prototype",
        "model",
        "visible_tools",
        "sandbox",
        "context_plan",
    }
    current_expected = {*legacy_expected, "protocol_features"}
    if (
        not isinstance(payload, dict)
        or frozenset(payload)
        not in {frozenset(legacy_expected), frozenset(current_expected)}
    ):
        raise ReplayCorruptionError("invalid run.started payload")
    model = payload.get("model")
    visible_tools = payload.get("visible_tools")
    if (
        payload.get("prototype") is not True
        or not isinstance(model, str)
        or _SAFE_NAME.fullmatch(model) is None
        or not isinstance(visible_tools, list)
        or tuple(visible_tools) != _VISIBLE_TOOL_IDS
        or payload.get("sandbox") != SANDBOX_POLICY
    ):
        raise ReplayCorruptionError("invalid run.started payload")
    if "protocol_features" in payload and payload.get("protocol_features") not in (
        [MODEL_BOUNDARY_FEATURE],
        [MODEL_BOUNDARY_FEATURE, MULTI_TOOL_LOOP_FEATURE],
    ):
        raise ReplayCorruptionError("invalid run.started protocol features")
    _validate_context_plan_metadata(payload.get("context_plan"))
    return payload


def _has_model_boundary_feature(started: dict[str, object]) -> bool:
    features = started.get("protocol_features")
    return isinstance(features, list) and MODEL_BOUNDARY_FEATURE in features


def _has_multi_tool_loop_feature(started: dict[str, object]) -> bool:
    features = started.get("protocol_features")
    return isinstance(features, list) and MULTI_TOOL_LOOP_FEATURE in features


def _validate_model_request_payload(
    payload: object, started: dict[str, object]
) -> dict[str, object]:
    expected = {
        "request_id",
        "iteration",
        "context_plan_id",
        "context_plan_digest",
        "request_digest",
        "request_bytes",
        "estimated_input_tokens",
        "message_count",
        "tool_count",
        "tool_result_call_ids",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ReplayCorruptionError("invalid model.request.started payload")
    context = started.get("context_plan")
    visible_tools = started.get("visible_tools")
    if not isinstance(context, dict) or not isinstance(visible_tools, list):
        raise ReplayCorruptionError("invalid model request context")
    iteration = _bounded_integer(
        payload.get("iteration"), minimum=1, maximum=8, field="model iteration"
    )
    request_id = _worker_id(payload.get("request_id"), "model request_id")
    result_ids = payload.get("tool_result_call_ids")
    if (
        request_id != f"model-{iteration}"
        or payload.get("context_plan_id") != context.get("plan_id")
        or payload.get("context_plan_digest") != context.get("digest")
        or not isinstance(payload.get("request_digest"), str)
        or _DIGEST.fullmatch(str(payload.get("request_digest"))) is None
        or not isinstance(result_ids, list)
        or len(result_ids) > 3
    ):
        raise ReplayCorruptionError("invalid model request identity")
    validated_result_ids = [
        _worker_id(item, "model Tool result call_id") for item in result_ids
    ]
    if len(set(validated_result_ids)) != len(validated_result_ids):
        raise ReplayCorruptionError("model request repeats a Tool result")
    _bounded_integer(
        payload.get("request_bytes"),
        minimum=1,
        maximum=MAX_PROVIDER_REQUEST_BYTES,
        field="model request bytes",
    )
    estimated = _bounded_integer(
        payload.get("estimated_input_tokens"),
        minimum=1,
        maximum=MAX_OPERATIONAL_CONTEXT_TOKENS,
        field="model estimated input tokens",
    )
    _bounded_integer(
        payload.get("message_count"),
        minimum=1,
        maximum=MAX_ARRAY_ITEMS,
        field="model message count",
    )
    tool_count = _bounded_integer(
        payload.get("tool_count"),
        minimum=0,
        maximum=len(visible_tools),
        field="model Tool count",
    )
    expected_tool_count = (
        len(visible_tools)
        if _has_multi_tool_loop_feature(started)
        and len(validated_result_ids) < 2
        else (len(visible_tools) if iteration == 1 else 0)
    )
    if (
        estimated > context.get("input_budget_tokens", 0)
        or (iteration == 1 and validated_result_ids)
        or (iteration > 1 and not validated_result_ids)
        or tool_count != expected_tool_count
    ):
        raise ReplayCorruptionError("invalid model request capability state")
    return payload


def _validate_model_response_payload(
    payload: object, started: dict[str, object]
) -> dict[str, object]:
    expected = {
        "request_id",
        "iteration",
        "outcome",
        "input_tokens",
        "output_tokens",
        "usage_complete",
        "error_code",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ReplayCorruptionError("invalid model.response.finished payload")
    iteration = _bounded_integer(
        payload.get("iteration"), minimum=1, maximum=3, field="model iteration"
    )
    request_id = _worker_id(payload.get("request_id"), "model request_id")
    input_tokens = _bounded_integer(
        payload.get("input_tokens"),
        minimum=0,
        maximum=MAX_USAGE_TOKENS,
        field="model input token usage",
    )
    output_tokens = _bounded_integer(
        payload.get("output_tokens"),
        minimum=0,
        maximum=MAX_USAGE_TOKENS,
        field="model output token usage",
    )
    outcome = payload.get("outcome")
    complete = payload.get("usage_complete")
    error_code = payload.get("error_code")
    context = started.get("context_plan")
    if not isinstance(context, dict):
        raise ReplayCorruptionError("invalid model response context")
    if (
        request_id != f"model-{iteration}"
        or outcome not in {"tool_use", "end_turn", "error", "cancelled"}
        or not isinstance(complete, bool)
        or input_tokens > context.get("input_budget_tokens", 0)
        or output_tokens > context.get("output_reserve_tokens", 0)
        or input_tokens + output_tokens
        > context.get("operational_context_tokens", 0)
    ):
        raise ReplayCorruptionError("invalid model response usage")
    if outcome in {"tool_use", "end_turn"}:
        if complete is not True or error_code is not None:
            raise ReplayCorruptionError("invalid successful model response")
    elif (
        complete is not False
        or input_tokens != 0
        or output_tokens != 0
        or not isinstance(error_code, str)
        or _WORKER_ID.fullmatch(error_code) is None
    ):
        raise ReplayCorruptionError("invalid unsuccessful model response")
    return payload


def _validate_model_usage_rollup(
    model_calls: Sequence[dict[str, object]], terminal_payload: dict[str, object]
) -> None:
    if not model_calls:
        return
    usage = terminal_payload.get("usage")
    if not isinstance(usage, dict):
        raise ReplayCorruptionError("terminal has no model usage rollup")
    completed = [item for item in model_calls if item.get("usage_complete") is True]
    expected_input = sum(int(item["input_tokens"]) for item in completed)
    expected_output = sum(int(item["output_tokens"]) for item in completed)
    expected_last_input = int(completed[-1]["input_tokens"]) if completed else 0
    expected_complete = model_calls[-1].get("usage_complete") is True
    if usage != {
        "input_tokens": expected_input,
        "output_tokens": expected_output,
        "last_input_tokens": expected_last_input,
        "complete": expected_complete,
    }:
        raise ReplayCorruptionError("terminal model usage rollup is inconsistent")


def _validate_usage(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "input_tokens",
        "output_tokens",
        "last_input_tokens",
        "complete",
    }:
        raise ReplayCorruptionError("invalid terminal usage")
    input_tokens = _bounded_integer(
        value.get("input_tokens"),
        minimum=0,
        maximum=MAX_USAGE_TOKENS,
        field="input token usage",
    )
    _bounded_integer(
        value.get("output_tokens"),
        minimum=0,
        maximum=MAX_USAGE_TOKENS,
        field="output token usage",
    )
    last_input_tokens = _bounded_integer(
        value.get("last_input_tokens"),
        minimum=0,
        maximum=MAX_USAGE_TOKENS,
        field="last input token usage",
    )
    if last_input_tokens > input_tokens or not isinstance(
        value.get("complete"), bool
    ):
        raise ReplayCorruptionError("invalid terminal usage")
    return value


def _validate_terminal_payload(
    kind: str, payload: object
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ReplayCorruptionError(f"invalid {kind} payload")
    if kind == "run.completed":
        if set(payload) != {"reason", "model_iterations", "usage"}:
            raise ReplayCorruptionError("invalid run.completed payload")
        iterations = payload.get("model_iterations")
        usage = _validate_usage(payload.get("usage"))
        if (
            payload.get("reason") != "end_turn"
            or not isinstance(iterations, int)
            or isinstance(iterations, bool)
            or not 1 <= iterations <= 3
            or usage.get("complete") is not True
        ):
            raise ReplayCorruptionError("invalid run.completed payload")
    elif kind == "run.failed":
        if set(payload) != {"code", "message", "retryable", "usage"}:
            raise ReplayCorruptionError("invalid run.failed payload")
        _worker_id(payload.get("code"), "failure code")
        _bounded_text(
            payload.get("message"),
            maximum_bytes=512,
            field="failure message",
        )
        if not isinstance(payload.get("retryable"), bool):
            raise ReplayCorruptionError("invalid run.failed payload")
        _validate_usage(payload.get("usage"))
    elif kind == "run.cancelled":
        if set(payload) != {"reason", "usage"}:
            raise ReplayCorruptionError("invalid run.cancelled payload")
        if payload.get("reason") != "cancelled":
            raise ReplayCorruptionError("invalid run.cancelled payload")
        _validate_usage(payload.get("usage"))
    else:
        raise ReplayCorruptionError("invalid terminal kind")
    return payload


def _tool_spec(tool_id: object) -> ToolSpec:
    if not isinstance(tool_id, str):
        raise ReplayCorruptionError("invalid tool_id")
    spec = _TOOL_SPECS.get(tool_id)
    if spec is None:
        raise ReplayCorruptionError("unknown Tool")
    return spec


def _validate_tool_arguments(spec: ToolSpec, value: object) -> dict[str, str]:
    try:
        return spec.validate_arguments(value)
    except (UnicodeError, ValueError) as exc:
        raise ReplayCorruptionError("invalid Tool arguments") from exc


def _validate_tool_result(spec: ToolSpec, value: object) -> str:
    try:
        return spec.validate_result(value)
    except (UnicodeError, ValueError) as exc:
        raise ReplayCorruptionError("invalid Tool result") from exc


def _validate_tool_outcome(
    *, spec: ToolSpec, arguments: dict[str, str], outcome: object, result: object
) -> str:
    if outcome not in {"succeeded", "failed", "cancelled"}:
        raise ReplayCorruptionError("invalid Tool outcome")
    validated_result = _validate_tool_result(spec, result)
    if spec.tool_id == "builtin/echo" and (
        (outcome == "succeeded" and validated_result != arguments.get("text"))
        or (outcome == "cancelled" and validated_result != "cancelled")
    ):
        raise ReplayCorruptionError("invalid builtin/echo result")
    return validated_result


def _snapshot_sequence(value: object, *, field: str, through_seq: int) -> int:
    return _bounded_integer(
        value, minimum=2, maximum=through_seq, field=field
    )


def _validate_snapshot_document(
    document: dict[str, object],
    *,
    through_seq: int,
    complete: bool,
    version: str = PROJECTION_VERSION,
) -> None:
    expected_document_fields = {"started", "blocks", "tools", "terminal"}
    if version == PROJECTION_VERSION:
        expected_document_fields.add("model_calls")
    elif version != LEGACY_PROJECTION_VERSION:
        raise ReplayCorruptionError("projection version is unsupported")
    if set(document) != expected_document_fields:
        raise ReplayCorruptionError("projection document fields are invalid")
    started = _validate_started_payload(document.get("started"))
    blocks = document.get("blocks")
    tools = document.get("tools")
    model_calls = document.get("model_calls", [])
    terminal = document.get("terminal")
    if (
        not isinstance(blocks, list)
        or not isinstance(tools, list)
        or not isinstance(model_calls, list)
    ):
        raise ReplayCorruptionError("projection document collections are invalid")
    maximum_tools = 2 if _has_multi_tool_loop_feature(started) else 1
    if len(tools) > maximum_tools:
        raise ReplayCorruptionError("projection has too many Tool calls")
    if len(model_calls) > 3:
        raise ReplayCorruptionError("projection has too many model calls")
    boundary_feature = _has_model_boundary_feature(started)
    if model_calls and not boundary_feature:
        raise ReplayCorruptionError("projection has unadvertised model boundaries")

    seen_blocks: set[str] = set()
    known_sequences: set[int] = {1}
    open_model_calls = 0
    last_model_sequence = 1
    for expected_iteration, item in enumerate(model_calls, start=1):
        if not isinstance(item, dict) or set(item) != {
            "request_id",
            "iteration",
            "context_plan_id",
            "context_plan_digest",
            "request_digest",
            "request_bytes",
            "estimated_input_tokens",
            "message_count",
            "tool_count",
            "tool_result_call_ids",
            "state",
            "request_seq",
            "response_seq",
            "outcome",
            "input_tokens",
            "output_tokens",
            "usage_complete",
            "error_code",
        }:
            raise ReplayCorruptionError("projection model call is invalid")
        request_payload = {
            key: item[key]
            for key in (
                "request_id",
                "iteration",
                "context_plan_id",
                "context_plan_digest",
                "request_digest",
                "request_bytes",
                "estimated_input_tokens",
                "message_count",
                "tool_count",
                "tool_result_call_ids",
            )
        }
        _validate_model_request_payload(request_payload, started)
        request_seq = _snapshot_sequence(
            item.get("request_seq"),
            field="model request sequence",
            through_seq=through_seq,
        )
        if (
            item.get("iteration") != expected_iteration
            or request_seq <= last_model_sequence
            or request_seq in known_sequences
        ):
            raise ReplayCorruptionError("projection model request is out of sequence")
        known_sequences.add(request_seq)
        if item.get("state") == "started":
            if (
                expected_iteration != len(model_calls)
                or any(
                    item.get(field) is not None
                    for field in (
                        "response_seq",
                        "outcome",
                        "input_tokens",
                        "output_tokens",
                        "usage_complete",
                        "error_code",
                    )
                )
            ):
                raise ReplayCorruptionError("projection open model call is invalid")
            open_model_calls += 1
            last_model_sequence = request_seq
        elif item.get("state") == "finished":
            response_payload = {
                "request_id": item["request_id"],
                "iteration": item["iteration"],
                "outcome": item["outcome"],
                "input_tokens": item["input_tokens"],
                "output_tokens": item["output_tokens"],
                "usage_complete": item["usage_complete"],
                "error_code": item["error_code"],
            }
            _validate_model_response_payload(response_payload, started)
            response_seq = _snapshot_sequence(
                item.get("response_seq"),
                field="model response sequence",
                through_seq=through_seq,
            )
            if response_seq <= request_seq or response_seq in known_sequences:
                raise ReplayCorruptionError("projection model response is out of sequence")
            known_sequences.add(response_seq)
            last_model_sequence = response_seq
        else:
            raise ReplayCorruptionError("projection model call state is invalid")
    if open_model_calls > 1:
        raise ReplayCorruptionError("projection has multiple open model calls")

    last_block_end = 1
    open_blocks = 0
    for item in blocks:
        if not isinstance(item, dict):
            raise ReplayCorruptionError("projection block is invalid")
        state = item.get("state")
        expected = {"block_id", "state", "content", "start_seq", "end_seq"}
        if state == "discarded":
            expected.add("reason")
        if set(item) != expected or state not in {
            "open",
            "finished",
            "discarded",
        }:
            raise ReplayCorruptionError("projection block is invalid")
        block_id = _worker_id(item.get("block_id"), "block_id")
        start_seq = _snapshot_sequence(
            item.get("start_seq"), field="block start sequence", through_seq=through_seq
        )
        if (
            block_id in seen_blocks
            or open_blocks
            or start_seq <= last_block_end
        ):
            raise ReplayCorruptionError("projection block identity is invalid")
        seen_blocks.add(block_id)
        if start_seq in known_sequences:
            raise ReplayCorruptionError("projection sequence is reused")
        known_sequences.add(start_seq)
        if state == "open":
            if item.get("content") is not None or item.get("end_seq") is not None:
                raise ReplayCorruptionError("projection open block is invalid")
            open_blocks += 1
        else:
            end_seq = _snapshot_sequence(
                item.get("end_seq"), field="block end sequence", through_seq=through_seq
            )
            if end_seq <= start_seq or end_seq in known_sequences:
                raise ReplayCorruptionError("projection block sequence is invalid")
            known_sequences.add(end_seq)
            last_block_end = end_seq
            if state == "finished":
                _bounded_text(
                    item.get("content"),
                    maximum_bytes=MAX_WORKER_TEXT_BYTES,
                    field="assistant content",
                )
            elif (
                item.get("content") is not None
                or item.get("reason")
                not in {"cancelled", "runtime_failure", "worker_failure"}
            ):
                raise ReplayCorruptionError("projection discarded block is invalid")
    if open_blocks > 1:
        raise ReplayCorruptionError("projection has multiple open blocks")

    seen_calls: set[str] = set()
    last_tool_finish = 1
    pending_tools = 0
    for item in tools:
        if not isinstance(item, dict) or set(item) != {
            "call_id",
            "tool_id",
            "state",
            "arguments",
            "outcome",
            "result",
            "request_seq",
            "finish_seq",
        }:
            raise ReplayCorruptionError("projection Tool is invalid")
        call_id = _worker_id(item.get("call_id"), "call_id")
        spec = _tool_spec(item.get("tool_id"))
        arguments = _validate_tool_arguments(spec, item.get("arguments"))
        request_seq = _snapshot_sequence(
            item.get("request_seq"),
            field="Tool request sequence",
            through_seq=through_seq,
        )
        if (
            call_id in seen_calls
            or pending_tools
            or request_seq <= last_tool_finish
        ):
            raise ReplayCorruptionError("projection Tool identity is invalid")
        seen_calls.add(call_id)
        if request_seq in known_sequences:
            raise ReplayCorruptionError("projection sequence is reused")
        known_sequences.add(request_seq)
        state = item.get("state")
        if state in {"requested", "started"}:
            if (
                item.get("outcome") is not None
                or item.get("result") is not None
                or item.get("finish_seq") is not None
            ):
                raise ReplayCorruptionError("projection pending Tool is invalid")
            pending_tools += 1
        elif state == "finished":
            finish_seq = _snapshot_sequence(
                item.get("finish_seq"),
                field="Tool finish sequence",
                through_seq=through_seq,
            )
            if finish_seq <= request_seq or finish_seq in known_sequences:
                raise ReplayCorruptionError("projection Tool sequence is invalid")
            known_sequences.add(finish_seq)
            last_tool_finish = finish_seq
            _validate_tool_outcome(
                spec=spec,
                arguments=arguments,
                outcome=item.get("outcome"),
                result=item.get("result"),
            )
        else:
            raise ReplayCorruptionError("projection Tool state is invalid")
    if pending_tools > 1:
        raise ReplayCorruptionError("projection has multiple pending Tools")

    for call in model_calls:
        assert isinstance(call, dict)
        request_seq = call.get("request_seq")
        referenced_results = call.get("tool_result_call_ids")
        if not isinstance(request_seq, int) or not isinstance(referenced_results, list):
            raise ReplayCorruptionError("projection model Tool results are invalid")
        available_results = [
            item.get("call_id")
            for item in tools
            if isinstance(item, dict)
            and item.get("state") == "finished"
            and isinstance(item.get("finish_seq"), int)
            and item["finish_seq"] < request_seq
        ]
        if referenced_results != available_results:
            raise ReplayCorruptionError(
                "projection model request has inconsistent Tool results"
            )

    durable_operation_sequences: list[int] = []
    for item in blocks:
        assert isinstance(item, dict)
        durable_operation_sequences.append(int(item["start_seq"]))
        if isinstance(item.get("end_seq"), int):
            durable_operation_sequences.append(int(item["end_seq"]))
    for item in tools:
        assert isinstance(item, dict)
        durable_operation_sequences.append(int(item["request_seq"]))
        if isinstance(item.get("finish_seq"), int):
            durable_operation_sequences.append(int(item["finish_seq"]))
    for index, call in enumerate(model_calls):
        assert isinstance(call, dict)
        request_seq = int(call["request_seq"])
        response_seq = call.get("response_seq")
        boundary_end = int(response_seq) if isinstance(response_seq, int) else through_seq + 1
        if any(
            request_seq < sequence < boundary_end
            for sequence in durable_operation_sequences
        ):
            raise ReplayCorruptionError(
                "projection interleaves a Worker operation with a model stream"
            )
        if index:
            previous = model_calls[index - 1]
            assert isinstance(previous, dict)
            if (
                previous.get("outcome") != "tool_use"
                or not isinstance(previous.get("response_seq"), int)
                or request_seq <= int(previous["response_seq"])
            ):
                raise ReplayCorruptionError(
                    "projection model calls have an invalid transition"
                )
    for tool in tools:
        assert isinstance(tool, dict)
        request_seq = int(tool["request_seq"])
        preceding = [
            item
            for item in model_calls
            if isinstance(item, dict)
            and isinstance(item.get("response_seq"), int)
            and int(item["response_seq"]) < request_seq
        ]
        if model_calls and (
            not preceding or preceding[-1].get("outcome") != "tool_use"
        ):
            raise ReplayCorruptionError(
                "projection Tool request has no matching model response"
            )

    if complete:
        if (
            not isinstance(terminal, dict)
            or set(terminal) != {"kind", "payload"}
            or terminal.get("kind") not in TERMINAL_KINDS
            or open_blocks
            or pending_tools
            or open_model_calls
            or through_seq in known_sequences
        ):
            raise ReplayCorruptionError("complete projection is invalid")
        terminal_payload = _validate_terminal_payload(
            str(terminal["kind"]), terminal.get("payload")
        )
        if boundary_feature:
            _validate_model_usage_rollup(model_calls, terminal_payload)
        if terminal["kind"] == "run.completed":
            expected_iterations = (
                len(model_calls)
                if boundary_feature
                else len(tools) + 1
            )
            if (
                (boundary_feature and not model_calls)
                or
                terminal_payload.get("model_iterations") != expected_iterations
                or (
                    boundary_feature
                    and (
                        model_calls[-1].get("outcome") != "end_turn"  # type: ignore[union-attr]
                        or sum(
                            item.get("outcome") == "tool_use"  # type: ignore[union-attr]
                            for item in model_calls[:-1]
                        )
                        != len(tools)
                    )
                )
            ):
                raise ReplayCorruptionError(
                    "completed projection has an invalid model iteration count"
                )
    elif terminal is not None:
        raise ReplayCorruptionError("incomplete projection has a terminal")


def project_durable_run(
    events: Sequence[EventEnvelope],
    *,
    reserved_through: int | None = None,
) -> tuple[ProjectionSnapshot, tuple[ReplayGap, ...]]:
    """Validate and deterministically project one complete retained prefix."""

    if not events or len(events) > MAX_REPLAY_EVENTS:
        raise ReplayCorruptionError("durable Run event count is invalid")
    if (
        reserved_through is not None
        and (
            not isinstance(reserved_through, int)
            or isinstance(reserved_through, bool)
            or not 1 <= reserved_through <= MAX_REPLAY_SEQUENCE
        )
    ):
        raise ReplayCorruptionError("reserved cursor boundary is invalid")
    identity = RunIdentity.from_event(events[0])
    if events[0].seq != 1 or events[0].kind != "run.started":
        raise ReplayCorruptionError("durable Run has no canonical start")

    last_seq = 0
    event_ids: set[str] = set()
    open_block: str | None = None
    pending_tool: tuple[str, str, bool] | None = None
    seen_blocks: set[str] = set()
    seen_tools: set[str] = set()
    blocks: list[dict[str, object]] = []
    tools: list[dict[str, object]] = []
    model_calls: list[dict[str, object]] = []
    open_model_call: int | None = None
    model_boundaries_seen = False
    gaps: list[ReplayGap] = []
    terminal: dict[str, object] | None = None
    started_payload: dict[str, object] | None = None

    for index, event in enumerate(events):
        if RunIdentity.from_event(event) != identity:
            raise ReplayCorruptionError("durable Run changes identity")
        if event.durability != "durable" or event.kind not in _DURABLE_KINDS:
            raise ReplayCorruptionError("durable Run contains an invalid event")
        if event.event_id in event_ids:
            raise ReplayCorruptionError("durable Run repeats an event ID")
        event_ids.add(event.event_id)
        if event.seq <= last_seq:
            raise ReplayCorruptionError("durable Run sequence is not increasing")
        if event.seq > last_seq + 1:
            reserved_recovery_gap = (
                reserved_through is not None
                and last_seq < reserved_through
                and event.seq == reserved_through + 1
            )
            if open_block is None and not reserved_recovery_gap:
                raise ReplayCorruptionError("durable Run has an unexplained gap")
            gaps.append(
                ReplayGap(
                    last_seq + 1,
                    event.seq - 1,
                    "ephemeral_not_durable",
                )
            )
        if terminal is not None:
            raise ReplayCorruptionError("durable Run has an event after terminal")

        if event.kind == "run.started":
            if index != 0:
                raise ReplayCorruptionError("durable Run repeats its start")
            started_payload = _validate_started_payload(event.payload)
        elif event.kind == "model.request.started":
            if started_payload is None:
                raise ReplayCorruptionError("model request precedes Run start")
            if not _has_model_boundary_feature(started_payload):
                raise ReplayCorruptionError("model request feature is not advertised")
            payload = _validate_model_request_payload(event.payload, started_payload)
            iteration = payload.get("iteration")
            expected_results = [
                item.get("call_id")
                for item in tools
                if item.get("state") == "finished"
            ]
            previous_outcome = (
                model_calls[-1].get("outcome") if model_calls else None
            )
            if (
                open_model_call is not None
                or open_block is not None
                or pending_tool is not None
                or iteration != len(model_calls) + 1
                or payload.get("tool_result_call_ids") != expected_results
                or (not model_calls and (blocks or tools))
                or (model_calls and previous_outcome != "tool_use")
            ):
                raise ReplayCorruptionError("model request is out of sequence")
            model_boundaries_seen = True
            model_calls.append(
                {
                    **payload,
                    "state": "started",
                    "request_seq": event.seq,
                    "response_seq": None,
                    "outcome": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "usage_complete": None,
                    "error_code": None,
                }
            )
            open_model_call = len(model_calls) - 1
        elif event.kind == "model.response.finished":
            if started_payload is None:
                raise ReplayCorruptionError("model response precedes Run start")
            payload = _validate_model_response_payload(event.payload, started_payload)
            if open_model_call is None:
                raise ReplayCorruptionError("model response has no request")
            request = model_calls[open_model_call]
            if (
                payload.get("request_id") != request.get("request_id")
                or payload.get("iteration") != request.get("iteration")
                or (
                    payload.get("outcome") == "tool_use"
                    and request.get("tool_count") == 0
                )
            ):
                raise ReplayCorruptionError("model response does not match its request")
            model_calls[open_model_call] = {
                **request,
                "state": "finished",
                "response_seq": event.seq,
                "outcome": payload["outcome"],
                "input_tokens": payload["input_tokens"],
                "output_tokens": payload["output_tokens"],
                "usage_complete": payload["usage_complete"],
                "error_code": payload["error_code"],
            }
            open_model_call = None
        elif event.kind == "assistant.block.started":
            payload = _exact_payload(event, {"block_id", "block_type"})
            block_id = _worker_id(payload.get("block_id"), "block_id")
            if (
                payload.get("block_type") != "content"
                or open_block is not None
                or block_id in seen_blocks
                or open_model_call is not None
            ):
                raise ReplayCorruptionError("invalid assistant block start")
            seen_blocks.add(block_id)
            open_block = block_id
            blocks.append(
                {
                    "block_id": block_id,
                    "state": "open",
                    "content": None,
                    "start_seq": event.seq,
                    "end_seq": None,
                }
            )
        elif event.kind == "assistant.block.finished":
            payload = _exact_payload(event, {"block_id", "content"})
            block_id = _worker_id(payload.get("block_id"), "block_id")
            content = payload.get("content")
            if block_id != open_block:
                raise ReplayCorruptionError("invalid assistant block finish")
            _bounded_text(
                content,
                maximum_bytes=MAX_WORKER_TEXT_BYTES,
                field="assistant content",
            )
            blocks[-1] = {
                **blocks[-1],
                "state": "finished",
                "content": content,
                "end_seq": event.seq,
            }
            open_block = None
        elif event.kind == "assistant.block.discarded":
            payload = _exact_payload(event, {"block_id", "reason"})
            block_id = _worker_id(payload.get("block_id"), "block_id")
            reason = payload.get("reason")
            if block_id != open_block or reason not in {
                "cancelled",
                "runtime_failure",
                "worker_failure",
            }:
                raise ReplayCorruptionError("invalid assistant block discard")
            blocks[-1] = {
                **blocks[-1],
                "state": "discarded",
                "reason": reason,
                "end_seq": event.seq,
            }
            open_block = None
        elif event.kind == "tool.call.requested":
            payload = _exact_payload(
                event, {"call_id", "tool_id", "arguments"}
            )
            call_id = _worker_id(payload.get("call_id"), "call_id")
            tool_id = payload.get("tool_id")
            spec = _tool_spec(tool_id)
            arguments = _validate_tool_arguments(
                spec, payload.get("arguments")
            )
            if (
                pending_tool is not None
                or call_id in seen_tools
                or (
                    bool(seen_tools)
                    and (
                        started_payload is None
                        or not _has_multi_tool_loop_feature(started_payload)
                    )
                )
                or open_model_call is not None
                or (
                    model_boundaries_seen
                    and (
                        not model_calls
                        or model_calls[-1].get("outcome") != "tool_use"
                    )
                )
            ):
                raise ReplayCorruptionError("invalid Tool request")
            assert isinstance(tool_id, str)
            seen_tools.add(call_id)
            pending_tool = (call_id, tool_id, False)
            tools.append(
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "state": "requested",
                    "arguments": arguments,
                    "outcome": None,
                    "result": None,
                    "request_seq": event.seq,
                    "finish_seq": None,
                }
            )
        elif event.kind == "tool.call.started":
            payload = _exact_payload(event, {"call_id", "tool_id"})
            call_id = _worker_id(payload.get("call_id"), "call_id")
            if (
                pending_tool is None
                or pending_tool[0] != call_id
                or pending_tool[1] != payload.get("tool_id")
                or pending_tool[2]
            ):
                raise ReplayCorruptionError("invalid Tool start")
            pending_tool = (pending_tool[0], pending_tool[1], True)
            tools[-1] = {**tools[-1], "state": "started"}
        elif event.kind == "tool.call.finished":
            allowed = {"call_id", "outcome", "result"}
            if "tool_id" in event.payload:
                allowed.add("tool_id")
            payload = _exact_payload(event, allowed)
            call_id = _worker_id(payload.get("call_id"), "call_id")
            if (
                pending_tool is None
                or not pending_tool[2]
                or pending_tool[0] != call_id
                or payload.get("outcome")
                not in {"succeeded", "failed", "cancelled"}
                or (
                    "tool_id" in payload
                    and payload.get("tool_id") != pending_tool[1]
                )
            ):
                raise ReplayCorruptionError("invalid Tool finish")
            spec = _tool_spec(pending_tool[1])
            _validate_tool_outcome(
                spec=spec,
                arguments=tools[-1]["arguments"],  # type: ignore[arg-type]
                outcome=payload.get("outcome"),
                result=payload.get("result"),
            )
            tools[-1] = {
                **tools[-1],
                "state": "finished",
                "outcome": payload["outcome"],
                "result": payload["result"],
                "finish_seq": event.seq,
            }
            pending_tool = None
        elif event.kind in TERMINAL_KINDS:
            if (
                open_block is not None
                or pending_tool is not None
                or open_model_call is not None
            ):
                raise ReplayCorruptionError("terminal leaves an operation open")
            terminal_payload = _validate_terminal_payload(
                event.kind, event.payload
            )
            if _has_model_boundary_feature(started_payload):
                _validate_model_usage_rollup(model_calls, terminal_payload)
            if event.kind == "run.completed":
                expected_iterations = (
                    len(model_calls)
                    if _has_model_boundary_feature(started_payload)
                    else len(tools) + 1
                )
                if (
                    (
                        _has_model_boundary_feature(started_payload)
                        and not model_boundaries_seen
                    )
                    or
                    terminal_payload.get("model_iterations") != expected_iterations
                    or (
                        model_boundaries_seen
                        and (
                            not model_calls
                            or model_calls[-1].get("outcome") != "end_turn"
                            or sum(
                                item.get("outcome") == "tool_use"
                                for item in model_calls[:-1]
                            )
                            != len(tools)
                        )
                    )
                ):
                    raise ReplayCorruptionError(
                        "completed Run has an invalid model iteration count"
                    )
            terminal = {
                "kind": event.kind,
                "payload": terminal_payload,
            }
        else:
            raise ReplayCorruptionError("unsupported durable event")
        last_seq = event.seq

    document = {
        "started": started_payload,
        "model_calls": model_calls,
        "blocks": blocks,
        "tools": tools,
        "terminal": terminal,
    }
    document_json = _canonical_json(document)
    digest, encoded_unsigned = _snapshot_digest(
        identity, last_seq, terminal is not None, document
    )
    if len(encoded_unsigned) > MAX_SNAPSHOT_BYTES:
        raise ReplayCorruptionError("Run projection exceeds its snapshot limit")
    _validate_snapshot_document(
        document,
        through_seq=last_seq,
        complete=terminal is not None,
        version=PROJECTION_VERSION,
    )
    snapshot = ProjectionSnapshot(
        identity=identity,
        through_seq=last_seq,
        complete=terminal is not None,
        document_json=document_json,
        digest=digest,
    )
    return snapshot, tuple(gaps)


__all__ = [
    "decode_durable_event",
    "decode_projection_snapshot",
    "DurableReplay",
    "encode_projection_snapshot",
    "LEGACY_PROJECTION_VERSION",
    "MAX_DURABLE_EVENT_BYTES",
    "MAX_REPLAY_BYTES",
    "MAX_REPLAY_EVENTS",
    "MAX_REPLAY_PAGE",
    "MAX_REPLAY_SEQUENCE",
    "MODEL_BOUNDARY_FEATURE",
    "ProjectionSnapshot",
    "project_durable_run",
    "PROJECTION_VERSION",
    "ReplayCorruptionError",
    "ReplayGap",
    "RunIdentity",
]

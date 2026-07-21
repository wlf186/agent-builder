"""Strict durable decoding and deterministic UI projection."""

from __future__ import annotations

import hashlib
import json

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.replay import (
    PROJECTION_VERSION,
    ReplayCorruptionError,
    decode_durable_event,
    decode_projection_snapshot,
    project_durable_run,
)
from agent_builder_v2.tools import (
    PROTOTYPE_ECHO_SPEC_V1,
    PROTOTYPE_ECHO_SPEC_V2,
    prototype_tool_specs,
    toolset_digest,
)


CONVERSATION_ID = "1" * 32
TURN_ID = "2" * 32
RUN_ID = "3" * 32
PLAN_DIGEST = "a" * 64


def _started_payload() -> dict[str, object]:
    return {
        "prototype": True,
        "model": "qwen3.5:2b",
        "visible_tools": ["builtin/echo"],
        "sandbox": "harness-v2-worker-v1",
        "context_plan": {
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
        },
    }


def _current_started_payload() -> dict[str, object]:
    return {
        **_started_payload(),
        "protocol_features": ["model-call-boundaries-v1"],
    }


def _multi_tool_started_payload() -> dict[str, object]:
    return {
        **_started_payload(),
        "protocol_features": [
            "model-call-boundaries-v1",
            "sequential-multi-tool-v1",
        ],
    }


def _model_request(
    iteration: int,
    *,
    result_call_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "request_id": f"model-{iteration}",
        "iteration": iteration,
        "context_plan_id": f"context-{PLAN_DIGEST[:24]}",
        "context_plan_digest": PLAN_DIGEST,
        "request_digest": f"{iteration:x}" * 64,
        "request_bytes": 512 + iteration,
        "estimated_input_tokens": 1_000 + iteration,
        "message_count": 2 + (iteration - 1) * 2,
        "tool_count": 1 if iteration == 1 else 0,
        "tool_result_call_ids": result_call_ids or [],
    }


def _model_response(
    iteration: int,
    outcome: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_code: str | None = None,
) -> dict[str, object]:
    successful = outcome in {"tool_use", "end_turn"}
    return {
        "request_id": f"model-{iteration}",
        "iteration": iteration,
        "outcome": outcome,
        "input_tokens": input_tokens if successful else 0,
        "output_tokens": output_tokens if successful else 0,
        "usage_complete": successful,
        "error_code": None if successful else error_code or "model_unavailable",
    }


def _usage(*, complete: bool = True) -> dict[str, object]:
    return {
        "input_tokens": 100,
        "output_tokens": 10,
        "last_input_tokens": 100,
        "complete": complete,
    }


def _completed(*, iterations: int = 1) -> dict[str, object]:
    return {
        "reason": "end_turn",
        "model_iterations": iterations,
        "usage": _usage(),
    }


def _boundary_completed() -> dict[str, object]:
    return {
        "reason": "end_turn",
        "model_iterations": 2,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "last_input_tokens": 60,
            "complete": True,
        },
    }


def _failed() -> dict[str, object]:
    return {
        "code": "control_restarted",
        "message": "Control Plane restarted before terminal publication.",
        "retryable": True,
        "usage": _usage(complete=False),
    }


def _event(
    seq: int, kind: str, payload: dict[str, object]
) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"{seq:032x}",
        agent_id=PROTOTYPE_AGENT_ID,
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
        run_id=RUN_ID,
        seq=seq,
        occurred_at=f"2026-07-18T00:00:00.{seq:03d}Z",
        kind=kind,
        durability="durable",
        payload=payload,
    )


def _raw(event: EventEnvelope) -> bytes:
    return json.dumps(
        event.to_dict(), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _snapshot_raw_with_recomputed_digest(
    value: dict[str, object],
) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "digest"}
    value["digest"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode(event: EventEnvelope) -> EventEnvelope:
    return decode_durable_event(
        _raw(event),
        column_run_id=event.run_id,
        column_seq=event.seq,
        column_kind=event.kind,
        column_occurred_at=event.occurred_at,
    )


def test_strict_decoder_round_trips_a_canonical_event() -> None:
    event = _event(1, "run.started", {"prototype": True})

    assert _decode(event) == event


@pytest.mark.parametrize(
    "legacy_spec", (PROTOTYPE_ECHO_SPEC_V1, PROTOTYPE_ECHO_SPEC_V2)
)
def test_retained_tool_manifest_digest_remains_replayable(legacy_spec: object) -> None:
    payload = _started_payload()
    context = payload["context_plan"]
    assert isinstance(context, dict)
    context["toolset_digest"] = toolset_digest((legacy_spec,))  # type: ignore[arg-type]
    snapshot, gaps = project_durable_run(
        (_event(1, "run.started", payload), _event(2, "run.failed", _failed()))
    )
    assert snapshot.complete is True
    assert gaps == ()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.replace(b'"seq":1', b'"seq":1,"seq":1'),
        lambda raw: raw.replace(b'"prototype":true', b'"prototype":NaN'),
        lambda raw: raw.replace(b'"schema_version":"2.2-prototype"', b'"schema_version":"old"'),
    ],
)
def test_strict_decoder_rejects_duplicate_nan_and_schema_drift(
    mutation: object,
) -> None:
    event = _event(1, "run.started", {"prototype": True})
    mutated = mutation(_raw(event))  # type: ignore[operator]

    with pytest.raises(ReplayCorruptionError):
        decode_durable_event(
            mutated,
            column_run_id=event.run_id,
            column_seq=event.seq,
            column_kind=event.kind,
            column_occurred_at=event.occurred_at,
        )


def test_strict_decoder_binds_json_identity_and_sequence_to_columns() -> None:
    event = _event(1, "run.started", {})

    with pytest.raises(ReplayCorruptionError):
        decode_durable_event(
            _raw(event),
            column_run_id="4" * 32,
            column_seq=event.seq,
            column_kind=event.kind,
            column_occurred_at=event.occurred_at,
        )
    with pytest.raises(ReplayCorruptionError):
        decode_durable_event(
            _raw(event),
            column_run_id=event.run_id,
            column_seq=2,
            column_kind=event.kind,
            column_occurred_at=event.occurred_at,
        )


def test_projector_marks_missing_deltas_and_uses_finished_full_content() -> None:
    events = (
        _event(1, "run.started", _started_payload()),
        _event(
            2,
            "assistant.block.started",
            {"block_id": "answer", "block_type": "content"},
        ),
        # seq=3 was an ephemeral assistant.block.delta and is absent.
        _event(
            4,
            "assistant.block.finished",
            {"block_id": "answer", "content": "完整回答"},
        ),
        _event(5, "run.completed", _completed()),
    )

    first, gaps = project_durable_run(events)
    second, second_gaps = project_durable_run(events)

    assert first.version == PROJECTION_VERSION
    assert first.complete is True
    assert first.through_seq == 5
    assert first.digest == second.digest
    assert first.document_json == second.document_json
    assert gaps == second_gaps
    assert [gap.to_dict() for gap in gaps] == [
        {
            "from_seq": 3,
            "to_seq": 3,
            "reason": "ephemeral_not_durable",
        }
    ]
    assert first.document["blocks"] == [
        {
            "block_id": "answer",
            "content": "完整回答",
            "end_seq": 4,
            "start_seq": 2,
            "state": "finished",
        }
    ]


def test_projector_strictly_pairs_current_model_call_boundaries() -> None:
    events = (
        _event(1, "run.started", _current_started_payload()),
        _event(2, "model.request.started", _model_request(1)),
        _event(
            3,
            "model.response.finished",
            _model_response(1, "tool_use", input_tokens=40, output_tokens=3),
        ),
        _event(
            4,
            "tool.call.requested",
            {
                "call_id": "echo-1",
                "tool_id": "builtin/echo",
                "arguments": {"text": "hello"},
            },
        ),
        _event(
            5,
            "tool.call.started",
            {"call_id": "echo-1", "tool_id": "builtin/echo"},
        ),
        _event(
            6,
            "tool.call.finished",
            {"call_id": "echo-1", "outcome": "succeeded", "result": "hello"},
        ),
        _event(
            7,
            "model.request.started",
            _model_request(2, result_call_ids=["echo-1"]),
        ),
        _event(
            8,
            "model.response.finished",
            _model_response(2, "end_turn", input_tokens=60, output_tokens=7),
        ),
        _event(
            9,
            "assistant.block.started",
            {"block_id": "answer", "block_type": "content"},
        ),
        _event(
            10,
            "assistant.block.finished",
            {"block_id": "answer", "content": "hello"},
        ),
        _event(11, "run.completed", _boundary_completed()),
    )

    snapshot, gaps = project_durable_run(events)

    assert gaps == ()
    assert snapshot.version == "run-ui-v2"
    assert snapshot.document["model_calls"] == [
        {
            **_model_request(1),
            "state": "finished",
            "request_seq": 2,
            "response_seq": 3,
            "outcome": "tool_use",
            "input_tokens": 40,
            "output_tokens": 3,
            "usage_complete": True,
            "error_code": None,
        },
        {
            **_model_request(2, result_call_ids=["echo-1"]),
            "state": "finished",
            "request_seq": 7,
            "response_seq": 8,
            "outcome": "end_turn",
            "input_tokens": 60,
            "output_tokens": 7,
            "usage_complete": True,
            "error_code": None,
        },
    ]
    decoded = decode_projection_snapshot(
        json.dumps(snapshot.to_dict(), separators=(",", ":")).encode(),
        expected_identity=snapshot.identity,
        expected_through_seq=snapshot.through_seq,
    )
    assert decoded == snapshot


def test_feature_marked_completed_run_cannot_masquerade_as_legacy() -> None:
    with pytest.raises(ReplayCorruptionError, match="model iteration"):
        project_durable_run(
            (
                _event(1, "run.started", _current_started_payload()),
                _event(2, "run.completed", _completed()),
            )
        )


@pytest.mark.parametrize(
    "events",
    [
        (
            _event(1, "run.started", _current_started_payload()),
            _event(
                2,
                "model.response.finished",
                _model_response(1, "end_turn", input_tokens=10, output_tokens=1),
            ),
        ),
        (
            _event(1, "run.started", _current_started_payload()),
            _event(2, "model.request.started", _model_request(1)),
            _event(3, "model.request.started", _model_request(2)),
        ),
        (
            _event(1, "run.started", _current_started_payload()),
            _event(2, "model.request.started", _model_request(1)),
            _event(
                3,
                "model.response.finished",
                _model_response(2, "end_turn", input_tokens=10, output_tokens=1),
            ),
        ),
    ],
)
def test_projector_rejects_unpaired_or_mismatched_model_boundaries(
    events: tuple[EventEnvelope, ...],
) -> None:
    with pytest.raises(ReplayCorruptionError):
        project_durable_run(events)


def test_projector_keeps_an_open_prefix_explicitly_incomplete() -> None:
    snapshot, gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                2,
                "assistant.block.started",
                {"block_id": "open", "block_type": "content"},
            ),
        )
    )

    assert snapshot.complete is False
    assert snapshot.document["blocks"][0]["state"] == "open"  # type: ignore[index]
    assert gaps == ()


def test_projector_exposes_the_reserved_recovery_cursor_interval() -> None:
    snapshot, gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                513,
                "run.failed",
                _failed(),
            ),
        ),
        reserved_through=512,
    )

    assert snapshot.complete is True
    assert snapshot.through_seq == 513
    assert [gap.to_dict() for gap in gaps] == [
        {
            "from_seq": 2,
            "to_seq": 512,
            "reason": "ephemeral_not_durable",
        }
    ]


def test_reserved_boundary_does_not_excuse_a_gap_beyond_the_reservation() -> None:
    with pytest.raises(ReplayCorruptionError, match="unexplained gap"):
        project_durable_run(
            (
                _event(1, "run.started", _started_payload()),
                _event(514, "run.failed", _failed()),
            ),
            reserved_through=512,
        )


def test_projector_rejects_unexplained_gap_and_conflicting_identity() -> None:
    with pytest.raises(ReplayCorruptionError, match="unexplained gap"):
        project_durable_run(
            (
                _event(1, "run.started", _started_payload()),
                _event(3, "run.completed", _completed()),
            )
        )

    foreign = _event(2, "run.completed", _completed())
    foreign = EventEnvelope(
        **{
            **foreign.__dict__,
            "conversation_id": "9" * 32,
        }
    )
    with pytest.raises(ReplayCorruptionError, match="changes identity"):
        project_durable_run(
            (_event(1, "run.started", _started_payload()), foreign)
        )


def test_projector_rejects_duplicate_event_id() -> None:
    terminal = _event(2, "run.completed", _completed())
    terminal = EventEnvelope(
        **{
            **terminal.__dict__,
            "event_id": _event(
                1, "run.started", _started_payload()
            ).event_id,
        }
    )

    with pytest.raises(ReplayCorruptionError, match="event ID"):
        project_durable_run(
            (_event(1, "run.started", _started_payload()), terminal)
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**_started_payload(), "sandbox": "unconfined"},
        {
            **_started_payload(),
            "context_plan": {
                **_started_payload()["context_plan"],  # type: ignore[dict-item]
                "input_budget_tokens": 4_096,
            },
        },
    ],
)
def test_projector_rejects_invalid_started_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ReplayCorruptionError, match="run.started"):
        project_durable_run((_event(1, "run.started", payload),))


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("run.completed", {"reason": "end_turn"}),
        (
            "run.failed",
            {
                **_failed(),
                "retryable": "yes",
            },
        ),
        (
            "run.failed",
            {
                **_failed(),
                "code": "not a safe identifier",
            },
        ),
        (
            "run.cancelled",
            {"reason": "cancelled", "usage": {"complete": False}},
        ),
    ],
)
def test_projector_rejects_invalid_terminal_payloads(
    kind: str, payload: dict[str, object]
) -> None:
    with pytest.raises(ReplayCorruptionError):
        project_durable_run(
            (
                _event(1, "run.started", _started_payload()),
                _event(2, kind, payload),
            )
        )


def test_projector_validates_the_complete_tool_contract() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                2,
                "tool.call.requested",
                {
                    "call_id": "echo-1",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": "hello"},
                },
            ),
            _event(
                3,
                "tool.call.started",
                {"call_id": "echo-1", "tool_id": "builtin/echo"},
            ),
            _event(
                4,
                "tool.call.finished",
                {
                    "call_id": "echo-1",
                    "outcome": "succeeded",
                    "result": "hello",
                },
            ),
            _event(5, "run.completed", _completed(iterations=2)),
        )
    )

    assert snapshot.document["tools"] == [
        {
            "arguments": {"text": "hello"},
            "call_id": "echo-1",
            "finish_seq": 4,
            "outcome": "succeeded",
            "request_seq": 2,
            "result": "hello",
            "state": "finished",
            "tool_id": "builtin/echo",
        }
    ]


def test_projector_rejects_second_tool_call_after_toolset_narrows() -> None:
    with pytest.raises(ReplayCorruptionError, match="Tool request"):
        project_durable_run(
            (
                _event(1, "run.started", _started_payload()),
                _event(
                    2,
                    "tool.call.requested",
                    {
                        "call_id": "echo-1",
                        "tool_id": "builtin/echo",
                        "arguments": {"text": "first"},
                    },
                ),
                _event(
                    3,
                    "tool.call.started",
                    {"call_id": "echo-1", "tool_id": "builtin/echo"},
                ),
                _event(
                    4,
                    "tool.call.finished",
                    {
                        "call_id": "echo-1",
                        "outcome": "succeeded",
                        "result": "first",
                    },
                ),
                _event(
                    5,
                    "tool.call.requested",
                    {
                        "call_id": "echo-2",
                        "tool_id": "builtin/echo",
                        "arguments": {"text": "second"},
                    },
                ),
            )
        )


def test_projector_accepts_two_sequential_tools_under_advertised_feature() -> None:
    request_two = _model_request(2, result_call_ids=["echo-1"])
    request_two["tool_count"] = 1
    request_three = _model_request(
        3, result_call_ids=["echo-1", "echo-2"]
    )
    request_three["tool_count"] = 0
    events = (
        _event(1, "run.started", _multi_tool_started_payload()),
        _event(2, "model.request.started", _model_request(1)),
        _event(3, "model.response.finished", _model_response(1, "tool_use", input_tokens=10, output_tokens=1)),
        _event(4, "tool.call.requested", {"call_id": "echo-1", "tool_id": "builtin/echo", "arguments": {"text": "first"}}),
        _event(5, "tool.call.started", {"call_id": "echo-1", "tool_id": "builtin/echo"}),
        _event(6, "tool.call.finished", {"call_id": "echo-1", "outcome": "succeeded", "result": "first"}),
        _event(7, "model.request.started", request_two),
        _event(8, "model.response.finished", _model_response(2, "tool_use", input_tokens=10, output_tokens=1)),
        _event(9, "tool.call.requested", {"call_id": "echo-2", "tool_id": "builtin/echo", "arguments": {"text": "second"}}),
        _event(10, "tool.call.started", {"call_id": "echo-2", "tool_id": "builtin/echo"}),
        _event(11, "tool.call.finished", {"call_id": "echo-2", "outcome": "succeeded", "result": "second"}),
        _event(12, "model.request.started", request_three),
        _event(13, "model.response.finished", _model_response(3, "end_turn", input_tokens=10, output_tokens=1)),
        _event(14, "assistant.block.started", {"block_id": "answer", "block_type": "content"}),
        _event(15, "assistant.block.finished", {"block_id": "answer", "content": "done"}),
        _event(16, "run.completed", {"reason": "end_turn", "model_iterations": 3, "usage": {"input_tokens": 30, "output_tokens": 3, "last_input_tokens": 10, "complete": True}}),
    )

    snapshot, gaps = project_durable_run(events)

    assert gaps == ()
    assert [item["call_id"] for item in snapshot.document["tools"]] == [
        "echo-1",
        "echo-2",
    ]
    assert snapshot.document["terminal"]["kind"] == "run.completed"


@pytest.mark.parametrize(("with_tool", "iterations"), [(False, 2), (True, 1)])
def test_projector_rejects_completed_iteration_count_that_disagrees_with_tools(
    with_tool: bool, iterations: int
) -> None:
    events = [_event(1, "run.started", _started_payload())]
    if with_tool:
        events.extend(
            (
                _event(
                    2,
                    "tool.call.requested",
                    {
                        "call_id": "echo-1",
                        "tool_id": "builtin/echo",
                        "arguments": {"text": "hello"},
                    },
                ),
                _event(
                    3,
                    "tool.call.started",
                    {"call_id": "echo-1", "tool_id": "builtin/echo"},
                ),
                _event(
                    4,
                    "tool.call.finished",
                    {
                        "call_id": "echo-1",
                        "outcome": "succeeded",
                        "result": "hello",
                    },
                ),
            )
        )
    events.append(
        _event(events[-1].seq + 1, "run.completed", _completed(iterations=iterations))
    )

    with pytest.raises(ReplayCorruptionError, match="model iteration"):
        project_durable_run(events)


@pytest.mark.parametrize(
    "requested",
    [
        {
            "call_id": "echo-1",
            "tool_id": "unknown/tool",
            "arguments": {"text": "hello"},
        },
        {
            "call_id": "echo-1",
            "tool_id": "builtin/echo",
            "arguments": {"text": 1},
        },
        {
            "call_id": "echo-1",
            "tool_id": "builtin/echo",
            "arguments": {"text": "hello", "extra": True},
        },
    ],
)
def test_projector_rejects_invalid_tool_requests(
    requested: dict[str, object],
) -> None:
    with pytest.raises(ReplayCorruptionError):
        project_durable_run(
            (
                _event(1, "run.started", _started_payload()),
                _event(2, "tool.call.requested", requested),
            )
        )


def test_projector_rejects_invalid_tool_result() -> None:
    with pytest.raises(ReplayCorruptionError, match="builtin/echo"):
        project_durable_run(
            (
                _event(1, "run.started", _started_payload()),
                _event(
                    2,
                    "tool.call.requested",
                    {
                        "call_id": "echo-1",
                        "tool_id": "builtin/echo",
                        "arguments": {"text": "expected"},
                    },
                ),
                _event(
                    3,
                    "tool.call.started",
                    {"call_id": "echo-1", "tool_id": "builtin/echo"},
                ),
                _event(
                    4,
                    "tool.call.finished",
                    {
                        "call_id": "echo-1",
                        "outcome": "succeeded",
                        "result": "forged",
                    },
                ),
            )
        )


def test_projector_accepts_control_plane_tool_recovery_failure() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                2,
                "tool.call.requested",
                {
                    "call_id": "echo-1",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": "hello"},
                },
            ),
            _event(
                3,
                "tool.call.started",
                {"call_id": "echo-1", "tool_id": "builtin/echo"},
            ),
            _event(
                4,
                "tool.call.finished",
                {
                    "call_id": "echo-1",
                    "tool_id": "builtin/echo",
                    "outcome": "failed",
                    "result": "Control Plane restarted",
                },
            ),
            _event(5, "run.failed", _failed()),
        )
    )

    assert snapshot.complete is True


def test_snapshot_semantics_reject_invalid_document_with_recomputed_digest() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(2, "run.completed", _completed()),
        )
    )
    forged = snapshot.to_dict()
    forged["document"] = {
        **forged["document"],  # type: ignore[dict-item]
        "terminal": {
            "kind": "run.completed",
            "payload": {"reason": "end_turn"},
        },
    }
    raw = _snapshot_raw_with_recomputed_digest(forged)

    with pytest.raises(ReplayCorruptionError, match="run.completed"):
        decode_projection_snapshot(
            raw,
            expected_identity=snapshot.identity,
            expected_through_seq=snapshot.through_seq,
        )


def test_decoder_keeps_existing_run_ui_v1_snapshots_readable() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(2, "run.completed", _completed()),
        )
    )
    legacy = snapshot.to_dict()
    legacy["version"] = "run-ui-v1"
    document = legacy["document"]
    assert isinstance(document, dict)
    document.pop("model_calls")
    raw = _snapshot_raw_with_recomputed_digest(legacy)

    decoded = decode_projection_snapshot(
        raw,
        expected_identity=snapshot.identity,
        expected_through_seq=snapshot.through_seq,
    )

    assert decoded.version == "run-ui-v1"
    assert "model_calls" not in decoded.document


def test_snapshot_semantics_reject_invalid_state_with_recomputed_digest() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                2,
                "assistant.block.started",
                {"block_id": "answer", "block_type": "content"},
            ),
            _event(
                3,
                "assistant.block.finished",
                {"block_id": "answer", "content": "done"},
            ),
            _event(4, "run.completed", _completed()),
        )
    )
    forged = snapshot.to_dict()
    document = forged["document"]
    assert isinstance(document, dict)
    blocks = document["blocks"]
    assert isinstance(blocks, list) and isinstance(blocks[0], dict)
    blocks[0]["state"] = "open"
    raw = _snapshot_raw_with_recomputed_digest(forged)

    with pytest.raises(ReplayCorruptionError, match="open block"):
        decode_projection_snapshot(
            raw,
            expected_identity=snapshot.identity,
            expected_through_seq=snapshot.through_seq,
        )


def test_snapshot_rejects_second_tool_call_with_recomputed_digest() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                2,
                "tool.call.requested",
                {
                    "call_id": "echo-1",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": "first"},
                },
            ),
            _event(
                3,
                "tool.call.started",
                {"call_id": "echo-1", "tool_id": "builtin/echo"},
            ),
            _event(
                4,
                "tool.call.finished",
                {
                    "call_id": "echo-1",
                    "outcome": "succeeded",
                    "result": "first",
                },
            ),
            _event(5, "run.completed", _completed(iterations=2)),
        )
    )
    forged = snapshot.to_dict()
    forged["through_seq"] = 8
    document = forged["document"]
    assert isinstance(document, dict)
    tools = document["tools"]
    terminal = document["terminal"]
    assert isinstance(tools, list) and isinstance(terminal, dict)
    tools.append(
        {
            "arguments": {"text": "second"},
            "call_id": "echo-2",
            "finish_seq": 7,
            "outcome": "succeeded",
            "request_seq": 5,
            "result": "second",
            "state": "finished",
            "tool_id": "builtin/echo",
        }
    )
    payload = terminal["payload"]
    assert isinstance(payload, dict)
    payload["model_iterations"] = 3
    raw = _snapshot_raw_with_recomputed_digest(forged)

    with pytest.raises(ReplayCorruptionError, match="too many Tool"):
        decode_projection_snapshot(
            raw,
            expected_identity=snapshot.identity,
            expected_through_seq=8,
        )


def test_snapshot_rejects_iteration_count_with_recomputed_digest() -> None:
    snapshot, _gaps = project_durable_run(
        (
            _event(1, "run.started", _started_payload()),
            _event(
                2,
                "tool.call.requested",
                {
                    "call_id": "echo-1",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": "hello"},
                },
            ),
            _event(
                3,
                "tool.call.started",
                {"call_id": "echo-1", "tool_id": "builtin/echo"},
            ),
            _event(
                4,
                "tool.call.finished",
                {
                    "call_id": "echo-1",
                    "outcome": "succeeded",
                    "result": "hello",
                },
            ),
            _event(5, "run.completed", _completed(iterations=2)),
        )
    )
    forged = snapshot.to_dict()
    document = forged["document"]
    assert isinstance(document, dict)
    terminal = document["terminal"]
    assert isinstance(terminal, dict)
    payload = terminal["payload"]
    assert isinstance(payload, dict)
    payload["model_iterations"] = 1
    raw = _snapshot_raw_with_recomputed_digest(forged)

    with pytest.raises(ReplayCorruptionError, match="model iteration"):
        decode_projection_snapshot(
            raw,
            expected_identity=snapshot.identity,
            expected_through_seq=snapshot.through_seq,
        )

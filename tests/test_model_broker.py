"""Worker-side bounded model capability protocol tests."""

from __future__ import annotations

from io import BytesIO
import json

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import ContextCompiler, ModelContext, ModelProfile
from agent_builder_v2.model import (
    BrokeredCapabilityClient,
    BrokeredStreamingModel,
    ModelToolResult,
)
from agent_builder_v2.tools import prototype_tool_specs, runtime_tool_specs


def _context(
    message: str, *, tools=prototype_tool_specs()
) -> ModelContext:
    profile = ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest="a" * 64,
        native_context_tokens=262_144,
        operational_context_tokens=32_768,
        max_output_tokens=2_048,
        profile_source="test",
    )
    plan = ContextCompiler().compile(
        message,
        model_profile=profile,
        tools=tools,
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    return ModelContext(plan.reference, message)


def _frame(request_id: str, frame_type: str, **values: object) -> bytes:
    return (
        json.dumps(
            {
                "internal": "model.response",
                "version": 2,
                "request_id": request_id,
                "type": frame_type,
                **values,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def test_brokered_model_normalizes_tool_then_final_content() -> None:
    responses = BytesIO(
        b"".join(
            (
                _frame("model-1", "content", text="准备调用工具。"),
                _frame(
                    "model-1",
                    "tool.use",
                    call_id="call-safe-1",
                    tool_id="builtin/echo",
                    arguments={"text": "hello"},
                ),
                _frame("model-2", "content", text="真实模型已收到：hello"),
                _frame("model-2", "stop", reason="end_turn"),
            )
        )
    )
    requests = BytesIO()
    model = BrokeredStreamingModel(responses, requests)
    context = _context("hello")

    first = list(model.stream(context, (), lambda: False))
    second = list(
        model.stream(
            context,
            (ModelToolResult("call-safe-1", "builtin/echo", "succeeded", "hello"),),
            lambda: False,
        )
    )

    assert [block.kind for block in first] == [
        "text.start",
        "text.delta",
        "text.finish",
        "tool.use",
    ]
    assert first[-1].payload == {
        "call_id": "call-safe-1",
        "tool_id": "builtin/echo",
        "arguments": {"text": "hello"},
    }
    assert [block.kind for block in second] == [
        "text.start",
        "text.delta",
        "text.finish",
        "stop",
    ]
    sent = [json.loads(line) for line in requests.getvalue().splitlines()]
    assert sent == [
        {
            "internal": "model.request",
            "version": 2,
            "request_id": "model-1",
            "iteration": 1,
            "context_plan": context.reference.to_dict(),
            "tool_result_call_ids": [],
        },
        {
            "internal": "model.request",
            "version": 2,
            "request_id": "model-2",
            "iteration": 2,
            "context_plan": context.reference.to_dict(),
            "tool_result_call_ids": ["call-safe-1"],
        },
    ]


def test_brokered_model_accepts_repetition_truncation_stop() -> None:
    model = BrokeredStreamingModel(
        BytesIO(
            b"".join(
                (
                    _frame("model-1", "content", text="bounded answer"),
                    _frame(
                        "model-1",
                        "stop",
                        reason="repetition_truncated",
                    ),
                )
            )
        ),
        BytesIO(),
        effective_tools=(),
    )

    blocks = list(model.stream(_context("write a joke", tools=()), (), lambda: False))

    assert blocks[-1].kind == "stop"
    assert blocks[-1].payload == {"reason": "repetition_truncated"}


def test_brokered_model_rejects_mismatched_or_oversized_frames() -> None:
    context = _context("hello")
    mismatched = BrokeredStreamingModel(
        BytesIO(_frame("another-request", "stop", reason="end_turn")),
        BytesIO(),
    )
    with pytest.raises(RuntimeError, match="invalid envelope"):
        list(mismatched.stream(context, (), lambda: False))

    oversized = BrokeredStreamingModel(BytesIO(b"x" * 65_537 + b"\n"), BytesIO())
    with pytest.raises(RuntimeError, match="exceeded"):
        list(oversized.stream(context, (), lambda: False))


def test_brokered_model_rejects_capability_escalation() -> None:
    responses = BytesIO(
        _frame(
            "model-1",
            "tool.use",
            call_id="call-1",
            tool_id="arbitrary/shell",
            arguments={"command": "id"},
        )
    )
    model = BrokeredStreamingModel(responses, BytesIO())

    with pytest.raises(RuntimeError, match="tool call is invalid"):
        list(model.stream(_context("hello"), (), lambda: False))


def test_explicit_empty_tool_set_never_restores_prototype_capabilities() -> None:
    responses = BytesIO(
        _frame(
            "model-1",
            "tool.use",
            call_id="call-1",
            tool_id="builtin/echo",
            arguments={"text": "not authorized"},
        )
    )
    model = BrokeredStreamingModel(responses, BytesIO(), effective_tools=())

    with pytest.raises(RuntimeError, match="tool call is invalid"):
        list(model.stream(_context("hello", tools=()), (), lambda: False))


def _capability_response(**changes: object) -> bytes:
    value = {
        "internal": "capability.response",
        "version": 2,
        "request_id": "capability-1",
        "type": "result",
        "call_id": "read-call",
        "tool_id": "file/read_text",
        "outcome": "succeeded",
        "content": '{"content":"bounded"}',
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def test_brokered_capability_client_binds_call_and_arguments() -> None:
    requests = BytesIO()
    client = BrokeredCapabilityClient(
        BytesIO(_capability_response()), requests, runtime_tool_specs()
    )
    result = client.execute(
        "file/read_text", {"path": "facts.txt", "max_bytes": 128}, "read-call"
    )

    assert result.outcome == "succeeded"
    assert result.content == '{"content":"bounded"}'
    assert json.loads(requests.getvalue()) == {
        "internal": "capability.request",
        "version": 2,
        "request_id": "capability-1",
        "call_id": "read-call",
        "tool_id": "file/read_text",
        "arguments": {"path": "facts.txt", "max_bytes": 128},
    }


@pytest.mark.parametrize(
    "changes",
    (
        {"request_id": "different"},
        {"call_id": "different"},
        {"tool_id": "file/stat"},
        {"outcome": "approved"},
        {"extra": "field"},
    ),
)
def test_brokered_capability_client_rejects_response_confusion(
    changes: dict[str, object],
) -> None:
    client = BrokeredCapabilityClient(
        BytesIO(_capability_response(**changes)), BytesIO(), runtime_tool_specs()
    )
    result = client.execute("file/read_text", {"path": "facts.txt"}, "read-call")
    assert result.outcome == "failed"
    assert result.content == "Brokered capability response is invalid"

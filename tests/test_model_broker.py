"""Worker-side bounded model capability protocol tests."""

from __future__ import annotations

from io import BytesIO
import json

import pytest

from agent_builder_v2.context import ContextCompiler
from agent_builder_v2.model import BrokeredStreamingModel


def _frame(request_id: str, frame_type: str, **values: object) -> bytes:
    return (
        json.dumps(
            {
                "internal": "model.response",
                "version": 1,
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
    sections = ContextCompiler().compile("hello")

    first = list(model.stream(sections, (), lambda: False))
    second = list(model.stream(sections, ("hello",), lambda: False))

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
            "version": 1,
            "request_id": "model-1",
            "iteration": 1,
            "tool_results": [],
        },
        {
            "internal": "model.request",
            "version": 1,
            "request_id": "model-2",
            "iteration": 2,
            "tool_results": ["hello"],
        },
    ]


def test_brokered_model_rejects_mismatched_or_oversized_frames() -> None:
    sections = ContextCompiler().compile("hello")
    mismatched = BrokeredStreamingModel(
        BytesIO(_frame("another-request", "stop", reason="end_turn")),
        BytesIO(),
    )
    with pytest.raises(RuntimeError, match="invalid envelope"):
        list(mismatched.stream(sections, (), lambda: False))

    oversized = BrokeredStreamingModel(BytesIO(b"x" * 65_537 + b"\n"), BytesIO())
    with pytest.raises(RuntimeError, match="exceeded"):
        list(oversized.stream(sections, (), lambda: False))


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
        list(model.stream(ContextCompiler().compile("hello"), (), lambda: False))

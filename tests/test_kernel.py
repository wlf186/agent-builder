"""Contract tests for the single finite HarnessKernel run loop."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest

from agent_builder_v2.contracts import TERMINAL_KINDS, WorkerEvent
from agent_builder_v2.kernel import CancellationToken, HarnessKernel
from agent_builder_v2.model import ModelBlock


def _assert_single_terminal_last(events: list[WorkerEvent], expected: str) -> None:
    terminal_events = [event for event in events if event.kind in TERMINAL_KINDS]
    assert [event.kind for event in terminal_events] == [expected]
    assert events[-1] is terminal_events[0]


def _assert_tool_calls_are_paired(events: list[WorkerEvent]) -> None:
    requested = Counter(
        str(event.payload["call_id"])
        for event in events
        if event.kind == "tool.call.requested"
    )
    finished = Counter(
        str(event.payload["call_id"])
        for event in events
        if event.kind == "tool.call.finished"
    )
    assert requested
    assert requested == finished
    assert set(requested.values()) == {1}


def test_golden_path_streams_tools_and_completes_in_order() -> None:
    kernel = HarnessKernel()

    events = list(kernel.run("  hello harness  "))

    assert [event.kind for event in events] == [
        "assistant.block.started",
        "assistant.block.delta",
        "assistant.block.delta",
        "assistant.block.finished",
        "tool.call.requested",
        "tool.call.started",
        "tool.call.finished",
        "assistant.block.started",
        "assistant.block.delta",
        "assistant.block.delta",
        "assistant.block.finished",
        "tool.call.requested",
        "tool.call.started",
        "tool.call.finished",
        "assistant.block.started",
        "assistant.block.delta",
        "assistant.block.delta",
        "assistant.block.delta",
        "assistant.block.finished",
        "run.completed",
    ]
    assert all(
        event.durability == (
            "ephemeral" if event.kind == "assistant.block.delta" else "durable"
        )
        for event in events
    )

    requested_events = [
        event for event in events if event.kind == "tool.call.requested"
    ]
    started_events = [event for event in events if event.kind == "tool.call.started"]
    finished_events = [event for event in events if event.kind == "tool.call.finished"]
    assert requested_events[0].payload == {
        "call_id": "prototype-echo-call-1",
        "tool_id": "builtin/echo",
        "arguments": {"text": "hello harness"},
    }
    assert requested_events[1].payload == {
        "call_id": "prototype-echo-call-2",
        "tool_id": "builtin/echo",
        "arguments": {"text": "hello harness"},
    }
    assert [item.payload["call_id"] for item in started_events] == [
        item.payload["call_id"] for item in requested_events
    ]
    assert finished_events[-1].payload == {
        "call_id": "prototype-echo-call-2",
        "tool_id": "builtin/echo",
        "outcome": "succeeded",
        "result": "hello harness",
    }
    final_block = [
        event
        for event in events
        if event.kind == "assistant.block.finished"
    ][-1]
    assert "hello harness" in str(final_block.payload["content"])
    assert events[-1].payload == {
        "reason": "end_turn",
        "model_iterations": 3,
    }
    assert kernel.state.open_blocks == {}
    assert kernel.state.pending_tools == set()
    assert kernel.state.terminal_kind == "run.completed"
    _assert_tool_calls_are_paired(events)
    _assert_single_terminal_last(events, "run.completed")


class _CancelWithOpenBlockModel:
    def __init__(self, cancellation: CancellationToken) -> None:
        self._cancellation = cancellation

    def stream(self, *_args: Any, **_kwargs: Any) -> Iterator[ModelBlock]:
        yield ModelBlock("text.start", {"block_id": "interrupted"})
        self._cancellation.cancel()
        yield ModelBlock(
            "text.delta", {"block_id": "interrupted", "text": "must not escape"}
        )


def test_cancellation_discards_open_blocks_before_terminal() -> None:
    cancellation = CancellationToken()
    kernel = HarnessKernel(
        model=_CancelWithOpenBlockModel(cancellation),  # type: ignore[arg-type]
        cancellation=cancellation,
    )

    events = list(kernel.run("cancel this run"))

    assert [event.kind for event in events] == [
        "assistant.block.started",
        "assistant.block.discarded",
        "run.cancelled",
    ]
    assert events[1].payload == {
        "block_id": "interrupted",
        "reason": "cancelled",
    }
    assert all(event.kind != "assistant.block.delta" for event in events)
    assert kernel.state.open_blocks == {}
    assert kernel.state.pending_tools == set()
    _assert_single_terminal_last(events, "run.cancelled")


class _UnknownToolModel:
    def stream(self, *_args: Any, **_kwargs: Any) -> Iterator[ModelBlock]:
        yield ModelBlock(
            "tool.use",
            {
                "call_id": "unknown-call",
                "tool_id": "missing/tool",
                "arguments": {},
            },
        )


def test_tool_failure_is_paired_before_failed_terminal() -> None:
    kernel = HarnessKernel(model=_UnknownToolModel())  # type: ignore[arg-type]

    events = list(kernel.run("exercise failure cleanup"))

    assert [event.kind for event in events] == [
        "tool.call.requested",
        "tool.call.started",
        "tool.call.finished",
        "run.failed",
    ]
    assert events[2].payload == {
        "call_id": "unknown-call",
        "outcome": "failed",
        "result": "runtime failure",
    }
    assert kernel.state.pending_tools == set()
    _assert_tool_calls_are_paired(events)
    _assert_single_terminal_last(events, "run.failed")


def test_kernel_rejects_emission_after_terminal() -> None:
    kernel = HarnessKernel()
    terminal = kernel._event("run.completed")

    assert terminal.kind == "run.completed"
    with pytest.raises(RuntimeError, match="after terminal"):
        kernel._event("assistant.block.started", {"block_id": "too-late"})

"""The single finite Run loop for the greenfield walking skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from threading import Event
from typing import Iterator

from .context import ContextPlanReference, ModelContext
from .contracts import TERMINAL_KINDS, WorkerEvent
from .model import FakeStreamingModel, ModelBlock, ModelToolResult, StreamingModel
from .tools import ToolRegistry, prototype_tools, toolset_digest


@dataclass
class RunState:
    phase: str = "accepted"
    model_iterations: int = 0
    open_blocks: dict[str, list[str]] = field(default_factory=dict)
    pending_tools: set[str] = field(default_factory=set)
    tool_results: list[ModelToolResult] = field(default_factory=list)
    terminal_kind: str | None = None


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class HarnessKernel:
    """Own model, Tool, context, cancellation and terminal state in one loop."""

    def __init__(
        self,
        *,
        model: StreamingModel | None = None,
        tools: ToolRegistry | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.model = model or FakeStreamingModel()
        self.tools = tools or prototype_tools()
        self.cancellation = cancellation or CancellationToken()
        self.state = RunState()

    def _event(
        self, kind: str, payload: dict[str, object] | None = None
    ) -> WorkerEvent:
        if self.state.terminal_kind is not None:
            raise RuntimeError("event emitted after terminal state")
        if kind in TERMINAL_KINDS:
            self.state.terminal_kind = kind
            self.state.phase = "terminal"
        return WorkerEvent(
            kind=kind,
            durability="durable" if kind != "assistant.block.delta" else "ephemeral",
            payload=payload or {},
        )

    def _cancel_events(self) -> Iterator[WorkerEvent]:
        for block_id in tuple(self.state.open_blocks):
            self.state.open_blocks.pop(block_id, None)
            yield self._event(
                "assistant.block.discarded",
                {"block_id": block_id, "reason": "cancelled"},
            )
        for call_id in tuple(self.state.pending_tools):
            self.state.pending_tools.remove(call_id)
            yield self._event(
                "tool.call.finished",
                {"call_id": call_id, "outcome": "cancelled", "result": "cancelled"},
            )
        yield self._event("run.cancelled", {"reason": "cancelled"})

    def _handle_model_block(self, block: ModelBlock) -> Iterator[WorkerEvent]:
        payload = block.payload
        if block.kind == "text.start":
            block_id = str(payload["block_id"])
            if block_id in self.state.open_blocks:
                raise RuntimeError("duplicate content block")
            self.state.open_blocks[block_id] = []
            yield self._event(
                "assistant.block.started",
                {"block_id": block_id, "block_type": "content"},
            )
        elif block.kind == "text.delta":
            block_id = str(payload["block_id"])
            text = str(payload["text"])
            if block_id not in self.state.open_blocks:
                raise RuntimeError("delta for unopened block")
            self.state.open_blocks[block_id].append(text)
            yield self._event(
                "assistant.block.delta", {"block_id": block_id, "text": text}
            )
        elif block.kind == "text.finish":
            block_id = str(payload["block_id"])
            fragments = self.state.open_blocks.pop(block_id, None)
            if fragments is None:
                raise RuntimeError("finish for unopened block")
            yield self._event(
                "assistant.block.finished",
                {"block_id": block_id, "content": "".join(fragments)},
            )
        elif block.kind == "tool.use":
            call_id = str(payload["call_id"])
            tool_id = str(payload["tool_id"])
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                raise RuntimeError("tool arguments must be an object")
            if call_id in self.state.pending_tools:
                raise RuntimeError("duplicate tool call")
            self.state.pending_tools.add(call_id)
            yield self._event(
                "tool.call.requested",
                {"call_id": call_id, "tool_id": tool_id, "arguments": arguments},
            )
            yield self._event(
                "tool.call.started", {"call_id": call_id, "tool_id": tool_id}
            )
            if self.cancellation.is_cancelled():
                result_outcome = "cancelled"
                result_content = "cancelled"
            else:
                result = self.tools.execute(tool_id, arguments)
                result_outcome = result.outcome
                result_content = result.content
            self.state.pending_tools.remove(call_id)
            self.state.tool_results.append(
                ModelToolResult(
                    call_id=call_id,
                    tool_id=tool_id,
                    outcome=result_outcome,
                    content=result_content,
                )
            )
            yield self._event(
                "tool.call.finished",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "outcome": result_outcome,
                    "result": result_content,
                },
            )
        elif block.kind == "stop":
            return
        else:
            raise RuntimeError(f"unknown normalized model block: {block.kind}")

    def _local_context_reference(self, user_message: str) -> ContextPlanReference:
        effective_toolset_digest = toolset_digest(self.tools.specs())
        digest = hashlib.sha256(
            b"agent-builder-local-kernel-context-v1\0"
            + effective_toolset_digest.encode("ascii")
            + b"\0"
            + user_message.encode("utf-8")
        ).hexdigest()
        return ContextPlanReference(
            plan_id=f"context-{digest[:24]}",
            digest=digest,
            toolset_digest=effective_toolset_digest,
        )

    def run(
        self,
        user_message: str,
        context_reference: ContextPlanReference | None = None,
    ) -> Iterator[WorkerEvent]:
        try:
            self.state.phase = "preparing_context"
            model_context = ModelContext(
                context_reference or self._local_context_reference(user_message),
                user_message,
            )
            while self.state.model_iterations < 3:
                if self.cancellation.is_cancelled():
                    yield from self._cancel_events()
                    return
                self.state.model_iterations += 1
                self.state.phase = "streaming_model"
                saw_stop = False
                saw_tool = False
                for block in self.model.stream(
                    model_context,
                    tuple(self.state.tool_results),
                    self.cancellation.is_cancelled,
                ):
                    if self.cancellation.is_cancelled():
                        yield from self._cancel_events()
                        return
                    if block.kind == "tool.use":
                        saw_tool = True
                        self.state.phase = "executing_tools"
                    if block.kind == "stop":
                        saw_stop = True
                    yield from self._handle_model_block(block)
                if self.cancellation.is_cancelled():
                    yield from self._cancel_events()
                    return
                if saw_stop:
                    yield self._event(
                        "run.completed",
                        {
                            "reason": "end_turn",
                            "model_iterations": self.state.model_iterations,
                        },
                    )
                    return
                if not saw_tool:
                    raise RuntimeError("model ended without stop or tool request")
            raise RuntimeError("model iteration budget exhausted")
        except Exception:
            if self.state.terminal_kind is None:
                for block_id in tuple(self.state.open_blocks):
                    self.state.open_blocks.pop(block_id, None)
                    yield self._event(
                        "assistant.block.discarded",
                        {"block_id": block_id, "reason": "runtime_failure"},
                    )
                for call_id in tuple(self.state.pending_tools):
                    self.state.pending_tools.remove(call_id)
                    yield self._event(
                        "tool.call.finished",
                        {
                            "call_id": call_id,
                            "outcome": "failed",
                            "result": "runtime failure",
                        },
                    )
                yield self._event(
                    "run.failed",
                    {
                        "code": "prototype_runtime_failure",
                        "message": "The prototype Run failed.",
                        "retryable": False,
                    },
                )

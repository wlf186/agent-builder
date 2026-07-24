"""Normalized model services for the finite Harness Kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from time import sleep
from typing import Any, BinaryIO, Callable, Iterator, Protocol

from .context import ModelContext
from .tools import ToolResult, ToolSpec, prototype_tool_specs, toolset_digest


VALID_COMPLETED_STOP_REASONS = frozenset(
    {"end_turn", "max_output", "repetition_truncated"}
)


@dataclass(frozen=True)
class ModelBlock:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelToolResult:
    call_id: str
    tool_id: str
    outcome: str
    content: str = field(repr=False)


class BrokeredCapabilityClient:
    """A reference-only Tool client over the already inherited Worker pipes."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        effective_tools: tuple[ToolSpec, ...],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._tools = {spec.tool_id: spec for spec in effective_tools}
        self._sequence = 0

    def execute(
        self,
        tool_id: str,
        arguments: dict[str, str | int | bool],
        call_id: str,
    ) -> ToolResult:
        spec = self._tools.get(tool_id)
        if spec is None or tool_id not in {
            "file/stat", "file/read_text", "file/glob", "file/grep",
            "file/edit", "file/write", "exec/run", "extension/call", "skill/run",
            "agent/delegate",
        }:
            return ToolResult("failed", "Unsupported brokered capability")
        try:
            validated = spec.validate_arguments(arguments)
        except ValueError:
            return ToolResult("failed", "Invalid brokered capability arguments")
        if _BROKER_ID.fullmatch(call_id) is None:
            return ToolResult("failed", "Invalid brokered capability identity")
        self._sequence += 1
        request_id = f"capability-{self._sequence}"
        payload = json.dumps(
            {
                "internal": "capability.request",
                "version": BROKER_PROTOCOL_VERSION,
                "request_id": request_id,
                "call_id": call_id,
                "tool_id": tool_id,
                "arguments": validated,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(payload) > MAX_BROKER_FRAME_BYTES:
            return ToolResult("failed", "Brokered capability request is too large")
        self._writer.write(payload)
        self._writer.flush()
        raw = self._reader.readline(MAX_BROKER_FRAME_BYTES + 1)
        if (
            not raw
            or len(raw) > MAX_BROKER_FRAME_BYTES
            or not raw.endswith(b"\n")
        ):
            return ToolResult("failed", "Brokered capability response is invalid")
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ToolResult("failed", "Brokered capability response is invalid")
        if (
            not isinstance(response, dict)
            or set(response)
            != {
                "internal", "version", "request_id", "type", "call_id",
                "tool_id", "outcome", "content",
            }
            or response.get("internal") != "capability.response"
            or response.get("version") != BROKER_PROTOCOL_VERSION
            or response.get("request_id") != request_id
            or response.get("type") != "result"
            or response.get("call_id") != call_id
            or response.get("tool_id") != tool_id
            or response.get("outcome") not in {"succeeded", "failed", "cancelled"}
        ):
            return ToolResult("failed", "Brokered capability response is invalid")
        try:
            content = spec.validate_result(response.get("content"))
        except ValueError:
            return ToolResult("failed", "Brokered capability response is invalid")
        return ToolResult(str(response["outcome"]), content)


class StreamingModel(Protocol):
    def stream(
        self,
        context: ModelContext,
        tool_results: tuple[ModelToolResult, ...],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[ModelBlock]: ...


class FakeStreamingModel:
    """Deterministically requests two sequential Tools, then answers."""

    def stream(
        self,
        context: ModelContext,
        tool_results: tuple[ModelToolResult, ...],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[ModelBlock]:
        if len(tool_results) < 2:
            call_number = len(tool_results) + 1
            block_id = f"analysis-summary-{call_number}"
            yield ModelBlock("text.start", {"block_id": block_id})
            for fragment in (
                "已装配上下文。",
                f"现在执行第 {call_number} 次结构化 Echo 调用。",
            ):
                if is_cancelled():
                    return
                sleep(0.03)
                yield ModelBlock(
                    "text.delta", {"block_id": block_id, "text": fragment}
                )
            yield ModelBlock("text.finish", {"block_id": block_id})
            yield ModelBlock(
                "tool.use",
                {
                    "call_id": f"prototype-echo-call-{call_number}",
                    "tool_id": "builtin/echo",
                    "arguments": {
                        "text": (
                            context.user_message.strip()
                            if not tool_results
                            else tool_results[-1].content
                        )
                    },
                },
            )
            return

        block_id = "final-answer"
        yield ModelBlock("text.start", {"block_id": block_id})
        fragments = (
            "工具结果已按原调用 ID 回流：",
            tool_results[-1].content,
            "。这条响应来自同一个 HarnessKernel 的第三次模型迭代。",
        )
        for fragment in fragments:
            if is_cancelled():
                return
            sleep(0.03)
            yield ModelBlock("text.delta", {"block_id": block_id, "text": fragment})
        yield ModelBlock("text.finish", {"block_id": block_id})
        yield ModelBlock("stop", {"reason": "end_turn"})


BROKER_PROTOCOL_VERSION = 2
MAX_BROKER_FRAME_BYTES = 65_536
# The trusted provider adapter may emit at most 128 coalesced content frames
# plus one Tool/stop/error terminal frame per model iteration.
MAX_BROKER_RESPONSE_FRAMES = 129
MAX_BROKER_TEXT_BYTES = 12_288
_BROKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class BrokeredStreamingModel:
    """Use bounded inherited pipes; the untrusted Worker never opens a socket."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        effective_tools: tuple[ToolSpec, ...] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._iteration = 0
        specs = prototype_tool_specs() if effective_tools is None else effective_tools
        self._tools = {spec.tool_id: spec for spec in specs}
        if len(self._tools) != len(specs):
            raise RuntimeError("model capability set has duplicate Tool IDs")
        self._toolset_digest = toolset_digest(specs)

    @staticmethod
    def _bounded_text(value: object, maximum: int = MAX_BROKER_TEXT_BYTES) -> str:
        if not isinstance(value, str):
            raise RuntimeError("model broker returned invalid text")
        if len(value) > maximum or len(value.encode("utf-8")) > maximum:
            raise RuntimeError("model broker text exceeded its limit")
        return value

    def _send_request(
        self,
        request_id: str,
        context: ModelContext,
        tool_results: tuple[ModelToolResult, ...],
    ) -> None:
        if context.reference.toolset_digest != self._toolset_digest:
            raise RuntimeError("model context Tool set changed")
        if len(tool_results) > 3 or len({item.call_id for item in tool_results}) != len(
            tool_results
        ):
            raise RuntimeError("model broker tool-result limit exceeded")
        for result in tool_results:
            if not isinstance(result, ModelToolResult):
                raise RuntimeError("model broker Tool result is invalid")
            spec = self._tools.get(result.tool_id)
            if (
                not _BROKER_ID.fullmatch(result.call_id)
                or spec is None
                or result.outcome not in {"succeeded", "failed", "cancelled"}
            ):
                raise RuntimeError("model broker Tool result is invalid")
            try:
                spec.validate_result(result.content)
            except ValueError as exc:
                raise RuntimeError("model broker Tool result is invalid") from exc
        payload = json.dumps(
            {
                "internal": "model.request",
                "version": BROKER_PROTOCOL_VERSION,
                "request_id": request_id,
                "iteration": self._iteration,
                "context_plan": context.reference.to_dict(),
                "tool_result_call_ids": [result.call_id for result in tool_results],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(payload) > MAX_BROKER_FRAME_BYTES:
            raise RuntimeError("model broker request exceeded its limit")
        self._writer.write(payload)
        self._writer.flush()

    def _read_response(self, request_id: str) -> dict[str, Any]:
        raw = self._reader.readline(MAX_BROKER_FRAME_BYTES + 1)
        if not raw:
            raise RuntimeError("model broker closed unexpectedly")
        if len(raw) > MAX_BROKER_FRAME_BYTES or not raw.endswith(b"\n"):
            raise RuntimeError("model broker frame exceeded its limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("model broker returned invalid JSON") from exc
        if (
            not isinstance(value, dict)
            or value.get("internal") != "model.response"
            or value.get("version") != BROKER_PROTOCOL_VERSION
            or value.get("request_id") != request_id
            or not isinstance(value.get("type"), str)
        ):
            raise RuntimeError("model broker returned an invalid envelope")
        return value

    def stream(
        self,
        context: ModelContext,
        tool_results: tuple[ModelToolResult, ...],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[ModelBlock]:
        if not isinstance(context, ModelContext):
            raise RuntimeError("model context is invalid")
        self._iteration += 1
        request_id = f"model-{self._iteration}"
        self._send_request(request_id, context, tool_results)
        block_id = f"ollama-content-{self._iteration}"
        block_open = False

        for _frame_index in range(MAX_BROKER_RESPONSE_FRAMES):
            if is_cancelled():
                return
            frame = self._read_response(request_id)
            frame_type = frame["type"]
            if frame_type == "content":
                if set(frame) != {
                    "internal",
                    "version",
                    "request_id",
                    "type",
                    "text",
                }:
                    raise RuntimeError("model broker content frame is invalid")
                text = self._bounded_text(frame.get("text"))
                if not text:
                    continue
                if not block_open:
                    block_open = True
                    yield ModelBlock("text.start", {"block_id": block_id})
                yield ModelBlock(
                    "text.delta", {"block_id": block_id, "text": text}
                )
                continue
            if frame_type == "tool.use":
                if set(frame) != {
                    "internal",
                    "version",
                    "request_id",
                    "type",
                    "call_id",
                    "tool_id",
                    "arguments",
                }:
                    raise RuntimeError("model broker tool frame is invalid")
                call_id = frame.get("call_id")
                tool_id = frame.get("tool_id")
                spec = self._tools.get(tool_id) if isinstance(tool_id, str) else None
                if (
                    not isinstance(call_id, str)
                    or _BROKER_ID.fullmatch(call_id) is None
                    or spec is None
                ):
                    raise RuntimeError("model broker tool call is invalid")
                try:
                    arguments = spec.validate_arguments(frame.get("arguments"))
                except ValueError as exc:
                    raise RuntimeError("model broker tool call is invalid") from exc
                if block_open:
                    yield ModelBlock("text.finish", {"block_id": block_id})
                yield ModelBlock(
                    "tool.use",
                    {
                        "call_id": call_id,
                        "tool_id": spec.tool_id,
                        "arguments": arguments,
                    },
                )
                return
            if frame_type == "stop":
                if set(frame) != {
                    "internal",
                    "version",
                    "request_id",
                    "type",
                    "reason",
                } or frame.get("reason") not in VALID_COMPLETED_STOP_REASONS:
                    raise RuntimeError("model broker stop frame is invalid")
                if block_open:
                    yield ModelBlock("text.finish", {"block_id": block_id})
                yield ModelBlock("stop", {"reason": frame["reason"]})
                return
            if frame_type == "error":
                if set(frame) != {
                    "internal",
                    "version",
                    "request_id",
                    "type",
                    "code",
                }:
                    raise RuntimeError("model broker error frame is invalid")
                self._bounded_text(frame.get("code"), 128)
                raise RuntimeError("trusted model broker rejected the request")
            raise RuntimeError("model broker returned an unknown frame")
        raise RuntimeError("model broker response-frame limit exceeded")


__all__ = [
    "BROKER_PROTOCOL_VERSION",
    "BrokeredCapabilityClient",
    "BrokeredStreamingModel",
    "FakeStreamingModel",
    "MAX_BROKER_FRAME_BYTES",
    "ModelBlock",
    "ModelToolResult",
    "StreamingModel",
    "VALID_COMPLETED_STOP_REASONS",
]

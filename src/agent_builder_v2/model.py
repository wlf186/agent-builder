"""Normalized model services for the finite Harness Kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from time import sleep
from typing import Any, BinaryIO, Callable, Iterator, Protocol

from .context import PromptSection


@dataclass(frozen=True)
class ModelBlock:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


class StreamingModel(Protocol):
    def stream(
        self,
        sections: tuple[PromptSection, ...],
        tool_results: tuple[str, ...],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[ModelBlock]: ...


class FakeStreamingModel:
    """Deterministically requests one tool, then consumes its result."""

    def stream(
        self,
        sections: tuple[PromptSection, ...],
        tool_results: tuple[str, ...],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[ModelBlock]:
        user_message = next(
            section.content for section in sections if section.section_id == "turn.user"
        )
        if not tool_results:
            block_id = "analysis-summary"
            yield ModelBlock("text.start", {"block_id": block_id})
            for fragment in ("已装配上下文。", "现在调用结构化 Echo 工具。"):
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
                    "call_id": "prototype-echo-call",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": user_message.strip()},
                },
            )
            return

        block_id = "final-answer"
        yield ModelBlock("text.start", {"block_id": block_id})
        fragments = (
            "工具结果已按原调用 ID 回流：",
            tool_results[-1],
            "。这条响应来自同一个 HarnessKernel 的第二次模型迭代。",
        )
        for fragment in fragments:
            if is_cancelled():
                return
            sleep(0.03)
            yield ModelBlock("text.delta", {"block_id": block_id, "text": fragment})
        yield ModelBlock("text.finish", {"block_id": block_id})
        yield ModelBlock("stop", {"reason": "end_turn"})


BROKER_PROTOCOL_VERSION = 1
MAX_BROKER_FRAME_BYTES = 65_536
MAX_BROKER_RESPONSE_FRAMES = 256
MAX_BROKER_TEXT_BYTES = 12_288
_BROKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class BrokeredStreamingModel:
    """Use bounded inherited pipes; the untrusted Worker never opens a socket."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._iteration = 0

    @staticmethod
    def _bounded_text(value: object, maximum: int = MAX_BROKER_TEXT_BYTES) -> str:
        if not isinstance(value, str):
            raise RuntimeError("model broker returned invalid text")
        if len(value) > maximum or len(value.encode("utf-8")) > maximum:
            raise RuntimeError("model broker text exceeded its limit")
        return value

    def _send_request(self, request_id: str, tool_results: tuple[str, ...]) -> None:
        if len(tool_results) > 3:
            raise RuntimeError("model broker tool-result limit exceeded")
        for result in tool_results:
            self._bounded_text(result, 2_048)
        payload = json.dumps(
            {
                "internal": "model.request",
                "version": BROKER_PROTOCOL_VERSION,
                "request_id": request_id,
                "iteration": self._iteration,
                "tool_results": list(tool_results),
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
        sections: tuple[PromptSection, ...],
        tool_results: tuple[str, ...],
        is_cancelled: Callable[[], bool],
    ) -> Iterator[ModelBlock]:
        # The trusted broker already owns the original validated user message.
        # Sections are deliberately not allowed to select a model or endpoint.
        if not any(section.section_id == "turn.user" for section in sections):
            raise RuntimeError("model context has no user turn")
        self._iteration += 1
        request_id = f"model-{self._iteration}"
        self._send_request(request_id, tool_results)
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
                arguments = frame.get("arguments")
                if (
                    not isinstance(call_id, str)
                    or _BROKER_ID.fullmatch(call_id) is None
                    or frame.get("tool_id") != "builtin/echo"
                    or not isinstance(arguments, dict)
                    or set(arguments) != {"text"}
                ):
                    raise RuntimeError("model broker tool call is invalid")
                self._bounded_text(arguments.get("text"), 2_048)
                if block_open:
                    yield ModelBlock("text.finish", {"block_id": block_id})
                yield ModelBlock(
                    "tool.use",
                    {
                        "call_id": call_id,
                        "tool_id": "builtin/echo",
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
                } or frame.get("reason") != "end_turn":
                    raise RuntimeError("model broker stop frame is invalid")
                if block_open:
                    yield ModelBlock("text.finish", {"block_id": block_id})
                yield ModelBlock("stop", {"reason": "end_turn"})
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

"""Vendor-neutral observability contracts used by the agent runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Tuple


SpanHandle = Tuple[str, str]


def _exception_metadata(exc: BaseException) -> Dict[str, str]:
    """Describe a failure without copying provider or user data into telemetry."""
    return {"type": type(exc).__name__, "message": "operation failed"}


class Tracer(Protocol):
    """Small tracing surface consumed by the application.

    The protocol intentionally does not expose SDK-specific span objects.  The
    first element returned by :meth:`create_span` is an opaque handle used to
    end the span; the second is an observation id that can be used as a parent.
    """

    enabled: bool
    backend: str
    reason: Optional[str]

    def create_trace(
        self,
        trace_id: str,
        name: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        input: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]: ...

    def end_trace(
        self,
        trace_id: str,
        output: Optional[Any] = None,
        status: Optional[str] = "success",
        error: Optional[Any] = None,
    ) -> None: ...

    def create_span(
        self,
        trace_id: str,
        span_name: str,
        parent_observation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        input: Optional[Any] = None,
        span_type: Optional[str] = "DEFAULT",
    ) -> Optional[SpanHandle]: ...

    def end_span(
        self,
        trace_id: str,
        span_id: str,
        output: Optional[Any] = None,
        status: Optional[str] = "success",
        error: Optional[Any] = None,
        usage: Optional[Dict[str, int]] = None,
        level: Optional[str] = None,
    ) -> None: ...

    def force_flush(self, timeout_millis: int = 10_000) -> bool: ...

    async def shutdown(self) -> None: ...


@dataclass
class LLMCallContext:
    """Mutable result container returned by ``trace_llm_call``."""

    output: Optional[Any] = None
    usage: Optional[Dict[str, int]] = None

    def update(
        self,
        output: Optional[Any] = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> None:
        if output is not None:
            self.output = output
        if usage is not None:
            self.usage = usage


@dataclass
class ToolCallContext:
    """Mutable result container returned by ``trace_tool_call``."""

    result: Optional[Any] = None
    error: Optional[str] = None

    def update(
        self,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> None:
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error


class TracerContextManagers:
    """Reusable context-manager helpers for concrete tracer implementations."""

    @asynccontextmanager
    async def trace_llm_call(
        self: Tracer,
        trace_id: str,
        model: str,
        provider: str,
        prompt: str,
        parent_observation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMCallContext]:
        handle = self.create_span(
            trace_id=trace_id,
            span_name=f"llm.{provider}.{model}",
            parent_observation_id=parent_observation_id,
            span_type="LLM",
            input={"model": model, "provider": provider, "prompt": prompt},
            metadata=metadata,
        )
        span_id = handle[0] if handle else ""
        context = LLMCallContext()
        try:
            yield context
        except BaseException as exc:
            if span_id:
                self.end_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    output=context.output,
                    status="error",
                    error=_exception_metadata(exc),
                    usage=context.usage,
                )
            raise
        else:
            if span_id:
                self.end_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    output=context.output,
                    usage=context.usage,
                )

    @asynccontextmanager
    async def trace_tool_call(
        self: Tracer,
        trace_id: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_type: str = "mcp",
        parent_observation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[ToolCallContext]:
        handle = self.create_span(
            trace_id=trace_id,
            span_name=f"tool.{tool_type}.{tool_name}",
            parent_observation_id=parent_observation_id,
            span_type="TOOL",
            input={"tool": tool_name, "args": tool_args},
            metadata=metadata,
        )
        span_id = handle[0] if handle else ""
        context = ToolCallContext()
        try:
            yield context
        except BaseException as exc:
            if span_id:
                self.end_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    output={"result": context.result},
                    status="error",
                    error=_exception_metadata(exc),
                )
            raise
        else:
            if span_id:
                self.end_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    output={"result": context.result},
                    status="error" if context.error else "success",
                    error={"type": "ToolCallError", "message": "tool call failed"}
                    if context.error
                    else None,
                )

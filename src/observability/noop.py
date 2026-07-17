"""Explicit disabled tracer used for opt-out and dependency fallback."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import SpanHandle, TracerContextManagers


class NoopTracer(TracerContextManagers):
    enabled = False
    backend = "noop"

    def __init__(self, reason: str = "observability disabled"):
        self.reason = reason

    def create_trace(
        self,
        trace_id: str,
        name: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        input: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        return None

    def end_trace(
        self,
        trace_id: str,
        output: Optional[Any] = None,
        status: Optional[str] = "success",
        error: Optional[Any] = None,
    ) -> None:
        return None

    def create_span(
        self,
        trace_id: str,
        span_name: str,
        parent_observation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        input: Optional[Any] = None,
        span_type: Optional[str] = "DEFAULT",
    ) -> Optional[SpanHandle]:
        return None

    def end_span(
        self,
        trace_id: str,
        span_id: str,
        output: Optional[Any] = None,
        status: Optional[str] = "success",
        error: Optional[Any] = None,
        usage: Optional[Dict[str, int]] = None,
        level: Optional[str] = None,
    ) -> None:
        return None

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        return True

    async def shutdown(self) -> None:
        return None

"""OpenTelemetry/OpenInference tracer with OTLP/HTTP export.

The module has no hard import-time dependency on OpenTelemetry.  Deployments
without the optional packages can still import the application; the factory
will select an explicit :class:`~src.observability.noop.NoopTracer` instead.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
import warnings
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

from .base import SpanHandle, TracerContextManagers
from .pricing import PricingCatalog
from .redaction import redact_text, serialize_attribute, truncate_text
from .sampling import TailSamplingPolicy


try:  # All telemetry dependencies are intentionally optional.
    from opentelemetry.context import Context
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )
    from opentelemetry.trace import SpanKind, Status, StatusCode, set_span_in_context

    OTEL_AVAILABLE = True
    OTEL_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover - the no-dependency path is tested via factory
    Context = Any  # type: ignore[assignment,misc]
    OTLPSpanExporter = Any  # type: ignore[assignment,misc]
    Resource = Any  # type: ignore[assignment,misc]
    TracerProvider = Any  # type: ignore[assignment,misc]
    SpanProcessor = object  # type: ignore[assignment,misc]
    BatchSpanProcessor = Any  # type: ignore[assignment,misc]
    SpanExporter = object  # type: ignore[assignment,misc]
    SpanExportResult = Any  # type: ignore[assignment,misc]
    SpanKind = Any  # type: ignore[assignment,misc]
    Status = Any  # type: ignore[assignment,misc]
    StatusCode = Any  # type: ignore[assignment,misc]
    set_span_in_context = None  # type: ignore[assignment]
    OTEL_AVAILABLE = False
    OTEL_IMPORT_ERROR = exc


OPENINFERENCE_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
INPUT_MIME_TYPE = "input.mime_type"
OUTPUT_VALUE = "output.value"
OUTPUT_MIME_TYPE = "output.mime_type"
SESSION_ID = "session.id"
USER_ID = "user.id"
METADATA = "metadata"
TAG_TAGS = "tag.tags"

_SPAN_KIND_MAP = {
    "DEFAULT": "CHAIN",
    "CHAIN": "CHAIN",
    "LLM": "LLM",
    "TOOL": "TOOL",
    "AGENT": "AGENT",
    "RETRIEVER": "RETRIEVER",
    "RAG": "RETRIEVER",
    "EMBEDDING": "EMBEDDING",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(float(os.environ.get(name, str(default))), minimum)
    except ValueError:
        return default


def _otlp_endpoint() -> str:
    traces_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if traces_endpoint:
        return traces_endpoint
    base_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if base_endpoint:
        return f"{base_endpoint.rstrip('/')}/v1/traces"
    return "http://127.0.0.1:6006/v1/traces"


def _validated_otlp_endpoint() -> str:
    endpoint = _otlp_endpoint()
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("OTLP endpoint must be an HTTP(S) URL without credentials")
    return endpoint


def _otlp_headers() -> Dict[str, str]:
    raw = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
    )
    headers: Dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip():
            headers[unquote(key.strip())] = unquote(value.strip())
    return headers


@dataclass(frozen=True)
class ObservabilityConfig:
    endpoint: str
    service_name: str = "agent-builder"
    service_version: str = "dev"
    environment: str = "local"
    success_sample_ratio: float = 0.2
    slow_request_ms: float = 5_000.0
    keep_errors: bool = True
    keep_slow: bool = True
    max_attribute_length: int = 4_096
    max_collection_items: int = 50
    max_attribute_depth: int = 8
    batch_schedule_delay_millis: int = 2_000
    batch_max_queue_size: int = 2_048
    batch_max_export_batch_size: int = 256
    export_timeout_millis: int = 10_000
    max_pending_traces: int = 2_048
    max_spans_per_trace: int = 512
    max_trace_bytes: int = 1_048_576
    priority_queue_traces: int = 64
    priority_batch_delay_millis: int = 100
    headers: Optional[Mapping[str, str]] = None

    @classmethod
    def from_env(cls) -> "ObservabilityConfig":
        sample_ratio = min(
            _env_float("OBSERVABILITY_SUCCESS_SAMPLE_RATE", 0.2),
            1.0,
        )
        queue_size = _env_int("OBSERVABILITY_BATCH_QUEUE_SIZE", 2_048)
        export_batch_size = min(
            _env_int("OBSERVABILITY_BATCH_SIZE", 256),
            queue_size,
        )
        return cls(
            endpoint=_validated_otlp_endpoint(),
            service_name=os.environ.get("OTEL_SERVICE_NAME", "agent-builder"),
            service_version=os.environ.get("APP_VERSION", "dev"),
            environment=os.environ.get("APP_ENV", "local"),
            success_sample_ratio=sample_ratio,
            slow_request_ms=_env_float("OBSERVABILITY_SLOW_REQUEST_MS", 5_000.0),
            keep_errors=_env_bool("OBSERVABILITY_KEEP_ERRORS", True),
            keep_slow=_env_bool("OBSERVABILITY_KEEP_SLOW", True),
            max_attribute_length=_env_int("OBSERVABILITY_MAX_ATTRIBUTE_LENGTH", 4_096),
            max_collection_items=_env_int("OBSERVABILITY_MAX_COLLECTION_ITEMS", 50),
            max_attribute_depth=_env_int("OBSERVABILITY_MAX_ATTRIBUTE_DEPTH", 8),
            batch_schedule_delay_millis=_env_int(
                "OBSERVABILITY_BATCH_DELAY_MS", 2_000
            ),
            batch_max_queue_size=queue_size,
            batch_max_export_batch_size=export_batch_size,
            export_timeout_millis=_env_int("OBSERVABILITY_EXPORT_TIMEOUT_MS", 10_000),
            max_pending_traces=_env_int("OBSERVABILITY_MAX_PENDING_TRACES", 2_048),
            max_spans_per_trace=_env_int("OBSERVABILITY_MAX_SPANS_PER_TRACE", 512),
            max_trace_bytes=_env_int("OBSERVABILITY_MAX_TRACE_BYTES", 1_048_576),
            priority_queue_traces=_env_int(
                "OBSERVABILITY_PRIORITY_QUEUE_TRACES", 64
            ),
            priority_batch_delay_millis=_env_int(
                "OBSERVABILITY_PRIORITY_BATCH_DELAY_MS", 100
            ),
            headers=_otlp_headers(),
        )


if OTEL_AVAILABLE:

    class SynchronizedSpanExporter(SpanExporter):
        """Serialize shared exporter access from priority and batch paths."""

        def __init__(self, delegate: SpanExporter):
            self._delegate = delegate
            self._lock = threading.RLock()
            self._shutdown = False

        def export(self, spans: Sequence[Any]) -> SpanExportResult:
            with self._lock:
                if self._shutdown:
                    return SpanExportResult.FAILURE
                return self._delegate.export(spans)

        def force_flush(self, timeout_millis: int = 10_000) -> bool:
            with self._lock:
                flush = getattr(self._delegate, "force_flush", None)
                if not callable(flush):
                    return True
                try:
                    return flush(timeout_millis=timeout_millis) is not False
                except TypeError:
                    return flush(timeout_millis) is not False

        def shutdown(self) -> None:
            with self._lock:
                if self._shutdown:
                    return
                self._delegate.shutdown()
                self._shutdown = True


    class QueuedPrioritySpanExporter(SpanExporter):
        """Bounded background exporter for error, slow, and overflow traces.

        Producers never wait for collector I/O or queue capacity. Saturated
        critical traces fail fast and increment an explicit counter. The
        worker coalesces nearby traces into one OTLP call, keeping network I/O
        off the application event loop.
        """

        def __init__(
            self,
            delegate: SpanExporter,
            *,
            max_queue_traces: int,
            max_batch_spans: int,
            batch_delay_millis: int,
            enqueue_timeout_millis: int,
        ):
            self._delegate = delegate
            self._queue: "queue.Queue[object]" = queue.Queue(
                maxsize=max(1, max_queue_traces)
            )
            self._max_batch_spans = max(1, max_batch_spans)
            self._batch_delay_seconds = max(batch_delay_millis, 1) / 1_000
            self._shutdown_wait_seconds = max(enqueue_timeout_millis, 1) / 1_000
            self._condition = threading.Condition()
            self._pending_items = 0
            self._accepting = True
            self._stop_requested = threading.Event()
            self.export_failure_count = 0
            self.enqueue_failure_count = 0
            self._worker = threading.Thread(
                target=self._run,
                name="otel-priority-export",
                daemon=True,
            )
            self._worker.start()

        def export(self, spans: Sequence[Any]) -> SpanExportResult:
            if not spans:
                return SpanExportResult.SUCCESS
            item = tuple(spans)
            with self._condition:
                if not self._accepting:
                    return SpanExportResult.FAILURE
                self._pending_items += 1
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                with self._condition:
                    self._pending_items -= 1
                    self.enqueue_failure_count += 1
                    self._condition.notify_all()
                return SpanExportResult.FAILURE
            return SpanExportResult.SUCCESS

        def _complete(self, item_count: int, failed: bool) -> None:
            with self._condition:
                self._pending_items -= item_count
                if failed:
                    self.export_failure_count += item_count
                self._condition.notify_all()

        def _run(self) -> None:
            while True:
                try:
                    item = self._queue.get(timeout=self._batch_delay_seconds)
                except queue.Empty:
                    if self._stop_requested.is_set():
                        break
                    continue
                items = [item]
                spans = list(item)  # type: ignore[arg-type]
                deadline = time.monotonic() + self._batch_delay_seconds
                while len(spans) < self._max_batch_spans:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        following = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    items.append(following)
                    spans.extend(following)  # type: ignore[arg-type]
                failed = False
                try:
                    failed = self._delegate.export(spans) is SpanExportResult.FAILURE
                except Exception:
                    failed = True
                self._complete(len(items), failed)

        def force_flush(self, timeout_millis: int = 10_000) -> bool:
            deadline = time.monotonic() + max(timeout_millis, 1) / 1_000
            with self._condition:
                while self._pending_items:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(timeout=remaining)
            return True

        def shutdown(self) -> None:
            with self._condition:
                if not self._accepting:
                    return
                self._accepting = False
            self._stop_requested.set()
            self.force_flush(timeout_millis=int(self._shutdown_wait_seconds * 1_000))
            self._worker.join(timeout=self._shutdown_wait_seconds + 1)

    @dataclass
    class _PendingTrace:
        spans: List[Any]
        estimated_bytes: int = 0
        has_error: bool = False
        max_duration_ms: float = 0.0
        dropped_spans: int = 0


    class TailSamplingSpanProcessor(SpanProcessor):
        """Tail-sample before batching, with bounded per-trace memory.

        Error and slow traces bypass the normal success queue so a saturated
        batch queue cannot discard them before the tail decision. Successful
        sampled traces continue through OpenTelemetry's bounded batch
        processor. Oversized or excess pending traces fail-keep and are sent
        through the priority path.
        """

        _DROP = 0
        _BATCH = 1
        _PRIORITY = 2

        def __init__(
            self,
            batch_processor: SpanProcessor,
            priority_exporter: SpanExporter,
            policy: TailSamplingPolicy,
            max_pending_traces: int = 2_048,
            max_spans_per_trace: int = 512,
            max_trace_bytes: int = 1_048_576,
            max_decisions: int = 10_000,
        ):
            self._batch_processor = batch_processor
            self._priority_exporter = priority_exporter
            self._policy = policy
            self._max_pending_traces = max_pending_traces
            self._max_spans_per_trace = max_spans_per_trace
            self._max_trace_bytes = max_trace_bytes
            self._max_decisions = max_decisions
            self._pending: "OrderedDict[str, _PendingTrace]" = OrderedDict()
            self._decisions: "OrderedDict[str, int]" = OrderedDict()
            self._lock = threading.Lock()
            self._shutdown = False
            self.overflow_trace_count = 0
            self.truncated_span_count = 0
            self.priority_trace_count = 0
            self.priority_export_failure_count = 0

        @staticmethod
        def _trace_key(span: Any) -> str:
            return f"{span.context.trace_id:032x}"

        @staticmethod
        def _is_root(span: Any) -> bool:
            attributes = span.attributes or {}
            return span.parent is None or bool(attributes.get("agent_builder.trace.root"))

        @staticmethod
        def _span_signal(span: Any) -> Tuple[bool, float]:
            status = getattr(span, "status", None)
            attributes = span.attributes or {}
            has_error = bool(
                (status and status.status_code is StatusCode.ERROR)
                or attributes.get("agent_builder.error")
            )
            duration_ms = 0.0
            start_ns = getattr(span, "start_time", None)
            end_ns = getattr(span, "end_time", None)
            if start_ns is not None and end_ns is not None:
                duration_ms = max((end_ns - start_ns) / 1_000_000, 0.0)
            return has_error, duration_ms

        @staticmethod
        def _estimated_span_bytes(span: Any) -> int:
            """Cheap bounded estimate; never stringify an unbounded payload."""

            total = 256 + min(len(str(getattr(span, "name", ""))), 512)
            attributes = getattr(span, "attributes", None) or {}
            for index, (key, value) in enumerate(attributes.items()):
                if index >= 256:
                    break
                total += min(len(str(key)), 256)
                if isinstance(value, str):
                    total += min(len(value), 8_192)
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    total += min(len(value), 8_192)
                elif isinstance(value, Sequence):
                    total += min(len(value) * 64, 8_192)
                else:
                    total += 64
            return total

        def _remember_decision(self, trace_key: str, decision: int) -> None:
            self._decisions[trace_key] = decision
            self._decisions.move_to_end(trace_key)
            while len(self._decisions) > self._max_decisions:
                self._decisions.popitem(last=False)

        def _decide(self, trace_key: str, trace: _PendingTrace) -> int:
            critical = (
                (self._policy.keep_errors and trace.has_error)
                or (
                    self._policy.keep_slow
                    and trace.max_duration_ms >= self._policy.slow_request_ms
                )
            )
            if critical or trace.dropped_spans:
                return self._PRIORITY
            keep = self._policy.should_keep(
                trace_key,
                has_error=trace.has_error,
                max_duration_ms=trace.max_duration_ms,
            )
            return self._BATCH if keep else self._DROP

        def _evict_overflow_locked(self) -> List[Tuple[int, List[Any]]]:
            ready: List[Tuple[int, List[Any]]] = []
            while len(self._pending) > self._max_pending_traces:
                trace_key, trace = self._pending.popitem(last=False)
                self.overflow_trace_count += 1
                self._remember_decision(trace_key, self._PRIORITY)
                ready.append((self._PRIORITY, trace.spans))
            return ready

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            del span, parent_context

        def _forward(self, decision: int, spans: Sequence[Any]) -> None:
            if not spans or decision == self._DROP:
                return
            if decision == self._BATCH:
                for span in spans:
                    self._batch_processor.on_end(span)
                return
            self.priority_trace_count += 1
            try:
                result = self._priority_exporter.export(spans)
                if result is SpanExportResult.FAILURE:
                    self.priority_export_failure_count += 1
            except Exception:
                self.priority_export_failure_count += 1

        def on_end(self, span: Any) -> None:
            if self._shutdown:
                return

            ready: List[Tuple[int, List[Any]]] = []
            with self._lock:
                trace_key = self._trace_key(span)
                prior_decision = self._decisions.get(trace_key)
                if prior_decision is not None:
                    ready.append((prior_decision, [span]))
                else:
                    trace = self._pending.setdefault(trace_key, _PendingTrace([]))
                    has_error, duration_ms = self._span_signal(span)
                    trace.has_error = trace.has_error or has_error
                    trace.max_duration_ms = max(trace.max_duration_ms, duration_ms)
                    estimated_bytes = self._estimated_span_bytes(span)
                    within_limit = (
                        len(trace.spans) < self._max_spans_per_trace
                        and trace.estimated_bytes + estimated_bytes <= self._max_trace_bytes
                    )
                    if within_limit:
                        trace.spans.append(span)
                        trace.estimated_bytes += estimated_bytes
                    else:
                        trace.dropped_spans += 1
                        self.truncated_span_count += 1
                        # Preserve the root so exported partial traces remain useful.
                        if self._is_root(span):
                            if trace.spans:
                                trace.spans[-1] = span
                            else:
                                trace.spans.append(span)
                    self._pending.move_to_end(trace_key)
                    if self._is_root(span):
                        decision = self._decide(trace_key, trace)
                        self._remember_decision(trace_key, decision)
                        self._pending.pop(trace_key, None)
                        ready.append((decision, trace.spans))
                ready.extend(self._evict_overflow_locked())

            for decision, spans in ready:
                self._forward(decision, spans)

        def force_flush(self, timeout_millis: int = 10_000) -> bool:
            ready: List[Tuple[int, List[Any]]] = []
            with self._lock:
                for trace_key, trace in self._pending.items():
                    decision = self._decide(trace_key, trace)
                    self._remember_decision(trace_key, decision)
                    ready.append((decision, trace.spans))
                self._pending.clear()

            for decision, spans in ready:
                self._forward(decision, spans)
            batch_result = self._batch_processor.force_flush(timeout_millis)
            exporter_flush = getattr(self._priority_exporter, "force_flush", None)
            exporter_result = exporter_flush(timeout_millis) if callable(exporter_flush) else True
            return batch_result is not False and exporter_result is not False

        def shutdown(self) -> None:
            if self._shutdown:
                return
            self.force_flush()
            self._shutdown = True
            self._priority_exporter.shutdown()
            self._batch_processor.shutdown()

        def stats(self) -> Dict[str, int]:
            with self._lock:
                pending = len(self._pending)
            stats = {
                "pending_traces": pending,
                "overflow_traces": self.overflow_trace_count,
                "truncated_spans": self.truncated_span_count,
                "priority_traces": self.priority_trace_count,
                "priority_export_failures": self.priority_export_failure_count,
            }
            stats["priority_queue_failures"] = int(
                getattr(self._priority_exporter, "enqueue_failure_count", 0)
            )
            stats["priority_delegate_failures"] = int(
                getattr(self._priority_exporter, "export_failure_count", 0)
            )
            return stats

else:

    class TailSamplingSpanProcessor:  # pragma: no cover - construction is guarded by factory
        def __init__(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("OpenTelemetry dependencies unavailable")


@dataclass
class _SpanRecord:
    span: Any
    external_trace_id: str
    observation_id: str
    started_monotonic_ns: int
    model: Optional[str] = None


class OpenTelemetryTracer(TracerContextManagers):
    """OpenTelemetry tracer exporting OpenInference attributes over OTLP/HTTP."""

    enabled = True
    backend = "otlp"
    reason: Optional[str] = None

    def __init__(
        self,
        config: Optional[ObservabilityConfig] = None,
        *,
        exporter: Optional[Any] = None,
    ):
        if not OTEL_AVAILABLE:
            raise RuntimeError("OpenTelemetry dependencies unavailable")

        self.config = config or ObservabilityConfig.from_env()
        try:
            self._pricing = PricingCatalog.from_env()
        except ValueError as exc:
            warnings.warn(
                f"LLM cost estimation disabled: invalid configuration ({type(exc).__name__})",
                RuntimeWarning,
                stacklevel=2,
            )
            self._pricing = PricingCatalog()
        self._lock = threading.RLock()
        self._traces: Dict[str, _SpanRecord] = {}
        self._spans: Dict[str, _SpanRecord] = {}
        self._observations: Dict[str, _SpanRecord] = {}
        self._trace_observations: Dict[str, set[str]] = defaultdict(set)
        self._shutdown = False

        resource = Resource.create(
            {
                "service.name": self.config.service_name,
                "service.version": self.config.service_version,
                "deployment.environment.name": self.config.environment,
                "telemetry.sdk.language": "python",
            }
        )
        self._provider = TracerProvider(resource=resource)
        delegate = exporter or OTLPSpanExporter(
            endpoint=self.config.endpoint,
            headers=dict(self.config.headers or {}),
            timeout=self.config.export_timeout_millis / 1_000,
        )
        synchronized_exporter = SynchronizedSpanExporter(delegate)
        priority_exporter = QueuedPrioritySpanExporter(
            synchronized_exporter,
            max_queue_traces=self.config.priority_queue_traces,
            max_batch_spans=self.config.batch_max_export_batch_size,
            batch_delay_millis=self.config.priority_batch_delay_millis,
            enqueue_timeout_millis=self.config.export_timeout_millis,
        )
        policy = TailSamplingPolicy(
            success_ratio=self.config.success_sample_ratio,
            slow_request_ms=self.config.slow_request_ms,
            keep_errors=self.config.keep_errors,
            keep_slow=self.config.keep_slow,
        )
        self._processor = BatchSpanProcessor(
            synchronized_exporter,
            schedule_delay_millis=self.config.batch_schedule_delay_millis,
            max_queue_size=self.config.batch_max_queue_size,
            max_export_batch_size=self.config.batch_max_export_batch_size,
            export_timeout_millis=self.config.export_timeout_millis,
        )
        self._sampling_processor = TailSamplingSpanProcessor(
            self._processor,
            priority_exporter,
            policy,
            max_pending_traces=self.config.max_pending_traces,
            max_spans_per_trace=self.config.max_spans_per_trace,
            max_trace_bytes=self.config.max_trace_bytes,
        )
        self._provider.add_span_processor(self._sampling_processor)
        self._tracer = self._provider.get_tracer(
            "agent-builder.observability",
            self.config.service_version,
        )

    @staticmethod
    def _span_id(span: Any) -> str:
        return f"{span.get_span_context().span_id:016x}"

    def _serialized(self, value: Any) -> str:
        return serialize_attribute(
            value,
            max_length=self.config.max_attribute_length,
            max_items=self.config.max_collection_items,
            max_depth=self.config.max_attribute_depth,
        )

    @staticmethod
    def _set_attribute(span: Any, key: str, value: Any) -> None:
        if value is None:
            return
        try:
            span.set_attribute(key, value)
        except Exception:
            # Observability must not break an agent request because a third-party
            # SDK rejects an attribute value.
            return

    def _set_payload(self, span: Any, key: str, mime_key: str, value: Any) -> None:
        if value is None:
            return
        self._set_attribute(span, key, self._serialized(value))
        self._set_attribute(span, mime_key, "application/json")

    def _set_common_attributes(
        self,
        span: Any,
        *,
        external_trace_id: str,
        span_type: str,
        metadata: Optional[Dict[str, Any]],
        tags: Optional[List[str]],
        input_value: Any,
    ) -> None:
        kind = _SPAN_KIND_MAP.get(span_type.upper(), "CHAIN")
        self._set_attribute(span, OPENINFERENCE_KIND, kind)
        self._set_attribute(span, "agent_builder.trace.external_id", external_trace_id)
        self._set_attribute(span, "agent_builder.payload.policy", "redacted-truncated")
        self._set_payload(span, INPUT_VALUE, INPUT_MIME_TYPE, input_value)
        if metadata:
            self._set_attribute(span, METADATA, self._serialized(metadata))
        if tags:
            self._set_attribute(
                span,
                TAG_TAGS,
                [truncate_text(redact_text(str(tag)), 128) for tag in tags[:20]],
            )

        if isinstance(input_value, Mapping):
            model = input_value.get("model")
            provider = input_value.get("provider")
            tool = input_value.get("tool")
            if kind == "LLM":
                self._set_attribute(span, "llm.model_name", str(model) if model else None)
                self._set_attribute(span, "llm.provider", str(provider) if provider else None)
                self._set_attribute(span, "gen_ai.request.model", str(model) if model else None)
            if kind == "TOOL":
                self._set_attribute(span, "tool.name", str(tool) if tool else None)
                if "args" in input_value:
                    self._set_attribute(span, "tool.parameters", self._serialized(input_value["args"]))

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
        if self._shutdown:
            return None
        with self._lock:
            existing = self._traces.get(trace_id)
            if existing is not None:
                return {
                    "id": trace_id,
                    "observation_id": existing.observation_id,
                    "telemetry_trace_id": f"{existing.span.get_span_context().trace_id:032x}",
                    "name": name,
                    "user_id": user_id,
                    "session_id": session_id,
                }

        span = self._tracer.start_span(name, context=Context(), kind=SpanKind.INTERNAL)
        observation_id = self._span_id(span)
        self._set_common_attributes(
            span,
            external_trace_id=trace_id,
            span_type="AGENT",
            metadata=metadata,
            tags=tags,
            input_value=input,
        )
        self._set_attribute(span, "agent_builder.trace.root", True)
        self._set_attribute(span, USER_ID, user_id)
        self._set_attribute(span, SESSION_ID, session_id)
        record = _SpanRecord(
            span=span,
            external_trace_id=trace_id,
            observation_id=observation_id,
            started_monotonic_ns=time.monotonic_ns(),
        )
        with self._lock:
            self._traces[trace_id] = record
            self._observations[observation_id] = record
            self._trace_observations[trace_id].add(observation_id)
        return {
            "id": trace_id,
            "observation_id": observation_id,
            "telemetry_trace_id": f"{span.get_span_context().trace_id:032x}",
            "name": name,
            "user_id": user_id,
            "session_id": session_id,
        }

    def _mark_result(
        self,
        record: _SpanRecord,
        *,
        output: Optional[Any],
        status: Optional[str],
        error: Optional[Any],
        level: Optional[str] = None,
    ) -> None:
        span = record.span
        self._set_payload(span, OUTPUT_VALUE, OUTPUT_MIME_TYPE, output)
        duration_ms = (time.monotonic_ns() - record.started_monotonic_ns) / 1_000_000
        self._set_attribute(span, "agent_builder.duration_ms", duration_ms)
        normalized_status = (status or "success").lower()
        is_error = bool(error) or normalized_status in {"error", "failed", "failure"}
        is_error = is_error or (level or "").upper() == "ERROR"
        self._set_attribute(span, "agent_builder.error", is_error)
        if is_error:
            error_type = "Error"
            if isinstance(error, Mapping):
                candidate = error.get("type")
                if isinstance(candidate, str) and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", candidate
                ):
                    error_type = candidate
            error_payload = {
                "type": error_type,
                "message": "operation failed",
            }
            error_text = self._serialized(error_payload)
            self._set_attribute(span, "error.message", error_text)
            span.set_status(Status(StatusCode.ERROR, truncate_text(error_text, 512)))
            span.add_event(
                "exception",
                attributes={
                    "exception.type": error_type,
                    "exception.message": truncate_text(error_text, 512),
                },
            )
        else:
            span.set_status(Status(StatusCode.OK))

    def end_trace(
        self,
        trace_id: str,
        output: Optional[Any] = None,
        status: Optional[str] = "success",
        error: Optional[Any] = None,
    ) -> None:
        with self._lock:
            record = self._traces.get(trace_id)
            orphan_span_ids = [
                span_id
                for span_id, span_record in self._spans.items()
                if span_record.external_trace_id == trace_id
            ]
        if record is None:
            return

        for span_id in orphan_span_ids:
            self.end_span(
                trace_id=trace_id,
                span_id=span_id,
                status="error",
                error={"type": "UnclosedSpan", "message": "trace ended before span"},
            )

        try:
            self._mark_result(record, output=output, status=status, error=error)
            record.span.end()
        finally:
            with self._lock:
                self._traces.pop(trace_id, None)
                for observation_id in self._trace_observations.pop(trace_id, set()):
                    self._observations.pop(observation_id, None)

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
        if self._shutdown:
            return None
        with self._lock:
            root = self._traces.get(trace_id)
            parent = self._observations.get(parent_observation_id or "") or root
        if root is None or parent is None:
            return None

        parent_context = set_span_in_context(parent.span)
        span = self._tracer.start_span(
            span_name,
            context=parent_context,
            kind=SpanKind.INTERNAL,
        )
        span_id = uuid.uuid4().hex
        observation_id = self._span_id(span)
        normalized_type = (span_type or "DEFAULT").upper()
        self._set_common_attributes(
            span,
            external_trace_id=trace_id,
            span_type=normalized_type,
            metadata=metadata,
            tags=tags,
            input_value=input,
        )
        model = None
        if normalized_type == "LLM":
            if isinstance(input, Mapping) and input.get("model"):
                model = str(input["model"])
            elif span_name.startswith("llm."):
                model = span_name.split(".", 1)[1]
            self._set_attribute(span, "llm.model_name", model)
            self._set_attribute(span, "gen_ai.request.model", model)

        record = _SpanRecord(
            span=span,
            external_trace_id=trace_id,
            observation_id=observation_id,
            started_monotonic_ns=time.monotonic_ns(),
            model=model,
        )
        with self._lock:
            self._spans[span_id] = record
            # Keep ended observation contexts until the root trace ends.  Tool
            # spans are often created after their LLM parent has completed.
            self._observations[observation_id] = record
            self._trace_observations[trace_id].add(observation_id)
        return span_id, observation_id

    @staticmethod
    def _usage_value(usage: Mapping[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return max(int(value), 0)
                except (TypeError, ValueError):
                    continue
        return 0

    def _set_usage(self, record: _SpanRecord, usage: Optional[Mapping[str, Any]]) -> None:
        if not usage:
            return
        input_tokens = self._usage_value(usage, "input", "input_tokens", "prompt_tokens")
        output_tokens = self._usage_value(
            usage, "output", "output_tokens", "completion_tokens"
        )
        total_tokens = self._usage_value(usage, "total", "total_tokens")
        if not total_tokens:
            total_tokens = input_tokens + output_tokens
        attributes = {
            "llm.token_count.prompt": input_tokens,
            "llm.token_count.completion": output_tokens,
            "llm.token_count.total": total_tokens,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
        }
        for key, value in attributes.items():
            self._set_attribute(record.span, key, value)

        costs = self._pricing.estimate(record.model, input_tokens, output_tokens)
        if costs:
            self._set_attribute(record.span, "llm.cost.prompt", costs["input"])
            self._set_attribute(record.span, "llm.cost.completion", costs["output"])
            self._set_attribute(record.span, "llm.cost.total", costs["total"])

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
        if not span_id:
            return
        with self._lock:
            record = self._spans.get(span_id)
        if record is None or record.external_trace_id != trace_id:
            return
        try:
            self._set_usage(record, usage)
            self._mark_result(
                record,
                output=output,
                status=status,
                error=error,
                level=level,
            )
            record.span.end()
        finally:
            with self._lock:
                self._spans.pop(span_id, None)

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        if self._shutdown:
            return True
        try:
            return self._provider.force_flush(timeout_millis=timeout_millis) is not False
        except Exception:
            return False

    def stats(self) -> Dict[str, int]:
        return self._sampling_processor.stats()

    async def shutdown(self) -> None:
        if self._shutdown:
            return
        with self._lock:
            trace_ids = list(self._traces)
        for trace_id in trace_ids:
            self.end_trace(
                trace_id,
                status="error",
                error={"type": "Shutdown", "message": "application shutdown"},
            )
        self.force_flush()
        try:
            self._provider.shutdown()
        except Exception as exc:
            warnings.warn(
                f"OpenTelemetry shutdown failed and may be retried: {type(exc).__name__}",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        self._shutdown = True

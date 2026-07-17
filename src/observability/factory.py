"""Singleton construction and status reporting for observability."""

from __future__ import annotations

import os
import threading
import warnings
from typing import Dict, Optional

from .base import Tracer
from .noop import NoopTracer
from .otel_tracer import (
    OTEL_AVAILABLE,
    OTEL_IMPORT_ERROR,
    ObservabilityConfig,
    OpenTelemetryTracer,
)


_instance: Optional[Tracer] = None
_lock = threading.Lock()


def _enabled_from_env() -> bool:
    return os.environ.get("OBSERVABILITY_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _build_tracer() -> Tracer:
    if not _enabled_from_env():
        return NoopTracer("disabled by OBSERVABILITY_ENABLED")

    backend = os.environ.get("OBSERVABILITY_BACKEND", "otlp").strip().lower()
    if backend in {"none", "noop", "disabled"}:
        return NoopTracer(f"disabled by OBSERVABILITY_BACKEND={backend}")
    if backend not in {"otel", "otlp"}:
        message = f"unsupported observability backend: {backend}"
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        return NoopTracer(message)
    if not OTEL_AVAILABLE:
        error_type = (
            type(OTEL_IMPORT_ERROR).__name__
            if OTEL_IMPORT_ERROR is not None
            else "ImportError"
        )
        message = f"OpenTelemetry dependencies unavailable ({error_type})"
        warnings.warn(f"Observability disabled: {message}", RuntimeWarning, stacklevel=2)
        return NoopTracer(message)

    try:
        return OpenTelemetryTracer(ObservabilityConfig.from_env())
    except Exception as exc:
        message = f"OpenTelemetry initialization failed ({type(exc).__name__})"
        warnings.warn(f"Observability disabled: {message}", RuntimeWarning, stacklevel=2)
        return NoopTracer(message)


def get_tracer() -> Tracer:
    """Return the process-wide tracer, constructing it once on first use."""

    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = _build_tracer()
    return _instance


def is_observability_enabled() -> bool:
    """Report actual SDK availability/configuration, not merely user intent."""

    return bool(get_tracer().enabled)


def get_observability_status() -> Dict[str, object]:
    tracer = get_tracer()
    status: Dict[str, object] = {
        "enabled": tracer.enabled,
        "backend": tracer.backend,
        "reason": tracer.reason,
    }
    config = getattr(tracer, "config", None)
    if config is not None:
        status.update(
            {
                "endpoint": config.endpoint,
                "service_name": config.service_name,
                "success_sample_ratio": config.success_sample_ratio,
                "slow_request_ms": config.slow_request_ms,
            }
        )
    stats = getattr(tracer, "stats", None)
    if callable(stats):
        status["counters"] = stats()
    return status


def reset_tracer_for_tests() -> None:
    """Reset the singleton; intended only for isolated unit tests."""

    global _instance
    with _lock:
        _instance = None

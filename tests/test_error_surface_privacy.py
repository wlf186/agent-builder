"""Static regressions for public API and streaming error surfaces."""

from pathlib import Path

from src.trace_middleware import normalise_trace_id


ROOT = Path(__file__).resolve().parents[1]


def test_backend_does_not_publish_or_log_raw_exception_text() -> None:
    source = (ROOT / "backend.py").read_text(encoding="utf-8")
    forbidden = (
        "detail=str(e)",
        "detail=str(exc)",
        'logger.log_error("FileLoadError", str(e))',
        'logger.log_error("EndpointError", redact_text(str(e)))',
        "error_msg = str(e)",
    )
    for pattern in forbidden:
        assert pattern not in source


def test_trace_ids_are_header_safe_and_trace_errors_are_type_only() -> None:
    assert normalise_trace_id("request-123") == "request-123"
    generated = normalise_trace_id("private\r\nX-Injected: yes")
    assert generated != "private\r\nX-Injected: yes"
    assert len(generated) == 36

    source = (ROOT / "src" / "trace_middleware.py").read_text(encoding="utf-8")
    assert "trace_error = str(" not in source

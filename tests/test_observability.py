"""Offline tests for the vendor-neutral observability layer."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


# Import only the observability package.  ``src.__init__`` eagerly imports the
# complete application (and all optional runtime dependencies), which is not
# required for these isolated tests.
ROOT = Path(__file__).resolve().parents[1]
if "src" not in sys.modules:
    src_package = types.ModuleType("src")
    src_package.__path__ = [str(ROOT / "src")]
    sys.modules["src"] = src_package

from src.observability import NoopTracer  # noqa: E402
from src.observability import factory  # noqa: E402
from src.observability.base import TracerContextManagers  # noqa: E402
from src.observability.otel_tracer import (  # noqa: E402
    OTEL_AVAILABLE,
    ObservabilityConfig,
    OpenTelemetryTracer,
    QueuedPrioritySpanExporter,
)
from src.observability.pricing import ModelPrice, PricingCatalog  # noqa: E402
from src.observability.redaction import (  # noqa: E402
    REDACTED,
    redact_text,
    sanitize,
    serialize_attribute,
)
from src.observability.sampling import TailSamplingPolicy  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_redacts_credentials_without_hiding_token_metrics(self):
        credential_url = "https://" + "user:pass" + "@example.test/path"
        sanitized = sanitize(
            {
                "api_key": "sk-proj-super-secret",
                "headers": {"Authorization": "Bearer abcdefghijklmnop"},
                "input_tokens": 123,
                "url": credential_url,
            }
        )

        self.assertEqual(sanitized["api_key"], REDACTED)
        self.assertEqual(sanitized["headers"]["Authorization"], REDACTED)
        self.assertEqual(sanitized["input_tokens"], 123)
        self.assertNotIn("user:pass", sanitized["url"])

    def test_bounds_nested_collections_and_cycles(self):
        cyclic = []
        cyclic.append(cyclic)
        sanitized = sanitize(
            {"items": list(range(100)), "cyclic": cyclic},
            max_items=3,
        )

        self.assertEqual(sanitized["items"][-1], "[97 ITEMS TRUNCATED]")
        self.assertEqual(sanitized["cyclic"][0], "[CYCLE]")

    def test_serialized_attribute_has_a_hard_size_limit(self):
        encoded = serialize_attribute({"prompt": "x" * 10_000}, max_length=128)
        self.assertLessEqual(len(encoded), 128)
        self.assertIn("TRUNCATED", encoded)

    def test_redacts_inline_query_jwt_cloud_and_assignment_secrets(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        # Assemble a syntactically realistic example without leaving a
        # credential-shaped literal for the repository secret scanner.
        aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        value = (
            "https://example.test/run?api_key=query-secret&token=next-secret "
            f"jwt={jwt} aws={aws_key} password=hunter2 secret:top-secret"
        )

        redacted = redact_text(value)

        for secret in ("query-secret", "next-secret", jwt, aws_key, "hunter2", "top-secret"):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count(REDACTED), 6)


class SamplingTests(unittest.TestCase):
    def test_error_and_slow_traces_are_always_retained(self):
        policy = TailSamplingPolicy(success_ratio=0.0, slow_request_ms=500)

        self.assertTrue(policy.should_keep("error", has_error=True))
        self.assertTrue(policy.should_keep("slow", max_duration_ms=500))
        self.assertFalse(policy.should_keep("normal", max_duration_ms=499))

    def test_success_sampling_is_stable(self):
        policy = TailSamplingPolicy(success_ratio=0.4)
        first = policy.should_keep("stable-trace")
        self.assertEqual(first, policy.should_keep("stable-trace"))
        self.assertTrue(TailSamplingPolicy(success_ratio=1).should_keep("all"))
        self.assertFalse(TailSamplingPolicy(success_ratio=0).should_keep("none"))


class PricingTests(unittest.TestCase):
    def test_configured_prices_produce_auditable_costs(self):
        catalog = PricingCatalog(
            {"model-a": ModelPrice(input_per_million=1.0, output_per_million=2.0)}
        )

        self.assertEqual(
            catalog.estimate("MODEL-A", 1_000_000, 500_000),
            {"input": 1.0, "output": 1.0, "total": 2.0},
        )
        self.assertIsNone(catalog.estimate("unknown", 1, 1))


class NoopAndFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_noop_context_managers_preserve_application_semantics(self):
        tracer = NoopTracer("test")
        async with tracer.trace_llm_call("trace", "model", "provider", "prompt") as span:
            span.update(output={"answer": "ok"}, usage={"input": 1, "output": 1})
        async with tracer.trace_tool_call("trace", "tool", {"password": "secret"}) as span:
            span.update(result="ok")

        self.assertFalse(tracer.enabled)
        self.assertTrue(tracer.force_flush())
        await tracer.shutdown()

    async def test_context_manager_reraises_application_errors(self):
        tracer = NoopTracer("test")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with tracer.trace_tool_call("trace", "tool", {}):
                raise RuntimeError("boom")

    async def test_context_manager_never_records_exception_text(self):
        private_error = "provider failure /private/runtime/path?api_key=secret-value"

        class CapturingTracer(TracerContextManagers):
            def __init__(self):
                self.ended = []

            def create_span(self, **_kwargs):
                return ("span-id", "observation-id")

            def end_span(self, **kwargs):
                self.ended.append(kwargs)

        tracer = CapturingTracer()
        with self.assertRaisesRegex(RuntimeError, "provider failure"):
            async with tracer.trace_tool_call("trace", "tool", {}):
                raise RuntimeError(private_error)

        rendered = json.dumps(tracer.ended, ensure_ascii=False)
        self.assertNotIn(private_error, rendered)
        self.assertNotIn("/private/runtime/path", rendered)
        self.assertIn("RuntimeError", rendered)

    def test_explicit_disable_is_reported_as_disabled(self):
        with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": "false"}, clear=False):
            factory.reset_tracer_for_tests()
            tracer = factory.get_tracer()
            self.assertFalse(tracer.enabled)
            self.assertIn("OBSERVABILITY_ENABLED", tracer.reason)
        factory.reset_tracer_for_tests()

    def test_missing_sdk_does_not_claim_observability_is_enabled(self):
        with (
            patch.dict(
                os.environ,
                {"OBSERVABILITY_ENABLED": "true", "OBSERVABILITY_BACKEND": "otlp"},
                clear=False,
            ),
            patch.object(factory, "OTEL_AVAILABLE", False),
            patch.object(factory, "OTEL_IMPORT_ERROR", ImportError("not installed")),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            factory.reset_tracer_for_tests()
            tracer = factory.get_tracer()
            self.assertFalse(tracer.enabled)
            self.assertIn("unavailable", tracer.reason)
            self.assertNotIn("not installed", tracer.reason)
            self.assertNotIn("not installed", str(caught))
            self.assertTrue(caught)
        factory.reset_tracer_for_tests()


@unittest.skipUnless(OTEL_AVAILABLE, "OpenTelemetry SDK is optional")
class OpenTelemetryTracerTests(unittest.TestCase):
    class CapturingExporter:
        def __init__(self):
            self.spans = []
            self.stopped = False

        def export(self, spans):
            from opentelemetry.sdk.trace.export import SpanExportResult

            self.spans.extend(spans)
            return SpanExportResult.SUCCESS

        def force_flush(self, timeout_millis=10_000):
            return True

        def shutdown(self):
            self.stopped = True

    def _config(self, ratio=1.0):
        return ObservabilityConfig(
            endpoint="http://127.0.0.1:1/v1/traces",
            success_sample_ratio=ratio,
            slow_request_ms=60_000,
            batch_schedule_delay_millis=60_000,
        )

    def test_exports_parented_openinference_spans_without_network(self):
        exporter = self.CapturingExporter()
        tracer = OpenTelemetryTracer(self._config(), exporter=exporter)
        trace = tracer.create_trace(
            "request-1",
            "agent:test",
            session_id="conversation-1",
            input={"query": "hello", "api_key": "sk-proj-secret"},
        )
        child = tracer.create_span(
            "request-1",
            "llm.model-a",
            parent_observation_id=trace["observation_id"],
            span_type="LLM",
            input={"model": "model-a", "messages": ["hello"]},
        )
        tracer.end_span(
            "request-1",
            child[0],
            output={"answer": "world"},
            usage={"input": 10, "output": 5},
        )
        tracer.end_trace("request-1", output={"response": "world"})
        self.assertTrue(tracer.force_flush())

        spans = {span.name: span for span in exporter.spans}
        self.assertEqual(set(spans), {"agent:test", "llm.model-a"})
        self.assertEqual(spans["llm.model-a"].attributes["openinference.span.kind"], "LLM")
        self.assertEqual(spans["llm.model-a"].attributes["llm.token_count.total"], 15)
        self.assertEqual(
            spans["llm.model-a"].parent.span_id,
            spans["agent:test"].context.span_id,
        )
        root_input = json.loads(spans["agent:test"].attributes["input.value"])
        self.assertEqual(root_input["api_key"], REDACTED)
        asyncio.run(tracer.shutdown())

    def test_tail_sampling_keeps_a_trace_with_an_error_child(self):
        exporter = self.CapturingExporter()
        tracer = OpenTelemetryTracer(self._config(ratio=0.0), exporter=exporter)
        tracer.create_trace("request-error", "agent:test")
        child = tracer.create_span("request-error", "tool.failure", span_type="TOOL")
        tracer.end_span(
            "request-error",
            child[0],
            status="error",
            error={"type": "RuntimeError", "message": "failure"},
        )
        tracer.end_trace("request-error")
        tracer.force_flush()

        self.assertEqual({span.name for span in exporter.spans}, {"agent:test", "tool.failure"})
        asyncio.run(tracer.shutdown())

    def test_error_attributes_never_include_exception_text_or_paths(self):
        private_error = "provider failure /private/runtime/path?api_key=secret-value"
        exporter = self.CapturingExporter()
        tracer = OpenTelemetryTracer(self._config(), exporter=exporter)
        tracer.create_trace("private-error", "agent:test")
        child = tracer.create_span("private-error", "tool.failure", span_type="TOOL")
        tracer.end_span(
            "private-error",
            child[0],
            status="error",
            error={"type": "RuntimeError", "message": private_error},
        )
        tracer.end_trace("private-error")
        tracer.force_flush()

        rendered = repr(
            [
                (span.attributes, span.events, span.status.description)
                for span in exporter.spans
            ]
        )
        self.assertNotIn(private_error, rendered)
        self.assertNotIn("/private/runtime/path", rendered)
        self.assertIn("RuntimeError", rendered)
        asyncio.run(tracer.shutdown())

    def test_tail_sampling_drops_an_unsampled_success(self):
        exporter = self.CapturingExporter()
        tracer = OpenTelemetryTracer(self._config(ratio=0.0), exporter=exporter)
        tracer.create_trace("request-success", "agent:test")
        tracer.end_trace("request-success")
        tracer.force_flush()

        self.assertEqual(exporter.spans, [])
        asyncio.run(tracer.shutdown())

    def test_tail_sampling_bounds_one_trace_and_preserves_late_error_signal(self):
        exporter = self.CapturingExporter()
        config = ObservabilityConfig(
            endpoint="http://127.0.0.1:1/v1/traces",
            success_sample_ratio=0.0,
            slow_request_ms=60_000,
            batch_schedule_delay_millis=60_000,
            max_spans_per_trace=2,
            max_trace_bytes=1_048_576,
        )
        tracer = OpenTelemetryTracer(config, exporter=exporter)
        tracer.create_trace("bounded-error", "agent:test")
        for index in range(4):
            child = tracer.create_span(
                "bounded-error", f"tool.{index}", span_type="TOOL"
            )
            tracer.end_span(
                "bounded-error",
                child[0],
                status="error" if index == 3 else "success",
                error={"message": "late failure"} if index == 3 else None,
            )
        tracer.end_trace("bounded-error")
        tracer.force_flush()

        counters = tracer.stats()
        self.assertGreaterEqual(counters["truncated_spans"], 1)
        self.assertEqual(counters["priority_traces"], 1)
        self.assertIn("agent:test", {span.name for span in exporter.spans})
        asyncio.run(tracer.shutdown())

    def test_tags_are_redacted_before_export(self):
        exporter = self.CapturingExporter()
        tracer = OpenTelemetryTracer(self._config(), exporter=exporter)
        tracer.create_trace(
            "tag-secret",
            "agent:test",
            tags=["token=must-not-leak"],
        )
        tracer.end_trace("tag-secret")
        tracer.force_flush()

        tags = exporter.spans[0].attributes["tag.tags"]
        self.assertNotIn("must-not-leak", tags[0])
        self.assertIn(REDACTED, tags[0])
        asyncio.run(tracer.shutdown())

    def test_priority_export_network_io_does_not_block_request_thread(self):
        class BlockingExporter(self.CapturingExporter):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def export(self, spans):
                self.started.set()
                self.release.wait(timeout=2)
                return super().export(spans)

        exporter = BlockingExporter()
        tracer = OpenTelemetryTracer(self._config(ratio=0.0), exporter=exporter)
        tracer.create_trace("nonblocking-error", "agent:test")

        started_at = time.monotonic()
        tracer.end_trace(
            "nonblocking-error",
            status="error",
            error={"message": "failure"},
        )
        elapsed = time.monotonic() - started_at

        try:
            self.assertLess(elapsed, 0.25)
            self.assertTrue(exporter.started.wait(timeout=1))
        finally:
            exporter.release.set()
            tracer.force_flush()
        asyncio.run(tracer.shutdown())

    def test_full_priority_queue_never_blocks_request_thread(self):
        from opentelemetry.sdk.trace.export import SpanExportResult

        class BlockingExporter(self.CapturingExporter):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def export(self, spans):
                self.started.set()
                self.release.wait(timeout=2)
                return super().export(spans)

        delegate = BlockingExporter()
        exporter = QueuedPrioritySpanExporter(
            delegate,
            max_queue_traces=1,
            max_batch_spans=1,
            batch_delay_millis=1_000,
            enqueue_timeout_millis=10_000,
        )
        try:
            self.assertIs(exporter.export(("active",)), SpanExportResult.SUCCESS)
            self.assertTrue(delegate.started.wait(timeout=1))
            self.assertIs(exporter.export(("queued",)), SpanExportResult.SUCCESS)

            started = time.monotonic()
            result = exporter.export(("overflow",))
            elapsed = time.monotonic() - started

            self.assertIs(result, SpanExportResult.FAILURE)
            self.assertLess(elapsed, 0.1)
            self.assertEqual(exporter.enqueue_failure_count, 1)
        finally:
            delegate.release.set()
            exporter.shutdown()


class ManagedPhoenixEnvironmentTests(unittest.TestCase):
    def test_env_script_forces_loopback_sqlite_and_local_otlp(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PHOENIX_HOST": "0.0.0.0",
                "PHOENIX_PORT": "6006",
                "PHOENIX_SQL_DATABASE_URL": "postgresql://example.invalid/telemetry",
                "PHOENIX_POSTGRES_HOST": "example.invalid",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://example.invalid/v1/traces",
            }
        )
        command = (
            "source ./env.sh && "
            "printf '%s\\n' \"$PHOENIX_HOST\" "
            "\"${PHOENIX_SQL_DATABASE_URL-unset}\" "
            "\"${PHOENIX_POSTGRES_HOST-unset}\" "
            "\"$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT\""
        )

        result = subprocess.run(
            ["bash", "-c", command],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(
            result.stdout.splitlines(),
            ["127.0.0.1", "unset", "unset", "http://127.0.0.1:6006/v1/traces"],
        )


if __name__ == "__main__":
    unittest.main()

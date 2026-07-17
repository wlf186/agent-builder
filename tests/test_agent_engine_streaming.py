"""Focused regressions for AgentEngine streaming state and model reuse."""

from __future__ import annotations

from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from src.agent_engine import AgentEngine
from src.models import AgentConfig, LLMProvider, ModelProvider


class _Chunk:
    def __init__(
        self,
        content: str = "",
        *,
        tool_call_chunks=None,
        tool_calls=None,
        usage_metadata=None,
    ) -> None:
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []
        self.tool_calls = tool_calls or []
        self.usage_metadata = usage_metadata


def _engine(*, max_iterations: int = 1) -> AgentEngine:
    engine = AgentEngine(
        AgentConfig(
            name="test-agent",
            persona="p",
            llm_provider=LLMProvider.OLLAMA,
            llm_model="test-model",
            llm_base_url="http://127.0.0.1:11434",
            max_iterations=max_iterations,
        )
    )
    engine.llm = object()
    # These tests provide a deterministic offline stream and do not construct a
    # provider client or perform URL validation.
    engine._refresh_skill_tool_if_needed = lambda: None
    return engine


async def _collect(engine: AgentEngine, message: str = "hello"):
    return [
        event
        async for event in engine._stream_events(
            message,
            history=[],
            trace_id=f"trace-{message}",
        )
    ]


class AgentEngineStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.observability_patches = (
            patch("src.agent_engine.is_observability_enabled", return_value=False),
            patch("src.agent_engine.get_tracer", return_value=object()),
        )
        for active_patch in self.observability_patches:
            active_patch.start()

    async def asyncTearDown(self) -> None:
        for active_patch in reversed(self.observability_patches):
            active_patch.stop()

    async def test_native_tool_deltas_are_merged_without_second_model_call(self):
        engine = _engine()
        engine.llm_with_tools = object()
        stream_calls = 0
        invoke_calls = 0

        async def fake_stream(_llm, _messages):
            nonlocal stream_calls
            stream_calls += 1
            yield _Chunk(
                tool_call_chunks=[
                    {
                        "name": "demo_tool",
                        "args": '{"value":',
                        "id": "call_",
                        "index": 0,
                    }
                ]
            )
            yield _Chunk(
                tool_call_chunks=[
                    {"name": None, "args": "42}", "id": "123", "index": 0}
                ],
                usage_metadata={"input_tokens": 7, "output_tokens": 1},
            )

        async def forbidden_invoke(_llm, _messages):
            nonlocal invoke_calls
            invoke_calls += 1
            raise AssertionError("streamed tool calls must not trigger ainvoke")

        engine._stream_llm = fake_stream
        engine._invoke_llm = forbidden_invoke

        events = await _collect(engine)
        tool_events = [event for event in events if event.get("type") == "tool_call"]

        self.assertEqual(stream_calls, 1)
        self.assertEqual(invoke_calls, 0)
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["name"], "demo_tool")
        self.assertEqual(tool_events[0]["args"], {"value": 42})

    async def test_native_tool_deltas_merge_fragmented_id_without_index(self):
        engine = _engine()
        engine.llm_with_tools = object()

        async def fake_stream(_llm, _messages):
            yield _Chunk(
                tool_call_chunks=[
                    {"name": "demo_", "args": '{"value":', "id": "call_"}
                ]
            )
            yield _Chunk(
                tool_call_chunks=[
                    {"name": "tool", "args": "42}", "id": "123"}
                ]
            )

        engine._stream_llm = fake_stream
        events = await _collect(engine)
        tool_events = [event for event in events if event.get("type") == "tool_call"]

        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["name"], "demo_tool")
        self.assertEqual(tool_events[0]["args"], {"value": 42})

    async def test_rag_sources_are_emitted_after_current_request_retrieval(self):
        engine = _engine(max_iterations=2)
        engine.llm_with_tools = object()
        invocation = 0

        async def fake_stream(_llm, _messages):
            nonlocal invocation
            invocation += 1
            if invocation == 1:
                yield _Chunk(
                    tool_call_chunks=[
                        {
                            "name": "rag_retrieve",
                            "args": '{"query":"current"}',
                            "id": "rag-1",
                            "index": 0,
                        }
                    ]
                )
            else:
                yield _Chunk("done")

        async def fake_execute(_name, _args, trace_id=None):
            engine._retrieval_sources_context.set(
                ({"filename": "current.txt", "chunk_index": 0, "score": 0.9},)
            )
            return "retrieved"

        engine._stream_llm = fake_stream
        engine._execute_tool = fake_execute
        events = await _collect(engine)
        source_events = [
            event for event in events if event.get("type") == "rag_sources"
        ]

        self.assertEqual(
            source_events,
            [
                {
                    "type": "rag_sources",
                    "sources": [
                        {
                            "filename": "current.txt",
                            "chunk_index": 0,
                            "score": 0.9,
                        }
                    ],
                }
            ],
        )

    async def test_json_first_chunk_is_retained(self):
        engine = _engine()

        async def fake_stream(_llm, _messages):
            yield _Chunk('{"answer":')
            yield _Chunk('"ok"}')

        engine._stream_llm = fake_stream
        events = await _collect(engine)
        content = "".join(
            event.get("content", "")
            for event in events
            if event.get("type") == "content"
        )

        self.assertEqual(content, '{"answer":"ok"}')

    async def test_stream_rejects_oversized_content_and_tool_arguments(self):
        content_engine = _engine()
        content_engine.MAX_LLM_RESPONSE_CHARS = 3

        async def oversized_content(_llm, _messages):
            yield _Chunk("four")

        content_engine._stream_llm = oversized_content
        with self.assertRaisesRegex(ValueError, "2MB"):
            await _collect(content_engine, "content-limit")

        argument_engine = _engine()
        argument_engine.llm_with_tools = object()
        argument_engine.MAX_LLM_TOOL_ARGUMENT_BYTES = 8

        async def oversized_arguments(_llm, _messages):
            yield _Chunk(
                tool_call_chunks=[{
                    "name": "demo",
                    "args": '{"value":"0123456789"}',
                    "id": "call-1",
                    "index": 0,
                }]
            )

        argument_engine._stream_llm = oversized_arguments
        with self.assertRaisesRegex(ValueError, "1MB"):
            await _collect(argument_engine, "argument-limit")

    async def test_observability_never_receives_response_or_tool_values(self):
        class RecordingTracer:
            def __init__(self) -> None:
                self.calls = []

            def create_trace(self, **kwargs):
                self.calls.append(("create_trace", kwargs))
                return {"observation_id": "root"}

            def create_span(self, **kwargs):
                self.calls.append(("create_span", kwargs))
                return ("span", "observation")

            def end_span(self, **kwargs):
                self.calls.append(("end_span", kwargs))

            def end_trace(self, **kwargs):
                self.calls.append(("end_trace", kwargs))

        tracer = RecordingTracer()
        engine = _engine(max_iterations=1)
        engine.llm_with_tools = object()
        argument_secret = "private tool argument"
        result_secret = "private tool result"

        async def tool_stream(_llm, _messages):
            yield _Chunk(
                tool_call_chunks=[{
                    "name": "demo_tool",
                    "args": json.dumps({"query": argument_secret}),
                    "id": "call-1",
                    "index": 0,
                }]
            )

        async def execute_tool(_name, _args, trace_id=None):
            return result_secret

        engine._stream_llm = tool_stream
        engine._execute_tool = execute_tool
        with patch("src.agent_engine.is_observability_enabled", return_value=True), patch(
            "src.agent_engine.get_tracer", return_value=tracer
        ):
            await _collect(engine, "private user query")

        rendered = json.dumps(tracer.calls, ensure_ascii=False)
        self.assertNotIn("private user query", rendered)
        self.assertNotIn(argument_secret, rendered)
        self.assertNotIn(result_secret, rendered)
        self.assertIn("response_length", rendered)
        self.assertIn("tool_call_count", rendered)

    async def test_tool_subagent_and_rag_errors_do_not_expose_exception_text(self):
        tool_engine = _engine(max_iterations=1)
        tool_engine.llm_with_tools = object()
        tool_secret = "private tool exception with token-value"

        async def tool_stream(_llm, _messages):
            yield _Chunk(
                tool_call_chunks=[{
                    "name": "demo_tool",
                    "args": "{}",
                    "id": "call-1",
                    "index": 0,
                }]
            )

        async def fail_tool(_name, _args, trace_id=None):
            raise RuntimeError(tool_secret)

        tool_engine._stream_llm = tool_stream
        tool_engine._execute_tool = fail_tool
        tool_events = await _collect(tool_engine, "tool-error")
        rendered_events = json.dumps(tool_events, ensure_ascii=False)
        self.assertNotIn(tool_secret, rendered_events)
        self.assertIn("错误: 工具执行失败 (RuntimeError)", rendered_events)

        subagent_secret = "private sub-agent provider error"

        class SubAgentManager:
            async def get_instance(self, _name):
                return SimpleNamespace(engine=object())

        subagent_engine = _engine()
        subagent_engine.agent_manager = SubAgentManager()

        async def fail_subagent(*_args, **_kwargs):
            raise ValueError(subagent_secret)

        subagent_engine._run_sub_agent_with_stack = fail_subagent
        subagent_result = await subagent_engine._execute_sub_agent("child", "message")
        self.assertNotIn(subagent_secret, json.dumps(subagent_result, ensure_ascii=False))
        self.assertIn("ValueError", subagent_result["error"])

        rag_secret = "private vector backend error"
        rag_engine = _engine()
        rag_engine._retrievers_initialized = True
        rag_engine._retrievers = {"kb": object()}

        async def fail_search(*_args, **_kwargs):
            raise OSError(rag_secret)

        rag_engine._search_retrievers = fail_search
        rag_result = await rag_engine._execute_tool(
            "rag_retrieve",
            {"query": "safe query"},
            trace_id="trace-" + "1" * 32,
        )
        self.assertNotIn(rag_secret, rag_result)
        self.assertEqual(rag_result, "检索失败 (OSError)")

    async def test_token_usage_is_request_local_and_accumulates_iterations(self):
        engine = _engine(max_iterations=2)
        engine.llm_with_tools = object()
        invocation = 0
        observed_input_chars = []

        async def fake_stream(_llm, messages):
            nonlocal invocation
            invocation += 1
            observed_input_chars.append(
                sum(len(str(getattr(message, "content", ""))) for message in messages)
            )
            if invocation == 1:
                yield _Chunk(
                    tool_call_chunks=[
                        {
                            "name": "demo_tool",
                            "args": "{}",
                            "id": "call-1",
                            "index": 0,
                        }
                    ],
                    usage_metadata={"input_tokens": 10, "output_tokens": 2},
                )
            elif invocation == 2:
                yield _Chunk(
                    "done",
                    usage_metadata={"input_tokens": 20, "output_tokens": 3},
                )
            else:
                yield _Chunk("fresh")

        engine._stream_llm = fake_stream

        await _collect(engine, "first")
        self.assertEqual(
            engine.get_token_usage(),
            {"input_tokens": 30, "output_tokens": 5},
        )

        await _collect(engine, "second")
        self.assertEqual(invocation, 3)
        self.assertEqual(
            engine.get_token_usage(),
            {
                "input_tokens": engine._estimate_tokens_from_chars(
                    observed_input_chars[-1]
                ),
                "output_tokens": engine._estimate_tokens_from_chars(len("fresh")),
            },
        )


class AgentEngineFingerprintTests(unittest.TestCase):
    def test_llm_is_rebuilt_only_when_model_or_tool_fingerprint_changes(self):
        service = SimpleNamespace(
            name="local",
            provider=ModelProvider.OLLAMA,
            base_url="http://127.0.0.1:11434/v1",
            selected_model="model-a",
            api_key=None,
        )
        registry = SimpleNamespace(get_service=lambda _name: service)
        config = AgentConfig(name="test-agent", model_service="local")
        engine = AgentEngine(config, model_service_registry=registry)
        setup_calls = 0

        def fake_setup():
            nonlocal setup_calls
            setup_calls += 1
            engine.llm = object()

        engine._setup_llm = fake_setup

        engine._refresh_skill_tool_if_needed()
        engine._refresh_skill_tool_if_needed()
        self.assertEqual(setup_calls, 1)

        config.temperature = 0.2
        engine._refresh_skill_tool_if_needed()
        self.assertEqual(setup_calls, 2)

        service.selected_model = "model-b"
        engine._refresh_skill_tool_if_needed()
        self.assertEqual(setup_calls, 3)


if __name__ == "__main__":
    unittest.main()

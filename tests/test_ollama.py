"""Contract tests for the fixed-target trusted Ollama broker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
import hashlib
import json
import socket
from typing import Any

import httpx
import pytest

import agent_builder_v2.ollama as ollama_module
from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import (
    ContextCompiler,
    ConversationMessage,
    estimate_provider_input_tokens,
)
from agent_builder_v2.model import BROKER_PROTOCOL_VERSION, MAX_BROKER_FRAME_BYTES
from agent_builder_v2.model_catalog import (
    ModelCatalog,
    ModelCatalogEntry,
    ProviderEndpoint,
)
from agent_builder_v2.ollama import (
    HARNESS_TOOL_ID,
    MAX_CONCURRENT_MODEL_STREAMS,
    MAX_NDJSON_LINE_BYTES,
    MAX_NORMALIZED_CONTENT_FRAMES,
    MAX_OUTPUT_BYTES,
    MAX_STREAM_FRAMES,
    MODEL_NUM_PREDICT,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_PORT,
    TOOL_FINALIZATION_INSTRUCTION,
    OllamaBroker,
    OllamaBrokerError,
    OllamaCancelledError,
    OllamaFrame,
    OllamaRequestMetadata,
    OllamaToolResult,
    REQUEST_DIGEST_DOMAIN,
    RUNTIME_CONTEXT_TOKEN_CAP,
)
from agent_builder_v2.tools import prototype_tool_specs


SAFE_ADDRESS = "10.89.0.18"
DIGEST = "a" * 64


def _resolver(
    host: str, port: int, family: int, kind: int
) -> list[tuple[Any, ...]]:
    assert (host, port, family, kind) == (
        OLLAMA_HOST,
        OLLAMA_PORT,
        0,
        socket.SOCK_STREAM,
    )
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (SAFE_ADDRESS, OLLAMA_PORT),
        )
    ]


def _json_response(value: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json; charset=utf-8"},
        content=json.dumps(value, separators=(",", ":")).encode(),
    )


def _provider_frame(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    done: bool = False,
    done_reason: str | None = None,
    model: str = OLLAMA_MODEL,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    value: dict[str, Any] = {"model": model, "message": message, "done": done}
    if done_reason is not None:
        value["done_reason"] = done_reason
    if done:
        value["prompt_eval_count"] = 17
        value["eval_count"] = 5
    return value


def _ndjson(*values: object) -> bytes:
    return b"".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for value in values
    )


ChatFactory = Callable[[httpx.Request], httpx.Response]


class _MockProvider:
    def __init__(self, chat_factories: list[ChatFactory] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.chat_factories = list(chat_factories or [])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/version":
            return _json_response({"version": "0.23.2"})
        if request.url.path == "/api/tags":
            return _json_response(
                {
                    "models": [
                        {
                            "name": OLLAMA_MODEL,
                            "digest": DIGEST,
                            "size": 2_741_192_820,
                        }
                    ]
                }
            )
        if request.url.path == "/api/show":
            assert json.loads(request.content) == {"model": OLLAMA_MODEL}
            return _json_response(
                {
                    "capabilities": ["completion", "vision", "tools", "thinking"],
                    "model_info": {
                        "general.architecture": "qwen35",
                        "qwen35.context_length": 262_144,
                    },
                }
            )
        if request.url.path == "/api/chat" and self.chat_factories:
            return self.chat_factories.pop(0)(request)
        return _json_response({"error": "unexpected request"}, status=500)


def _chat_response(content: bytes, *, content_type: str = "application/x-ndjson") -> ChatFactory:
    def response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": content_type},
            content=content,
        )

    return response


def _status_response(
    status: int,
    value: object,
    *,
    content_type: str = "application/json",
) -> ChatFactory:
    def response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Content-Type": content_type},
            content=json.dumps(value, separators=(",", ":")).encode(),
        )

    return response


async def _started_broker(
    factories: list[ChatFactory],
) -> tuple[OllamaBroker, _MockProvider]:
    provider = _MockProvider(factories)
    broker = OllamaBroker(
        transport=httpx.MockTransport(provider),
        resolver=_resolver,
    )
    await broker.start()
    return broker, provider


async def _collect(stream: AsyncIterator[OllamaFrame]) -> list[OllamaFrame]:
    return [frame async for frame in stream]


def _plan(broker: OllamaBroker, message: str) -> object:
    qualification = broker.qualification
    assert qualification is not None
    return ContextCompiler().compile(
        message,
        model_profile=qualification.model_profile,
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )


@pytest.mark.asyncio
async def test_qualification_pins_safe_ip_and_fixed_identity() -> None:
    broker, provider = await _started_broker([])
    try:
        qualification = broker.qualification
        assert qualification is not None
        assert qualification.version == "0.23.2"
        assert qualification.model == OLLAMA_MODEL
        assert qualification.digest == DIGEST
        assert qualification.address == SAFE_ADDRESS
        assert qualification.model_profile.native_context_tokens == 262_144
        assert (
            qualification.model_profile.operational_context_tokens
            == RUNTIME_CONTEXT_TOKEN_CAP
        )
        assert qualification.model_profile.max_output_tokens == MODEL_NUM_PREDICT
        assert [request.url.host for request in provider.requests] == [
            SAFE_ADDRESS,
            SAFE_ADDRESS,
            SAFE_ADDRESS,
        ]
        assert all(
            request.headers["host"] == f"{OLLAMA_HOST}:{OLLAMA_PORT}"
            for request in provider.requests
        )
    finally:
        await broker.close()

    with pytest.raises(TypeError):
        OllamaBroker(endpoint="http://attacker.invalid")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        OllamaBroker(model="larger-model")  # type: ignore[call-arg]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "value", "content_type", "expected_code"),
    [
        (400, {"error": "context length exceeds maximum"}, "application/json", "model_context_overflow"),
        (413, {"error": "vision image is too large"}, "application/json", "model_media_overflow"),
        (400, {"error": "invalid api token"}, "application/json", "model_unavailable"),
        (401, {"error": "context length exceeds maximum"}, "application/json", "model_unavailable"),
        (400, {"error": "context length exceeds maximum", "detail": "x"}, "application/json", "model_unavailable"),
        (400, {"error": "context length exceeds maximum"}, "text/plain", "model_unavailable"),
    ],
)
async def test_provider_overflow_classification_is_exact_and_fail_closed(
    status: int,
    value: object,
    content_type: str,
    expected_code: str,
) -> None:
    broker, _provider = await _started_broker(
        [_status_response(status, value, content_type=content_type)]
    )
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                broker.new_run(_plan(broker, "classify provider status")).stream_turn(
                    "classify provider status"
                )
            )
        assert raised.value.code == expected_code
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_classified_overflow_can_install_one_recovery_projection() -> None:
    overflow = _status_response(
        400, {"error": "prompt is too long and exceeds context limit"}
    )
    success = _chat_response(
        _ndjson(_provider_frame(content="recovered", done=True, done_reason="stop"))
    )
    broker, provider = await _started_broker([overflow, success])
    try:
        profile = broker.qualification.model_profile  # type: ignore[union-attr]
        history = (
            ConversationMessage("1" * 32, "user", "older question " + "A" * 1024),
            ConversationMessage("2" * 32, "assistant", "older answer " + "B" * 1024),
            ConversationMessage("3" * 32, "user", "recent question"),
            ConversationMessage("4" * 32, "assistant", "recent answer"),
        )
        compiler = ContextCompiler()
        initial = compiler.compile(
            "current question",
            model_profile=profile,
            tools=prototype_tool_specs(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
            history=history,
        )
        recovery = compiler.compile(
            "current question",
            model_profile=profile,
            tools=prototype_tool_specs(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
            history=history,
            force_compact=True,
            collapse_to_recent=True,
        )
        assert recovery.reference != initial.reference
        session = broker.new_run(initial)
        observed: list[int] = []

        async def observe(metadata: OllamaRequestMetadata) -> None:
            observed.append(metadata.iteration)

        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                session.stream_turn("current question", on_request=observe)
            )
        assert raised.value.code == "model_context_overflow"
        session.install_recovery_context(recovery)
        frames = await _collect(
            session.stream_turn("current question", on_request=observe)
        )
        assert frames[-1].kind == "stop"
        assert observed == [1, 1]
        chat = [
            json.loads(request.content)
            for request in provider.requests
            if request.url.path == "/api/chat"
        ]
        assert len(chat) == 2
        assert chat[0]["messages"] != chat[1]["messages"]
        with pytest.raises(OllamaBrokerError) as reused:
            session.install_recovery_context(recovery)
        assert reused.value.code == "model_recovery_invalid"
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_partial_provider_stream_never_enables_overflow_recovery() -> None:
    partial_then_error = _chat_response(
        _ndjson(
            _provider_frame(content="partial"),
            {"error": "context length exceeds maximum"},
        )
    )
    broker, _provider = await _started_broker([partial_then_error])
    try:
        plan = _plan(broker, "partial stream")
        session = broker.new_run(plan)
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("partial stream"))
        assert raised.value.code == "model_protocol_error"
        with pytest.raises(OllamaBrokerError) as recovery:
            session.install_recovery_context(plan)
        assert recovery.value.code == "model_recovery_invalid"
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_catalog_qualifies_two_profiles_and_binds_each_request() -> None:
    endpoint = ProviderEndpoint("trusted", "ollama", OLLAMA_HOST, OLLAMA_PORT)
    catalog = ModelCatalog.create(
        endpoints=(endpoint,),
        models=(
            ModelCatalogEntry(
                "large-tools", "ollama", "large:1b", endpoint.endpoint_id,
                32_768, 2_048,
            ),
            ModelCatalogEntry(
                "small-text", "ollama", "small:1b", endpoint.endpoint_id,
                8_192, 512, ("completion", "streaming"),
            ),
        ),
        default_model_id="large-tools",
    )
    chat_bodies: list[dict[str, Any]] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _json_response({"version": "0.23.2"})
        if request.url.path == "/api/tags":
            return _json_response({
                "models": [
                    {"name": "large:1b", "digest": "a" * 64, "size": 100},
                    {"name": "small:1b", "digest": "b" * 64, "size": 80},
                ]
            })
        if request.url.path == "/api/show":
            model = json.loads(request.content)["model"]
            return _json_response({
                "capabilities": (
                    ["completion", "tools"] if model == "large:1b"
                    else ["completion"]
                ),
                "model_info": {
                    "general.architecture": "test",
                    "test.context_length": (
                        65_536 if model == "large:1b" else 16_384
                    ),
                },
            })
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            chat_bodies.append(body)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/x-ndjson"},
                content=_ndjson(_provider_frame(
                    content="ok",
                    done=True,
                    done_reason="stop",
                    model=body["model"],
                )),
            )
        return _json_response({"error": "unexpected"}, status=500)

    broker = OllamaBroker(
        catalog=catalog,
        transport=httpx.MockTransport(provider),
        resolver=_resolver,
    )
    try:
        default = await broker.start()
        small = broker.qualification_for("small-text")
        assert default.catalog_model_id == "large-tools"
        assert default.model_profile.operational_context_tokens == 32_768
        assert default.model_profile.supports_tools is True
        assert small.model_profile.operational_context_tokens == 8_192
        assert small.model_profile.supports_tools is False
        for qualification, tools in (
            (default, prototype_tool_specs()),
            (small, ()),
        ):
            plan = ContextCompiler().compile(
                "answer directly",
                model_profile=qualification.model_profile,
                tools=tools,
                agent_id=PROTOTYPE_AGENT_ID,
                capsule_generation=1,
            )
            frames = await _collect(
                broker.new_run(plan).stream_turn("answer directly")
            )
            assert frames[-1].kind == "stop"
        assert [body["model"] for body in chat_bodies] == ["large:1b", "small:1b"]
        assert chat_bodies[0]["options"]["num_ctx"] == 32_768
        assert chat_bodies[1]["options"]["num_ctx"] == 8_192
        assert chat_bodies[0]["tools"]
        assert chat_bodies[1]["tools"] == []
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_semantic_summary_uses_empty_toolset_and_binds_validated_usage() -> None:
    def summary_response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["tools"] == []
        assert "format" not in body
        assert "UNTRUSTED_TRANSCRIPT_JSON" in body["messages"][1]["content"]
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=_ndjson({
                "model": OLLAMA_MODEL,
                "message": {"role": "assistant", "content": json.dumps({
                    "facts": ["code is SUM-91"],
                    "decisions": ["keep isolation"],
                    "open_tasks": [],
                    "files": [],
                    "references": [],
                })},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 120,
                "eval_count": 30,
            }),
        )

    broker, _provider = await _started_broker([summary_response])
    try:
        source = (
            ConversationMessage("1" * 32, "user", "remember SUM-91"),
            ConversationMessage("2" * 32, "assistant", "remembered"),
        )
        snapshot = await broker.summarize(source)

        assert snapshot.source_message_ids == ("1" * 32, "2" * 32)
        assert snapshot.content.facts == ("code is SUM-91",)
        assert snapshot.input_tokens == 120
        assert snapshot.output_tokens == 30
        assert snapshot.model_profile_digest == broker.qualification.model_profile.profile_digest  # type: ignore[union-attr]
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_invalid_summaries_open_bounded_circuit_without_immediate_retry() -> None:
    def invalid_summary(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=_ndjson({
                "model": OLLAMA_MODEL,
                "message": {"role": "assistant", "content": "not-json"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 10,
                "eval_count": 2,
            }),
        )

    broker, provider = await _started_broker(
        [invalid_summary, invalid_summary, invalid_summary]
    )
    source = (
        ConversationMessage("1" * 32, "user", "untrusted"),
        ConversationMessage("2" * 32, "assistant", "data"),
    )
    try:
        for _index in range(3):
            with pytest.raises(OllamaBrokerError) as failure:
                await broker.summarize(source)
            assert failure.value.code == "summary_invalid"
        chat_count = sum(request.url.path == "/api/chat" for request in provider.requests)
        with pytest.raises(OllamaBrokerError) as circuit:
            await broker.summarize(source)
        assert circuit.value.code == "summary_circuit_open"
        assert sum(request.url.path == "/api/chat" for request in provider.requests) == chat_count
    finally:
        await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_address",
    ["127.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "240.0.0.1"],
)
async def test_qualification_rejects_unsafe_resolution(unsafe_address: str) -> None:
    calls = 0

    def unsafe_resolver(*_args: object) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (unsafe_address, OLLAMA_PORT),
            )
        ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response({})

    broker = OllamaBroker(
        transport=httpx.MockTransport(handler), resolver=unsafe_resolver
    )
    with pytest.raises(OllamaBrokerError) as raised:
        await broker.start()
    assert raised.value.code == "model_endpoint_rejected"
    assert calls == 0
    await broker.close()


@pytest.mark.asyncio
async def test_two_turn_tool_loop_preserves_assistant_call_and_tool_result() -> None:
    provider_tool_call = {
        "id": "call_provider_1",
        "function": {
            "index": 0,
            "name": "builtin_echo",
            "arguments": {"text": "hello"},
        },
    }
    first = _chat_response(
        _ndjson(
            _provider_frame(tool_calls=[provider_tool_call]),
            _provider_frame(done=True, done_reason="stop"),
        )
    )
    answer_fragments = ("A" * 100, "B" * 100, "C" * 100)
    second = _chat_response(
        _ndjson(
            *(_provider_frame(content=value) for value in answer_fragments),
            _provider_frame(done=True, done_reason="stop"),
        )
    )
    broker, provider = await _started_broker([first, second])
    try:
        plan = _plan(broker, "please echo hello")
        session = broker.new_run(plan)
        observed: list[OllamaRequestMetadata] = []

        async def observe(metadata: OllamaRequestMetadata) -> None:
            observed.append(metadata)

        tool_frames = await _collect(
            session.stream_turn("please echo hello", on_request=observe)
        )
        assert tool_frames == [
            OllamaFrame(
                "tool.use",
                {
                    "call_id": "call_provider_1",
                    "tool_id": HARNESS_TOOL_ID,
                    "arguments": {"text": "hello"},
                    "usage": {"prompt_eval_count": 17, "eval_count": 5},
                },
            )
        ]
        assert session.messages[-1] == {
            "role": "assistant",
            "content": "",
            "tool_calls": [provider_tool_call],
        }

        result = OllamaToolResult(
            call_id="call_provider_1",
            tool_id=HARNESS_TOOL_ID,
            content="hello",
        )
        final_frames = await _collect(
            session.stream_turn(
                "please echo hello", (result,), on_request=observe
            )
        )
        content_frames = [frame for frame in final_frames if frame.kind == "content"]
        assert "".join(str(frame.payload["text"]) for frame in content_frames) == "".join(
            answer_fragments
        )
        assert len(content_frames) <= 2
        assert final_frames[-1] == OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 17, "eval_count": 5},
            },
        )
        assert session.messages[-2] == {
            "role": "tool",
            "tool_name": "builtin_echo",
            "content": "hello",
        }

        chat_requests = [
            request for request in provider.requests if request.url.path == "/api/chat"
        ]
        assert len(chat_requests) == 2
        first_body = json.loads(chat_requests[0].content)
        second_body = json.loads(chat_requests[1].content)
        assert observed == [
            OllamaRequestMetadata(
                iteration=1,
                message_count=len(first_body["messages"]),
                tool_count=len(first_body["tools"]),
                estimated_input_tokens=estimate_provider_input_tokens(
                    first_body["messages"], plan.tools
                ),
                request_bytes=len(chat_requests[0].content),
                request_digest=hashlib.sha256(
                    REQUEST_DIGEST_DOMAIN + chat_requests[0].content
                ).hexdigest(),
            ),
                OllamaRequestMetadata(
                    iteration=2,
                    message_count=len(second_body["messages"]),
                    tool_count=len(second_body["tools"]),
                    estimated_input_tokens=estimate_provider_input_tokens(
                        second_body["messages"], plan.tools
                ),
                request_bytes=len(chat_requests[1].content),
                request_digest=hashlib.sha256(
                    REQUEST_DIGEST_DOMAIN + chat_requests[1].content
                ).hexdigest(),
            ),
        ]
        for body in (first_body, second_body):
            assert body["model"] == OLLAMA_MODEL
            assert body["stream"] is True
            assert body["think"] is False
            assert body["options"]["temperature"] == 0
            assert body["options"]["num_predict"] == MODEL_NUM_PREDICT
            assert body["options"]["num_ctx"] == RUNTIME_CONTEXT_TOKEN_CAP
        assert first_body["tools"][0]["function"]["name"] == "builtin_echo"
        assert second_body["tools"][0]["function"]["name"] == "builtin_echo"
        assert first_body["messages"][0]["role"] == "system"
        assert "[platform.contract]" in first_body["messages"][0]["content"]
        assert "[agent.instructions]" in first_body["messages"][0]["content"]
        assert first_body["messages"][1] == {
            "role": "user",
            "content": "please echo hello",
        }
        assert second_body["messages"][-2:] == [
            {"role": "assistant", "content": "", "tool_calls": [provider_tool_call]},
            {"role": "tool", "tool_name": "builtin_echo", "content": "hello"},
        ]
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_large_tool_result_uses_bounded_projection_before_readmission() -> None:
    canonical = "x" * 8_192
    provider_tool_call = {
        "id": "call_large",
        "function": {
            "index": 0,
            "name": "builtin_echo",
            "arguments": {"text": canonical},
        },
    }
    first = _chat_response(
        _ndjson(
            _provider_frame(tool_calls=[provider_tool_call]),
            _provider_frame(done=True, done_reason="stop"),
        )
    )
    second = _chat_response(
        _ndjson(
            _provider_frame(content="done"),
            _provider_frame(done=True, done_reason="stop"),
        )
    )
    broker, provider = await _started_broker([first, second])
    try:
        plan = _plan(broker, "echo a bounded large result")
        session = broker.new_run(plan)
        await _collect(session.stream_turn("echo a bounded large result"))
        await _collect(
            session.stream_turn(
                "echo a bounded large result",
                (OllamaToolResult("call_large", HARNESS_TOOL_ID, canonical),),
            )
        )

        requests = [
            json.loads(request.content)
            for request in provider.requests
            if request.url.path == "/api/chat"
        ]
        projected = requests[1]["messages"][-1]["content"]
        assert canonical not in projected
        assert "call_id=call_large" in projected
        assert "original_bytes=8192" in projected
        assert "reason=provider_projection_limit" in projected
        assert len(projected.encode("utf-8")) <= 4_096
        assert session.messages[-2]["content"] == projected
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_request_observer_failure_prevents_provider_http_and_state_commit() -> None:
    broker, provider = await _started_broker(
        [
            _chat_response(
                _ndjson(_provider_frame(done=True, done_reason="stop"))
            )
        ]
    )
    try:
        session = broker.new_run(_plan(broker, "hello"))

        async def reject(_metadata: OllamaRequestMetadata) -> None:
            raise RuntimeError("simulated durable boundary failure")

        with pytest.raises(RuntimeError, match="durable boundary"):
            await _collect(session.stream_turn("hello", on_request=reject))
        assert not any(request.url.path == "/api/chat" for request in provider.requests)
        assert session.messages == ()

        observed: list[OllamaRequestMetadata] = []

        async def accept(metadata: OllamaRequestMetadata) -> None:
            observed.append(metadata)

        frames = await _collect(session.stream_turn("hello", on_request=accept))
        assert frames[-1].kind == "stop"
        assert [item.iteration for item in observed] == [1]
        assert sum(request.url.path == "/api/chat" for request in provider.requests) == 1
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_slow_request_observer_preserves_session_single_flight() -> None:
    broker, provider = await _started_broker(
        [
            _chat_response(
                _ndjson(_provider_frame(done=True, done_reason="stop"))
            )
        ]
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    try:
        session = broker.new_run(_plan(broker, "hello"))

        async def observe(_metadata: OllamaRequestMetadata) -> None:
            entered.set()
            await release.wait()

        first = asyncio.create_task(
            _collect(session.stream_turn("hello", on_request=observe))
        )
        await entered.wait()
        assert not any(request.url.path == "/api/chat" for request in provider.requests)

        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("hello"))
        assert raised.value.code == "model_concurrency_error"

        release.set()
        assert (await first)[-1].kind == "stop"
        assert sum(request.url.path == "/api/chat" for request in provider.requests) == 1
    finally:
        release.set()
        await broker.close()


@pytest.mark.asyncio
async def test_frozen_tool_capability_supports_a_second_sequential_call() -> None:
    first_call = {
        "id": "call_first",
        "function": {"name": "builtin_echo", "arguments": {"text": "hello"}},
    }
    repeated_call = {
        "id": "call_repeated",
        "function": {"name": "builtin_echo", "arguments": {"text": "hello"}},
    }
    broker, provider = await _started_broker(
        [
            _chat_response(
                _ndjson(
                    _provider_frame(tool_calls=[first_call]),
                    _provider_frame(done=True, done_reason="stop"),
                )
            ),
            _chat_response(
                _ndjson(
                    _provider_frame(tool_calls=[repeated_call]),
                    _provider_frame(done=True, done_reason="stop"),
                )
            ),
        ]
    )
    try:
        session = broker.new_run(_plan(broker, "hello"))
        await _collect(session.stream_turn("hello"))
        result = OllamaToolResult("call_first", HARNESS_TOOL_ID, "hello")
        second_frames = await _collect(session.stream_turn("hello", (result,)))
        assert second_frames[-1] == OllamaFrame(
            "tool.use",
            {
                "call_id": "call_repeated",
                "tool_id": HARNESS_TOOL_ID,
                "arguments": {"text": "hello"},
                "usage": {"prompt_eval_count": 17, "eval_count": 5},
            },
        )
        chat_requests = [
            request for request in provider.requests if request.url.path == "/api/chat"
        ]
        assert json.loads(chat_requests[1].content)["tools"][0]["function"]["name"] == (
            "builtin_echo"
        )
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_tool_budget_transition_forces_a_visible_final_answer() -> None:
    first_call = {
        "id": "call_first",
        "function": {"name": "builtin_echo", "arguments": {"text": "first"}},
    }
    second_call = {
        "id": "call_second",
        "function": {"name": "builtin_echo", "arguments": {"text": "second"}},
    }
    broker, provider = await _started_broker(
        [
            _chat_response(
                _ndjson(
                    _provider_frame(tool_calls=[first_call]),
                    _provider_frame(done=True, done_reason="stop"),
                )
            ),
            _chat_response(
                _ndjson(
                    _provider_frame(tool_calls=[second_call]),
                    _provider_frame(done=True, done_reason="stop"),
                )
            ),
            _chat_response(
                _ndjson(
                    _provider_frame(content="final visible answer"),
                    _provider_frame(done=True, done_reason="stop"),
                )
            ),
        ]
    )
    try:
        message = "finish after two tools"
        session = broker.new_run(_plan(broker, message))
        await _collect(session.stream_turn(message))
        first_result = OllamaToolResult(
            "call_first", HARNESS_TOOL_ID, "first"
        )
        await _collect(session.stream_turn(message, (first_result,)))
        second_result = OllamaToolResult(
            "call_second", HARNESS_TOOL_ID, "second"
        )
        frames = await _collect(
            session.stream_turn(message, (first_result, second_result))
        )

        assert [frame.kind for frame in frames] == ["content", "stop"]
        requests = [
            json.loads(request.content)
            for request in provider.requests
            if request.url.path == "/api/chat"
        ]
        assert len(requests) == 3
        assert requests[2]["tools"] == []
        assert requests[2]["messages"][-2] == {
            "role": "tool",
            "tool_name": "builtin_echo",
            "content": "second",
        }
        assert requests[2]["messages"][-1] == {
            "role": "system",
            "content": TOOL_FINALIZATION_INSTRUCTION,
        }
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_tool_call_after_finalization_has_a_specific_failure_code() -> None:
    first_call = {
        "id": "call_first",
        "function": {"name": "builtin_echo", "arguments": {"text": "stale"}},
    }
    second_call = {
        "id": "call_second",
        "function": {"name": "builtin_echo", "arguments": {"text": "stale"}},
    }
    third_call = {
        "id": "call_third",
        "function": {"name": "builtin_echo", "arguments": {"text": "stale"}},
    }
    responses = [
        _chat_response(
            _ndjson(
                _provider_frame(tool_calls=[call]),
                _provider_frame(done=True, done_reason="stop"),
            )
        )
        for call in (first_call, second_call, third_call)
    ]
    broker, _provider = await _started_broker(responses)
    try:
        message = "never execute a third tool"
        session = broker.new_run(_plan(broker, message))
        await _collect(session.stream_turn(message))
        first_result = OllamaToolResult("call_first", HARNESS_TOOL_ID, "first")
        await _collect(session.stream_turn(message, (first_result,)))
        second_result = OllamaToolResult("call_second", HARNESS_TOOL_ID, "second")
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                session.stream_turn(message, (first_result, second_result))
            )
        assert raised.value.code == "model_tool_loop"
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_later_turn_fails_closed_when_full_transcript_exceeds_token_budget() -> None:
    tool_text = "t" * 8_192
    tool_call = {
        "id": "call_large",
        "function": {
            "name": "builtin_echo",
            "arguments": {"text": tool_text},
        },
    }
    first = _chat_response(
        _ndjson(
            _provider_frame(content="a" * MAX_OUTPUT_BYTES, tool_calls=[tool_call]),
            _provider_frame(done=True, done_reason="stop"),
        )
    )
    broker, provider = await _started_broker([first])
    message = "u" * 8_192
    try:
        session = broker.new_run(_plan(broker, message))
        first_turn = await _collect(session.stream_turn(message))
        assert first_turn[-1].kind == "tool.use"

        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                session.stream_turn(
                    message,
                    (
                        OllamaToolResult(
                            call_id="call_large",
                            tool_id=HARNESS_TOOL_ID,
                            content=tool_text,
                        ),
                    ),
                )
            )
        assert raised.value.code == "model_context_limit"
        assert sum(request.url.path == "/api/chat" for request in provider.requests) == 1
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_normalized_content_frames_are_capped_without_losing_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_module, "CONTENT_COALESCE_SECONDS", 0.0)
    body = _ndjson(
        *(_provider_frame(content="x") for _index in range(200)),
        _provider_frame(done=True, done_reason="stop"),
    )
    broker, _provider = await _started_broker([_chat_response(body)])
    try:
        frames = await _collect(
            broker.new_run(_plan(broker, "hello")).stream_turn("hello")
        )
        content = [frame.payload["text"] for frame in frames if frame.kind == "content"]
        assert len(content) <= MAX_NORMALIZED_CONTENT_FRAMES
        assert "".join(content) == "x" * 200
        assert frames[-1].kind == "stop"
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_json_control_characters_cannot_overflow_a_coalesced_ipc_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_module, "CONTENT_COALESCE_SECONDS", 0.0)
    early = 126
    tail_size = MAX_OUTPUT_BYTES - early
    first_tail = "\0" * (tail_size // 2)
    second_tail = "\0" * (tail_size - len(first_tail))
    body = _ndjson(
        *(_provider_frame(content="x") for _index in range(early)),
        _provider_frame(content=first_tail),
        _provider_frame(content=second_tail),
        _provider_frame(done=True, done_reason="stop"),
    )
    broker, _provider = await _started_broker([_chat_response(body)])
    try:
        frames = await _collect(
            broker.new_run(_plan(broker, "hello")).stream_turn("hello")
        )
        content = [frame.payload["text"] for frame in frames if frame.kind == "content"]
        assert len(content) == MAX_NORMALIZED_CONTENT_FRAMES
        assert "".join(content) == "x" * early + first_tail + second_tail
        for index, text in enumerate(content, start=1):
            encoded = (
                json.dumps(
                    {
                        "internal": "model.response",
                        "version": BROKER_PROTOCOL_VERSION,
                        "request_id": f"model-{index}",
                        "type": "content",
                        "text": text,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            assert len(encoded) <= MAX_BROKER_FRAME_BYTES
    finally:
        await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"not-json\n", "model_protocol_error"),
        (
            _ndjson(_provider_frame(model="wrong-model", done=True, done_reason="stop")),
            "model_protocol_error",
        ),
        (_ndjson(_provider_frame(content="unfinished")), "model_protocol_error"),
        (
            _ndjson(
                _provider_frame(done=True, done_reason="stop"),
                _provider_frame(content="late"),
            ),
            "model_protocol_error",
        ),
        (
            _ndjson(_provider_frame(content="partial", done=True, done_reason="length")),
            "model_output_limit",
        ),
        (
            _ndjson(
                {
                    "model": OLLAMA_MODEL,
                    "message": {"role": "assistant", "content": "done"},
                    "done": True,
                    "done_reason": "stop",
                }
            ),
            "model_protocol_error",
        ),
        (
            _ndjson(
                _provider_frame(content="x" * (MAX_OUTPUT_BYTES + 1)),
                _provider_frame(done=True, done_reason="stop"),
            ),
            "model_output_limit",
        ),
        (b"x" * (MAX_NDJSON_LINE_BYTES + 1), "model_protocol_error"),
        (
            _ndjson(
                *(_provider_frame() for _index in range(MAX_STREAM_FRAMES + 1)),
                _provider_frame(done=True, done_reason="stop"),
            ),
            "model_protocol_error",
        ),
        (
            _ndjson(
                _provider_frame(
                    tool_calls=[
                        {
                            "function": {
                                "name": "unexpected_tool",
                                "arguments": {"text": "hello"},
                            }
                        }
                    ]
                ),
                _provider_frame(done=True, done_reason="stop"),
            ),
            "model_protocol_error",
        ),
    ],
)
async def test_malformed_or_over_budget_stream_fails_closed(
    body: bytes, expected_code: str
) -> None:
    broker, _provider = await _started_broker([_chat_response(body)])
    try:
        session = broker.new_run(_plan(broker, "hello"))
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("hello"))
        assert raised.value.code == expected_code
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_body_level_protocol_errors_release_model_stream_slots() -> None:
    invalid = _provider_frame(done=True, done_reason="stop")
    invalid["message"]["role"] = "user"
    valid = _ndjson(_provider_frame(done=True, done_reason="stop"))
    broker, _provider = await _started_broker(
        [_chat_response(_ndjson(invalid)), _chat_response(_ndjson(invalid)), _chat_response(valid)]
    )
    try:
        for _index in range(MAX_CONCURRENT_MODEL_STREAMS):
            session = broker.new_run(_plan(broker, "hello"))
            with pytest.raises(OllamaBrokerError) as raised:
                await _collect(session.stream_turn("hello"))
            assert raised.value.code == "model_protocol_error"
            assert broker._model_slots._value == MAX_CONCURRENT_MODEL_STREAMS

        final_session = broker.new_run(_plan(broker, "hello"))
        assert await _collect(final_session.stream_turn("hello")) == [
            OllamaFrame(
                "stop",
                {
                    "reason": "end_turn",
                    "usage": {"prompt_eval_count": 17, "eval_count": 5},
                },
            )
        ]
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_redirect_wrong_content_type_and_status_are_not_followed() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "http://127.0.0.1/steal"})

    broker, _provider = await _started_broker([redirect])
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                broker.new_run(_plan(broker, "hello")).stream_turn("hello")
            )
        assert raised.value.code == "model_redirect_rejected"
    finally:
        await broker.close()

    broker, _provider = await _started_broker(
        [_chat_response(b"{}", content_type="application/json")]
    )
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                broker.new_run(_plan(broker, "hello")).stream_turn("hello")
            )
        assert raised.value.code == "model_protocol_error"
    finally:
        await broker.close()

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"provider detail must not escape")

    broker, _provider = await _started_broker([unavailable])
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                broker.new_run(_plan(broker, "hello")).stream_turn("hello")
            )
        assert raised.value.code == "model_unavailable"
        assert raised.value.retryable is True
        assert "provider detail" not in str(raised.value)
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_missing_fixed_model_rejects_qualification() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _json_response({"version": "0.23.2"})
        return _json_response({"models": []})

    broker = OllamaBroker(
        transport=httpx.MockTransport(handler), resolver=_resolver
    )
    with pytest.raises(OllamaBrokerError) as raised:
        await broker.start()
    assert raised.value.code == "model_missing"
    assert broker.qualification is None
    await broker.close()


@pytest.mark.asyncio
async def test_fixed_model_must_advertise_tool_capability() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _json_response({"version": "0.23.2"})
        if request.url.path == "/api/tags":
            return _json_response(
                {
                    "models": [
                        {"name": OLLAMA_MODEL, "digest": DIGEST, "size": 1}
                    ]
                }
            )
        if request.url.path == "/api/show":
            return _json_response({"capabilities": ["completion"]})
        return _json_response({}, status=500)

    broker = OllamaBroker(
        transport=httpx.MockTransport(handler), resolver=_resolver
    )
    with pytest.raises(OllamaBrokerError) as raised:
        await broker.start()
    assert raised.value.code == "model_capability_missing"
    await broker.close()


@pytest.mark.asyncio
async def test_smaller_model_window_drives_operational_budget() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _json_response({"version": "0.23.2"})
        if request.url.path == "/api/tags":
            return _json_response(
                {"models": [{"name": OLLAMA_MODEL, "digest": DIGEST, "size": 1}]}
            )
        if request.url.path == "/api/show":
            return _json_response(
                {
                    "capabilities": ["completion", "tools"],
                    "model_info": {
                        "general.architecture": "small",
                        "small.context_length": 16_384,
                    },
                }
            )
        return _json_response({}, status=500)

    broker = OllamaBroker(transport=httpx.MockTransport(handler), resolver=_resolver)
    qualification = await broker.start()
    try:
        assert qualification.model_profile.native_context_tokens == 16_384
        assert qualification.model_profile.operational_context_tokens == 16_384
        assert qualification.model_profile.max_output_tokens == 2_048
    finally:
        await broker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_info",
    [
        {},
        {"general.architecture": True, "x.context_length": 8_192},
        {"general.architecture": "x", "x.context_length": True},
        {"general.architecture": "x", "x.context_length": "32768"},
        {"general.architecture": "x", "x.context_length": 0},
        {"general.architecture": "x", "other.context_length": 32_768},
        {"general.architecture": "../../x", "../../x.context_length": 32_768},
    ],
)
async def test_invalid_model_context_metadata_fails_qualification(
    model_info: object,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return _json_response({"version": "0.23.2"})
        if request.url.path == "/api/tags":
            return _json_response(
                {"models": [{"name": OLLAMA_MODEL, "digest": DIGEST, "size": 1}]}
            )
        if request.url.path == "/api/show":
            return _json_response(
                {
                    "capabilities": ["completion", "tools"],
                    "model_info": model_info,
                }
            )
        return _json_response({}, status=500)

    broker = OllamaBroker(transport=httpx.MockTransport(handler), resolver=_resolver)
    with pytest.raises(OllamaBrokerError) as raised:
        await broker.start()
    assert raised.value.code == "model_protocol_error"
    await broker.close()


class _HangingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed.set()
        if False:  # pragma: no cover - keeps this an async generator
            yield b""

    async def aclose(self) -> None:
        self.closed.set()


class _FrameThenHangStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            yield _ndjson(_provider_frame(content="partial"))
            await asyncio.Event().wait()
        finally:
            self.closed.set()

    async def aclose(self) -> None:
        self.closed.set()


class _PulsingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield _ndjson(_provider_frame(content="x"))
            await asyncio.sleep(0.005)


def _stream_factory(stream: httpx.AsyncByteStream) -> ChatFactory:
    def response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            stream=stream,
        )

    return response


def _set_session_timeouts(session: object, **changes: object) -> None:
    entry = session._catalog_entry  # type: ignore[attr-defined]
    session._catalog_entry = replace(  # type: ignore[attr-defined]
        entry,
        timeouts=replace(entry.timeouts, **changes),
    )


@pytest.mark.asyncio
async def test_first_frame_timeout_retries_once_before_any_provider_output() -> None:
    hanging = _HangingStream()
    success = _chat_response(
        _ndjson(
            _provider_frame(content="recovered"),
            _provider_frame(done=True, done_reason="stop"),
        )
    )
    broker, provider = await _started_broker([_stream_factory(hanging), success])
    try:
        session = broker.new_run(_plan(broker, "retry first frame"))
        _set_session_timeouts(
            session,
            first_frame_seconds=0.02,
            stream_idle_seconds=0.02,
            turn_seconds=0.2,
        )
        frames = await _collect(session.stream_turn("retry first frame"))
        assert [frame.kind for frame in frames] == ["content", "stop"]
        assert frames[0].payload == {"text": "recovered"}
        assert len(
            [request for request in provider.requests if request.url.path == "/api/chat"]
        ) == 2
        await asyncio.wait_for(hanging.closed.wait(), timeout=1.0)
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_stream_idle_timeout_never_retries_after_provider_output() -> None:
    stalled = _FrameThenHangStream()
    broker, provider = await _started_broker([_stream_factory(stalled)])
    try:
        session = broker.new_run(_plan(broker, "stall after output"))
        _set_session_timeouts(
            session,
            first_frame_seconds=0.1,
            stream_idle_seconds=0.02,
            turn_seconds=0.2,
        )
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("stall after output"))
        assert raised.value.code == "model_stream_idle_timeout"
        assert raised.value.retryable is False
        assert len(
            [request for request in provider.requests if request.url.path == "/api/chat"]
        ) == 1
        await asyncio.wait_for(stalled.closed.wait(), timeout=1.0)
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_total_turn_deadline_stops_an_active_nonidle_stream() -> None:
    broker, provider = await _started_broker([_stream_factory(_PulsingStream())])
    try:
        session = broker.new_run(_plan(broker, "bounded active stream"))
        _set_session_timeouts(
            session,
            first_frame_seconds=0.02,
            stream_idle_seconds=0.02,
            turn_seconds=0.04,
        )
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("bounded active stream"))
        assert raised.value.code == "model_turn_deadline"
        assert raised.value.retryable is False
        assert len(
            [request for request in provider.requests if request.url.path == "/api/chat"]
        ) == 1
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_cancel_callback_closes_blocked_provider_stream() -> None:
    hanging = _HangingStream()

    def response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            stream=hanging,
        )

    broker, _provider = await _started_broker([response])
    cancelled = False
    try:
        session = broker.new_run(_plan(broker, "wait"))
        task = asyncio.create_task(
            _collect(session.stream_turn("wait", is_cancelled=lambda: cancelled))
        )
        await asyncio.wait_for(hanging.started.wait(), timeout=1.0)
        cancelled = True
        with pytest.raises(OllamaCancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        await asyncio.wait_for(hanging.closed.wait(), timeout=1.0)
        assert session.messages == ()
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_cancel_callback_interrupts_the_bounded_model_queue() -> None:
    broker, _provider = await _started_broker([])
    cancelled = False
    acquired = 0
    try:
        for _index in range(MAX_CONCURRENT_MODEL_STREAMS):
            await broker._model_slots.acquire()
            acquired += 1
        session = broker.new_run(_plan(broker, "queued"))
        task = asyncio.create_task(
            _collect(session.stream_turn("queued", is_cancelled=lambda: cancelled))
        )
        await asyncio.sleep(0.06)
        cancelled = True
        with pytest.raises(OllamaCancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        for _index in range(acquired):
            broker._model_slots.release()
        await broker.close()


@pytest.mark.asyncio
async def test_bounded_model_queue_times_out_with_retryable_busy(
) -> None:
    broker, _provider = await _started_broker([])
    acquired = 0
    try:
        for _index in range(MAX_CONCURRENT_MODEL_STREAMS):
            await broker._model_slots.acquire()
            acquired += 1
        session = broker.new_run(_plan(broker, "queued"))
        session._catalog_entry = replace(
            session._catalog_entry,
            timeouts=replace(session._catalog_entry.timeouts, queue_seconds=0.02),
        )
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("queued"))
        assert raised.value.code == "model_busy"
        assert raised.value.retryable is True
    finally:
        for _index in range(acquired):
            broker._model_slots.release()
        await broker.close()


@pytest.mark.asyncio
async def test_model_stream_concurrency_never_exceeds_two() -> None:
    hanging = [_HangingStream(), _HangingStream()]

    def response(stream: _HangingStream) -> ChatFactory:
        def build(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/x-ndjson"},
                stream=stream,
            )

        return build

    broker, provider = await _started_broker([response(item) for item in hanging])
    cancelled = [False, False, False]
    try:
        sessions = [
            broker.new_run(_plan(broker, f"run-{index}")) for index in range(3)
        ]
        tasks = [
            asyncio.create_task(
                _collect(
                    session.stream_turn(
                        f"run-{index}",
                        is_cancelled=lambda index=index: cancelled[index],
                    )
                )
            )
            for index, session in enumerate(sessions)
        ]
        await asyncio.gather(*(item.started.wait() for item in hanging))
        await asyncio.sleep(0.02)
        assert sum(request.url.path == "/api/chat" for request in provider.requests) == 2

        cancelled[:] = [True, True, True]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(item, OllamaCancelledError) for item in results)
        assert broker._model_slots._value == MAX_CONCURRENT_MODEL_STREAMS
    finally:
        cancelled[:] = [True, True, True]
        await broker.close()


@pytest.mark.asyncio
async def test_transport_error_releases_slot_for_the_next_request() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated transport failure", request=request)

    valid = _chat_response(_ndjson(_provider_frame(done=True, done_reason="stop")))
    broker, _provider = await _started_broker([broken, valid])
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(
                broker.new_run(_plan(broker, "first")).stream_turn("first")
            )
        assert raised.value.code == "model_unavailable"
        assert broker._model_slots._value == MAX_CONCURRENT_MODEL_STREAMS

        frames = await _collect(
            broker.new_run(_plan(broker, "second")).stream_turn("second")
        )
        assert frames[-1].kind == "stop"
        assert broker._model_slots._value == MAX_CONCURRENT_MODEL_STREAMS
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_tool_result_must_match_pending_call() -> None:
    tool_call = {
        "id": "call_expected",
        "function": {
            "index": 0,
            "name": "builtin_echo",
            "arguments": {"text": "hello"},
        },
    }
    broker, _provider = await _started_broker(
        [
            _chat_response(
                _ndjson(
                    _provider_frame(tool_calls=[tool_call]),
                    _provider_frame(done=True, done_reason="stop"),
                )
            )
        ]
    )
    try:
        session = broker.new_run(_plan(broker, "hello"))
        await _collect(session.stream_turn("hello"))
        wrong = OllamaToolResult("call_wrong", HARNESS_TOOL_ID, "hello")
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("hello", (wrong,)))
        assert raised.value.code == "model_state_error"
    finally:
        await broker.close()

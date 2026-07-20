"""Contract tests for the fixed-target trusted Ollama broker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import hashlib
import json
import socket
from typing import Any

import httpx
import pytest

import agent_builder_v2.ollama as ollama_module
from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import ContextCompiler, estimate_provider_input_tokens
from agent_builder_v2.model import BROKER_PROTOCOL_VERSION, MAX_BROKER_FRAME_BYTES
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
        assert qualification.model_profile.max_output_tokens == 1_024
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_module, "MODEL_QUEUE_TIMEOUT_SECONDS", 0.02)
    broker, _provider = await _started_broker([])
    acquired = 0
    try:
        for _index in range(MAX_CONCURRENT_MODEL_STREAMS):
            await broker._model_slots.acquire()
            acquired += 1
        session = broker.new_run(_plan(broker, "queued"))
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

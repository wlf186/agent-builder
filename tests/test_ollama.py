"""Contract tests for the fixed-target trusted Ollama broker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
import socket
from typing import Any

import httpx
import pytest

from agent_builder_v2.ollama import (
    HARNESS_TOOL_ID,
    MAX_NDJSON_LINE_BYTES,
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
    OllamaToolResult,
)


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
                {"capabilities": ["completion", "vision", "tools", "thinking"]}
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
        session = broker.new_run()
        tool_frames = await _collect(session.stream_turn("please echo hello"))
        assert tool_frames == [
            OllamaFrame(
                "tool.use",
                {
                    "call_id": "call_provider_1",
                    "tool_id": HARNESS_TOOL_ID,
                    "arguments": {"text": "hello"},
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
            session.stream_turn("please echo hello", (result,))
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
        for body in (first_body, second_body):
            assert body["model"] == OLLAMA_MODEL
            assert body["stream"] is True
            assert body["think"] is False
            assert body["options"]["temperature"] == 0
            assert body["options"]["num_predict"] == MODEL_NUM_PREDICT
            assert body["tools"][0]["function"]["name"] == "builtin_echo"
        assert second_body["messages"][-2:] == [
            {"role": "assistant", "content": "", "tool_calls": [provider_tool_call]},
            {"role": "tool", "tool_name": "builtin_echo", "content": "hello"},
        ]
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
        session = broker.new_run()
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("hello"))
        assert raised.value.code == expected_code
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_redirect_wrong_content_type_and_status_are_not_followed() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "http://127.0.0.1/steal"})

    broker, _provider = await _started_broker([redirect])
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(broker.new_run().stream_turn("hello"))
        assert raised.value.code == "model_redirect_rejected"
    finally:
        await broker.close()

    broker, _provider = await _started_broker(
        [_chat_response(b"{}", content_type="application/json")]
    )
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(broker.new_run().stream_turn("hello"))
        assert raised.value.code == "model_protocol_error"
    finally:
        await broker.close()

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"provider detail must not escape")

    broker, _provider = await _started_broker([unavailable])
    try:
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(broker.new_run().stream_turn("hello"))
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
        session = broker.new_run()
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
        session = broker.new_run()
        await _collect(session.stream_turn("hello"))
        wrong = OllamaToolResult("call_wrong", HARNESS_TOOL_ID, "hello")
        with pytest.raises(OllamaBrokerError) as raised:
            await _collect(session.stream_turn("hello", (wrong,)))
        assert raised.value.code == "model_state_error"
    finally:
        await broker.close()

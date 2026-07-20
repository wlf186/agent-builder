"""Trusted, fixed-target Ollama broker for the Harness V2 prototype.

This module deliberately belongs on the trusted control-plane side of the
Worker capability boundary.  A Run Worker receives normalized frames over an
inherited IPC capability; it never receives this HTTP client, the provider
address, or a general network socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
import socket
from typing import Any
from uuid import uuid4

import httpx

from .context import (
    MAX_NATIVE_CONTEXT_TOKENS,
    ContextPlan,
    ContextPlanError,
    ModelProfile,
    estimate_provider_input_tokens,
)
from .model import MAX_BROKER_FRAME_BYTES, MAX_BROKER_RESPONSE_FRAMES
from .tools import PROTOTYPE_ECHO_SPEC, ToolSpec, prototype_tool_specs


OLLAMA_HOST = "iollama"
OLLAMA_PORT = 11434
OLLAMA_MODEL = "qwen3.5:2b"
OLLAMA_TOOL_NAME = PROTOTYPE_ECHO_SPEC.provider_name
HARNESS_TOOL_ID = PROTOTYPE_ECHO_SPEC.tool_id

MAX_USER_BYTES = 8_192
MAX_METADATA_REQUEST_BYTES = 64 * 1024
MAX_QUALIFICATION_BYTES = 1024 * 1024
MAX_NDJSON_LINE_BYTES = 64 * 1024
MAX_STREAM_BYTES = 512 * 1024
MAX_STREAM_FRAMES = 4_096
MAX_OUTPUT_BYTES = 12_288
MAX_MODEL_TURNS = 8
MAX_CONCURRENT_MODEL_STREAMS = 2
MODEL_QUEUE_TIMEOUT_SECONDS = 30.0
RUNTIME_CONTEXT_TOKEN_CAP = 32_768
MODEL_NUM_PREDICT = 2_048
CONTENT_COALESCE_BYTES = 256
CONTENT_COALESCE_SECONDS = 0.05
MAX_NORMALIZED_CONTENT_FRAMES = MAX_BROKER_RESPONSE_FRAMES - 1
MAX_TAIL_CONTENT_FRAMES = 2
MAX_CONTENT_JSON_STRING_BYTES = MAX_BROKER_FRAME_BYTES - 16 * 1024
CANCEL_POLL_SECONDS = 0.05
QUALIFICATION_TIMEOUT_SECONDS = 8.0
TURN_TIMEOUT_SECONDS = 25.0
REQUEST_DIGEST_DOMAIN = b"agent-builder-ollama-request-v1\0"

_SAFE_CALL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ARCHITECTURE = re.compile(r"^[a-z0-9._-]{1,64}$")


class OllamaBrokerError(RuntimeError):
    """Bounded provider failure safe to translate into a Run error."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OllamaCancelledError(OllamaBrokerError):
    def __init__(self) -> None:
        super().__init__("model_cancelled", "The model request was cancelled.")


@dataclass(frozen=True)
class OllamaFrame:
    """Provider-neutral frame suitable for a bounded Worker IPC message."""

    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class OllamaToolResult:
    """One normalized result returned by the Kernel to the model capability."""

    call_id: str
    tool_id: str
    content: str
    outcome: str = "succeeded"


@dataclass(frozen=True, slots=True)
class OllamaRequestMetadata:
    """Bounded metadata for one exact provider request, never its raw body."""

    iteration: int
    message_count: int
    tool_count: int
    estimated_input_tokens: int
    request_bytes: int
    request_digest: str


@dataclass(frozen=True)
class OllamaQualification:
    version: str
    model: str
    digest: str
    size: int
    address: str
    model_profile: ModelProfile


@dataclass(frozen=True)
class _PendingTool:
    call_id: str
    tool_id: str
    assistant_call: dict[str, Any]


Resolver = Callable[..., list[tuple[Any, ...]]]
CancelCheck = Callable[[], bool]
RequestObserver = Callable[[OllamaRequestMetadata], Awaitable[None]]


def _bounded_text(value: object, maximum_bytes: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise OllamaBrokerError("model_protocol_error", "Invalid bounded text.")
    encoded = value.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise OllamaBrokerError("model_protocol_error", "Bounded text is too large.")
    return value


def _split_content_for_ipc(value: str) -> tuple[str, ...]:
    """Split text by its conservative JSON-string encoding size."""

    if not value:
        return ()
    chunks: list[str] = []
    characters: list[str] = []
    encoded_bytes = 0
    for character in value:
        if ord(character) < 0x20:
            character_bytes = 6
        elif character in {'"', "\\"}:
            character_bytes = 2
        else:
            try:
                character_bytes = len(character.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama returned invalid Unicode content."
                ) from exc
        if characters and (
            encoded_bytes + character_bytes > MAX_CONTENT_JSON_STRING_BYTES
        ):
            chunks.append("".join(characters))
            characters.clear()
            encoded_bytes = 0
        characters.append(character)
        encoded_bytes += character_bytes
    if characters:
        chunks.append("".join(characters))
    return tuple(chunks)


def _native_context_tokens(show_value: dict[str, Any]) -> int:
    model_info = show_value.get("model_info")
    if not isinstance(model_info, dict) or len(model_info) > 4_096:
        raise OllamaBrokerError(
            "model_protocol_error", "Ollama returned invalid model capabilities."
        )
    architecture = model_info.get("general.architecture")
    if not isinstance(architecture, str) or _ARCHITECTURE.fullmatch(architecture) is None:
        raise OllamaBrokerError(
            "model_protocol_error", "Ollama returned an invalid model architecture."
        )
    value = model_info.get(f"{architecture}.context_length")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 4_096 <= value <= MAX_NATIVE_CONTEXT_TOKENS
    ):
        raise OllamaBrokerError(
            "model_protocol_error", "Ollama returned an invalid context window."
        )
    return value


def _model_profile(*, digest: str, native_context_tokens: int) -> ModelProfile:
    operational_context_tokens = min(
        native_context_tokens, RUNTIME_CONTEXT_TOKEN_CAP
    )
    output_tokens = min(
        MODEL_NUM_PREDICT,
        max(256, operational_context_tokens // 16),
    )
    try:
        return ModelProfile(
            provider="ollama",
            model=OLLAMA_MODEL,
            model_digest=digest,
            native_context_tokens=native_context_tokens,
            operational_context_tokens=operational_context_tokens,
            max_output_tokens=output_tokens,
            profile_source="ollama-show+runtime-cap-v1",
        )
    except ContextPlanError as exc:
        raise OllamaBrokerError(
            "model_protocol_error", "The qualified model profile is invalid."
        ) from exc


def _validated_address(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError as exc:
        raise OllamaBrokerError(
            "model_endpoint_rejected", "The fixed model host resolved unsafely."
        ) from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise OllamaBrokerError(
            "model_endpoint_rejected", "The fixed model host resolved unsafely."
        )
    return address


class OllamaBroker:
    """Own the sole fixed Ollama client and create isolated per-Run sessions."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
    ) -> None:
        # Endpoint, model, path, and generation options are intentionally not
        # constructor parameters.  In particular, no Worker frame can turn this
        # broker into an SSRF primitive or select an expensive provider model.
        self._transport = transport
        self._resolver = resolver
        self._tool_catalog = {
            spec.tool_id: spec for spec in prototype_tool_specs()
        }
        self._client: httpx.AsyncClient | None = None
        self._qualification: OllamaQualification | None = None
        self._closed = False
        self._model_slots = asyncio.Semaphore(MAX_CONCURRENT_MODEL_STREAMS)

    @property
    def qualification(self) -> OllamaQualification | None:
        return self._qualification

    async def start(self) -> OllamaQualification:
        if self._closed:
            raise OllamaBrokerError("model_broker_closed", "The model broker is closed.")
        if self._qualification is not None:
            return self._qualification

        try:
            address_info = await asyncio.to_thread(
                self._resolver,
                OLLAMA_HOST,
                OLLAMA_PORT,
                0,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise OllamaBrokerError(
                "model_unavailable",
                "The fixed model host could not be resolved.",
                retryable=True,
            ) from exc

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for item in address_info:
            if len(item) < 5 or item[0] not in {socket.AF_INET, socket.AF_INET6}:
                continue
            sockaddr = item[4]
            if not isinstance(sockaddr, tuple) or not sockaddr:
                raise OllamaBrokerError(
                    "model_endpoint_rejected", "The fixed model host resolved unsafely."
                )
            addresses.append(_validated_address(str(sockaddr[0])))
        if not addresses:
            raise OllamaBrokerError(
                "model_unavailable",
                "The fixed model host has no usable address.",
                retryable=True,
            )

        # Reject the complete resolution result if any address is unsafe, then
        # pin one validated numeric address for the lifetime of this client.
        address = addresses[0]
        rendered = f"[{address}]" if address.version == 6 else str(address)
        base_url = f"http://{rendered}:{OLLAMA_PORT}"
        transport = self._transport or httpx.AsyncHTTPTransport(retries=0)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            headers={"Host": f"{OLLAMA_HOST}:{OLLAMA_PORT}"},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )
        try:
            async with asyncio.timeout(QUALIFICATION_TIMEOUT_SECONDS):
                version_value = await self._bounded_json("GET", "/api/version", 4_096)
                tags_value = await self._bounded_json(
                    "GET", "/api/tags", MAX_QUALIFICATION_BYTES
                )
                show_value = await self._bounded_json(
                    "POST",
                    "/api/show",
                    MAX_QUALIFICATION_BYTES,
                    content=json.dumps(
                        {"model": OLLAMA_MODEL}, separators=(",", ":")
                    ).encode("utf-8"),
                )
            version = _bounded_text(
                version_value.get("version") if isinstance(version_value, dict) else None,
                64,
                allow_empty=False,
            )
            models = tags_value.get("models") if isinstance(tags_value, dict) else None
            if not isinstance(models, list) or len(models) > 4_096:
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama returned an invalid model catalog."
                )
            selected: dict[str, Any] | None = None
            for candidate in models:
                if isinstance(candidate, dict) and candidate.get("name") == OLLAMA_MODEL:
                    selected = candidate
                    break
            if selected is None:
                raise OllamaBrokerError(
                    "model_missing", "The fixed Ollama model is not installed."
                )
            capabilities = (
                show_value.get("capabilities")
                if isinstance(show_value, dict)
                else None
            )
            if (
                not isinstance(capabilities, list)
                or len(capabilities) > 64
                or any(not isinstance(item, str) for item in capabilities)
                or not {"completion", "tools"}.issubset(capabilities)
            ):
                raise OllamaBrokerError(
                    "model_capability_missing",
                    "The fixed Ollama model lacks required capabilities.",
                )
            digest = selected.get("digest")
            size = selected.get("size")
            if (
                not isinstance(digest, str)
                or _DIGEST.fullmatch(digest) is None
                or not isinstance(size, int)
                or isinstance(size, bool)
                or not 0 < size <= 1024**4
            ):
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama returned invalid model metadata."
                )
            native_context_tokens = _native_context_tokens(show_value)
            model_profile = _model_profile(
                digest=digest,
                native_context_tokens=native_context_tokens,
            )
            self._qualification = OllamaQualification(
                version=version,
                model=OLLAMA_MODEL,
                digest=digest,
                size=size,
                address=str(address),
                model_profile=model_profile,
            )
            return self._qualification
        except OllamaBrokerError:
            await self._discard_client()
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            await self._discard_client()
            raise OllamaBrokerError(
                "model_timeout", "Ollama qualification timed out.", retryable=True
            ) from exc
        except httpx.RequestError as exc:
            await self._discard_client()
            raise OllamaBrokerError(
                "model_unavailable", "Ollama qualification failed.", retryable=True
            ) from exc

    async def _bounded_json(
        self,
        method: str,
        path: str,
        maximum_bytes: int,
        *,
        content: bytes | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        headers = {"Accept": "application/json"}
        if content is not None:
            if len(content) > MAX_METADATA_REQUEST_BYTES:
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama metadata request is too large."
                )
            headers["Content-Type"] = "application/json"
        async with client.stream(
            method, path, headers=headers, content=content
        ) as response:
            if response.is_redirect:
                raise OllamaBrokerError(
                    "model_redirect_rejected", "Ollama returned a redirect."
                )
            if response.status_code != 200:
                raise OllamaBrokerError(
                    "model_unavailable",
                    "Ollama qualification returned an error.",
                    retryable=response.status_code in {429, 502, 503, 504},
                )
            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama returned an invalid content type."
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > maximum_bytes:
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama metadata exceeded its limit."
                    )
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned invalid JSON."
            ) from exc
        if not isinstance(value, dict):
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned an invalid JSON object."
            )
        return value

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> OllamaRunSession:
        if self._qualification is None:
            raise OllamaBrokerError(
                "model_broker_not_ready", "The model broker is not qualified."
            )
        if (
            not isinstance(max_tool_calls, int)
            or isinstance(max_tool_calls, bool)
            or not 1 <= max_tool_calls <= 8
        ):
            raise ValueError("invalid model Tool-call budget")
        return OllamaRunSession(self, context_plan, max_tool_calls=max_tool_calls)

    async def close(self) -> None:
        self._closed = True
        self._qualification = None
        await self._discard_client()

    async def _discard_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise OllamaBrokerError(
                "model_broker_not_ready", "The model broker is not qualified."
            )
        return self._client


class OllamaRunSession:
    """Conversation state that is never shared between Runs."""

    def __init__(
        self,
        broker: OllamaBroker,
        context_plan: ContextPlan,
        *,
        max_tool_calls: int,
    ) -> None:
        qualification = broker.qualification
        catalog = broker._tool_catalog
        if not isinstance(context_plan, ContextPlan):
            raise OllamaBrokerError(
                "model_context_invalid", "The trusted context plan is not executable."
            )
        exposed_tools_are_allowed = all(
            catalog.get(spec.tool_id) == spec for spec in context_plan.tools
        )
        if (
            qualification is None
            or context_plan.model_profile != qualification.model_profile
            or not exposed_tools_are_allowed
        ):
            raise OllamaBrokerError(
                "model_context_invalid", "The trusted context plan is not executable."
            )
        self._broker = broker
        self._context_plan = context_plan
        self._tools_by_id: dict[str, ToolSpec] = {
            spec.tool_id: spec for spec in context_plan.tools
        }
        self._tools_by_provider: dict[str, ToolSpec] = {
            spec.provider_name: spec for spec in context_plan.tools
        }
        if len(self._tools_by_id) != len(context_plan.tools) or len(
            self._tools_by_provider
        ) != len(context_plan.tools):
            raise OllamaBrokerError(
                "model_context_invalid", "The trusted Tool set is ambiguous."
            )
        self._messages: list[dict[str, Any]] = []
        self._user_message: str | None = None
        self._applied_results: tuple[OllamaToolResult, ...] = ()
        self._pending_tool: _PendingTool | None = None
        self._seen_call_ids: set[str] = set()
        self._turns = 0
        self._in_flight = False
        self._stopped = False
        self._max_tool_calls = max_tool_calls

    @property
    def messages(self) -> tuple[dict[str, Any], ...]:
        # Tests and trusted diagnostics may inspect a defensive JSON-like copy;
        # callers cannot mutate the Run's actual provider transcript.
        return tuple(json.loads(json.dumps(item)) for item in self._messages)

    async def stream_turn(
        self,
        user_message: str,
        tool_results: Sequence[OllamaToolResult] = (),
        is_cancelled: CancelCheck = lambda: False,
        on_request: RequestObserver | None = None,
    ) -> AsyncIterator[OllamaFrame]:
        if self._in_flight:
            raise OllamaBrokerError(
                "model_concurrency_error", "A model turn is already active for this Run."
            )
        if self._stopped:
            raise OllamaBrokerError(
                "model_state_error", "The model conversation has already stopped."
            )
        if not callable(is_cancelled):
            raise TypeError("is_cancelled must be callable")
        if on_request is not None and not callable(on_request):
            raise TypeError("on_request must be callable")
        message = _bounded_text(user_message, MAX_USER_BYTES, allow_empty=False)
        if message != self._context_plan.user_message():
            raise OllamaBrokerError(
                "model_context_invalid", "The user turn does not match its context plan."
            )
        if self._user_message is not None and message != self._user_message:
            raise OllamaBrokerError(
                "model_state_error", "The Run user message changed between model turns."
            )
        if self._turns >= MAX_MODEL_TURNS:
            raise OllamaBrokerError(
                "model_iteration_limit", "The model turn budget was exhausted."
            )
        if len(tool_results) > MAX_MODEL_TURNS:
            raise OllamaBrokerError(
                "model_state_error", "The Tool result history exceeded its limit."
            )
        validated_results = tuple(self._validate_tool_result(item) for item in tool_results)
        if (
            len(validated_results) < len(self._applied_results)
            or validated_results[: len(self._applied_results)] != self._applied_results
        ):
            raise OllamaBrokerError(
                "model_state_error", "Tool result history changed between model turns."
            )
        new_results = validated_results[len(self._applied_results) :]

        candidate_messages = [json.loads(json.dumps(item)) for item in self._messages]
        if self._user_message is None:
            if validated_results:
                raise OllamaBrokerError(
                    "model_state_error", "A first model turn cannot have Tool results."
                )
            candidate_messages.extend(self._context_plan.provider_messages())
        else:
            pending = self._pending_tool
            if pending is None or len(new_results) != 1:
                raise OllamaBrokerError(
                    "model_state_error", "The model turn has no matching Tool result."
                )
            result = new_results[0]
            if result.call_id != pending.call_id or result.tool_id != pending.tool_id:
                raise OllamaBrokerError(
                    "model_state_error", "The Tool result does not match its call."
                )
            spec = self._tools_by_id[pending.tool_id]
            candidate_messages.append(
                {
                    "role": "tool",
                    "tool_name": spec.provider_name,
                    "content": result.content,
                }
            )

        profile = self._context_plan.model_profile
        # The immutable Tool set stays available until this Run's frozen call
        # budget is consumed; then the provider capability is narrowed to zero.
        available_tools = (
            self._context_plan.tools
            if len(validated_results) < self._max_tool_calls
            else ()
        )
        try:
            runtime_input_tokens = estimate_provider_input_tokens(
                candidate_messages, available_tools
            )
        except ContextPlanError as exc:
            raise OllamaBrokerError(
                "model_context_limit", "The model context could not be bounded."
            ) from exc
        if runtime_input_tokens > self._context_plan.policy.hard_input_tokens:
            raise OllamaBrokerError(
                "model_context_limit", "The model context exceeded its token budget."
            )
        request = {
            "model": profile.model,
            "messages": candidate_messages,
            "tools": [spec.ollama_definition() for spec in available_tools],
            "stream": True,
            "think": False,
            "keep_alive": "5m",
            "options": {
                "temperature": 0,
                "seed": 0,
                "num_ctx": profile.operational_context_tokens,
                "num_predict": profile.max_output_tokens,
            },
        }
        encoded_request = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded_request) > profile.request_byte_budget:
            raise OllamaBrokerError(
                "model_context_limit", "The model request exceeded its byte budget."
            )

        self._in_flight = True
        result: AsyncIterator[dict[str, Any]] | None = None
        try:
            if on_request is not None:
                # Occupy the session before this await so a slow observer cannot
                # let a second stream pass the single-flight guard.  Callback
                # failure reaches the same finally path without consuming a
                # model iteration or opening provider HTTP.
                await on_request(
                    OllamaRequestMetadata(
                        iteration=self._turns + 1,
                        message_count=len(candidate_messages),
                        tool_count=len(available_tools),
                        estimated_input_tokens=runtime_input_tokens,
                        request_bytes=len(encoded_request),
                        request_digest=hashlib.sha256(
                            REQUEST_DIGEST_DOMAIN + encoded_request
                        ).hexdigest(),
                    )
                )
            self._turns += 1
            result = self._stream_response(encoded_request, is_cancelled)
            content_parts: list[str] = []
            coalesced: list[str] = []
            coalesced_bytes = 0
            content_frames = 0
            output_bytes = 0
            provider_call: dict[str, Any] | None = None
            final_frame: dict[str, Any] | None = None
            last_flush = asyncio.get_running_loop().time()

            async for raw_frame in result:
                message_value = raw_frame.get("message")
                if not isinstance(message_value, dict) or message_value.get("role") != "assistant":
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama returned an invalid message."
                    )
                content = message_value.get("content", "")
                if not isinstance(content, str):
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama returned invalid content."
                    )
                thinking = message_value.get("thinking", "")
                if thinking is not None and not isinstance(thinking, str):
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama returned invalid thinking data."
                    )
                images = message_value.get("images")
                if images not in (None, []):
                    raise OllamaBrokerError(
                        "model_protocol_error", "Unexpected model image output."
                    )
                tool_calls = message_value.get("tool_calls", [])
                if tool_calls is None:
                    tool_calls = []
                if not isinstance(tool_calls, list):
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama returned invalid Tool calls."
                    )
                for call in tool_calls:
                    if not available_tools:
                        raise OllamaBrokerError(
                            "model_protocol_error",
                            "Ollama requested a Tool after capabilities were narrowed.",
                        )
                    if provider_call is not None:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Only one Tool call is allowed per turn."
                        )
                    provider_call = self._normalize_tool_call(call)

                if content:
                    try:
                        encoded_content = content.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        raise OllamaBrokerError(
                            "model_protocol_error",
                            "Ollama returned invalid Unicode content.",
                        ) from exc
                    output_bytes += len(encoded_content)
                    if output_bytes > MAX_OUTPUT_BYTES:
                        raise OllamaBrokerError(
                            "model_output_limit", "Model output exceeded its byte budget."
                        )
                    content_parts.append(content)
                    coalesced.append(content)
                    coalesced_bytes += len(encoded_content)
                    now = asyncio.get_running_loop().time()
                    should_flush = (
                        coalesced_bytes >= CONTENT_COALESCE_BYTES
                        or now - last_flush >= CONTENT_COALESCE_SECONDS
                    )
                    if should_flush:
                        candidate_chunks = _split_content_for_ipc(
                            "".join(coalesced)
                        )
                        if (
                            content_frames + len(candidate_chunks)
                            <= MAX_NORMALIZED_CONTENT_FRAMES
                            - MAX_TAIL_CONTENT_FRAMES
                        ):
                            for chunk in candidate_chunks:
                                yield OllamaFrame("content", {"text": chunk})
                                content_frames += 1
                            coalesced.clear()
                            coalesced_bytes = 0
                            last_flush = now

                if raw_frame["done"]:
                    if final_frame is not None:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Ollama returned duplicate terminal frames."
                        )
                    final_frame = raw_frame

            if final_frame is None:
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama ended without a terminal frame."
                )
            done_reason = final_frame.get("done_reason")
            if done_reason == "length":
                raise OllamaBrokerError(
                    "model_output_limit", "Ollama exhausted the output token budget."
                )
            if done_reason != "stop":
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama returned an invalid stop reason."
                )
            if is_cancelled():
                raise OllamaCancelledError()
            if coalesced:
                for chunk in _split_content_for_ipc("".join(coalesced)):
                    yield OllamaFrame("content", {"text": chunk})
                    content_frames += 1
            if content_frames > MAX_NORMALIZED_CONTENT_FRAMES:
                raise OllamaBrokerError(
                    "model_protocol_error", "Normalized model output exceeded its frame limit."
                )

            usage: dict[str, int] = {}
            for field in ("prompt_eval_count", "eval_count"):
                value = final_frame.get(field)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 0 <= value <= 1_000_000_000
                ):
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama returned invalid usage data."
                    )
                usage[field] = value

            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(content_parts),
            }
            if provider_call is not None:
                assistant["tool_calls"] = [provider_call]
            candidate_messages.append(assistant)
            self._messages = candidate_messages
            self._user_message = message
            self._applied_results = validated_results
            self._pending_tool = None

            if provider_call is not None:
                call_id = str(provider_call["id"])
                provider_name = str(provider_call["function"]["name"])
                spec = self._tools_by_provider[provider_name]
                self._pending_tool = _PendingTool(call_id, spec.tool_id, provider_call)
                yield OllamaFrame(
                    "tool.use",
                    {
                        "call_id": call_id,
                        "tool_id": spec.tool_id,
                        "arguments": provider_call["function"]["arguments"],
                        "usage": usage,
                    },
                )
            else:
                self._stopped = True
                yield OllamaFrame("stop", {"reason": "end_turn", "usage": usage})
        finally:
            if result is not None:
                await result.aclose()
            self._in_flight = False

    async def _stream_response(
        self, encoded_request: bytes, is_cancelled: CancelCheck
    ) -> AsyncIterator[dict[str, Any]]:
        if is_cancelled():
            raise OllamaCancelledError()
        client = self._broker._require_client()
        loop = asyncio.get_running_loop()
        queue_deadline = loop.time() + MODEL_QUEUE_TIMEOUT_SECONDS
        while True:
            if is_cancelled():
                raise OllamaCancelledError()
            remaining = queue_deadline - loop.time()
            if remaining <= 0:
                raise OllamaBrokerError(
                    "model_busy", "The bounded model queue is full.", retryable=True
                )
            try:
                await asyncio.wait_for(
                    self._broker._model_slots.acquire(),
                    timeout=min(CANCEL_POLL_SECONDS, remaining),
                )
                break
            except TimeoutError:
                continue
        try:
            if is_cancelled():
                raise OllamaCancelledError()
            async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                async with client.stream(
                    "POST",
                    "/api/chat",
                    content=encoded_request,
                    headers={
                        "Accept": "application/x-ndjson",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    if response.is_redirect:
                        raise OllamaBrokerError(
                            "model_redirect_rejected", "Ollama returned a redirect."
                        )
                    if response.status_code != 200:
                        code = (
                            "model_missing"
                            if response.status_code == 404
                            else "model_unavailable"
                        )
                        raise OllamaBrokerError(
                            code,
                            "Ollama returned an error status.",
                            retryable=response.status_code in {429, 502, 503, 504},
                        )
                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if media_type.strip().lower() != "application/x-ndjson":
                        raise OllamaBrokerError(
                            "model_protocol_error",
                            "Ollama returned an invalid streaming content type.",
                        )
                    seen_done = False
                    async for frame in self._iter_ndjson(response, is_cancelled):
                        if frame.get("model") != self._context_plan.model_profile.model:
                            raise OllamaBrokerError(
                                "model_protocol_error", "Ollama returned the wrong model."
                            )
                        done = frame.get("done")
                        if not isinstance(done, bool):
                            raise OllamaBrokerError(
                                "model_protocol_error", "Ollama returned an invalid done flag."
                            )
                        if seen_done:
                            raise OllamaBrokerError(
                                "model_protocol_error", "Ollama returned data after completion."
                            )
                        if done:
                            seen_done = True
                        elif frame.get("done_reason") is not None:
                            raise OllamaBrokerError(
                                "model_protocol_error", "Ollama stopped out of sequence."
                            )
                        yield frame
                    if not seen_done:
                        raise OllamaBrokerError(
                            "model_protocol_error", "Ollama ended without completion."
                        )
        except OllamaBrokerError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise OllamaBrokerError(
                "model_timeout", "The model request timed out.", retryable=True
            ) from exc
        except httpx.RequestError as exc:
            raise OllamaBrokerError(
                "model_unavailable", "The model request failed.", retryable=True
            ) from exc
        finally:
            self._broker._model_slots.release()

    async def _iter_ndjson(
        self, response: httpx.Response, is_cancelled: CancelCheck
    ) -> AsyncIterator[dict[str, Any]]:
        iterator = response.aiter_bytes().__aiter__()
        buffered = bytearray()
        total_bytes = 0
        frame_count = 0
        while True:
            try:
                chunk = await self._next_with_cancel(iterator, is_cancelled)
            except StopAsyncIteration:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_STREAM_BYTES:
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama stream exceeded its byte budget."
                )
            buffered.extend(chunk)
            while True:
                newline = buffered.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffered[:newline]).rstrip(b"\r")
                del buffered[: newline + 1]
                if not line.strip():
                    continue
                frame_count += 1
                if frame_count > MAX_STREAM_FRAMES:
                    raise OllamaBrokerError(
                        "model_protocol_error", "Ollama stream had too many frames."
                    )
                yield self._decode_line(line)
            if len(buffered) > MAX_NDJSON_LINE_BYTES:
                raise OllamaBrokerError(
                    "model_protocol_error", "An Ollama stream line was too large."
                )
        if buffered.strip():
            if len(buffered) > MAX_NDJSON_LINE_BYTES:
                raise OllamaBrokerError(
                    "model_protocol_error", "An Ollama stream line was too large."
                )
            frame_count += 1
            if frame_count > MAX_STREAM_FRAMES:
                raise OllamaBrokerError(
                    "model_protocol_error", "Ollama stream had too many frames."
                )
            yield self._decode_line(bytes(buffered).rstrip(b"\r"))

    @staticmethod
    async def _next_with_cancel(
        iterator: AsyncIterator[bytes], is_cancelled: CancelCheck
    ) -> bytes:
        pending = asyncio.ensure_future(iterator.__anext__())
        try:
            while True:
                if is_cancelled():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    raise OllamaCancelledError()
                done, _pending = await asyncio.wait(
                    {pending}, timeout=CANCEL_POLL_SECONDS
                )
                if pending in done:
                    return pending.result()
        except BaseException:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            raise

    @staticmethod
    def _decode_line(line: bytes) -> dict[str, Any]:
        if len(line) > MAX_NDJSON_LINE_BYTES:
            raise OllamaBrokerError(
                "model_protocol_error", "An Ollama stream line was too large."
            )
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned malformed NDJSON."
            ) from exc
        if not isinstance(value, dict):
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned a non-object frame."
            )
        return value

    def _normalize_tool_call(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned an invalid Tool call."
            )
        function = value.get("function")
        provider_name = function.get("name") if isinstance(function, dict) else None
        spec = (
            self._tools_by_provider.get(provider_name)
            if isinstance(provider_name, str)
            else None
        )
        if not isinstance(function, dict) or spec is None:
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama selected an unknown Tool."
            )
        index = function.get("index", 0)
        if not isinstance(index, int) or isinstance(index, bool) or index != 0:
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned an invalid Tool index."
            )
        try:
            arguments = spec.validate_arguments(function.get("arguments"))
        except ValueError as exc:
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama returned invalid Tool arguments."
            ) from exc
        call_id = value.get("id")
        if not isinstance(call_id, str) or _SAFE_CALL_ID.fullmatch(call_id) is None:
            call_id = f"call_{uuid4().hex}"
        if call_id in self._seen_call_ids:
            raise OllamaBrokerError(
                "model_protocol_error", "Ollama repeated a Tool call ID."
            )
        self._seen_call_ids.add(call_id)
        return {
            "id": call_id,
            "function": {
                "index": 0,
                "name": spec.provider_name,
                "arguments": arguments,
            },
        }

    def _validate_tool_result(self, result: OllamaToolResult) -> OllamaToolResult:
        if not isinstance(result, OllamaToolResult):
            raise TypeError("tool_results must contain OllamaToolResult values")
        if _SAFE_CALL_ID.fullmatch(result.call_id) is None:
            raise OllamaBrokerError(
                "model_state_error", "A Tool result has an invalid call ID."
            )
        spec = self._tools_by_id.get(result.tool_id)
        if spec is None:
            raise OllamaBrokerError(
                "model_state_error", "A Tool result named an unknown Tool."
            )
        if result.outcome not in {"succeeded", "failed", "cancelled"}:
            raise OllamaBrokerError(
                "model_state_error", "A Tool result has an invalid outcome."
            )
        try:
            content = spec.validate_result(result.content)
        except ValueError as exc:
            raise OllamaBrokerError(
                "model_state_error", "A Tool result exceeded its contract."
            ) from exc
        return OllamaToolResult(
            call_id=result.call_id,
            tool_id=result.tool_id,
            content=content,
            outcome=result.outcome,
        )


__all__ = [
    "HARNESS_TOOL_ID",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "OLLAMA_PORT",
    "OllamaBroker",
    "OllamaBrokerError",
    "OllamaCancelledError",
    "OllamaFrame",
    "OllamaQualification",
    "OllamaRequestMetadata",
    "OllamaRunSession",
    "OllamaToolResult",
    "REQUEST_DIGEST_DOMAIN",
]

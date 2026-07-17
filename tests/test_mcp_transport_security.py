"""Wire-level MCP response limits and DNS pinning regressions."""

from __future__ import annotations

import ipaddress
from unittest.mock import patch

import httpx
import pytest

from src.mcp_manager import (
    MCPResponseLimitError,
    _LimitedMCPResponseStream,
    _PinnedLimitedMCPTransport,
)
from src.security import (
    ResolvedOutboundTarget,
    SecurityValidationError,
    resolve_outbound_target,
)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_sse_limit_is_enforced_before_oversized_chunk_is_yielded() -> None:
    raw = _ChunkStream([b"data: " + b"a" * 8, b"b" * 32 + b"\n\n"])
    limited = _LimitedMCPResponseStream(raw, is_sse=True, limit=24)
    yielded: list[bytes] = []
    with pytest.raises(MCPResponseLimitError, match="SSE event"):
        async for chunk in limited:
            yielded.append(chunk)
    assert yielded == [b"data: " + b"a" * 8]
    assert raw.closed


@pytest.mark.asyncio
async def test_sse_limit_resets_per_event_but_http_limit_is_total() -> None:
    events = [b"data: 123456\r\n\r\n", b"data: abcdef\n\n"]
    raw_events = _ChunkStream(events)
    assert [
        chunk
        async for chunk in _LimitedMCPResponseStream(
            raw_events,
            is_sse=True,
            limit=16,
        )
    ] == events

    raw_body = _ChunkStream([b"1234", b"5678"])
    limited_body = _LimitedMCPResponseStream(raw_body, is_sse=False, limit=7)
    yielded: list[bytes] = []
    with pytest.raises(MCPResponseLimitError, match="HTTP response"):
        async for chunk in limited_body:
            yielded.append(chunk)
    assert yielded == [b"1234"]
    assert raw_body.closed


class _FakeNetworkStream:
    def __init__(self):
        self.writes: list[bytes] = []
        self.server_hostname: str | None = None
        self._response = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok",
            b"",
        ]

    async def read(self, _max_bytes: int, timeout=None) -> bytes:
        return self._response.pop(0) if self._response else b""

    async def write(self, buffer: bytes, timeout=None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str):
        return False if info == "is_readable" else None


class _FakeNetworkBackend:
    def __init__(self):
        self.hosts: list[tuple[str, int]] = []
        self.stream = _FakeNetworkStream()

    async def connect_tcp(self, host, port, **_kwargs):
        self.hosts.append((host, port))
        return self.stream

    async def sleep(self, _seconds: float) -> None:
        return None


@pytest.mark.asyncio
async def test_validated_ip_is_pinned_while_host_header_and_tls_sni_stay_original() -> None:
    with patch(
        "src.security._resolve_host_sync",
        return_value={ipaddress.ip_address("93.184.216.34")},
    ):
        target = await resolve_outbound_target(
            "https://example.test/mcp",
            allowlist="example.test:443",
        )

    transport = _PinnedLimitedMCPTransport(target)
    pinned_backend = transport._transport._pool._network_backend
    fake_backend = _FakeNetworkBackend()
    pinned_backend._backend = fake_backend

    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = await client.get(target.url)
        assert response.text == "ok"

    assert fake_backend.hosts == [("93.184.216.34", 443)]
    assert fake_backend.stream.server_hostname == "example.test"
    request_bytes = b"".join(fake_backend.stream.writes)
    assert b"Host: example.test\r\n" in request_bytes


@pytest.mark.asyncio
async def test_pinned_transport_fails_closed_on_origin_change() -> None:
    target = ResolvedOutboundTarget(
        url="https://example.test/mcp",
        hostname="example.test",
        port=443,
        scheme="https",
        addresses=("93.184.216.34",),
    )
    transport = _PinnedLimitedMCPTransport(target)
    request = httpx.Request("GET", "https://other.test/mcp")
    with pytest.raises(SecurityValidationError, match="origin changed"):
        await transport.handle_async_request(request)
    await transport.aclose()

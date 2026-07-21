"""Trust, protocol and transport tests for MCP/LSP extension adapters."""

from __future__ import annotations

import json
import time

import pytest

from agent_builder_v2.extensions import (
    ExtensionCatalog,
    ExtensionError,
    ExtensionExecutor,
    ExtensionSpec,
)
from agent_builder_v2.permissions import CapabilityRequest


PUBLIC_IP = "93.184.216.34"
EXTENSION_ID = "11111111-1111-4111-8111-111111111111"


def _resolver(_host: str, _port: int) -> tuple[str, ...]:
    return (PUBLIC_IP,)


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.response_override: bytes | None = None

    def call(self, spec, pinned_ip, payload, cancelled):
        if cancelled():
            raise ExtensionError("cancelled")
        value = json.loads(payload)
        self.calls.append((spec.extension_id, pinned_ip, value))
        if self.response_override is not None:
            return self.response_override
        return json.dumps(
            {"jsonrpc": "2.0", "id": value["id"], "result": {"ok": True}},
            separators=(",", ":"),
        ).encode()


def _spec(protocol: str = "mcp") -> ExtensionSpec:
    return ExtensionSpec.create(
        extension_id=EXTENSION_ID,
        protocol=protocol,
        endpoint="https://example.com/rpc",
        methods=("initialize", "tools/list"),
        allowlist=frozenset({"example.com:443"}),
        resolver=_resolver,
    )


def _request(prepared: dict[str, object], preview: str) -> CapabilityRequest:
    now = int(time.time() * 1000)
    return CapabilityRequest.create(
        agent_id="00000000-0000-4000-8000-000000000001",
        capsule_generation=1,
        conversation_id="2" * 32,
        run_id="3" * 32,
        call_id="extension-call",
        capability_id="extension/call",
        toolset_digest="4" * 64,
        policy_digest="5" * 64,
        arguments=prepared,
        preview=preview,
        expires_at_milliseconds=now + 30_000,
        now_milliseconds=now,
    )


@pytest.mark.parametrize("protocol", ["mcp", "lsp"])
def test_mcp_and_lsp_share_bound_identity_and_bounded_jsonrpc(protocol: str) -> None:
    transport = _Transport()
    executor = ExtensionExecutor(
        ExtensionCatalog((_spec(protocol),)),
        transport=transport,
        resolver=_resolver,
    )
    prepared, preview, dispatched = executor.prepare(
        {
            "extension_id": EXTENSION_ID,
            "method": "initialize",
            "params_json": '{"capabilities":{}}',
        }
    )
    result = json.loads(dispatched.execute(_request(prepared, preview), lambda: False))
    assert result == {
        "kind": "extension_result",
        "extension_id": EXTENSION_ID,
        "protocol": protocol,
        "method": "initialize",
        "result": {"ok": True},
        "error": None,
    }
    assert transport.calls[0][1] == PUBLIC_IP
    assert transport.calls[0][2]["jsonrpc"] == "2.0"
    assert "example.com" not in preview


@pytest.mark.parametrize(
    "endpoint,allowlist",
    [
        ("http://example.com/rpc", frozenset({"example.com:80"})),
        ("https://" + "user" + ":" + "secret" + "@example.com/rpc", frozenset({"example.com:443"})),
        ("https://example.com/rpc?q=1", frozenset({"example.com:443"})),
        ("stdio:///bin/server", frozenset()),
        ("https://example.com/rpc", frozenset({"other.example:443"})),
    ],
)
def test_endpoint_injection_credentials_redirect_surface_and_stdio_fail_closed(
    endpoint: str, allowlist: frozenset[str]
) -> None:
    with pytest.raises(ExtensionError):
        ExtensionSpec.create(
            extension_id=EXTENSION_ID,
            protocol="mcp",
            endpoint=endpoint,
            methods=("initialize",),
            allowlist=allowlist,
            resolver=_resolver,
        )


def test_private_dns_rebinding_method_drift_cancel_and_response_spoof_fail_closed() -> None:
    with pytest.raises(ExtensionError, match="unsafe"):
        ExtensionSpec.create(
            extension_id=EXTENSION_ID,
            protocol="mcp",
            endpoint="https://example.com/rpc",
            methods=("initialize",),
            allowlist=frozenset({"example.com:443"}),
            resolver=lambda _host, _port: ("127.0.0.1",),
        )
    transport = _Transport()
    executor = ExtensionExecutor(
        ExtensionCatalog((_spec(),)),
        transport=transport,
        resolver=lambda _host, _port: ("93.184.216.35",),
    )
    prepared, preview, dispatched = executor.prepare(
        {
            "extension_id": EXTENSION_ID,
            "method": "initialize",
            "params_json": "{}",
        }
    )
    with pytest.raises(ExtensionError, match="DNS identity"):
        dispatched.execute(_request(prepared, preview), lambda: False)
    with pytest.raises(ExtensionError, match="method"):
        ExtensionExecutor(
            ExtensionCatalog((_spec(),)), transport=transport, resolver=_resolver
        ).prepare(
            {
                "extension_id": EXTENSION_ID,
                "method": "shutdown",
                "params_json": "{}",
            }
        )

    stable = ExtensionExecutor(
        ExtensionCatalog((_spec(),)), transport=transport, resolver=_resolver
    )
    prepared, preview, dispatched = stable.prepare(
        {
            "extension_id": EXTENSION_ID,
            "method": "initialize",
            "params_json": "{}",
        }
    )
    with pytest.raises(ExtensionError, match="cancelled"):
        dispatched.execute(_request(prepared, preview), lambda: True)
    transport.response_override = b'{"jsonrpc":"2.0","id":"foreign","result":{}}'
    with pytest.raises(ExtensionError, match="binding"):
        dispatched.execute(_request(prepared, preview), lambda: False)


def test_empty_release_catalog_exposes_no_endpoint_and_dispatches_nothing() -> None:
    catalog = ExtensionCatalog.empty()
    assert catalog.public_metadata() == ()
    with pytest.raises(ExtensionError, match="not configured"):
        ExtensionExecutor(catalog).prepare(
            {
                "extension_id": EXTENSION_ID,
                "method": "initialize",
                "params_json": "{}",
            }
        )

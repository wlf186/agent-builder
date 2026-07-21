"""Fail-closed MCP/LSP JSON-RPC adapters behind the capability boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .permissions import CapabilityRequest


MAX_EXTENSIONS = 8
MAX_EXTENSION_METHODS = 32
MAX_EXTENSION_PARAMS_BYTES = 8 * 1024
MAX_EXTENSION_FRAME_BYTES = 64 * 1024
MAX_EXTENSION_RESULT_BYTES = 16 * 1024
MAX_EXTENSION_JSON_NODES = 2_048
MAX_EXTENSION_JSON_DEPTH = 12
EXTENSION_TIMEOUT_SECONDS = 5.0


class ExtensionError(RuntimeError):
    """An extension request failed a trust, transport or protocol boundary."""


def _canonical(value: object, maximum: int, field: str) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExtensionError(f"invalid {field}") from exc
    if len(raw) > maximum:
        raise ExtensionError(f"{field} exceeds its byte limit")
    return raw.decode("utf-8")


def _validate_json_shape(value: object) -> None:
    pending = [(value, 1)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_EXTENSION_JSON_NODES or depth > MAX_EXTENSION_JSON_DEPTH:
            raise ExtensionError("extension JSON shape exceeds its limit")
        if isinstance(current, dict):
            if any(not isinstance(key, str) or len(key.encode("utf-8")) > 128 for key in current):
                raise ExtensionError("extension JSON field is invalid")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif not isinstance(current, (str, int, float, bool, type(None))):
            raise ExtensionError("extension JSON value is invalid")


def _resolve(host: str, port: int) -> tuple[str, ...]:
    try:
        values = {
            item[4][0]
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ExtensionError("extension hostname cannot be resolved") from exc
    if not values or len(values) > 8:
        raise ExtensionError("extension DNS result is outside its limit")
    return tuple(sorted(values))


def _safe_ip(value: str, *, private_literal_allowed: bool) -> None:
    address = ipaddress.ip_address(value)
    unsafe = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )
    if unsafe and not private_literal_allowed:
        raise ExtensionError("extension endpoint resolved to an unsafe address")


@dataclass(frozen=True, slots=True)
class ExtensionSpec:
    extension_id: str
    protocol: str
    endpoint: str
    methods: tuple[str, ...]
    resolved_ips: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        extension_id: str,
        protocol: str,
        endpoint: str,
        methods: tuple[str, ...],
        allowlist: frozenset[str],
        resolver: Callable[[str, int], tuple[str, ...]] = _resolve,
    ) -> "ExtensionSpec":
        from .capsule import SAFE_ID

        if SAFE_ID.fullmatch(extension_id) is None or protocol not in {"mcp", "lsp"}:
            raise ExtensionError("invalid extension identity")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path in {"", "/"}
        ):
            raise ExtensionError("extension endpoint must be an exact credential-free HTTPS URL")
        port = parsed.port or 443
        authority = f"{parsed.hostname}:{port}"
        if authority not in allowlist:
            raise ExtensionError("extension endpoint is not allowlisted")
        if not 1 <= len(methods) <= MAX_EXTENSION_METHODS or tuple(sorted(set(methods))) != methods:
            raise ExtensionError("extension methods are invalid")
        if any(not method or len(method.encode("ascii", "ignore")) > 128 for method in methods):
            raise ExtensionError("extension method is invalid")
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        resolved = resolver(parsed.hostname, port)
        for address in resolved:
            _safe_ip(address, private_literal_allowed=literal is not None and str(literal) == address)
        if literal is not None and resolved != (str(literal),):
            raise ExtensionError("extension IP literal changed identity")
        return cls(extension_id, protocol, endpoint, methods, resolved)

    def public_metadata(self) -> dict[str, object]:
        return {
            "extension_id": self.extension_id,
            "protocol": self.protocol,
            "methods": list(self.methods),
            "transport": "pinned-https-jsonrpc-v1",
        }


class ExtensionTransport(Protocol):
    def call(
        self,
        spec: ExtensionSpec,
        pinned_ip: str,
        payload: bytes,
        cancelled: Callable[[], bool],
    ) -> bytes: ...


class PinnedHttpsTransport:
    """One non-redirecting TLS request connected to the already-validated IP."""

    def call(
        self,
        spec: ExtensionSpec,
        pinned_ip: str,
        payload: bytes,
        cancelled: Callable[[], bool],
    ) -> bytes:
        if cancelled():
            raise ExtensionError("extension request cancelled")
        parsed = urlsplit(spec.endpoint)
        port = parsed.port or 443
        raw = socket.create_connection((pinned_ip, port), timeout=EXTENSION_TIMEOUT_SECONDS)
        try:
            raw.settimeout(EXTENSION_TIMEOUT_SECONDS)
            tls = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            raw = None
            try:
                connection = http.client.HTTPConnection(parsed.hostname, port, timeout=EXTENSION_TIMEOUT_SECONDS)
                connection.sock = tls
                connection.request(
                    "POST",
                    parsed.path,
                    body=payload,
                    headers={
                        "Host": parsed.netloc,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Content-Length": str(len(payload)),
                        "User-Agent": "agent-builder-extension/1",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    raise ExtensionError("extension returned a non-success status")
                if "application/json" not in response.getheader("Content-Type", "").lower():
                    raise ExtensionError("extension returned an invalid content type")
                result = response.read(MAX_EXTENSION_FRAME_BYTES + 1)
                if len(result) > MAX_EXTENSION_FRAME_BYTES:
                    raise ExtensionError("extension response exceeded its byte limit")
                if response.read(1):
                    raise ExtensionError("extension response exceeded its byte limit")
                return result
            finally:
                tls.close()
        finally:
            if raw is not None:
                raw.close()


class ExtensionCatalog:
    def __init__(self, specs: tuple[ExtensionSpec, ...]) -> None:
        if len(specs) > MAX_EXTENSIONS or len({item.extension_id for item in specs}) != len(specs):
            raise ExtensionError("extension catalog is invalid")
        self._specs = {item.extension_id: item for item in specs}

    @classmethod
    def empty(cls) -> "ExtensionCatalog":
        return cls(())

    def get(self, extension_id: str) -> ExtensionSpec:
        try:
            return self._specs[extension_id]
        except KeyError as exc:
            raise ExtensionError("extension is not configured") from exc

    def public_metadata(self) -> tuple[dict[str, object], ...]:
        return tuple(self._specs[key].public_metadata() for key in sorted(self._specs))


class PreparedExtensionExecutor:
    executor_kind = "pinned-https-jsonrpc-v1"

    def __init__(
        self,
        spec: ExtensionSpec,
        prepared: Mapping[str, object],
        transport: ExtensionTransport,
        resolver: Callable[[str, int], tuple[str, ...]],
        semaphore: threading.BoundedSemaphore,
    ) -> None:
        self._spec = spec
        self._prepared = dict(prepared)
        self._transport = transport
        self._resolver = resolver
        self._semaphore = semaphore
        self.identity_digest = hashlib.sha256(
            b"agent-builder-extension-executor-v1\0"
            + _canonical(
                {
                    "extension_id": spec.extension_id,
                    "protocol": spec.protocol,
                    "endpoint": spec.endpoint,
                    "resolved_ips": spec.resolved_ips,
                },
                MAX_EXTENSION_FRAME_BYTES,
                "extension identity",
            ).encode()
        ).hexdigest()

    def execute(self, request: CapabilityRequest, cancelled: Callable[[], bool]) -> str:
        try:
            prepared = json.loads(request.arguments_json)
        except json.JSONDecodeError as exc:
            raise ExtensionError("extension request is invalid") from exc
        if prepared != self._prepared:
            raise ExtensionError("extension request binding changed")
        parsed = urlsplit(self._spec.endpoint)
        current = self._resolver(parsed.hostname or "", parsed.port or 443)
        if current != self._spec.resolved_ips:
            raise ExtensionError("extension DNS identity changed")
        request_id = self._prepared["request_id"]
        wire = _canonical(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": self._prepared["method"],
                "params": self._prepared["params"],
            },
            MAX_EXTENSION_FRAME_BYTES,
            "extension request",
        ).encode()
        if cancelled():
            raise ExtensionError("extension request cancelled")
        if not self._semaphore.acquire(timeout=EXTENSION_TIMEOUT_SECONDS):
            raise ExtensionError("extension concurrency is exhausted")
        try:
            raw = self._transport.call(self._spec, current[0], wire, cancelled)
        finally:
            self._semaphore.release()
        if len(raw) > MAX_EXTENSION_FRAME_BYTES:
            raise ExtensionError("extension response exceeded its byte limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExtensionError("extension response is invalid") from exc
        _validate_json_shape(value)
        if (
            not isinstance(value, dict)
            or value.get("jsonrpc") != "2.0"
            or value.get("id") != request_id
            or ("result" in value) == ("error" in value)
            or set(value) - {"jsonrpc", "id", "result", "error"}
        ):
            raise ExtensionError("extension response binding is invalid")
        return _canonical(
            {
                "kind": "extension_result",
                "extension_id": self._spec.extension_id,
                "protocol": self._spec.protocol,
                "method": self._prepared["method"],
                "result": value.get("result"),
                "error": value.get("error"),
            },
            MAX_EXTENSION_RESULT_BYTES,
            "extension result",
        )


class ExtensionExecutor:
    def __init__(
        self,
        catalog: ExtensionCatalog,
        *,
        transport: ExtensionTransport | None = None,
        resolver: Callable[[str, int], tuple[str, ...]] = _resolve,
    ) -> None:
        self.catalog = catalog
        self._transport = transport or PinnedHttpsTransport()
        self._resolver = resolver
        self._semaphore = threading.BoundedSemaphore(4)

    def prepare(self, arguments: Mapping[str, object]):
        if set(arguments) != {"extension_id", "method", "params_json"}:
            raise ExtensionError("extension arguments are invalid")
        spec = self.catalog.get(str(arguments.get("extension_id")))
        method = arguments.get("method")
        params_json = arguments.get("params_json")
        if not isinstance(method, str) or method not in spec.methods or not isinstance(params_json, str):
            raise ExtensionError("extension method is not allowed")
        if len(params_json.encode("utf-8")) > MAX_EXTENSION_PARAMS_BYTES:
            raise ExtensionError("extension parameters exceed their byte limit")
        try:
            params = json.loads(params_json)
        except json.JSONDecodeError as exc:
            raise ExtensionError("extension parameters are invalid") from exc
        _validate_json_shape(params)
        request_id = hashlib.sha256(
            b"agent-builder-extension-request-v1\0"
            + _canonical([spec.extension_id, method, params], MAX_EXTENSION_FRAME_BYTES, "extension request").encode()
        ).hexdigest()[:32]
        prepared = {
            "schema_version": 1,
            "extension_id": spec.extension_id,
            "protocol": spec.protocol,
            "method": method,
            "params": params,
            "request_id": request_id,
            "transport": "pinned-https-jsonrpc-v1",
        }
        preview = _canonical(
            {
                "action": "extension/call",
                "extension_id": spec.extension_id,
                "protocol": spec.protocol,
                "method": method,
                "params": params,
                "endpoint": "operator-configured-pinned-https",
                "credentials": "none",
            },
            MAX_EXTENSION_FRAME_BYTES,
            "extension preview",
        )
        return prepared, preview, PreparedExtensionExecutor(
            spec, prepared, self._transport, self._resolver, self._semaphore
        )


__all__ = [
    "ExtensionCatalog",
    "ExtensionError",
    "ExtensionExecutor",
    "ExtensionSpec",
    "ExtensionTransport",
    "MAX_EXTENSIONS",
    "PinnedHttpsTransport",
]

"""Security primitives shared by the Agent Builder API and outbound clients.

The helpers in this module deliberately have no dependency on FastAPI.  This
keeps validation usable by background MCP/model clients and makes the security
policy straightforward to test offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit


API_TOKEN_ENV = "AGENT_BUILDER_API_TOKEN"
SSRF_ALLOWLIST_ENV = "AGENT_BUILDER_SSRF_ALLOWLIST"
CORS_ORIGINS_ENV = "AGENT_BUILDER_CORS_ORIGINS"

DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:20815",
    "http://localhost:20815",
)

MAX_URL_LENGTH = 2_048
MAX_COMMAND_LENGTH = 1_024
MAX_ARGUMENT_COUNT = 128
MAX_ARGUMENT_LENGTH = 8_192
MAX_ENVIRONMENT_ENTRIES = 128
MAX_HEADER_ENTRIES = 64
MAX_MAPPING_VALUE_LENGTH = 16_384

_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}


class SecurityValidationError(ValueError):
    """Raised when untrusted input violates a security boundary."""


class APIAuthenticationError(Exception):
    """Authentication failure carrying an HTTP-compatible status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """ASGI middleware enforcing per-endpoint limits, including chunked bodies."""

    def __init__(self, app: Any):
        self.app = app

    @staticmethod
    def _limit_for_path(path: str) -> int:
        if path == "/api/log":
            return 64 * 1024
        if path == "/api/client-logs":
            return 256 * 1024
        if path == "/api/skills/upload":
            return 27 * 1024 * 1024  # archive plus multipart envelope
        if re.fullmatch(r"/api/agents/[^/]+/files", path):
            return 102 * 1024 * 1024
        if re.fullmatch(r"/api/knowledge-bases/[^/]+/documents", path):
            return 12 * 1024 * 1024
        return 2 * 1024 * 1024

    @staticmethod
    async def _reject(send: Any, status: int = 413) -> None:
        payload = b'{"detail":"Request body is too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    def _contains_limit_error(exc: BaseException) -> bool:
        if isinstance(exc, _RequestBodyTooLarge):
            return True
        return any(
            RequestBodyLimitMiddleware._contains_limit_error(child)
            for child in getattr(exc, "exceptions", ())
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api"):
            await self.app(scope, receive, send)
            return

        limit = self._limit_for_path(scope.get("path", ""))
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    declared_length = int(value)
                    if declared_length < 0:
                        raise ValueError
                    if declared_length > limit:
                        await self._reject(send)
                        return
                except ValueError:
                    payload = b'{"detail":"Invalid Content-Length header"}'
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 400,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode("ascii")),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": payload})
                    return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except Exception as exc:
            if self._contains_limit_error(exc):
                if not response_started:
                    await self._reject(send)
                return
            raise


def parse_cors_origins(raw: Optional[str] = None) -> list[str]:
    """Return an explicit CORS origin list; wildcards are never accepted."""
    value = os.environ.get(CORS_ORIGINS_ENV) if raw is None else raw
    origins = list(DEFAULT_CORS_ORIGINS) if not value else [
        item.strip().rstrip("/") for item in value.split(",") if item.strip()
    ]
    if not origins:
        raise SecurityValidationError("CORS origin allowlist cannot be empty")

    validated: list[str] = []
    for origin in origins:
        if origin == "*":
            raise SecurityValidationError("Wildcard CORS origins are forbidden")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise SecurityValidationError(f"Invalid CORS origin: {origin!r}")
        # Accessing .port also rejects malformed/out-of-range ports.
        try:
            _ = parsed.port
        except ValueError as exc:
            raise SecurityValidationError(f"Invalid CORS origin: {origin!r}") from exc
        if origin not in validated:
            validated.append(origin)
    return validated


def _configured_api_token(configured_token: Optional[str] = None) -> str:
    token = os.environ.get(API_TOKEN_ENV, "") if configured_token is None else configured_token
    token = token.strip()
    if not token:
        raise APIAuthenticationError(
            503,
            f"API authentication is not configured; set {API_TOKEN_ENV}",
        )
    if len(token) < 32:
        raise APIAuthenticationError(
            503,
            f"{API_TOKEN_ENV} must contain at least 32 characters",
        )
    return token


def authenticate_api_headers(
    headers: Mapping[str, str], configured_token: Optional[str] = None
) -> None:
    """Authenticate a Bearer or X-API-Key header using constant-time compare.

    An unset or obviously weak server token is treated as a server
    misconfiguration (503), not as an implicit authentication bypass.
    """
    expected = _configured_api_token(configured_token)
    authorization = headers.get("authorization", "").strip()
    api_key = headers.get("x-api-key", "").strip()

    bearer = ""
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            bearer = parts[1].strip()

    def token_digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    if bearer and api_key and not hmac.compare_digest(token_digest(bearer), token_digest(api_key)):
        raise APIAuthenticationError(401, "Conflicting API credentials")

    supplied = bearer or api_key
    if not supplied or not hmac.compare_digest(token_digest(supplied), token_digest(expected)):
        raise APIAuthenticationError(401, "Invalid or missing API credentials")


@dataclass(frozen=True)
class ParsedOutboundURL:
    url: str
    hostname: str
    port: Optional[int]
    scheme: str


@dataclass(frozen=True)
class ResolvedOutboundTarget:
    """One authorized origin and the exact IP addresses approved for dialing."""

    url: str
    hostname: str
    port: int
    scheme: str
    addresses: tuple[str, ...]


def _normalise_hostname(hostname: str) -> str:
    try:
        return hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SecurityValidationError("URL hostname is not valid IDNA") from exc


def validate_outbound_url_syntax(url: str) -> ParsedOutboundURL:
    """Validate URL structure before DNS resolution or network access."""
    if not isinstance(url, str) or not url.strip():
        raise SecurityValidationError("URL is required")
    if len(url) > MAX_URL_LENGTH or any(ord(char) < 32 for char in url):
        raise SecurityValidationError("URL is too long or contains control characters")

    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SecurityValidationError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise SecurityValidationError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise SecurityValidationError("Credentials in URLs are forbidden")

    try:
        port = parsed.port
    except ValueError as exc:
        raise SecurityValidationError("URL contains an invalid port") from exc

    hostname = _normalise_hostname(parsed.hostname)
    if hostname in {"metadata", "metadata.google.internal"}:
        raise SecurityValidationError("Cloud metadata endpoints are forbidden")
    return ParsedOutboundURL(url=url.strip(), hostname=hostname, port=port, scheme=parsed.scheme.lower())


def _split_allowlist(raw: Optional[str]) -> list[str]:
    value = os.environ.get(SSRF_ALLOWLIST_ENV, "") if raw is None else raw
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _host_rule_matches(rule: str, hostname: str, port: Optional[int]) -> bool:
    """Match an exact hostname rule, optionally scoped to a port."""
    rule_host = rule
    rule_port: Optional[int] = None

    if rule.startswith("[") and "]" in rule:
        closing = rule.index("]")
        rule_host = rule[1:closing]
        remainder = rule[closing + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                return False
            rule_port = int(remainder[1:])
    elif rule.count(":") == 1:
        candidate_host, candidate_port = rule.rsplit(":", 1)
        if candidate_port.isdigit():
            rule_host = candidate_host
            rule_port = int(candidate_port)

    if rule_port is not None and rule_port != port:
        # A URL without an explicit port uses its scheme default; callers pass
        # that effective port below, so a mismatch is unambiguous here.
        return False

    if rule_host.startswith("*."):
        return False
    normalised_rule = _normalise_hostname(rule_host)
    return hmac.compare_digest(hostname, normalised_rule)


def _address_is_allowlisted(address: ipaddress._BaseAddress, entries: Sequence[str]) -> bool:
    for entry in entries:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _target_is_allowlisted(
    hostname: str, effective_port: int, entries: Sequence[str]
) -> bool:
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
            continue
        except ValueError:
            pass
        if _host_rule_matches(entry, hostname, effective_port):
            return True
    return False


def _resolve_host_sync(hostname: str, port: int) -> set[ipaddress._BaseAddress]:
    try:
        literal = ipaddress.ip_address(hostname)
        return {literal}
    except ValueError:
        pass

    results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses: set[ipaddress._BaseAddress] = set()
    for result in results:
        addresses.add(ipaddress.ip_address(result[4][0]))
    return addresses


async def resolve_outbound_target(
    url: str,
    *,
    allowlist: Optional[str] = None,
    dns_timeout: float = 5.0,
) -> ResolvedOutboundTarget:
    """Authorize and resolve a target once for a DNS-pinned transport.

    DNS hostnames are denied unless an administrator has explicitly trusted the
    hostname through ``AGENT_BUILDER_SSRF_ALLOWLIST``.  Requiring that boundary
    prevents an attacker from supplying a disposable public hostname and then
    rebinding it between validation and connection.  Global IP literals remain
    usable without an entry; non-global literals require an explicit host or
    network rule. Entries may be exact hosts, ``host:port``, or IP networks
    such as ``10.0.0.0/24``; wildcard hostnames are intentionally rejected.

    An allowlisted hostname is a trust decision, not merely a one-time DNS
    result.  Metadata addresses are forbidden even for trusted names.
    """
    parsed = validate_outbound_url_syntax(url)
    effective_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    entries = _split_allowlist(allowlist)
    host_is_allowlisted = _target_is_allowlisted(parsed.hostname, effective_port, entries)

    try:
        ipaddress.ip_address(parsed.hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if not is_ip_literal and not host_is_allowlisted:
        raise SecurityValidationError(
            "Outbound DNS host is not trusted; add an exact "
            f"{SSRF_ALLOWLIST_ENV} hostname entry before using it"
        )

    loop = asyncio.get_running_loop()
    try:
        addresses = await asyncio.wait_for(
            loop.run_in_executor(None, _resolve_host_sync, parsed.hostname, effective_port),
            timeout=dns_timeout,
        )
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        raise SecurityValidationError("Unable to safely resolve outbound hostname") from exc

    if not addresses:
        raise SecurityValidationError("Outbound hostname did not resolve")
    if len(addresses) > 16:
        raise SecurityValidationError("Outbound hostname resolved to too many addresses")

    if any(address in _METADATA_ADDRESSES for address in addresses):
        raise SecurityValidationError("Cloud metadata addresses are always forbidden")

    def non_global_address_is_authorized(address: ipaddress._BaseAddress) -> bool:
        # A trusted public DNS name must not become an implicit gateway to a
        # private network.  Private targets therefore use an IP literal (and a
        # port-scoped host rule where possible).  ``localhost`` is the sole DNS
        # exception and is accepted only when both its hostname/port and its
        # loopback resolution match.
        if is_ip_literal:
            return host_is_allowlisted or _address_is_allowlisted(address, entries)
        return (
            parsed.hostname == "localhost"
            and host_is_allowlisted
            and address.is_loopback
        )

    forbidden = [
        str(address)
        for address in addresses
        if not address.is_global and not non_global_address_is_authorized(address)
    ]
    if forbidden:
        raise SecurityValidationError(
            "Outbound URL resolves to a non-global address; add an explicit "
            f"{SSRF_ALLOWLIST_ENV} entry only if this target is trusted"
        )
    return ResolvedOutboundTarget(
        url=parsed.url,
        hostname=parsed.hostname,
        port=effective_port,
        scheme=parsed.scheme,
        addresses=tuple(sorted(str(address) for address in addresses)),
    )


async def validate_outbound_url(
    url: str,
    *,
    allowlist: Optional[str] = None,
    dns_timeout: float = 5.0,
) -> str:
    """Authorize an outbound URL while preserving the legacy string API."""
    target = await resolve_outbound_target(
        url,
        allowlist=allowlist,
        dns_timeout=dns_timeout,
    )
    return target.url


def validate_stdio_configuration(
    command: Optional[str],
    args: Sequence[str],
    env: Mapping[str, str],
) -> None:
    """Validate the explicitly enabled, trusted stdio MCP capability."""
    if os.environ.get("AGENT_BUILDER_ALLOW_STDIO_MCP", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise SecurityValidationError(
            "stdio MCP is disabled by default; set AGENT_BUILDER_ALLOW_STDIO_MCP=1 "
            "only for trusted local commands"
        )
    if not command or not isinstance(command, str):
        raise SecurityValidationError("stdio MCP command is required")
    if len(command) > MAX_COMMAND_LENGTH or "\x00" in command or "\n" in command or "\r" in command:
        raise SecurityValidationError("stdio MCP command is invalid")
    if len(args) > MAX_ARGUMENT_COUNT:
        raise SecurityValidationError("Too many stdio MCP arguments")
    for argument in args:
        if not isinstance(argument, str) or len(argument) > MAX_ARGUMENT_LENGTH or "\x00" in argument:
            raise SecurityValidationError("stdio MCP argument is invalid")

    if len(env) > MAX_ENVIRONMENT_ENTRIES:
        raise SecurityValidationError("Too many stdio MCP environment variables")
    for key, value in env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SecurityValidationError(f"Invalid environment variable name: {key!r}")
        if not isinstance(value, str) or len(value) > MAX_MAPPING_VALUE_LENGTH or "\x00" in value:
            raise SecurityValidationError(f"Invalid environment variable value for {key!r}")
        if key == API_TOKEN_ENV:
            raise SecurityValidationError("The control-plane API token cannot be passed to MCP")


def validate_headers(headers: Mapping[str, str]) -> None:
    if len(headers) > MAX_HEADER_ENTRIES:
        raise SecurityValidationError("Too many custom headers")
    forbidden = {
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "te",
        "transfer-encoding",
        "upgrade",
    }
    for key, value in headers.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise SecurityValidationError(f"Invalid HTTP header name: {key!r}")
        if key.lower() in forbidden:
            raise SecurityValidationError(f"Hop-by-hop or routing header is forbidden: {key!r}")
        if not isinstance(value, str) or len(value) > MAX_MAPPING_VALUE_LENGTH:
            raise SecurityValidationError(f"Invalid HTTP header value for {key!r}")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise SecurityValidationError("HTTP header values may not contain control characters")


_PACKAGE_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9_,.-]+\])?"
    r"(?:\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+)?$"
)


def validate_package_specs(packages: Sequence[str]) -> list[str]:
    """Allow package names/version constraints, but not pip flags, URLs or paths."""
    if not packages or len(packages) > 64:
        raise SecurityValidationError("Package list must contain between 1 and 64 entries")
    validated: list[str] = []
    for package in packages:
        candidate = package.strip() if isinstance(package, str) else ""
        if len(candidate) > 256 or not _PACKAGE_SPEC_RE.fullmatch(candidate):
            raise SecurityValidationError(
                f"Invalid package specification: {candidate!r}; URLs, paths and pip options are forbidden"
            )
        validated.append(candidate)
    return validated


def validate_execution_arguments(arguments: Sequence[str], timeout: int) -> None:
    if len(arguments) > MAX_ARGUMENT_COUNT:
        raise SecurityValidationError("Too many script arguments")
    for argument in arguments:
        if not isinstance(argument, str) or len(argument) > MAX_ARGUMENT_LENGTH or "\x00" in argument:
            raise SecurityValidationError("Script argument is invalid")
    if timeout < 1 or timeout > 300:
        raise SecurityValidationError("Execution timeout must be between 1 and 300 seconds")


def resolve_contained_path(
    base: Path,
    untrusted_path: str,
    *,
    must_exist: bool = True,
    require_file: bool = True,
) -> Path:
    """Resolve a relative path and prove that it remains beneath ``base``."""
    if not isinstance(untrusted_path, str) or not untrusted_path or "\x00" in untrusted_path:
        raise SecurityValidationError("Path is required")
    normalised = untrusted_path.replace("\\", "/")
    pure = PurePosixPath(normalised)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or (pure.parts and re.match(r"^[A-Za-z]:$", pure.parts[0]))
    ):
        raise SecurityValidationError("Path must be a contained relative path")

    root = base.resolve()
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SecurityValidationError("Path escapes its allowed directory") from exc

    if must_exist and not candidate.exists():
        raise SecurityValidationError("Path does not exist")
    if must_exist and require_file and not candidate.is_file():
        raise SecurityValidationError("Path is not a regular file")
    return candidate


def sanitise_filename(filename: str) -> str:
    """Return a safe basename for metadata and copies into execution workdirs."""
    if not isinstance(filename, str) or "\x00" in filename:
        raise SecurityValidationError("Filename is invalid")
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if basename in {"", ".", ".."} or len(basename.encode("utf-8")) > 255:
        raise SecurityValidationError("Filename is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in basename):
        raise SecurityValidationError("Filename contains control characters")
    if any(char in '<>:"|?*' for char in basename):
        raise SecurityValidationError("Filename contains unsafe platform characters")
    stem = basename.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise SecurityValidationError("Filename is reserved by the platform")
    return basename


def validate_archive_member_name(name: str) -> PurePosixPath:
    """Normalise a ZIP member name without accepting traversal variants."""
    if not name or "\x00" in name or "\\" in name or len(name) > 1_024:
        raise SecurityValidationError("ZIP contains an invalid member name")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SecurityValidationError("ZIP member escapes the extraction directory")
    if len(pure.parts) > 20 or (pure.parts and re.match(r"^[A-Za-z]:$", pure.parts[0])):
        raise SecurityValidationError("ZIP member path is invalid or too deeply nested")
    for part in pure.parts:
        if sanitise_filename(part) != part:
            raise SecurityValidationError("ZIP member uses an unsafe platform filename")
    return pure


async def read_json_body_limited(request: Any, max_bytes: int) -> dict[str, Any]:
    """Stream and decode a small JSON request body with a hard byte limit."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise SecurityValidationError("Invalid Content-Length header") from exc
        if declared_length < 0:
            raise SecurityValidationError("Invalid Content-Length header")
        if declared_length > max_bytes:
            raise SecurityValidationError("Request body is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise SecurityValidationError("Request body is too large")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityValidationError("Request body must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise SecurityValidationError("JSON request body must be an object")
    return decoded


def redact_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    """Mask common secret-bearing mapping entries before returning them via API."""
    sensitive = re.compile(r"(?:authorization|cookie|token|secret|password|passwd|api[_-]?key)", re.I)
    return {key: "***" if sensitive.search(key) else value for key, value in mapping.items()}


def redact_arguments(arguments: Sequence[str]) -> list[str]:
    """Mask values following common secret-bearing command-line options."""
    sensitive = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|authorization)", re.I)
    redacted: list[str] = []
    mask_next = False
    for argument in arguments:
        if mask_next:
            redacted.append("***")
            mask_next = False
            continue
        if "=" in argument:
            key, _value = argument.split("=", 1)
            if sensitive.search(key):
                redacted.append(f"{key}=***")
                continue
        redacted.append(argument)
        if argument.startswith("-") and sensitive.search(argument):
            mask_next = True
    return redacted

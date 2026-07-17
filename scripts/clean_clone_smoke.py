#!/usr/bin/env python3
"""Dependency-free lifecycle smoke checks for a cold checkout."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
MAX_RESPONSE_BYTES = 1024 * 1024


class SmokeFailure(RuntimeError):
    """A bounded, non-secret smoke-test failure."""


def port(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SmokeFailure(f"{name} is not an integer") from exc
    if not 1 <= value <= 65535:
        raise SmokeFailure(f"{name} is outside the valid port range")
    return value


PORTS = {
    "frontend": port("FRONTEND_PORT", 20815),
    "backend": port("BACKEND_PORT", 20881),
    "mcp": port("MCP_SSE_PORT", 20882),
    "docs": port("DOCS_PORT", 4173),
    "phoenix": port("PHOENIX_PORT", 6006),
}


def request(
    component: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    expected: int = 200,
) -> bytes:
    connection = http.client.HTTPConnection("127.0.0.1", PORTS[component], timeout=5)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise SmokeFailure(f"{component} request failed: {type(exc).__name__}") from exc
    finally:
        connection.close()
    if response.status != expected:
        raise SmokeFailure(
            f"{component} {path} returned {response.status}; expected {expected}"
        )
    if len(payload) > MAX_RESPONSE_BYTES:
        raise SmokeFailure(f"{component} response exceeded the smoke-test limit")
    return payload


def json_object(component: str, path: str, **kwargs: Any) -> dict[str, Any]:
    payload = request(component, path, **kwargs)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"{component} {path} did not return JSON") from exc
    if not isinstance(decoded, dict):
        raise SmokeFailure(f"{component} {path} did not return a JSON object")
    return decoded


def load_token() -> bytes:
    token_path = RUNTIME / "secrets" / "api-token"
    try:
        metadata = token_path.lstat()
        token = token_path.read_bytes().strip()
    except OSError as exc:
        raise SmokeFailure("managed API token is missing") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SmokeFailure("managed API token is not a private regular file")
    if len(token) != 64 or any(byte not in b"0123456789abcdef" for byte in token):
        raise SmokeFailure("managed API token has an invalid format")
    if mode != 0o600:
        raise SmokeFailure("managed API token permissions are not 0600")
    return token


def assert_token_absent_from_logs(token: bytes) -> None:
    logs = RUNTIME / "logs"
    if not logs.exists():
        return
    for log_path in sorted(logs.rglob("*")):
        if log_path.is_symlink():
            raise SmokeFailure(f"managed log path is a symlink: {log_path.name}")
        if not log_path.is_file():
            continue
        overlap = b""
        try:
            with log_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    combined = overlap + chunk
                    if token in combined:
                        raise SmokeFailure(
                            f"managed API token appeared in log file {log_path.name}"
                        )
                    overlap = combined[-(len(token) - 1) :]
        except OSError as exc:
            raise SmokeFailure(f"could not inspect log file {log_path.name}") from exc


def check_running() -> None:
    token = load_token()
    authorization = {"Authorization": f"Bearer {token.decode('ascii')}"}

    request("frontend", "/")
    request("backend", "/health")
    request("mcp", "/health")
    request("docs", "/docs/")
    request("phoenix", "/healthz")

    request("backend", "/api/system/check-runtime", expected=401)
    request(
        "backend",
        "/api/system/check-runtime",
        headers={"Authorization": "Bearer invalid"},
        expected=401,
    )
    backend_runtime = json_object(
        "backend", "/api/system/check-runtime", headers=authorization
    )
    if backend_runtime.get("available") is not True:
        raise SmokeFailure("backend did not report the project-local runtime available")

    frontend_runtime = json_object("frontend", "/api/system/check-runtime")
    if frontend_runtime.get("available") is not True:
        raise SmokeFailure("frontend proxy did not report the runtime available")

    observability = json_object(
        "backend", "/api/system/observability", headers=authorization
    )
    expected_endpoint = f"http://127.0.0.1:{PORTS['phoenix']}/v1/traces"
    if (
        observability.get("enabled") is not True
        or observability.get("backend") != "otlp"
        or observability.get("endpoint") != expected_endpoint
    ):
        raise SmokeFailure("local observability did not report the expected OTLP backend")

    assert_token_absent_from_logs(token)
    print("Full-stack running smoke check passed.")


def check_stopped() -> None:
    token = load_token()
    pid_dir = RUNTIME / "pids"
    remaining = list(pid_dir.glob("*.pid")) if pid_dir.exists() else []
    if remaining:
        raise SmokeFailure("managed PID files remain after stop")

    for component, component_port in PORTS.items():
        try:
            connection = socket.create_connection(
                ("127.0.0.1", component_port), timeout=0.5
            )
        except OSError:
            continue
        connection.close()
        raise SmokeFailure(f"{component} port is still accepting connections after stop")

    root_bytes = os.fsencode(str(ROOT))
    process_markers = (
        b"run_with_rotating_log.py",
        b"start_backend.sh",
        b"start_frontend.sh",
        b"sse_server.py",
        b"/.venv/bin/phoenix",
        b"/node_modules/.bin/vitepress",
        b"next-server",
    )
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                command_line = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if root_bytes in command_line and any(
                marker in command_line for marker in process_markers
            ):
                raise SmokeFailure("a checkout-managed process remains after stop")

    assert_token_absent_from_logs(token)
    print("Full-stack stopped smoke check passed.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", choices=("running", "stopped"))
    args = parser.parse_args()
    try:
        if args.state == "running":
            check_running()
        else:
            check_stopped()
    except SmokeFailure as exc:
        print(f"Smoke check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

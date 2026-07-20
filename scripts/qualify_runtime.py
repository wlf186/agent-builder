#!/usr/bin/env python3
"""Bounded, redacted application qualification for the running local gateway."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import platform
import re
import stat
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPCookieProcessor,
    Request,
    build_opener,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
try:
    sys.path.remove(str(SOURCE_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(SOURCE_ROOT))

from agent_builder_v2.auth import (  # noqa: E402
    MAX_PROJECT_TOKEN_LENGTH,
    is_valid_project_token,
)
from agent_builder_v2.sync_counter import (  # noqa: E402
    COUNTER_ABI,
    SyncCounterError,
    read_sync_counter,
    sync_counter_delta,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:20815"
ORIGIN = BASE_URL
TOKEN_RELATIVE = Path(".runtime/secrets/web-bootstrap-token")
QUALIFICATION_RELATIVE = Path(".runtime/qualification")
GATEWAY_PID_RELATIVE = Path(".runtime/control-plane/gateway.pid")

RR_ID = re.compile(r"RR-QUA-[0-9]{8}-[0-9]{2}")
IMPLEMENTATION_REF = re.compile(r"(?:worktree|[0-9a-f]{40})")
PROCESS_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,31}")
RUNTIME_ID = re.compile(r"[a-f0-9]{32}")

DEFAULT_TURNS = 4
MIN_TURNS = 2
MAX_TURNS = 16
HTTP_TIMEOUT_SECONDS = 75.0
WORKLOAD_TIMEOUT_SECONDS = 900.0
FAST_SAMPLE_SECONDS = 0.5
SLOW_SAMPLE_SECONDS = 15.0
MAX_SCAN_ENTRIES = 100_000
MAX_JSON_BYTES = 1024 * 1024
MAX_SSE_LINE_BYTES = 128 * 1024
MAX_SSE_EVENTS = 1024
MAX_EXTRA_PIDS = 7

MIB = 1024 * 1024
KIB = 1024
THRESHOLDS = {
    "state_after_logical_growth_bytes": 8 * MIB,
    "state_after_allocated_growth_bytes": 16 * MIB,
    "wal_peak_logical_bytes": 20 * MIB,
    "logs_peak_logical_bytes": 20 * MIB,
    "cache_after_logical_growth_bytes": 2 * MIB,
    "cache_after_allocated_growth_bytes": 4 * MIB,
    "temp_peak_logical_bytes": 20 * MIB,
    "temp_peak_allocated_bytes": 36 * MIB,
    "temp_after_logical_growth_bytes": 64 * KIB,
    "temp_after_allocated_growth_bytes": 64 * KIB,
    "process_write_bytes_delta": 64 * MIB,
    "process_syscw_delta": 20_000,
}
BASE_LIMITATIONS = [
    "kernel_physical_flush_not_observed",
    "ssd_smart_not_observed",
]
IO_FIELDS = (
    "rchar",
    "wchar",
    "syscr",
    "syscw",
    "read_bytes",
    "write_bytes",
    "cancelled_write_bytes",
)
TERMINALS = {"run.completed", "run.failed", "run.cancelled"}


class QualificationError(RuntimeError):
    """A controlled qualification failure with no attacker text."""

    def __init__(self, stage: str, code: str) -> None:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", stage):
            stage = "internal"
        if not re.fullmatch(r"[a-z0-9_-]{1,96}", code):
            code = "unexpected_error"
        super().__init__(code)
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class StorageUsage:
    logical_bytes: int = 0
    allocated_bytes: int = 0
    files: int = 0
    directories: int = 0

    def combine(self, other: "StorageUsage") -> "StorageUsage":
        return StorageUsage(
            self.logical_bytes + other.logical_bytes,
            self.allocated_bytes + other.allocated_bytes,
            self.files + other.files,
            self.directories + other.directories,
        )

    def maximum(self, other: "StorageUsage") -> "StorageUsage":
        return StorageUsage(
            max(self.logical_bytes, other.logical_bytes),
            max(self.allocated_bytes, other.allocated_bytes),
            max(self.files, other.files),
            max(self.directories, other.directories),
        )

    def delta(self, before: "StorageUsage") -> dict[str, int]:
        return {
            "logical_bytes": self.logical_bytes - before.logical_bytes,
            "allocated_bytes": self.allocated_bytes - before.allocated_bytes,
            "files": self.files - before.files,
            "directories": self.directories - before.directories,
        }

    def to_dict(self) -> dict[str, int]:
        return {
            "logical_bytes": self.logical_bytes,
            "allocated_bytes": self.allocated_bytes,
            "files": self.files,
            "directories": self.directories,
        }


def _safe_root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise QualificationError("output", "repository_root_not_directory")
    return resolved


def _contained_managed_path(root: Path, relative: Path) -> Path:
    root = _safe_root(root)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise QualificationError("input", "unsafe_relative_path")
    current = root
    for component in relative.parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise QualificationError("input", "symlink_in_managed_path")
    return root / relative


def _ensure_private_directory(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise QualificationError("output", "unsafe_relative_path")
    current = _safe_root(root)
    for component in relative.parts:
        current = current / component
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        metadata = os.lstat(current)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise QualificationError("output", "unsafe_directory")
        os.chmod(current, 0o700)
    return current


def prepare_rr_directory(root: Path, rr_id: str) -> Path:
    if RR_ID.fullmatch(rr_id) is None:
        raise QualificationError("output", "invalid_rr_id")
    parent = _ensure_private_directory(root, QUALIFICATION_RELATIVE)
    destination = parent / rr_id
    try:
        os.mkdir(destination, 0o700)
    except FileExistsError as exc:
        raise QualificationError("output", "rr_already_exists") from exc
    metadata = os.lstat(destination)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise QualificationError("output", "unsafe_rr_directory")
    return destination


def _read_regular_file(path: Path, *, maximum_bytes: int) -> tuple[bytes, os.stat_result]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QualificationError("input", "o_nofollow_unavailable")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | no_follow)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > maximum_bytes
        ):
            raise QualificationError("input", "unsafe_input_file")
        raw = os.read(descriptor, maximum_bytes + 1)
        if len(raw) > maximum_bytes or os.read(descriptor, 1):
            raise QualificationError("input", "input_file_too_large")
        return raw, metadata
    finally:
        os.close(descriptor)


def read_project_token(root: Path) -> tuple[str, tuple[int, int, int, int]]:
    path = _contained_managed_path(root, TOKEN_RELATIVE)
    raw, metadata = _read_regular_file(
        path, maximum_bytes=MAX_PROJECT_TOKEN_LENGTH + 1
    )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise QualificationError("authentication", "unsafe_token_mode")
    token = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        decoded = token.decode("ascii")
    except UnicodeDecodeError as exc:
        raise QualificationError("authentication", "invalid_token") from exc
    if not is_valid_project_token(decoded):
        raise QualificationError("authentication", "invalid_token")
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    return decoded, identity


def token_identity(root: Path) -> tuple[int, int, int, int]:
    _token, identity = read_project_token(root)
    return identity


def _walk_usage(
    paths: Iterable[Path],
    device: int,
    *,
    allow_unfollowed_symlinks: bool = False,
) -> StorageUsage:
    usage = StorageUsage()
    stack = list(paths)
    seen: set[tuple[int, int]] = set()
    entries = 0
    while stack:
        path = stack.pop()
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            continue
        entries += 1
        if entries > MAX_SCAN_ENTRIES:
            raise QualificationError("metrics", "scan_entry_limit")
        if metadata.st_dev != device:
            raise QualificationError("metrics", "cross_device_entry")
        if stat.S_ISLNK(metadata.st_mode):
            if not allow_unfollowed_symlinks:
                raise QualificationError("metrics", "symlink_in_managed_tree")
            usage = usage.combine(
                StorageUsage(metadata.st_size, metadata.st_blocks * 512, 1, 0)
            )
            continue
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        if stat.S_ISREG(metadata.st_mode):
            usage = usage.combine(
                StorageUsage(metadata.st_size, metadata.st_blocks * 512, 1, 0)
            )
        elif stat.S_ISDIR(metadata.st_mode):
            usage = usage.combine(StorageUsage(directories=1))
            try:
                with os.scandir(path) as children:
                    stack.extend(Path(child.path) for child in children)
            except FileNotFoundError:
                continue
        else:
            raise QualificationError("metrics", "special_file_in_managed_tree")
    return usage


def _agent_entries(root: Path, names: set[str]) -> list[Path]:
    agents = root / "data" / "agents"
    if not agents.exists():
        return []
    metadata = os.lstat(agents)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise QualificationError("metrics", "unsafe_agent_data_root")
    result: list[Path] = []
    for entry in os.scandir(agents):
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            raise QualificationError("metrics", "unsafe_agent_data_entry")
        for name in names:
            result.append(Path(entry.path) / name)
    return result


def _run_roots(root: Path) -> list[Path]:
    agents = root / ".runtime" / "agents"
    if not agents.exists():
        return []
    metadata = os.lstat(agents)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise QualificationError("metrics", "unsafe_agent_runtime_root")
    result: list[Path] = []
    for entry in os.scandir(agents):
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            raise QualificationError("metrics", "unsafe_agent_runtime_entry")
        result.append(Path(entry.path) / "runs")
    return result


def measure_storage(root: Path, category: str) -> StorageUsage:
    root = _safe_root(root)
    device = os.stat(root).st_dev
    if category == "state":
        paths = _agent_entries(
            root, {"manifest.json", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm"}
        )
    elif category == "wal":
        paths = _agent_entries(root, {"state.sqlite-wal"})
    elif category == "logs":
        base = root / ".runtime" / "control-plane"
        paths = [base / "gateway.log", *(base / f"gateway.log.{i}" for i in range(1, 4))]
    elif category == "temp":
        paths = [root / ".runtime" / "tmp", *_run_roots(root)]
    elif category == "cache":
        paths = [root / ".runtime" / "cache"]
    else:
        raise QualificationError("metrics", "unknown_storage_category")
    return _walk_usage(
        paths,
        device,
        allow_unfollowed_symlinks=category == "cache",
    )


def _process_marker(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError as exc:
        raise QualificationError("metrics", "process_unavailable") from exc
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split() if closing >= 0 else []
    if len(fields) < 20 or not fields[19].isdigit():
        raise QualificationError("metrics", "invalid_process_marker")
    return fields[19]


def _process_parent_and_group(pid: int) -> tuple[int, int]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError as exc:
        raise QualificationError("metrics", "process_unavailable") from exc
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split() if closing >= 0 else []
    if (
        len(fields) < 3
        or not fields[1].isdigit()
        or not fields[2].isdigit()
    ):
        raise QualificationError("metrics", "invalid_process_relationship")
    return int(fields[1]), int(fields[2])


def _process_command_parts(pid: int) -> tuple[bytes, ...]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise QualificationError("metrics", "process_command_unavailable") from exc
    if not raw or len(raw) > 4096 or not raw.endswith(b"\0"):
        raise QualificationError("metrics", "invalid_process_command")
    parts = tuple(raw[:-1].split(b"\0"))
    if not parts or len(parts) > 32 or any(not part or len(part) > 1024 for part in parts):
        raise QualificationError("metrics", "invalid_process_command")
    return parts


def _process_cwd(pid: int) -> Path:
    try:
        raw = os.readlink(f"/proc/{pid}/cwd")
    except OSError as exc:
        raise QualificationError("metrics", "process_cwd_unavailable") from exc
    return Path(raw)


def read_process_io(pid: int, marker: str) -> dict[str, int]:
    if _process_marker(pid) != marker:
        raise QualificationError("metrics", "process_identity_changed")
    try:
        lines = Path(f"/proc/{pid}/io").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise QualificationError("metrics", "process_io_unavailable") from exc
    parsed: dict[str, int] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in IO_FIELDS and value.strip().isdigit():
            parsed[key] = int(value.strip())
    if set(parsed) != set(IO_FIELDS):
        raise QualificationError("metrics", "process_io_incomplete")
    return parsed


def read_gateway_pid_record(root: Path) -> dict[str, str]:
    path = _contained_managed_path(root, GATEWAY_PID_RELATIVE)
    raw_bytes, metadata = _read_regular_file(path, maximum_bytes=4096)
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise QualificationError("metrics", "unsafe_gateway_pid_mode")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QualificationError("metrics", "invalid_gateway_pid_record") from exc
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise QualificationError("metrics", "invalid_gateway_pid_record")
        values[key] = value
    required = {
        "schema",
        "role",
        "pid",
        "pgid",
        "marker",
        "root",
        "web_pid",
        "web_marker",
    }
    keys = frozenset(values)
    if keys not in {frozenset(required), frozenset({*required, "sync_counter"})}:
        raise QualificationError("metrics", "invalid_gateway_pid_record")
    root_text = str(_safe_root(root))
    if (
        values.get("schema") != "1"
        or values.get("role") != "gateway"
        or values.get("root") != root_text
    ):
        raise QualificationError("metrics", "invalid_gateway_pid_record")
    pid_text = values.get("pid", "")
    pgid_text = values.get("pgid", "")
    web_pid_text = values.get("web_pid", "")
    if (
        not pid_text.isdigit()
        or int(pid_text) <= 1
        or pgid_text != pid_text
        or not web_pid_text.isdigit()
        or int(web_pid_text) <= 1
        or int(web_pid_text) == int(pid_text)
        or re.fullmatch(r"linux:[0-9]+", values.get("marker", "")) is None
        or re.fullmatch(r"linux:[0-9]+", values.get("web_marker", "")) is None
    ):
        raise QualificationError("metrics", "invalid_gateway_pid_record")
    sync_counter = values.get("sync_counter")
    if sync_counter not in {None, COUNTER_ABI}:
        raise QualificationError("metrics", "invalid_gateway_pid_record")
    return values


def read_gateway_pid(root: Path) -> int:
    return int(read_gateway_pid_record(root)["pid"])


def managed_gateway_processes(root: Path) -> dict[str, int]:
    """Resolve and validate the supervisor → Web Gateway process chain."""

    safe_root = _safe_root(root)
    values = read_gateway_pid_record(safe_root)
    supervisor = int(values["pid"])
    gateway = int(values["web_pid"])
    supervisor_marker = values["marker"].removeprefix("linux:")
    gateway_marker = values["web_marker"].removeprefix("linux:")
    if (
        _process_marker(supervisor) != supervisor_marker
        or _process_marker(gateway) != gateway_marker
    ):
        raise QualificationError("metrics", "gateway_process_identity_changed")
    supervisor_parent, supervisor_group = _process_parent_and_group(supervisor)
    gateway_parent, gateway_group = _process_parent_and_group(gateway)
    if (
        supervisor_parent < 1
        or supervisor_group != supervisor
        or gateway_parent != supervisor
        or gateway_group != supervisor
    ):
        raise QualificationError("metrics", "invalid_gateway_process_tree")
    python = os.fsencode(str(safe_root / ".venv" / "bin" / "python"))
    supervisor_command = _process_command_parts(supervisor)
    gateway_command = _process_command_parts(gateway)
    expected_suffix = (python, b"-m", b"agent_builder_v2.web")
    if (
        len(supervisor_command) < 5
        or supervisor_command[0] != python
        or supervisor_command[1]
        != os.fsencode(str(safe_root / "scripts" / "log_supervisor.py"))
        or supervisor_command[-3:] != expected_suffix
        or gateway_command != expected_suffix
        or _process_cwd(supervisor) != safe_root
        or _process_cwd(gateway) != safe_root
    ):
        raise QualificationError("metrics", "invalid_gateway_process_identity")
    return {"supervisor": supervisor, "gateway": gateway}


class MetricSampler:
    def __init__(self, root: Path, processes: dict[str, int]) -> None:
        self.root = _safe_root(root)
        self.processes = processes
        self.markers = {label: _process_marker(pid) for label, pid in processes.items()}
        self.before_storage: dict[str, StorageUsage] = {}
        self.peak_storage: dict[str, StorageUsage] = {}
        self.after_storage: dict[str, StorageUsage] = {}
        self.before_io: dict[str, dict[str, int]] = {}
        self.peak_io: dict[str, dict[str, int]] = {}
        self.after_io: dict[str, dict[str, int]] = {}
        self.error: QualificationError | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self, *, slow: bool) -> None:
        categories = ("state", "wal", "logs", "temp", "cache") if slow else (
            "state", "wal", "logs", "temp"
        )
        for category in categories:
            current = measure_storage(self.root, category)
            self.peak_storage[category] = self.peak_storage.get(category, current).maximum(current)
        for label, pid in self.processes.items():
            current = read_process_io(pid, self.markers[label])
            previous = self.peak_io.get(label, current)
            self.peak_io[label] = {key: max(previous[key], current[key]) for key in IO_FIELDS}

    def start(self) -> None:
        for category in ("state", "wal", "logs", "temp", "cache"):
            current = measure_storage(self.root, category)
            self.before_storage[category] = current
            self.peak_storage[category] = current
        for label, pid in self.processes.items():
            current = read_process_io(pid, self.markers[label])
            self.before_io[label] = current
            self.peak_io[label] = dict(current)

        def run() -> None:
            next_slow = time.monotonic() + SLOW_SAMPLE_SECONDS
            while not self._stop.wait(FAST_SAMPLE_SECONDS):
                try:
                    now = time.monotonic()
                    self._sample(slow=now >= next_slow)
                    if now >= next_slow:
                        next_slow = now + SLOW_SAMPLE_SECONDS
                except QualificationError as exc:
                    self.error = exc
                    self._stop.set()
                    return

        self._thread = threading.Thread(target=run, name="qualification-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.error is not None:
            raise self.error
        self._sample(slow=True)
        for category in ("state", "wal", "logs", "temp", "cache"):
            self.after_storage[category] = measure_storage(self.root, category)
            self.peak_storage[category] = self.peak_storage[category].maximum(
                self.after_storage[category]
            )
        for label, pid in self.processes.items():
            self.after_io[label] = read_process_io(pid, self.markers[label])
            self.peak_io[label] = {
                key: max(self.peak_io[label][key], self.after_io[label][key])
                for key in IO_FIELDS
            }

    def summary(self) -> dict[str, object]:
        storage: dict[str, object] = {}
        for category, before in self.before_storage.items():
            after = self.after_storage[category]
            storage[category] = {
                "before": before.to_dict(),
                "peak": self.peak_storage[category].to_dict(),
                "after": after.to_dict(),
                "growth": after.delta(before),
            }
        process_io: dict[str, object] = {}
        for label, before in self.before_io.items():
            after = self.after_io[label]
            process_io[label] = {
                "before": before,
                "peak": self.peak_io[label],
                "after": after,
                "delta": {key: after[key] - before[key] for key in IO_FIELDS},
            }
        return {"storage": storage, "process_io": process_io}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class ApiClient:
    def __init__(self) -> None:
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(_NoRedirect(), HTTPCookieProcessor(self.cookies))
        self.csrf: str | None = None
        self.forbidden: set[str] = set()

    def _request(
        self,
        method: str,
        path: str,
        expected: set[int],
        body: dict[str, object] | None = None,
    ) -> tuple[int, object | None]:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise QualificationError("http", "unsafe_api_path")
        payload = None
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(payload) > 16_384:
                raise QualificationError("http", "request_body_too_large")
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Origin"] = ORIGIN
        if self.csrf is not None:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(BASE_URL + path, data=payload, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS)
        except HTTPError as exc:
            if exc.code not in expected:
                exc.close()
                raise QualificationError("http", f"unexpected_status_{exc.code}") from None
            response = exc
        except OSError as exc:
            raise QualificationError("http", "transport_error") from exc
        with response:
            status_code = int(response.status)
            if status_code not in expected:
                raise QualificationError("http", f"unexpected_status_{status_code}")
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise QualificationError("http", "response_too_large")
            if not raw:
                return status_code, None
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise QualificationError("http", "wrong_content_type")
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QualificationError("http", "invalid_json_response") from exc
            return status_code, decoded

    def health(self) -> dict[str, object]:
        _status, value = self._request("GET", "/health", {200})
        if not isinstance(value, dict) or value.get("status") != "ok" or value.get("agent_ready") is not True:
            raise QualificationError("health", "gateway_not_ready")
        if not isinstance(value.get("model"), str) or value.get("sandbox") != "landlock+seccomp":
            raise QualificationError("health", "qualification_missing")
        return {"model": value["model"], "sandbox": value["sandbox"]}

    def login(self, token: str) -> None:
        self.forbidden.add(token)
        _status, value = self._request("POST", "/api/auth/login", {200}, {"token": token})
        if not isinstance(value, dict) or not isinstance(value.get("csrf_token"), str):
            raise QualificationError("authentication", "invalid_login_response")
        self.csrf = value["csrf_token"]
        self.forbidden.add(self.csrf)
        for cookie in self.cookies:
            self.forbidden.add(cookie.value)

    def create_conversation(self, title: str) -> str:
        _status, value = self._request("POST", "/api/sessions", {201}, {"title": title})
        session_id = value.get("session_id") if isinstance(value, dict) else None
        if not isinstance(session_id, str) or RUNTIME_ID.fullmatch(session_id) is None:
            raise QualificationError("workload", "invalid_conversation_response")
        self.forbidden.add(session_id)
        return session_id

    def start_run(self, conversation_id: str, message: str) -> str:
        _status, value = self._request(
            "POST", f"/api/sessions/{conversation_id}/runs", {202}, {"message": message}
        )
        run_id = value.get("run_id") if isinstance(value, dict) else None
        if (
            not isinstance(run_id, str)
            or RUNTIME_ID.fullmatch(run_id) is None
            or not isinstance(value, dict)
            or value.get("session_id") != conversation_id
            or value.get("events_url") != f"/api/runs/{run_id}/events"
        ):
            raise QualificationError("workload", "invalid_run_response")
        self.forbidden.add(run_id)
        return run_id

    def reject_oversized_run(self, conversation_id: str) -> None:
        self._request(
            "POST",
            f"/api/sessions/{conversation_id}/runs",
            {400},
            {"message": "x" * 8193},
        )

    def cancel(self, run_id: str) -> None:
        _status, value = self._request("POST", f"/api/runs/{run_id}/cancel", {202})
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise QualificationError("workload", "cancel_not_accepted")

    def consume_terminal(self, run_id: str) -> str:
        request = Request(
            BASE_URL + f"/api/runs/{run_id}/events",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-store"},
            method="GET",
        )
        try:
            response = self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS)
        except (HTTPError, OSError) as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            raise QualificationError("sse", "stream_open_failed") from exc
        expected_seq = 1
        events = 0
        data_lines: list[str] = []
        try:
            if response.status != 200 or response.headers.get_content_type() != "text/event-stream":
                raise QualificationError("sse", "invalid_stream_response")
            while True:
                raw = response.readline(MAX_SSE_LINE_BYTES + 1)
                if len(raw) > MAX_SSE_LINE_BYTES:
                    raise QualificationError("sse", "sse_line_too_large")
                if not raw:
                    raise QualificationError("sse", "stream_ended_without_terminal")
                try:
                    line = raw.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError as exc:
                    raise QualificationError("sse", "invalid_sse_utf8") from exc
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line != "" or not data_lines:
                    continue
                events += 1
                if events > MAX_SSE_EVENTS:
                    raise QualificationError("sse", "sse_event_limit")
                try:
                    envelope = json.loads("\n".join(data_lines))
                except json.JSONDecodeError as exc:
                    raise QualificationError("sse", "invalid_sse_json") from exc
                data_lines.clear()
                if not isinstance(envelope, dict):
                    raise QualificationError("sse", "invalid_event_envelope")
                if envelope.get("run_id") != run_id or envelope.get("seq") != expected_seq:
                    raise QualificationError("sse", "event_identity_or_sequence_error")
                expected_seq += 1
                kind = envelope.get("kind")
                if kind in TERMINALS:
                    return str(kind)
        finally:
            response.close()

    def delete_and_verify(self, conversation_id: str, run_ids: list[str]) -> None:
        self._request("DELETE", f"/api/sessions/{conversation_id}", {204})
        self._request("GET", f"/api/sessions/{conversation_id}", {404})
        for run_id in run_ids:
            self._request("GET", f"/api/runs/{run_id}/events", {404})

    def logout(self) -> None:
        if self.csrf is not None:
            self._request("POST", "/api/auth/logout", {204})
            self.csrf = None


class Workload:
    def __init__(self, turns: int, deadline: float) -> None:
        self.turns = turns
        self.deadline = deadline
        self.client = ApiClient()
        self.conversations: dict[str, list[str]] = {}
        self.active_runs: set[str] = set()
        self.counts = {
            "health_checks": 0,
            "conversations_created": 0,
            "conversations_deleted_and_verified": 0,
            "completed_turns": 0,
            "cancelled_runs": 0,
            "rejected_requests": 0,
        }
        self.runtime = {"model": "unknown", "sandbox": "unknown"}

    def _time(self) -> None:
        if time.monotonic() >= self.deadline:
            raise QualificationError("workload", "wall_deadline_exceeded")

    def run(self, token: str) -> None:
        self._time()
        self.runtime = self.client.health()
        self.counts["health_checks"] += 1
        self.client.login(token)

        main = self.client.create_conversation("Runtime qualification")
        self.conversations[main] = []
        self.counts["conversations_created"] += 1
        for index in range(self.turns):
            self._time()
            run_id = self.client.start_run(
                main, f"Qualification turn {index + 1} of {self.turns}. Reply briefly."
            )
            self.conversations[main].append(run_id)
            self.active_runs.add(run_id)
            terminal = self.client.consume_terminal(run_id)
            self.active_runs.discard(run_id)
            if terminal != "run.completed":
                raise QualificationError("workload", "turn_did_not_complete")
            self.counts["completed_turns"] += 1

        self.client.reject_oversized_run(main)
        self.counts["rejected_requests"] += 1

        cancellation = self.client.create_conversation("Cancellation qualification")
        self.conversations[cancellation] = []
        self.counts["conversations_created"] += 1
        cancel_run = self.client.start_run(
            cancellation,
            "Write a very long numbered explanation with many distinct paragraphs before stopping.",
        )
        self.conversations[cancellation].append(cancel_run)
        self.active_runs.add(cancel_run)
        self.client.cancel(cancel_run)
        terminal = self.client.consume_terminal(cancel_run)
        self.active_runs.discard(cancel_run)
        if terminal != "run.cancelled":
            raise QualificationError("workload", "cancel_did_not_converge")
        self.counts["cancelled_runs"] += 1

        for conversation_id, run_ids in list(self.conversations.items()):
            self.client.delete_and_verify(conversation_id, run_ids)
            self.counts["conversations_deleted_and_verified"] += 1
            del self.conversations[conversation_id]

        self.runtime = self.client.health()
        self.counts["health_checks"] += 1
        self.client.logout()

    def cleanup(self) -> None:
        for run_id in list(self.active_runs):
            try:
                self.client.cancel(run_id)
            except Exception:
                pass
        deadline = time.monotonic() + 15
        for conversation_id, run_ids in list(self.conversations.items()):
            while time.monotonic() < deadline:
                try:
                    self.client.delete_and_verify(conversation_id, run_ids)
                except QualificationError:
                    time.sleep(0.25)
                    continue
                self.counts["conversations_deleted_and_verified"] += 1
                del self.conversations[conversation_id]
                break
        try:
            self.client.logout()
        except Exception:
            pass


def audit_run_residuals(root: Path) -> dict[str, object]:
    run_entries = 0
    worker_pid_records = 0
    for runs in _run_roots(_safe_root(root)):
        if not runs.exists():
            continue
        for directory, subdirectories, filenames in os.walk(runs, followlinks=False):
            run_entries += len(subdirectories) + len(filenames)
            worker_pid_records += sum(name == "worker.pid" for name in filenames)
            if run_entries > MAX_SCAN_ENTRIES:
                raise QualificationError("residual", "residual_scan_limit")
            for name in subdirectories + filenames:
                if (Path(directory) / name).is_symlink():
                    raise QualificationError("residual", "symlink_in_run_roots")
    return {"run_entries": run_entries, "worker_pid_records": worker_pid_records}


def evaluate(metrics: dict[str, object], residual: dict[str, object]) -> list[str]:
    findings: list[str] = []
    storage = metrics["storage"]
    assert isinstance(storage, dict)

    def value(category: str, section: str, field: str) -> int:
        return int(storage[category][section][field])  # type: ignore[index]

    checks = (
        ("state_after_logical_growth", value("state", "growth", "logical_bytes"), THRESHOLDS["state_after_logical_growth_bytes"]),
        ("state_after_allocated_growth", value("state", "growth", "allocated_bytes"), THRESHOLDS["state_after_allocated_growth_bytes"]),
        ("wal_peak_logical", value("wal", "peak", "logical_bytes"), THRESHOLDS["wal_peak_logical_bytes"]),
        ("logs_peak_logical", value("logs", "peak", "logical_bytes"), THRESHOLDS["logs_peak_logical_bytes"]),
        ("cache_after_logical_growth", value("cache", "growth", "logical_bytes"), THRESHOLDS["cache_after_logical_growth_bytes"]),
        ("cache_after_allocated_growth", value("cache", "growth", "allocated_bytes"), THRESHOLDS["cache_after_allocated_growth_bytes"]),
        ("temp_peak_logical", value("temp", "peak", "logical_bytes"), THRESHOLDS["temp_peak_logical_bytes"]),
        ("temp_peak_allocated", value("temp", "peak", "allocated_bytes"), THRESHOLDS["temp_peak_allocated_bytes"]),
        ("temp_after_logical_growth", value("temp", "growth", "logical_bytes"), THRESHOLDS["temp_after_logical_growth_bytes"]),
        ("temp_after_allocated_growth", value("temp", "growth", "allocated_bytes"), THRESHOLDS["temp_after_allocated_growth_bytes"]),
    )
    for code, observed, limit in checks:
        if observed > limit:
            findings.append(code)
    process_io = metrics["process_io"]
    assert isinstance(process_io, dict)
    for label, values in process_io.items():
        delta = values["delta"]  # type: ignore[index]
        if delta["write_bytes"] > THRESHOLDS["process_write_bytes_delta"]:
            findings.append(f"{label}_write_bytes_delta")
        if delta["syscw"] > THRESHOLDS["process_syscw_delta"]:
            findings.append(f"{label}_syscw_delta")
    if residual["run_entries"] != 0:
        findings.append("run_entries_remain")
    if residual["worker_pid_records"] != 0:
        findings.append("worker_pid_records_remain")
    return sorted(findings)


def _platform_summary() -> dict[str, str]:
    libc_name, libc_version = platform.libc_ver()
    return {
        "architecture": platform.machine().lower(),
        "kernel": platform.release(),
        "libc": f"{libc_name}-{libc_version}",
        "python": platform.python_version(),
    }


def _qualification_limitations(
    architecture: str,
    *,
    observed_libc_sync_calls: bool,
) -> list[str]:
    limitations = list(BASE_LIMITATIONS)
    if architecture not in {"aarch64", "arm64"}:
        limitations.insert(0, "aarch64_native_qualification_not_included")
    if not observed_libc_sync_calls:
        limitations.append("exact_libc_sync_call_count_not_observed")
    limitations.append("non_libc_and_direct_syscall_durability_not_observed")
    return limitations


def _expected_worker_process_images(workload_counts: dict[str, int]) -> int:
    return workload_counts.get("completed_turns", 0) + workload_counts.get(
        "cancelled_runs", 0
    )


def encode_summary(summary: dict[str, object], forbidden: Iterable[str], root: Path) -> bytes:
    encoded = (json.dumps(summary, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise QualificationError("output", "summary_too_large")
    text = encoded.decode("utf-8")
    forbidden_values = [value for value in forbidden if value]
    forbidden_values.append(str(_safe_root(root)))
    if any(value in text for value in forbidden_values):
        raise QualificationError("output", "summary_redaction_failed")
    if re.search(r'"(?:conversation|session|turn|run)_id"', text):
        raise QualificationError("output", "runtime_id_field_in_summary")
    return encoded


def publish_summary(directory: Path, encoded: bytes) -> Path:
    metadata = os.lstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise QualificationError("output", "unsafe_rr_directory")
    final = directory / "summary.json"
    temporary = directory / f".summary.{os.getpid()}.{time.monotonic_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QualificationError("output", "o_nofollow_unavailable")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags | no_follow, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise QualificationError("output", "summary_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, final, follow_symlinks=False)
        except FileExistsError as exc:
            raise QualificationError("output", "summary_already_exists") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return final


def parse_processes(root: Path, values: list[str]) -> dict[str, int]:
    processes = managed_gateway_processes(root)
    if len(values) > MAX_EXTRA_PIDS:
        raise QualificationError("arguments", "too_many_pids")
    for value in values:
        label, separator, pid_text = value.partition("=")
        if (
            not separator
            or PROCESS_LABEL.fullmatch(label) is None
            or label in processes
            or not pid_text.isdigit()
            or int(pid_text) <= 1
        ):
            raise QualificationError("arguments", "invalid_pid_specification")
        processes[label] = int(pid_text)
    return processes


def run_qualification(args: argparse.Namespace) -> tuple[int, Path]:
    root = _safe_root(REPOSITORY_ROOT)
    rr_directory = prepare_rr_directory(root, args.rr_id)
    started = time.monotonic()
    recorded_at = datetime.now(timezone.utc).isoformat()
    forbidden: set[str] = set()
    failure: dict[str, str] | None = None
    metrics: dict[str, object] = {"storage": {}, "process_io": {}}
    residual: dict[str, object] = {
        "run_entries": -1,
        "worker_pid_records": -1,
        "api_resources_deleted": False,
        "gateway_identity_stable": False,
        "token_file_identity_stable": False,
        "findings": [],
    }
    workload_counts: dict[str, int] = {}
    runtime = {"model": "unknown", "sandbox": "unknown"}
    sampler: MetricSampler | None = None
    workload: Workload | None = None
    managed_process_markers: dict[str, tuple[int, str]] = {}
    initial_token_identity: tuple[int, int, int, int] | None = None
    sync_counter_enabled = False
    sync_counter_before: dict[str, object] | None = None
    sync_counter_after: dict[str, object] | None = None
    try:
        token, initial_token_identity = read_project_token(root)
        forbidden.add(token)
        gateway_record = read_gateway_pid_record(root)
        sync_counter_enabled = gateway_record.get("sync_counter") == COUNTER_ABI
        if sync_counter_enabled:
            try:
                sync_counter_before = read_sync_counter(root)
            except SyncCounterError as exc:
                raise QualificationError("metrics", "sync_counter_unavailable") from exc
            before_roles = sync_counter_before.get("roles")
            if (
                not isinstance(before_roles, dict)
                or set(before_roles) != {"supervisor", "gateway"}
                or sync_counter_before.get("ready_slots") != 2
            ):
                raise QualificationError("preflight", "sync_counter_not_fresh")
        processes = parse_processes(root, args.pid)
        managed_process_markers = {
            label: (processes[label], _process_marker(processes[label]))
            for label in ("supervisor", "gateway")
        }
        preexisting = audit_run_residuals(root)
        if preexisting["run_entries"] or preexisting["worker_pid_records"]:
            raise QualificationError("preflight", "preexisting_run_residuals")
        sampler = MetricSampler(root, processes)
        sampler.start()
        workload = Workload(args.turns, started + WORKLOAD_TIMEOUT_SECONDS)
        workload.run(token)
        runtime = workload.runtime
    except QualificationError as exc:
        failure = {"stage": exc.stage, "code": exc.code}
    except Exception:
        failure = {"stage": "internal", "code": "unexpected_exception"}
    finally:
        if workload is not None:
            workload.cleanup()
            forbidden.update(workload.client.forbidden)
            workload_counts = dict(workload.counts)
            runtime = workload.runtime
        if sampler is not None:
            try:
                sampler.stop()
                metrics = sampler.summary()
            except QualificationError as exc:
                if failure is None:
                    failure = {"stage": exc.stage, "code": exc.code}
        if sync_counter_enabled:
            try:
                if sync_counter_before is None:
                    raise SyncCounterError("missing initial sync counter snapshot")
                sync_counter_after = read_sync_counter(root)
                metrics["libc_sync_calls"] = sync_counter_delta(
                    sync_counter_before, sync_counter_after
                )
            except SyncCounterError:
                if failure is None:
                    failure = {"stage": "metrics", "code": "sync_counter_invalid"}
        try:
            run_residuals = audit_run_residuals(root)
            residual.update(run_residuals)
            residual["api_resources_deleted"] = bool(
                workload is not None
                and not workload.conversations
                and not workload.active_runs
                and workload.counts["conversations_created"]
                == workload.counts["conversations_deleted_and_verified"]
            )
            residual["gateway_identity_stable"] = bool(
                managed_process_markers
                and all(
                    _process_marker(pid) == marker
                    for pid, marker in managed_process_markers.values()
                )
            )
            residual["token_file_identity_stable"] = bool(
                initial_token_identity is not None and token_identity(root) == initial_token_identity
            )
        except QualificationError as exc:
            if failure is None:
                failure = {"stage": exc.stage, "code": exc.code}

    findings: list[str] = []
    if metrics.get("storage"):
        findings.extend(evaluate(metrics, residual))
    else:
        findings.append("metrics_unavailable")
    for field in ("api_resources_deleted", "gateway_identity_stable", "token_file_identity_stable"):
        if residual[field] is not True:
            findings.append(field)
    if sync_counter_enabled and sync_counter_after is not None:
        roles = sync_counter_after.get("roles")
        expected_workers = _expected_worker_process_images(workload_counts)
        worker_images = 0
        if isinstance(roles, dict) and isinstance(roles.get("worker"), dict):
            value = roles["worker"].get("process_images")
            if isinstance(value, int) and not isinstance(value, bool):
                worker_images = value
        if worker_images < expected_workers:
            findings.append("sync_counter_worker_coverage")
    residual["findings"] = sorted(set(findings))
    passed = failure is None and not findings
    platform_summary = _platform_summary()
    summary: dict[str, object] = {
        "schema": 1,
        "policy": "runtime-qualification-v1",
        "rr_id": args.rr_id,
        "implementation_ref": args.implementation_ref,
        "result": "pass" if passed else "fail",
        "recorded_at": recorded_at,
        "duration_seconds": round(time.monotonic() - started, 3),
        "platform": platform_summary,
        "runtime": runtime,
        "workload": {
            "configured_turns": args.turns,
            "counts": workload_counts,
            "wall_deadline_seconds": int(WORKLOAD_TIMEOUT_SECONDS),
        },
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "residual_audit": residual,
        "failure": failure,
        "limitations": _qualification_limitations(
            platform_summary["architecture"],
            observed_libc_sync_calls="libc_sync_calls" in metrics,
        ),
    }
    encoded = encode_summary(summary, forbidden, root)
    path = publish_summary(rr_directory, encoded)
    return (0 if passed else 1), path


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded local runtime qualification")
    parser.add_argument("--rr-id", required=True)
    parser.add_argument("--implementation-ref", default="worktree")
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--pid", action="append", default=[], metavar="LABEL=PID")
    args = parser.parse_args(argv)
    if RR_ID.fullmatch(args.rr_id) is None:
        parser.error("--rr-id must match RR-QUA-YYYYMMDD-NN")
    if IMPLEMENTATION_REF.fullmatch(args.implementation_ref) is None:
        parser.error("--implementation-ref must be worktree or a full lowercase commit SHA")
    if not MIN_TURNS <= args.turns <= MAX_TURNS:
        parser.error(f"--turns must be between {MIN_TURNS} and {MAX_TURNS}")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        status, path = run_qualification(arguments(argv))
    except QualificationError as exc:
        print(f"qualification setup failed: {exc.stage}/{exc.code}", file=sys.stderr)
        return 2
    relative = path.relative_to(REPOSITORY_ROOT)
    print(f"qualification {'PASS' if status == 0 else 'FAIL'}: {relative}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

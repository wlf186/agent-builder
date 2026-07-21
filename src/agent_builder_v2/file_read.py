"""Descriptor-anchored, fail-closed reads inside one Agent workspace."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .capsule import AgentCapsule
from .permissions import CapabilityRequest


MAX_FILE_BYTES = 1024 * 1024
MAX_READ_BYTES = 4 * 1024
MAX_READ_LINES = 256
MAX_PATH_BYTES = 1024
MAX_PATH_COMPONENTS = 32
READ_TIMEOUT_SECONDS = 0.5
_READ_CAPABILITIES = frozenset({"file/stat", "file/read_text"})


class FileReadError(RuntimeError):
    """A requested file could not be observed without weakening containment."""


@dataclass(frozen=True, slots=True)
class CapturedWorkspaceFile:
    path: str
    content: bytes
    metadata: os.stat_result
    identity_digest: str
    content_digest: str


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + payload).hexdigest()


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
    )


def _validate_relative_path(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise FileReadError("invalid workspace-relative path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FileReadError("invalid workspace-relative path") from exc
    candidate = Path(value)
    parts = candidate.parts
    if (
        candidate.is_absolute()
        or len(encoded) > MAX_PATH_BYTES
        or not 1 <= len(parts) <= MAX_PATH_COMPONENTS
        or any(part in {"", ".", ".."} or "/" in part for part in parts)
    ):
        raise FileReadError("invalid workspace-relative path")
    normalized = "/".join(parts)
    if normalized != value:
        raise FileReadError("workspace path is not canonical")
    return normalized, parts


def validate_workspace_relative_path(value: object) -> tuple[str, tuple[str, ...]]:
    """Public path codec shared by descriptor-anchored file capabilities."""

    return _validate_relative_path(value)


def _require_safe_directory(metadata: os.stat_result, root_device: int) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_dev != root_device
    ):
        raise FileReadError("unsafe workspace directory")


def require_safe_file_metadata(
    metadata: os.stat_result, root_device: int
) -> None:
    allocated = metadata.st_blocks * 512
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_dev != root_device
        or not 0 <= metadata.st_size <= MAX_FILE_BYTES
        or (metadata.st_size > 0 and allocated < metadata.st_size)
    ):
        raise FileReadError("unsafe workspace file")


def _require_text(raw: bytes) -> str:
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileReadError("workspace file is not UTF-8 text") from exc
    if "\x00" in content or any(
        ord(character) < 32 and character not in "\t\n\r" for character in content
    ):
        raise FileReadError("workspace file is binary")
    return content


def capture_workspace_file(
    capsule: AgentCapsule, relative_path: object
) -> CapturedWorkspaceFile:
    path, parts = _validate_relative_path(relative_path)
    workspace = capsule.data_root / "workspace"
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        raise FileReadError("descriptor-anchored reads are unavailable")
    try:
        named_root = os.lstat(workspace)
        root_fd = os.open(
            workspace,
            os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
        )
    except OSError as exc:
        raise FileReadError("workspace is unavailable") from exc
    descriptors = [root_fd]
    file_fd: int | None = None
    started = time.monotonic()
    try:
        opened_root = os.fstat(root_fd)
        _require_safe_directory(opened_root, opened_root.st_dev)
        if _identity(opened_root) != _identity(named_root):
            raise FileReadError("workspace changed during read")
        current_fd = root_fd
        for component in parts[:-1]:
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
                dir_fd=current_fd,
            )
            descriptors.append(child_fd)
            child = os.fstat(child_fd)
            _require_safe_directory(child, opened_root.st_dev)
            current_fd = child_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY
            | os.O_CLOEXEC
            | no_follow
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current_fd,
        )
        before = os.fstat(file_fd)
        require_safe_file_metadata(before, opened_root.st_dev)
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            if time.monotonic() - started > READ_TIMEOUT_SECONDS:
                raise FileReadError("workspace read exceeded its time limit")
            chunk = os.read(file_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != before.st_size or os.read(file_fd, 1):
            raise FileReadError("workspace file changed size during read")
        after = os.fstat(file_fd)
        named_file = os.stat(parts[-1], dir_fd=current_fd, follow_symlinks=False)
        final_root = os.fstat(root_fd)
        current_named_root = os.lstat(workspace)
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(named_file)
            or _identity(opened_root) != _identity(final_root)
            or _identity(final_root) != _identity(current_named_root)
        ):
            raise FileReadError("workspace file changed during read")
        _require_text(raw)
    except OSError as exc:
        raise FileReadError("workspace file could not be read safely") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    identity_payload = _canonical(
        {
            "agent_id": capsule.agent_id,
            "generation": capsule.generation,
            "path": path,
            "device": before.st_dev,
            "inode": before.st_ino,
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
            "mode": stat.S_IMODE(before.st_mode),
        }
    ).encode("utf-8")
    return CapturedWorkspaceFile(
        path=path,
        content=raw,
        metadata=before,
        identity_digest=_digest(b"agent-builder-file-identity-v1", identity_payload),
        content_digest=_digest(b"agent-builder-file-content-v1", raw),
    )


def file_receipt(captured: CapturedWorkspaceFile) -> dict[str, object]:
    return {
        "path": captured.path,
        "path_identity": captured.identity_digest,
        "content_digest": captured.content_digest,
        "size_bytes": captured.metadata.st_size,
        "mtime_ns": captured.metadata.st_mtime_ns,
        "mode": format(stat.S_IMODE(captured.metadata.st_mode), "04o"),
    }


class FileReadExecutor:
    """Trusted Control Plane executor scoped to one immutable Capsule identity."""

    executor_kind = "workspace-file-read-v1"

    def __init__(self, capsule: AgentCapsule) -> None:
        self._capsule = capsule
        root = os.lstat(capsule.data_root / "workspace")
        self.identity_digest = _digest(
            b"agent-builder-file-executor-v1",
            _canonical(
                {
                    "agent_id": capsule.agent_id,
                    "generation": capsule.generation,
                    "device": root.st_dev,
                    "inode": root.st_ino,
                }
            ).encode("utf-8"),
        )

    def execute(self, request: CapabilityRequest, cancelled: object) -> str:
        if request.context.tool_id not in _READ_CAPABILITIES:
            raise FileReadError("unsupported file capability")
        if callable(cancelled) and cancelled():
            raise FileReadError("file capability was cancelled")
        try:
            arguments = json.loads(request.arguments_json)
        except json.JSONDecodeError as exc:
            raise FileReadError("invalid file capability arguments") from exc
        if not isinstance(arguments, dict):
            raise FileReadError("invalid file capability arguments")
        captured = capture_workspace_file(self._capsule, arguments.get("path"))
        receipt = file_receipt(captured)
        if request.context.tool_id == "file/stat":
            return _canonical(
                {
                    "schema_version": 1,
                    "kind": "file_stat",
                    "receipt": receipt,
                    "truncated": False,
                }
            )

        allowed = {"path", "offset_bytes", "line_offset", "max_bytes", "max_lines"}
        if not set(arguments).issubset(allowed):
            raise FileReadError("invalid file read arguments")
        offset = arguments.get("offset_bytes", 0)
        line_offset = arguments.get("line_offset", 0)
        max_bytes = arguments.get("max_bytes", MAX_READ_BYTES)
        max_lines = arguments.get("max_lines", MAX_READ_LINES)
        if (
            any(not isinstance(item, int) or isinstance(item, bool) for item in (
                offset, line_offset, max_bytes, max_lines
            ))
            or not 0 <= offset <= MAX_FILE_BYTES
            or not 0 <= line_offset <= 100_000
            or not 1 <= max_bytes <= MAX_READ_BYTES
            or not 1 <= max_lines <= MAX_READ_LINES
            or (offset and line_offset)
        ):
            raise FileReadError("invalid file read limits")
        raw = captured.content
        if line_offset:
            lines = raw.splitlines(keepends=True)
            start = sum(len(item) for item in lines[:line_offset])
        else:
            start = offset
        if start > len(raw):
            start = len(raw)
        try:
            raw[:start].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileReadError("byte offset splits a UTF-8 sequence") from exc
        end = min(len(raw), start + max_bytes)
        while end > start:
            try:
                selected = raw[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            selected = ""
        selected_lines = selected.splitlines(keepends=True)
        if len(selected_lines) > max_lines:
            selected = "".join(selected_lines[:max_lines])
            end = start + len(selected.encode("utf-8"))
        truncated = start > 0 or end < len(raw)
        return _canonical(
            {
                "schema_version": 1,
                "kind": "file_read_text",
                "receipt": receipt,
                "content": selected,
                "range": {
                    "start_byte": start,
                    "returned_bytes": len(selected.encode("utf-8")),
                    "line_offset": line_offset,
                    "returned_lines": len(selected.splitlines()),
                },
                "truncated": truncated,
                "truncation_reason": "bounded_range" if truncated else "none",
            }
        )


__all__ = [
    "FileReadError",
    "FileReadExecutor",
    "CapturedWorkspaceFile",
    "MAX_FILE_BYTES",
    "MAX_PATH_BYTES",
    "MAX_READ_BYTES",
    "MAX_READ_LINES",
    "READ_TIMEOUT_SECONDS",
    "capture_workspace_file",
    "file_receipt",
    "require_safe_file_metadata",
    "validate_workspace_relative_path",
]

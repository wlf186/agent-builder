"""Receipt-bound, descriptor-anchored atomic workspace mutations."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import difflib
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from threading import Lock
from typing import Mapping

from .capsule import AgentCapsule
from .file_read import (
    MAX_FILE_BYTES,
    FileReadError,
    capture_workspace_file,
    file_receipt,
    require_safe_file_metadata,
    validate_workspace_relative_path,
)
from .permissions import CapabilityOutcomeUnknownError, CapabilityRequest


MAX_MUTATION_CONTENT_BYTES = 8 * 1024
MAX_EDIT_FRAGMENT_BYTES = 4 * 1024
MAX_DIFF_BYTES = 4 * 1024
MAX_TEMP_FILES = 16
MAX_CLEANUP_ENTRIES = 4_096
_MUTATION_CAPABILITIES = frozenset({"file/edit", "file/write"})
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


class FileWriteError(RuntimeError):
    """A mutation could not be proven safe and was not committed."""


class FileWriteOutcomeUnknownError(FileWriteError, CapabilityOutcomeUnknownError):
    """The atomic commit may have happened and must never be replayed."""


@dataclass(frozen=True, slots=True)
class FullReadReceipt:
    path: str
    path_identity: str
    content_digest: str
    size_bytes: int

    @classmethod
    def from_result(cls, value: object) -> FullReadReceipt:
        if not isinstance(value, dict) or value.get("kind") != "file_read_text":
            raise FileWriteError("full read receipt is invalid")
        receipt = value.get("receipt")
        returned = value.get("range")
        if (
            value.get("truncated") is not False
            or not isinstance(receipt, dict)
            or not isinstance(returned, dict)
            or returned.get("start_byte") != 0
            or returned.get("returned_bytes") != receipt.get("size_bytes")
        ):
            raise FileWriteError("a complete file read is required")
        path = receipt.get("path")
        identity = receipt.get("path_identity")
        digest = receipt.get("content_digest")
        size = receipt.get("size_bytes")
        if (
            not isinstance(path, str)
            or not isinstance(identity, str)
            or len(identity) != 64
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_FILE_BYTES
        ):
            raise FileWriteError("full read receipt is invalid")
        return cls(path, identity, digest, size)


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


def _validate_text(value: object, maximum: int, field: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise FileWriteError(f"invalid {field}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FileWriteError(f"invalid {field}") from exc
    if len(encoded) > maximum or any(
        ord(character) < 32 and character not in "\t\n\r" for character in value
    ):
        raise FileWriteError(f"invalid {field}")
    return value


def _diff(path: str, before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )
    value = "".join(lines)
    if not value or len(value.encode("utf-8")) > MAX_DIFF_BYTES:
        raise FileWriteError("mutation diff is empty or exceeds its approval limit")
    return value


def _directory_identity(
    capsule: AgentCapsule, path: str, metadata: os.stat_result
) -> str:
    return _digest(
        b"agent-builder-write-parent-v1",
        _canonical(
            {
                "agent_id": capsule.agent_id,
                "generation": capsule.generation,
                "path": path,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "mtime_ns": metadata.st_mtime_ns,
                "ctime_ns": metadata.st_ctime_ns,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        ).encode("utf-8"),
    )


def _renameat2(
    source_fd: int,
    source: str,
    target_fd: int,
    target: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise FileWriteError("atomic renameat2 is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(
        source_fd,
        source.encode("utf-8"),
        target_fd,
        target.encode("utf-8"),
        flags,
    ) != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value))


class FileMutationExecutor:
    """Prepare approval-bound diffs and atomically commit one target."""

    executor_kind = "workspace-file-mutation-v1"

    def __init__(self, capsule: AgentCapsule) -> None:
        self._capsule = capsule
        self._workspace = capsule.data_root / "workspace"
        root = os.lstat(self._workspace)
        self._root_identity = (
            root.st_dev,
            root.st_ino,
            root.st_mode,
            root.st_uid,
        )
        self.identity_digest = _digest(
            b"agent-builder-file-mutation-executor-v1",
            _canonical(
                {
                    "agent_id": capsule.agent_id,
                    "generation": capsule.generation,
                    "device": root.st_dev,
                    "inode": root.st_ino,
                    "renameat2": True,
                }
            ).encode("utf-8"),
        )
        self._locks_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._cleanup_stale_temps()

    def _target_lock(self, path: str) -> Lock:
        with self._locks_guard:
            lock = self._locks.get(path)
            if lock is None:
                lock = Lock()
                self._locks[path] = lock
            return lock

    def _cleanup_stale_temps(self) -> None:
        removed = 0
        entries = 0
        for parent, directories, files in os.walk(
            self._workspace, topdown=True, followlinks=False
        ):
            entries += len(directories) + len(files)
            if entries > MAX_CLEANUP_ENTRIES:
                raise FileWriteError("mutation temp cleanup capacity exceeded")
            directories[:] = [
                item
                for item in directories
                if not Path(parent, item).is_symlink()
            ]
            for name in files:
                if not name.startswith(".agent-builder-write-") or not name.endswith(
                    ".tmp"
                ):
                    continue
                removed += 1
                if removed > MAX_TEMP_FILES:
                    raise FileWriteError("stale mutation temp capacity exceeded")
                candidate = Path(parent, name)
                metadata = os.lstat(candidate)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                    or metadata.st_size > MAX_FILE_BYTES
                ):
                    raise FileWriteError("unsafe stale mutation temp")
                os.unlink(candidate)

    def prepare(
        self,
        tool_id: str,
        arguments: Mapping[str, str | int | bool],
        receipts: Mapping[str, FullReadReceipt],
    ) -> tuple[dict[str, object], str]:
        if tool_id not in _MUTATION_CAPABILITIES:
            raise FileWriteError("unsupported file mutation")
        path, parts = validate_workspace_relative_path(arguments.get("path"))
        if parts[-1].startswith(".agent-builder-write-"):
            raise FileWriteError("mutation target uses reserved internal namespace")
        create = tool_id == "file/write" and arguments.get("create") is True
        before = ""
        prepared_receipt: dict[str, object]
        if create:
            parent_path = "/".join(parts[:-1])
            parent = self._workspace.joinpath(*parts[:-1])
            parent_metadata = os.lstat(parent)
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != os.getuid()
                or stat.S_IMODE(parent_metadata.st_mode) & 0o022
                or parent_metadata.st_dev != self._root_identity[0]
            ):
                raise FileWriteError("unsafe mutation parent")
            parent_fd = os.open(
                parent,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                try:
                    os.stat(
                        parts[-1], dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise FileWriteError("create target already exists")
            finally:
                os.close(parent_fd)
            prepared_receipt = {
                "kind": "target_absent",
                "path": path,
                "parent_path": parent_path,
                "parent_identity": _directory_identity(
                    self._capsule, parent_path, parent_metadata
                ),
            }
        else:
            trusted = receipts.get(path)
            expected_identity = arguments.get("path_identity")
            expected_content = arguments.get("content_digest")
            if (
                trusted is None
                or expected_identity != trusted.path_identity
                or expected_content != trusted.content_digest
            ):
                raise FileWriteError("mutation requires this Run's complete read receipt")
            captured = capture_workspace_file(self._capsule, path)
            if (
                captured.identity_digest != trusted.path_identity
                or captured.content_digest != trusted.content_digest
                or captured.metadata.st_size != trusted.size_bytes
            ):
                raise FileWriteError("mutation read receipt is stale")
            before = captured.content.decode("utf-8")
            prepared_receipt = {"kind": "existing", **file_receipt(captured)}

        if tool_id == "file/edit":
            old = _validate_text(
                arguments.get("old_text"), MAX_EDIT_FRAGMENT_BYTES, "old_text"
            )
            new = _validate_text(
                arguments.get("new_text"), MAX_EDIT_FRAGMENT_BYTES, "new_text"
            )
            if not old or before.count(old) != 1:
                raise FileWriteError("edit match must occur exactly once")
            after = before.replace(old, new, 1)
        else:
            after = _validate_text(
                arguments.get("content"), MAX_MUTATION_CONTENT_BYTES, "content"
            )
        encoded_after = after.encode("utf-8")
        if len(encoded_after) > MAX_FILE_BYTES or after == before:
            raise FileWriteError("mutation result is unchanged or too large")
        diff = _diff(path, before, after)
        prepared = {
            "schema_version": 1,
            "tool_id": tool_id,
            "path": path,
            "receipt": prepared_receipt,
            "new_content": after,
            "new_content_digest": _digest(
                b"agent-builder-file-content-v1", encoded_after
            ),
            "diff_digest": _digest(
                b"agent-builder-file-diff-v1", diff.encode("utf-8")
            ),
        }
        preview = _canonical(
            {
                "action": tool_id,
                "path": path,
                "receipt": prepared_receipt,
                "diff": diff,
                "diff_digest": prepared["diff_digest"],
                "new_content_digest": prepared["new_content_digest"],
            }
        )
        if len(preview.encode("utf-8")) > MAX_DIFF_BYTES:
            raise FileWriteError("mutation approval preview exceeds its limit")
        return prepared, preview

    def execute(self, request: CapabilityRequest, cancelled: object) -> str:
        if request.context.tool_id not in _MUTATION_CAPABILITIES:
            raise FileWriteError("unsupported file mutation")
        try:
            prepared = json.loads(request.arguments_json)
        except json.JSONDecodeError as exc:
            raise FileWriteError("invalid prepared mutation") from exc
        if (
            not isinstance(prepared, dict)
            or set(prepared)
            != {
                "schema_version",
                "tool_id",
                "path",
                "receipt",
                "new_content",
                "new_content_digest",
                "diff_digest",
            }
            or prepared.get("schema_version") != 1
            or prepared.get("tool_id") != request.context.tool_id
            or not isinstance(prepared.get("receipt"), dict)
        ):
            raise FileWriteError("invalid prepared mutation")
        path, parts = validate_workspace_relative_path(prepared.get("path"))
        if parts[-1].startswith(".agent-builder-write-"):
            raise FileWriteError("mutation target uses reserved internal namespace")
        content = _validate_text(
            prepared.get("new_content"), MAX_MUTATION_CONTENT_BYTES, "new_content"
        )
        content_bytes = content.encode("utf-8")
        if prepared.get("new_content_digest") != _digest(
            b"agent-builder-file-content-v1", content_bytes
        ):
            raise FileWriteError("prepared mutation content changed")
        with self._target_lock(path):
            return self._commit(parts, prepared, content_bytes, cancelled)

    def _commit(
        self,
        parts: tuple[str, ...],
        prepared: dict[str, object],
        content: bytes,
        cancelled: object,
    ) -> str:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None:
            raise FileWriteError("descriptor-anchored mutations are unavailable")
        root_fd = os.open(
            self._workspace,
            os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
        )
        descriptors = [root_fd]
        parent_fd = root_fd
        target_fd: int | None = None
        temp_name = f".agent-builder-write-{secrets.token_hex(16)}.tmp"
        temp_created = False
        exchanged = False
        try:
            root = os.fstat(root_fd)
            if (
                root.st_dev,
                root.st_ino,
                root.st_mode,
                root.st_uid,
            ) != self._root_identity:
                raise FileWriteError("workspace identity changed")
            for component in parts[:-1]:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
                    dir_fd=parent_fd,
                )
                descriptors.append(child)
                metadata = os.fstat(child)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or metadata.st_dev != root.st_dev
                ):
                    raise FileWriteError("unsafe mutation directory")
                parent_fd = child
            receipt = prepared["receipt"]
            assert isinstance(receipt, dict)
            create = receipt.get("kind") == "target_absent"
            if create:
                parent_path = "/".join(parts[:-1])
                if receipt.get("parent_identity") != _directory_identity(
                    self._capsule, parent_path, os.fstat(parent_fd)
                ):
                    raise FileWriteError("create parent receipt is stale")
                try:
                    os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileWriteError("create target already exists")
            elif receipt.get("kind") == "existing":
                target_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_CLOEXEC | no_follow,
                    dir_fd=parent_fd,
                )
                metadata = os.fstat(target_fd)
                require_safe_file_metadata(metadata, root.st_dev)
                raw = self._read_exact(target_fd, metadata.st_size)
                current = capture_workspace_file(
                    self._capsule, "/".join(parts)
                )
                if (
                    current.identity_digest != receipt.get("path_identity")
                    or current.content_digest != receipt.get("content_digest")
                    or raw != current.content
                ):
                    raise FileWriteError("mutation receipt is stale")
            else:
                raise FileWriteError("invalid mutation receipt")

            temp_fd = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | no_follow,
                0o600,
                dir_fd=parent_fd,
            )
            temp_created = True
            try:
                view = memoryview(content)
                while view:
                    written = os.write(temp_fd, view)
                    if written <= 0:
                        raise FileWriteError("mutation temp write made no progress")
                    view = view[written:]
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
            if callable(cancelled) and cancelled():
                raise FileWriteError("file mutation was cancelled before commit")
            if create:
                _renameat2(parent_fd, temp_name, parent_fd, parts[-1], _RENAME_NOREPLACE)
                temp_created = False
            else:
                assert target_fd is not None
                named = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if _identity(named) != _identity(os.fstat(target_fd)):
                    raise FileWriteError("mutation target raced before commit")
                _renameat2(parent_fd, temp_name, parent_fd, parts[-1], _RENAME_EXCHANGE)
                exchanged = True
                displaced = os.open(
                    temp_name,
                    os.O_RDONLY | os.O_CLOEXEC | no_follow,
                    dir_fd=parent_fd,
                )
                try:
                    displaced_metadata = os.fstat(displaced)
                    displaced_raw = self._read_exact(
                        displaced, displaced_metadata.st_size
                    )
                finally:
                    os.close(displaced)
                if (
                    _identity(displaced_metadata) != _identity(os.fstat(target_fd))
                    or _digest(b"agent-builder-file-content-v1", displaced_raw)
                    != receipt.get("content_digest")
                ):
                    try:
                        _renameat2(
                            parent_fd,
                            temp_name,
                            parent_fd,
                            parts[-1],
                            _RENAME_EXCHANGE,
                        )
                        exchanged = False
                    except OSError as exc:
                        raise FileWriteOutcomeUnknownError(
                            "mutation race rollback is unprovable"
                        ) from exc
                    raise FileWriteError("mutation target raced at commit")
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_created = False
                exchanged = False
            os.fsync(parent_fd)
            captured = capture_workspace_file(self._capsule, "/".join(parts))
            if captured.content != content:
                raise FileWriteOutcomeUnknownError(
                    "committed mutation could not be verified"
                )
            return _canonical(
                {
                    "schema_version": 1,
                    "kind": "file_mutation",
                    "tool_id": prepared["tool_id"],
                    "receipt": file_receipt(captured),
                    "diff_digest": prepared["diff_digest"],
                    "outcome": "committed",
                }
            )
        except FileWriteOutcomeUnknownError:
            raise
        except OSError as exc:
            if exchanged:
                raise FileWriteOutcomeUnknownError(
                    "mutation commit outcome is unknown"
                ) from exc
            if exc.errno in {errno.EEXIST, errno.ENOENT, errno.ELOOP}:
                raise FileWriteError("mutation target changed") from exc
            raise FileWriteError("atomic file mutation failed") from exc
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if temp_created and not exchanged:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _read_exact(descriptor: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size + 1
        os.lseek(descriptor, 0, os.SEEK_SET)
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != size:
            raise FileWriteError("mutation target changed size")
        return raw


__all__ = [
    "FileMutationExecutor",
    "FileWriteError",
    "FileWriteOutcomeUnknownError",
    "FullReadReceipt",
    "MAX_DIFF_BYTES",
    "MAX_CLEANUP_ENTRIES",
    "MAX_EDIT_FRAGMENT_BYTES",
    "MAX_MUTATION_CONTENT_BYTES",
    "MAX_TEMP_FILES",
]

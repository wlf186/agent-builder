"""Contained, symlink-safe storage for project-local diagnostic summaries."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .storage_paths import (
    UnsafeStoragePathError,
    ensure_real_directory,
    validate_regular_file,
)


_LOG_FILE_LOCK = threading.RLock()
_CLIENT_LOG_NAME = re.compile(r"^client_log_[A-Za-z0-9_-]+\.json$")


class LocalLogStoreError(RuntimeError):
    """Raised when a diagnostic log path is unsafe or malformed."""


def _ensure_contained_directory(root: Path, directory: Path) -> Path:
    root = Path(root).absolute()
    directory = Path(directory).absolute()
    try:
        root = ensure_real_directory(root)
    except UnsafeStoragePathError as exc:
        raise LocalLogStoreError("log root is not a real directory") from exc
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise LocalLogStoreError("log directory escapes project data") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LocalLogStoreError("log directory cannot contain symlinks")
        if current.exists() and not current.is_dir():
            raise LocalLogStoreError("log directory component is not a directory")
        try:
            current = ensure_real_directory(current)
        except UnsafeStoragePathError as exc:
            raise LocalLogStoreError("log directory cannot contain symlinks") from exc

    resolved_root = root.resolve(strict=True)
    resolved_directory = directory.resolve(strict=True)
    try:
        resolved_directory.relative_to(resolved_root)
    except ValueError as exc:
        raise LocalLogStoreError("log directory escapes project data") from exc
    return directory


def _validate_regular_target(path: Path, directory: Path) -> None:
    path = Path(path).absolute()
    if path.parent != Path(directory).absolute():
        raise LocalLogStoreError("log target is outside its managed directory")
    if path.is_symlink():
        raise LocalLogStoreError("log target cannot be a symlink")
    if path.exists() and not path.is_file():
        raise LocalLogStoreError("log target is not a regular file")
    try:
        validate_regular_file(path, allow_missing=True)
    except UnsafeStoragePathError as exc:
        raise LocalLogStoreError("log target is not a private regular file") from exc


def _open_append_no_follow(path: Path):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a", encoding="utf-8")


def append_rotating_log(
    root: Path,
    path: Path,
    entry: str,
    max_bytes: int = 20 * 1024 * 1024,
    backups: int = 5,
) -> None:
    """Append one summary without following target or rotation symlinks."""
    if not isinstance(entry, str) or len(entry.encode("utf-8")) > 64 * 1024:
        raise LocalLogStoreError("log entry is too large")
    if max_bytes < 1 or not 1 <= backups <= 100:
        raise LocalLogStoreError("invalid rotation policy")

    with _LOG_FILE_LOCK:
        path = Path(path).absolute()
        directory = _ensure_contained_directory(root, path.parent)
        candidates = [path] + [
            path.with_name(f"{path.name}.{index}")
            for index in range(1, backups + 1)
        ]
        for candidate in candidates:
            _validate_regular_target(candidate, directory)

        encoded_size = len(entry.encode("utf-8"))
        if path.exists() and path.stat().st_size + encoded_size > max_bytes:
            candidates[-1].unlink(missing_ok=True)
            for index in range(backups - 1, 0, -1):
                source = path.with_name(f"{path.name}.{index}")
                if source.exists():
                    os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
            os.replace(path, path.with_name(f"{path.name}.1"))

        _validate_regular_target(path, directory)
        with _open_append_no_follow(path) as handle:
            handle.write(entry)


def _client_log_files(logs_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in logs_dir.iterdir():
        if not _CLIENT_LOG_NAME.fullmatch(path.name):
            continue
        if path.is_symlink() or not path.is_file():
            raise LocalLogStoreError("client log target must be a regular file")
        files.append(path)
    return files


def _prune_client_logs(
    logs_dir: Path,
    max_files: int = 100,
    max_bytes: int = 25 * 1024 * 1024,
) -> None:
    files = sorted(
        _client_log_files(logs_dir),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained_size = 0
    for index, path in enumerate(files):
        size = path.stat().st_size
        if index >= max_files or retained_size + size > max_bytes:
            path.unlink(missing_ok=True)
        else:
            retained_size += size


def write_client_log(
    root: Path,
    log_file: Path,
    log_data: Any,
    logs_dir: Path,
) -> None:
    """Atomically store one value-free client summary under ``root``."""
    encoded = json.dumps(
        log_data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=lambda item: f"<{type(item).__name__}>",
    ).encode("utf-8")
    if len(encoded) > 512 * 1024:
        raise LocalLogStoreError("client log summary is too large")

    with _LOG_FILE_LOCK:
        logs_dir = _ensure_contained_directory(root, logs_dir)
        log_file = Path(log_file).absolute()
        temporary = log_file.with_suffix(".tmp")
        _validate_regular_target(log_file, logs_dir)
        _validate_regular_target(temporary, logs_dir)
        if log_file.exists() or temporary.exists():
            raise LocalLogStoreError("client log target already exists")
        _client_log_files(logs_dir)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_regular_target(log_file, logs_dir)
            os.replace(temporary, log_file)
        finally:
            temporary.unlink(missing_ok=True)
        _prune_client_logs(logs_dir)

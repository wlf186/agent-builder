"""Bounded, index-free Glob/Grep over one descriptor-anchored workspace."""

from __future__ import annotations

from functools import lru_cache
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable

from .capsule import AgentCapsule
from .file_read import (
    FileReadError,
    capture_workspace_file,
    file_receipt,
    require_safe_file_metadata,
)
from .permissions import CapabilityRequest


MAX_SEARCH_DEPTH = 16
MAX_GLOB_COMPONENTS = 16
MAX_SEARCH_ENTRIES = 4_096
MAX_SEARCH_FILES = 1_024
MAX_SEARCH_BYTES = 2 * 1024 * 1024
MAX_SEARCH_MATCHES = 128
MAX_SEARCH_RESULT_BYTES = 12 * 1024
MAX_GLOB_BYTES = 256
MAX_REGEX_BYTES = 256
MAX_LINE_BYTES = 16 * 1024
MAX_EXCERPT_BYTES = 512
SEARCH_TIMEOUT_SECONDS = 1.0
_SEARCH_CAPABILITIES = frozenset({"file/glob", "file/grep"})


class FileSearchError(RuntimeError):
    """Search could not complete inside all declared safety bounds."""


class _SearchLimit(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


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


def _validate_glob(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        raise FileSearchError("invalid glob pattern")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FileSearchError("invalid glob pattern") from exc
    parts = Path(value).parts
    if (
        Path(value).is_absolute()
        or len(encoded) > MAX_GLOB_BYTES
        or not 1 <= len(parts) <= MAX_GLOB_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
        or "/".join(parts) != value
        or sum(part.count(item) for part in parts for item in "*?[") > 32
        or any("**" in part and part != "**" for part in parts)
    ):
        raise FileSearchError("invalid glob pattern")
    return parts


def _glob_match(path: str, pattern: tuple[str, ...]) -> bool:
    parts = tuple(path.split("/"))

    @lru_cache(maxsize=1024)
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern):
            return path_index == len(parts)
        component = pattern[pattern_index]
        if component == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], component)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def _safe_regex(value: object, case_sensitive: bool) -> re.Pattern[str]:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        raise FileSearchError("invalid grep expression")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FileSearchError("invalid grep expression") from exc
    if (
        len(encoded) > MAX_REGEX_BYTES
        or any(character in value for character in "(){}|*+")
        or value.count("?") > 8
        or re.search(r"\\[A-Za-z0-9]", value)
    ):
        raise FileSearchError("grep expression is outside the safe regex subset")
    try:
        return re.compile(value, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise FileSearchError("invalid grep expression") from exc


def _excerpt(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_EXCERPT_BYTES:
        return value
    end = MAX_EXCERPT_BYTES
    while end:
        try:
            return encoded[:end].decode("utf-8")
        except UnicodeDecodeError:
            end -= 1
    return ""


class _Walker:
    def __init__(self, capsule: AgentCapsule, cancelled: Callable[[], bool]) -> None:
        self.capsule = capsule
        self.workspace = capsule.data_root / "workspace"
        self.started = time.monotonic()
        self.entries = 0
        self.files = 0
        self.bytes_read = 0
        self.visited: set[tuple[int, int]] = set()
        self.root_device = 0
        self.root_identity: tuple[int, int, int, int] | None = None
        self.cancelled = cancelled

    def _check_time(self) -> None:
        if self.cancelled():
            raise FileSearchError("search capability was cancelled")
        if time.monotonic() - self.started > SEARCH_TIMEOUT_SECONDS:
            raise _SearchLimit("time_limit")

    @staticmethod
    def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)

    def walk(self, visit: Callable[[str, os.stat_result], None]) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None:
            raise FileSearchError("descriptor-anchored search is unavailable")
        try:
            named_root = os.lstat(self.workspace)
            root_fd = os.open(
                self.workspace,
                os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
            )
        except OSError as exc:
            raise FileSearchError("workspace is unavailable") from exc
        try:
            opened = os.fstat(root_fd)
            self.root_device = opened.st_dev
            self.root_identity = self._directory_identity(opened)
            self._require_directory(opened)
            if self.root_identity != self._directory_identity(named_root):
                raise FileSearchError("workspace changed during search")
            self._walk_directory(root_fd, (), 0, visit, no_follow, directory_flag)
            final = os.fstat(root_fd)
            current_named = os.lstat(self.workspace)
            if (
                self.root_identity != self._directory_identity(final)
                or self.root_identity != self._directory_identity(current_named)
            ):
                raise FileSearchError("workspace changed during search")
        except OSError as exc:
            raise FileSearchError("workspace search failed closed") from exc
        finally:
            os.close(root_fd)

    def _require_directory(self, metadata: os.stat_result) -> None:
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_dev != self.root_device
            or identity in self.visited
        ):
            raise FileSearchError("unsafe workspace directory")
        self.visited.add(identity)

    def _walk_directory(
        self,
        directory_fd: int,
        prefix: tuple[str, ...],
        depth: int,
        visit: Callable[[str, os.stat_result], None],
        no_follow: int,
        directory_flag: int,
    ) -> None:
        self._check_time()
        if depth > MAX_SEARCH_DEPTH:
            raise _SearchLimit("depth_limit")
        names: list[str] = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_SEARCH_ENTRIES:
                    raise _SearchLimit("entry_limit")
                try:
                    entry.name.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise FileSearchError("directory entry is not UTF-8") from exc
                names.append(entry.name)
        names.sort(key=lambda name: name.encode("utf-8", "surrogateescape"))
        for name in names:
            self._check_time()
            if name in {".", ".."} or "\x00" in name:
                raise FileSearchError("unsafe directory entry")
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise FileSearchError("directory entry changed during search") from exc
            relative_parts = (*prefix, name)
            relative = "/".join(relative_parts)
            if len(relative.encode("utf-8")) > 1_024:
                raise FileSearchError("workspace path exceeds its byte limit")
            if stat.S_ISDIR(metadata.st_mode):
                if name == ".git":
                    if (
                        metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) & 0o022
                        or metadata.st_dev != self.root_device
                    ):
                        raise FileSearchError("unsafe workspace directory")
                    continue
                if depth >= MAX_SEARCH_DEPTH:
                    raise _SearchLimit("depth_limit")
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise FileSearchError("workspace directory changed") from exc
                try:
                    opened = os.fstat(child_fd)
                    self._require_directory(opened)
                    if self._directory_identity(opened) != self._directory_identity(metadata):
                        raise FileSearchError("workspace directory changed")
                    self._walk_directory(
                        child_fd,
                        relative_parts,
                        depth + 1,
                        visit,
                        no_follow,
                        directory_flag,
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                try:
                    require_safe_file_metadata(metadata, self.root_device)
                except FileReadError as exc:
                    raise FileSearchError("unsafe workspace file") from exc
                self.files += 1
                if self.files > MAX_SEARCH_FILES:
                    raise _SearchLimit("file_limit")
                visit(relative, metadata)
            else:
                raise FileSearchError("special workspace entry is not searchable")


class FileSearchExecutor:
    executor_kind = "workspace-file-search-v1"

    def __init__(self, capsule: AgentCapsule) -> None:
        self._capsule = capsule
        root = os.lstat(capsule.data_root / "workspace")
        self.identity_digest = _digest(
            b"agent-builder-file-search-executor-v1",
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
        if request.context.tool_id not in _SEARCH_CAPABILITIES:
            raise FileSearchError("unsupported search capability")
        if callable(cancelled) and cancelled():
            raise FileSearchError("search capability was cancelled")
        try:
            arguments = json.loads(request.arguments_json)
        except json.JSONDecodeError as exc:
            raise FileSearchError("invalid search arguments") from exc
        if not isinstance(arguments, dict):
            raise FileSearchError("invalid search arguments")
        pattern = _validate_glob(arguments.get("pattern"))
        max_results = arguments.get("max_results", 64)
        if (
            not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or not 1 <= max_results <= MAX_SEARCH_MATCHES
        ):
            raise FileSearchError("invalid search result limit")
        cancel_check = cancelled if callable(cancelled) else lambda: False
        walker = _Walker(self._capsule, cancel_check)
        matches: list[dict[str, object]] = []
        truncation_reason = "none"

        def append_bounded(item: dict[str, object]) -> None:
            nonlocal truncation_reason
            if len(matches) >= max_results:
                raise _SearchLimit("match_limit")
            matches.append(item)
            probe = _canonical({"matches": matches})
            if len(probe.encode("utf-8")) > MAX_SEARCH_RESULT_BYTES - 512:
                matches.pop()
                truncation_reason = "result_bytes_limit"
                raise _SearchLimit(truncation_reason)

        if request.context.tool_id == "file/glob":
            allowed = {"pattern", "max_results"}
            if not set(arguments).issubset(allowed):
                raise FileSearchError("invalid glob arguments")

            def visit_glob(path: str, metadata: os.stat_result) -> None:
                if not _glob_match(path, pattern):
                    return
                if walker.bytes_read + metadata.st_size > MAX_SEARCH_BYTES:
                    raise _SearchLimit("byte_limit")
                captured = capture_workspace_file(self._capsule, path)
                walker.bytes_read += len(captured.content)
                append_bounded({"receipt": file_receipt(captured)})

            visitor = visit_glob
        else:
            allowed = {
                "pattern", "query", "regex", "case_sensitive", "max_results"
            }
            if not set(arguments).issubset(allowed):
                raise FileSearchError("invalid grep arguments")
            query = arguments.get("query")
            regex_mode = arguments.get("regex", False)
            case_sensitive = arguments.get("case_sensitive", True)
            if (
                not isinstance(query, str)
                or not query
                or "\x00" in query
                or "\n" in query
                or len(query.encode("utf-8")) > MAX_REGEX_BYTES
                or not isinstance(regex_mode, bool)
                or not isinstance(case_sensitive, bool)
            ):
                raise FileSearchError("invalid grep arguments")
            expression = _safe_regex(query, case_sensitive) if regex_mode else None
            literal = query if case_sensitive else query.casefold()

            def visit_grep(path: str, metadata: os.stat_result) -> None:
                if not _glob_match(path, pattern):
                    return
                if walker.bytes_read + metadata.st_size > MAX_SEARCH_BYTES:
                    raise _SearchLimit("byte_limit")
                captured = capture_workspace_file(self._capsule, path)
                walker.bytes_read += len(captured.content)
                content = captured.content.decode("utf-8")
                receipt = file_receipt(captured)
                for line_number, line in enumerate(content.splitlines(), 1):
                    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                        raise FileSearchError("workspace line exceeds grep limit")
                    if expression is not None:
                        found = expression.search(line)
                        column = -1 if found is None else found.start()
                    else:
                        column = (
                            line.find(literal)
                            if case_sensitive
                            else line.casefold().find(literal)
                        )
                    if column < 0:
                        continue
                    append_bounded(
                        {
                            "path": path,
                            "line": line_number,
                            "column": column + 1,
                            "excerpt": _excerpt(line),
                            "path_identity": receipt["path_identity"],
                            "content_digest": receipt["content_digest"],
                        }
                    )

            visitor = visit_grep
        try:
            walker.walk(visitor)
        except _SearchLimit as exc:
            truncation_reason = exc.reason
        except FileReadError as exc:
            raise FileSearchError("workspace file capture failed closed") from exc
        if callable(cancelled) and cancelled():
            raise FileSearchError("search capability was cancelled")
        result = _canonical(
            {
                "schema_version": 1,
                "kind": "file_glob" if request.context.tool_id == "file/glob" else "file_grep",
                "provenance": (
                    f"capsule:{self._capsule.agent_id}:generation:"
                    f"{self._capsule.generation}:workspace"
                ),
                "matches": matches,
                "scanned": {
                    "entries": walker.entries,
                    "files": walker.files,
                    "bytes": walker.bytes_read,
                },
                "truncated": truncation_reason != "none",
                "truncation_reason": truncation_reason,
            }
        )
        if len(result.encode("utf-8")) > MAX_SEARCH_RESULT_BYTES:
            raise FileSearchError("search result exceeded its byte limit")
        return result


__all__ = [
    "FileSearchError",
    "FileSearchExecutor",
    "MAX_SEARCH_BYTES",
    "MAX_SEARCH_DEPTH",
    "MAX_SEARCH_ENTRIES",
    "MAX_SEARCH_FILES",
    "MAX_SEARCH_MATCHES",
    "MAX_SEARCH_RESULT_BYTES",
    "SEARCH_TIMEOUT_SECONDS",
]

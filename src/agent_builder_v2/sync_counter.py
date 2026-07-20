"""Qualification-only libc sync-call counter contract and artifact checks.

The production runtime never enables this module implicitly.  A lifecycle
qualification start prepares one fixed shared object and one fixed counter
page under ``.runtime/`` and then propagates only the validated, fixed paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mmap
import os
from pathlib import Path
import stat
import struct
from typing import Mapping


SYNC_COUNTER_RELATIVE = Path(".runtime/qualification-sync")
SYNC_COUNTER_LIBRARY_NAME = "libagent_builder_sync_counter.so"
SYNC_COUNTER_FILE_NAME = "sync-counter.bin"
SYNC_COUNTER_BUILD_NAME = "build.json"

SYNC_COUNTER_ENABLE_ENV = "_AGENT_BUILDER_QUALIFICATION_SYNC_COUNTER"
SYNC_COUNTER_FILE_ENV = "_AGENT_BUILDER_SYNC_COUNTER_FILE"
SYNC_COUNTER_ROLE_ENV = "_AGENT_BUILDER_SYNC_COUNTER_ROLE"
SYNC_COUNTER_REQUIRED_ENV = "_AGENT_BUILDER_SYNC_COUNTER_REQUIRED"
SYNC_COUNTER_SELFTEST_ENV = "_AGENT_BUILDER_SYNC_COUNTER_SELFTEST_NO_GLOBAL_SYNC"

COUNTER_MAGIC = b"ABSYNC1\0"
COUNTER_VERSION = 1
COUNTER_SIZE = 4096
COUNTER_HEADER_SIZE = 256
COUNTER_SLOT_SIZE = 192
COUNTER_SLOT_COUNT = 20
COUNTER_STATE_READY = 2
COUNTER_ABI = "libc-sync-calls-v1"

SYNC_OPERATIONS = (
    "fsync",
    "fdatasync",
    "msync",
    "syncfs",
    "sync_file_range",
    "sync",
)
SYNC_OUTCOMES = ("attempts", "successes", "failures")
SYNC_ROLES = {
    1: "supervisor",
    2: "gateway",
    3: "worker",
    4: "selftest",
}

_HEADER = struct.Struct("<8sIIIIIIQQQ")
_SLOT_PREFIX = struct.Struct("<IIiIQQ")
_COUNTERS = struct.Struct("<" + "Q" * (len(SYNC_OPERATIONS) * len(SYNC_OUTCOMES)))
_BUILD_RECORD_LIMIT = 4096


class SyncCounterError(RuntimeError):
    """A qualification counter artifact or environment is unsafe."""


@dataclass(frozen=True, slots=True)
class SyncCounterPaths:
    directory: Path
    library: Path
    counter: Path
    build_record: Path


def sync_counter_paths(repository_root: Path) -> SyncCounterPaths:
    root = repository_root.resolve(strict=True)
    directory = root / SYNC_COUNTER_RELATIVE
    return SyncCounterPaths(
        directory=directory,
        library=directory / SYNC_COUNTER_LIBRARY_NAME,
        counter=directory / SYNC_COUNTER_FILE_NAME,
        build_record=directory / SYNC_COUNTER_BUILD_NAME,
    )


def _reject_symlink_components(repository_root: Path, candidate: Path) -> None:
    root = repository_root.resolve(strict=True)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise SyncCounterError("sync counter path is outside the checkout") from exc
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise SyncCounterError("sync counter path contains a symlink")


def _validate_directory(repository_root: Path, path: Path) -> None:
    _reject_symlink_components(repository_root, path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SyncCounterError("sync counter directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SyncCounterError("sync counter directory is unsafe")


def _validate_regular(path: Path, *, mode: int, size: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SyncCounterError("sync counter artifact is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or (size is not None and metadata.st_size != size)
    ):
        raise SyncCounterError("sync counter artifact is unsafe")
    return metadata


def _read_nofollow(path: Path, *, maximum_bytes: int) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SyncCounterError("O_NOFOLLOW is required for sync counter evidence")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | no_follow)
    except OSError as exc:
        raise SyncCounterError("sync counter artifact could not be opened") from exc
    try:
        opened = os.fstat(descriptor)
        named = _validate_regular(path, mode=stat.S_IMODE(opened.st_mode))
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise SyncCounterError("sync counter artifact changed while opening")
        payload = os.read(descriptor, maximum_bytes + 1)
        if len(payload) > maximum_bytes or os.read(descriptor, 1):
            raise SyncCounterError("sync counter artifact exceeds its size limit")
        return payload
    finally:
        os.close(descriptor)


def _sha256_regular(path: Path, *, mode: int, maximum_bytes: int) -> str:
    metadata = _validate_regular(path, mode=mode)
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise SyncCounterError("sync counter artifact has an invalid size")
    return hashlib.sha256(_read_nofollow(path, maximum_bytes=maximum_bytes)).hexdigest()


def validate_sync_counter_artifacts(repository_root: Path) -> SyncCounterPaths:
    """Validate fixed qualification artifacts and their recorded digest."""

    root = repository_root.resolve(strict=True)
    paths = sync_counter_paths(root)
    _validate_directory(root, paths.directory)
    _reject_symlink_components(root, paths.library)
    _reject_symlink_components(root, paths.counter)
    _reject_symlink_components(root, paths.build_record)
    library_digest = _sha256_regular(paths.library, mode=0o500, maximum_bytes=8 * 1024 * 1024)
    _validate_regular(paths.counter, mode=0o600, size=COUNTER_SIZE)
    _validate_regular(paths.build_record, mode=0o600)
    try:
        record = json.loads(
            _read_nofollow(paths.build_record, maximum_bytes=_BUILD_RECORD_LIMIT)
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncCounterError("sync counter build record is invalid") from exc
    expected_keys = {
        "schema",
        "counter_abi",
        "source_sha256",
        "library_sha256",
        "compiler",
        "compiler_version",
        "compile_flags",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise SyncCounterError("sync counter build record has an invalid schema")
    if (
        record.get("schema") != 1
        or record.get("counter_abi") != COUNTER_ABI
        or record.get("library_sha256") != library_digest
        or not isinstance(record.get("source_sha256"), str)
        or len(record["source_sha256"]) != 64
        or not isinstance(record.get("compiler"), str)
        or not isinstance(record.get("compiler_version"), str)
        or not isinstance(record.get("compile_flags"), list)
        or any(not isinstance(item, str) for item in record["compile_flags"])
    ):
        raise SyncCounterError("sync counter build record does not match the library")
    return paths


def qualification_environment(
    repository_root: Path,
    source: Mapping[str, str],
    *,
    expected_role: str,
    child_role: str | None = None,
) -> dict[str, str]:
    """Return a fixed validated preload environment, or an empty mapping.

    The enable marker is set only by the explicit lifecycle flag.  If it is
    present, every value must exactly match the checkout-owned fixed paths.
    """

    enabled = source.get(SYNC_COUNTER_ENABLE_ENV)
    internal_names = {
        SYNC_COUNTER_ENABLE_ENV,
        SYNC_COUNTER_FILE_ENV,
        SYNC_COUNTER_ROLE_ENV,
        SYNC_COUNTER_REQUIRED_ENV,
        SYNC_COUNTER_SELFTEST_ENV,
    }
    if enabled is None:
        if any(name in source for name in internal_names - {SYNC_COUNTER_ENABLE_ENV}):
            raise SyncCounterError("partial sync counter environment")
        return {}
    if enabled != "1" or SYNC_COUNTER_SELFTEST_ENV in source:
        raise SyncCounterError("invalid sync counter enablement")
    paths = validate_sync_counter_artifacts(repository_root)
    expected = {
        "LD_PRELOAD": str(paths.library),
        SYNC_COUNTER_ENABLE_ENV: "1",
        SYNC_COUNTER_FILE_ENV: str(paths.counter),
        SYNC_COUNTER_ROLE_ENV: expected_role,
        SYNC_COUNTER_REQUIRED_ENV: "1",
    }
    if any(source.get(name) != value for name, value in expected.items()):
        raise SyncCounterError("sync counter environment does not match fixed artifacts")
    if "LD_LIBRARY_PATH" in source:
        raise SyncCounterError("LD_LIBRARY_PATH is forbidden in sync counter mode")
    if child_role is not None:
        if child_role not in {"gateway", "worker"}:
            raise SyncCounterError("invalid sync counter child role")
        expected[SYNC_COUNTER_ROLE_ENV] = child_role
    return expected


def _empty_operation_counts() -> dict[str, dict[str, int]]:
    return {
        operation: {outcome: 0 for outcome in SYNC_OUTCOMES}
        for operation in SYNC_OPERATIONS
    }


def _decode_counter(payload: bytes) -> dict[str, object]:
    if len(payload) != COUNTER_SIZE:
        raise SyncCounterError("sync counter page has the wrong size")
    (
        magic,
        version,
        file_size,
        header_size,
        slot_size,
        slot_count,
        operation_count,
        registration_failures,
        slot_overflow,
        generation,
    ) = _HEADER.unpack_from(payload)
    if (
        magic != COUNTER_MAGIC
        or version != COUNTER_VERSION
        or file_size != COUNTER_SIZE
        or header_size != COUNTER_HEADER_SIZE
        or slot_size != COUNTER_SLOT_SIZE
        or slot_count != COUNTER_SLOT_COUNT
        or operation_count != len(SYNC_OPERATIONS)
        or any(payload[_HEADER.size:COUNTER_HEADER_SIZE])
    ):
        raise SyncCounterError("sync counter page has an invalid header")

    roles: dict[str, dict[str, object]] = {}
    total = _empty_operation_counts()
    ready_slots = 0
    for index in range(COUNTER_SLOT_COUNT):
        offset = COUNTER_HEADER_SIZE + index * COUNTER_SLOT_SIZE
        state, role_number, pid, reserved, instance_ns, start_ticks = _SLOT_PREFIX.unpack_from(
            payload, offset
        )
        if state == 0:
            if any(payload[offset : offset + COUNTER_SLOT_SIZE]):
                raise SyncCounterError("unused sync counter slot is not empty")
            continue
        if state != COUNTER_STATE_READY:
            raise SyncCounterError("sync counter slot registration is incomplete")
        role = SYNC_ROLES.get(role_number)
        if (
            role is None
            or pid <= 1
            or reserved != 0
            or instance_ns == 0
            or start_ticks == 0
            or any(payload[offset + 176 : offset + COUNTER_SLOT_SIZE])
        ):
            raise SyncCounterError("sync counter slot identity is invalid")
        values = _COUNTERS.unpack_from(payload, offset + _SLOT_PREFIX.size)
        role_entry = roles.setdefault(
            role,
            {"process_images": 0, "operations": _empty_operation_counts()},
        )
        role_entry["process_images"] = int(role_entry["process_images"]) + 1
        operations = role_entry["operations"]
        assert isinstance(operations, dict)
        for operation_index, operation in enumerate(SYNC_OPERATIONS):
            attempts, successes, failures = values[
                operation_index * 3 : operation_index * 3 + 3
            ]
            if attempts != successes + failures:
                raise SyncCounterError("sync counter outcome totals are inconsistent")
            operation_counts = operations[operation]
            for outcome, value in zip(SYNC_OUTCOMES, (attempts, successes, failures), strict=True):
                operation_counts[outcome] += value
                total[operation][outcome] += value
        ready_slots += 1
    return {
        "schema": 1,
        "counter_abi": COUNTER_ABI,
        "generation": generation,
        "ready_slots": ready_slots,
        "registration_failures": registration_failures,
        "slot_overflow": slot_overflow,
        "complete": registration_failures == 0 and slot_overflow == 0,
        "roles": roles,
        "total": total,
    }


def read_sync_counter(repository_root: Path) -> dict[str, object]:
    """Read a stable snapshot without flushing or modifying the mmap page."""

    paths = validate_sync_counter_artifacts(repository_root)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SyncCounterError("O_NOFOLLOW is required for sync counter evidence")
    descriptor = os.open(paths.counter, os.O_RDONLY | os.O_CLOEXEC | no_follow)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size != COUNTER_SIZE:
            raise SyncCounterError("sync counter page changed while opening")
        page = mmap.mmap(descriptor, COUNTER_SIZE, access=mmap.ACCESS_READ)
        try:
            last_error: SyncCounterError | None = None
            for _attempt in range(20):
                first = page[:]
                second = page[:]
                if first == second:
                    try:
                        return _decode_counter(first)
                    except SyncCounterError as exc:
                        # A writer increments attempts immediately before its
                        # outcome.  Two fast copies can see that valid transient.
                        last_error = exc
                        os.sched_yield()
            if last_error is not None:
                raise SyncCounterError(
                    "sync counter page did not reach a consistent snapshot"
                ) from last_error
            raise SyncCounterError("sync counter page did not reach a stable snapshot")
        finally:
            page.close()
    finally:
        os.close(descriptor)


def sync_counter_delta(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, object]:
    """Return a bounded aggregate delta while rejecting reset or regression."""

    if (
        before.get("schema") != 1
        or after.get("schema") != 1
        or before.get("counter_abi") != COUNTER_ABI
        or after.get("counter_abi") != COUNTER_ABI
        or before.get("generation") != after.get("generation")
        or before.get("complete") is not True
        or after.get("complete") is not True
    ):
        raise SyncCounterError("sync counter changed or became incomplete")
    before_total = before.get("total")
    after_total = after.get("total")
    before_roles = before.get("roles")
    after_roles = after.get("roles")
    if not all(
        isinstance(value, dict)
        for value in (before_total, after_total, before_roles, after_roles)
    ):
        raise SyncCounterError("sync counter snapshot schema is invalid")
    delta = _empty_operation_counts()
    for operation in SYNC_OPERATIONS:
        for outcome in SYNC_OUTCOMES:
            try:
                initial = before_total[operation][outcome]  # type: ignore[index]
                final = after_total[operation][outcome]  # type: ignore[index]
            except (KeyError, TypeError) as exc:
                raise SyncCounterError("sync counter snapshot schema is invalid") from exc
            if (
                not isinstance(initial, int)
                or isinstance(initial, bool)
                or not isinstance(final, int)
                or isinstance(final, bool)
                or initial < 0
                or final < initial
            ):
                raise SyncCounterError("sync counter values regressed")
            delta[operation][outcome] = final - initial

    def process_images(value: Mapping[str, object]) -> dict[str, int]:
        result: dict[str, int] = {}
        for role, entry in value.items():
            if role not in SYNC_ROLES.values() or not isinstance(entry, dict):
                raise SyncCounterError("sync counter role summary is invalid")
            count = entry.get("process_images")
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise SyncCounterError("sync counter role count is invalid")
            result[role] = count
        return result

    initial_images = process_images(before_roles)
    final_images = process_images(after_roles)
    if any(final_images.get(role, 0) < count for role, count in initial_images.items()):
        raise SyncCounterError("sync counter process coverage regressed")
    return {
        "counter_abi": COUNTER_ABI,
        "scope": "preloaded-libc-symbol-calls",
        "before_process_images": initial_images,
        "after_process_images": final_images,
        "operations": delta,
    }

#!/usr/bin/env python3
"""Build, self-test, reset, and report the qualification sync counter."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = ROOT / "src"
try:
    sys.path.remove(str(SOURCE_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(SOURCE_ROOT))

from agent_builder_v2.sync_counter import (  # noqa: E402
    COUNTER_ABI,
    COUNTER_HEADER_SIZE,
    COUNTER_MAGIC,
    COUNTER_SIZE,
    COUNTER_SLOT_COUNT,
    COUNTER_SLOT_SIZE,
    COUNTER_VERSION,
    SYNC_COUNTER_ENABLE_ENV,
    SYNC_COUNTER_FILE_ENV,
    SYNC_COUNTER_FILE_NAME,
    SYNC_COUNTER_LIBRARY_NAME,
    SYNC_COUNTER_REQUIRED_ENV,
    SYNC_COUNTER_ROLE_ENV,
    SYNC_COUNTER_SELFTEST_ENV,
    SyncCounterError,
    read_sync_counter,
    sync_counter_paths,
    validate_sync_counter_artifacts,
)


C_SOURCE = ROOT / "scripts" / "sync_counter.c"
COMPILE_FLAGS = (
    "-std=c11",
    "-shared",
    "-fPIC",
    "-O2",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wl,-z,relro,-z,now,-z,noexecstack",
    "-pthread",
    "-ldl",
)


def _safe_existing(path: Path, *, mode: int | None = None) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise SyncCounterError("unsafe existing sync counter artifact")


def _ensure_private_directory(path: Path) -> None:
    current = path.anchor and Path(path.anchor) or Path()
    for component in path.parts[1:] if path.is_absolute() else path.parts:
        current /= component
        if current.is_symlink():
            raise SyncCounterError("sync counter directory contains a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise SyncCounterError("sync counter directory is unsafe")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise SyncCounterError("could not write sync counter artifact")
        view = view[written:]


def _publish_file(path: Path, payload: bytes, *, mode: int) -> None:
    _safe_existing(path)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _initial_counter_page() -> bytes:
    page = bytearray(COUNTER_SIZE)
    struct.pack_into(
        "<8sIIIIIIQQQ",
        page,
        0,
        COUNTER_MAGIC,
        COUNTER_VERSION,
        COUNTER_SIZE,
        COUNTER_HEADER_SIZE,
        COUNTER_SLOT_SIZE,
        COUNTER_SLOT_COUNT,
        6,
        0,
        0,
        time.monotonic_ns(),
    )
    return bytes(page)


def _managed_gateway_may_be_live(repository_root: Path) -> bool:
    pid_file = repository_root / ".runtime" / "control-plane" / "gateway.pid"
    if not os.path.lexists(pid_file):
        return False
    metadata = pid_file.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size > 4096
    ):
        raise SyncCounterError("unsafe gateway PID record blocks counter reset")
    descriptor = os.open(
        pid_file,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        named = pid_file.lstat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise SyncCounterError("gateway PID record changed while opening")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096 or os.read(descriptor, 1):
            raise SyncCounterError("gateway PID record is too large")
        payload = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncCounterError("gateway PID record is not UTF-8") from exc
    finally:
        os.close(descriptor)
    values = dict(line.split("=", 1) for line in payload.splitlines() if "=" in line)
    raw_pid = values.get("pid", "")
    if not raw_pid.isdigit() or int(raw_pid) <= 1:
        raise SyncCounterError("invalid gateway PID record blocks counter reset")
    try:
        os.kill(int(raw_pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _compiler() -> Path:
    value = shutil.which("gcc")
    if value is None:
        raise SyncCounterError("gcc is required for sync counter qualification")
    path = Path(value).resolve(strict=True)
    if not path.is_file():
        raise SyncCounterError("gcc is not a regular file")
    return path


def _compiler_version(compiler: Path) -> str:
    result = subprocess.run(
        [str(compiler), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
        check=False,
        timeout=10,
    )
    first = result.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    if result.returncode != 0 or not first or len(first[0]) > 256:
        raise SyncCounterError("could not identify gcc")
    return first[0]


def _build_library(repository_root: Path, source: Path) -> None:
    paths = sync_counter_paths(repository_root)
    source_metadata = source.lstat()
    if (
        not stat.S_ISREG(source_metadata.st_mode)
        or source_metadata.st_uid != os.geteuid()
        or source_metadata.st_nlink != 1
        or source_metadata.st_size <= 0
        or source_metadata.st_size > 512 * 1024
    ):
        raise SyncCounterError("sync counter C source is unsafe")
    source_payload = source.read_bytes()
    source_digest = hashlib.sha256(source_payload).hexdigest()
    compiler = _compiler()
    temporary = paths.directory / f".{paths.library.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(repository_root / ".runtime" / "home"),
        "TMPDIR": str(repository_root / ".runtime" / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    _ensure_private_directory(Path(environment["HOME"]))
    _ensure_private_directory(Path(environment["TMPDIR"]))
    try:
        result = subprocess.run(
            [str(compiler), *COMPILE_FLAGS[:-2], "-o", str(temporary), str(source), *COMPILE_FLAGS[-2:]],
            cwd=repository_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise SyncCounterError("sync counter library compilation failed")
        metadata = temporary.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > 8 * 1024 * 1024
            or temporary.read_bytes()[:4] != b"\x7fELF"
        ):
            raise SyncCounterError("compiled sync counter library is invalid")
        temporary.chmod(0o500)
        _safe_existing(paths.library)
        os.replace(temporary, paths.library)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    library_digest = hashlib.sha256(paths.library.read_bytes()).hexdigest()
    record = {
        "schema": 1,
        "counter_abi": COUNTER_ABI,
        "source_sha256": source_digest,
        "library_sha256": library_digest,
        "compiler": str(compiler),
        "compiler_version": _compiler_version(compiler),
        "compile_flags": list(COMPILE_FLAGS),
    }
    _publish_file(
        paths.build_record,
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        mode=0o600,
    )


def _reset_counter(repository_root: Path) -> None:
    paths = sync_counter_paths(repository_root)
    _publish_file(paths.counter, _initial_counter_page(), mode=0o600)
    directory = os.open(paths.directory, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _selftest_environment(repository_root: Path) -> dict[str, str]:
    paths = validate_sync_counter_artifacts(repository_root)
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(repository_root / ".runtime" / "home"),
        "TMPDIR": str(repository_root / ".runtime" / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(SOURCE_ROOT),
        "LD_PRELOAD": str(paths.library),
        SYNC_COUNTER_ENABLE_ENV: "1",
        SYNC_COUNTER_FILE_ENV: str(paths.counter),
        SYNC_COUNTER_ROLE_ENV: "selftest",
        SYNC_COUNTER_REQUIRED_ENV: "1",
        SYNC_COUNTER_SELFTEST_ENV: "1",
    }


def _run_selftest(repository_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "_exercise"],
        cwd=repository_root,
        env=_selftest_environment(repository_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise SyncCounterError("sync counter preload self-test failed")
    snapshot = read_sync_counter(repository_root)
    roles = snapshot["roles"]
    if set(roles) != {"selftest"} or snapshot["ready_slots"] != 1 or not snapshot["complete"]:
        raise SyncCounterError("sync counter self-test registration is invalid")
    operations = roles["selftest"]["operations"]
    for operation, counts in operations.items():
        expected = {"attempts": 1, "successes": 1, "failures": 0}
        if operation != "sync":
            expected = {"attempts": 2, "successes": 1, "failures": 1}
        if counts != expected:
            raise SyncCounterError("sync counter self-test counts are invalid")


def prepare_sync_counter(repository_root: Path, *, source: Path = C_SOURCE) -> None:
    root = repository_root.resolve(strict=True)
    if sys.platform != "linux" or sys.byteorder != "little":
        raise SyncCounterError("sync counter qualification requires little-endian Linux")
    if _managed_gateway_may_be_live(root):
        raise SyncCounterError("refusing to reset sync counter while Gateway may be live")
    paths = sync_counter_paths(root)
    _ensure_private_directory(root / ".runtime")
    _ensure_private_directory(paths.directory)
    for path in (paths.library, paths.counter, paths.build_record):
        _safe_existing(path)
    _build_library(root, source)
    _reset_counter(root)
    validate_sync_counter_artifacts(root)
    _run_selftest(root)
    _reset_counter(root)
    validate_sync_counter_artifacts(root)
    empty = read_sync_counter(root)
    if empty["ready_slots"] != 0 or not empty["complete"]:
        raise SyncCounterError("sync counter did not reset after self-test")


def _exercise() -> int:
    counter_text = os.environ.get(SYNC_COUNTER_FILE_ENV, "")
    counter_path = Path(counter_text) if counter_text else Path()
    if (
        os.environ.get(SYNC_COUNTER_ENABLE_ENV) != "1"
        or os.environ.get(SYNC_COUNTER_REQUIRED_ENV) != "1"
        or os.environ.get(SYNC_COUNTER_ROLE_ENV) != "selftest"
        or os.environ.get(SYNC_COUNTER_SELFTEST_ENV) != "1"
        or not counter_path.is_absolute()
        or counter_path.name != SYNC_COUNTER_FILE_NAME
        or os.environ.get("LD_PRELOAD")
        != str(counter_path.parent / SYNC_COUNTER_LIBRARY_NAME)
    ):
        return 2
    libc = ctypes.CDLL(None, use_errno=True)
    memfd_create = libc.memfd_create
    memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    memfd_create.restype = ctypes.c_int
    descriptor = memfd_create(b"agent-builder-sync-counter-selftest", 1)
    if descriptor < 0:
        return 2
    try:
        os.ftruncate(descriptor, 4096)
        mapping = mmap.mmap(descriptor, 4096)
        try:
            buffer = (ctypes.c_char * 4096).from_buffer(mapping)
            address = ctypes.addressof(buffer)
            calls = (
                ("fsync", (descriptor,), (ctypes.c_int,), ctypes.c_int),
                ("fdatasync", (descriptor,), (ctypes.c_int,), ctypes.c_int),
                ("msync", (address, 4096, 4), (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int), ctypes.c_int),
                ("syncfs", (descriptor,), (ctypes.c_int,), ctypes.c_int),
                (
                    "sync_file_range",
                    (descriptor, 0, 0, 0),
                    (ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_uint),
                    ctypes.c_int,
                ),
            )
            for name, arguments, argument_types, result_type in calls:
                function = getattr(libc, name)
                function.argtypes = list(argument_types)
                function.restype = result_type
                ctypes.set_errno(0)
                if function(*arguments) != 0:
                    return 3
            del buffer
        finally:
            mapping.close()
    finally:
        os.close(descriptor)

    failures = (
        ("fsync", (-1,), (ctypes.c_int,)),
        ("fdatasync", (-1,), (ctypes.c_int,)),
        ("msync", (1, 4096, 4), (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)),
        ("syncfs", (-1,), (ctypes.c_int,)),
        (
            "sync_file_range",
            (-1, 0, 0, 0),
            (ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_uint),
        ),
    )
    for name, arguments, argument_types in failures:
        function = getattr(libc, name)
        function.argtypes = list(argument_types)
        function.restype = ctypes.c_int
        ctypes.set_errno(0)
        if function(*arguments) != -1 or ctypes.get_errno() == 0:
            return 4
    sync = libc.sync
    sync.argtypes = []
    sync.restype = None
    sync()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "report", "_exercise"))
    args = parser.parse_args()
    if args.command == "_exercise":
        return _exercise()
    try:
        if args.command == "prepare":
            prepare_sync_counter(ROOT)
            return 0
        snapshot = read_sync_counter(ROOT)
        print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
        return 0 if snapshot["complete"] else 1
    except (OSError, SyncCounterError, subprocess.SubprocessError):
        print("agent-builder sync counter: qualification counter failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

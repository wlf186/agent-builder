#!/usr/bin/env python3
"""Run the Agent Builder gateway with bounded, checkout-local combined logs."""

from __future__ import annotations

import argparse
import codecs
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Sequence

_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src"
try:
    sys.path.remove(str(_SOURCE_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(_SOURCE_ROOT))

from agent_builder_v2.sync_counter import (
    COUNTER_ABI,
    SyncCounterError,
    qualification_environment,
)


_CAPTURE_FLUSH_INTERVAL_SECONDS = 0.75


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--clean-env", action="store_true")
    parser.add_argument("--qualification-sync-counter", action="store_true")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--backups", type=int, default=3)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if not 1_024 <= args.max_bytes <= 64 * 1024 * 1024:
        parser.error("--max-bytes must be between 1024 and 67108864")
    if not 1 <= args.backups <= 10:
        parser.error("--backups must be between 1 and 10")
    return args


def _sanitised_environment(source: dict[str, str]) -> dict[str, str]:
    """Keep only non-secret settings needed by the Agent Builder gateway."""

    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "AGENT_BUILDER_ROOT",
        "AGENT_BUILDER_RUNTIME_DIR",
        "HARNESS_V2_HOST",
        "HARNESS_V2_PORT",
        "HARNESS_V2_CONTEXT_REVEAL",
    }
    return {key: value for key, value in source.items() if key in allowed}


def _reject_symlinks(checkout: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(checkout)
    except ValueError as exc:
        raise ValueError("managed path is outside the checkout") from exc
    current = checkout
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"managed path contains a symlink: {current}")


def _managed_log_path(runtime_argument: str, log_argument: str) -> Path:
    try:
        checkout = Path(os.environ["AGENT_BUILDER_ROOT"]).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise ValueError("AGENT_BUILDER_ROOT is missing or invalid") from exc

    runtime = Path(os.path.abspath(runtime_argument))
    expected_runtime = checkout / ".runtime" / "control-plane"
    if runtime != expected_runtime:
        raise ValueError("runtime root must be .runtime/control-plane")
    _reject_symlinks(checkout, runtime)
    if not runtime.is_dir():
        raise ValueError("control-plane runtime root does not exist")

    log_file = Path(os.path.abspath(log_argument))
    if log_file.parent != runtime or log_file.name != "gateway.log":
        raise ValueError("gateway log must be directly inside the control-plane runtime root")
    _reject_symlinks(checkout, log_file)
    if log_file.exists():
        metadata = log_file.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("gateway log is not a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("gateway log must not be hard-linked")
    return log_file


def _managed_pid_path(runtime_argument: str, pid_argument: str) -> tuple[Path, Path]:
    try:
        checkout = Path(os.environ["AGENT_BUILDER_ROOT"]).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise ValueError("AGENT_BUILDER_ROOT is missing or invalid") from exc
    runtime = Path(os.path.abspath(runtime_argument))
    expected_runtime = checkout / ".runtime" / "control-plane"
    if runtime != expected_runtime:
        raise ValueError("runtime root must be .runtime/control-plane")
    pid_file = Path(os.path.abspath(pid_argument))
    if pid_file.parent != runtime or pid_file.name != "gateway.pid":
        raise ValueError("gateway PID file must be directly inside the runtime root")
    _reject_symlinks(checkout, pid_file)
    if os.path.lexists(pid_file):
        raise ValueError("gateway PID file already exists")
    return checkout, pid_file


def _process_marker(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("process stat has no command terminator")
    fields = raw[closing + 1 :].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("process stat has no valid starttime")
    return f"linux:{fields[19]}"


def _process_parent_and_group(pid: int) -> tuple[int, int]:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split() if closing >= 0 else []
    if len(fields) < 3 or not fields[1].isdigit() or not fields[2].isdigit():
        raise ValueError("process stat has no valid parent/group identity")
    return int(fields[1]), int(fields[2])


def _process_argv(pid: int) -> tuple[str, ...]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    if not raw or len(raw) > 4096 or not raw.endswith(b"\0"):
        raise ValueError("process command line is invalid")
    try:
        values = tuple(part.decode() for part in raw[:-1].split(b"\0"))
    except UnicodeDecodeError as exc:
        raise ValueError("process command line is not UTF-8") from exc
    if not values or len(values) > 32 or any(not value for value in values):
        raise ValueError("process command line is invalid")
    return values


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write gateway PID record")
        view = view[written:]


def _publish_gateway_pid_record(
    path: Path,
    checkout: Path,
    *,
    qualification_sync_counter: bool = False,
) -> None:
    pid = os.getpid()
    pgid = os.getpgrp()
    if pgid != pid:
        raise RuntimeError("gateway supervisor is not its process-group leader")
    payload = (
        "schema=1\n"
        "role=gateway\n"
        f"pid={pid}\n"
        f"pgid={pgid}\n"
        f"marker={_process_marker(pid)}\n"
        f"root={checkout}\n"
        + (f"sync_counter={COUNTER_ABI}\n" if qualification_sync_counter else "")
    ).encode("utf-8")
    temporary = path.parent / f".gateway.pid.{pid}.{time.monotonic_ns()}.tmp"
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("secure PID records require O_NOFOLLOW")
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | no_follow,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        published = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if not published:
        raise RuntimeError("gateway PID record was not published")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeError("gateway PID record publication was unsafe")
    directory = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _publish_gateway_child_identity(
    path: Path,
    checkout: Path,
    child_pid: int,
    expected_argv: Sequence[str],
    *,
    qualification_sync_counter: bool = False,
) -> None:
    """Atomically add the real Web process identity after successful exec.

    The supervisor remains the process-group authority used by start/stop.
    Qualification needs the child identity as well because application SQLite
    and broker I/O are charged to that process, not to the log supervisor.
    """

    supervisor_pid = os.getpid()
    supervisor_pgid = os.getpgrp()
    child_parent, child_pgid = _process_parent_and_group(child_pid)
    if (
        supervisor_pgid != supervisor_pid
        or child_parent != supervisor_pid
        or child_pgid != supervisor_pgid
        or _process_argv(child_pid) != tuple(expected_argv)
        or Path(os.readlink(f"/proc/{child_pid}/cwd")) != checkout
    ):
        raise RuntimeError("gateway child escaped its managed process group")
    existing = path.lstat()
    if (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.getuid()
        or existing.st_nlink != 1
        or stat.S_IMODE(existing.st_mode) != 0o600
        or existing.st_size > 4096
    ):
        raise RuntimeError("gateway PID record changed before child publication")
    expected = (
        "schema=1\n"
        "role=gateway\n"
        f"pid={supervisor_pid}\n"
        f"pgid={supervisor_pgid}\n"
        f"marker={_process_marker(supervisor_pid)}\n"
        f"root={checkout}\n"
        + (f"sync_counter={COUNTER_ABI}\n" if qualification_sync_counter else "")
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        current = os.read(descriptor, 4097)
        current_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        current != expected
        or current_metadata.st_dev != existing.st_dev
        or current_metadata.st_ino != existing.st_ino
    ):
        raise RuntimeError("gateway PID record identity changed before child publication")

    payload = expected + (
        f"web_pid={child_pid}\n"
        f"web_marker={_process_marker(child_pid)}\n"
    ).encode("utf-8")
    temporary = path.parent / (
        f".gateway.pid.{supervisor_pid}.{time.monotonic_ns()}.tmp"
    )
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("secure PID records require O_NOFOLLOW")
    temporary_descriptor: int | None = None
    try:
        temporary_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | no_follow,
            0o600,
        )
        os.fchmod(temporary_descriptor, 0o600)
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        os.replace(temporary, path)
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    published = path.lstat()
    if (
        not stat.S_ISREG(published.st_mode)
        or published.st_uid != os.getuid()
        or published.st_nlink != 1
        or stat.S_IMODE(published.st_mode) != 0o600
        or published.st_size != len(payload)
    ):
        raise RuntimeError("gateway child identity publication was unsafe")
    directory = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class SecureRotatingFileHandler(RotatingFileHandler):
    """Rotate a fixed log name through an anchored directory descriptor."""

    def __init__(self, path: Path, *, max_bytes: int, backups: int) -> None:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        self._directory_fd = os.open(path.parent, directory_flags)
        self._entry_name = path.name
        self._maximum_bytes = max_bytes
        self._managed_backups = backups
        self._entry_identity: tuple[int, int] | None = None
        try:
            self._validate_existing_segments()
            super().__init__(
                path,
                maxBytes=max_bytes,
                backupCount=backups,
                encoding="utf-8",
            )
        except BaseException:
            os.close(self._directory_fd)
            self._directory_fd = -1
            raise

    def _metadata(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _validate_metadata(self, name: str, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"managed log segment is not a regular file: {name}")
        if metadata.st_uid != os.getuid():
            raise ValueError(f"managed log segment has the wrong owner: {name}")
        if metadata.st_nlink != 1:
            raise ValueError(f"managed log segment must not be hard-linked: {name}")
        if metadata.st_size > self._maximum_bytes:
            raise ValueError(f"managed log segment exceeds its size limit: {name}")

    def _validate_existing_segments(self) -> None:
        allowed = {
            self._entry_name,
            *(f"{self._entry_name}.{index}" for index in range(1, self._managed_backups + 1)),
        }
        for name in os.listdir(self._directory_fd):
            if name.startswith(f"{self._entry_name}."):
                suffix = name.removeprefix(f"{self._entry_name}.")
                if suffix.isdigit() and name not in allowed:
                    raise ValueError(f"unmanaged rotated log segment exists: {name}")
        for name in allowed:
            metadata = self._metadata(name)
            if metadata is not None:
                self._validate_metadata(name, metadata)

    def _open(self) -> object:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("secure logs require O_NOFOLLOW")
        descriptor = os.open(
            self._entry_name,
            flags | no_follow,
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            self._validate_metadata(self._entry_name, metadata)
            path_metadata = self._metadata(self._entry_name)
            if path_metadata is None or (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("managed log changed while it was opened")
            os.fchmod(descriptor, 0o600)
            self._entry_identity = (metadata.st_dev, metadata.st_ino)
            return os.fdopen(
                descriptor,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def validate_stream(self) -> None:
        if self.stream is None or self._entry_identity is None:
            raise ValueError("managed log stream is closed")
        metadata = os.fstat(self.stream.fileno())
        self._validate_metadata(self._entry_name, metadata)
        path_metadata = self._metadata(self._entry_name)
        if path_metadata is None or (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != self._entry_identity:
            raise ValueError("managed log path no longer names the open file")

    def _remove_destination(self, name: str) -> None:
        metadata = self._metadata(name)
        if metadata is None:
            return
        self._validate_metadata(name, metadata)
        os.unlink(name, dir_fd=self._directory_fd)

    def doRollover(self) -> None:  # noqa: N802 - standard logging API
        if self.stream is not None:
            self.validate_stream()
            self.stream.flush()
            self.stream.close()
            self.stream = None
            self._entry_identity = None

        for index in range(self._managed_backups - 1, 0, -1):
            source = f"{self._entry_name}.{index}"
            destination = f"{self._entry_name}.{index + 1}"
            source_metadata = self._metadata(source)
            if source_metadata is None:
                continue
            self._validate_metadata(source, source_metadata)
            self._remove_destination(destination)
            os.rename(
                source,
                destination,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )

        current_metadata = self._metadata(self._entry_name)
        if current_metadata is not None:
            self._validate_metadata(self._entry_name, current_metadata)
            first_backup = f"{self._entry_name}.1"
            self._remove_destination(first_backup)
            os.rename(
                self._entry_name,
                first_backup,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
        if not self.delay:
            self.stream = self._open()  # type: ignore[assignment]

    def close(self) -> None:
        try:
            super().close()
        finally:
            if getattr(self, "_directory_fd", -1) >= 0:
                os.close(self._directory_fd)
                self._directory_fd = -1


def _write(handler: SecureRotatingFileHandler, text: str) -> None:
    if not text:
        return
    record = logging.LogRecord("gateway", logging.INFO, "", 0, text, (), None)
    handler.acquire()
    try:
        handler.validate_stream()
        if handler.shouldRollover(record):
            handler.doRollover()
            handler.validate_stream()
        assert handler.stream is not None
        handler.stream.write(text)
        handler.flush()
    finally:
        handler.release()


def _capture_wait_timeout(*, has_pending: bool, now: float, last_flush: float) -> float:
    """Block while idle; only an actual pending batch has a flush deadline."""

    if not has_pending:
        return _CAPTURE_FLUSH_INTERVAL_SECONDS
    return max(0.0, _CAPTURE_FLUSH_INTERVAL_SECONDS - (now - last_flush))


def _capture(stream: object, handler: SecureRotatingFileHandler, max_bytes: int) -> None:
    """Batch bounded chunks so output without newlines cannot grow in memory."""

    descriptor = stream.fileno()  # type: ignore[attr-defined]
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = bytearray()
    flush_bytes = min(64 * 1024, max(1_024, max_bytes // 2))
    last_flush = time.monotonic()

    def flush(count: int | None = None, *, final: bool = False) -> None:
        nonlocal last_flush
        if count is None:
            payload = bytes(pending)
            pending.clear()
        else:
            payload = bytes(pending[:count])
            del pending[:count]
        _write(handler, decoder.decode(payload, final=final))
        last_flush = time.monotonic()

    try:
        while True:
            timeout = _capture_wait_timeout(
                has_pending=bool(pending),
                now=time.monotonic(),
                last_flush=last_flush,
            )
            try:
                ready = selector.select(timeout)
            except InterruptedError:
                ready = []
            if ready:
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except InterruptedError:
                    continue
                if not chunk:
                    while len(pending) > flush_bytes:
                        flush(flush_bytes)
                    flush(final=True)
                    return
                pending.extend(chunk)
            while len(pending) >= flush_bytes:
                flush(flush_bytes)
            if (
                pending
                and time.monotonic() - last_flush
                >= _CAPTURE_FLUSH_INTERVAL_SECONDS
            ):
                flush()
    finally:
        selector.close()


def main() -> int:
    args = _arguments()
    os.umask(0o077)

    if args.new_session and os.name != "nt" and os.getpid() != os.getsid(0):
        os.setsid()

    qualification_values: dict[str, str] = {}
    if args.qualification_sync_counter:
        try:
            qualification_root = Path(
                os.environ["AGENT_BUILDER_ROOT"]
            ).resolve(strict=True)
            qualification_values = qualification_environment(
                qualification_root,
                os.environ,
                expected_role="supervisor",
            )
        except (KeyError, OSError, SyncCounterError):
            print(
                "agent-builder log supervisor: invalid qualification counter configuration",
                file=sys.stderr,
            )
            return 2

    child_environment = os.environ.copy()
    if args.clean_env:
        already_clean = os.environ.get("_HARNESS_V2_CLEAN_ENV") == "1"
        child_environment = _sanitised_environment(os.environ)
        child_environment.update(qualification_values)
        child_environment["_HARNESS_V2_CLEAN_ENV"] = "1"
        if not already_clean:
            os.execve(
                sys.executable,
                [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                child_environment,
            )
        os.environ.clear()
        os.environ.update(child_environment)

    try:
        log_path = _managed_log_path(args.runtime_root, args.log_file)
        checkout, pid_path = _managed_pid_path(args.runtime_root, args.pid_file)
    except ValueError as exc:
        print(f"agent-builder log supervisor: {exc}", file=sys.stderr)
        return 2

    try:
        handler = SecureRotatingFileHandler(
            log_path,
            max_bytes=args.max_bytes,
            backups=args.backups,
        )
    except (OSError, ValueError) as exc:
        print(f"agent-builder log supervisor: {exc}", file=sys.stderr)
        return 2
    handler.setFormatter(logging.Formatter("%(message)s"))

    child: subprocess.Popen[bytes] | None = None
    stop_requested: int | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = signum
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)

    try:
        _publish_gateway_pid_record(
            pid_path,
            checkout,
            qualification_sync_counter=args.qualification_sync_counter,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"agent-builder log supervisor: {exc}", file=sys.stderr)
        handler.close()
        return 2

    if stop_requested is not None:
        handler.close()
        return 128 + stop_requested

    try:
        gateway_environment = child_environment
        if args.qualification_sync_counter:
            try:
                gateway_environment = {
                    **child_environment,
                    **qualification_environment(
                        checkout,
                        child_environment,
                        expected_role="supervisor",
                        child_role="gateway",
                    ),
                }
            except SyncCounterError:
                _write(handler, "gateway qualification counter validation failed\n")
                return 2
        child = subprocess.Popen(
            args.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=gateway_environment,
            bufsize=0,
        )
        try:
            _publish_gateway_child_identity(
                pid_path,
                checkout,
                child.pid,
                args.command,
                qualification_sync_counter=args.qualification_sync_counter,
            )
        except (OSError, RuntimeError, ValueError):
            _write(handler, "gateway child identity publication failed\n")
            return 127
        if stop_requested is not None and child.poll() is None:
            child.send_signal(stop_requested)

        assert child.stdout is not None
        _capture(child.stdout, handler, args.max_bytes)
        return child.wait()
    except OSError as exc:
        _write(handler, f"gateway launch failed: {exc}\n")
        return 127
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())

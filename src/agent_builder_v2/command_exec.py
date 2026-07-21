"""Allowlisted, shell-free command execution in a singleton kernel sandbox."""

from __future__ import annotations

import hashlib
import ctypes
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
from typing import Callable, Mapping

from .capsule import AgentCapsule
from .permissions import CapabilityOutcomeUnknownError, CapabilityRequest


COMMAND_ID = "runtime-compile"
BOUNDED_BASH_ID = "bounded-bash"
SKILL_RUN_ID = "skill-run"
RUNNER_POLICY = "singleton-landlock-seccomp-v1"
MAX_READY_BYTES = 2_048
MAX_COMMAND_OUTPUT_BYTES = 12 * 1024
COMMAND_READY_TIMEOUT_SECONDS = 2.0
COMMAND_WALL_TIMEOUT_SECONDS = 12.0
_RUNNER_ID = re.compile(r"^[a-f0-9]{32}$")
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434


class CommandExecutionError(RuntimeError):
    """The command could not be safely dispatched or supervised."""


class CommandOutcomeUnknownError(CommandExecutionError, CapabilityOutcomeUnknownError):
    """A released command could not be proven stopped and must not be replayed."""


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + payload).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _safe_regular(path: Path, *, executable: bool = False) -> os.stat_result:
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (executable and not stat.S_IMODE(metadata.st_mode) & 0o111)
    ):
        raise CommandExecutionError("allowlisted command identity is unsafe")
    return metadata


def _safe_system_executable(path: Path) -> os.stat_result:
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_nlink < 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o111
    ):
        raise CommandExecutionError("bounded Bash executable identity is unsafe")
    return metadata


def _file_digest(path: Path, maximum: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > maximum:
            raise CommandExecutionError("allowlisted command identity is too large")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) > maximum
            or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise CommandExecutionError("allowlisted command identity changed")
        return _digest(b"agent-builder-command-file-v1", bytes(content))
    finally:
        os.close(descriptor)


def _source_digest(source_root: Path) -> tuple[str, int, int]:
    root = os.stat(source_root, follow_symlinks=False)
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid():
        raise CommandExecutionError("command source root is unsafe")
    files: list[tuple[bytes, bytes]] = []
    entries = 0
    total = 0
    pending = [source_root]
    while pending:
        directory = pending.pop()
        children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        for child in children:
            entries += 1
            if entries > 256:
                raise CommandExecutionError("command source entry limit exceeded")
            metadata = child.stat(follow_symlinks=False)
            path = Path(child.path)
            if metadata.st_uid != os.getuid() or metadata.st_dev != root.st_dev:
                raise CommandExecutionError("command source identity changed")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CommandExecutionError("command source contains a special entry")
            if path.suffix != ".py":
                continue
            if metadata.st_nlink != 1:
                raise CommandExecutionError("command source file is unsafe")
            content = path.read_bytes()
            total += len(content)
            if total > 2 * 1024 * 1024:
                raise CommandExecutionError("command source byte limit exceeded")
            relative = str(path.relative_to(source_root)).encode("utf-8")
            files.append((relative, content))
    digest = hashlib.sha256(b"agent-builder-command-source-v1\0")
    for relative, content in sorted(files):
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), len(files), total


def _process_marker(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split()
    if closing < 0 or len(fields) < 20:
        raise CommandExecutionError("runner process identity is unavailable")
    return f"linux:{int(fields[19])}"


def _status(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/status").read_bytes()
    if len(raw) > 64 * 1024:
        raise CommandExecutionError("runner process status is too large")
    values: dict[str, str] = {}
    for line in raw.decode("ascii").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return values


def _pidfd_open(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    descriptor = libc.syscall(_SYS_PIDFD_OPEN, pid, 0)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise CommandExecutionError(f"pidfd supervision is unavailable: errno={error}")
    return int(descriptor)


def _pidfd_kill(descriptor: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(_SYS_PIDFD_SEND_SIGNAL, descriptor, signal.SIGKILL, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _write_pid_record(path: Path, values: Mapping[str, object]) -> None:
    payload = "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode("ascii")
    if len(payload) > 4_096:
        raise CommandExecutionError("runner PID record is too large")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CommandExecutionError("runner PID record write failed")
            view = view[written:]
    finally:
        os.close(descriptor)


def _cleanup_output(root: Path) -> None:
    entries = list(os.scandir(root))
    if len(entries) > 257:
        raise CommandExecutionError("command output cleanup limit exceeded")
    for entry in entries:
        metadata = entry.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > 2 * 1024 * 1024
        ):
            raise CommandExecutionError("command output cleanup found an unsafe entry")
        os.unlink(entry.path)


def _empty_output_identity(root: Path) -> tuple[int, int, int, int]:
    metadata = os.stat(root, follow_symlinks=False)
    with os.scandir(root) as iterator:
        has_entries = next(iterator, None) is not None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or has_entries
    ):
        raise CommandExecutionError("command output root is not empty and private")
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid


class PreparedCommandExecutor:
    executor_kind = "singleton-command-runner-v1"

    def __init__(
        self,
        capsule: AgentCapsule,
        source_root: Path,
        run_root: Path,
        prepared: Mapping[str, object],
        catalog_digest: str,
        executable_path: Path,
        executable_identity: tuple[int, int, int, str],
        bash_path: Path,
        bash_identity: tuple[int, int, int, str],
        python_home: Path,
        module_path: Path,
    ) -> None:
        self._capsule = capsule
        self._source_root = source_root
        self._run_root = run_root
        self._prepared = dict(prepared)
        self._executable_path = executable_path
        self._executable_identity = executable_identity
        self._bash_path = bash_path
        self._bash_identity = bash_identity
        self._python_home = python_home
        self._module_path = module_path
        runner_id = str(prepared["runner_id"])
        self.identity_digest = _digest(
            b"agent-builder-command-executor-instance-v1",
            f"{catalog_digest}\0{runner_id}\0{prepared['command_id']}\0{RUNNER_POLICY}".encode("ascii"),
        )

    def _terminate(
        self,
        process: subprocess.Popen[bytes],
        pidfd: int,
        marker: str,
    ) -> None:
        if process.poll() is not None:
            return
        try:
            _pidfd_kill(pidfd)
        except OSError:
            try:
                if _process_marker(process.pid) != marker:
                    raise CommandOutcomeUnknownError("runner PID identity changed")
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise CommandOutcomeUnknownError("runner did not converge after kill") from exc

    def execute(self, request: CapabilityRequest, cancelled: Callable[[], bool]) -> str:
        try:
            prepared = json.loads(request.arguments_json)
        except json.JSONDecodeError as exc:
            raise CommandExecutionError("prepared command is invalid") from exc
        return self._execute_prepared_value(prepared, cancelled)

    def execute_prepared(self, cancelled: Callable[[], bool]) -> str:
        """Execute the already-bound request for a trusted background Task."""

        return self._execute_prepared_value(dict(self._prepared), cancelled)

    def _execute_prepared_value(
        self,
        prepared: object,
        cancelled: Callable[[], bool],
    ) -> str:
        if not isinstance(prepared, dict) or prepared != self._prepared:
            raise CommandExecutionError("prepared command binding changed")
        command_id = prepared.get("command_id")
        expected_keys = {
            "schema_version", "command_id", "runner_id", "source_digest",
            "source_files", "source_bytes", "catalog_digest", "output_identity",
            "cwd_policy", "environment_policy", "redirections",
        }
        if command_id == BOUNDED_BASH_ID:
            expected_keys.update({"normalized_script", "ast_digest", "bash_identity"})
        elif command_id == SKILL_RUN_ID:
            expected_keys.update(
                {"skill_id", "skill_version", "package_digest", "entrypoint", "input_json"}
            )
        if set(prepared) != expected_keys:
            raise CommandExecutionError("prepared command binding changed")
        runner_id = prepared.get("runner_id")
        if (
            prepared.get("schema_version") != 1
            or command_id not in {COMMAND_ID, BOUNDED_BASH_ID, SKILL_RUN_ID}
            or not isinstance(runner_id, str)
            or _RUNNER_ID.fullmatch(runner_id) is None
        ):
            raise CommandExecutionError("prepared command is invalid")
        if (
            prepared.get("cwd_policy") != "isolated-work-root-v1"
            or prepared.get("environment_policy") != "clean-command-env-v1"
            or prepared.get("redirections") != []
        ):
            raise CommandExecutionError("prepared command policy changed")
        source_digest, source_files, source_bytes = _source_digest(self._source_root)
        if (source_digest, source_files, source_bytes) != (
            prepared.get("source_digest"),
            prepared.get("source_files"),
            prepared.get("source_bytes"),
        ):
            raise CommandExecutionError("allowlisted command source changed")
        executable = self._capsule.interpreter.resolve(strict=True)
        executable_metadata = _safe_regular(executable, executable=True)
        current_executable_identity = (
            executable_metadata.st_dev,
            executable_metadata.st_ino,
            executable_metadata.st_size,
            _file_digest(executable, 64 * 1024 * 1024),
        )
        if (
            executable != self._executable_path
            or current_executable_identity != self._executable_identity
        ):
            raise CommandExecutionError("allowlisted executable identity changed")
        if list(_empty_output_identity(self._run_root / "output")) != prepared.get(
            "output_identity"
        ):
            raise CommandExecutionError("command output identity changed")
        if command_id == BOUNDED_BASH_ID:
            from .bounded_bash import parse_bounded_bash

            plan = parse_bounded_bash(prepared.get("normalized_script"))
            if plan.normalized_script != prepared.get("normalized_script") or plan.ast_digest != prepared.get("ast_digest"):
                raise CommandExecutionError("bounded Bash AST binding changed")
            bash_metadata = _safe_system_executable(self._bash_path)
            current_bash_identity = (
                bash_metadata.st_dev,
                bash_metadata.st_ino,
                bash_metadata.st_size,
                _file_digest(self._bash_path, 8 * 1024 * 1024),
            )
            if current_bash_identity != self._bash_identity or list(current_bash_identity) != prepared.get("bash_identity"):
                raise CommandExecutionError("bounded Bash executable identity changed")
        elif command_id == SKILL_RUN_ID:
            entrypoint = prepared.get("entrypoint")
            if entrypoint != "main.py" or not (self._source_root / "main.py").is_file():
                raise CommandExecutionError("Skill entrypoint binding changed")
            try:
                skill_input = json.loads(str(prepared.get("input_json")))
            except json.JSONDecodeError as exc:
                raise CommandExecutionError("Skill input binding is invalid") from exc
            if not isinstance(skill_input, dict):
                raise CommandExecutionError("Skill input binding is invalid")

        executable_fd = os.open(
            self._executable_path,
            os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened_executable = os.fstat(executable_fd)
        if (
            opened_executable.st_dev,
            opened_executable.st_ino,
            opened_executable.st_size,
        ) != self._executable_identity[:3]:
            os.close(executable_fd)
            raise CommandExecutionError("allowlisted executable raced before dispatch")
        bash_fd = -1
        if command_id == BOUNDED_BASH_ID:
            bash_fd = os.open(self._bash_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            opened_bash = os.fstat(bash_fd)
            if (opened_bash.st_dev, opened_bash.st_ino, opened_bash.st_size) != self._bash_identity[:3]:
                os.close(bash_fd)
                bash_fd = -1
                os.close(executable_fd)
                raise CommandExecutionError("bounded Bash executable raced before dispatch")
        try:
            ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        except BaseException:
            if bash_fd >= 0:
                os.close(bash_fd)
            os.close(executable_fd)
            raise
        try:
            release_read, release_write = os.pipe2(os.O_CLOEXEC)
        except BaseException:
            os.close(ready_read)
            os.close(ready_write)
            os.close(executable_fd)
            if bash_fd >= 0:
                os.close(bash_fd)
            raise
        process: subprocess.Popen[bytes] | None = None
        pidfd: int | None = None
        pid_record = self._run_root / f"runner-{runner_id}.pid"
        released = False
        try:
            environment = {
                "HOME": str(self._run_root / "home"),
                "TMPDIR": str(self._run_root / "tmp"),
                "XDG_CACHE_HOME": str(self._run_root / "xdg" / "cache"),
                "XDG_CONFIG_HOME": str(self._run_root / "xdg" / "config"),
                "XDG_DATA_HOME": str(self._run_root / "xdg" / "data"),
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
                "PYTHONHOME": str(self._python_home),
                "PYTHONPATH": str(self._module_path),
                "AGENT_BUILDER_RUNNER_ID": runner_id,
                "AGENT_BUILDER_RUNNER_SOURCE": str(self._source_root),
                "AGENT_BUILDER_RUNNER_OUTPUT": str(self._run_root / "output"),
                "AGENT_BUILDER_RUNNER_WORK": str(self._run_root / "work"),
                "AGENT_BUILDER_RUNNER_SOURCE_DIGEST": source_digest,
                "AGENT_BUILDER_RUNNER_READY_FD": str(ready_write),
                "AGENT_BUILDER_RUNNER_RELEASE_FD": str(release_read),
                "AGENT_BUILDER_RUNNER_PARENT_PID": str(os.getpid()),
                "AGENT_BUILDER_RUNNER_MODE": str(command_id),
            }
            pass_descriptors = [ready_write, release_read, executable_fd]
            if command_id == BOUNDED_BASH_ID:
                environment.update(
                    {
                        "AGENT_BUILDER_RUNNER_SCRIPT": str(prepared["normalized_script"]),
                        "AGENT_BUILDER_RUNNER_AST_DIGEST": str(prepared["ast_digest"]),
                        "AGENT_BUILDER_RUNNER_BASH_FD": str(bash_fd),
                        "AGENT_BUILDER_RUNNER_BASH_PATH": str(self._bash_path),
                        "AGENT_BUILDER_RUNNER_BASH_IDENTITY": _canonical(list(self._bash_identity)),
                    }
                )
                pass_descriptors.append(bash_fd)
            elif command_id == SKILL_RUN_ID:
                environment.update(
                    {
                        "AGENT_BUILDER_SKILL_ID": str(prepared["skill_id"]),
                        "AGENT_BUILDER_SKILL_VERSION": str(prepared["skill_version"]),
                        "AGENT_BUILDER_SKILL_PACKAGE_DIGEST": str(prepared["package_digest"]),
                        "AGENT_BUILDER_SKILL_ENTRYPOINT": str(prepared["entrypoint"]),
                        "AGENT_BUILDER_SKILL_INPUT": str(prepared["input_json"]),
                    }
                )
            process = subprocess.Popen(
                (
                    "capsule-python",
                    "-B",
                    "-m",
                    "agent_builder_v2.command_child",
                ),
                executable=f"/proc/self/fd/{executable_fd}",
                cwd=self._run_root / "work",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                pass_fds=tuple(pass_descriptors),
                start_new_session=True,
            )
            os.close(ready_write)
            ready_write = -1
            os.close(release_read)
            release_read = -1
            os.close(executable_fd)
            executable_fd = -1
            if bash_fd >= 0:
                os.close(bash_fd)
                bash_fd = -1
            pidfd = _pidfd_open(process.pid)
            marker = _process_marker(process.pid)
            selector = selectors.DefaultSelector()
            selector.register(ready_read, selectors.EVENT_READ)
            ready = bytearray()
            deadline = time.monotonic() + COMMAND_READY_TIMEOUT_SECONDS
            while b"\n" not in ready:
                if cancelled():
                    self._terminate(process, pidfd, marker)
                    raise CommandExecutionError("command cancelled before dispatch")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process, pidfd, marker)
                    raise CommandExecutionError("runner attestation timed out")
                if not selector.select(min(remaining, 0.05)):
                    if process.poll() is not None:
                        raise CommandExecutionError("runner exited before attestation")
                    continue
                chunk = os.read(ready_read, MAX_READY_BYTES + 1 - len(ready))
                if not chunk:
                    raise CommandExecutionError("runner attestation was incomplete")
                ready.extend(chunk)
                if len(ready) > MAX_READY_BYTES:
                    raise CommandExecutionError("runner attestation exceeded its limit")
            selector.close()
            try:
                attestation = json.loads(bytes(ready).decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CommandExecutionError("runner attestation is invalid") from exc
            expected_keys = {
                "internal", "version", "runner_id", "pid", "parent_pid",
                "landlock_abi", "seccomp_arch", "seccomp_mode",
                "no_new_privileges", "process_creation_denied", "network_denied",
                "descriptor_isolation", "source_digest",
            }
            if (
                not isinstance(attestation, dict)
                or set(attestation) != expected_keys
                or attestation.get("internal") != "runner.ready"
                or attestation.get("version") != 1
                or attestation.get("runner_id") != runner_id
                or attestation.get("pid") != process.pid
                or attestation.get("parent_pid") != os.getpid()
                or not isinstance(attestation.get("landlock_abi"), int)
                or attestation["landlock_abi"] < 6
                or attestation.get("seccomp_arch") not in {"x86_64", "aarch64"}
                or attestation.get("seccomp_mode") != 2
                or attestation.get("no_new_privileges") is not True
                or attestation.get("process_creation_denied") is not True
                or attestation.get("network_denied") is not True
                or attestation.get("descriptor_isolation") is not True
                or attestation.get("source_digest") != source_digest
            ):
                raise CommandExecutionError("runner attestation failed validation")
            status = _status(process.pid)
            if (
                int(status["PPid"].split()[0]) != os.getpid()
                or int(status["NoNewPrivs"].split()[0]) != 1
                or int(status["Seccomp"].split()[0]) != 2
                or int(status["Seccomp_filters"].split()[0]) < 1
            ):
                raise CommandExecutionError("runner kernel state is invalid")
            _write_pid_record(
                pid_record,
                {
                    "schema": 1,
                    "runner_id": runner_id,
                    "pid": process.pid,
                    "marker": marker,
                    "policy": RUNNER_POLICY,
                    "executor_identity_digest": self.identity_digest,
                    "source_digest": source_digest,
                },
            )
            if cancelled():
                self._terminate(process, pidfd, marker)
                raise CommandExecutionError("command cancelled before dispatch")
            os.write(release_write, b"1")
            os.close(release_write)
            release_write = -1
            released = True

            assert process.stdout is not None and process.stderr is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            deadline = time.monotonic() + COMMAND_WALL_TIMEOUT_SECONDS
            while selector.get_map():
                if cancelled() or time.monotonic() >= deadline:
                    self._terminate(process, pidfd, marker)
                    break
                for key, _events in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 4_096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk)
                    if sum(len(value) for value in buffers.values()) > MAX_COMMAND_OUTPUT_BYTES:
                        self._terminate(process, pidfd, marker)
                        raise CommandExecutionError("command output exceeded its limit")
            selector.close()
            return_code = process.wait(timeout=2)
            result = {
                "kind": "command_result",
                "command_id": command_id,
                "exit_code": return_code,
                "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
                "stderr": buffers["stderr"].decode("utf-8", errors="replace"),
                "timed_out": time.monotonic() >= deadline,
                "cancelled": cancelled(),
                "sandbox": RUNNER_POLICY,
                "source_digest": source_digest,
            }
            _cleanup_output(self._run_root / "output")
            encoded = _canonical(result)
            if len(encoded.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES:
                raise CommandExecutionError("command result exceeded its limit")
            return encoded
        except CommandOutcomeUnknownError:
            raise
        except BaseException:
            if process is not None and process.poll() is None and pidfd is not None:
                try:
                    self._terminate(process, pidfd, _process_marker(process.pid))
                except CommandOutcomeUnknownError:
                    if released:
                        raise
            raise
        finally:
            for descriptor in (
                ready_read, ready_write, release_read, release_write,
                executable_fd, bash_fd, pidfd,
            ):
                if descriptor is not None and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                if process.poll() is not None:
                    try:
                        _cleanup_output(self._run_root / "output")
                    except (FileNotFoundError, CommandExecutionError):
                        pass
            try:
                pid_record.unlink()
            except FileNotFoundError:
                pass


class CommandExecutor:
    """Trusted catalog and preparation boundary for the v1 command runner."""

    def __init__(
        self,
        repository_root: Path,
        source_parent: Path,
        capsule: AgentCapsule,
    ) -> None:
        self._repository_root = repository_root.resolve(strict=True)
        self._capsule = capsule
        self._source_root = source_parent.resolve(strict=True) / "agent_builder_v2"
        try:
            capsule.interpreter.relative_to(self._repository_root)
        except ValueError as exc:
            raise CommandExecutionError("runner executable escaped the checkout") from exc
        executable = capsule.interpreter.resolve(strict=True)
        executable_metadata = _safe_regular(executable, executable=True)
        executable_digest = _file_digest(executable, 64 * 1024 * 1024)
        self._executable_path = executable
        self._executable_identity = (
            executable_metadata.st_dev,
            executable_metadata.st_ino,
            executable_metadata.st_size,
            executable_digest,
        )
        bash_path = Path("/bin/bash").resolve(strict=True)
        bash_metadata = _safe_system_executable(bash_path)
        bash_digest = _file_digest(bash_path, 8 * 1024 * 1024)
        self._bash_path = bash_path
        self._bash_identity = (
            bash_metadata.st_dev,
            bash_metadata.st_ino,
            bash_metadata.st_size,
            bash_digest,
        )
        source_digest, source_files, source_bytes = _source_digest(self._source_root)
        self._source_digest = source_digest
        self._source_files = source_files
        self._source_bytes = source_bytes
        self.catalog_digest = _digest(
            b"agent-builder-command-catalog-v1",
            _canonical(
                {
                    "command_id": COMMAND_ID,
                    "bounded_bash": {
                        "command_id": BOUNDED_BASH_ID,
                        "executable_identity": list(self._bash_identity),
                        "grammar": "builtin-only-v1",
                    },
                    "executable_identity": [
                        executable_metadata.st_dev,
                        executable_metadata.st_ino,
                        executable_metadata.st_size,
                        executable_digest,
                    ],
                    "argv": ["-B", "-m", "agent_builder_v2.command_child"],
                    "source_digest": source_digest,
                    "sandbox": RUNNER_POLICY,
                }
            ).encode("utf-8"),
        )

    def prepare(
        self,
        arguments: Mapping[str, str | int | bool],
        run_root: Path,
    ) -> tuple[dict[str, object], str, PreparedCommandExecutor]:
        command_id = arguments.get("command_id")
        if command_id == COMMAND_ID and arguments == {"command_id": COMMAND_ID}:
            bash_plan = None
        elif command_id == BOUNDED_BASH_ID and set(arguments) == {"command_id", "script"}:
            from .bounded_bash import BashParseError, parse_bounded_bash

            try:
                bash_plan = parse_bounded_bash(arguments.get("script"))
            except BashParseError as exc:
                raise CommandExecutionError(str(exc)) from exc
        else:
            raise CommandExecutionError("command is not in the allowlist")
        runner_id = os.urandom(16).hex()
        output_identity = _empty_output_identity(run_root / "output")
        prepared = {
            "schema_version": 1,
            "command_id": command_id,
            "runner_id": runner_id,
            "source_digest": self._source_digest,
            "source_files": self._source_files,
            "source_bytes": self._source_bytes,
            "catalog_digest": self.catalog_digest,
            "output_identity": list(output_identity),
            "cwd_policy": "isolated-work-root-v1",
            "environment_policy": "clean-command-env-v1",
            "redirections": [],
        }
        if bash_plan is not None:
            prepared.update(
                {
                    "normalized_script": bash_plan.normalized_script,
                    "ast_digest": bash_plan.ast_digest,
                    "bash_identity": list(self._bash_identity),
                }
            )
        preview = _canonical(
            {
                "action": "exec/run",
                "command_id": command_id,
                "executable": (
                    "capsule-python" if bash_plan is None else "system-bash"
                ),
                "argv": (
                    ["compile trusted runtime sources"]
                    if bash_plan is None
                    else ["--noprofile", "--norc", "-c", bash_plan.normalized_script]
                ),
                "normalized_ast": None if bash_plan is None else bash_plan.ast(),
                "cwd": "run/work",
                "sandbox": RUNNER_POLICY,
                "network": "denied",
                "write_scope": "run/output" if bash_plan is None else "none",
                "source_digest": self._source_digest,
            }
        )
        return (
            prepared,
            preview,
            PreparedCommandExecutor(
                self._capsule,
                self._source_root,
                run_root,
                prepared,
                self.catalog_digest,
                self._executable_path,
                self._executable_identity,
                self._bash_path,
                self._bash_identity,
                self._executable_path.parent.parent,
                self._source_root.parent,
            ),
        )

    def prepare_skill(
        self,
        *,
        skill_id: str,
        skill_version: str,
        package_digest: str,
        package_root: Path,
        interpreter: Path,
        input_value: dict[str, object],
        run_root: Path,
    ) -> tuple[dict[str, object], str, PreparedCommandExecutor]:
        source_root = package_root.resolve(strict=True)
        try:
            source_root.relative_to(self._capsule.data_root / "skills")
        except ValueError as exc:
            raise CommandExecutionError("Skill package escaped its Capsule") from exc
        if not (source_root / "main.py").is_file():
            raise CommandExecutionError("Skill entrypoint is missing")
        input_json = _canonical(input_value)
        if len(input_json.encode("utf-8")) > 4_096:
            raise CommandExecutionError("Skill input exceeds its byte limit")
        source_digest, source_files, source_bytes = _source_digest(source_root)
        if source_files != 1 or len(package_digest) != 64:
            raise CommandExecutionError("Skill package identity changed")
        runner_id = os.urandom(16).hex()
        output_identity = _empty_output_identity(run_root / "output")
        catalog_digest = _digest(
            b"agent-builder-skill-executor-catalog-v1",
            f"{skill_id}\0{skill_version}\0{package_digest}\0{RUNNER_POLICY}".encode(),
        )
        executable_path = interpreter.resolve(strict=True)
        try:
            executable_path.relative_to(self._capsule.runtime_root / "skills" / skill_id)
        except ValueError as exc:
            raise CommandExecutionError("Skill interpreter escaped its environment") from exc
        executable_metadata = _safe_regular(executable_path, executable=True)
        executable_identity = (
            executable_metadata.st_dev,
            executable_metadata.st_ino,
            executable_metadata.st_size,
            _file_digest(executable_path, 64 * 1024 * 1024),
        )
        skill_capsule = AgentCapsule(
            self._capsule.agent_id,
            self._capsule.data_root,
            self._capsule.runtime_root,
            interpreter,
            self._capsule.generation,
            self._capsule.display_name,
        )
        prepared = {
            "schema_version": 1,
            "command_id": SKILL_RUN_ID,
            "runner_id": runner_id,
            "source_digest": source_digest,
            "source_files": source_files,
            "source_bytes": source_bytes,
            "catalog_digest": catalog_digest,
            "output_identity": list(output_identity),
            "cwd_policy": "isolated-work-root-v1",
            "environment_policy": "clean-command-env-v1",
            "redirections": [],
            "skill_id": skill_id,
            "skill_version": skill_version,
            "package_digest": package_digest,
            "entrypoint": "main.py",
            "input_json": input_json,
        }
        preview = _canonical(
            {
                "action": "skill/run",
                "skill_id": skill_id,
                "version": skill_version,
                "package_digest": package_digest,
                "entrypoint": "main.py",
                "input": input_value,
                "network": "denied",
                "write_scope": "transient-run-output",
                "sandbox": RUNNER_POLICY,
            }
        )
        return (
            prepared,
            preview,
            PreparedCommandExecutor(
                skill_capsule,
                source_root,
                run_root,
                prepared,
                catalog_digest,
                executable_path,
                executable_identity,
                self._bash_path,
                self._bash_identity,
                self._executable_path.parent.parent,
                self._source_root.parent,
            ),
        )


__all__ = [
    "COMMAND_ID",
    "BOUNDED_BASH_ID",
    "COMMAND_WALL_TIMEOUT_SECONDS",
    "CommandExecutionError",
    "CommandExecutor",
    "CommandOutcomeUnknownError",
    "PreparedCommandExecutor",
    "RUNNER_POLICY",
    "SKILL_RUN_ID",
]

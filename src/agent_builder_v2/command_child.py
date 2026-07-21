"""Fixed payload for the singleton allowlisted command runner."""

from __future__ import annotations

import hashlib
import ctypes
import errno
import json
import os
from pathlib import Path
import py_compile
import re
import runpy
import socket
import stat
import sys

from .bounded_bash import parse_bounded_bash
from .sandbox import (
    WorkerResourceLimits,
    apply_singleton_command_sandbox,
    apply_bounded_bash_sandbox,
    apply_worker_resource_limits,
    apply_worker_umask,
)


_RUNNER_ID = re.compile(r"^[a-f0-9]{32}$")
MAX_SOURCE_ENTRIES = 256
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError("runner environment is incomplete")
    return value


def _source_files(source_root: Path) -> tuple[tuple[Path, ...], str, int]:
    files: list[Path] = []
    total = 0
    entries = 0
    root_device = source_root.stat().st_dev
    pending = [source_root]
    while pending:
        directory = pending.pop()
        children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        for child in children:
            entries += 1
            if entries > MAX_SOURCE_ENTRIES:
                raise RuntimeError("runner source entry limit exceeded")
            metadata = child.stat(follow_symlinks=False)
            if metadata.st_uid != os.getuid() or metadata.st_dev != root_device:
                raise RuntimeError("runner source identity changed")
            path = Path(child.path)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode) and path.suffix == ".py":
                if metadata.st_nlink != 1:
                    raise RuntimeError("runner source file is unsafe")
                total += metadata.st_size
                if total > MAX_SOURCE_BYTES:
                    raise RuntimeError("runner source byte limit exceeded")
                files.append(path)
            elif not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("runner source contains a special entry")
    ordered = tuple(sorted(files, key=lambda path: os.fsencode(str(path.relative_to(source_root)))))
    digest = hashlib.sha256(b"agent-builder-command-source-v1\0")
    for path in ordered:
        relative = str(path.relative_to(source_root)).encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return ordered, digest.hexdigest(), total


def _emit_ready(descriptor: int, value: dict[str, object]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(payload) > 2_048:
        raise RuntimeError("runner attestation exceeded its limit")
    os.write(descriptor, payload)
    os.close(descriptor)


def _close_extra_descriptors(preserved: set[int]) -> None:
    try:
        names = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise RuntimeError("runner descriptors cannot be audited") from exc
    for name in names:
        if not name.isdigit():
            continue
        descriptor = int(name)
        if descriptor <= 2 or descriptor in preserved:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass
    live = []
    for name in os.listdir("/proc/self/fd"):
        if name.isdigit() and int(name) > 2:
            try:
                os.fstat(int(name))
            except OSError:
                continue
            live.append(int(name))
    if sorted(live) != sorted(preserved):
        raise RuntimeError("runner inherited an unexpected descriptor")


def _descriptor_digest(descriptor: int, maximum: int) -> str:
    metadata = os.fstat(descriptor)
    if metadata.st_size > maximum:
        raise RuntimeError("bounded Bash executable is too large")
    offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = bytearray()
    while len(content) <= maximum:
        chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    os.lseek(descriptor, offset, os.SEEK_SET)
    if len(content) > maximum:
        raise RuntimeError("bounded Bash executable is too large")
    return hashlib.sha256(
        b"agent-builder-command-file-v1\0" + bytes(content)
    ).hexdigest()


def main() -> int:
    mode = _required_environment("AGENT_BUILDER_RUNNER_MODE")
    expected_environment = {
        "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "LC_ALL", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "PYTHONNOUSERSITE",
        "PYTHONHOME",
        "PYTHONPATH", "AGENT_BUILDER_RUNNER_ID", "AGENT_BUILDER_RUNNER_SOURCE",
        "AGENT_BUILDER_RUNNER_OUTPUT", "AGENT_BUILDER_RUNNER_WORK",
        "AGENT_BUILDER_RUNNER_SOURCE_DIGEST", "AGENT_BUILDER_RUNNER_READY_FD",
        "AGENT_BUILDER_RUNNER_RELEASE_FD", "AGENT_BUILDER_RUNNER_PARENT_PID",
        "AGENT_BUILDER_RUNNER_MODE", "AGENT_BUILDER_RUNNER_ENVIRONMENT",
    }
    if mode == "bounded-bash":
        expected_environment.update(
            {
                "AGENT_BUILDER_RUNNER_SCRIPT",
                "AGENT_BUILDER_RUNNER_AST_DIGEST",
                "AGENT_BUILDER_RUNNER_BASH_FD",
                "AGENT_BUILDER_RUNNER_BASH_PATH",
                "AGENT_BUILDER_RUNNER_BASH_IDENTITY",
            }
        )
    elif mode == "skill-run":
        expected_environment.update(
            {
                "AGENT_BUILDER_SKILL_ID",
                "AGENT_BUILDER_SKILL_VERSION",
                "AGENT_BUILDER_SKILL_PACKAGE_DIGEST",
                "AGENT_BUILDER_SKILL_ENTRYPOINT",
                "AGENT_BUILDER_SKILL_INPUT",
            }
        )
    elif mode != "runtime-compile":
        raise RuntimeError("runner mode is invalid")
    if set(os.environ) != expected_environment:
        raise RuntimeError("runner inherited an unexpected environment variable")
    apply_worker_umask()
    apply_worker_resource_limits(
        WorkerResourceLimits(
            cpu_seconds=10,
            address_space_bytes=256 * 1024 * 1024,
            file_size_bytes=2 * 1024 * 1024,
            open_files=32,
            processes=1,
        )
    )
    runner_id = _required_environment("AGENT_BUILDER_RUNNER_ID")
    if _RUNNER_ID.fullmatch(runner_id) is None:
        raise RuntimeError("runner identity is invalid")
    source_root = Path(_required_environment("AGENT_BUILDER_RUNNER_SOURCE"))
    output_root = Path(_required_environment("AGENT_BUILDER_RUNNER_OUTPUT"))
    work_root = Path(_required_environment("AGENT_BUILDER_RUNNER_WORK"))
    environment_root = Path(
        _required_environment("AGENT_BUILDER_RUNNER_ENVIRONMENT")
    )
    expected_source_digest = _required_environment("AGENT_BUILDER_RUNNER_SOURCE_DIGEST")
    ready_fd = int(_required_environment("AGENT_BUILDER_RUNNER_READY_FD"))
    release_fd = int(_required_environment("AGENT_BUILDER_RUNNER_RELEASE_FD"))
    expected_parent = int(_required_environment("AGENT_BUILDER_RUNNER_PARENT_PID"))
    bash_fd = (
        int(_required_environment("AGENT_BUILDER_RUNNER_BASH_FD"))
        if mode == "bounded-bash"
        else None
    )
    preserved = {ready_fd, release_fd}
    if bash_fd is not None:
        preserved.add(bash_fd)
    _close_extra_descriptors(preserved)

    files, source_digest, source_bytes = _source_files(source_root)
    if source_digest != expected_source_digest:
        raise RuntimeError("runner source snapshot changed")
    skill_input = None
    if mode == "skill-run":
        if _required_environment("AGENT_BUILDER_SKILL_ENTRYPOINT") != "main.py":
            raise RuntimeError("Skill entrypoint is invalid")
        skill_input = _required_environment("AGENT_BUILDER_SKILL_INPUT")
        if len(skill_input.encode("utf-8")) > 4_096:
            raise RuntimeError("Skill input exceeds its byte limit")
        if not isinstance(json.loads(skill_input), dict):
            raise RuntimeError("Skill input is invalid")
    bash_plan = None
    if mode == "bounded-bash":
        bash_plan = parse_bounded_bash(
            _required_environment("AGENT_BUILDER_RUNNER_SCRIPT")
        )
        if bash_plan.ast_digest != _required_environment(
            "AGENT_BUILDER_RUNNER_AST_DIGEST"
        ):
            raise RuntimeError("bounded Bash AST binding changed")
        assert bash_fd is not None
        expected_bash_identity = json.loads(
            _required_environment("AGENT_BUILDER_RUNNER_BASH_IDENTITY")
        )
        opened_bash = os.fstat(bash_fd)
        actual_bash_identity = [
            opened_bash.st_dev,
            opened_bash.st_ino,
            opened_bash.st_size,
            _descriptor_digest(bash_fd, 8 * 1024 * 1024),
        ]
        if actual_bash_identity != expected_bash_identity:
            raise RuntimeError("bounded Bash executable identity changed")
        attestation = apply_bounded_bash_sandbox(
            Path(_required_environment("AGENT_BUILDER_RUNNER_BASH_PATH")),
            work_root,
            expected_parent_pid=expected_parent,
        )
    else:
        attestation = apply_singleton_command_sandbox(
            source_root,
            output_root,
            work_root,
            environment_root=environment_root,
            expected_parent_pid=expected_parent,
        )
    _emit_ready(
        ready_fd,
        {
            "internal": "runner.ready",
            "version": 1,
            "runner_id": runner_id,
            "pid": os.getpid(),
            "parent_pid": attestation.parent_pid,
            "landlock_abi": attestation.landlock_abi,
            "seccomp_arch": attestation.seccomp_arch,
            "seccomp_mode": attestation.seccomp_mode,
            "no_new_privileges": attestation.no_new_privileges,
            "process_creation_denied": True,
            "network_denied": True,
            "descriptor_isolation": True,
            "source_digest": source_digest,
        },
    )
    if os.read(release_fd, 2) != b"1":
        raise RuntimeError("runner was not released")
    os.close(release_fd)

    if bash_plan is not None:
        assert bash_fd is not None
        os.execve(
            bash_fd,
            ("bash", "--noprofile", "--norc", "-c", bash_plan.normalized_script),
            {
                "HOME": _required_environment("HOME"),
                "TMPDIR": _required_environment("TMPDIR"),
                "LC_ALL": "C.UTF-8",
            },
        )
        raise RuntimeError("bounded Bash image replacement returned")

    try:
        forked = os.fork()
    except OSError:
        fork_denied = True
    else:
        if forked == 0:
            os._exit(91)
        os.waitpid(forked, 0)
        raise RuntimeError("runner process creation was not denied")
    try:
        probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        network_denied = True
    else:
        probe_socket.close()
        raise RuntimeError("runner network creation was not denied")
    execve_number = 59 if attestation.seccomp_arch == "x86_64" else 221
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(execve_number, 0, 0, 0) != -1 or ctypes.get_errno() != errno.EPERM:
        raise RuntimeError("runner image replacement was not denied")

    if skill_input is not None:
        clean_environment = {
            "HOME": _required_environment("HOME"),
            "TMPDIR": _required_environment("TMPDIR"),
            "LC_ALL": "C.UTF-8",
            "AGENT_BUILDER_SKILL_INPUT": skill_input,
        }
        os.environ.clear()
        os.environ.update(clean_environment)
        sys.argv = ["main.py"]
        runpy.run_path(str(source_root / "main.py"), run_name="__main__")
        return 0

    output_bytes = 0
    for index, source in enumerate(files):
        relative = str(source.relative_to(source_root))
        name_digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
        destination = output_root / f"{index:03d}-{name_digest}.pyc"
        py_compile.compile(
            str(source),
            cfile=str(destination),
            dfile=f"agent_builder_v2/{relative}",
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        metadata = os.lstat(destination)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError("runner output is unsafe")
        output_bytes += metadata.st_blocks * 512
        if output_bytes > MAX_OUTPUT_BYTES:
            raise RuntimeError("runner output quota exceeded")
    _files_after, digest_after, _bytes_after = _source_files(source_root)
    if digest_after != source_digest:
        raise RuntimeError("runner source changed during execution")
    print(
        json.dumps(
            {
                "command_id": "runtime-compile",
                "outcome": "completed",
                "source_files": len(files),
                "source_bytes": source_bytes,
                "output_files": len(files),
                "allocated_output_bytes": output_bytes,
                "source_digest": source_digest,
                "fork_denied": fork_denied,
                "network_denied": network_denied,
                "environment_clean": True,
                "exec_denied": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        try:
            print(
                json.dumps(
                    {
                        "error": "runner failed closed",
                        "error_type": type(exc).__name__,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )
        finally:
            raise
    raise SystemExit(exit_code)

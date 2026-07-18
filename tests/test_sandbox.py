"""Real-kernel negative tests for the fail-closed Harness V2 Worker sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import agent_builder_v2.sandbox as sandbox_module
from agent_builder_v2.sandbox import (
    MINIMUM_LANDLOCK_ABI,
    SandboxUnavailableError,
    qualify_host,
    verify_worker_file_descriptors,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _sandbox_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    run_root = _private_directory(tmp_path / "run")
    for name in ("input", "home", "tmp", "xdg", "work", "output"):
        _private_directory(run_root / name)
    environment_root = _private_directory(tmp_path / "environment")
    source_root = _private_directory(tmp_path / "source")
    outside_root = _private_directory(tmp_path / "outside")
    (run_root / "input" / "allowed.txt").write_text("allowed", encoding="utf-8")
    (source_root / "readonly.txt").write_text("source", encoding="utf-8")
    (outside_root / "secret.txt").write_text("secret", encoding="utf-8")
    return run_root, environment_root, source_root, outside_root


def test_host_qualifies_for_complete_worker_policy() -> None:
    qualification = qualify_host()

    assert qualification.qualified, qualification.reason
    assert qualification.landlock_abi >= MINIMUM_LANDLOCK_ABI
    assert qualification.machine in {"x86_64", "aarch64"}


def test_both_architecture_policies_block_persistence_and_cross_process_controls() -> None:
    x86 = sandbox_module._SECCOMP_ARCHITECTURES["x86_64"][2]
    arm = sandbox_module._SECCOMP_ARCHITECTURES["aarch64"][2]

    assert {
        16, 29, 64, 68, 72, 73, 74, 141, 197, 220, 240, 261,
        302, 306, 319,
    }.issubset(x86)
    assert {
        0, 14, 25, 29, 30, 32, 81, 118, 140, 180, 186, 190,
        194, 261, 267, 279, 420,
    }.issubset(arm)


def test_descriptor_audit_rejects_an_extra_inherited_descriptor(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path / "unexpected", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(SandboxUnavailableError, match="unexpected file descriptor"):
            verify_worker_file_descriptors()
    finally:
        os.close(descriptor)


def test_descriptor_cleanup_closes_an_inherited_capability(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path / "inherited", os.O_CREAT | os.O_RDWR, 0o600)
    code = (
        "import os,sys; "
        "from agent_builder_v2.sandbox import "
        "close_worker_file_descriptors,verify_worker_file_descriptors; "
        "close_worker_file_descriptors(); verify_worker_file_descriptors(); "
        "\ntry: os.fstat(int(sys.argv[1]))\n"
        "except OSError: print('closed', flush=True)\n"
        "else: raise SystemExit('descriptor survived')"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, str(descriptor)],
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(SOURCE_ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            pass_fds=(descriptor,),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(descriptor)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "closed"


def test_real_worker_sandbox_denies_files_network_processes_and_metadata(
    tmp_path: Path,
) -> None:
    run_root, environment_root, source_root, outside_root = _sandbox_tree(tmp_path)
    code = r'''
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import resource
import socket
import sys

from agent_builder_v2.sandbox import (
    apply_worker_resource_limits,
    apply_worker_sandbox,
    apply_worker_umask,
    verify_worker_file_descriptors,
)

run_root, environment_root, source_root, outside_root = map(Path, sys.argv[1:])
apply_worker_umask()
verify_worker_file_descriptors()
apply_worker_resource_limits()
attestation = apply_worker_sandbox(run_root, environment_root, source_root)

results = {}
results["input"] = (run_root / "input" / "allowed.txt").read_text()

def denied(name, operation):
    try:
        operation()
    except OSError as exc:
        results[name] = exc.errno in {errno.EACCES, errno.EPERM}
    else:
        results[name] = False

denied("work_write", lambda: (run_root / "work" / "created.txt").write_text("inside"))
denied("outside_read", lambda: (outside_root / "secret.txt").read_text())
denied("outside_write", lambda: (outside_root / "created.txt").write_text("bad"))
denied("source_write", lambda: (source_root / "readonly.txt").write_text("bad"))
denied("source_chmod", lambda: os.chmod(source_root / "readonly.txt", 0o777))
denied("source_removexattr", lambda: os.removexattr(source_root / "readonly.txt", "user.missing"))

def try_ioctl():
    descriptor = os.open(source_root / "readonly.txt", os.O_RDONLY)
    try:
        fcntl.ioctl(descriptor, 0x40086602, 0)
    finally:
        os.close(descriptor)

denied("ioctl", try_ioctl)
denied("tcp_socket", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
denied("unix_socketpair", socket.socketpair)
denied("fork", os.fork)
denied("exec", lambda: os.execve("/bin/true", ["true"], {}))
denied("signal_parent", lambda: os.kill(os.getppid(), 0))
denied("process_group", lambda: os.setpgid(0, 0))
libc = ctypes.CDLL(None, use_errno=True)

def try_sysv_shm():
    identifier = libc.shmget(0, 4096, 0o1000 | 0o600)
    if identifier < 0:
        raise OSError(ctypes.get_errno(), "shmget denied")
    libc.shmctl(identifier, 0, None)

def try_futimesat():
    if sys.platform == "linux" and platform.machine().lower() in {"x86_64", "amd64"}:
        result = libc.syscall(261, -100, os.fsencode(source_root / "readonly.txt"), None)
        if result != 0:
            raise OSError(ctypes.get_errno(), "futimesat denied")
    else:
        os.utime(source_root / "readonly.txt", None)

denied("sysv_shm", try_sysv_shm)
denied("source_futimesat", try_futimesat)
denied("prctl_mutation", lambda: (
    (_ for _ in ()).throw(OSError(ctypes.get_errno(), "prctl denied"))
    if libc.prctl(4, 1, 0, 0, 0) != 0 else None
))
try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
except (OSError, ValueError):
    results["raise_rlimit"] = True
else:
    results["raise_rlimit"] = False

results["attestation"] = {
    "landlock_abi": attestation.landlock_abi,
    "seccomp_mode": attestation.seccomp_mode,
    "no_new_privileges": attestation.no_new_privileges,
    "network_denied": attestation.tcp_network_denied,
    "process_creation_denied": attestation.process_creation_denied,
    "filesystem_write_denied": attestation.filesystem_write_denied,
    "persistent_ipc_denied": attestation.persistent_ipc_denied,
    "dumpable": attestation.dumpable,
}
print(json.dumps(results, separators=(",", ":")), flush=True)
'''
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SOURCE_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(run_root),
            str(environment_root),
            str(source_root),
            str(outside_root),
        ],
        cwd=run_root / "work",
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["input"] == "allowed"
    for key in (
        "work_write",
        "outside_read",
        "outside_write",
        "source_write",
        "source_chmod",
        "source_removexattr",
        "ioctl",
        "tcp_socket",
        "unix_socketpair",
        "fork",
        "exec",
        "signal_parent",
        "process_group",
        "sysv_shm",
        "source_futimesat",
        "prctl_mutation",
        "raise_rlimit",
    ):
        assert result[key] is True, key
    assert result["attestation"] == {
        "landlock_abi": qualify_host().landlock_abi,
        "seccomp_mode": 2,
        "no_new_privileges": True,
        "network_denied": True,
        "process_creation_denied": True,
        "filesystem_write_denied": True,
        "persistent_ipc_denied": True,
        "dumpable": False,
    }
    assert not (outside_root / "created.txt").exists()
    assert (source_root / "readonly.txt").read_text(encoding="utf-8") == "source"
    assert (source_root / "readonly.txt").stat().st_mode & 0o777 != 0o777


def test_symlinked_rule_root_fails_closed_before_running_code(tmp_path: Path) -> None:
    run_root, environment_root, source_root, _outside_root = _sandbox_tree(tmp_path)
    real_input = run_root / "input"
    moved_input = run_root / "real-input"
    real_input.rename(moved_input)
    real_input.symlink_to(moved_input, target_is_directory=True)
    code = (
        "from pathlib import Path; import sys; "
        "from agent_builder_v2.sandbox import apply_worker_sandbox; "
        "apply_worker_sandbox(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]))"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(run_root),
            str(environment_root),
            str(source_root),
        ],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SOURCE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower() or "not a directory" in completed.stderr.lower()


def test_parent_death_signal_kills_worker_if_supervisor_disappears() -> None:
    worker_code = (
        "import time; "
        "from agent_builder_v2.sandbox import configure_parent_death_signal; "
        "configure_parent_death_signal(); print('ready', flush=True); time.sleep(60)"
    )
    launcher_code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{worker_code!r}], stdout=subprocess.PIPE, text=True); "
        "assert p.stdout is not None and p.stdout.readline().strip() == 'ready'; "
        "print(p.pid, flush=True); time.sleep(60)"
    )
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_code],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SOURCE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        assert launcher.stdout is not None
        child_pid = int(launcher.stdout.readline().strip())
        launcher.kill()
        launcher.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = Path(f"/proc/{child_pid}/stat")
            if not status.exists():
                break
            try:
                raw = status.read_text(encoding="ascii")
            except (FileNotFoundError, ProcessLookupError):
                break
            closing = raw.rfind(")")
            fields = raw[closing + 1 :].split()
            if fields and fields[0] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Worker survived supervisor SIGKILL")
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=3)
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass

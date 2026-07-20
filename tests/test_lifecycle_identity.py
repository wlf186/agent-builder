"""Real-process shutdown ownership tests for the Linux lifecycle boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
AGENT_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "11111111-1111-4111-8111-111111111111"


def _marker(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 1 :].split()
    return f"linux:{int(fields[19])}"


def _status(pid: int) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} remained alive")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write(path: Path, payload: str, mode: int = 0o600) -> None:
    path.write_text(payload, encoding="utf-8")
    path.chmod(mode)


def _worker_record(
    repository: Path,
    pid: int,
    *,
    mode: int = 0o600,
) -> Path:
    run_root = (
        repository / ".runtime" / "agents" / AGENT_ID / "runs" / RUN_ID
    )
    interpreter = (
        repository
        / ".runtime"
        / "agents"
        / AGENT_ID
        / "worker-env"
        / "bin"
        / "python"
    )
    record = run_root / "worker.pid"
    _write(
        record,
        "\n".join(
            (
                "schema=1",
                "role=worker",
                f"pid={pid}",
                f"pgid={pid}",
                f"marker={_marker(pid)}",
                f"root={repository}",
                f"agent_id={AGENT_ID}",
                f"run={RUN_ID}",
                f"run_root={run_root}",
                "module=agent_builder_v2.worker",
                f"interpreter={interpreter}",
                f"cwd={run_root / 'work'}",
                f"command={interpreter} -m agent_builder_v2.worker",
                "",
            )
        ),
        mode,
    )
    return record


_SUPERVISOR = r"""
import os
from pathlib import Path
import signal
import subprocess
import sys

root = Path(os.environ["FAKE_ROOT"])
arguments = sys.argv[1:]
separator = arguments.index("--")
command = arguments[separator + 1:]
child = subprocess.Popen(command, cwd=root, env=os.environ.copy())

def marker(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 1:].split()
    return f"linux:{int(fields[19])}"

record = root / ".runtime" / "control-plane" / "gateway.pid"
payload = "\n".join((
    "schema=1", "role=gateway", f"pid={os.getpid()}",
    f"pgid={os.getpgrp()}", f"marker={marker(os.getpid())}",
    f"root={root}", f"web_pid={child.pid}",
    f"web_marker={marker(child.pid)}", "",
))
temporary = record.with_name(".gateway.pid.tmp")
temporary.write_text(payload, encoding="utf-8")
temporary.chmod(0o600)
os.replace(temporary, record)

def forward(signum, _frame):
    if child.poll() is None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(signum, forward)
raise SystemExit(child.wait())
"""


_WEB = r"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path(os.environ["FAKE_ROOT"])
mode = os.environ["FAKE_MODE"]
ready = Path(os.environ["FAKE_READY"])
signal_log = Path(os.environ["FAKE_SIGNAL_LOG"])
agent_id = "00000000-0000-4000-8000-000000000001"
run_id = "11111111-1111-4111-8111-111111111111"
run_root = root / ".runtime" / "agents" / agent_id / "runs" / run_id
interpreter = root / ".runtime" / "agents" / agent_id / "worker-env" / "bin" / "python"
worker = None
stopping = False

def marker(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 1:].split()
    return f"linux:{int(fields[19])}"

def status(pid):
    values = {}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values

def publish_worker(pid):
    record = run_root / "worker.pid"
    payload = "\n".join((
        "schema=1", "role=worker", f"pid={pid}", f"pgid={pid}",
        f"marker={marker(pid)}", f"root={root}", f"agent_id={agent_id}",
        f"run={run_id}", f"run_root={run_root}",
        "module=agent_builder_v2.worker", f"interpreter={interpreter}",
        f"cwd={run_root / 'work'}",
        f"command={interpreter} -m agent_builder_v2.worker", "",
    ))
    record.write_text(payload, encoding="utf-8")
    record.chmod(int(os.environ.get("FAKE_WORKER_RECORD_MODE", "600"), 8))

if mode != "external":
    environment = os.environ.copy()
    if mode == "seccomp_spoof":
        code = os.environ["FAKE_SECCOMP_CODE"]
        command = [str(interpreter), "-c", code]
    else:
        command = [str(interpreter), "-m", "agent_builder_v2.worker"]
        environment.update({
            "HARNESS_V2_RUN_ROOT": str(run_root),
            "HARNESS_V2_ENVIRONMENT_ROOT": str(interpreter.parents[1]),
            "HARNESS_V2_SOURCE_ROOT": str(root / "src"),
        })
    worker = subprocess.Popen(
        command,
        cwd=run_root / "work",
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if mode == "real":
        assert worker.stdout is not None
        frame = json.loads(worker.stdout.readline())
        assert frame["internal"] == "sandbox.ready"
    else:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                raise RuntimeError("spoof Worker exited")
            values = status(worker.pid)
            if mode != "seccomp_spoof" or (
                values.get("NoNewPrivs") == "1" and values.get("Seccomp") == "2"
            ):
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("spoof Worker did not become ready")
    publish_worker(worker.pid)

ready.write_text(json.dumps({
    "web_pid": os.getpid(),
    "worker_pid": None if worker is None else worker.pid,
}), encoding="utf-8")

def stop(signum, _frame):
    global stopping
    stopping = True
    with signal_log.open("a", encoding="utf-8") as stream:
        stream.write(f"gateway:{signum}\n")
    if mode == "real":
        if worker is not None and worker.stdin is not None:
            worker.stdin.close()
    else:
        os._exit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

if worker is None:
    while not stopping:
        time.sleep(0.05)
else:
    worker.wait()
    if not stopping:
        raise SystemExit(1)
"""


_EXACT_ARGV_SPOOF = r"""
import time
while True:
    time.sleep(1)
"""


_SECCOMP_SPOOF = r"""
import ctypes
import time

class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]
class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort),
                ("filters", ctypes.POINTER(Filter))]

libc = ctypes.CDLL(None, use_errno=True)
rule = Filter(0x06, 0, 0, 0x7fff0000)
program = Program(1, ctypes.pointer(rule))
if libc.prctl(38, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
if libc.prctl(22, 2, ctypes.byref(program)) != 0:
    raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP")
while True:
    time.sleep(1)
"""


def _prepare_repository(tmp_path: Path, mode: str) -> tuple[Path, int]:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / ".venv" / "bin").mkdir(parents=True)
    (repository / ".runtime" / "control-plane").mkdir(parents=True)
    worker_environment = (
        repository / ".runtime" / "agents" / AGENT_ID / "worker-env"
    )
    (worker_environment / "bin").mkdir(parents=True)
    run_root = worker_environment.parent / "runs" / RUN_ID
    for name in ("input", "home", "tmp", "xdg", "work", "output"):
        (run_root / name).mkdir(parents=True, mode=0o700)
        (run_root / name).chmod(0o700)

    (repository / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (worker_environment / "bin" / "python").symlink_to(sys.executable)
    shutil.copy2(ROOT / "env.sh", repository / "env.sh")
    shutil.copy2(
        ROOT / "scripts" / "process_identity.sh",
        repository / "scripts" / "process_identity.sh",
    )
    _write(repository / "scripts" / "log_supervisor.py", _SUPERVISOR, 0o700)
    shutil.copytree(ROOT / "src" / "agent_builder_v2", repository / "src" / "agent_builder_v2")
    _write(repository / "src" / "agent_builder_v2" / "web.py", _WEB, 0o600)
    if mode == "exact_argv_spoof":
        _write(
            repository / "src" / "agent_builder_v2" / "worker.py",
            _EXACT_ARGV_SPOOF,
            0o600,
        )

    port = _free_port()
    stop_source = (ROOT / "stop.sh").read_text(encoding="utf-8")
    old = "export HARNESS_V2_PORT=20815"
    assert stop_source.count(old) == 1
    _write(repository / "stop.sh", stop_source.replace(old, f"export HARNESS_V2_PORT={port}"), 0o700)
    return repository, port


def _launch_gateway(repository: Path, mode: str) -> tuple[subprocess.Popen[bytes], dict[str, int | None]]:
    runtime = repository / ".runtime" / "control-plane"
    ready = runtime / "ready.json"
    signal_log = runtime / "signals.log"
    python = repository / ".venv" / "bin" / "python"
    environment = {
        **os.environ,
        "PYTHONPATH": str(repository / "src"),
        "FAKE_ROOT": str(repository),
        "FAKE_MODE": mode,
        "FAKE_READY": str(ready),
        "FAKE_SIGNAL_LOG": str(signal_log),
        "FAKE_SECCOMP_CODE": _SECCOMP_SPOOF,
    }
    process = subprocess.Popen(
        [
            str(python),
            str(repository / "scripts" / "log_supervisor.py"),
            "--new-session",
            "--clean-env",
            "--runtime-root",
            str(runtime),
            "--log-file",
            str(runtime / "gateway.log"),
            "--pid-file",
            str(runtime / "gateway.pid"),
            "--max-bytes",
            "5242880",
            "--backups",
            "3",
            "--",
            str(python),
            "-m",
            "agent_builder_v2.web",
        ],
        cwd=repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for(ready)
    payload = json.loads(ready.read_text(encoding="utf-8"))
    assert isinstance(payload["web_pid"], int)
    return process, payload


def _run_stop(repository: Path, process: subprocess.Popen[bytes]) -> subprocess.CompletedProcess[str]:
    reaper = threading.Thread(target=process.wait, daemon=True)
    reaper.start()
    result = subprocess.run(
        [str(repository / "stop.sh"), "--force"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
    )
    reaper.join(timeout=5)
    return result


def _spawn_external_real_worker(repository: Path) -> subprocess.Popen[bytes]:
    run_root = (
        repository / ".runtime" / "agents" / AGENT_ID / "runs" / RUN_ID
    )
    interpreter = (
        repository
        / ".runtime"
        / "agents"
        / AGENT_ID
        / "worker-env"
        / "bin"
        / "python"
    )
    process = subprocess.Popen(
        [str(interpreter), "-m", "agent_builder_v2.worker"],
        cwd=run_root / "work",
        env={
            **os.environ,
            "PYTHONPATH": str(repository / "src"),
            "HARNESS_V2_RUN_ROOT": str(run_root),
            "HARNESS_V2_ENVIRONMENT_ROOT": str(interpreter.parents[1]),
            "HARNESS_V2_SOURCE_ROOT": str(repository / "src"),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    frame = json.loads(process.stdout.readline())
    assert frame["internal"] == "sandbox.ready"
    _worker_record(repository, process.pid)
    return process


def _kill_worker(pid: int) -> None:
    if Path(f"/proc/{pid}").exists():
        os.killpg(pid, signal.SIGKILL)
        _wait_gone(pid)


@pytest.mark.skipif(not Path("/proc/self/status").is_file(), reason="Linux procfs required")
def test_stop_reaps_real_nondumpable_worker_before_gateway(tmp_path: Path) -> None:
    repository, port = _prepare_repository(tmp_path, "real")
    supervisor, identities = _launch_gateway(repository, "real")
    web_pid = int(identities["web_pid"])
    worker_pid = int(identities["worker_pid"])
    run_root = repository / ".runtime" / "agents" / AGENT_ID / "runs" / RUN_ID
    worker_status = _status(worker_pid)
    assert worker_status["NoNewPrivs"] == "1"
    assert worker_status["Seccomp"] == "2"
    assert worker_status["PPid"] == str(web_pid)

    result = _run_stop(repository, supervisor)

    assert result.returncode == 0, result.stdout
    worker_message = "stopping 1 validated Worker process(es)"
    gateway_message = "stopping gateway process group"
    assert result.stdout.index(worker_message) < result.stdout.index(gateway_message)
    _wait_gone(worker_pid)
    _wait_gone(web_pid)
    _wait_gone(supervisor.pid)
    assert not run_root.exists()
    assert not (repository / ".runtime" / "control-plane" / "gateway.pid").exists()
    with socket.socket() as client:
        assert client.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("mode", ("exact_argv_spoof", "seccomp_spoof"))
def test_stop_rejects_independent_worker_identity_spoofs(
    tmp_path: Path, mode: str
) -> None:
    repository, _port = _prepare_repository(tmp_path, mode)
    supervisor, identities = _launch_gateway(repository, mode)
    worker_pid = int(identities["worker_pid"])
    values = _status(worker_pid)
    command = Path(f"/proc/{worker_pid}/cmdline").read_bytes().split(b"\0")[:-1]
    if mode == "exact_argv_spoof":
        assert command[-2:] == [b"-m", b"agent_builder_v2.worker"]
        assert values["NoNewPrivs"] == "0"
    else:
        assert command[1] == b"-c"
        assert values["NoNewPrivs"] == "1"
        assert values["Seccomp"] == "2"

    try:
        result = _run_stop(repository, supervisor)
        assert result.returncode == 1
        assert "failed ownership validation; not killing its process" in result.stdout
        assert Path(f"/proc/{worker_pid}").exists()
    finally:
        _kill_worker(worker_pid)


def test_stop_rejects_non_private_worker_pid_record(tmp_path: Path) -> None:
    repository, _port = _prepare_repository(tmp_path, "exact_argv_spoof")
    supervisor, identities = _launch_gateway(repository, "exact_argv_spoof")
    worker_pid = int(identities["worker_pid"])
    record = (
        repository
        / ".runtime"
        / "agents"
        / AGENT_ID
        / "runs"
        / RUN_ID
        / "worker.pid"
    )
    record.chmod(0o644)
    assert stat.S_IMODE(record.stat().st_mode) == 0o644
    try:
        result = _run_stop(repository, supervisor)
        assert result.returncode == 1
        assert "failed ownership validation; not killing its process" in result.stdout
        assert Path(f"/proc/{worker_pid}").exists()
    finally:
        _kill_worker(worker_pid)


def test_stop_rejects_non_private_gateway_pid_record_without_signalling(
    tmp_path: Path,
) -> None:
    repository, _port = _prepare_repository(tmp_path, "exact_argv_spoof")
    supervisor, identities = _launch_gateway(repository, "exact_argv_spoof")
    web_pid = int(identities["web_pid"])
    worker_pid = int(identities["worker_pid"])
    gateway_record = repository / ".runtime" / "control-plane" / "gateway.pid"
    gateway_record.chmod(0o644)
    assert stat.S_IMODE(gateway_record.stat().st_mode) == 0o644
    try:
        result = subprocess.run(
            [str(repository / "stop.sh"), "--force"],
            cwd=repository,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        assert result.returncode == 1
        assert "refusing unsafe gateway PID record" in result.stdout
        assert supervisor.poll() is None
        assert Path(f"/proc/{web_pid}").exists()
        assert Path(f"/proc/{worker_pid}").exists()
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=5)
        _kill_worker(worker_pid)


def test_nondumpable_fallback_rejects_worker_outside_verified_web_parent(
    tmp_path: Path,
) -> None:
    repository, _port = _prepare_repository(tmp_path, "external")
    supervisor, identities = _launch_gateway(repository, "external")
    web_pid = int(identities["web_pid"])
    worker = _spawn_external_real_worker(repository)
    values = _status(worker.pid)
    assert values["NoNewPrivs"] == "1"
    assert values["Seccomp"] == "2"
    assert values["PPid"] == str(os.getpid())
    assert values["PPid"] != str(web_pid)

    try:
        result = _run_stop(repository, supervisor)
        assert result.returncode == 1
        assert "failed ownership validation; not killing its process" in result.stdout
        assert worker.poll() is None
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)


def test_private_pid_record_rejects_mode_hardlink_empty_and_oversize(
    tmp_path: Path,
) -> None:
    helper = ROOT / "scripts" / "process_identity.sh"
    record = tmp_path / "worker.pid"
    _write(record, "schema=1\n", 0o600)

    def accepted() -> bool:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; agent_builder_private_pid_record "$2" 4096',
                "bash",
                str(helper),
                str(record),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    assert accepted()
    record.chmod(0o640)
    assert not accepted()
    record.chmod(0o600)
    alias = tmp_path / "alias.pid"
    os.link(record, alias)
    assert not accepted()
    alias.unlink()
    _write(record, "", 0o600)
    assert not accepted()
    _write(record, "x" * 4097, 0o600)
    assert not accepted()

"""Identity publication checks for the V2 gateway supervisor."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "log_supervisor.py"
SPEC = importlib.util.spec_from_file_location("agent_builder_log_supervisor", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
log_supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(log_supervisor)


def _cpu_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split()
    return int(fields[11]) + int(fields[12])


def test_capture_wait_timeout_blocks_when_idle() -> None:
    interval = log_supervisor._CAPTURE_FLUSH_INTERVAL_SECONDS

    assert log_supervisor._capture_wait_timeout(
        has_pending=False,
        now=100.0,
        last_flush=1.0,
    ) == interval
    assert log_supervisor._capture_wait_timeout(
        has_pending=True,
        now=1.25,
        last_flush=1.0,
    ) == pytest.approx(interval - 0.25)
    assert log_supervisor._capture_wait_timeout(
        has_pending=True,
        now=10.0,
        last_flush=1.0,
    ) == 0.0


def test_clean_environment_preserves_only_the_operator_summary_gate() -> None:
    source = {
        "PATH": "/bin",
        "HARNESS_V2_SEMANTIC_SUMMARY_V2": "1",
        "AGENT_BUILDER_API_TOKEN": "must-not-cross",
        "UNRELATED_SECRET": "must-not-cross",
    }

    assert log_supervisor._sanitised_environment(source) == {
        "PATH": "/bin",
        "HARNESS_V2_SEMANTIC_SUMMARY_V2": "1",
    }


def test_supervisor_publishes_complete_private_identity_before_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(parents=True)
    pid_file = runtime / "gateway.pid"
    monkeypatch.setenv("AGENT_BUILDER_ROOT", str(repository))
    checkout, managed_path = log_supervisor._managed_pid_path(
        str(runtime), str(pid_file)
    )
    monkeypatch.setattr(log_supervisor.os, "getpid", lambda: 12345)
    monkeypatch.setattr(log_supervisor.os, "getpgrp", lambda: 12345)
    monkeypatch.setattr(
        log_supervisor, "_process_marker", lambda _pid: "linux:67890"
    )

    log_supervisor._publish_gateway_pid_record(managed_path, checkout)

    assert stat.S_IMODE(pid_file.stat().st_mode) == 0o600
    assert pid_file.read_text(encoding="utf-8").splitlines() == [
        "schema=1",
        "role=gateway",
        "pid=12345",
        "pgid=12345",
        "marker=linux:67890",
        f"root={repository}",
    ]
    assert list(runtime.glob(".gateway.pid.*.tmp")) == []


def test_supervisor_removes_only_its_exact_incomplete_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(parents=True)
    pid_file = runtime / "gateway.pid"
    monkeypatch.setenv("AGENT_BUILDER_ROOT", str(repository))
    checkout, managed_path = log_supervisor._managed_pid_path(
        str(runtime), str(pid_file)
    )
    monkeypatch.setattr(log_supervisor.os, "getpid", lambda: 12345)
    monkeypatch.setattr(log_supervisor.os, "getpgrp", lambda: 12345)
    monkeypatch.setattr(
        log_supervisor, "_process_marker", lambda _pid: "linux:67890"
    )
    log_supervisor._publish_gateway_pid_record(managed_path, checkout)

    assert log_supervisor._remove_initial_pid_record_if_owned(
        managed_path, checkout
    ) is True
    assert not pid_file.exists()

    log_supervisor._publish_gateway_pid_record(managed_path, checkout)
    pid_file.write_text("changed\n", encoding="utf-8")
    os.chmod(pid_file, 0o600)
    assert log_supervisor._remove_initial_pid_record_if_owned(
        managed_path, checkout
    ) is False
    assert pid_file.read_text(encoding="utf-8") == "changed\n"


def test_supervisor_refuses_existing_or_linked_pid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_text("keep\n", encoding="utf-8")
    pid_file = runtime / "gateway.pid"
    pid_file.symlink_to(target)
    monkeypatch.setenv("AGENT_BUILDER_ROOT", str(repository))

    with pytest.raises(ValueError):
        log_supervisor._managed_pid_path(str(runtime), str(pid_file))

    assert target.read_text(encoding="utf-8") == "keep\n"


def test_supervisor_record_precedes_child_and_term_reaps_both(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(parents=True)
    pid_file = runtime / "gateway.pid"
    log_file = runtime / "gateway.log"
    child_pid_file = runtime / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"assert Path({str(pid_file)!r}).is_file(); "
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--new-session",
            "--clean-env",
            "--runtime-root",
            str(runtime),
            "--log-file",
            str(log_file),
            "--pid-file",
            str(pid_file),
            "--",
            sys.executable,
            "-c",
            child_code,
        ],
        cwd=repository,
        env={
            **os.environ,
            "AGENT_BUILDER_ROOT": str(repository),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (
                pid_file.is_file()
                and child_pid_file.is_file()
                and "web_pid=" in pid_file.read_text(encoding="utf-8")
            ):
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
        assert child_pid is not None
        pid_record = pid_file.read_text(encoding="utf-8")
        assert f"pid={process.pid}" in pid_record
        assert f"web_pid={child_pid}" in pid_record
        assert "web_marker=linux:" in pid_record

        ticks_before = _cpu_ticks(process.pid)
        time.sleep(1.0)
        ticks_after = _cpu_ticks(process.pid)
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        assert ticks_after - ticks_before < max(5, ticks_per_second // 5)

        process.terminate()
        process.wait(timeout=5)

        assert not Path(f"/proc/{child_pid}").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)

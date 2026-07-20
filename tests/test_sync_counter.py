"""Qualification-only libc sync counter safety and coverage tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_builder_v2.capsule import AgentCapsule
from agent_builder_v2.control import RunService
from agent_builder_v2.sync_counter import (
    SYNC_COUNTER_ENABLE_ENV,
    SYNC_COUNTER_FILE_ENV,
    SYNC_COUNTER_REQUIRED_ENV,
    SYNC_COUNTER_ROLE_ENV,
    SYNC_COUNTER_SELFTEST_ENV,
    SyncCounterError,
    qualification_environment,
    read_sync_counter,
    sync_counter_paths,
    validate_sync_counter_artifacts,
)
from scripts.sync_counter_tool import prepare_sync_counter


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
SUPERVISOR = ROOT / "scripts" / "log_supervisor.py"


def _enabled_environment(repository: Path, role: str) -> dict[str, str]:
    paths = validate_sync_counter_artifacts(repository)
    return {
        "LD_PRELOAD": str(paths.library),
        SYNC_COUNTER_ENABLE_ENV: "1",
        SYNC_COUNTER_FILE_ENV: str(paths.counter),
        SYNC_COUNTER_ROLE_ENV: role,
        SYNC_COUNTER_REQUIRED_ENV: "1",
    }


def test_prepare_selftests_then_leaves_one_empty_fixed_page(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)

    prepare_sync_counter(repository)

    paths = validate_sync_counter_artifacts(repository)
    assert paths.library.is_relative_to(repository / ".runtime")
    assert paths.counter.stat().st_size == 4096
    assert paths.counter.stat().st_nlink == 1
    assert read_sync_counter(repository) == {
        "schema": 1,
        "counter_abi": "libc-sync-calls-v1",
        "generation": read_sync_counter(repository)["generation"],
        "ready_slots": 0,
        "registration_failures": 0,
        "slot_overflow": 0,
        "complete": True,
        "roles": {},
        "total": {
            operation: {"attempts": 0, "successes": 0, "failures": 0}
            for operation in (
                "fsync",
                "fdatasync",
                "msync",
                "syncfs",
                "sync_file_range",
                "sync",
            )
        },
    }
    build = json.loads(paths.build_record.read_text(encoding="utf-8"))
    assert build["counter_abi"] == "libc-sync-calls-v1"
    assert len(build["source_sha256"]) == len(build["library_sha256"]) == 64


def test_fixed_environment_is_fail_closed_and_can_narrow_child_role(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    prepare_sync_counter(repository)
    source = _enabled_environment(repository, "supervisor")

    child = qualification_environment(
        repository,
        source,
        expected_role="supervisor",
        child_role="gateway",
    )
    assert child[SYNC_COUNTER_ROLE_ENV] == "gateway"
    assert child["LD_PRELOAD"] == str(sync_counter_paths(repository).library)

    with pytest.raises(SyncCounterError, match="fixed artifacts"):
        qualification_environment(
            repository,
            {**source, SYNC_COUNTER_FILE_ENV: str(tmp_path / "outside")},
            expected_role="supervisor",
        )
    with pytest.raises(SyncCounterError, match="partial"):
        qualification_environment(
            repository,
            {SYNC_COUNTER_FILE_ENV: str(sync_counter_paths(repository).counter)},
            expected_role="supervisor",
        )
    with pytest.raises(SyncCounterError, match="enablement"):
        qualification_environment(
            repository,
            {**source, SYNC_COUNTER_SELFTEST_ENV: "1"},
            expected_role="supervisor",
        )


def test_counter_rejects_linked_artifact(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    prepare_sync_counter(repository)
    paths = sync_counter_paths(repository)
    outside = tmp_path / "outside"
    outside.write_bytes(paths.counter.read_bytes())
    paths.counter.unlink()
    paths.counter.symlink_to(outside)

    with pytest.raises(SyncCounterError):
        validate_sync_counter_artifacts(repository)


def test_supervisor_and_gateway_images_share_low_write_counter_page(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    prepare_sync_counter(repository)
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(mode=0o700)
    pid_file = runtime / "gateway.pid"
    log_file = runtime / "gateway.log"
    child_code = (
        "import ctypes,os;"
        "libc=ctypes.CDLL(None,use_errno=True);"
        "make=libc.memfd_create;"
        "make.argtypes=[ctypes.c_char_p,ctypes.c_uint];"
        "make.restype=ctypes.c_int;"
        "fd=make(b'ab-sync-gateway-test',1);"
        "assert fd>=0;os.ftruncate(fd,4096);os.fsync(fd);os.close(fd);"
        "__import__('time').sleep(0.1)"
    )
    environment = {
        **os.environ,
        **_enabled_environment(repository, "supervisor"),
        "AGENT_BUILDER_ROOT": str(repository),
        "AGENT_BUILDER_RUNTIME_DIR": str(repository / ".runtime"),
        "PYTHONPATH": str(SOURCE_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--new-session",
            "--clean-env",
            "--qualification-sync-counter",
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
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    assert "sync_counter=libc-sync-calls-v1" in pid_file.read_text(encoding="utf-8")

    snapshot = read_sync_counter(repository)
    assert snapshot["complete"] is True
    assert snapshot["ready_slots"] == 2
    roles = snapshot["roles"]
    assert set(roles) == {"supervisor", "gateway"}
    assert roles["supervisor"]["process_images"] == 1
    assert roles["supervisor"]["operations"]["fsync"] == {
        "attempts": 4,
        "successes": 4,
        "failures": 0,
    }
    assert roles["gateway"]["operations"]["fsync"] == {
        "attempts": 1,
        "successes": 1,
        "failures": 0,
    }


def test_run_service_propagates_only_validated_fixed_worker_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    prepare_sync_counter(repository)
    enabled = _enabled_environment(repository, "gateway")
    for name in (
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        SYNC_COUNTER_ENABLE_ENV,
        SYNC_COUNTER_FILE_ENV,
        SYNC_COUNTER_ROLE_ENV,
        SYNC_COUNTER_REQUIRED_ENV,
        SYNC_COUNTER_SELFTEST_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in enabled.items():
        monkeypatch.setenv(name, value)

    service = RunService(repository, SOURCE_ROOT)
    capsule = AgentCapsule(
        agent_id="0" * 32,
        data_root=repository / "data" / "agents" / ("0" * 32),
        runtime_root=repository / ".runtime" / "agents" / ("0" * 32),
        interpreter=repository
        / ".runtime"
        / "agents"
        / ("0" * 32)
        / "worker-env"
        / "bin"
        / "python",
    )
    worker = service._worker_environment(
        capsule.runtime_root / "runs" / ("1" * 32), capsule
    )

    assert worker[SYNC_COUNTER_ROLE_ENV] == "worker"
    assert worker["LD_PRELOAD"] == enabled["LD_PRELOAD"]
    assert worker[SYNC_COUNTER_FILE_ENV] == enabled[SYNC_COUNTER_FILE_ENV]
    assert SYNC_COUNTER_SELFTEST_ENV not in worker

    process = subprocess.run(
        [sys.executable, "-c", "pass"],
        cwd=repository,
        env=worker,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    snapshot = read_sync_counter(repository)
    assert snapshot["roles"]["worker"]["process_images"] == 1


def test_delta_rejects_counter_reset_and_reports_all_operations(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    prepare_sync_counter(repository)
    before = read_sync_counter(repository)
    enabled = _enabled_environment(repository, "worker")
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import ctypes;"
                "libc=ctypes.CDLL(None,use_errno=True);"
                "libc.fsync.argtypes=[ctypes.c_int];"
                "libc.fsync.restype=ctypes.c_int;"
                "raise SystemExit(0 if libc.fsync(-1)==-1 else 1)"
            ),
        ],
        cwd=repository,
        env={**os.environ, **enabled, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    assert process.returncode == 0
    after = read_sync_counter(repository)

    from agent_builder_v2.sync_counter import sync_counter_delta

    delta = sync_counter_delta(before, after)
    assert delta["operations"]["fsync"] == {
        "attempts": 1,
        "successes": 0,
        "failures": 1,
    }
    assert set(delta["operations"]) == {
        "fsync",
        "fdatasync",
        "msync",
        "syncfs",
        "sync_file_range",
        "sync",
    }
    reset = {**after, "generation": int(after["generation"]) + 1}
    with pytest.raises(SyncCounterError, match="changed"):
        sync_counter_delta(before, reset)

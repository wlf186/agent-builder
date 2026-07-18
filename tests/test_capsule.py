"""Containment checks for the prototype Agent Capsule paths."""

from __future__ import annotations

import shutil
import os
from pathlib import Path

import pytest

from agent_builder_v2.capsule import (
    PROTOTYPE_AGENT_ID,
    CapsuleManager,
)


def test_agent_directory_symlink_cannot_redirect_writes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    agents = repository / "data" / "agents"
    agents.mkdir(parents=True)
    (agents / PROTOTYPE_AGENT_ID).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Capsule directory is unsafe"):
        CapsuleManager(repository).ensure_prototype_agent()

    assert list(outside.iterdir()) == []


def test_manifest_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    outside = tmp_path / "outside-manifest"
    repository.mkdir()
    outside.write_text("keep me\n", encoding="utf-8")
    data_root = (
        repository
        / "data"
        / "agents"
        / PROTOTYPE_AGENT_ID
    )
    data_root.mkdir(parents=True)
    (data_root / "manifest.json").symlink_to(outside)

    with pytest.raises(OSError):
        CapsuleManager(repository).ensure_prototype_agent()

    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_run_and_environment_symlinks_fail_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    manager = CapsuleManager(repository)
    capsule = manager.ensure_prototype_agent()

    run_id = "1" * 32
    run_root = capsule.runtime_root / "runs" / run_id
    run_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError, match="Run root already exists"):
        manager.create_run_root(capsule, run_id)
    run_root.unlink()

    environment = capsule.runtime_root / "worker-env"
    shutil.rmtree(environment)
    environment.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="Capsule directory is unsafe"):
        manager.ensure_prototype_agent()

    assert list(outside.iterdir()) == []


def test_startup_recovery_fails_closed_on_invalid_worker_identity(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    manager = CapsuleManager(repository)
    capsule = manager.ensure_prototype_agent()
    orphan_id = "2" * 32
    retained_id = "3" * 32
    orphan = manager.create_run_root(capsule, orphan_id)
    retained = manager.create_run_root(capsule, retained_id)
    (retained / "worker.pid").write_text("managed later\n", encoding="utf-8")
    os.chmod(retained / "worker.pid", 0o600)

    with pytest.raises(RuntimeError, match="unsafe Worker PID record"):
        manager.cleanup_orphan_run_roots(capsule)

    assert retained.is_dir()


def test_startup_recovery_removes_complete_stale_worker_record(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    manager = CapsuleManager(repository)
    capsule = manager.ensure_prototype_agent()
    run_id = "4" * 32
    run_root = manager.create_run_root(capsule, run_id)
    pid = 2_000_000_000
    interpreter = str(capsule.interpreter)
    values = {
        "schema": "1",
        "role": "worker",
        "pid": str(pid),
        "pgid": str(pid),
        "marker": "linux:1",
        "root": str(repository),
        "agent_id": capsule.agent_id,
        "run": run_id,
        "run_root": str(run_root),
        "module": "agent_builder_v2.worker",
        "interpreter": interpreter,
        "cwd": str(run_root / "work"),
        "command": f"{interpreter} -m agent_builder_v2.worker",
    }
    pid_file = run_root / "worker.pid"
    pid_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    pid_file.chmod(0o600)

    removed = manager.cleanup_orphan_run_roots(capsule)

    assert removed == 1
    assert not run_root.exists()

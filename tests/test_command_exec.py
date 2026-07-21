"""Kernel-boundary tests for the allowlisted singleton command runner."""

from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

import pytest

import agent_builder_v2.command_exec as command_exec
from agent_builder_v2.capsule import CapsuleManager
from agent_builder_v2.command_exec import CommandExecutionError, CommandExecutor
from agent_builder_v2.permissions import CapabilityRequest
from agent_builder_v2.tools import runtime_effective_toolset


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _execution() -> tuple[CapsuleManager, object, str, Path, object, CapabilityRequest]:
    manager = CapsuleManager(REPOSITORY_ROOT)
    capsule = manager.ensure_prototype_agent()
    run_id = uuid.uuid4().hex
    run_root = manager.create_run_root(capsule, run_id)
    catalog = CommandExecutor(REPOSITORY_ROOT, REPOSITORY_ROOT / "src", capsule)
    prepared, preview, executor = catalog.prepare(
        {"command_id": "runtime-compile"}, run_root
    )
    now = int(time.time() * 1000)
    request = CapabilityRequest.create(
        agent_id=capsule.agent_id,
        capsule_generation=capsule.generation,
        conversation_id="1" * 32,
        run_id=run_id,
        call_id="exec-call",
        capability_id="exec/run",
        toolset_digest=runtime_effective_toolset().toolset_digest,
        policy_digest="2" * 64,
        arguments=prepared,
        preview=preview,
        expires_at_milliseconds=now + 30_000,
        now_milliseconds=now,
    )
    return manager, capsule, run_id, run_root, executor, request


def test_runtime_compile_uses_pidfd_singleton_sandbox_and_cleans_record() -> None:
    manager, capsule, run_id, run_root, executor, request = _execution()
    try:
        result = json.loads(executor.execute(request, lambda: False))
        payload = json.loads(result["stdout"])
        assert result["exit_code"] == 0
        assert result["stderr"] == ""
        assert result["sandbox"] == "singleton-landlock-seccomp-v1"
        assert payload["outcome"] == "completed"
        assert payload["fork_denied"] is True
        assert payload["network_denied"] is True
        assert payload["environment_clean"] is True
        assert payload["exec_denied"] is True
        assert 1 <= payload["source_files"] == payload["output_files"] <= 256
        assert payload["source_bytes"] <= 2 * 1024 * 1024
        assert payload["allocated_output_bytes"] <= 8 * 1024 * 1024
        assert not list((run_root / "output").iterdir())
        assert not list(run_root.glob("runner-*.pid"))
    finally:
        manager.remove_run_root(capsule, run_id)


def test_command_allowlist_and_prepared_snapshot_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, capsule, run_id, run_root, executor, request = _execution()
    try:
        catalog = CommandExecutor(REPOSITORY_ROOT, REPOSITORY_ROOT / "src", capsule)
        with pytest.raises(CommandExecutionError, match="allowlist"):
            catalog.prepare({"command_id": "../../bin/sh"}, run_root)
        original = command_exec._source_digest

        def changed(path: Path) -> tuple[str, int, int]:
            digest, files, size = original(path)
            return "f" * 64, files, size

        monkeypatch.setattr(command_exec, "_source_digest", changed)
        with pytest.raises(CommandExecutionError, match="source changed"):
            executor.execute(request, lambda: False)
        assert not list(run_root.glob("runner-*.pid"))
        assert not list((run_root / "output").iterdir())
    finally:
        manager.remove_run_root(capsule, run_id)


def test_cancellation_before_release_never_runs_the_command() -> None:
    manager, capsule, run_id, run_root, executor, request = _execution()
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    try:
        with pytest.raises(CommandExecutionError, match="cancelled before dispatch"):
            executor.execute(request, cancelled)
        assert not list((run_root / "output").iterdir())
        assert not list(run_root.glob("runner-*.pid"))
    finally:
        manager.remove_run_root(capsule, run_id)


def test_wall_timeout_kills_exact_pid_and_removes_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, capsule, run_id, run_root, executor, request = _execution()
    monkeypatch.setattr(command_exec, "COMMAND_WALL_TIMEOUT_SECONDS", 0.0)
    try:
        result = json.loads(executor.execute(request, lambda: False))
        assert result["timed_out"] is True
        assert result["exit_code"] < 0
        assert not list((run_root / "output").iterdir())
        assert not list(run_root.glob("runner-*.pid"))
    finally:
        manager.remove_run_root(capsule, run_id)


def test_output_flood_limit_kills_runner_and_cleans_every_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, capsule, run_id, run_root, executor, request = _execution()
    monkeypatch.setattr(command_exec, "MAX_COMMAND_OUTPUT_BYTES", 1)
    try:
        with pytest.raises(CommandExecutionError, match="output exceeded"):
            executor.execute(request, lambda: False)
        assert not list((run_root / "output").iterdir())
        assert not list(run_root.glob("runner-*.pid"))
    finally:
        manager.remove_run_root(capsule, run_id)

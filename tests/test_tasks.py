"""Durability, cancellation and isolation tests for background Tasks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
import uuid

import pytest

import agent_builder_v2.tasks as task_module
from agent_builder_v2.capsule import CapsuleManager
from agent_builder_v2.command_exec import CommandExecutionError, CommandExecutor
from agent_builder_v2.tasks import (
    BackgroundTaskManager,
    TaskError,
    TaskParentIdentity,
    TaskStore,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AGENT_ID = "00000000-0000-4000-8000-000000000001"


def _parent() -> TaskParentIdentity:
    return TaskParentIdentity(
        agent_id=AGENT_ID,
        conversation_id=uuid.uuid4().hex,
        turn_id=uuid.uuid4().hex,
        run_id=uuid.uuid4().hex,
    )


def _create(store: TaskStore, parent: TaskParentIdentity | None = None):
    return store.create(
        task_id=uuid.uuid4().hex,
        capsule_generation=1,
        parent=parent or _parent(),
        command_id="runtime-compile",
        executor_identity_digest="a" * 64,
        request_digest="b" * 64,
    )


def test_task_store_persists_semantic_transitions_and_bounded_notifications(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    store = TaskStore(database, AGENT_ID)
    parent = _parent()
    queued = _create(store, parent)
    running = store.mark_running(queued.task_id)
    completed = store.finish(
        queued.task_id,
        "completed",
        result={"exit_code": 0, "stdout": "ok"},
    )
    assert queued.state == "queued"
    assert running.state == "running"
    assert completed.state == "completed"
    assert completed.result_digest is not None
    assert completed.output_bytes > 0
    assert [item.kind for item in store.notifications(queued.task_id)] == [
        "task.queued",
        "task.running",
        "task.completed",
    ]
    store.close()

    reopened = TaskStore(database, AGENT_ID)
    assert reopened.get(queued.task_id) == completed
    assert reopened.delete_conversation(parent.conversation_id) == 1
    with pytest.raises(KeyError, match="not found"):
        reopened.get(queued.task_id)
    reopened.close()


def test_task_store_recovers_incomplete_and_enforces_active_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TaskStore(tmp_path / "state.sqlite", AGENT_ID)
    monkeypatch.setattr(task_module, "MAX_ACTIVE_TASKS", 2)
    first = _create(store)
    second = _create(store)
    store.mark_running(first.task_id)
    with pytest.raises(TaskError, match="concurrency"):
        _create(store)
    assert store.recover_incomplete() == 2
    assert store.get(first.task_id).state == "interrupted"
    assert store.get(second.task_id).state == "interrupted"
    assert all(
        record.notification_count in {2, 3} for record in store.list()
    )
    store.close()


@pytest.mark.asyncio
async def test_real_background_task_runs_in_its_own_root_and_leaves_no_residue(
    tmp_path: Path,
) -> None:
    capsules = CapsuleManager(REPOSITORY_ROOT)
    capsule = capsules.ensure_prototype_agent()
    store = TaskStore(tmp_path / "tasks.sqlite", capsule.agent_id)
    commands = CommandExecutor(REPOSITORY_ROOT, REPOSITORY_ROOT / "src", capsule)
    manager = BackgroundTaskManager(capsule, capsules, commands, store)
    await manager.initialize()
    record = await manager.submit(_parent())
    task_root = capsule.runtime_root / "tasks" / record.task_id
    for _ in range(400):
        current = store.get(record.task_id)
        if current.state in {"completed", "failed", "cancelled", "interrupted"}:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("background Task did not terminate")
    assert current.state == "completed"
    assert current.result_json is not None
    result = json.loads(current.result_json)
    payload = json.loads(result["stdout"])
    assert result["exit_code"] == 0
    assert payload["fork_denied"] is True
    assert payload["network_denied"] is True
    assert payload["exec_denied"] is True
    assert not task_root.exists()
    await manager.close()


@pytest.mark.asyncio
async def test_bounded_bash_uses_the_same_background_task_boundary(
    tmp_path: Path,
) -> None:
    capsules = CapsuleManager(REPOSITORY_ROOT)
    capsule = capsules.ensure_prototype_agent()
    store = TaskStore(tmp_path / "bash-tasks.sqlite", capsule.agent_id)
    commands = CommandExecutor(REPOSITORY_ROOT, REPOSITORY_ROOT / "src", capsule)
    manager = BackgroundTaskManager(capsule, capsules, commands, store)
    await manager.initialize()
    record = await manager.submit(
        _parent(),
        {"command_id": "bounded-bash", "script": "printf '%s' TASK-BASH-OK"},
    )
    for _ in range(400):
        current = store.get(record.task_id)
        if current.state in {"completed", "failed", "cancelled", "interrupted"}:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("bounded Bash Task did not terminate")
    assert current.state == "completed"
    assert current.command_id == "bounded-bash"
    assert json.loads(current.result_json or "{}")["stdout"] == "TASK-BASH-OK"
    assert not (capsule.runtime_root / "tasks" / record.task_id).exists()
    await manager.close()


class _BlockingExecutor:
    identity_digest = "c" * 64

    def execute_prepared(self, cancelled) -> str:
        deadline = time.monotonic() + 5
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.005)
        raise CommandExecutionError("cancelled")


class _FakeCommands:
    def prepare(self, arguments, task_root):
        assert arguments == {"command_id": "runtime-compile"}
        assert task_root.name
        return {"command_id": "runtime-compile"}, "preview", _BlockingExecutor()


@pytest.mark.asyncio
async def test_cancel_and_restart_recovery_remove_exact_task_roots(
    tmp_path: Path,
) -> None:
    capsules = CapsuleManager(REPOSITORY_ROOT)
    capsule = capsules.ensure_prototype_agent()
    store = TaskStore(tmp_path / "tasks.sqlite", capsule.agent_id)
    manager = BackgroundTaskManager(
        capsule, capsules, _FakeCommands(), store  # type: ignore[arg-type]
    )
    await manager.initialize()
    record = await manager.submit(_parent())
    for _ in range(100):
        if store.get(record.task_id).state == "running":
            break
        await asyncio.sleep(0.005)
    cancelled = await manager.cancel(record.task_id)
    assert cancelled.state == "cancelled"
    assert not (capsule.runtime_root / "tasks" / record.task_id).exists()
    await manager.close()

    recovery_store = TaskStore(tmp_path / "recovery.sqlite", capsule.agent_id)
    orphan = _create(recovery_store)
    orphan_root = capsules.create_task_root(capsule, orphan.task_id)
    recovery_store.mark_running(orphan.task_id)
    recovery_store.close()
    reopened = TaskStore(tmp_path / "recovery.sqlite", capsule.agent_id)
    recovery = BackgroundTaskManager(
        capsule, capsules, _FakeCommands(), reopened  # type: ignore[arg-type]
    )
    assert await recovery.initialize() == 1
    assert reopened.get(orphan.task_id).state == "interrupted"
    assert not orphan_root.exists()
    await recovery.close()

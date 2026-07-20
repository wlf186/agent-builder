"""Persistent Agent registry, generation and residual-cleanup tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from agent_builder_v2.agents import AgentRegistry
from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID


class _Hooks:
    def __init__(self) -> None:
        self.drained: list[str] = []
        self.retired: list[str] = []
        self.references: dict[str, tuple[str, ...]] = {}

    def drain(self, agent_id: str) -> None:
        self.drained.append(agent_id)

    def retire(self, agent_id: str) -> None:
        self.retired.append(agent_id)

    def residual_references(self, agent_id: str) -> tuple[str, ...]:
        return self.references.get(agent_id, ())


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_create_upgrade_delete_isolated_capsules_and_invalidates_generation(
    tmp_path: Path,
) -> None:
    hooks = _Hooks()
    registry = AgentRegistry(tmp_path, hooks=hooks)
    try:
        registry.initialize()
        assert registry.get(PROTOTYPE_AGENT_ID).state == "active"
        first = registry.create("First Agent")
        second = registry.create("Second Agent")
        first_capsule = registry.capsules.load_agent(first.agent_id)
        second_capsule = registry.capsules.load_agent(second.agent_id)
        first_capsule.data_root.joinpath("workspace", "marker.txt").write_text("first")
        second_capsule.data_root.joinpath("workspace", "marker.txt").write_text("second")
        second_before = _tree_digest(second_capsule.data_root)
        old_interpreter = first_capsule.interpreter

        upgraded = registry.upgrade(first.agent_id, display_name="First Agent v2")

        assert upgraded.generation == 2
        assert upgraded.display_name == "First Agent v2"
        current = registry.capsules.load_agent(first.agent_id)
        assert current.generation == 2
        assert current.interpreter.is_file()
        assert not old_interpreter.exists()
        assert _tree_digest(second_capsule.data_root) == second_before

        registry.delete(first.agent_id)

        with pytest.raises(KeyError):
            registry.get(first.agent_id)
        assert not first_capsule.data_root.exists()
        assert not first_capsule.runtime_root.exists()
        assert registry.get(second.agent_id).state == "active"
        assert _tree_digest(second_capsule.data_root) == second_before
        assert first.agent_id in hooks.retired
    finally:
        registry.close()


def test_upgrade_and_delete_recover_from_durable_intermediate_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    created = registry.create("Recoverable")
    original_promote = registry.capsules.promote_generation

    def fail_once(*args: object, **kwargs: object) -> object:
        monkeypatch.setattr(registry.capsules, "promote_generation", original_promote)
        raise RuntimeError("simulated promotion crash")

    monkeypatch.setattr(registry.capsules, "promote_generation", fail_once)
    with pytest.raises(RuntimeError, match="promotion crash"):
        registry.upgrade(created.agent_id)
    assert registry.get(created.agent_id).state == "upgrading"
    registry.close()

    recovered = AgentRegistry(tmp_path)
    try:
        recovered.initialize()
        assert recovered.get(created.agent_id).generation == 2
        record = recovered.get(created.agent_id)
        recovered._set_state_locked(  # fault point: deleting committed
            created.agent_id,
            state="deleting",
            generation=record.generation,
            target_generation=None,
            display_name=record.display_name,
        )
        recovered.capsules.delete_agent(
            recovered.capsules.load_agent(created.agent_id)
        )
    finally:
        recovered.close()

    after_delete_crash = AgentRegistry(tmp_path)
    try:
        after_delete_crash.initialize()
        with pytest.raises(KeyError):
            after_delete_crash.get(created.agent_id)
    finally:
        after_delete_crash.close()


def test_lifecycle_fails_closed_while_runtime_references_remain(tmp_path: Path) -> None:
    hooks = _Hooks()
    registry = AgentRegistry(tmp_path, hooks=hooks)
    try:
        registry.initialize()
        created = registry.create("Busy Agent")
        hooks.references[created.agent_id] = ("run:still-active",)
        with pytest.raises(RuntimeError, match="runtime references"):
            registry.upgrade(created.agent_id)
        with pytest.raises(RuntimeError, match="residual references"):
            registry.delete(created.agent_id)
        assert registry.get(created.agent_id).state == "deleting"
        assert registry.capsules.load_agent(created.agent_id).data_root.exists()
    finally:
        registry.close()


def test_provisioning_recovers_after_capsule_creation_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = AgentRegistry(tmp_path)
    original = registry.capsules.ensure_agent
    failed = False

    def interrupt_once(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated provisioning interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(registry.capsules, "ensure_agent", interrupt_once)
    with pytest.raises(OSError, match="provisioning interruption"):
        registry.create("Interrupted create")
    record = registry.list()[0]
    assert record.state == "provisioning"
    agent_id = record.agent_id
    registry.close()
    monkeypatch.undo()

    recovered = AgentRegistry(tmp_path)
    try:
        recovered.initialize()
        assert recovered.get(agent_id).state == "active"
        assert recovered.capsules.load_agent(agent_id).interpreter.is_file()
    finally:
        recovered.close()


@pytest.mark.parametrize("failure_boundary", ["retire", "active_commit"])
def test_upgrade_recovers_before_or_after_old_generation_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    created = registry.create("Interrupted upgrade")
    old_interpreter = registry.capsules.load_agent(created.agent_id).interpreter
    if failure_boundary == "retire":
        original = registry.capsules.retire_generation

        def fail_retire_once(*args: object, **kwargs: object) -> object:
            monkeypatch.setattr(
                registry.capsules, "retire_generation", original
            )
            raise OSError("simulated retirement interruption")

        monkeypatch.setattr(
            registry.capsules, "retire_generation", fail_retire_once
        )
    else:
        original_state = registry._set_state_locked

        def fail_active_commit(
            agent_id: str, **kwargs: object
        ) -> object:
            if kwargs.get("state") == "active" and kwargs.get("generation") == 2:
                raise OSError("simulated active commit interruption")
            return original_state(agent_id, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(registry, "_set_state_locked", fail_active_commit)
    with pytest.raises(OSError, match="interruption"):
        registry.upgrade(created.agent_id)
    assert registry.get(created.agent_id).state == "upgrading"
    registry.close()
    monkeypatch.undo()

    recovered = AgentRegistry(tmp_path)
    try:
        recovered.initialize()
        assert recovered.get(created.agent_id).generation == 2
        assert not old_interpreter.exists()
    finally:
        recovered.close()


def test_delete_recovers_after_runtime_tree_removed_before_data_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    created = registry.create("Interrupted delete")
    capsule = registry.capsules.load_agent(created.agent_id)
    original = registry.capsules.delete_agent

    def remove_runtime_then_interrupt(value: object) -> None:
        assert value == capsule
        import shutil

        shutil.rmtree(capsule.runtime_root)
        raise OSError("simulated delete interruption")

    monkeypatch.setattr(registry.capsules, "delete_agent", remove_runtime_then_interrupt)
    with pytest.raises(OSError, match="delete interruption"):
        registry.delete(created.agent_id)
    assert registry.get(created.agent_id).state == "deleting"
    assert not capsule.runtime_root.exists()
    assert capsule.data_root.exists()
    registry.close()
    monkeypatch.undo()

    recovered = AgentRegistry(tmp_path)
    try:
        recovered.initialize()
        with pytest.raises(KeyError):
            recovered.get(created.agent_id)
        assert not capsule.runtime_root.exists()
        assert not capsule.data_root.exists()
    finally:
        recovered.close()


def test_delete_refuses_live_process_with_cwd_inside_agent_capsule(
    tmp_path: Path,
) -> None:
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    created = registry.create("Referenced Agent")
    capsule = registry.capsules.load_agent(created.agent_id)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=capsule.data_root / "workspace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(RuntimeError, match="referenced by a process"):
            registry.delete(created.agent_id)
        assert registry.get(created.agent_id).state == "deleting"
        assert capsule.data_root.exists()
        assert capsule.runtime_root.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)
    try:
        registry.delete(created.agent_id)
        with pytest.raises(KeyError):
            registry.get(created.agent_id)
    finally:
        registry.close()

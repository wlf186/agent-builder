"""Agent runtime activation, drain and cross-Agent isolation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import agent_builder_v2.agent_runtime as runtime_module
from agent_builder_v2.agent_runtime import AgentRuntimeManager
from agent_builder_v2.agents import AgentRegistry
from agent_builder_v2.capsule import CapsuleManager, PROTOTYPE_AGENT_ID


class _Broker:
    def __init__(self) -> None:
        self.qualification: object | None = None
        self.starts = 0
        self.closes = 0

    async def start(self) -> object:
        self.starts += 1
        self.qualification = object()
        return self.qualification

    async def close(self) -> None:
        self.closes += 1
        self.qualification = None


class _Service:
    instances: list["_Service"] = []

    def __init__(
        self,
        repository_root: Path,
        source_root: Path,
        *,
        agent_id: str,
        model_broker: object,
        manage_model_broker: bool,
    ) -> None:
        del source_root
        self.repository_root = repository_root
        self.agent_id = agent_id
        self.model_broker = model_broker
        self.manage_model_broker = manage_model_broker
        self.capsule = None
        self.closed = False
        self.runs: dict[str, object] = {}
        self.__class__.instances.append(self)

    async def initialize(self) -> None:
        self.capsule = CapsuleManager(self.repository_root).load_agent(self.agent_id)

    async def close(self) -> None:
        self.closed = True


def test_runtime_manager_shares_broker_and_isolates_agent_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    first = registry.create("First")
    second = registry.create("Second")
    broker = _Broker()
    _Service.instances.clear()
    monkeypatch.setattr(runtime_module, "RunService", _Service)
    manager = AgentRuntimeManager(
        tmp_path,
        Path(__file__).resolve().parents[1] / "src",
        registry,
        model_broker=broker,  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        await manager.initialize()
        one = await manager.for_agent(first.agent_id)
        same = await manager.for_agent(first.agent_id)
        other = await manager.for_agent(second.agent_id)
        assert one is same
        assert one is not other
        assert one.run_service.model_broker is other.run_service.model_broker
        assert one.run_service.manage_model_broker is False

        await manager.begin_drain(first.agent_id)
        assert one.run_service.closed is True
        with pytest.raises(RuntimeError, match="draining"):
            await manager.for_agent(first.agent_id)
        upgraded = await asyncio.to_thread(registry.upgrade, first.agent_id)
        await manager.end_drain(first.agent_id)
        replacement = await manager.for_agent(first.agent_id)
        assert replacement.generation == upgraded.generation == 2
        assert replacement is not one
        assert await manager.for_agent(second.agent_id) is other

        await manager.begin_drain(first.agent_id)
        await asyncio.to_thread(registry.delete, first.agent_id)
        await manager.end_drain(first.agent_id)
        with pytest.raises(KeyError):
            await manager.for_agent(first.agent_id)
        assert await manager.for_agent(second.agent_id) is other
        await manager.close()

    try:
        asyncio.run(exercise())
        assert broker.starts == 1
        assert broker.closes == 1
        assert all(value.closed for value in _Service.instances)
        assert registry.get(PROTOTYPE_AGENT_ID).state == "active"
    finally:
        registry.close()

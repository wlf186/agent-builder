"""Lazy, isolated Agent runtimes behind one bounded model broker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentRegistry
from .capsule import CapsuleManager
from .commands import CommandBus
from .control import RunService
from .extensions import ExtensionCatalog
from .ollama import OllamaBroker, OllamaQualification
from .query_engine import QueryEngineRegistry
from .research import (
    ResearchEnvironmentManager,
    ResearchEnvironmentRecord,
)
from .subagents import SubagentCoordinator


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """The application services owned by exactly one Agent generation."""

    agent_id: str
    generation: int
    run_service: RunService
    query_engines: QueryEngineRegistry
    commands: CommandBus


class AgentRuntimeManager:
    """Activate Agent generations lazily and drain them without cross-talk.

    Agent-local mutable state lives below the Capsule roots owned by its
    ``RunService``.  The Ollama broker is intentionally shared so its global
    provider-stream semaphore remains authoritative across all Agents.
    """

    def __init__(
        self,
        repository_root: Path,
        source_root: Path,
        registry: AgentRegistry,
        *,
        model_broker: OllamaBroker | None = None,
        extension_catalog: ExtensionCatalog | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.source_root = source_root.resolve(strict=True)
        self.registry = registry
        self.capsules = CapsuleManager(self.repository_root)
        self.model_broker = model_broker or OllamaBroker()
        self.extension_catalog = extension_catalog or ExtensionCatalog.empty()
        self._runtimes: dict[str, AgentRuntime] = {}
        self._draining: set[str] = set()
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self.subagents = SubagentCoordinator(self.registry, self.for_agent)

    @property
    def qualification(self) -> OllamaQualification | None:
        return self.model_broker.qualification

    async def initialize(self) -> OllamaQualification:
        async with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime manager is closing")
            if self._started:
                qualification = self.model_broker.qualification
                if qualification is None:
                    raise RuntimeError("Agent runtime manager lost qualification")
                return qualification
            qualification = await self.model_broker.start()
            self.subagents.bind_loop(asyncio.get_running_loop())
            self._started = True
            return qualification

    async def for_agent(self, agent_id: str) -> AgentRuntime:
        """Return the current active generation, creating it at most once."""

        async with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime manager is closing")
            if not self._started:
                raise RuntimeError("Agent runtime manager is not initialized")
            if agent_id in self._draining:
                raise RuntimeError("Agent runtime is draining")
            existing = self._runtimes.get(agent_id)
            if existing is not None:
                return existing

            record = await asyncio.to_thread(self.registry.get, agent_id)
            if record.state != "active":
                raise RuntimeError("Agent is not active")
            service = RunService(
                self.repository_root,
                self.source_root,
                agent_id=agent_id,
                model_broker=self.model_broker,
                manage_model_broker=False,
                extension_catalog=self.extension_catalog,
            )
            service.subagent_coordinator = self.subagents
            try:
                await service.initialize()
            except BaseException:
                await service.close()
                raise
            capsule = service.capsule
            if capsule is None or capsule.generation != record.generation:
                await service.close()
                raise RuntimeError("Agent generation changed during activation")
            query_engines = QueryEngineRegistry(service, agent_id)
            runtime = AgentRuntime(
                agent_id=agent_id,
                generation=record.generation,
                run_service=service,
                query_engines=query_engines,
                commands=CommandBus(
                    query_engines,
                    services=service,
                    model_catalog=getattr(self.model_broker, "catalog", None),
                ),
            )
            self._runtimes[agent_id] = runtime
            return runtime

    async def begin_drain(self, agent_id: str) -> None:
        """Fence new admission, detach the generation, then cancel and close it."""

        async with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime manager is closing")
            if agent_id in self._draining:
                raise RuntimeError("Agent runtime is already draining")
            self._draining.add(agent_id)
            runtime = self._runtimes.pop(agent_id, None)
        if runtime is not None:
            await self.subagents.cancel_agent(agent_id)
            await runtime.query_engines.close()
            await runtime.run_service.close()

    async def end_drain(self, agent_id: str) -> None:
        """Remove a lifecycle admission fence after the registry converges."""

        async with self._lock:
            self._draining.discard(agent_id)

    async def research_environment_status(
        self, agent_id: str
    ) -> ResearchEnvironmentRecord | None:
        """Read one Agent generation's curated environment without activating it."""

        async with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime manager is closing")
            if agent_id in self._draining:
                raise RuntimeError("Agent runtime is draining")
            runtime = self._runtimes.get(agent_id)
            environment = (
                runtime.run_service.research_environment
                if runtime is not None
                else None
            )
        if environment is not None:
            return await asyncio.to_thread(environment.status)
        record = await asyncio.to_thread(self.registry.get, agent_id)
        if record.state != "active":
            raise RuntimeError("Agent is not active")
        capsule = await asyncio.to_thread(self.capsules.load_agent, agent_id)
        if capsule.generation != record.generation:
            raise RuntimeError("Agent generation changed during inspection")
        manager = ResearchEnvironmentManager(self.repository_root, capsule)
        return await asyncio.to_thread(manager.status)

    async def install_research_environment(
        self, agent_id: str
    ) -> ResearchEnvironmentRecord:
        existing = await self.research_environment_status(agent_id)
        if existing is not None:
            return existing
        result = await self._mutate_research_environment(agent_id, install=True)
        assert result is not None
        return result

    async def delete_research_environment(self, agent_id: str) -> None:
        if await self.research_environment_status(agent_id) is None:
            return
        await self._mutate_research_environment(agent_id, install=False)

    async def _mutate_research_environment(
        self, agent_id: str, *, install: bool
    ) -> ResearchEnvironmentRecord | None:
        """Fence one generation so package publication cannot race a Run."""

        async with self._lock:
            if self._closing:
                raise RuntimeError("Agent runtime manager is closing")
            if agent_id in self._draining:
                raise RuntimeError("Agent runtime is draining")
            runtime = self._runtimes.get(agent_id)
            if runtime is not None and any(
                record.terminal_kind is None
                for record in runtime.run_service.runs.values()
            ):
                raise RuntimeError(
                    "research environment cannot change while a Run is active"
                )
            self._draining.add(agent_id)
            runtime = self._runtimes.pop(agent_id, None)
        try:
            if runtime is not None:
                await self.subagents.cancel_agent(agent_id)
                await runtime.query_engines.close()
                await runtime.run_service.close()
            record = await asyncio.to_thread(self.registry.get, agent_id)
            if record.state != "active":
                raise RuntimeError("Agent is not active")
            capsule = await asyncio.to_thread(self.capsules.load_agent, agent_id)
            if capsule.generation != record.generation:
                raise RuntimeError("Agent generation changed during environment change")
            manager = ResearchEnvironmentManager(self.repository_root, capsule)
            if install:
                return await asyncio.to_thread(manager.install)
            await asyncio.to_thread(manager.delete)
            return None
        finally:
            async with self._lock:
                self._draining.discard(agent_id)

    async def close(self) -> None:
        async with self._lock:
            if self._closing:
                return
            self._closing = True
            self._draining.update(self._runtimes)
            runtimes = tuple(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            await self.subagents.cancel_agent(runtime.agent_id)
            await runtime.query_engines.close()
            await runtime.run_service.close()
        await self.subagents.close()
        await self.model_broker.close()

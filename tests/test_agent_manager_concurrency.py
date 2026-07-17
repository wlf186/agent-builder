"""Concurrency regressions for cached Agent instances and lifecycle changes."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from src.agent_manager import AgentInstance, AgentManager
from src.models import AgentConfig, LLMProvider


def _config(*, with_mcp: bool = False) -> AgentConfig:
    return AgentConfig(
        name="demo",
        persona="test",
        llm_provider=LLMProvider.OLLAMA,
        llm_model="test",
        mcp_services=["service"] if with_mcp else [],
    )


class _FakeManagedInstance:
    created = 0
    initialize_started: asyncio.Event | None = None
    initialize_release: asyncio.Event | None = None

    def __init__(self, config, *_args, **_kwargs) -> None:
        type(self).created += 1
        self.config = config
        self.shutdown_count = 0

    async def initialize(self) -> bool:
        if self.initialize_started is not None:
            self.initialize_started.set()
        if self.initialize_release is not None:
            await self.initialize_release.wait()
        await asyncio.sleep(0)
        return True

    async def shutdown(self) -> None:
        self.shutdown_count += 1
        await asyncio.sleep(0)


class AgentManagerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = AgentManager(Path(self.temporary.name))
        self.manager.configs["demo"] = _config(with_mcp=True)
        self.manager._config_generations["demo"] = 0
        _FakeManagedInstance.created = 0
        _FakeManagedInstance.initialize_started = None
        _FakeManagedInstance.initialize_release = None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_concurrent_gets_share_one_initialized_instance_even_with_mcp(self):
        with patch("src.agent_manager.AgentInstance", _FakeManagedInstance):
            instances = await asyncio.gather(
                *(self.manager.get_instance("demo") for _ in range(20))
            )

        self.assertEqual(_FakeManagedInstance.created, 1)
        self.assertTrue(all(instance is instances[0] for instance in instances))

        await self.manager.shutdown_all()
        self.assertEqual(instances[0].shutdown_count, 1)

    async def test_delete_during_initialize_never_publishes_stale_instance(self):
        _FakeManagedInstance.initialize_started = asyncio.Event()
        _FakeManagedInstance.initialize_release = asyncio.Event()

        with patch("src.agent_manager.AgentInstance", _FakeManagedInstance):
            pending = asyncio.create_task(self.manager.get_instance("demo"))
            await _FakeManagedInstance.initialize_started.wait()
            self.assertTrue(self.manager.delete_agent_config("demo"))
            _FakeManagedInstance.initialize_release.set()
            result = await pending

        self.assertIsNone(result)
        self.assertNotIn("demo", self.manager.agents)

    async def test_identical_update_does_not_rewrite_or_restart(self):
        existing = self.manager.configs["demo"]
        generation = self.manager._config_generations["demo"]
        sentinel_instance = object()
        self.manager.agents["demo"] = sentinel_instance

        with patch.object(
            self.manager,
            "_save_configs",
            side_effect=AssertionError("no-op update must not write"),
        ), patch.object(
            self.manager,
            "_schedule_instance_shutdown",
            side_effect=AssertionError("no-op update must not restart"),
        ):
            self.assertTrue(
                self.manager.update_agent_config(
                    "demo",
                    existing.model_copy(deep=True),
                )
            )

        self.assertEqual(self.manager._config_generations["demo"], generation)
        self.assertIs(self.manager.agents["demo"], sentinel_instance)


class _StreamingEngine:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.close_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *_args, **_kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            yield {"type": "thinking", "content": "working"}
            await self.release.wait()
            yield {"type": "content", "content": "done"}
        finally:
            self.active -= 1

    def get_token_usage(self):
        return {"input_tokens": 1, "output_tokens": 1}

    async def aclose(self) -> None:
        self.close_count += 1


class _FakeMCPManager:
    def __init__(self) -> None:
        self.servers = {}
        self.shutdown_count = 0

    async def shutdown(self) -> None:
        self.shutdown_count += 1


class AgentInstanceConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_are_serialized_and_shutdown_waits_for_active_stream(self):
        instance = AgentInstance(_config())
        engine = _StreamingEngine()
        mcp_manager = _FakeMCPManager()
        instance.engine = engine
        instance.mcp_manager = mcp_manager

        async def consume(message: str):
            return [event async for event in instance.chat_stream(message)]

        first = asyncio.create_task(consume("first"))
        await engine.started.wait()
        second = asyncio.create_task(consume("second"))
        shutdown = asyncio.create_task(instance.shutdown())
        await asyncio.sleep(0)

        self.assertEqual(engine.max_active, 1)
        self.assertFalse(shutdown.done())

        engine.release.set()
        await first
        await second
        await shutdown

        self.assertEqual(engine.max_active, 1)
        self.assertEqual(mcp_manager.shutdown_count, 1)
        self.assertEqual(engine.close_count, 1)


if __name__ == "__main__":
    unittest.main()

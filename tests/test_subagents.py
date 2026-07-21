"""Explicit subagent Task, mailbox and isolation boundary tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_builder_v2.agents import AgentRegistry
from agent_builder_v2.contracts import new_id
from agent_builder_v2.subagents import SubagentCoordinator, SubagentError, SubagentStore
from agent_builder_v2.tasks import SUBAGENT_TASK_ID, TaskParentIdentity, TaskStore


class _ChildService:
    def __init__(self, agent_id: str, answer: str = "CHILD-OK") -> None:
        self.agent_id = agent_id
        self.answer = answer
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
        self.record = None

    async def create_conversation(self, _title: str) -> object:
        return SimpleNamespace(conversation_id=new_id())

    async def start(self, command: object) -> object:
        record = SimpleNamespace(
            agent_id=self.agent_id,
            conversation_id=command.conversation_id,
            turn_id=new_id(),
            run_id=new_id(),
            terminal_kind="run.completed",
            condition=asyncio.Condition(),
        )
        self.record = record
        return record

    async def get_conversation(self, conversation_id: str) -> object:
        assert self.record is not None
        assert conversation_id == self.record.conversation_id
        return SimpleNamespace(
            turns=(
                SimpleNamespace(
                    run_id=self.record.run_id,
                    assistant_content=self.answer,
                ),
            )
        )

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def delete_conversation(self, conversation_id: str) -> None:
        self.deleted.append(conversation_id)


def _fixture(tmp_path: Path) -> tuple[object, ...]:
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    parent = registry.create("Parent")
    child = registry.create("Child")
    database = tmp_path / "state.sqlite"
    tasks = TaskStore(database, parent.agent_id)
    links = SubagentStore(database, parent.agent_id)
    identity = TaskParentIdentity(
        agent_id=parent.agent_id,
        conversation_id=new_id(),
        turn_id=new_id(),
        run_id=new_id(),
    )
    return registry, parent, child, tasks, links, identity


def test_delegation_owns_task_mailbox_and_child_run(tmp_path: Path) -> None:
    registry, parent, child, tasks, links, identity = _fixture(tmp_path)
    child_service = _ChildService(child.agent_id)

    async def exercise() -> str:
        async def runtime_provider(agent_id: str) -> object:
            assert agent_id == child.agent_id
            return SimpleNamespace(run_service=child_service)

        coordinator = SubagentCoordinator(registry, runtime_provider)
        coordinator.bind_loop(asyncio.get_running_loop())
        service = SimpleNamespace(
            agent_id=parent.agent_id,
            capsule=SimpleNamespace(generation=1),
            task_store=tasks,
            subagent_store=links,
        )
        result = await coordinator._delegate(
            service, identity, child.agent_id, "Return one bounded answer"
        )
        records = links.list_for_conversation(identity.conversation_id)
        assert len(records) == 1
        link = records[0]
        assert link.state == "completed"
        assert link.parent_run_id == identity.run_id
        assert link.child_agent_id == child.agent_id
        assert link.child_run_id == child_service.record.run_id
        mailbox = links.mailbox(link.task_id)
        assert [item.direction for item in mailbox] == [
            "parent_to_child",
            "child_to_parent",
        ]
        assert [item.content for item in mailbox] == [
            "Return one bounded answer",
            "CHILD-OK",
        ]
        task = tasks.get(link.task_id)
        assert task.command_id == SUBAGENT_TASK_ID
        assert task.state == "completed"
        assert [item.kind for item in tasks.notifications(task.task_id)] == [
            "task.queued",
            "task.running",
            "task.completed",
        ]
        await coordinator.cleanup_conversation(service, identity.conversation_id)
        assert child_service.deleted == [link.child_conversation_id]
        assert links.list_for_conversation(identity.conversation_id) == ()
        await coordinator.close()
        return result

    result = json.loads(asyncio.run(exercise()))
    assert result["answer"] == "CHILD-OK"
    assert result["child_agent_id"] == child.agent_id
    tasks.close()
    links.close()


def test_prepare_denies_self_unknown_depth_and_oversize(tmp_path: Path) -> None:
    registry, parent, child, tasks, links, identity = _fixture(tmp_path)

    async def exercise() -> None:
        async def runtime_provider(_agent_id: str) -> object:
            raise AssertionError("prepare must not activate a child")

        coordinator = SubagentCoordinator(registry, runtime_provider)
        coordinator.bind_loop(asyncio.get_running_loop())
        service = SimpleNamespace(
            agent_id=parent.agent_id,
            capsule=SimpleNamespace(generation=1),
            task_store=tasks,
            subagent_store=links,
        )
        with pytest.raises(SubagentError, match="not admissible"):
            coordinator.prepare(
                service,
                identity,
                {"child_agent_id": parent.agent_id, "message": "self"},
            )
        with pytest.raises(SubagentError, match="unavailable"):
            coordinator.prepare(
                service,
                identity,
                {"child_agent_id": "0" * 32, "message": "unknown"},
            )
        coordinator._active_children.add(identity.run_id)
        with pytest.raises(SubagentError, match="not admissible"):
            coordinator.prepare(
                service,
                identity,
                {"child_agent_id": child.agent_id, "message": "nested"},
            )
        coordinator._active_children.clear()
        with pytest.raises(SubagentError, match="not admissible"):
            coordinator.prepare(
                service,
                identity,
                {"child_agent_id": child.agent_id, "message": "x" * 4097},
            )
        await coordinator.close()

    asyncio.run(exercise())
    tasks.close()
    links.close()


def test_restart_marks_task_and_link_interrupted_once(tmp_path: Path) -> None:
    registry, parent, child, tasks, links, identity = _fixture(tmp_path)
    task_id = new_id()
    tasks.create(
        task_id=task_id,
        capsule_generation=1,
        parent=identity,
        command_id=SUBAGENT_TASK_ID,
        executor_identity_digest="a" * 64,
        request_digest="b" * 64,
    )
    links.create(
        task_id=task_id,
        parent=identity,
        child_agent_id=child.agent_id,
        message="interrupted",
    )
    tasks.mark_running(task_id)
    assert tasks.recover_incomplete() == 1
    assert links.recover_incomplete() == 1
    assert tasks.get(task_id).state == "interrupted"
    assert links.get(task_id).state == "interrupted"
    assert tasks.recover_incomplete() == 0
    assert links.recover_incomplete() == 0
    tasks.close()
    links.close()

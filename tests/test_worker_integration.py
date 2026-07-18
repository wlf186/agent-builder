"""Real one-process-per-Run vertical slice without network or legacy services."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import ContextPlan, ModelProfile
from agent_builder_v2.contracts import TERMINAL_KINDS, StartRunCommand
from agent_builder_v2.control import RunService
from agent_builder_v2.ollama import (
    OllamaBrokerError,
    OllamaFrame,
    OllamaQualification,
    OllamaToolResult,
)
from agent_builder_v2.sessions import ConversationNotFoundError


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class _FakeModelSession:
    async def stream_turn(
        self,
        user_message: str,
        tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
    ) -> object:
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "real-broker-call",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": user_message},
                    "usage": {"prompt_eval_count": 8, "eval_count": 2},
                },
            )
            return
        assert tool_results == (
            OllamaToolResult(
                call_id="real-broker-call",
                tool_id="builtin/echo",
                content=user_message,
                outcome="succeeded",
            ),
        )
        yield OllamaFrame("content", {"text": f"broker result: {user_message}"})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 10, "eval_count": 4},
            },
        )


class _FakeModelBroker:
    def __init__(self) -> None:
        self.plans: list[ContextPlan] = []
        self.qualification = OllamaQualification(
            version="test",
            model="qwen3.5:2b",
            digest="a" * 64,
            size=1,
            address="10.89.0.18",
            model_profile=ModelProfile(
                provider="ollama",
                model="qwen3.5:2b",
                model_digest="a" * 64,
                native_context_tokens=262_144,
                operational_context_tokens=32_768,
                max_output_tokens=2_048,
                profile_source="test",
            ),
        )

    async def start(self) -> OllamaQualification:
        return self.qualification

    def new_run(self, context_plan: ContextPlan) -> _FakeModelSession:
        assert context_plan.model_profile == self.qualification.model_profile
        self.plans.append(context_plan)
        return _FakeModelSession()

    async def close(self) -> None:
        return None


class _BusyModelSession:
    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
    ) -> object:
        if False:  # pragma: no cover - retain async-generator semantics
            yield OllamaFrame("content", {"text": "unreachable"})
        raise OllamaBrokerError(
            "model_busy", "simulated bounded queue timeout", retryable=True
        )


class _BusyModelBroker(_FakeModelBroker):
    def new_run(self, context_plan: ContextPlan) -> _BusyModelSession:
        assert context_plan.model_profile == self.qualification.model_profile
        return _BusyModelSession()


def test_control_plane_runs_and_cleans_agent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_launch = asyncio.create_subprocess_exec
    launch_options: list[dict[str, object]] = []

    async def capture_launch(*args: object, **kwargs: object) -> object:
        launch_options.append(dict(kwargs))
        return await original_launch(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)

    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            assert service.capsule is not None
            assert service.capsule.interpreter.is_file()
            assert service.capsule.interpreter.is_relative_to(
                service.capsule.runtime_root
            )

            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "real process integration")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[0].kind == "run.started"
            assert events[-1].kind == "run.completed"
            assert [event.seq for event in events] == list(
                range(1, len(events) + 1)
            )
            assert sum(event.kind in TERMINAL_KINDS for event in events) == 1
            requested = {
                event.payload["call_id"]
                for event in events
                if event.kind == "tool.call.requested"
            }
            finished = {
                event.payload["call_id"]
                for event in events
                if event.kind == "tool.call.finished"
            }
            assert requested == finished == {"real-broker-call"}
            assert record.process is None
            assert not (
                service.capsule.runtime_root / "runs" / record.run_id
            ).exists()
            assert len(launch_options) == 1
            assert launch_options[0]["start_new_session"] is True
            assert "preexec_fn" not in launch_options[0]
            worker_environment = launch_options[0]["env"]
            assert isinstance(worker_environment, dict)
            assert not any("OLLAMA" in str(key) for key in worker_environment)
            assert events[0].payload["model"] == "qwen3.5:2b"
            assert events[0].payload["sandbox"] == "harness-v2-worker-v1"
            assert events[0].payload["context_plan"]["input_budget_tokens"] == 30_720
            assert events[-1].payload["usage"] == {
                "input_tokens": 18,
                "output_tokens": 6,
                "last_input_tokens": 10,
                "complete": True,
            }
        finally:
            await service.close()

    asyncio.run(exercise())


def test_control_plane_preserves_trusted_model_error_semantics(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_BusyModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "exercise bounded model queue")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[-1].kind == "run.failed"
            assert events[-1].payload == {
                "code": "model_busy",
                "message": "The trusted model broker could not complete the request.",
                "retryable": True,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "last_input_tokens": 0,
                    "complete": False,
                },
            }
            assert record.model_failure == ("model_busy", True)
            restored = await service.get_conversation(record.conversation_id)
            assert len(restored.turns) == 1
            assert restored.turns[0].status == "failed"
            assert service.conversations is not None
            assert service.conversations.committed_history(record.conversation_id) == ()
        finally:
            await service.close()

    asyncio.run(exercise())


def test_completed_conversation_restores_into_the_next_isolated_run(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _FakeModelBroker()
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation = await service.create_conversation("多轮集成")

            first = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "第一轮：记住代号是青竹",
                    conversation.conversation_id,
                )
            )
            first_events = [
                event
                async for event in service.stream(first.run_id)
                if event is not None
            ]
            assert first_events[-1].kind == "run.completed"

            second = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "第二轮：刚才的代号是什么？",
                    conversation.conversation_id,
                )
            )
            second_events = [
                event
                async for event in service.stream(second.run_id)
                if event is not None
            ]
            assert second_events[-1].kind == "run.completed"
            assert first.conversation_id == second.conversation_id
            assert first.turn_id != second.turn_id
            assert first.run_id != second.run_id

            assert len(broker.plans) == 2
            second_messages = broker.plans[1].provider_messages()
            assert [message["role"] for message in second_messages] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            assert second_messages[1]["content"] == "第一轮：记住代号是青竹"
            assert second_messages[2]["content"] == (
                "broker result: 第一轮：记住代号是青竹"
            )
            assert second_messages[-1]["content"] == "第二轮：刚才的代号是什么？"

            restored = await service.get_conversation(conversation.conversation_id)
            assert [turn.status for turn in restored.turns] == [
                "completed",
                "completed",
            ]
            assert [turn.assistant_content for turn in restored.turns] == [
                "broker result: 第一轮：记住代号是青竹",
                "broker result: 第二轮：刚才的代号是什么？",
            ]

            deleted = await service.delete_conversation(conversation.conversation_id)
            assert deleted.deleted is True
            assert deleted.deleted_turns == 2
            with pytest.raises(ConversationNotFoundError):
                await service.get_conversation(conversation.conversation_id)
            assert first.run_id not in service.runs
            assert second.run_id not in service.runs
            assert service.journal is not None
            assert service.journal.events_for_run(first.run_id) == []
            assert service.journal.events_for_run(second.run_id) == []
        finally:
            await service.close()

    asyncio.run(exercise())

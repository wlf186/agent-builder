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
    OllamaCancelledError,
    OllamaFrame,
    OllamaQualification,
    OllamaRequestMetadata,
    OllamaToolResult,
)
from agent_builder_v2.query_engine import QueryEngineRegistry, QueryEngineRetiredError
from agent_builder_v2.sessions import ConversationNotFoundError


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class _FakeModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        user_message: str,
        tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
    ) -> object:
        iteration = len(tool_results) + 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(self.context_plan.tools),
                    estimated_input_tokens=(
                        self.context_plan.estimated_input_tokens
                        + (123 if iteration == 2 else 0)
                    ),
                    request_bytes=512 + 64 * len(tool_results),
                    request_digest=("c" if iteration == 1 else "d") * 64,
                )
            )
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

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _FakeModelSession:
        assert max_tool_calls == 2
        assert context_plan.model_profile == self.qualification.model_profile
        self.plans.append(context_plan)
        return _FakeModelSession(context_plan)

    async def close(self) -> None:
        return None


class _BusyModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
    ) -> object:
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest="c" * 64,
                )
            )
        if False:  # pragma: no cover - retain async-generator semantics
            yield OllamaFrame("content", {"text": "unreachable"})
        raise OllamaBrokerError(
            "model_busy", "simulated bounded queue timeout", retryable=True
        )


class _BusyModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _BusyModelSession:
        assert max_tool_calls == 2
        assert context_plan.model_profile == self.qualification.model_profile
        return _BusyModelSession(context_plan)


class _CancellableModelSession:
    def __init__(self, context_plan: ContextPlan, entered: asyncio.Event) -> None:
        self.context_plan = context_plan
        self.entered = entered

    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        is_cancelled: object = None,
        on_request: object = None,
    ) -> object:
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest="e" * 64,
                )
            )
        self.entered.set()
        while callable(is_cancelled) and not is_cancelled():
            await asyncio.sleep(0.01)
        if False:  # pragma: no cover - retain async-generator semantics
            yield OllamaFrame("content", {"text": "unreachable"})
        raise OllamaCancelledError()


class _CancellableModelBroker(_FakeModelBroker):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _CancellableModelSession:
        assert max_tool_calls == 2
        assert context_plan.model_profile == self.qualification.model_profile
        return _CancellableModelSession(context_plan, self.entered)


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
            model_events = [
                event for event in events if event.kind.startswith("model.")
            ]
            assert [event.kind for event in model_events] == [
                "model.request.started",
                "model.response.finished",
                "model.request.started",
                "model.response.finished",
            ]
            assert model_events[0].payload["tool_result_call_ids"] == []
            assert model_events[0].payload["tool_count"] == 1
            assert model_events[0].payload["request_digest"] == "c" * 64
            assert model_events[1].payload == {
                "request_id": "model-1",
                "iteration": 1,
                "outcome": "tool_use",
                "input_tokens": 8,
                "output_tokens": 2,
                "usage_complete": True,
                "error_code": None,
            }
            assert model_events[2].payload["tool_result_call_ids"] == [
                "real-broker-call"
            ]
            assert model_events[2].payload["tool_count"] == 1
            assert model_events[3].payload["outcome"] == "end_turn"
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
            assert service.conversations is not None
            usage = service.conversations.provider_usage_for_run(record.run_id)
            assert [(item.call_index, item.status) for item in usage] == [
                (1, "complete"),
                (2, "complete"),
            ]
            assert [
                (item.input_tokens, item.output_tokens) for item in usage
            ] == [(8, 2), (10, 4)]
            assert [item.estimated_input_tokens for item in usage] == [
                events[0].payload["context_plan"]["estimated_input_tokens"],
                events[0].payload["context_plan"]["estimated_input_tokens"] + 123,
            ]
            assert all(item.cost_minor_units is None for item in usage)
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
            model_events = [
                event for event in events if event.kind.startswith("model.")
            ]
            assert [event.kind for event in model_events] == [
                "model.request.started",
                "model.response.finished",
            ]
            assert model_events[-1].payload == {
                "request_id": "model-1",
                "iteration": 1,
                "outcome": "error",
                "input_tokens": 0,
                "output_tokens": 0,
                "usage_complete": False,
                "error_code": "model_busy",
            }
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
            usage = service.conversations.provider_usage_for_run(record.run_id)
            assert len(usage) == 1
            assert usage[0].status == "incomplete"
            assert usage[0].input_tokens is None
            assert usage[0].output_tokens is None
        finally:
            await service.close()

    asyncio.run(exercise())


def test_control_plane_closes_cancelled_provider_boundary_once(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _CancellableModelBroker()
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "cancel provider stream")
            )
            await asyncio.wait_for(broker.entered.wait(), timeout=5)
            await service.cancel(record.run_id)
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[-1].kind == "run.cancelled"
            model_events = [
                event for event in events if event.kind.startswith("model.")
            ]
            assert [event.kind for event in model_events] == [
                "model.request.started",
                "model.response.finished",
            ]
            assert model_events[-1].payload == {
                "request_id": "model-1",
                "iteration": 1,
                "outcome": "cancelled",
                "input_tokens": 0,
                "output_tokens": 0,
                "usage_complete": False,
                "error_code": "model_cancelled",
            }
            assert record.model_response_count == 1
            assert service.conversations is not None
            usage = service.conversations.provider_usage_for_run(record.run_id)
            assert len(usage) == 1
            assert usage[0].status == "incomplete"
        finally:
            await service.close()

    asyncio.run(exercise())


def test_durable_replay_survives_gateway_restart_without_duplicate_terminal(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        await first.initialize()
        try:
            record = await first.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "restart replay integration")
            )
            live = [
                event
                async for event in first.stream(record.run_id)
                if event is not None
            ]
            assert live[-1].kind == "run.completed"
            run_id = record.run_id
            terminal_cursor = live[-1].seq
        finally:
            await first.close()

        restored = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        await restored.initialize()
        try:
            assert restored.runs == {}
            identity = await restored.resolve_run_identity(run_id)
            cursor = 0
            replayed = []
            while True:
                page = await restored.replay_run(
                    run_id,
                    after=cursor,
                    limit=2,
                    expected_identity=identity,
                )
                replayed.extend(page.events)
                cursor = page.next_cursor
                if not page.has_more:
                    break
            assert cursor == terminal_cursor
            assert sum(event.kind in TERMINAL_KINDS for event in replayed) == 1
            reconnect = await restored.replay_run(
                run_id,
                after=terminal_cursor,
                limit=128,
                expected_identity=identity,
            )
            assert reconnect.events == ()
            assert reconnect.next_cursor == terminal_cursor
            assert reconnect.has_more is False
        finally:
            await restored.close()

    asyncio.run(exercise())


def test_query_engine_restores_completed_conversation_into_next_isolated_run(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _FakeModelBroker()
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        registry: QueryEngineRegistry | None = None
        try:
            await service.initialize()
            registry = QueryEngineRegistry(service, PROTOTYPE_AGENT_ID)
            conversation = await registry.create_conversation("多轮集成")
            engine = await registry.for_conversation(
                conversation.conversation_id
            )

            first = await engine.submit_message(
                "第一轮：记住代号是青竹"
            )
            first_events = [
                event
                async for event in engine.stream(first.run_id)
                if event is not None
            ]
            assert first_events[-1].kind == "run.completed"

            second = await engine.submit_message(
                "第二轮：刚才的代号是什么？"
            )
            second_events = [
                event
                async for event in engine.stream(second.run_id)
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

            restored = await engine.restore()
            assert [turn.status for turn in restored.turns] == [
                "completed",
                "completed",
            ]
            assert [turn.assistant_content for turn in restored.turns] == [
                "broker result: 第一轮：记住代号是青竹",
                "broker result: 第二轮：刚才的代号是什么？",
            ]

            deleted = await engine.delete()
            assert deleted.deleted is True
            assert deleted.deleted_turns == 2
            with pytest.raises(ConversationNotFoundError):
                await service.get_conversation(conversation.conversation_id)
            with pytest.raises(QueryEngineRetiredError):
                await engine.restore()
            assert registry.cached_engine_count == 0
            assert first.run_id not in service.runs
            assert second.run_id not in service.runs
            assert service.journal is not None
            assert service.journal.events_for_run(first.run_id) == []
            assert service.journal.events_for_run(second.run_id) == []
        finally:
            if registry is not None:
                await registry.close()
            await service.close()

    asyncio.run(exercise())


def test_query_engine_is_lazily_rebuilt_from_sqlite_after_gateway_restart(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first_service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        first_registry: QueryEngineRegistry | None = None
        conversation_id: str
        old_engine = None
        try:
            await first_service.initialize()
            first_registry = QueryEngineRegistry(
                first_service, PROTOTYPE_AGENT_ID
            )
            conversation = await first_registry.create_conversation(
                "重启恢复"
            )
            conversation_id = conversation.conversation_id
            old_engine = await first_registry.for_conversation(conversation_id)
            first = await old_engine.submit_message("重启前的代号是青竹")
            events = [
                event
                async for event in old_engine.stream(first.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
        finally:
            if first_registry is not None:
                await first_registry.close()
            await first_service.close()

        assert old_engine is not None
        with pytest.raises(QueryEngineRetiredError):
            await old_engine.restore()

        second_broker = _FakeModelBroker()
        second_service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=second_broker,  # type: ignore[arg-type]
        )
        second_registry: QueryEngineRegistry | None = None
        try:
            await second_service.initialize()
            second_registry = QueryEngineRegistry(
                second_service, PROTOTYPE_AGENT_ID
            )
            new_engine = await second_registry.for_conversation(
                conversation_id
            )
            assert new_engine is not old_engine
            restored = await new_engine.restore()
            assert restored.turns[0].assistant_content == (
                "broker result: 重启前的代号是青竹"
            )

            second = await new_engine.submit_message("重启后继续这一会话")
            events = [
                event
                async for event in new_engine.stream(second.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            projected = second_broker.plans[0].provider_messages()
            assert [message["role"] for message in projected] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            assert projected[1]["content"] == "重启前的代号是青竹"
            assert projected[2]["content"] == (
                "broker result: 重启前的代号是青竹"
            )
            assert projected[3]["content"] == "重启后继续这一会话"
        finally:
            if second_registry is not None:
                await second_registry.close()
            await second_service.close()

    asyncio.run(exercise())

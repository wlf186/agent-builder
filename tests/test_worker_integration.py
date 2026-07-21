"""Real one-process-per-Run vertical slice without network or legacy services."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import ContextPlan, ModelProfile
from agent_builder_v2.contracts import TERMINAL_KINDS, StartRunCommand
from agent_builder_v2.control import RunService
from agent_builder_v2.context_projection import ContextProjectionBoundary
from agent_builder_v2.ollama import (
    OllamaBrokerError,
    OllamaCancelledError,
    OllamaFrame,
    OllamaQualification,
    OllamaRequestMetadata,
    OllamaToolResult,
)
from agent_builder_v2.query_engine import QueryEngineRegistry, QueryEngineRetiredError
from agent_builder_v2.sessions import (
    ConversationConflictError,
    ConversationNotFoundError,
)


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
                    "tool_id": "file/glob",
                    "arguments": {"pattern": "**/*", "max_results": 1},
                    "usage": {"prompt_eval_count": 8, "eval_count": 2},
                },
            )
            return
        assert len(tool_results) == 1
        result = tool_results[0]
        assert result.call_id == "real-broker-call"
        assert result.tool_id == "file/glob"
        assert result.outcome == "succeeded"
        assert result.content
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


class _FileReadModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
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
                    estimated_input_tokens=self.context_plan.estimated_input_tokens + 64 * len(tool_results),
                    request_bytes=768 + 256 * len(tool_results),
                    request_digest=("a" if iteration == 1 else "b") * 64,
                )
            )
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "workspace-read-call",
                    "tool_id": "file/read_text",
                    "arguments": {"path": "facts.txt", "max_bytes": 512},
                    "usage": {"prompt_eval_count": 12, "eval_count": 3},
                },
            )
            return
        assert len(tool_results) == 1
        result = tool_results[0]
        assert result.tool_id == "file/read_text"
        assert result.outcome == "succeeded"
        decoded = json.loads(result.content)
        assert decoded["content"] == "The bounded answer is 42.\n"
        assert decoded["receipt"]["path"] == "facts.txt"
        yield OllamaFrame("content", {"text": "I read the workspace file; the answer is 42."})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 16, "eval_count": 8},
            },
        )


class _FileReadModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _FileReadModelSession:
        assert max_tool_calls == 2
        return _FileReadModelSession(context_plan)


class _SearchReadModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
        tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
    ) -> object:
        iteration = len(tool_results) + 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages()) + 2 * len(tool_results),
                    tool_count=len(self.context_plan.tools) if len(tool_results) < 2 else 0,
                    estimated_input_tokens=self.context_plan.estimated_input_tokens + 64 * len(tool_results),
                    request_bytes=768 + 256 * len(tool_results),
                    request_digest=("4" if iteration == 1 else "5" if iteration == 2 else "6") * 64,
                )
            )
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "workspace-grep-call",
                    "tool_id": "file/grep",
                    "arguments": {
                        "pattern": "**/*.txt",
                        "query": "SEARCH-01 target",
                        "max_results": 8,
                    },
                    "usage": {"prompt_eval_count": 12, "eval_count": 3},
                },
            )
            return
        if len(tool_results) == 1:
            search = json.loads(tool_results[0].content)
            assert [item["path"] for item in search["matches"]] == ["docs/found.txt"]
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "workspace-read-after-search",
                    "tool_id": "file/read_text",
                    "arguments": {"path": "docs/found.txt", "max_bytes": 512},
                    "usage": {"prompt_eval_count": 16, "eval_count": 4},
                },
            )
            return
        assert len(tool_results) == 2
        read = json.loads(tool_results[1].content)
        assert read["content"] == "SEARCH-01 target: amber-42\n"
        yield OllamaFrame("content", {"text": "Search and bounded read found amber-42."})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 20, "eval_count": 7},
            },
        )


class _SearchReadModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _SearchReadModelSession:
        assert max_tool_calls == 2
        return _SearchReadModelSession(context_plan)


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


class _EmptyModelSession:
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
                    request_digest="7" * 64,
                )
            )
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 1472, "eval_count": 33},
            },
        )


class _EmptyModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _EmptyModelSession:
        assert max_tool_calls == 2
        assert context_plan.model_profile == self.qualification.model_profile
        return _EmptyModelSession(context_plan)


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


class _OverflowModelSession:
    def __init__(
        self,
        context_plan: ContextPlan,
        *,
        overflow_twice: bool = False,
        partial: bool = False,
        wait_for_cancel: bool = False,
    ) -> None:
        self.context_plan = context_plan
        self.overflow_twice = overflow_twice
        self.partial = partial
        self.wait_for_cancel = wait_for_cancel
        self.entered = asyncio.Event()
        self.attempts = 0
        self.installed: ContextPlan | None = None

    def install_recovery_context(self, context_plan: ContextPlan) -> None:
        assert self.attempts == 1
        assert context_plan.model_profile == self.context_plan.model_profile
        assert context_plan.tools == self.context_plan.tools
        assert context_plan.user_message() == self.context_plan.user_message()
        assert context_plan.reference != self.context_plan.reference
        self.context_plan = context_plan
        self.installed = context_plan

    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        is_cancelled: object = None,
        on_request: object = None,
    ) -> object:
        self.attempts += 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest=("e" if self.attempts == 1 else "f") * 64,
                )
            )
        self.entered.set()
        if self.wait_for_cancel:
            while callable(is_cancelled) and not is_cancelled():
                await asyncio.sleep(0.01)
        if self.partial and self.attempts == 1:
            yield OllamaFrame("content", {"text": "partial"})
        if self.attempts == 1 or self.overflow_twice:
            raise OllamaBrokerError(
                "model_context_overflow", "simulated exact provider overflow"
            )
        yield OllamaFrame("content", {"text": "recovered answer"})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 12, "eval_count": 3},
            },
        )


class _OverflowModelBroker(_FakeModelBroker):
    def __init__(
        self,
        *,
        overflow_twice: bool = False,
        partial: bool = False,
        wait_for_cancel: bool = False,
    ) -> None:
        super().__init__()
        self.overflow_twice = overflow_twice
        self.partial = partial
        self.wait_for_cancel = wait_for_cancel
        self.sessions: list[object] = []

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> object:
        assert max_tool_calls == 2
        if len(self.sessions) < 2:
            session: object = _FakeModelSession(context_plan)
        else:
            session = _OverflowModelSession(
                context_plan,
                overflow_twice=self.overflow_twice,
                partial=self.partial,
                wait_for_cancel=self.wait_for_cancel,
            )
        self.sessions.append(session)
        return session


class _WriteModelSession:
    def __init__(self, context_plan: ContextPlan, content: str = "approved\n") -> None:
        self.context_plan = context_plan
        self.content = content

    async def stream_turn(
        self,
        _user_message: str,
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
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=768,
                    request_digest=("7" if iteration == 1 else "8") * 64,
                )
            )
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "write-provider-call",
                    "tool_id": "file/write",
                    "arguments": {
                        "path": "created.txt",
                        "content": self.content,
                        "create": True,
                    },
                    "usage": {"prompt_eval_count": 8, "eval_count": 2},
                },
            )
            return
        assert len(tool_results) == 1
        yield OllamaFrame("content", {"text": f"write {tool_results[0].outcome}"})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 10, "eval_count": 3},
            },
        )


class _WriteModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _WriteModelSession:
        assert max_tool_calls == 2
        return _WriteModelSession(context_plan)


class _ExecModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
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
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=768,
                    request_digest=("a" if iteration == 1 else "b") * 64,
                )
            )
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "exec-provider-call",
                    "tool_id": "exec/run",
                    "arguments": {"command_id": "runtime-compile"},
                    "usage": {"prompt_eval_count": 8, "eval_count": 2},
                },
            )
            return
        result = json.loads(tool_results[0].content)
        yield OllamaFrame(
            "content",
            {"text": f"compile exit {result['exit_code']}"},
        )
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 10, "eval_count": 3},
            },
        )


class _ExecModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _ExecModelSession:
        assert max_tool_calls == 2
        return _ExecModelSession(context_plan)


class _EditModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
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
                    tool_count=(len(self.context_plan.tools) if len(tool_results) < 2 else 0),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=768,
                    request_digest=f"{iteration}" * 64,
                )
            )
        if not tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "read-before-edit",
                    "tool_id": "file/read_text",
                    "arguments": {"path": "edit.txt"},
                    "usage": {"prompt_eval_count": 8, "eval_count": 2},
                },
            )
            return
        if len(tool_results) == 1:
            read = json.loads(tool_results[0].content)
            receipt = read["receipt"]
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "edit-provider-call",
                    "tool_id": "file/edit",
                    "arguments": {
                        "path": "edit.txt",
                        "old_text": "before",
                        "new_text": "after",
                        "path_identity": receipt["path_identity"],
                        "content_digest": receipt["content_digest"],
                    },
                    "usage": {"prompt_eval_count": 10, "eval_count": 3},
                },
            )
            return
        yield OllamaFrame("content", {"text": "edit complete"})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 12, "eval_count": 3},
            },
        )


class _EditModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _EditModelSession:
        assert max_tool_calls == 2
        return _EditModelSession(context_plan)


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
            assert model_events[0].payload["tool_count"] == 8
            assert model_events[0].payload["request_digest"] == "c" * 64
            assert model_events[1].payload == {
                "request_id": "model-1",
                "iteration": 1,
                "attempt": 0,
                "recovery_id": None,
                "provider_call_index": 1,
                "outcome": "tool_use",
                "input_tokens": 8,
                "output_tokens": 2,
                "usage_complete": True,
                "error_code": None,
            }
            assert model_events[2].payload["tool_result_call_ids"] == [
                "real-broker-call"
            ]
            assert model_events[2].payload["tool_count"] == 8
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
            boundary = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert boundary is not None
            assert boundary.conversation_revision == 0
            assert boundary.context_plan_digest == record.context_plan.reference.digest
            assert boundary.toolset_digest == record.context_plan.reference.toolset_digest
            assert boundary.reason == "admission"
            assert "real process integration" not in boundary.to_json()
            assert record.runtime_snapshot is not None
            replay_boundary = ContextProjectionBoundary.create(
                record.runtime_snapshot,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                conversation_revision=0,
                reason="replay",
            )
            service.conversations.replace_context_projection_boundary(
                replay_boundary,
                expected_boundary_digest=boundary.boundary_digest,
            )
            assert (
                service.conversations.read_context_projection_boundary(record.run_id)
                == replay_boundary
            )
            assert service.conversations._connection.execute(
                "SELECT COUNT(*) FROM context_projection_boundaries WHERE run_id = ?",
                (record.run_id,),
            ).fetchone()[0] == 1
            with pytest.raises(ConversationConflictError, match="CAS failed"):
                service.conversations.replace_context_projection_boundary(
                    boundary,
                    expected_boundary_digest=boundary.boundary_digest,
                )
        finally:
            await service.close()

    asyncio.run(exercise())


def test_file_read_is_brokered_outside_worker_and_audited_end_to_end(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FileReadModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            assert service.capsule is not None
            target = service.capsule.data_root / "workspace" / "facts.txt"
            target.write_text("The bounded answer is 42.\n", encoding="utf-8")
            target.chmod(0o600)
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "Read facts.txt and answer.")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[-1].kind == "run.completed"
            finished = next(
                event for event in events if event.kind == "tool.call.finished"
            )
            assert finished.payload["tool_id"] == "file/read_text"
            result = json.loads(finished.payload["result"])
            assert result["content"] == "The bounded answer is 42.\n"
            assert result["receipt"]["content_digest"]
            assert record.process is None
            assert not (
                service.capsule.runtime_root / "runs" / record.run_id
            ).exists()
            permissions = await service.list_permission_requests(pending_only=False)
            permission = next(
                item for item in permissions if item.run_id == record.run_id
            )
            assert permission.capability_id == "file/read_text"
            assert permission.policy_decision == "allow"
            assert permission.status == "approved"
            audit = await service.capability_audit_events(record.run_id)
            assert [item.kind for item in audit] == [
                "permission.requested",
                "permission.resolved",
                "operation.intent",
                "operation.dispatched",
                "operation.outcome",
            ]
            assert all(len(item.detail_digest) == 64 for item in audit)
        finally:
            await service.close()

    asyncio.run(exercise())


def test_search_then_read_then_answer_stays_in_one_run_and_cleans_up(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_SearchReadModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            assert service.capsule is not None
            workspace = service.capsule.data_root / "workspace"
            (workspace / "docs").mkdir(mode=0o700)
            target = workspace / "docs" / "found.txt"
            target.write_text("SEARCH-01 target: amber-42\n", encoding="utf-8")
            target.chmod(0o600)
            other = workspace / "docs" / "other.txt"
            other.write_text("no match\n", encoding="utf-8")
            other.chmod(0o600)
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "Find the SEARCH-01 target and read it.")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[-1].kind == "run.completed"
            assert [
                event.payload["tool_id"]
                for event in events
                if event.kind == "tool.call.finished"
            ] == ["file/grep", "file/read_text"]
            assert [
                event.payload["outcome"]
                for event in events
                if event.kind == "tool.call.finished"
            ] == ["succeeded", "succeeded"]
            audit = await service.capability_audit_events(record.run_id)
            assert [item.kind for item in audit] == [
                "permission.requested", "permission.resolved", "operation.intent",
                "operation.dispatched", "operation.outcome",
                "permission.requested", "permission.resolved", "operation.intent",
                "operation.dispatched", "operation.outcome",
            ]
            assert record.process is None
            assert not (service.capsule.runtime_root / "runs" / record.run_id).exists()
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
                "attempt": 0,
                "recovery_id": None,
                "provider_call_index": 1,
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


def test_empty_model_response_fails_cleanly_and_preserves_usage(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_EmptyModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "return no visible content")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert [event.kind for event in events if event.kind.startswith("model.")] == [
                "model.request.started",
                "model.response.finished",
            ]
            assert events[-2].payload == {
                "request_id": "model-1",
                "iteration": 1,
                "attempt": 0,
                "recovery_id": None,
                "provider_call_index": 1,
                "outcome": "end_turn",
                "input_tokens": 1472,
                "output_tokens": 33,
                "usage_complete": True,
                "error_code": None,
            }
            assert events[-1].kind == "run.failed"
            assert events[-1].payload == {
                "code": "model_empty_response",
                "message": "The trusted model broker could not complete the request.",
                "retryable": True,
                "usage": {
                    "input_tokens": 1472,
                    "output_tokens": 33,
                    "last_input_tokens": 1472,
                    "complete": True,
                },
            }
            assert record.journal_failed is False
            assert record.model_failure == ("model_empty_response", True)
            restored = await service.get_conversation(record.conversation_id)
            assert restored.turns[-1].status == "failed"
            assert service.conversations is not None
            journal_state = service.conversations.get_run_journal_state(record.run_id)
            assert journal_state.availability == "full"
            assert journal_state.terminal_kind == "run.failed"
            assert journal_state.usage_complete is True
            usage = service.conversations.provider_usage_for_run(record.run_id)
            assert len(usage) == 1
            assert usage[0].status == "complete"
            assert usage[0].input_tokens == 1472
            assert usage[0].output_tokens == 33
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
                "attempt": 0,
                "recovery_id": None,
                "provider_call_index": 1,
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
            assert first.conversations is not None
            original_boundary = first.conversations.read_context_projection_boundary(
                run_id
            )
            assert original_boundary is not None
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
            assert restored.conversations is not None
            assert (
                restored.conversations.read_context_projection_boundary(run_id)
                == original_boundary
            )
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
            assert service.conversations is not None
            assert (
                service.conversations.read_context_projection_boundary(first.run_id)
                is None
            )
            assert (
                service.conversations.read_context_projection_boundary(second.run_id)
                is None
            )
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


async def _prime_overflow_history(service: RunService) -> str:
    first = await service.start(StartRunCommand(PROTOTYPE_AGENT_ID, "A" * 2_000))
    first_events = [
        event async for event in service.stream(first.run_id) if event is not None
    ]
    assert first_events[-1].kind == "run.completed"
    second = await service.start(
        StartRunCommand(
            PROTOTYPE_AGENT_ID,
            "B" * 2_000,
            conversation_id=first.conversation_id,
        )
    )
    second_events = [
        event async for event in service.stream(second.run_id) if event is not None
    ]
    assert second_events[-1].kind == "run.completed"
    return first.conversation_id


def test_control_plane_recovers_one_provider_overflow_with_durable_identity(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _OverflowModelBroker()
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation_id = await _prime_overflow_history(service)
            recovered = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "recover this turn",
                    conversation_id=conversation_id,
                )
            )
            events = [
                event
                async for event in service.stream(recovered.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            model_events = [
                event for event in events if event.kind.startswith("model.")
            ]
            assert [event.kind for event in model_events] == [
                "model.request.started",
                "model.response.finished",
                "model.recovery.started",
                "model.request.started",
                "model.response.finished",
            ]
            assert [
                model_events[index].payload["attempt"]
                for index in (0, 1, 3, 4)
            ] == [0, 0, 1, 1]
            assert [
                model_events[index].payload["provider_call_index"]
                for index in (0, 1, 3, 4)
            ] == [1, 1, 2, 2]
            recovery_id = model_events[2].payload["recovery_id"]
            assert model_events[3].payload["recovery_id"] == recovery_id
            assert model_events[4].payload["recovery_id"] == recovery_id
            assert model_events[1].payload["error_code"] == "model_context_overflow"
            assert model_events[4].payload["outcome"] == "end_turn"
            assert events[-1].payload["usage"] == {
                "input_tokens": 12,
                "output_tokens": 3,
                "last_input_tokens": 12,
                "complete": False,
            }
            assert service.conversations is not None
            usage = service.conversations.provider_usage_for_run(recovered.run_id)
            assert [(item.call_index, item.status) for item in usage] == [
                (1, "incomplete"),
                (2, "complete"),
            ]
            boundary = service.conversations.read_context_projection_boundary(
                recovered.run_id
            )
            assert boundary is not None
            assert boundary.context_plan_digest == model_events[3].payload[
                "context_plan_digest"
            ]
            assert boundary.context_plan_digest != recovered.context_plan.reference.digest
            restored = await service.get_conversation(conversation_id)
            assert len(restored.turns) == 3
            assert restored.turns[-1].assistant_content == "recovered answer"
            assert recovered.recovery_context_plan is None
            assert recovered.recovery_history == ()
            assert recovered.recovery_prompt_sources is None
        finally:
            await service.close()

        restarted = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await restarted.initialize()
            conversation = await restarted.get_conversation(conversation_id)
            assert conversation.turns[-1].assistant_content == "recovered answer"
            identity = await restarted.resolve_run_identity(recovered.run_id)
            replay = await restarted.replay_run(
                recovered.run_id,
                after=0,
                limit=128,
                expected_identity=identity,
            )
            calls = replay.snapshot.document["model_calls"]
            assert isinstance(calls, list)
            assert [item["attempt"] for item in calls] == [0, 1]
            assert [item["provider_call_index"] for item in calls] == [1, 2]
            assert replay.snapshot.complete is True
        finally:
            await restarted.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("overflow_twice", "partial"),
    [(True, False), (False, True)],
)
def test_overflow_recovery_never_loops_or_retries_after_partial_output(
    tmp_path: Path,
    overflow_twice: bool,
    partial: bool,
) -> None:
    async def exercise() -> None:
        broker = _OverflowModelBroker(
            overflow_twice=overflow_twice,
            partial=partial,
        )
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation_id = await _prime_overflow_history(service)
            failed = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "overflow",
                    conversation_id=conversation_id,
                )
            )
            events = [
                event
                async for event in service.stream(failed.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.failed"
            recoveries = [
                event for event in events if event.kind == "model.recovery.started"
            ]
            requests = [
                event for event in events if event.kind == "model.request.started"
            ]
            if partial:
                assert recoveries == []
                assert len(requests) == 1
            else:
                assert len(recoveries) == 1
                assert len(requests) == 2
                assert requests[-1].payload["attempt"] == 1
            assert len(requests) <= 2
        finally:
            await service.close()

    asyncio.run(exercise())


def test_cancellation_between_overflow_and_recovery_fails_closed(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _OverflowModelBroker(wait_for_cancel=True)
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation_id = await _prime_overflow_history(service)
            cancelled = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "cancel overflow",
                    conversation_id=conversation_id,
                )
            )
            for _index in range(500):
                if len(broker.sessions) >= 3:
                    break
                await asyncio.sleep(0.01)
            overflow_session = broker.sessions[-1]
            assert isinstance(overflow_session, _OverflowModelSession)
            await asyncio.wait_for(overflow_session.entered.wait(), timeout=5)
            await service.cancel(cancelled.run_id)
            events = [
                event
                async for event in service.stream(cancelled.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.cancelled"
            assert not any(
                event.kind == "model.recovery.started" for event in events
            )
            assert sum(
                event.kind == "model.request.started" for event in events
            ) == 1
        finally:
            await service.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_file_write_waits_for_bound_operator_decision(
    tmp_path: Path,
    decision: str,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_WriteModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "create the approved file")
            )
            permission = None
            for _index in range(500):
                pending = await service.list_permission_requests()
                if pending:
                    permission = pending[0]
                    break
                await asyncio.sleep(0.01)
            assert permission is not None
            assert permission.capability_id == "file/write"
            preview = json.loads(permission.preview)
            assert preview["action"] == "file/write"
            assert preview["path"] == "created.txt"
            assert "+approved" in preview["diff"]
            resolved = await service.resolve_permission_request(
                permission.permission_id, decision
            )
            assert resolved.status == (
                "approved" if decision == "approve" else "denied"
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            assert service.capsule is not None
            target = service.capsule.data_root / "workspace" / "created.txt"
            assert target.exists() is (decision == "approve")
            if decision == "approve":
                assert target.read_text() == "approved\n"
            audit = await service.capability_audit_events(record.run_id)
            assert any(item.kind == "permission.resolved" for item in audit)
        finally:
            await service.close()

    asyncio.run(exercise())


def test_file_create_race_after_approval_never_clobbers_new_target(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_WriteModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "create without clobber")
            )
            permission = None
            for _index in range(500):
                pending = await service.list_permission_requests()
                if pending:
                    permission = pending[0]
                    break
                await asyncio.sleep(0.01)
            assert permission is not None
            assert service.capsule is not None
            target = service.capsule.data_root / "workspace" / "created.txt"
            target.write_text("racer")
            os.chmod(target, 0o600)
            await service.resolve_permission_request(
                permission.permission_id, "approve"
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            assert target.read_text() == "racer"
            finished = [
                event for event in events if event.kind == "tool.call.finished"
            ]
            assert finished[-1].payload["outcome"] == "failed"
        finally:
            await service.close()

    asyncio.run(exercise())


def test_allowlisted_command_waits_for_approval_and_cleans_singleton_runner(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_ExecModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "compile the trusted runtime")
            )
            permission = None
            for _index in range(500):
                pending = await service.list_permission_requests()
                if pending:
                    permission = pending[0]
                    break
                await asyncio.sleep(0.01)
            assert permission is not None
            assert permission.capability_id == "exec/run"
            preview = json.loads(permission.preview)
            assert preview["command_id"] == "runtime-compile"
            assert preview["sandbox"] == "singleton-landlock-seccomp-v1"
            assert preview["network"] == "denied"
            await service.resolve_permission_request(
                permission.permission_id, "approve"
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            finished = next(
                event for event in events if event.kind == "tool.call.finished"
            )
            result = json.loads(finished.payload["result"])
            child = json.loads(result["stdout"])
            assert result["exit_code"] == 0
            assert child["fork_denied"] is True
            assert child["network_denied"] is True
            audit = await service.capability_audit_events(record.run_id)
            assert [item.kind for item in audit] == [
                "permission.requested", "permission.resolved", "operation.intent",
                "operation.dispatched", "operation.outcome",
            ]
            assert service.capsule is not None
            run_root = service.capsule.runtime_root / "runs" / record.run_id
            assert not run_root.exists()
            assert not list(service.capsule.runtime_root.rglob("runner-*.pid"))
        finally:
            await service.close()

    asyncio.run(exercise())


def test_existing_file_edit_requires_same_run_full_read_then_approval(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_EditModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            assert service.capsule is not None
            target = service.capsule.data_root / "workspace" / "edit.txt"
            target.write_text("line before line\n")
            os.chmod(target, 0o600)
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "read and edit the file")
            )
            permission = None
            for _index in range(500):
                pending = await service.list_permission_requests()
                if pending:
                    permission = pending[0]
                    break
                await asyncio.sleep(0.01)
            assert permission is not None
            assert permission.capability_id == "file/edit"
            preview = json.loads(permission.preview)
            assert "-line before line" in preview["diff"]
            assert "+line after line" in preview["diff"]
            await service.resolve_permission_request(
                permission.permission_id, "approve"
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            assert target.read_text() == "line after line\n"
            requested = [
                event.payload["tool_id"]
                for event in events
                if event.kind == "tool.call.requested"
            ]
            assert requested == ["file/read_text", "file/edit"]
        finally:
            await service.close()

    asyncio.run(exercise())

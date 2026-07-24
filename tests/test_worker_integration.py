"""Real one-process-per-Run vertical slice without network or legacy services."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import (
    CONTEXT_RENDERER_VERSION,
    PROMPT_SECTION_REGISTRY_VERSION,
    ContextPlan,
    ModelProfile,
)
from agent_builder_v2.contracts import TERMINAL_KINDS, StartRunCommand
from agent_builder_v2.control import RunService
from agent_builder_v2.context_projection import ContextProjectionBoundary
from agent_builder_v2.ollama import (
    OUTPUT_TRUNCATION_MARKER,
    REPETITION_TRUNCATION_MARKER,
    OllamaBrokerError,
    OllamaCancelledError,
    OllamaFrame,
    OllamaQualification,
    OllamaRequestMetadata,
    OllamaToolResult,
    OllamaTransportAttempt,
)
from agent_builder_v2.query_engine import QueryEngineRegistry, QueryEngineRetiredError
from agent_builder_v2.sessions import (
    ConversationConflictError,
    ConversationNotFoundError,
)
from agent_builder_v2.semantic_summary import SemanticSummaryContent
from agent_builder_v2.semantic_summary_v2 import SemanticSummaryV2Snapshot
from agent_builder_v2.tools import toolset_digest


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class _FakeModelSession:
    def __init__(
        self, context_plan: ContextPlan, *, emit_transport_attempts: bool = False
    ) -> None:
        self.context_plan = context_plan
        self.emit_transport_attempts = emit_transport_attempts

    async def stream_turn(
        self,
        user_message: str,
        tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> object:
        iteration = len(tool_results) + 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
                    estimated_input_tokens=(
                        self.context_plan.estimated_input_tokens
                        + (123 if iteration == 2 else 0)
                    ),
                    request_bytes=512 + 64 * len(tool_results),
                    request_digest=("c" if iteration == 1 else "d") * 64,
                )
            )
        if self.emit_transport_attempts and on_transport_attempt is not None:
            await on_transport_attempt(  # type: ignore[operator]
                OllamaTransportAttempt(
                    attempt=1,
                    max_attempts=2,
                    phase="attempt_started",
                    outcome=None,
                    elapsed_ms=0,
                    first_frame_ms=None,
                )
            )
            first_frame_ms = 12 + iteration
            await on_transport_attempt(  # type: ignore[operator]
                OllamaTransportAttempt(
                    attempt=1,
                    max_attempts=2,
                    phase="attempt_finished",
                    outcome="first_frame_received",
                    elapsed_ms=first_frame_ms,
                    first_frame_ms=first_frame_ms,
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


class _TransportModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _FakeModelSession:
        assert max_tool_calls == 2
        assert context_plan.model_profile == self.qualification.model_profile
        self.plans.append(context_plan)
        return _FakeModelSession(context_plan, emit_transport_attempts=True)


class _SummaryV2ModelBroker(_FakeModelBroker):
    semantic_summary_enabled = True

    def __init__(self) -> None:
        super().__init__()
        self.summary_calls: list[dict[str, object]] = []

    async def summarize_v2(
        self,
        source: tuple[object, ...],
        *,
        aggregate_source: tuple[object, ...] | None = None,
        parent: SemanticSummaryV2Snapshot | None = None,
        model_id: str | None = None,
        is_cancelled: object = None,
    ) -> SemanticSummaryV2Snapshot:
        del model_id, is_cancelled
        aggregate = aggregate_source or source
        self.summary_calls.append({
            "delta": tuple(item.turn_id for item in source),
            "aggregate": tuple(item.turn_id for item in aggregate),
            "parent": parent.snapshot_digest if parent is not None else None,
        })
        return SemanticSummaryV2Snapshot.create(
            source_bundles=aggregate,  # type: ignore[arg-type]
            parent_snapshot_digest=(
                parent.snapshot_digest if parent is not None else None
            ),
            model_profile_digest=self.qualification.model_profile.profile_digest,
            renderer_version=CONTEXT_RENDERER_VERSION,
            section_registry_version=PROMPT_SECTION_REGISTRY_VERSION,
            content=SemanticSummaryContent(
                facts=("summary-v2-fact",),
                open_tasks=("continue",),
            ),
            provider_request_digest=f"{len(self.summary_calls):064x}",
            input_tokens=200,
            output_tokens=20,
        )


class _CancellableSummaryV2ModelBroker(_SummaryV2ModelBroker):
    def __init__(self) -> None:
        super().__init__()
        self.summary_entered = asyncio.Event()

    async def summarize_v2(
        self,
        source: tuple[object, ...],
        *,
        aggregate_source: tuple[object, ...] | None = None,
        parent: SemanticSummaryV2Snapshot | None = None,
        model_id: str | None = None,
        is_cancelled: object = None,
    ) -> SemanticSummaryV2Snapshot:
        del source, aggregate_source, parent, model_id
        self.summary_entered.set()
        assert callable(is_cancelled)
        while not is_cancelled():
            await asyncio.sleep(0.005)
        raise asyncio.CancelledError


class _FileReadModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
        tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        iteration = len(tool_results) + 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        iteration = len(tool_results) + 1
        request_tools = self.context_plan.tools if len(tool_results) < 2 else ()
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(request_tools),
                    tool_ids=tuple(spec.tool_id for spec in request_tools),
                    toolset_digest=toolset_digest(request_tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens
                    + 64 * len(tool_results),
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
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


class _DirectModelSession:
    def __init__(self, context_plan: ContextPlan) -> None:
        self.context_plan = context_plan

    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest="8" * 64,
                )
            )
        yield OllamaFrame("content", {"text": "A quiet direct answer."})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 1_472, "eval_count": 33},
            },
        )


class _DirectModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _DirectModelSession:
        assert max_tool_calls == 2
        return _DirectModelSession(context_plan)


class _TruncatedModelSession(_DirectModelSession):
    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest="9" * 64,
                )
            )
        yield OllamaFrame(
            "content", {"text": f"bounded prefix{OUTPUT_TRUNCATION_MARKER}"}
        )
        yield OllamaFrame(
            "stop",
            {
                "reason": "max_output",
                "usage": {"prompt_eval_count": 1_472, "eval_count": 2_048},
            },
        )


class _TruncatedModelBroker(_FakeModelBroker):
    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _TruncatedModelSession:
        assert max_tool_calls == 2
        return _TruncatedModelSession(context_plan)


class _RepetitionTruncatedModelSession(_DirectModelSession):
    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=0,
                    tool_ids=(),
                    toolset_digest=toolset_digest(()),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest="8" * 64,
                )
            )
        yield OllamaFrame(
            "content",
            {"text": f"bounded prefix{REPETITION_TRUNCATION_MARKER}"},
        )
        yield OllamaFrame(
            "stop",
            {"reason": "repetition_truncated", "usage": None},
        )


class _RepetitionTruncatedModelBroker(_FakeModelBroker):
    def __init__(self) -> None:
        super().__init__()
        self.qualification = replace(
            self.qualification,
            model_profile=replace(
                self.qualification.model_profile,
                supports_tools=False,
            ),
        )

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _RepetitionTruncatedModelSession:
        assert max_tool_calls == 2
        return _RepetitionTruncatedModelSession(context_plan)


class _RepetitionContinuationModelSession(_DirectModelSession):
    async def stream_turn(
        self,
        user_message: str,
        _tool_results: tuple[OllamaToolResult, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=0,
                    tool_ids=(),
                    toolset_digest=toolset_digest(()),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest=(
                        "7" if user_message == "write a joke" else "6"
                    )
                    * 64,
                )
            )
        if user_message == "write a joke":
            yield OllamaFrame(
                "content",
                {"text": f"bounded prefix{REPETITION_TRUNCATION_MARKER}"},
            )
            yield OllamaFrame(
                "stop",
                {"reason": "repetition_truncated", "usage": None},
            )
            return
        assert user_message == "ok"
        marker_count = sum(
            str(message.get("content", "")).count(
                REPETITION_TRUNCATION_MARKER
            )
            for message in self.context_plan.provider_messages()
        )
        assert marker_count == 1
        yield OllamaFrame("content", {"text": "continued after bounded history"})
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {"prompt_eval_count": 120, "eval_count": 8},
            },
        )


class _RepetitionContinuationModelBroker(_RepetitionTruncatedModelBroker):
    def __init__(self) -> None:
        super().__init__()
        self.plans: list[ContextPlan] = []

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _RepetitionContinuationModelSession:
        assert max_tool_calls == 2
        self.plans.append(context_plan)
        return _RepetitionContinuationModelSession(context_plan)


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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=1,
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
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


class _ScriptedModelBroker(_FakeModelBroker):
    def __init__(self, outcomes: tuple[str, ...]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.last_cancel_event: asyncio.Event | None = None

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> object:
        assert max_tool_calls == 2
        outcome = self.outcomes.pop(0)
        if outcome == "completed":
            return _FakeModelSession(context_plan)
        if outcome == "failed":
            return _BusyModelSession(context_plan)
        assert outcome == "cancelled"
        self.last_cancel_event = asyncio.Event()
        return _CancellableModelSession(context_plan, self.last_cancel_event)


class _OverflowModelSession:
    def __init__(
        self,
        context_plan: ContextPlan,
        *,
        overflow_twice: bool = False,
        partial: bool = False,
        wait_for_cancel: bool = False,
        tool_after_recovery: bool = False,
        fail_recovery_install: bool = False,
    ) -> None:
        self.context_plan = context_plan
        self.overflow_twice = overflow_twice
        self.partial = partial
        self.wait_for_cancel = wait_for_cancel
        self.tool_after_recovery = tool_after_recovery
        self.fail_recovery_install = fail_recovery_install
        self.entered = asyncio.Event()
        self.attempts = 0
        self.installed: ContextPlan | None = None

    def install_recovery_context(self, context_plan: ContextPlan) -> None:
        if self.fail_recovery_install:
            raise RuntimeError("simulated recovery session install failure")
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        self.attempts += 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=(2 if _tool_results else 1),
                    message_count=len(self.context_plan.provider_messages()),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
                    estimated_input_tokens=self.context_plan.estimated_input_tokens,
                    request_bytes=512,
                    request_digest=(
                        "e" if self.attempts == 1
                        else "f" if self.attempts == 2 else "9"
                    ) * 64,
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
        if self.tool_after_recovery and not _tool_results:
            yield OllamaFrame(
                "tool.use",
                {
                    "call_id": "recovery-tool-call",
                    "tool_id": "file/glob",
                    "arguments": {"pattern": "**/*", "max_results": 1},
                    "usage": {"prompt_eval_count": 12, "eval_count": 2},
                },
            )
            return
        if self.tool_after_recovery:
            assert len(_tool_results) == 1
            assert _tool_results[0].call_id == "recovery-tool-call"
            assert _tool_results[0].tool_id == "file/glob"
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
        tool_after_recovery: bool = False,
        fail_recovery_install: bool = False,
    ) -> None:
        super().__init__()
        self.overflow_twice = overflow_twice
        self.partial = partial
        self.wait_for_cancel = wait_for_cancel
        self.tool_after_recovery = tool_after_recovery
        self.fail_recovery_install = fail_recovery_install
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
                tool_after_recovery=self.tool_after_recovery,
                fail_recovery_install=self.fail_recovery_install,
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        iteration = len(tool_results) + 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        iteration = len(tool_results) + 1
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(self.context_plan.tools),
                    tool_ids=tuple(
                        spec.tool_id for spec in self.context_plan.tools
                    ),
                    toolset_digest=toolset_digest(self.context_plan.tools),
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
        on_transport_attempt: object = None,
    ) -> object:
        del on_transport_attempt
        iteration = len(tool_results) + 1
        request_tools = self.context_plan.tools if len(tool_results) < 2 else ()
        if on_request is not None:
            await on_request(  # type: ignore[operator]
                OllamaRequestMetadata(
                    iteration=iteration,
                    message_count=len(self.context_plan.provider_messages())
                    + 2 * len(tool_results),
                    tool_count=len(request_tools),
                    tool_ids=tuple(spec.tool_id for spec in request_tools),
                    toolset_digest=toolset_digest(request_tools),
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
                StartRunCommand(PROTOTYPE_AGENT_ID, "real process integration test")
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            assert events[0].kind == "run.started"
            assert events[-1].kind == "run.completed", events[-1].payload
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
            assert model_events[0].payload["tool_count"] == 7
            assert "agent/delegate" not in {
                item.tool_id for item in record.effective_tools
            }
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
            assert model_events[2].payload["tool_count"] == 7
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
            assert "real process integration test" not in boundary.to_json()
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


def test_transport_observer_events_are_canonical_persisted_and_replayable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_TransportModelBroker(),  # type: ignore[arg-type]
        )
        await first.initialize()
        try:
            record = await first.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "observe provider transport test")
            )
            live = [
                event
                async for event in first.stream(record.run_id)
                if event is not None
            ]
            assert live[-1].kind == "run.completed"
            attempts = [
                event for event in live
                if event.kind == "model.transport.attempt"
            ]
            assert len(attempts) == 4
            assert [event.payload["phase"] for event in attempts] == [
                "attempt_started",
                "attempt_finished",
                "attempt_started",
                "attempt_finished",
            ]
            assert [event.payload["request_id"] for event in attempts] == [
                "model-1", "model-1", "model-2", "model-2"
            ]
            assert [event.payload["provider_call_index"] for event in attempts] == [
                1, 1, 2, 2
            ]
            expected_fields = {
                "version",
                "request_id",
                "iteration",
                "provider_call_index",
                "attempt",
                "max_attempts",
                "phase",
                "outcome",
                "elapsed_ms",
                "first_frame_ms",
            }
            for event in attempts:
                assert event.durability == "durable"
                assert event.agent_id == record.agent_id
                assert event.conversation_id == record.conversation_id
                assert event.turn_id == record.turn_id
                assert event.run_id == record.run_id
                assert set(event.payload) == expected_fields
                assert len(json.dumps(event.payload).encode("utf-8")) < 512
                assert "observe provider transport test" not in json.dumps(event.payload)
            assert attempts[0].payload == {
                "version": "provider-transport-attempt-v1",
                "request_id": "model-1",
                "iteration": 1,
                "provider_call_index": 1,
                "attempt": 1,
                "max_attempts": 2,
                "phase": "attempt_started",
                "outcome": None,
                "elapsed_ms": 0,
                "first_frame_ms": None,
            }
            assert attempts[1].payload["outcome"] == "first_frame_received"
            assert attempts[1].payload["first_frame_ms"] == 13
            assert attempts[3].payload["first_frame_ms"] == 14
            assert first.conversations is not None
            persisted = first.conversations._connection.execute(
                """
                SELECT COUNT(*), MAX(length(CAST(envelope_json AS BLOB)))
                FROM events WHERE run_id = ? AND kind = 'model.transport.attempt'
                """,
                (record.run_id,),
            ).fetchone()
            assert persisted is not None
            assert persisted[0] == 4
            assert 0 < persisted[1] < 1_024
            identity = await first.resolve_run_identity(record.run_id)
            run_id = record.run_id
        finally:
            await first.close()

        restored = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_FakeModelBroker(),  # type: ignore[arg-type]
        )
        await restored.initialize()
        try:
            replay = await restored.replay_run(
                run_id,
                after=0,
                limit=128,
                expected_identity=identity,
            )
            replayed_attempts = [
                event for event in replay.events
                if event.kind == "model.transport.attempt"
            ]
            assert [event.to_dict() for event in replayed_attempts] == [
                event.to_dict() for event in attempts
            ]
            assert replay.snapshot.complete is True
            assert replay.has_more is False
        finally:
            await restored.close()

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
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "Search the workspace text files for SEARCH-01 and read the matching file.",
                )
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
            assert service.conversations is not None
            usage = service.conversations.provider_usage_for_run(record.run_id)
            assert len(usage) == 3
            full_toolset = record.context_plan.tools if record.context_plan else ()
            assert [item.toolset_digest for item in usage] == [
                toolset_digest(full_toolset),
                toolset_digest(full_toolset),
                toolset_digest(()),
            ]
            boundary = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert boundary is not None
            common_scope = {
                "profile_digest": boundary.model_profile_digest,
                "renderer_version": boundary.renderer_version,
                "policy_digest": boundary.compression_policy_digest,
            }
            assert service.conversations.recent_provider_calibration_samples(
                **common_scope,
                toolset_digest=toolset_digest(full_toolset),
            ) == tuple(
                (item.estimated_input_tokens, item.input_tokens)
                for item in usage[:2]
            )
            assert service.conversations.recent_provider_calibration_samples(
                **common_scope,
                toolset_digest=toolset_digest(()),
            ) == (
                (usage[2].estimated_input_tokens, usage[2].input_tokens),
            )
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
                StartRunCommand(PROTOTYPE_AGENT_ID, "restart replay integration test")
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


def test_soft_calibration_is_identical_after_restart_and_used_for_admission(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first_broker = _FakeModelBroker()
        first = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=first_broker,  # type: ignore[arg-type]
        )
        try:
            await first.initialize()
            seeded = await first.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "seed durable calibration test")
            )
            events = [
                event
                async for event in first.stream(seeded.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            conversation_id = seeded.conversation_id
            before_restart = await first.next_turn_preview(conversation_id)
            assert before_restart["availability"] == "available"
            assert before_restart["projection_mode"] == "conservative_tools"
            assert before_restart["chat_calibration_available"] is False
        finally:
            await first.close()

        restarted_broker = _FakeModelBroker()

        def forbidden_process_local_calibration(_plan: ContextPlan) -> object:
            raise AssertionError("Broker memory must not control admission")

        restarted_broker.soft_calibration_for = (  # type: ignore[attr-defined]
            forbidden_process_local_calibration
        )
        restarted = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=restarted_broker,  # type: ignore[arg-type]
        )
        try:
            await restarted.initialize()
            after_restart = await restarted.next_turn_preview(conversation_id)
            for field in (
                "availability",
                "fixed_context_tokens",
                "fixed_context_error_margin_tokens",
                "compact_before_user_tokens",
                "safe_user_tokens",
                "count_basis",
                "projection_mode",
                "chat_calibration_available",
            ):
                assert after_restart[field] == before_restart[field]

            admitted = await restarted.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                        "admit from durable calibration test",
                    conversation_id=conversation_id,
                )
            )
            assert admitted.context_plan is not None
            estimate = admitted.context_plan.soft_context_estimate
            assert estimate.availability == "available"
            assert estimate.basis == "provider-observed-calibration-v1"
            assert estimate.sample_count == 2
            admitted_events = [
                event
                async for event in restarted.stream(admitted.run_id)
                if event is not None
            ]
            assert admitted_events[-1].kind == "run.completed"
        finally:
            await restarted.close()

    asyncio.run(exercise())


def test_chat_calibration_is_not_promoted_to_conservative_tool_preview(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_DirectModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            chat = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "write a poem for around 500 words",
                )
            )
            assert chat.context_plan is not None
            assert chat.context_plan.tools == ()
            events = [
                event
                async for event in service.stream(chat.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"

            preview = await service.next_turn_preview(chat.conversation_id)
            assert preview["projection_mode"] == "conservative_tools"
            assert preview["availability"] == "unavailable"
            assert preview["stale_reason"] == "toolset_calibration_unavailable"
            assert preview["chat_calibration_available"] is True
            assert preview["fixed_context_tokens"] is None
            assert preview["safe_user_tokens"] is None
            assert preview["toolset_digest"] != (
                chat.context_plan.reference.toolset_digest
            )
            chat_only = preview["chat_only_projection"]
            assert isinstance(chat_only, dict)
            assert chat_only["version"] == "next-turn-chat-only-projection-v1"
            assert chat_only["availability"] == "available"
            assert chat_only["projection_mode"] == "chat_only"
            assert chat_only["fixed_context_tokens"] > 0
            assert chat_only["fixed_context_error_margin_tokens"] >= 256
            assert chat_only["safe_user_tokens"] > 0
            assert chat_only["compact_before_user_tokens"] > 0
            assert chat_only["operational_context_tokens"] == (
                chat.context_plan.model_profile.operational_context_tokens
            )
            assert chat_only["hard_input_tokens"] == (
                chat.context_plan.policy.hard_input_tokens
            )
            assert chat_only["toolset_digest"] == (
                chat.context_plan.reference.toolset_digest
            )
        finally:
            await service.close()

    asyncio.run(exercise())


def test_output_limit_commits_truncated_turn_with_machine_readable_terminal(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_TruncatedModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "bounded long-answer integration test",
                )
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed"
            assert events[-1].payload == {
                "reason": "max_output",
                "model_iterations": 1,
                "usage": {
                    "input_tokens": 1_472,
                    "output_tokens": 2_048,
                    "last_input_tokens": 1_472,
                    "complete": True,
                },
            }
            model_finished = next(
                event for event in events
                if event.kind == "model.response.finished"
            )
            assert model_finished.payload["outcome"] == "end_turn"
            assert model_finished.payload["usage_complete"] is True

            assert service.conversations is not None
            conversation = service.conversations.get_conversation(
                record.conversation_id
            )
            assert conversation.turns[-1].status == "completed"
            assert conversation.turns[-1].assistant_content == (
                f"bounded prefix{OUTPUT_TRUNCATION_MARKER}"
            )
            replayed = service.journal.events_for_run(record.run_id)
            assert replayed[-1]["payload"]["reason"] == "max_output"
            identity = await service.resolve_run_identity(record.run_id)
            replay = await service.replay_run(
                record.run_id,
                after=0,
                limit=128,
                expected_identity=identity,
            )
            assert replay.events[-1].payload["reason"] == "max_output"
            assert replay.snapshot.complete is True
        finally:
            await service.close()

    asyncio.run(exercise())


def test_repetition_truncation_commits_with_incomplete_provider_usage(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=_RepetitionTruncatedModelBroker(),  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "write a joke",
                )
            )
            events = [
                event
                async for event in service.stream(record.run_id)
                if event is not None
            ]

            model_finished = next(
                event
                for event in events
                if event.kind == "model.response.finished"
            )
            assert model_finished.payload == {
                "request_id": "model-1",
                "iteration": 1,
                "attempt": 0,
                "recovery_id": None,
                "provider_call_index": 1,
                "outcome": "repetition_truncated",
                "input_tokens": 0,
                "output_tokens": 0,
                "usage_complete": False,
                "error_code": None,
            }
            assert events[-1].kind == "run.completed"
            assert events[-1].payload == {
                "reason": "repetition_truncated",
                "model_iterations": 1,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "last_input_tokens": 0,
                    "complete": False,
                },
            }

            assert service.conversations is not None
            conversation = service.conversations.get_conversation(
                record.conversation_id
            )
            completed_turn = conversation.turns[-1]
            assert completed_turn.status == "completed"
            assert completed_turn.assistant_content.endswith(
                REPETITION_TRUNCATION_MARKER
            )
            usage = service.conversations.provider_usage_for_run(record.run_id)
            assert len(usage) == 1
            assert usage[0].status == "incomplete"
            assert usage[0].input_tokens is None
            assert usage[0].output_tokens is None
            snapshot = service.conversations.snapshot_for_turn(
                record.conversation_id
            )
            assert snapshot.completed_turn_contexts[-1].items[-1].content == (
                completed_turn.assistant_content
            )

            identity = await service.resolve_run_identity(record.run_id)
            replay = await service.replay_run(
                record.run_id,
                after=0,
                limit=128,
                expected_identity=identity,
            )
            assert replay.snapshot.complete is True
            assert replay.events[-1].payload["reason"] == "repetition_truncated"
        finally:
            await service.close()

    asyncio.run(exercise())


def test_repetition_completion_continues_from_exact_committed_history_after_restart(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first_broker = _RepetitionContinuationModelBroker()
        first = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=first_broker,  # type: ignore[arg-type]
        )
        try:
            await first.initialize()
            repeated = await first.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "write a joke")
            )
            repeated_events = [
                event
                async for event in first.stream(repeated.run_id)
                if event is not None
            ]
            assert repeated_events[-1].kind == "run.completed"
            assert repeated_events[-1].payload["reason"] == "repetition_truncated"
            conversation_id = repeated.conversation_id
            assert first.conversations is not None
            conversation = first.conversations.get_conversation(conversation_id)
            assert conversation.revision == 2
            assert conversation.turns[-1].status == "completed"
            assert conversation.turns[-1].assistant_content.count(
                REPETITION_TRUNCATION_MARKER
            ) == 1
            usage = first.conversations.provider_usage_for_run(repeated.run_id)
            assert len(usage) == 1
            assert usage[0].status == "incomplete"
            assert usage[0].input_tokens is None
            assert usage[0].output_tokens is None
            first_boundary = (
                first.conversations.read_context_projection_boundary(
                    repeated.run_id
                )
            )
            assert first_boundary is not None
            assert first.conversations.recent_provider_calibration_samples(
                profile_digest=first_boundary.model_profile_digest,
                renderer_version=first_boundary.renderer_version,
                toolset_digest=toolset_digest(()),
                policy_digest=first_boundary.compression_policy_digest,
            ) == ()
            preview = await first.next_turn_preview(conversation_id)
            assert preview["availability"] == "unavailable"
            assert preview["stale_reason"] == "incomplete_provider_usage"
            assert preview["conversation_revision"] == 2
            assert preview["fixed_context_tokens"] is None
            assert preview["safe_user_tokens"] is None
            assert preview["last_run_usage"] == {
                "input_tokens": 0,
                "output_tokens": 0,
                "provider_calls": 1,
                "complete": False,
            }
        finally:
            await first.close()

        second_broker = _RepetitionContinuationModelBroker()
        second = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=second_broker,  # type: ignore[arg-type]
        )
        try:
            await second.initialize()
            restored = await second.get_conversation(conversation_id)
            assert restored.revision == 2
            restarted_preview = await second.next_turn_preview(conversation_id)
            assert restarted_preview["availability"] == "unavailable"
            assert restarted_preview["stale_reason"] == (
                "incomplete_provider_usage"
            )
            assert restarted_preview["conversation_revision"] == 2

            continued = await second.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "ok",
                    conversation_id=conversation_id,
                )
            )
            assert continued.conversation_revision == 2
            assert continued.context_plan is not None
            assert continued.context_plan.history_message_count == 2
            marker_count = sum(
                str(message.get("content", "")).count(
                    REPETITION_TRUNCATION_MARKER
                )
                for message in continued.context_plan.provider_messages()
            )
            assert marker_count == 1
            continued_events = [
                event
                async for event in second.stream(continued.run_id)
                if event is not None
            ]
            assert continued_events[-1].kind == "run.completed"
            assert continued_events[-1].payload["reason"] == "end_turn"
            second_boundary = (
                second.conversations.read_context_projection_boundary(
                    continued.run_id
                )
                if second.conversations is not None
                else None
            )
            assert second_boundary is not None
            assert second_boundary.conversation_revision == 2
            assert second_boundary.history_source_digest == (
                continued.context_plan.history_source_digest
            )
            final = await second.get_conversation(conversation_id)
            assert [turn.status for turn in final.turns] == [
                "completed",
                "completed",
            ]
            assert final.turns[-1].assistant_content == (
                "continued after bounded history"
            )
            recovered_preview = await second.next_turn_preview(conversation_id)
            assert recovered_preview["availability"] == "available"
            assert recovered_preview["stale_reason"] is None
            assert recovered_preview["conversation_revision"] == 4
            assert continued.process is None
            assert second.capsule is not None
            assert not (
                second.capsule.runtime_root / "runs" / continued.run_id
            ).exists()
        finally:
            await second.close()

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
                "工作区测试第一轮：记住代号是青竹"
            )
            first_events = [
                event
                async for event in engine.stream(first.run_id)
                if event is not None
            ]
            assert first_events[-1].kind == "run.completed"

            preview = await service.next_turn_preview(
                conversation.conversation_id
            )
            assert preview["availability"] == "available"
            assert preview["conversation_revision"] == 2
            assert preview["fixed_context_tokens"] > 0
            assert preview["safe_user_tokens"] > 0
            assert preview["single_message_byte_limit"] == 8_192
            assert preview["last_run_usage"] == {
                "input_tokens": 18,
                "output_tokens": 6,
                "provider_calls": 2,
                "complete": True,
            }

            second = await engine.submit_message(
                "工作区测试第二轮：刚才的代号是什么？"
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
                "tool",
                "assistant",
                "user",
            ]
            assert second_messages[1]["content"] == "工作区测试第一轮：记住代号是青竹"
            assert second_messages[2]["tool_calls"][0]["function"]["name"] == "file_glob"
            assert second_messages[3]["tool_name"] == "file_glob"
            assert second_messages[3]["content"]
            assert second_messages[4]["content"] == (
                "broker result: 工作区测试第一轮：记住代号是青竹"
            )
            assert second_messages[-1]["content"] == "工作区测试第二轮：刚才的代号是什么？"

            restored = await engine.restore()
            assert [turn.status for turn in restored.turns] == [
                "completed",
                "completed",
            ]
            assert [turn.assistant_content for turn in restored.turns] == [
                "broker result: 工作区测试第一轮：记住代号是青竹",
                "broker result: 工作区测试第二轮：刚才的代号是什么？",
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
            first = await old_engine.submit_message("工作区测试：重启前的代号是青竹")
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
                "broker result: 工作区测试：重启前的代号是青竹"
            )

            second = await new_engine.submit_message("继续这个工作区测试会话")
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
                "tool",
                "assistant",
                "user",
            ]
            assert projected[1]["content"] == "工作区测试：重启前的代号是青竹"
            assert projected[2]["tool_calls"][0]["function"]["name"] == "file_glob"
            assert projected[3]["tool_name"] == "file_glob"
            assert projected[4]["content"] == (
                "broker result: 工作区测试：重启前的代号是青竹"
            )
            assert projected[5]["content"] == "继续这个工作区测试会话"
        finally:
            if second_registry is not None:
                await second_registry.close()
            await second_service.close()

    asyncio.run(exercise())


def test_twenty_turn_mixed_terminal_soak_survives_gateway_restart(
    tmp_path: Path,
) -> None:
    first_half = (
        "completed", "failed", "completed", "cancelled", "completed",
        "failed", "completed", "cancelled", "completed", "completed",
    )
    second_half = (
        "completed", "cancelled", "completed", "failed", "completed",
        "completed", "failed", "completed", "cancelled", "completed",
    )

    async def run_half(
        service: RunService,
        broker: _ScriptedModelBroker,
        outcomes: tuple[str, ...],
        conversation_id: str | None,
    ) -> str:
        for position, expected in enumerate(outcomes, start=1):
            if expected == "cancelled":
                broker.last_cancel_event = None
            record = await service.start(StartRunCommand(
                PROTOTYPE_AGENT_ID,
                f"mixed soak test turn {position}: {expected}",
                conversation_id=conversation_id,
            ))
            conversation_id = record.conversation_id
            if expected == "cancelled":
                for _index in range(500):
                    if broker.last_cancel_event is not None:
                        break
                    await asyncio.sleep(0.01)
                assert broker.last_cancel_event is not None
                await asyncio.wait_for(broker.last_cancel_event.wait(), timeout=5)
                await service.cancel(record.run_id)
            events = [
                event async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == f"run.{expected}"
        assert conversation_id is not None
        return conversation_id

    async def exercise() -> None:
        first_broker = _ScriptedModelBroker(first_half)
        first = RunService(
            tmp_path, SOURCE_ROOT, model_broker=first_broker  # type: ignore[arg-type]
        )
        try:
            await first.initialize()
            conversation_id = await run_half(
                first, first_broker, first_half, None
            )
        finally:
            await first.close()

        second_broker = _ScriptedModelBroker(second_half)
        second = RunService(
            tmp_path, SOURCE_ROOT, model_broker=second_broker  # type: ignore[arg-type]
        )
        try:
            await second.initialize()
            conversation_id = await run_half(
                second, second_broker, second_half, conversation_id
            )
            conversation = await second.get_conversation(conversation_id)
            assert len(conversation.turns) == 20
            assert [turn.status for turn in conversation.turns].count("completed") == 12
            assert [turn.status for turn in conversation.turns].count("failed") == 4
            assert [turn.status for turn in conversation.turns].count("cancelled") == 4
            assert second.conversations is not None
            snapshot = second.conversations.snapshot_for_turn(conversation_id)
            assert len(snapshot.completed_turn_contexts) == 12
            assert all(
                bundle.turn_id in {
                    turn.turn_id for turn in conversation.turns
                    if turn.status == "completed"
                }
                for bundle in snapshot.completed_turn_contexts
            )
            preview = await second.next_turn_preview(conversation_id)
            assert preview["availability"] == "available"
            assert preview["turn_count"] == 20
            assert preview["turns_remaining"] == 108
        finally:
            await second.close()

    asyncio.run(exercise())


async def _prime_overflow_history(service: RunService) -> str:
    first = await service.start(
        StartRunCommand(PROTOTYPE_AGENT_ID, "test " + "A" * 2_000)
    )
    first_events = [
        event async for event in service.stream(first.run_id) if event is not None
    ]
    assert first_events[-1].kind == "run.completed"
    second = await service.start(
        StartRunCommand(
            PROTOTYPE_AGENT_ID,
            "test " + "B" * 2_000,
            conversation_id=first.conversation_id,
        )
    )
    second_events = [
        event async for event in service.stream(second.run_id) if event is not None
    ]
    assert second_events[-1].kind == "run.completed"
    return first.conversation_id


def test_summary_v2_is_reused_across_turns_and_gateway_restart(
    tmp_path: Path,
) -> None:
    async def run_turn(
        service: RunService, message: str, conversation_id: str | None = None,
        *, compact: bool = False,
    ) -> str:
        record = await service.start(StartRunCommand(
            PROTOTYPE_AGENT_ID, message, conversation_id=conversation_id,
            compact=compact,
        ))
        events = [
            event async for event in service.stream(record.run_id)
            if event is not None
        ]
        assert events[-1].kind == "run.completed"
        return record.conversation_id

    async def exercise() -> None:
        broker = _SummaryV2ModelBroker()
        service = RunService(tmp_path, SOURCE_ROOT, model_broker=broker)  # type: ignore[arg-type]
        try:
            await service.initialize()
            conversation_id = await run_turn(service, "test " + "A" * 5_000)
            await run_turn(service, "test " + "B" * 5_000, conversation_id, compact=True)
            assert len(broker.summary_calls) == 1
            first_call = broker.summary_calls[0]
            assert service.conversations is not None
            generated = service.conversations.read_summary_projection(conversation_id)
            assert generated is not None
            assert generated.status == "generated"
            await run_turn(service, "test small follow-up", conversation_id, compact=True)
            assert len(broker.summary_calls) == 1
            stored = service.conversations.read_summary_projection(conversation_id)
            assert stored is not None
            assert stored.status == "reused"
            assert stored.snapshot is not None
            assert first_call["aggregate"] == stored.snapshot.source_turn_ids
        finally:
            await service.close()

        restarted_broker = _SummaryV2ModelBroker()
        restarted = RunService(
            tmp_path, SOURCE_ROOT, model_broker=restarted_broker  # type: ignore[arg-type]
        )
        try:
            await restarted.initialize()
            await run_turn(
                restarted, "test after restart", conversation_id, compact=True
            )
            assert restarted_broker.summary_calls == []
        finally:
            await restarted.close()

    asyncio.run(exercise())


def test_cancelling_real_summary_preparation_writes_no_projection_or_run(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _CancellableSummaryV2ModelBroker()
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            first = await service.start(StartRunCommand(
                PROTOTYPE_AGENT_ID, "test " + "A" * 5_000,
            ))
            first_events = [
                event async for event in service.stream(first.run_id)
                if event is not None
            ]
            assert first_events[-1].kind == "run.completed"
            before_run_ids = set(service.runs)
            before = await service.get_conversation(first.conversation_id)

            admission = asyncio.create_task(service.start(StartRunCommand(
                PROTOTYPE_AGENT_ID,
                "test " + "B" * 5_000,
                conversation_id=first.conversation_id,
                compact=True,
            )))
            await asyncio.wait_for(broker.summary_entered.wait(), timeout=0.5)
            status = await service.preparation_status(first.conversation_id)
            assert status is not None
            assert status["state"] == "preparing"
            assert status["stage"] == "summarizing_history"
            operation_id = status["operation_id"]
            assert isinstance(operation_id, str)

            result = await service.cancel_preparation(
                first.conversation_id, operation_id
            )
            assert result == {
                "version": "run-preparation-cancel-v1",
                "state": "cancellation_requested",
                "target": "preparation",
            }
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(admission, timeout=0.5)

            assert await service.preparation_status(first.conversation_id) is None
            after = await service.get_conversation(first.conversation_id)
            assert after.turns == before.turns
            assert set(service.runs) == before_run_ids
            assert service.conversations is not None
            assert service.conversations.read_summary_projection(
                first.conversation_id
            ) is None
        finally:
            await service.close()

    asyncio.run(exercise())


def test_summary_v2_updates_only_the_new_complete_turn_delta(
    tmp_path: Path,
) -> None:
    async def run_turn(
        service: RunService, message: str, conversation_id: str | None = None,
        *, compact: bool = False,
    ) -> str:
        record = await service.start(StartRunCommand(
            PROTOTYPE_AGENT_ID, message, conversation_id=conversation_id,
            compact=compact,
        ))
        events = [
            event async for event in service.stream(record.run_id)
            if event is not None
        ]
        assert events[-1].kind == "run.completed"
        return record.conversation_id

    async def exercise() -> None:
        broker = _SummaryV2ModelBroker()
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation_id = await run_turn(service, "test " + "A" * 5_000)
            await run_turn(service, "test " + "B" * 5_000, conversation_id, compact=True)
            assert len(broker.summary_calls) == 1
            first = broker.summary_calls[0]
            await run_turn(service, "test " + "C" * 5_000, conversation_id, compact=True)
            await run_turn(service, "test " + "D" * 5_000, conversation_id, compact=True)
            assert len(broker.summary_calls) == 2
            second = broker.summary_calls[1]
            assert second["parent"] is not None
            assert second["aggregate"][: len(first["aggregate"])] == first["aggregate"]
            assert second["delta"] == second["aggregate"][len(first["aggregate"]):]
            assert second["delta"]
        finally:
            await service.close()

    asyncio.run(exercise())


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
            assert recovered.active_context_plan is not None
            assert boundary.context_plan_digest == recovered.active_context_plan.reference.digest
            assert boundary.context_plan_digest != recovered.context_plan.reference.digest
            assert recovered.active_runtime_snapshot is not None
            assert recovered.active_runtime_snapshot.projection_reason == "overflow_recovery"
            restored = await service.get_conversation(conversation_id)
            assert len(restored.turns) == 3
            assert restored.turns[-1].assistant_content == "recovered answer"
            assert recovered.recovery_context_plan is None
            assert recovered.recovery_history == ()
            assert recovered.recovery_prompt_sources is None
            preview = await service.next_turn_preview(conversation_id)
            assert preview["availability"] == "available"
            assert preview["last_run_usage"] == {
                "input_tokens": 12,
                "output_tokens": 3,
                "provider_calls": 2,
                "complete": False,
            }
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
            preview = await restarted.next_turn_preview(conversation_id)
            assert preview["availability"] == "available"
            assert preview["conversation_revision"] == conversation.revision
        finally:
            await restarted.close()

    asyncio.run(exercise())


def test_overflow_recovery_keeps_active_plan_through_tool_and_final(
    tmp_path: Path,
) -> None:
    """The post-recovery Tool loop must never fall back to admission identity."""

    async def exercise() -> None:
        broker = _OverflowModelBroker(tool_after_recovery=True)
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation_id = await _prime_overflow_history(service)
            record = await service.start(StartRunCommand(
                PROTOTYPE_AGENT_ID,
                "recover, list files in the workspace with the bounded Tool, then finish",
                conversation_id=conversation_id,
            ))
            events = [
                event async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.completed", events[-1].payload
            recovery = next(
                event for event in events if event.kind == "model.recovery.started"
            )
            recovered_digest = recovery.payload["to_context_plan_digest"]
            requests = [
                event for event in events if event.kind == "model.request.started"
            ]
            assert len(requests) == 3
            assert requests[0].payload["attempt"] == 0
            assert [item.payload["attempt"] for item in requests[1:]] == [1, 0]
            assert all(
                item.payload["context_plan_digest"] == recovered_digest
                for item in requests[1:]
            )
            assert any(event.kind == "tool.call.finished" for event in events)
            assert record.active_context_plan is not None
            assert record.active_context_plan.reference.digest == recovered_digest
            assert service.conversations is not None
            boundary = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert boundary is not None
            assert boundary.context_plan_digest == recovered_digest
            bundles = service.conversations.snapshot_for_turn(
                conversation_id
            ).completed_turn_contexts
            latest = bundles[-1]
            assert [item.kind for item in latest.items] == [
                "user", "assistant_tool_use", "tool_result_receipt",
                "assistant_final",
            ]
            assert latest.items[1].call_id == "recovery-tool-call"
            assert latest.items[2].call_id == "recovery-tool-call"
        finally:
            await service.close()

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
            preview = await service.next_turn_preview(conversation_id)
            assert preview["availability"] == "available"
            assert preview["stale_reason"] is None
            assert preview["last_run_usage"]["complete"] is True
        finally:
            await service.close()

    asyncio.run(exercise())


def test_recovery_session_install_failure_keeps_the_admission_boundary(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _OverflowModelBroker(fail_recovery_install=True)
        service = RunService(
            tmp_path, SOURCE_ROOT, model_broker=broker  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            conversation_id = await _prime_overflow_history(service)
            record = await service.start(StartRunCommand(
                PROTOTYPE_AGENT_ID, "install fault",
                conversation_id=conversation_id,
            ))
            assert service.conversations is not None
            admission = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert admission is not None
            events = [
                event async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.failed"
            assert not any(
                event.kind == "model.recovery.started" for event in events
            )
            boundary = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert boundary == admission
            assert record.active_context_plan == record.context_plan
        finally:
            await service.close()

    asyncio.run(exercise())


def test_recovery_sql_fault_rolls_back_boundary_and_transition_event(
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
            assert service.conversations is not None
            service.conversations._connection.execute(
                """
                CREATE TEMP TRIGGER fail_recovery_transition
                BEFORE INSERT ON events
                WHEN NEW.kind = 'model.recovery.started'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated recovery SQL fault');
                END
                """
            )
            service.conversations._connection.commit()
            record = await service.start(StartRunCommand(
                PROTOTYPE_AGENT_ID, "transaction fault",
                conversation_id=conversation_id,
            ))
            admission = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert admission is not None
            events = [
                event async for event in service.stream(record.run_id)
                if event is not None
            ]
            assert events[-1].kind == "run.failed"
            assert not any(
                event.kind == "model.recovery.started" for event in events
            )
            boundary = service.conversations.read_context_projection_boundary(
                record.run_id
            )
            assert boundary == admission
            assert service.journal is not None
            durable = service.journal.events_for_run(record.run_id)
            assert not any(
                event.get("kind") == "model.recovery.started" for event in durable
            )
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
            preview = await service.next_turn_preview(conversation_id)
            assert preview["availability"] == "available"
            assert preview["stale_reason"] is None
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
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "create the approved workspace file created.txt",
                )
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
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "create workspace file created.txt without clobbering",
                )
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

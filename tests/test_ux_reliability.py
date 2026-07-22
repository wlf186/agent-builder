"""Cross-layer regressions for UX reliability and long-context recovery.

These tests deliberately use deterministic brokers, checkout-local temporary
state, and a local static HTTP fixture.  They must never contact the qualified
Ollama endpoint or write production ``data/``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import (
    CONTEXT_RENDERER_VERSION,
    CompressionPolicy,
    ContextCompiler,
    ContextPlan,
    ConversationMessage,
    ModelProfile,
)
from agent_builder_v2.context_counts import CountScope, SoftContextCalibration
from agent_builder_v2.contracts import EventEnvelope, StartRunCommand, TERMINAL_KINDS
from agent_builder_v2.control import RunService
from agent_builder_v2.ollama import (
    OllamaBrokerError,
    OllamaFrame,
    OllamaQualification,
    OllamaRequestMetadata,
    OllamaTransportAttempt,
)
from agent_builder_v2.replay import (
    MODEL_BOUNDARY_FEATURE,
    MULTI_TOOL_LOOP_FEATURE,
    OVERFLOW_RECOVERY_FEATURE,
    ReplayCorruptionError,
    project_durable_run,
)
from agent_builder_v2.sessions import ConversationStore, DATABASE_NAME
from agent_builder_v2.tools import prototype_tool_specs, toolset_digest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
STATIC = SOURCE_ROOT / "agent_builder_v2" / "static"
CHROMIUM = shutil.which("chromium")
CONVERSATION_ID = "1" * 32
TURN_ID = "2" * 32
RUN_ID = "3" * 32


def _profile(
    operational_tokens: int = 32_768,
    output_tokens: int = 2_048,
    *,
    supports_tools: bool = True,
) -> ModelProfile:
    return ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest="a" * 64,
        native_context_tokens=operational_tokens,
        operational_context_tokens=operational_tokens,
        max_output_tokens=output_tokens,
        profile_source="ux-reliability-test",
        supports_tools=supports_tools,
    )


def _plan() -> ContextPlan:
    return ContextCompiler().compile(
        "canonical transport test",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )


def _event(
    seq: int,
    kind: str,
    payload: dict[str, object],
    *,
    conversation_id: str = CONVERSATION_ID,
    turn_id: str = TURN_ID,
    run_id: str = RUN_ID,
) -> EventEnvelope:
    event_id = hashlib.sha256(
        f"{run_id}:{seq}:{kind}".encode("ascii")
    ).hexdigest()[:32]
    return EventEnvelope(
        event_id=event_id,
        agent_id=PROTOTYPE_AGENT_ID,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        seq=seq,
        occurred_at=f"2026-07-22T00:00:00.{seq:03d}Z",
        kind=kind,
        durability="durable",
        payload=payload,
    )


def _started_payload(plan: ContextPlan, *, boundaries: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "prototype": True,
        "model": plan.model_profile.model,
        "visible_tools": [spec.tool_id for spec in plan.tools],
        "sandbox": "harness-v2-worker-v1",
        "context_plan": plan.public_metadata(),
    }
    if boundaries:
        payload["protocol_features"] = [
            MODEL_BOUNDARY_FEATURE,
            MULTI_TOOL_LOOP_FEATURE,
            OVERFLOW_RECOVERY_FEATURE,
        ]
    return payload


def _request_payload(plan: ContextPlan) -> dict[str, object]:
    return {
        "request_id": "model-1",
        "iteration": 1,
        "attempt": 0,
        "recovery_id": None,
        "provider_call_index": 1,
        "context_plan_id": plan.reference.plan_id,
        "context_plan_digest": plan.reference.digest,
        "request_digest": "b" * 64,
        "request_bytes": 512,
        "estimated_input_tokens": plan.estimated_input_tokens,
        "message_count": len(plan.provider_messages()),
        "tool_count": len(plan.tools),
        "tool_result_call_ids": [],
    }


def _transport_payload(
    attempt: int,
    phase: str,
    *,
    outcome: str | None = None,
    elapsed_ms: int = 0,
    first_frame_ms: int | None = None,
) -> dict[str, object]:
    return {
        "version": "provider-transport-attempt-v1",
        "request_id": "model-1",
        "iteration": 1,
        "provider_call_index": 1,
        "attempt": attempt,
        "max_attempts": 2,
        "phase": phase,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "first_frame_ms": first_frame_ms,
    }


def _canonical_transport_events() -> tuple[EventEnvelope, ...]:
    plan = _plan()
    input_tokens = min(plan.estimated_input_tokens, plan.policy.hard_input_tokens)
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": 10,
        "last_input_tokens": input_tokens,
        "complete": True,
    }
    return (
        _event(1, "run.started", _started_payload(plan)),
        _event(2, "model.request.started", _request_payload(plan)),
        _event(3, "model.transport.attempt", _transport_payload(1, "attempt_started")),
        _event(
            4,
            "model.transport.attempt",
            _transport_payload(
                1,
                "attempt_finished",
                outcome="first_frame_timeout",
                elapsed_ms=60,
            ),
        ),
        _event(5, "model.transport.attempt", _transport_payload(2, "attempt_started")),
        _event(
            6,
            "model.transport.attempt",
            _transport_payload(
                2,
                "attempt_finished",
                outcome="first_frame_received",
                elapsed_ms=7,
                first_frame_ms=7,
            ),
        ),
        _event(
            7,
            "model.response.finished",
            {
                "request_id": "model-1",
                "iteration": 1,
                "attempt": 0,
                "recovery_id": None,
                "provider_call_index": 1,
                "outcome": "end_turn",
                "input_tokens": input_tokens,
                "output_tokens": 10,
                "usage_complete": True,
                "error_code": None,
            },
        ),
        _event(
            8,
            "assistant.block.started",
            {"block_id": "answer", "block_type": "content"},
        ),
        _event(
            9,
            "assistant.block.finished",
            {"block_id": "answer", "content": "ok"},
        ),
        _event(
            10,
            "run.completed",
            {"reason": "end_turn", "model_iterations": 1, "usage": usage},
        ),
    )


def test_transport_attempts_are_canonical_ordered_and_content_free() -> None:
    events = _canonical_transport_events()
    snapshot, gaps = project_durable_run(events)

    assert gaps == ()
    assert snapshot.complete is True
    assert snapshot.document["terminal"]["kind"] == "run.completed"
    attempts = [
        event.payload for event in events if event.kind == "model.transport.attempt"
    ]
    assert [(item["attempt"], item["phase"]) for item in attempts] == [
        (1, "attempt_started"),
        (1, "attempt_finished"),
        (2, "attempt_started"),
        (2, "attempt_finished"),
    ]
    assert all(
        set(item)
        == {
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
        for item in attempts
    )
    encoded = json.dumps(attempts, ensure_ascii=False, sort_keys=True)
    assert "iollama" not in encoded
    assert "11434" not in encoded
    assert "canonical transport test" not in encoded
    assert "endpoint" not in encoded
    assert "prompt" not in encoded


@pytest.mark.parametrize(
    "tamper",
    (
        "extra_endpoint",
        "attempt_out_of_order",
        "first_frame_mismatch",
        "response_while_attempt_open",
        "unknown_outcome",
    ),
)
def test_transport_attempt_replay_tamper_fails_closed(tamper: str) -> None:
    events = list(_canonical_transport_events())
    if tamper == "extra_endpoint":
        events[2] = replace(
            events[2], payload={**events[2].payload, "endpoint": "http://secret"}
        )
    elif tamper == "attempt_out_of_order":
        events[4] = replace(
            events[4], payload={**events[4].payload, "attempt": 1}
        )
    elif tamper == "first_frame_mismatch":
        events[5] = replace(
            events[5], payload={**events[5].payload, "first_frame_ms": 6}
        )
    elif tamper == "response_while_attempt_open":
        events[3] = replace(
            events[3],
            kind="model.response.finished",
            payload=events[6].payload,
        )
    elif tamper == "unknown_outcome":
        events[3] = replace(
            events[3], payload={**events[3].payload, "outcome": "secret_failure"}
        )
    else:  # pragma: no cover - protects the parametrized fixture itself
        raise AssertionError(tamper)

    with pytest.raises(ReplayCorruptionError):
        project_durable_run(tuple(events))


class _DeterministicModelSession:
    def __init__(
        self,
        context_plan: ContextPlan,
        *,
        response: str = "deterministic answer",
        response_chunks: int = 1,
        failure_code: str | None = None,
        emit_transport_retry: bool = False,
    ) -> None:
        self.context_plan = context_plan
        self.response = response
        self.response_chunks = response_chunks
        self.failure_code = failure_code
        self.emit_transport_retry = emit_transport_retry

    async def stream_turn(
        self,
        _user_message: str,
        _tool_results: tuple[object, ...] = (),
        _is_cancelled: object = None,
        on_request: object = None,
        on_transport_attempt: object = None,
    ) -> Any:
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
        if self.emit_transport_retry and on_transport_attempt is not None:
            for observation in (
                OllamaTransportAttempt(1, 2, "attempt_started", None, 0, None),
                OllamaTransportAttempt(
                    1, 2, "attempt_finished", "first_frame_timeout", 60, None
                ),
                OllamaTransportAttempt(2, 2, "attempt_started", None, 0, None),
                OllamaTransportAttempt(
                    2, 2, "attempt_finished", "first_frame_received", 7, 7
                ),
            ):
                await on_transport_attempt(observation)  # type: ignore[operator]
        if self.failure_code is not None:
            if False:  # pragma: no cover - preserve async-generator semantics
                yield OllamaFrame("content", {"text": "unreachable"})
            raise OllamaBrokerError(
                self.failure_code,
                "deterministic trusted model failure",
                retryable=True,
            )
        chunk_size = max(
            1,
            (len(self.response) + self.response_chunks - 1) // self.response_chunks,
        )
        for offset in range(0, len(self.response), chunk_size):
            yield OllamaFrame(
                "content", {"text": self.response[offset : offset + chunk_size]}
            )
        yield OllamaFrame(
            "stop",
            {
                "reason": "end_turn",
                "usage": {
                    "prompt_eval_count": self.context_plan.estimated_input_tokens,
                    "eval_count": 1,
                },
            },
        )


class _DeterministicModelBroker:
    semantic_summary_enabled = False

    def __init__(
        self,
        profile: ModelProfile,
        *,
        response: str = "deterministic answer",
        response_chunks: int = 1,
        failure_code: str | None = None,
        emit_transport_retry: bool = False,
    ) -> None:
        self.response = response
        self.response_chunks = response_chunks
        self.failure_code = failure_code
        self.emit_transport_retry = emit_transport_retry
        self.plans: list[ContextPlan] = []
        self.qualification = OllamaQualification(
            version="test",
            model=profile.model,
            digest=profile.model_digest,
            size=1,
            address="127.0.0.1",
            model_profile=profile,
        )

    def soft_calibration_for(self, _plan: ContextPlan) -> None:
        # A fresh broker deliberately has no process-local observations.  The
        # RunService must recover admission calibration from durable state.
        return None

    def new_run(
        self, context_plan: ContextPlan, *, max_tool_calls: int = 2
    ) -> _DeterministicModelSession:
        assert max_tool_calls == 2
        self.plans.append(context_plan)
        return _DeterministicModelSession(
            context_plan,
            response=self.response,
            response_chunks=self.response_chunks,
            failure_code=self.failure_code,
            emit_transport_retry=self.emit_transport_retry,
        )

    async def close(self) -> None:
        return None


def test_main_model_circuit_failure_converges_to_one_durable_terminal(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _DeterministicModelBroker(
            _profile(),
            failure_code="model_temporarily_unhealthy",
        )
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "fail fast through circuit")
            )
            events = [event async for event in service.stream(record.run_id) if event]

            terminals = [event for event in events if event.kind in TERMINAL_KINDS]
            assert [event.kind for event in terminals] == ["run.failed"]
            assert terminals[0].payload["code"] == "model_temporarily_unhealthy"
            assert terminals[0].payload["retryable"] is True
            assert [
                event.kind for event in events if event.kind.startswith("model.")
            ] == ["model.request.started", "model.response.finished"]
            assert all(event.kind != "model.transport.attempt" for event in events)

            restored = await service.get_conversation(record.conversation_id)
            assert restored.turns[-1].status == "failed"
            assert restored.turns[-1].terminal is not None
            assert restored.turns[-1].terminal.code == "model_temporarily_unhealthy"
            assert restored.turns[-1].terminal.retryable is True
            assert service.conversations is not None
            journal_state = service.conversations.get_run_journal_state(record.run_id)
            assert journal_state.terminal_kind == "run.failed"
            assert journal_state.usage_complete is False
        finally:
            await service.close()

    asyncio.run(exercise())


def test_control_plane_persists_transport_attempts_in_canonical_order(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        broker = _DeterministicModelBroker(
            _profile(),
            emit_transport_retry=True,
        )
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "observe transport attempts")
            )
            events = [event async for event in service.stream(record.run_id) if event]
            model_events = [event for event in events if event.kind.startswith("model.")]
            assert [event.kind for event in model_events] == [
                "model.request.started",
                "model.transport.attempt",
                "model.transport.attempt",
                "model.transport.attempt",
                "model.transport.attempt",
                "model.response.finished",
            ]
            attempts = model_events[1:-1]
            assert [
                (event.payload["attempt"], event.payload["phase"])
                for event in attempts
            ] == [
                (1, "attempt_started"),
                (1, "attempt_finished"),
                (2, "attempt_started"),
                (2, "attempt_finished"),
            ]
            assert all(
                "endpoint" not in event.payload and "prompt" not in event.payload
                for event in attempts
            )
            assert events[-1].kind == "run.completed"
            assert sum(event.kind in TERMINAL_KINDS for event in events) == 1
            assert service.conversations is not None
            restored = service.conversations.read_run_snapshot(record.run_id)
            assert restored is not None
            assert restored.complete is True
        finally:
            await service.close()

    asyncio.run(exercise())


def test_toolless_model_profile_keeps_terminal_replay_available(
    tmp_path: Path,
) -> None:
    """Tools-off qualification is a real profile, not a replay downgrade."""

    async def exercise() -> None:
        broker = _DeterministicModelBroker(
            _profile(supports_tools=False),
            response="tool-free answer",
        )
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(PROTOTYPE_AGENT_ID, "answer without tools")
            )
            events = [event async for event in service.stream(record.run_id) if event]
            assert events[-1].kind == "run.completed"
            assert sum(event.kind in TERMINAL_KINDS for event in events) == 1
            assert record.context_plan.tools == ()
            assert service.conversations is not None
            journal_state = service.conversations.get_run_journal_state(record.run_id)
            assert journal_state.availability == "full"
            assert journal_state.terminal_kind == "run.completed"
            replay = service.conversations.read_run_snapshot(record.run_id)
            assert replay is not None
            assert replay.complete is True
        finally:
            await service.close()

    asyncio.run(exercise())


def _calibration(
    profile: ModelProfile, tools: tuple[object, ...]
) -> SoftContextCalibration:
    policy = CompressionPolicy.for_profile(profile)
    return SoftContextCalibration(
        scope=CountScope(
            profile_digest=profile.profile_digest,
            renderer_version=CONTEXT_RENDERER_VERSION,
            toolset_digest=toolset_digest(tools),  # type: ignore[arg-type]
            policy_digest=policy.policy_digest,
        ),
        ratio_parts_per_million=1_000_000,
        error_parts_per_million=0,
        error_floor_tokens=0,
        sample_count=1,
    )


@pytest.mark.parametrize(
    ("window_tokens", "output_tokens"),
    ((8_192, 512), (16_384, 1_024), (32_768, 2_048)),
)
@pytest.mark.parametrize("tools_enabled", (False, True))
@pytest.mark.parametrize(
    ("trigger_offset", "should_compact"),
    ((-1, False), (0, True), (1, True)),
)
def test_context_profiles_share_exact_soft_trigger_semantics(
    window_tokens: int,
    output_tokens: int,
    tools_enabled: bool,
    trigger_offset: int,
    should_compact: bool,
) -> None:
    profile = _profile(window_tokens, output_tokens)
    tools = prototype_tool_specs() if tools_enabled else ()
    compiler = ContextCompiler()
    minimal_history = (
        ConversationMessage("4" * 32, "user", "u"),
        ConversationMessage("5" * 32, "assistant", "a"),
    )
    base = compiler.compile(
        ".",
        history=minimal_history,
        model_profile=profile,
        tools=tools,
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    padding = base.policy.compact_at_tokens - base.estimated_input_tokens + trigger_offset
    assert padding > 0
    history = (
        minimal_history[0],
        ConversationMessage("5" * 32, "assistant", "a" + "x" * padding),
    )

    plan = compiler.compile(
        ".",
        history=history,
        model_profile=profile,
        tools=tools,
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        soft_calibration=_calibration(profile, tools),
    )

    assert (plan.windowing_strategy != "full") is should_compact
    assert plan.soft_context_estimate.availability == "available"
    assert plan.model_profile.operational_context_tokens == window_tokens


def test_forty_turn_history_remains_ordered_in_the_context_projection() -> None:
    history = tuple(
        message
        for position in range(1, 41)
        for message in (
            ConversationMessage(
                f"{position * 2 - 1:032x}", "user", f"question-{position}"
            ),
            ConversationMessage(
                f"{position * 2:032x}", "assistant", f"answer-{position}"
            ),
        )
    )

    plan = ContextCompiler().compile(
        "question-41",
        history=history,
        model_profile=_profile(),
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    assert plan.history_message_count == 80
    assert plan.included_history_message_count == 80
    assert plan.windowing_strategy == "full"
    provider_messages = plan.provider_messages()
    assert [item["role"] for item in provider_messages[1:-1]] == [
        role for _position in range(40) for role in ("user", "assistant")
    ]
    assert provider_messages[1]["content"] == "question-1"
    assert provider_messages[-2]["content"] == "answer-40"
    assert provider_messages[-1] == {"role": "user", "content": "question-41"}


@pytest.mark.parametrize(
    ("word_count", "response_chunks"),
    ((500, 25), (1_500, 75)),
)
def test_long_assistant_output_commits_without_per_chunk_disk_writes_or_residuals(
    tmp_path: Path,
    word_count: int,
    response_chunks: int,
) -> None:
    response = " ".join("verse" for _index in range(word_count))

    async def exercise() -> None:
        broker = _DeterministicModelBroker(
            _profile(output_tokens=4_096),
            response=response,
            response_chunks=response_chunks,
        )
        service = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=broker,  # type: ignore[arg-type]
        )
        try:
            await service.initialize()
            record = await service.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    f"write a poem for around {word_count} words",
                )
            )
            events = [event async for event in service.stream(record.run_id) if event]

            assert events[-1].kind == "run.completed"
            assert sum(
                event.kind == "assistant.block.delta" for event in events
            ) > 1
            conversation = await service.get_conversation(record.conversation_id)
            assert conversation.turns[-1].assistant_content == response
            assert len(response.split()) == word_count

            assert service.conversations is not None
            snapshot = service.conversations.snapshot_for_turn(record.conversation_id)
            assert snapshot.committed_history[-1].content == response
            followup = ContextCompiler().compile(
                "continue",
                model_profile=record.context_plan.model_profile,  # type: ignore[union-attr]
                tools=(),
                agent_id=PROTOTYPE_AGENT_ID,
                capsule_generation=1,
                completed_turns=snapshot.completed_turn_contexts,
            )
            assert any(
                message.get("role") == "assistant"
                and message.get("content") == response
                for message in followup.provider_messages()
            )

            assert service.journal is not None
            durable = service.journal.events_for_run(record.run_id)
            assert all(
                item["kind"] != "assistant.block.delta" for item in durable
            )
            journal_state = service.conversations.get_run_journal_state(record.run_id)
            assert journal_state.event_count == len(durable)
            assert len(durable) < len(events)

            assert service.capsule is not None
            run_root = service.capsule.runtime_root / "runs" / record.run_id
            assert not run_root.exists()
            assert not list(service.capsule.runtime_root.rglob("worker.pid"))
        finally:
            await service.close()

    asyncio.run(exercise())


def _database(tmp_path: Path) -> Path:
    root = tmp_path / "state" / PROTOTYPE_AGENT_ID
    root.mkdir(parents=True, mode=0o700)
    return root / DATABASE_NAME


def test_failed_and_cancelled_turns_never_enter_followup_context(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), PROTOTYPE_AGENT_ID)
    conversation_id = "6" * 32
    plan = _plan()
    try:
        store.create_conversation(conversation_id=conversation_id)

        completed_turn, completed_run = "7" * 32, "8" * 32
        snapshot = store.snapshot_for_turn(conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=completed_turn,
            run_id=completed_run,
            user_content="committed user",
            expected_revision=snapshot.revision,
            started_event=_event(
                1,
                "run.started",
                _started_payload(plan, boundaries=False),
                conversation_id=conversation_id,
                turn_id=completed_turn,
                run_id=completed_run,
            ),
        )
        store.finalize_completed(
            completed_run,
            "committed assistant",
            _event(
                2,
                "run.completed",
                {
                    "reason": "end_turn",
                    "model_iterations": 1,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "last_input_tokens": 0,
                        "complete": True,
                    },
                },
                conversation_id=conversation_id,
                turn_id=completed_turn,
                run_id=completed_run,
            ),
        )

        for index, status in enumerate(("failed", "cancelled"), start=9):
            turn_id = f"{index:032x}"
            run_id = f"{index + 10:032x}"
            snapshot = store.snapshot_for_turn(conversation_id)
            assert [item.content for item in snapshot.committed_history] == [
                "committed user",
                "committed assistant",
            ]
            store.begin_turn(
                conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                user_content=f"must stay out: {status}",
                expected_revision=snapshot.revision,
                started_event=_event(
                    1,
                    "run.started",
                    _started_payload(plan, boundaries=False),
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    run_id=run_id,
                ),
            )
            store.finalize_noncompleted(run_id, status)  # type: ignore[arg-type]

        followup = store.snapshot_for_turn(conversation_id)
        assert [item.content for item in followup.committed_history] == [
            "committed user",
            "committed assistant",
        ]
        assert all("must stay out" not in item.content for item in followup.committed_history)

        followup_turn, followup_run = "d" * 32, "e" * 32
        continued = store.begin_turn(
            conversation_id,
            turn_id=followup_turn,
            run_id=followup_run,
            user_content="continue after both terminal failures",
            expected_revision=followup.revision,
            started_event=_event(
                1,
                "run.started",
                _started_payload(plan, boundaries=False),
                conversation_id=conversation_id,
                turn_id=followup_turn,
                run_id=followup_run,
            ),
        )
        assert [item.content for item in continued.committed_history] == [
            "committed user",
            "committed assistant",
        ]
        store.finalize_completed(
            followup_run,
            "continued assistant",
            _event(
                2,
                "run.completed",
                {
                    "reason": "end_turn",
                    "model_iterations": 1,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "last_input_tokens": 0,
                        "complete": True,
                    },
                },
                conversation_id=conversation_id,
                turn_id=followup_turn,
                run_id=followup_run,
            ),
        )
        final_snapshot = store.snapshot_for_turn(conversation_id)
        assert [item.content for item in final_snapshot.committed_history] == [
            "committed user",
            "committed assistant",
            "continue after both terminal failures",
            "continued assistant",
        ]
    finally:
        store.close()


def test_restart_reuses_durable_calibration_for_preview_and_admission(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        profile = _profile(8_192, 512)
        # The qualified release ToolSet already puts the fixed request close to
        # the 8K soft threshold.  A bounded 500-byte answer leaves the next
        # projection above soft trigger but below hard admission, which is the
        # exact restart-sensitive region this regression needs.
        first_broker = _DeterministicModelBroker(profile, response="x" * 500)
        first = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=first_broker,  # type: ignore[arg-type]
        )
        await first.initialize()
        try:
            initial = await first.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                    "inspect workspace to seed durable calibration test",
                )
            )
            initial_events = [event async for event in first.stream(initial.run_id) if event]
            assert initial_events[-1].kind == "run.completed"
            conversation_id = initial.conversation_id
            assert initial.context_plan.windowing_strategy == "full"
        finally:
            await first.close()

        # A new broker deliberately starts with an empty memory-only registry.
        second_broker = _DeterministicModelBroker(profile, response="after restart")
        second = RunService(
            tmp_path,
            SOURCE_ROOT,
            model_broker=second_broker,  # type: ignore[arg-type]
        )
        await second.initialize()
        try:
            preview = await second.next_turn_preview(conversation_id)
            assert preview["availability"] == "available", (
                preview.get("stale_reason"),
                preview.get("count_basis"),
                preview.get("projection_strategy"),
            )
            assert preview["stale_reason"] is None
            assert preview["projection_strategy"] != "full"

            followup = await second.start(
                StartRunCommand(
                    PROTOTYPE_AGENT_ID,
                        "inspect workspace next",
                    conversation_id=conversation_id,
                )
            )
            followup_events = [
                event async for event in second.stream(followup.run_id) if event
            ]
            assert followup_events[-1].kind == "run.completed"
            assert followup.context_plan.windowing_strategy == preview["projection_strategy"]
            assert followup.context_plan.windowing_strategy != "full"
        finally:
            await second.close()

    asyncio.run(exercise())


_UX_BROWSER_TEST = r"""
const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

window.setTimeout(async () => {
  const outcomes = {};
  const sessionId = "11111111111111111111111111111111";
  const agentId = "00000000-0000-4000-8000-000000000001";
  const runId = "22222222222222222222222222222222";
  document.querySelector("#login-panel").hidden = true;
  document.querySelector("#workspace").hidden = false;
  state.csrfToken = "test-csrf";
  state.agentId = agentId;
  state.sessionId = sessionId;
  state.sessions = [{
    session_id: sessionId,
    title: "UX regression",
    revision: 0,
    state: "idle",
    message_count: 0,
    updated_at: "2026-07-22T00:00:00.000Z",
  }];
  state.models = [{ model_id: "qwen3.5:2b" }];
  elements.modelSelect.replaceChildren(new Option("qwen3.5:2b", "qwen3.5:2b"));
  elements.modelSelect.value = "qwen3.5:2b";
  elements.compactInput.checked = true;

  let runPosts = 0;
  let fetchMode = "pending";
  let preparationCancelled = false;
  const preparationOperationId = "34343434343434343434343434343434";
  window.fetch = (_input, init = {}) => {
    const requestUrl = new URL(
      typeof _input === "string" ? _input : _input.url,
      window.location.href,
    );
    const method = String(init.method || "GET").toUpperCase();
    if (method === "GET" && requestUrl.pathname.endsWith("/subagents")) {
      return Promise.resolve(new Response(JSON.stringify({ delegations: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }
    if (
      method === "GET" &&
      requestUrl.pathname.endsWith(`/sessions/${sessionId}`)
    ) {
      return Promise.resolve(new Response(JSON.stringify({
        session: {
          session_id: sessionId,
          title: "UX regression",
          revision: 0,
          state: "idle",
          message_count: 0,
          turn_count: 0,
          turn_limit: 128,
          turns_remaining: 128,
          submission_blocker: null,
          created_at: "2026-07-22T00:00:00.000Z",
          updated_at: "2026-07-22T00:00:00.000Z",
        },
        messages: [],
        page: {
          version: "turn-page-v2",
          limit: 32,
          before_cursor: null,
          returned_turns: 0,
          total_turns: 0,
          oldest_position: null,
          newest_position: null,
          has_older: false,
          has_newer: false,
          next_before_cursor: null,
        },
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }
    if (method === "GET" && requestUrl.pathname.endsWith("/preparation")) {
      return Promise.resolve(new Response(JSON.stringify(
        preparationCancelled
          ? {
            version: "run-preparation-v1",
            state: "idle",
            operation_id: null,
            stage: null,
            elapsed_ms: 0,
          }
          : {
            version: "run-preparation-v1",
            state: "preparing",
            operation_id: preparationOperationId,
            stage: "summarizing_history",
            elapsed_ms: 25,
          },
      ), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }
    if (method === "POST" && requestUrl.pathname.endsWith("/preparation/cancel")) {
      const cancelBody = JSON.parse(init.body || "{}");
      if (cancelBody.operation_id !== preparationOperationId) {
        return Promise.resolve(new Response("{}", { status: 400 }));
      }
      preparationCancelled = true;
      return Promise.resolve(new Response(JSON.stringify({
        version: "run-preparation-cancel-v1",
        state: "cancellation_requested",
        target: "preparation",
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }
    if (method === "POST" && requestUrl.pathname.endsWith("/runs")) runPosts += 1;
    if (fetchMode === "network-error") {
      return Promise.reject(new Error("simulated admission network failure"));
    }
    return new Promise((_resolve, reject) => {
      if (init.signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      init.signal?.addEventListener("abort", () => {
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    });
  };

  const input = elements.messageInput;
  input.value = "草稿必须保留";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  setRunControls();
  elements.runForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await pause(25);
  outcomes.preparing = {
    visible: state.preparingRun !== null,
    draft: input.value,
    button: elements.runButton.textContent,
    cancel: elements.cancelButton.textContent,
    status: elements.composerStatus.textContent,
  };

  elements.runForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await pause(0);
  outcomes.duplicatePreparingFeedback = elements.composerStatus.textContent;
  elements.cancelButton.click();
  await pause(350);
  outcomes.cancelledPreparation = {
    preparing: state.preparingRun !== null,
    draft: input.value,
    model: elements.modelSelect.value,
    compact: elements.compactInput.checked,
    status: elements.composerStatus.textContent,
    runPosts,
  };

  fetchMode = "network-error";
  elements.runForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await pause(25);
  outcomes.admissionFailure = {
    preparing: state.preparingRun !== null,
    draft: input.value,
    model: elements.modelSelect.value,
    compact: elements.compactInput.checked,
    status: elements.composerStatus.textContent,
    runPosts,
  };

  state.activeRun = {
    runId,
    sessionId,
    terminalSeen: false,
    cancelPending: false,
  };
  state.settling = false;
  input.value = "运行中再次发送";
  setRunControls();
  elements.runForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await pause(0);
  outcomes.activeRunFeedback = elements.composerStatus.textContent;
  state.activeRun = null;
  setRunControls();

  window.__uxPwned = 0;
  const hostile = [
    "# Safe heading",
    "<img src=x onerror=\"window.__uxPwned=1\">",
    "<script>window.__uxPwned=2</script>",
    "[unsafe](javascript:window.__uxPwned=3)",
    "[safe](https://example.com/path)",
    "**strong** and `code`",
  ].join("\n");
  const rendered = createMessageElement("assistant", hostile, {
    messageId: "message-safe",
    turnStatus: "completed",
  });
  document.body.append(rendered.message);
  await pause(20);
  const safeLink = rendered.body.querySelector('a[href^="https://example.com/"]');
  outcomes.markdown = {
    pwned: window.__uxPwned,
    dangerousNodes: rendered.body.querySelectorAll("script,img,iframe,svg,style").length,
    literalHtml: rendered.body.textContent.includes("<img src=x"),
    literalJavascript: rendered.body.textContent.includes("javascript:"),
    safeLink: Boolean(safeLink),
    safeRel: safeLink?.rel || "",
    safeTarget: safeLink?.target || "",
  };

  let copied = null;
  try {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { copied = value; } },
    });
  } catch (_error) {
    copied = "clipboard-stub-unavailable";
  }
  const copy = rendered.message.querySelector(".message-action");
  copy.click();
  await pause(20);
  outcomes.copy = {
    accessible: copy.getAttribute("aria-label"),
    copied,
    feedback: copy.textContent,
  };
  for (let index = 0; index < 12; index += 1) copy.click();
  await pause(20);
  outcomes.copyTimers = {
    active: copyFeedbackTimers.size,
    feedback: copy.textContent,
  };
  clearCopyFeedbackTimers();
  outcomes.copyTimers.afterClear = copyFeedbackTimers.size;
  outcomes.copyTimers.restored = copy.textContent;
  const renderedUser = createMessageElement("user", "用户原文也必须可复制", {
    messageId: "message-user-copy",
    turnStatus: "completed",
  });
  document.body.append(renderedUser.message);
  const userCopy = renderedUser.message.querySelector(".message-action");
  userCopy.click();
  await pause(20);
  outcomes.userCopy = {
    accessible: userCopy.getAttribute("aria-label"),
    copied,
    feedback: userCopy.textContent,
  };

  renderMessages([{
    message_id: "failed-user-message",
    role: "user",
    content: "please retry this exact input",
    created_at: "2026-07-22T00:00:00.000Z",
    turn_id: "33333333333333333333333333333333",
    run_id: "44444444444444444444444444444444",
    turn_status: "failed",
    terminal: {
      version: "turn-terminal-v1",
      code: "model_first_frame_timeout",
      stage: "model",
      retryable: true,
      duration_ms: 120000,
    },
  }]);
  const retry = document.querySelector(".turn-retry");
  outcomes.retry = {
    present: Boolean(retry),
    enabled: retry ? !retry.disabled : false,
    accessible: retry?.getAttribute("aria-label") || retry?.textContent || "",
  };

  const output = document.createElement("output");
  output.id = "ux-reliability-result";
  output.dataset.payload = JSON.stringify(outcomes);
  output.textContent = "complete";
  document.body.append(output);
}, 350);
"""


class _UxStaticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback contract
        if self.path == "/":
            source = (STATIC / "index.html").read_text(encoding="utf-8")
            source = source.replace(
                '<script src="/assets/app.js" defer></script>',
                '<script src="/assets/app.js" defer></script>'
                '<script src="/ux-reliability-test.js" defer></script>',
            )
            self._send(200, "text/html; charset=utf-8", source.encode())
            return
        if self.path == "/assets/app.js":
            self._send(
                200,
                "text/javascript; charset=utf-8",
                (STATIC / "app.js").read_bytes(),
            )
            return
        if self.path == "/assets/styles.css":
            self._send(
                200,
                "text/css; charset=utf-8",
                (STATIC / "styles.css").read_bytes(),
            )
            return
        if self.path == "/ux-reliability-test.js":
            self._send(
                200,
                "text/javascript; charset=utf-8",
                _UX_BROWSER_TEST.encode(),
            )
            return
        self._send(404, "application/json", b'{"detail":"test fixture"}')

    def _send(self, status: int, media_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.mark.skipif(CHROMIUM is None, reason="qualified Chromium is unavailable")
def test_preparing_markdown_copy_and_retry_are_real_browser_behaviors() -> None:
    results_root = ROOT / ".runtime" / "test-results"
    results_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UxStaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix="chromium-ux-reliability-", dir=results_root
        ) as profile:
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(ROOT / ".runtime" / "home"),
                    "TMPDIR": str(ROOT / ".runtime" / "tmp"),
                }
            )
            result = subprocess.run(
                [
                    CHROMIUM or "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=2200",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=environment,
            )
        assert result.returncode == 0, result.stderr[-2_000:]
        match = re.search(
            r'id="ux-reliability-result"[^>]*data-payload="([^"]+)"',
            result.stdout,
        )
        assert match is not None, result.stdout[-3_000:]
        outcome = json.loads(html.unescape(match.group(1)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert outcome["preparing"]["visible"] is True
    assert outcome["preparing"]["draft"] == "草稿必须保留"
    assert outcome["preparing"]["button"] == "准备中…"
    assert outcome["preparing"]["cancel"] == "取消准备"
    assert "准备" in outcome["preparing"]["status"]
    assert "准备" in outcome["duplicatePreparingFeedback"]
    assert outcome["cancelledPreparation"] == {
        "preparing": False,
        "draft": "草稿必须保留",
        "model": "qwen3.5:2b",
        "compact": True,
        "status": "已安全取消上下文准备；消息草稿仍在输入框中",
        "runPosts": 1,
    }
    assert outcome["admissionFailure"] == {
        "preparing": False,
        "draft": "草稿必须保留",
        "model": "qwen3.5:2b",
        "compact": True,
        "status": "无法连接本地服务，请检查服务状态后重试",
        "runPosts": 2,
    }
    assert "当前会话仍在运行" in outcome["activeRunFeedback"]

    assert outcome["markdown"] == {
        "pwned": 0,
        "dangerousNodes": 0,
        "literalHtml": True,
        "literalJavascript": True,
        "safeLink": True,
        "safeRel": "noopener noreferrer nofollow",
        "safeTarget": "_blank",
    }
    assert outcome["copy"]["accessible"] == "复制智能体消息"
    assert outcome["copy"]["copied"].startswith("# Safe heading")
    assert outcome["copy"]["feedback"] == "已复制"
    assert outcome["copyTimers"] == {
        "active": 1,
        "feedback": "已复制",
        "afterClear": 0,
        "restored": "复制",
    }
    assert outcome["userCopy"] == {
        "accessible": "复制用户消息",
        "copied": "用户原文也必须可复制",
        "feedback": "已复制",
    }
    assert outcome["retry"]["present"] is True
    assert outcome["retry"]["enabled"] is True
    assert "重试" in outcome["retry"]["accessible"]

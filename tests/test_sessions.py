"""Conversation state, recovery, transaction, and containment boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
import time

import pytest

import agent_builder_v2.sessions as sessions_module
from agent_builder_v2.contracts import (
    RUN_CURSOR_RESERVED_THROUGH,
    EventEnvelope,
    LoopLimits,
)
from agent_builder_v2.completed_context import CompletedContextItem, CompletedTurnContext
from agent_builder_v2.context import (
    CONTEXT_RENDERER_VERSION,
    PROMPT_SECTION_REGISTRY_VERSION,
    ContextCompiler,
    ModelProfile,
)
from agent_builder_v2.context_projection import ContextProjectionBoundary
from agent_builder_v2.replay import (
    MODEL_BOUNDARY_FEATURE,
    MULTI_TOOL_LOOP_FEATURE,
    OVERFLOW_RECOVERY_FEATURE,
    PROJECTION_VERSION,
)
from agent_builder_v2.semantic_summary import SemanticSummaryContent
from agent_builder_v2.semantic_summary_v2 import (
    SemanticSummaryV2Snapshot,
    summary_v2_source_digest,
)
from agent_builder_v2.sessions import (
    DATABASE_NAME,
    MAX_ASSISTANT_CONTENT_BYTES,
    MAX_LIST_LIMIT,
    MAX_TITLE_BYTES,
    MAX_TURNS_PER_CONVERSATION,
    MAX_USER_CONTENT_BYTES,
    ConversationConflictError,
    ConversationNotFoundError,
    ProviderUsageMutation,
    ConversationStore,
    ConversationStoreUnavailableError,
    ConversationTurnCapacityError,
    conversation_message_id,
)
from agent_builder_v2.runtime import TurnRuntimeSnapshot
from agent_builder_v2.state import EventJournal
from agent_builder_v2.tools import (
    prototype_effective_toolset,
    prototype_tool_specs,
    toolset_digest,
)


AGENT_ID = "00000000-0000-4000-8000-000000000001"
PLAN_DIGEST = "a" * 64
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _id(value: int) -> str:
    return f"{value:032x}"


def _database(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "agents" / AGENT_ID
    root.mkdir(parents=True, mode=0o700)
    return root / DATABASE_NAME


def _summary_bundle() -> CompletedTurnContext:
    return CompletedTurnContext(
        agent_id=AGENT_ID,
        conversation_id=_id(950),
        turn_id=_id(951),
        run_id=_id(952),
        position=1,
        model_profile_digest="b" * 64,
        context_plan_digest="c" * 64,
        items=(
            CompletedContextItem.plain(0, "user", "remember FACT-17"),
            CompletedContextItem.plain(1, "assistant_final", "remembered"),
        ),
    )


def _calibration_boundary(
    conversation_id: str, turn_id: str, run_id: str
) -> ContextProjectionBoundary:
    profile = ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest="9" * 64,
        native_context_tokens=262_144,
        operational_context_tokens=32_768,
        max_output_tokens=4_096,
        profile_source="test",
    )
    effective = prototype_effective_toolset()
    plan = ContextCompiler().compile(
        "calibration turn",
        model_profile=profile,
        tools=effective.specs,
        agent_id=AGENT_ID,
        capsule_generation=1,
    )
    runtime = TurnRuntimeSnapshot.create(
        context_plan=plan,
        effective_toolset=effective,
        loop_limits=LoopLimits(max_model_iterations=4, max_tool_calls=2),
        wall_timeout_seconds=120,
    )
    return ContextProjectionBoundary.create(
        runtime,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        conversation_revision=0,
    )


def _transport_payload(
    *,
    attempt: int = 1,
    maximum: int = 2,
    phase: str = "attempt_started",
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
        "max_attempts": maximum,
        "phase": phase,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "first_frame_ms": first_frame_ms,
    }


def _started_payload() -> dict[str, object]:
    return {
        "prototype": True,
        "model": "qwen3.5:2b",
        "visible_tools": ["builtin/echo"],
        "sandbox": "harness-v2-worker-v1",
        "context_plan": {
            "plan_id": f"context-{PLAN_DIGEST[:24]}",
            "digest": PLAN_DIGEST,
            "toolset_digest": toolset_digest(prototype_tool_specs()),
            "section_count": 3,
            "history_message_count": 0,
            "included_history_message_count": 0,
            "omitted_history_message_count": 0,
            "history_source_digest": "b" * 64,
            "windowing_strategy": "full",
            "estimated_input_tokens": 1_024,
            "native_context_tokens": 262_144,
            "operational_context_tokens": 32_768,
            "input_budget_tokens": 30_720,
            "compact_at_tokens": 24_576,
            "compact_target_tokens": 18_432,
            "output_reserve_tokens": 2_048,
            "template_reserve_tokens": 256,
            "estimator": "utf8-bytes-upper-bound-v1",
        },
    }


def _boundary_started_payload() -> dict[str, object]:
    return {
        **_started_payload(),
        "protocol_features": ["model-call-boundaries-v1"],
    }


def _tool_free_boundary_started_payload() -> dict[str, object]:
    payload = _boundary_started_payload()
    payload["visible_tools"] = []
    context = payload["context_plan"]
    assert isinstance(context, dict)
    context["toolset_digest"] = toolset_digest(())
    return payload


def _model_request_payload(
    iteration: int = 1,
    *,
    estimated_input_tokens: int = 1_024,
) -> dict[str, object]:
    return {
        "request_id": f"model-{iteration}",
        "iteration": iteration,
        "context_plan_id": f"context-{PLAN_DIGEST[:24]}",
        "context_plan_digest": PLAN_DIGEST,
        "request_digest": f"{iteration:x}" * 64,
        "request_bytes": 512,
        "estimated_input_tokens": estimated_input_tokens,
        "message_count": 2,
        "tool_count": 1 if iteration == 1 else 0,
        "tool_result_call_ids": [],
    }


def _model_response_payload(
    iteration: int = 1,
    *,
    input_tokens: int = 23,
    output_tokens: int = 4,
) -> dict[str, object]:
    return {
        "request_id": f"model-{iteration}",
        "iteration": iteration,
        "outcome": "end_turn",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_complete": True,
        "error_code": None,
    }


def _repetition_response_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": "model-1",
        "iteration": 1,
        "outcome": "repetition_truncated",
        "input_tokens": 0,
        "output_tokens": 0,
        "usage_complete": False,
        "error_code": None,
    }
    payload.update(changes)
    return payload


def _usage(*, complete: bool = True) -> dict[str, object]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "last_input_tokens": 0,
        "complete": complete,
    }


def _canonical_payload(kind: str) -> dict[str, object]:
    if kind == "run.started":
        return _started_payload()
    if kind == "run.completed":
        return {
            "reason": "end_turn",
            "model_iterations": 1,
            "usage": _usage(),
        }
    if kind == "run.failed":
        return {
            "code": "worker_failure",
            "message": "The prototype Worker stopped unexpectedly.",
            "retryable": False,
            "usage": _usage(),
        }
    if kind == "run.cancelled":
        return {"reason": "cancelled", "usage": _usage()}
    return {"kind": kind}


def _kill_child_after_marker(
    tmp_path: Path,
    name: str,
    source: str,
    *arguments: str,
) -> None:
    marker = tmp_path / f"{name}.ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            source,
            *arguments,
            str(marker),
            str(SOURCE_ROOT),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not marker.is_file():
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(
                f"crash child exited before its marker: {stderr[-2000:]}"
            )
        time.sleep(0.01)
    if not marker.is_file():
        process.kill()
        process.wait(timeout=5.0)
        raise AssertionError("crash child did not reach its failpoint")
    process.kill()
    assert process.wait(timeout=5.0) < 0


def _event(
    *,
    kind: str,
    seq: int,
    conversation_id: str,
    turn_id: str,
    run_id: str,
    payload: dict[str, object] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=_id(90_000 + seq),
        agent_id=AGENT_ID,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        seq=seq,
        occurred_at=f"2026-07-18T00:00:{seq:02d}.000Z",
        kind=kind,
        durability="durable",
        payload=payload if payload is not None else _canonical_payload(kind),
    )


def _started(conversation_id: str, turn_id: str, run_id: str) -> EventEnvelope:
    return _event(
        kind="run.started",
        seq=1,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
    )


def _completed(conversation_id: str, turn_id: str, run_id: str) -> EventEnvelope:
    return _event(
        kind="run.completed",
        seq=2,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
    )


def test_create_list_read_and_reopen_conversations(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    try:
        first = store.create_conversation("First", conversation_id=_id(1))
        second = store.create_conversation("第二个会话", conversation_id=_id(2))

        summaries = store.list_conversations()

        assert {item.conversation_id for item in summaries} == {
            first.conversation_id,
            second.conversation_id,
        }
        assert all(item.agent_id == AGENT_ID for item in summaries)
        assert all(item.turn_count == 0 for item in summaries)
        assert store.get_conversation(first.conversation_id) == first
        assert store.database_path == database
    finally:
        store.close()

    reopened = ConversationStore(database, AGENT_ID)
    try:
        assert reopened.get_conversation(_id(1)).title == "First"
        assert reopened.get_conversation(_id(2)).title == "第二个会话"
    finally:
        reopened.close()


def test_completed_turns_are_the_only_committed_multiturn_history(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    try:
        conversation = store.create_conversation(
            "Multi-turn", conversation_id=_id(10)
        )
        first = store.begin_turn(
            conversation.conversation_id,
            turn_id=_id(11),
            run_id=_id(12),
            user_content="What is one?",
            expected_revision=0,
            started_event=_started(_id(10), _id(11), _id(12)),
        )
        assert first.committed_history == ()
        assert store.committed_history(conversation.conversation_id) == ()
        completed = store.finalize_completed(
            _id(12), "One.", _completed(_id(10), _id(11), _id(12))
        )
        assert completed.status == "completed"

        failed = store.begin_turn(
            conversation.conversation_id,
            turn_id=_id(21),
            run_id=_id(22),
            user_content="This Run will fail.",
            expected_revision=2,
            started_event=_started(_id(10), _id(21), _id(22)),
        )
        assert [message.content for message in failed.committed_history] == [
            "What is one?",
            "One.",
        ]
        store.finalize_noncompleted(
            _id(22),
            "failed",
            _event(
                kind="run.failed",
                seq=2,
                conversation_id=_id(10),
                turn_id=_id(21),
                run_id=_id(22),
            ),
        )

        third = store.begin_turn(
            conversation.conversation_id,
            turn_id=_id(31),
            run_id=_id(32),
            user_content="What is two?",
            expected_revision=4,
            started_event=_started(_id(10), _id(31), _id(32)),
        )
        assert [message.content for message in third.committed_history] == [
            "What is one?",
            "One.",
        ]
        store.finalize_completed(
            _id(32), "Two.", _completed(_id(10), _id(31), _id(32))
        )

        restored = store.get_conversation(conversation.conversation_id)
        assert [turn.position for turn in restored.turns] == [1, 2, 3]
        assert [turn.status for turn in restored.turns] == [
            "completed",
            "failed",
            "completed",
        ]
        assert restored.turns[1].assistant_content is None
        assert [message.role for message in store.committed_history(_id(10))] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        history = store.committed_history(_id(10))
        assert history[0].message_id == conversation_message_id(_id(11), "user")
        assert history[0].message_id == first.turn.user_message_id
        assert history[1].message_id == completed.assistant_message_id
        assert history[0].message_id != history[1].message_id
        assert failed.turn.assistant_message_id is None
        summary = store.list_conversations()[0]
        assert summary.turn_count == 3
        assert summary.completed_turn_count == 2
        assert summary.last_run_id == _id(32)
        assert summary.active_run_id is None
        assert summary.revision == 6
    finally:
        store.close()


def test_begin_and_finalize_atomically_persist_canonical_boundary_events(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(40), _id(41), _id(42)
    try:
        store.create_conversation(conversation_id=conversation_id)
        started = _event(
            kind="run.started",
            seq=1,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
        )
        terminal = _event(
            kind="run.completed",
            seq=2,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
        )

        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="persist atomically",
            expected_revision=0,
            started_event=started,
        )
        assert [event["kind"] for event in journal.events_for_run(run_id)] == [
            "run.started"
        ]
        started_state = store.get_run_journal_state(run_id)
        assert started_state.reserved_through == RUN_CURSOR_RESERVED_THROUGH
        assert started_state.latest_durable_seq == 1
        assert started_state.terminal_seq is None

        store.finalize_completed(run_id, "done", terminal)
        assert [event["kind"] for event in journal.events_for_run(run_id)] == [
            "run.started",
            "run.completed",
        ]
        terminal_state = store.get_run_journal_state(run_id)
        assert terminal_state.terminal_seq == 2
        assert terminal_state.terminal_kind == "run.completed"
        assert terminal_state.event_count == 2
        identity = store.resolve_run_identity(run_id)
        assert identity.agent_id == AGENT_ID
        assert identity.conversation_id == conversation_id
        assert identity.turn_id == turn_id
        snapshot = store.read_run_snapshot(run_id)
        assert snapshot is not None
        assert snapshot.version == PROJECTION_VERSION
        assert snapshot.identity == identity
        assert snapshot.through_seq == 2
        assert snapshot.complete is True
        assert snapshot.document == {
            "blocks": [],
            "model_calls": [],
            "started": _started_payload(),
            "terminal": {
                "kind": "run.completed",
                "payload": {
                    "reason": "end_turn",
                    "model_iterations": 1,
                    "usage": _usage(),
                },
            },
            "tools": [],
        }
        assert store.get_run_snapshot(run_id) == snapshot.to_dict()
    finally:
        journal.close()
        store.close()


def test_late_sqlite_faults_roll_back_turn_and_terminal_transactions(
    tmp_path: Path,
) -> None:
    """A failure at each transaction tail must expose no semantic prefix."""

    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(401), _id(402), _id(403)
    started = _started(conversation_id, turn_id, run_id)
    terminal = _completed(conversation_id, turn_id, run_id)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store._connection.executescript(
            """
            CREATE TRIGGER qualification_abort_begin_tail
            BEFORE INSERT ON run_journal_state
            BEGIN
                SELECT RAISE(ABORT, 'qualification begin fault');
            END;
            """
        )

        with pytest.raises(ConversationConflictError):
            store.begin_turn(
                conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                user_content="must roll back",
                expected_revision=0,
                started_event=started,
            )

        after_begin_fault = store.get_conversation(conversation_id)
        assert after_begin_fault.revision == 0
        assert after_begin_fault.active_run_id is None
        assert after_begin_fault.turns == ()
        assert journal.events_for_run(run_id) == []
        assert store._connection.execute(
            "SELECT COUNT(*) FROM run_journal_state WHERE run_id = ?", (run_id,)
        ).fetchone() == (0,)

        store._connection.execute("DROP TRIGGER qualification_abort_begin_tail")
        store._connection.commit()
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="retry after rollback",
            expected_revision=0,
            started_event=started,
        )
        store._connection.executescript(
            """
            CREATE TRIGGER qualification_abort_terminal_tail
            BEFORE INSERT ON run_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'qualification terminal fault');
            END;
            """
        )

        with pytest.raises(ConversationConflictError):
            store.finalize_completed(run_id, "must not commit", terminal)

        after_terminal_fault = store.get_conversation(conversation_id)
        assert after_terminal_fault.revision == 1
        assert after_terminal_fault.active_run_id == run_id
        assert len(after_terminal_fault.turns) == 1
        assert after_terminal_fault.turns[0].status == "running"
        assert after_terminal_fault.turns[0].assistant_content is None
        assert [event["kind"] for event in journal.events_for_run(run_id)] == [
            "run.started"
        ]
        state = store.get_run_journal_state(run_id)
        assert state.latest_durable_seq == 1
        assert state.terminal_seq is None
        assert store.read_run_snapshot(run_id) is None

        store._connection.execute("DROP TRIGGER qualification_abort_terminal_tail")
        store._connection.commit()
        completed = store.finalize_completed(run_id, "committed once", terminal)
        assert completed.status == "completed"
        assert [event["kind"] for event in journal.events_for_run(run_id)] == [
            "run.started",
            "run.completed",
        ]
        assert store.get_run_journal_state(run_id).terminal_seq == 2
        assert store.read_run_snapshot(run_id) is not None
    finally:
        journal.close()
        store.close()


def test_late_tombstone_fault_rolls_back_every_degraded_run_mutation(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(420), _id(421), _id(422)
    operation_id = _id(423)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="atomically tombstone a degraded Run",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "degraded-block", "block_type": "content"},
            )
        )
        store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qualified-model",
            profile_digest="d" * 64,
            context_plan_id="degraded-plan",
            toolset_digest="0" * 64,
            estimated_input_tokens=8,
            hard_input_tokens=128,
        )
        store.record_operation_intent(
            operation_id=operation_id,
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="e" * 64,
            request_digest="f" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="degraded-call",
        )
        store.mark_operation_dispatched(
            operation_id,
            executor_kind="sandbox-runner",
            executor_identity_digest="1" * 64,
        )
        before = store.get_run_journal_state(run_id)
        store._connection.executescript(
            f"""
            CREATE TRIGGER qualification_abort_tombstone_tail
            BEFORE UPDATE OF availability ON run_journal_state
            WHEN OLD.run_id = '{run_id}' AND NEW.availability = 'pruned'
            BEGIN
                SELECT RAISE(ABORT, 'qualification tombstone fault');
            END;
            """
        )

        with pytest.raises(
            ConversationStoreUnavailableError,
            match="unavailable Run tombstone",
        ):
            store.finalize_noncompleted(run_id, "failed")

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert [
            event["kind"] for event in journal.events_for_run(run_id)
        ] == ["run.started", "assistant.block.started"]
        assert store.provider_usage_for_run(run_id)[0].status == "started"
        duplicate = store.record_operation_intent(
            operation_id=_id(424),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="e" * 64,
            request_digest="f" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="degraded-call",
        )
        assert duplicate.changed is False
        assert duplicate.record.status == "dispatched"
        assert store.get_run_journal_state(run_id) == before

        store._connection.execute("DROP TRIGGER qualification_abort_tombstone_tail")
        store._connection.commit()
        store.finalize_noncompleted(run_id, "failed")

        terminal = store.get_conversation(conversation_id)
        assert terminal.active_run_id is None
        assert terminal.turns[0].status == "failed"
        assert journal.events_for_run(run_id) == []
        assert store.provider_usage_for_run(run_id)[0].status == "incomplete"
        operation = store.record_operation_intent(
            operation_id=_id(425),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="e" * 64,
            request_digest="f" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="degraded-call",
        )
        assert operation.changed is False
        assert operation.record.status == "outcome_unknown"
        state = store.get_run_journal_state(run_id)
        assert state.availability == "pruned"
        assert state.event_count == state.durable_bytes == 0
        assert state.terminal_seq is None
    finally:
        journal.close()
        store.close()


def test_operation_and_provider_ledgers_are_idempotent_and_recover_unknown(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    conversation_id, turn_id, run_id = _id(43), _id(44), _id(45)
    store.create_conversation(conversation_id=conversation_id)
    store.begin_turn(
        conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        user_content="recover durable ledgers",
        expected_revision=0,
        started_event=_started(conversation_id, turn_id, run_id),
    )
    try:
        intent = store.record_operation_intent(
            operation_id=_id(46),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="a" * 64,
            request_digest="b" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="call-1",
        )
        assert intent.changed is True
        duplicate = store.record_operation_intent(
            operation_id=_id(47),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="a" * 64,
            request_digest="b" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="call-1",
        )
        assert duplicate.changed is False
        assert duplicate.record.operation_id == _id(46)
        with pytest.raises(ConversationConflictError, match="different operation"):
            store.record_operation_intent(
                operation_id=_id(48),
                capability_id="builtin/test-mutation",
                policy_revision="policy-v1",
                idempotency_key_hash="a" * 64,
                request_digest="c" * 64,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                call_id="call-1",
            )
        dispatched = store.mark_operation_dispatched(
            _id(46),
            executor_kind="sandbox-runner",
            executor_identity_digest="d" * 64,
        )
        assert dispatched.changed is True
        assert store.mark_operation_dispatched(
            _id(46),
            executor_kind="sandbox-runner",
            executor_identity_digest="d" * 64,
        ).changed is False

        first_usage = store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qualified-model",
            profile_digest="e" * 64,
            context_plan_id="context-plan-1",
            toolset_digest="0" * 64,
            estimated_input_tokens=6,
            hard_input_tokens=100,
        )
        assert first_usage.changed is True
        completed_usage = store.complete_provider_usage(
            run_id, 1, input_tokens=7, output_tokens=3
        )
        assert completed_usage.record.cost_minor_units is None
        assert completed_usage.record.currency is None
        assert store.complete_provider_usage(
            run_id, 1, input_tokens=7, output_tokens=3
        ).changed is False
        store.start_provider_usage(
            run_id,
            2,
            provider="ollama",
            model="qualified-model",
            profile_digest="e" * 64,
            context_plan_id="context-plan-1",
            toolset_digest="0" * 64,
            estimated_input_tokens=10,
            hard_input_tokens=100,
        )

        recovered = store.recover_running_as_interrupted()

        assert [turn.status for turn in recovered] == ["interrupted"]
        usages = store.provider_usage_for_run(run_id)
        assert [item.status for item in usages] == ["complete", "incomplete"]
        state = store.get_run_journal_state(run_id)
        assert state.terminal_seq == 513
        assert state.input_tokens == 7
        assert state.output_tokens == 3
        assert state.last_input_tokens == 7
        assert state.usage_complete is False
        recovered_operation = store.record_operation_intent(
            operation_id=_id(49),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="a" * 64,
            request_digest="b" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="call-1",
        )
        assert recovered_operation.changed is False
        assert recovered_operation.record.status == "outcome_unknown"
        assert store._connection.execute(
            "SELECT ephemeral_loss FROM run_snapshots WHERE run_id = ?", (run_id,)
        ).fetchone() == (1,)
        snapshot = store.read_run_snapshot(run_id)
        assert snapshot is not None
        assert snapshot.version == PROJECTION_VERSION
        assert snapshot.through_seq == 513
    finally:
        store.close()


def test_recovery_closes_an_inflight_model_boundary_before_terminal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(301), _id(302), _id(303)
    started_payload = {
        **_started_payload(),
        "protocol_features": ["model-call-boundaries-v1"],
    }
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="recover provider stream",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=started_payload,
            ),
        )
        journal.append(
            _event(
                kind="model.request.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={
                    "request_id": "model-1",
                    "iteration": 1,
                    "context_plan_id": f"context-{PLAN_DIGEST[:24]}",
                    "context_plan_digest": PLAN_DIGEST,
                    "request_digest": "c" * 64,
                    "request_bytes": 512,
                    "estimated_input_tokens": 1_024,
                    "message_count": 2,
                    "tool_count": 1,
                    "tool_result_call_ids": [],
                },
            )
        )
        store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest="d" * 64,
            context_plan_id=f"context-{PLAN_DIGEST[:24]}",
            toolset_digest="0" * 64,
            estimated_input_tokens=1_024,
            hard_input_tokens=30_720,
        )

        recovered = store.recover_running_as_interrupted()

        assert [item.status for item in recovered] == ["interrupted"]
        events = journal.events_for_run(run_id)
        assert [item["kind"] for item in events] == [
            "run.started",
            "model.request.started",
            "model.response.finished",
            "run.failed",
        ]
        assert events[-2]["payload"] == {
            "request_id": "model-1",
            "iteration": 1,
            "outcome": "error",
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_complete": False,
            "error_code": "control_restarted",
        }
        snapshot = store.read_run_snapshot(run_id)
        assert snapshot is not None
        model_calls = snapshot.document["model_calls"]
        assert isinstance(model_calls, list)
        assert model_calls[0]["state"] == "finished"
        assert model_calls[0]["outcome"] == "error"
        assert store.provider_usage_for_run(run_id)[0].status == "incomplete"
    finally:
        journal.close()
        store.close()


def test_recovery_closes_an_open_provider_transport_attempt_before_terminal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(304), _id(305), _id(306)
    started_payload = {
        **_started_payload(),
        "protocol_features": [
            MODEL_BOUNDARY_FEATURE,
            MULTI_TOOL_LOOP_FEATURE,
            OVERFLOW_RECOVERY_FEATURE,
        ],
    }
    request_payload = {
        **_model_request_payload(),
        "attempt": 0,
        "recovery_id": None,
        "provider_call_index": 1,
    }
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="recover provider transport",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=started_payload,
            ),
        )
        journal.append(
            _event(
                kind="model.request.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=request_payload,
            )
        )
        journal.append(
            _event(
                kind="model.transport.attempt",
                seq=3,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_transport_payload(),
            )
        )
        store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest="d" * 64,
            context_plan_id=f"context-{PLAN_DIGEST[:24]}",
            toolset_digest="0" * 64,
            estimated_input_tokens=1_024,
            hard_input_tokens=30_720,
        )

        assert [turn.status for turn in store.recover_running_as_interrupted()] == [
            "interrupted"
        ]
        events = journal.events_for_run(run_id)
        assert [event["kind"] for event in events] == [
            "run.started",
            "model.request.started",
            "model.transport.attempt",
            "model.transport.attempt",
            "model.response.finished",
            "run.failed",
        ]
        assert events[-3]["payload"] == {
            **_transport_payload(
                phase="attempt_finished",
                outcome="failed_before_first_frame",
            )
        }
        assert store.provider_usage_for_run(run_id)[0].status == "incomplete"
    finally:
        journal.close()
        store.close()


@pytest.mark.parametrize(
    "transport_case",
    ("outside_request", "attempt_out_of_order", "finish_without_start", "response_while_open"),
)
def test_recovery_rejects_invalid_provider_transport_sequence(
    tmp_path: Path, transport_case: str
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(307), _id(308), _id(309)
    started_payload = {
        **_started_payload(),
        "protocol_features": [
            MODEL_BOUNDARY_FEATURE,
            MULTI_TOOL_LOOP_FEATURE,
            OVERFLOW_RECOVERY_FEATURE,
        ],
    }
    request_payload = {
        **_model_request_payload(),
        "attempt": 0,
        "recovery_id": None,
        "provider_call_index": 1,
    }
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reject invalid provider transport",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=started_payload,
            ),
        )
        next_seq = 2
        if transport_case != "outside_request":
            journal.append(
                _event(
                    kind="model.request.started",
                    seq=next_seq,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    payload=request_payload,
                )
            )
            next_seq += 1
        payload = (
            _transport_payload(attempt=2)
            if transport_case == "attempt_out_of_order"
            else _transport_payload(
                phase="attempt_finished",
                outcome="first_frame_timeout",
                elapsed_ms=60_000,
            )
            if transport_case == "finish_without_start"
            else _transport_payload()
        )
        journal.append(
            _event(
                kind="model.transport.attempt",
                seq=next_seq,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=payload,
            )
        )
        if transport_case == "response_while_open":
            journal.append(
                _event(
                    kind="model.response.finished",
                    seq=next_seq + 1,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    payload={
                        **_model_response_payload(),
                        "attempt": 0,
                        "recovery_id": None,
                        "provider_call_index": 1,
                    },
                )
            )

        with pytest.raises(
            ConversationConflictError, match="model transport|model response"
        ):
            store.recover_running_as_interrupted()
        assert store.get_conversation(conversation_id).turns[0].status == "running"
    finally:
        journal.close()
        store.close()


def test_provider_usage_boundaries_roll_back_as_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    conversation_id, turn_id, run_id = _id(330), _id(331), _id(332)
    request_event = _event(
        kind="model.request.started",
        seq=2,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        payload=_model_request_payload(),
    )
    original_insert = store._insert_boundary_event

    def fail_request_insert(event: EventEnvelope, encoded: str) -> None:
        if event.kind == "model.request.started":
            raise sqlite3.OperationalError("simulated request boundary failure")
        original_insert(event, encoded)

    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="provider boundary atomicity",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_boundary_started_payload(),
            ),
        )
        monkeypatch.setattr(store, "_insert_boundary_event", fail_request_insert)
        with pytest.raises(
            ConversationStoreUnavailableError,
            match="provider request boundary",
        ):
            store.start_provider_usage_with_event(
                run_id,
                1,
                provider="ollama",
                model="qwen3.5:2b",
                profile_digest="d" * 64,
                context_plan_id=f"context-{PLAN_DIGEST[:24]}",
                toolset_digest="0" * 64,
                estimated_input_tokens=1_024,
                hard_input_tokens=30_720,
                boundary_event=request_event,
            )

        assert store.provider_usage_for_run(run_id) == ()
        assert store._connection.execute(
            "SELECT kind FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall() == [("run.started",)]
        state = store.get_run_journal_state(run_id)
        assert (state.latest_durable_seq, state.event_count) == (1, 1)

        monkeypatch.setattr(store, "_insert_boundary_event", original_insert)
        store.start_provider_usage_with_event(
            run_id,
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest="d" * 64,
            context_plan_id=f"context-{PLAN_DIGEST[:24]}",
            toolset_digest="0" * 64,
            estimated_input_tokens=1_024,
            hard_input_tokens=30_720,
            boundary_event=request_event,
        )
        mismatched_response = _event(
            kind="model.response.finished",
            seq=3,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            payload=_model_response_payload(input_tokens=24),
        )
        with pytest.raises(ValueError, match="response boundary disagrees"):
            store.complete_provider_usage_with_event(
                run_id,
                1,
                input_tokens=23,
                output_tokens=4,
                boundary_event=mismatched_response,
            )

        def fail_response_insert(event: EventEnvelope, encoded: str) -> None:
            if event.kind == "model.response.finished":
                raise sqlite3.OperationalError(
                    "simulated response boundary failure"
                )
            original_insert(event, encoded)

        monkeypatch.setattr(store, "_insert_boundary_event", fail_response_insert)
        with pytest.raises(
            ConversationStoreUnavailableError,
            match="provider response boundary",
        ):
            store.complete_provider_usage_with_event(
                run_id,
                1,
                input_tokens=23,
                output_tokens=4,
                boundary_event=replace(
                    mismatched_response,
                    payload=_model_response_payload(),
                ),
            )

        usage = store.provider_usage_for_run(run_id)
        assert len(usage) == 1
        assert usage[0].status == "started"
        assert usage[0].input_tokens is None
        assert usage[0].output_tokens is None
        assert store._connection.execute(
            "SELECT kind FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall() == [
            ("run.started",),
            ("model.request.started",),
        ]
        state = store.get_run_journal_state(run_id)
        assert (state.latest_durable_seq, state.event_count) == (2, 2)
    finally:
        store.close()


def test_operation_capacity_failure_has_no_partial_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    monkeypatch.setattr(sessions_module, "MAX_OPERATION_RECORDS_PER_AGENT", 1)
    try:
        store.record_operation_intent(
            operation_id=_id(53),
            capability_id="lifecycle/test",
            policy_revision="policy-v1",
            idempotency_key_hash="1" * 64,
            request_digest="2" * 64,
        )
        with pytest.raises(ConversationConflictError, match="capacity"):
            store.record_operation_intent(
                operation_id=_id(54),
                capability_id="lifecycle/test",
                policy_revision="policy-v1",
                idempotency_key_hash="3" * 64,
                request_digest="4" * 64,
            )
        assert store._connection.execute(
            "SELECT COUNT(*) FROM operation_ledger"
        ).fetchone() == (1,)
    finally:
        store.close()


def test_terminal_usage_must_match_provider_aggregate_and_rolls_back(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(55), _id(56), _id(57)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="usage consistency",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qualified-model",
            profile_digest="5" * 64,
            context_plan_id="context-plan-usage",
            toolset_digest="0" * 64,
            estimated_input_tokens=6,
            hard_input_tokens=100,
        )
        store.complete_provider_usage(
            run_id, 1, input_tokens=7, output_tokens=3
        )
        mismatched = _event(
            kind="run.completed",
            seq=2,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            payload={
                "reason": "end_turn",
                "model_iterations": 1,
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 3,
                    "last_input_tokens": 8,
                    "complete": True,
                }
            },
        )
        with pytest.raises(ConversationConflictError, match="provider ledger"):
            store.finalize_completed(run_id, "must roll back", mismatched)
        assert store.get_conversation(conversation_id).turns[0].status == "running"
        assert store.get_run_journal_state(run_id).terminal_seq is None
        assert store._connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone() == (1,)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM completed_turn_contexts WHERE run_id = ?",
            (run_id,),
        ).fetchone() == (0,)

        usage = {
            "input_tokens": 7,
            "output_tokens": 3,
            "last_input_tokens": 7,
            "complete": True,
        }
        terminal = replace(
            mismatched, payload={**mismatched.payload, "usage": usage}
        )
        store.finalize_completed(run_id, "committed", terminal)
        state = store.get_run_journal_state(run_id)
        assert state.usage_complete is True
        assert (state.input_tokens, state.output_tokens) == (7, 3)
        snapshot = store.read_run_snapshot(run_id)
        assert snapshot is not None
        assert snapshot.document["terminal"] == {
            "kind": "run.completed",
            "payload": {
                "reason": "end_turn",
                "model_iterations": 1,
                "usage": usage,
            },
        }
    finally:
        store.close()


@pytest.mark.parametrize(
    ("changes", "accepted"),
    (
        ({}, True),
        ({"input_tokens": 1}, False),
        ({"output_tokens": 1}, False),
        ({"usage_complete": True}, False),
        ({"error_code": "model_protocol_error"}, False),
    ),
)
def test_repetition_response_boundary_requires_exact_incomplete_usage(
    tmp_path: Path,
    changes: dict[str, object],
    accepted: bool,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(850), _id(851), _id(852)
    request_payload = _model_request_payload()
    request_payload["tool_count"] = 0
    response_payload = _repetition_response_payload(**changes)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="write a joke",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_tool_free_boundary_started_payload(),
            ),
        )
        store.start_provider_usage_with_event(
            run_id,
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest="d" * 64,
            context_plan_id=f"context-{PLAN_DIGEST[:24]}",
            toolset_digest=toolset_digest(()),
            estimated_input_tokens=1_024,
            hard_input_tokens=30_720,
            boundary_event=_event(
                kind="model.request.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=request_payload,
            ),
        )
        response_event = _event(
            kind="model.response.finished",
            seq=3,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            payload=response_payload,
        )
        def complete_response() -> ProviderUsageMutation:
            return store.complete_provider_usage_with_event(
                run_id,
                1,
                input_tokens=int(response_payload["input_tokens"]),
                output_tokens=int(response_payload["output_tokens"]),
                boundary_event=response_event,
            )

        if accepted:
            mutation = complete_response()
            assert mutation.record.status == "incomplete"
            assert mutation.record.input_tokens is None
            assert mutation.record.output_tokens is None
        else:
            with pytest.raises(ValueError, match="response boundary disagrees"):
                complete_response()
            assert store.provider_usage_for_run(run_id)[0].status == "started"
    finally:
        store.close()


def test_terminal_marks_started_provider_usage_incomplete_atomically(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(58), _id(59), _id(60)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="provider failed",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qualified-model",
            profile_digest="6" * 64,
            context_plan_id="context-plan-failed",
            toolset_digest="0" * 64,
            estimated_input_tokens=6,
            hard_input_tokens=100,
        )
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "last_input_tokens": 0,
            "complete": False,
        }
        store.finalize_noncompleted(
            run_id,
            "failed",
            _event(
                kind="run.failed",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={
                    "code": "provider_failed",
                    "message": "failed",
                    "retryable": False,
                    "usage": usage,
                },
            ),
        )
        assert store.provider_usage_for_run(run_id)[0].status == "incomplete"
        assert store.get_run_journal_state(run_id).usage_complete is False
        snapshot = store.read_run_snapshot(run_id)
        assert snapshot is not None
        assert snapshot.document["terminal"]["payload"]["usage"] == usage  # type: ignore[index]
    finally:
        store.close()


def test_strict_snapshot_read_detects_digest_and_identity_drift(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(61), _id(62), _id(63)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="strict snapshot",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        store.finalize_completed(
            run_id, "done", _completed(conversation_id, turn_id, run_id)
        )
        original_digest = store._connection.execute(
            "SELECT source_digest FROM run_snapshots WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        store._connection.execute(
            "UPDATE run_snapshots SET source_digest = ? WHERE run_id = ?",
            ("0" * 64, run_id),
        )
        with pytest.raises(ConversationConflictError, match="digest"):
            store.read_run_snapshot(run_id)
        store._connection.execute(
            "UPDATE run_snapshots SET source_digest = ? WHERE run_id = ?",
            (original_digest, run_id),
        )

        foreign_conversation = _id(64)
        store.create_conversation(conversation_id=foreign_conversation)
        store._connection.execute(
            "UPDATE conversation_turns SET conversation_id = ? WHERE run_id = ?",
            (foreign_conversation, run_id),
        )
        assert store.resolve_run_identity(run_id).conversation_id == foreign_conversation
        with pytest.raises(ConversationConflictError, match="invalid"):
            store.read_run_snapshot(run_id)
    finally:
        store.close()


def test_snapshot_format_remains_readable_across_atomic_journal_prune(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    first_ids = (_id(71), _id(72), _id(73))
    second_ids = (_id(74), _id(75), _id(76))
    try:
        for conversation_id, turn_id, run_id in (first_ids, second_ids):
            store.create_conversation(conversation_id=conversation_id)
            store.begin_turn(
                conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                user_content="snapshot retention",
                expected_revision=0,
                started_event=_started(conversation_id, turn_id, run_id),
            )
            store.finalize_completed(
                run_id,
                "done",
                _completed(conversation_id, turn_id, run_id),
            )

        before = store.read_run_snapshot(first_ids[2])
        assert before is not None
        assert journal.prune_to_recent_runs(1) == 2
        after = store.read_run_snapshot(first_ids[2])
        assert after == before
        assert store.get_run_journal_state(first_ids[2]).availability == "snapshot_only"
        replay = journal.replay(
            first_ids[2], expected_identity=store.resolve_run_identity(first_ids[2])
        )
        assert replay is not None
        assert replay.availability == "snapshot_only"
        assert replay.snapshot == before
    finally:
        journal.close()
        store.close()


def test_boundary_event_conflict_rolls_back_the_turn_transition(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(50), _id(51), _id(52)
    started = _event(
        kind="run.started",
        seq=1,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
    )
    try:
        store.create_conversation(conversation_id=conversation_id)
        journal.append(started)

        with pytest.raises(ConversationConflictError, match="already exists"):
            store.begin_turn(
                conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                user_content="must roll back",
                expected_revision=0,
                started_event=started,
            )

        restored = store.get_conversation(conversation_id)
        assert restored.turns == ()
        assert restored.active_run_id is None
        assert restored.revision == 0
    finally:
        journal.close()
        store.close()


def test_one_running_turn_per_conversation_under_concurrent_connections(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first_store = ConversationStore(database, AGENT_ID)
    second_store = ConversationStore(database, AGENT_ID)
    conversation_id = _id(60)
    try:
        first_store.create_conversation(conversation_id=conversation_id)

        def begin(index: int) -> str:
            store = first_store if index == 1 else second_store
            try:
                store.begin_turn(
                    conversation_id,
                    turn_id=_id(60 + index),
                    run_id=_id(70 + index),
                    user_content=f"user-{index}",
                    expected_revision=0,
                    started_event=_started(
                        conversation_id, _id(60 + index), _id(70 + index)
                    ),
                )
                return "started"
            except ConversationConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(begin, (1, 2)))

        assert sorted(outcomes) == ["conflict", "started"]
        restored = first_store.get_conversation(conversation_id)
        assert len(restored.turns) == 1
        assert restored.turns[0].status == "running"
        assert restored.active_run_id == restored.turns[0].run_id
    finally:
        second_store.close()
        first_store.close()


def test_stale_history_revision_cannot_accept_a_turn(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first_store = ConversationStore(database, AGENT_ID)
    second_store = ConversationStore(database, AGENT_ID)
    conversation_id = _id(75)
    try:
        first_store.create_conversation(conversation_id=conversation_id)
        stale = first_store.snapshot_for_turn(conversation_id)
        assert stale.revision == 0

        second_store.begin_turn(
            conversation_id,
            turn_id=_id(76),
            run_id=_id(77),
            user_content="committed after the stale snapshot",
            expected_revision=0,
            started_event=_started(conversation_id, _id(76), _id(77)),
        )
        second_store.finalize_completed(
            _id(77), "new history", _completed(conversation_id, _id(76), _id(77))
        )

        with pytest.raises(ConversationConflictError, match="changed after"):
            first_store.begin_turn(
                conversation_id,
                turn_id=_id(78),
                run_id=_id(79),
                user_content="compiled with stale history",
                expected_revision=stale.revision,
                started_event=_started(conversation_id, _id(78), _id(79)),
            )
        fresh = first_store.snapshot_for_turn(conversation_id)
        assert fresh.revision == 2
        assert [message.content for message in fresh.committed_history] == [
            "committed after the stale snapshot",
            "new history",
        ]
    finally:
        second_store.close()
        first_store.close()


def test_get_conversation_uses_one_snapshot_across_concurrent_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    reader = ConversationStore(database, AGENT_ID)
    writer = ConversationStore(database, AGENT_ID)
    conversation_id, turn_id, run_id = _id(130), _id(131), _id(132)
    reader.create_conversation(conversation_id=conversation_id)
    reader.begin_turn(
        conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        user_content="running in the read snapshot",
        expected_revision=0,
        started_event=_started(conversation_id, turn_id, run_id),
    )
    first_select_done = Event()
    allow_second_select = Event()
    original_turn_rows = reader._turn_rows

    def paused_turn_rows(value: str) -> list[tuple[object, ...]]:
        first_select_done.set()
        assert allow_second_select.wait(5)
        return original_turn_rows(value)

    monkeypatch.setattr(reader, "_turn_rows", paused_turn_rows)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reader.get_conversation, conversation_id)
            assert first_select_done.wait(5)
            writer.finalize_completed(
                run_id,
                "committed concurrently",
                _completed(conversation_id, turn_id, run_id),
            )
            allow_second_select.set()
            snapshot = future.result(timeout=5)

        assert snapshot.active_run_id == run_id
        assert snapshot.turns[0].status == "running"
        current = writer.get_conversation(conversation_id)
        assert current.active_run_id is None
        assert current.turns[0].status == "completed"
    finally:
        allow_second_select.set()
        writer.close()
        reader.close()


def test_history_existence_and_rows_share_a_snapshot_during_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    reader = ConversationStore(database, AGENT_ID)
    writer = ConversationStore(database, AGENT_ID)
    conversation_id, turn_id, run_id = _id(140), _id(141), _id(142)
    reader.create_conversation(conversation_id=conversation_id)
    reader.begin_turn(
        conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        user_content="visible in snapshot",
        expected_revision=0,
        started_event=_started(conversation_id, turn_id, run_id),
    )
    reader.finalize_completed(
        run_id,
        "also visible",
        _completed(conversation_id, turn_id, run_id),
    )
    existence_checked = Event()
    allow_history_select = Event()
    original_history_rows = reader._committed_history_rows

    def paused_history_rows(value: str) -> list[tuple[object, ...]]:
        existence_checked.set()
        assert allow_history_select.wait(5)
        return original_history_rows(value)

    monkeypatch.setattr(reader, "_committed_history_rows", paused_history_rows)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reader.committed_history, conversation_id)
            assert existence_checked.wait(5)
            assert writer.delete_conversation(conversation_id).deleted is True
            allow_history_select.set()
            history = future.result(timeout=5)

        assert [message.content for message in history] == [
            "visible in snapshot",
            "also visible",
        ]
        with pytest.raises(ConversationNotFoundError):
            writer.committed_history(conversation_id)
    finally:
        allow_history_select.set()
        writer.close()
        reader.close()


def test_sigkill_cannot_commit_complete_usage_without_response_boundary(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(740), _id(741), _id(742)
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="crash inside provider response commit",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_boundary_started_payload(),
            ),
        )
        store.start_provider_usage_with_event(
            run_id,
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest="d" * 64,
            context_plan_id=f"context-{PLAN_DIGEST[:24]}",
            toolset_digest="0" * 64,
            estimated_input_tokens=1_024,
            hard_input_tokens=30_720,
            boundary_event=_event(
                kind="model.request.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_model_request_payload(),
            ),
        )
    finally:
        store.close()

    completion_child = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.sessions import ConversationStore
database, agent_id, conversation_id, turn_id, run_id, marker = sys.argv[1:-1]
store = ConversationStore(Path(database), agent_id)
original = store._insert_boundary_event
def failpoint(event, encoded):
    if event.kind == 'model.response.finished':
        Path(marker).write_text('usage-updated-inside-transaction', encoding='utf-8')
        time.sleep(30)
    original(event, encoded)
store._insert_boundary_event = failpoint
store.complete_provider_usage_with_event(
    run_id,
    1,
    input_tokens=23,
    output_tokens=4,
    boundary_event=EventEnvelope(
        event_id='d' * 32,
        agent_id=agent_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        seq=3,
        occurred_at='2026-07-19T00:00:03.000Z',
        kind='model.response.finished',
        durability='durable',
        payload={
            'request_id': 'model-1',
            'iteration': 1,
            'outcome': 'end_turn',
            'input_tokens': 23,
            'output_tokens': 4,
            'usage_complete': True,
            'error_code': None,
        },
    ),
)
"""
    _kill_child_after_marker(
        tmp_path,
        "provider-response-transaction",
        completion_child,
        str(database),
        AGENT_ID,
        conversation_id,
        turn_id,
        run_id,
    )

    reopened = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        usage_before_recovery = reopened.provider_usage_for_run(run_id)
        assert len(usage_before_recovery) == 1
        assert usage_before_recovery[0].status == "started"
        assert usage_before_recovery[0].input_tokens is None
        assert usage_before_recovery[0].output_tokens is None
        assert [
            event["kind"] for event in journal.events_for_run(run_id)
        ] == ["run.started", "model.request.started"]
        state = reopened.get_run_journal_state(run_id)
        assert (state.latest_durable_seq, state.event_count) == (2, 2)

        recovered = reopened.recover_running_as_interrupted()

        assert [turn.run_id for turn in recovered] == [run_id]
        assert reopened.recover_running_as_interrupted() == ()
        final_events = journal.events_for_run(run_id)
        assert [event["kind"] for event in final_events] == [
            "run.started",
            "model.request.started",
            "model.response.finished",
            "run.failed",
        ]
        assert final_events[-2]["payload"] == {
            "request_id": "model-1",
            "iteration": 1,
            "outcome": "error",
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_complete": False,
            "error_code": "control_restarted",
        }
        recovered_usage = reopened.provider_usage_for_run(run_id)
        assert recovered_usage[0].status == "incomplete"
        snapshot = reopened.read_run_snapshot(run_id)
        assert snapshot is not None
        model_calls = snapshot.document["model_calls"]
        terminal = snapshot.document["terminal"]
        assert isinstance(model_calls, list)
        assert isinstance(terminal, dict)
        assert model_calls[0]["outcome"] == "error"
        assert terminal["payload"]["usage"] == {  # type: ignore[index]
            "input_tokens": 0,
            "output_tokens": 0,
            "last_input_tokens": 0,
            "complete": False,
        }
    finally:
        journal.close()
        reopened.close()


def test_sigkill_after_complete_response_commit_recovers_exact_usage(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(750), _id(751), _id(752)
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="crash after provider response commit",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_boundary_started_payload(),
            ),
        )
        store.start_provider_usage_with_event(
            run_id,
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest="d" * 64,
            context_plan_id=f"context-{PLAN_DIGEST[:24]}",
            toolset_digest="0" * 64,
            estimated_input_tokens=1_024,
            hard_input_tokens=30_720,
            boundary_event=_event(
                kind="model.request.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload=_model_request_payload(),
            ),
        )
    finally:
        store.close()

    committed_child = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.sessions import ConversationStore
database, agent_id, conversation_id, turn_id, run_id, marker = sys.argv[1:-1]
store = ConversationStore(Path(database), agent_id)
store.complete_provider_usage_with_event(
    run_id,
    1,
    input_tokens=23,
    output_tokens=4,
    boundary_event=EventEnvelope(
        event_id='e' * 32,
        agent_id=agent_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        seq=3,
        occurred_at='2026-07-19T00:00:03.000Z',
        kind='model.response.finished',
        durability='durable',
        payload={
            'request_id': 'model-1',
            'iteration': 1,
            'outcome': 'end_turn',
            'input_tokens': 23,
            'output_tokens': 4,
            'usage_complete': True,
            'error_code': None,
        },
    ),
)
Path(marker).write_text('response-transaction-committed', encoding='utf-8')
time.sleep(30)
"""
    _kill_child_after_marker(
        tmp_path,
        "provider-response-committed",
        committed_child,
        str(database),
        AGENT_ID,
        conversation_id,
        turn_id,
        run_id,
    )

    reopened = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        committed_usage = reopened.provider_usage_for_run(run_id)
        assert len(committed_usage) == 1
        assert committed_usage[0].status == "complete"
        assert (
            committed_usage[0].input_tokens,
            committed_usage[0].output_tokens,
        ) == (23, 4)
        assert [
            event["kind"] for event in journal.events_for_run(run_id)
        ] == [
            "run.started",
            "model.request.started",
            "model.response.finished",
        ]
        assert reopened.get_run_journal_state(run_id).terminal_seq is None

        recovered = reopened.recover_running_as_interrupted()

        assert [turn.run_id for turn in recovered] == [run_id]
        final_events = journal.events_for_run(run_id)
        assert [event["kind"] for event in final_events] == [
            "run.started",
            "model.request.started",
            "model.response.finished",
            "run.failed",
        ]
        assert final_events[-2]["payload"] == _model_response_payload()
        assert final_events[-1]["payload"]["usage"] == {
            "input_tokens": 23,
            "output_tokens": 4,
            "last_input_tokens": 23,
            "complete": True,
        }
        state = reopened.get_run_journal_state(run_id)
        assert (state.input_tokens, state.output_tokens) == (23, 4)
        assert state.last_input_tokens == 23
        assert state.usage_complete is True
        assert reopened.provider_usage_for_run(run_id)[0].status == "complete"
        snapshot = reopened.read_run_snapshot(run_id)
        assert snapshot is not None
        model_calls = snapshot.document["model_calls"]
        terminal = snapshot.document["terminal"]
        assert isinstance(model_calls, list)
        assert isinstance(terminal, dict)
        assert model_calls[0]["outcome"] == "end_turn"
        assert model_calls[0]["usage_complete"] is True
        assert terminal["payload"] == final_events[-1]["payload"]
    finally:
        journal.close()
        reopened.close()


def test_sigkill_boundaries_recover_once_without_reusing_side_effects(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first_conversation, first_turn, first_run = _id(700), _id(701), _id(702)
    second_conversation, second_turn, second_run = _id(703), _id(704), _id(705)
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=first_conversation)
        store.begin_turn(
            first_conversation,
            turn_id=first_turn,
            run_id=first_run,
            user_content="crash after durable append",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                    conversation_id=first_conversation,
                    turn_id=first_turn,
                    run_id=first_run,
                    payload=_started_payload(),
            ),
        )
        store.start_provider_usage(
            first_run,
            1,
            provider="ollama",
            model="qualified-model",
            profile_digest="7" * 64,
            context_plan_id="sigkill-plan",
            toolset_digest="0" * 64,
            estimated_input_tokens=8,
            hard_input_tokens=128,
        )
        store.record_operation_intent(
            operation_id=_id(706),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="8" * 64,
            request_digest="9" * 64,
            conversation_id=first_conversation,
            turn_id=first_turn,
            run_id=first_run,
            call_id="sigkill-call",
        )
        store.mark_operation_dispatched(
            _id(706),
            executor_kind="sandbox-runner",
            executor_identity_digest="a" * 64,
        )

        store.create_conversation(conversation_id=second_conversation)
        store.begin_turn(
            second_conversation,
            turn_id=second_turn,
            run_id=second_run,
            user_content="crash after terminal commit",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                    conversation_id=second_conversation,
                    turn_id=second_turn,
                    run_id=second_run,
                    payload=_started_payload(),
            ),
        )
    finally:
        store.close()

    append_child = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.state import EventJournal
database, agent_id, conversation_id, turn_id, run_id, marker = sys.argv[1:-1]
journal = EventJournal(Path(database))
journal.append(EventEnvelope(
    event_id='b' * 32,
    agent_id=agent_id,
    conversation_id=conversation_id,
    turn_id=turn_id,
    run_id=run_id,
    seq=2,
    occurred_at='2026-07-19T00:00:02.000Z',
    kind='assistant.block.started',
    durability='durable',
    payload={'block_id': 'sigkill-block', 'block_type': 'content'},
))
Path(marker).write_text('committed', encoding='utf-8')
time.sleep(30)
"""
    _kill_child_after_marker(
        tmp_path,
        "append-commit",
        append_child,
        str(database),
        AGENT_ID,
        first_conversation,
        first_turn,
        first_run,
    )

    terminal_child = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.sessions import ConversationStore
database, agent_id, conversation_id, turn_id, run_id, marker = sys.argv[1:-1]
store = ConversationStore(Path(database), agent_id)
store.finalize_noncompleted(run_id, 'failed', EventEnvelope(
    event_id='c' * 32,
    agent_id=agent_id,
    conversation_id=conversation_id,
    turn_id=turn_id,
    run_id=run_id,
    seq=2,
    occurred_at='2026-07-19T00:00:02.000Z',
    kind='run.failed',
    durability='durable',
    payload={
        'code': 'simulated_failure',
        'message': 'terminal committed before process death',
        'retryable': False,
        'usage': {
            'input_tokens': 0,
            'output_tokens': 0,
            'last_input_tokens': 0,
            'complete': True,
        },
    },
))
Path(marker).write_text('committed', encoding='utf-8')
time.sleep(30)
"""
    _kill_child_after_marker(
        tmp_path,
        "terminal-commit",
        terminal_child,
        str(database),
        AGENT_ID,
        second_conversation,
        second_turn,
        second_run,
    )

    recovery_child = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
from agent_builder_v2.sessions import ConversationStore
database, agent_id, marker = sys.argv[1:-1]
store = ConversationStore(Path(database), agent_id)
original = store._insert_boundary_event
tripped = False
def failpoint(event, encoded):
    global tripped
    original(event, encoded)
    if not tripped and event.kind == 'assistant.block.discarded':
        tripped = True
        Path(marker).write_text('inside-transaction', encoding='utf-8')
        time.sleep(30)
store._insert_boundary_event = failpoint
store.recover_running_as_interrupted()
"""
    _kill_child_after_marker(
        tmp_path,
        "recovery-transaction",
        recovery_child,
        str(database),
        AGENT_ID,
    )

    reopened = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        first_before = reopened.get_conversation(first_conversation)
        assert first_before.active_run_id == first_run
        assert first_before.turns[0].status == "running"
        assert [
            event["kind"] for event in journal.events_for_run(first_run)
        ] == ["run.started", "assistant.block.started"]
        assert reopened.provider_usage_for_run(first_run)[0].status == "started"
        duplicate = reopened.record_operation_intent(
            operation_id=_id(707),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="8" * 64,
            request_digest="9" * 64,
            conversation_id=first_conversation,
            turn_id=first_turn,
            run_id=first_run,
            call_id="sigkill-call",
        )
        assert duplicate.changed is False
        assert duplicate.record.status == "dispatched"

        second = reopened.get_conversation(second_conversation)
        assert second.active_run_id is None
        assert second.turns[0].status == "failed"
        assert [
            event["kind"] for event in journal.events_for_run(second_run)
        ] == ["run.started", "run.failed"]

        recovered = reopened.recover_running_as_interrupted()
        assert [turn.run_id for turn in recovered] == [first_run]
        assert reopened.recover_running_as_interrupted() == ()
        final_events = journal.events_for_run(first_run)
        assert [event["kind"] for event in final_events] == [
            "run.started",
            "assistant.block.started",
            "assistant.block.discarded",
            "run.failed",
        ]
        assert len(
            [event for event in final_events if event["kind"] == "run.failed"]
        ) == 1
        assert len({event["seq"] for event in final_events}) == len(final_events)
        assert reopened.provider_usage_for_run(first_run)[0].status == "incomplete"
        recovered_operation = reopened.record_operation_intent(
            operation_id=_id(708),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="8" * 64,
            request_digest="9" * 64,
            conversation_id=first_conversation,
            turn_id=first_turn,
            run_id=first_run,
            call_id="sigkill-call",
        )
        assert recovered_operation.changed is False
        assert recovered_operation.record.status == "outcome_unknown"
    finally:
        journal.close()
        reopened.close()


@pytest.mark.parametrize(
    ("column", "replacement"),
    (
        ("oldest_available_seq", 2),
        ("latest_durable_seq", 99),
        ("reserved_through", 513),
        ("availability", "pruned"),
        ("event_count", 99),
        ("durable_bytes", 99),
        ("input_tokens", 1),
    ),
)
def test_recovery_rejects_inconsistent_running_journal_metadata_atomically(
    tmp_path: Path,
    column: str,
    replacement: object,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(720), _id(721), _id(722)
    operation_id = _id(723)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reject inconsistent recovery metadata",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "metadata-block", "block_type": "content"},
            )
        )
        store.start_provider_usage(
            run_id,
            1,
            provider="ollama",
            model="qualified-model",
            profile_digest="2" * 64,
            context_plan_id="metadata-plan",
            toolset_digest="0" * 64,
            estimated_input_tokens=8,
            hard_input_tokens=128,
        )
        store.record_operation_intent(
            operation_id=operation_id,
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="3" * 64,
            request_digest="4" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="metadata-call",
        )
        store.mark_operation_dispatched(
            operation_id,
            executor_kind="sandbox-runner",
            executor_identity_digest="5" * 64,
        )
        before = store.get_run_journal_state(run_id)
        allowed_columns = {
            "oldest_available_seq",
            "latest_durable_seq",
            "reserved_through",
            "availability",
            "event_count",
            "durable_bytes",
            "input_tokens",
        }
        assert column in allowed_columns
        store._connection.execute(
            f"UPDATE run_journal_state SET {column} = ? WHERE run_id = ?",
            (replacement, run_id),
        )
        store._connection.commit()
        corrupted = store.get_run_journal_state(run_id)
        assert corrupted != before

        with pytest.raises(
            ConversationConflictError,
            match="journal metadata is inconsistent",
        ):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert [
            event["kind"] for event in journal.events_for_run(run_id)
        ] == ["run.started", "assistant.block.started"]
        assert store.provider_usage_for_run(run_id)[0].status == "started"
        operation = store.record_operation_intent(
            operation_id=_id(724),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="3" * 64,
            request_digest="4" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="metadata-call",
        )
        assert operation.changed is False
        assert operation.record.status == "dispatched"
        assert store.get_run_journal_state(run_id) == corrupted

        store._connection.execute(
            f"UPDATE run_journal_state SET {column} = ? WHERE run_id = ?",
            (getattr(before, column), run_id),
        )
        store._connection.commit()
        recovered = store.recover_running_as_interrupted()
        assert [turn.run_id for turn in recovered] == [run_id]
        assert store.provider_usage_for_run(run_id)[0].status == "incomplete"
        operation = store.record_operation_intent(
            operation_id=_id(725),
            capability_id="builtin/test-mutation",
            policy_revision="policy-v1",
            idempotency_key_hash="3" * 64,
            request_digest="4" * 64,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            call_id="metadata-call",
        )
        assert operation.record.status == "outcome_unknown"
    finally:
        journal.close()
        store.close()


def test_recovery_rejects_running_run_with_no_durable_events_atomically(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(726), _id(727), _id(728)
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reject missing recovery prefix",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        before = store.get_run_journal_state(run_id)
        store._connection.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        store._connection.commit()

        with pytest.raises(
            ConversationConflictError,
            match="journal metadata is inconsistent",
        ):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert store.get_run_journal_state(run_id) == before
        assert store._connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_recovery_marks_running_turns_interrupted_and_clears_active_run(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    try:
        conversation = store.create_conversation(conversation_id=_id(80))
        store.begin_turn(
            conversation.conversation_id,
            turn_id=_id(81),
            run_id=_id(82),
            user_content="crash before terminal",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation.conversation_id,
                turn_id=_id(81),
                run_id=_id(82),
            ),
        )
    finally:
        store.close()

    reopened = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        recovered = reopened.recover_running_as_interrupted()
        assert len(recovered) == 1
        assert recovered[0].status == "interrupted"
        restored = reopened.get_conversation(_id(80))
        assert restored.active_run_id is None
        assert restored.turns[0].status == "interrupted"
        assert reopened.committed_history(_id(80)) == ()
        events = journal.events_for_run(_id(82))
        assert [event["kind"] for event in events] == [
            "run.started",
            "run.failed",
        ]
        assert events[-1]["payload"]["code"] == "control_restarted"
        assert events[-1]["payload"]["usage"]["complete"] is False
        assert reopened.recover_running_as_interrupted() == ()
    finally:
        journal.close()
        reopened.close()


def test_recovery_discards_open_assistant_block_before_terminal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(180), _id(181), _id(182)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="crash with an open assistant block",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "crashed-block", "block_type": "content"},
            )
        )

        recovered = store.recover_running_as_interrupted()

        assert [turn.status for turn in recovered] == ["interrupted"]
        events = journal.events_for_run(run_id)
        assert [event["kind"] for event in events] == [
            "run.started",
            "assistant.block.started",
            "assistant.block.discarded",
            "run.failed",
        ]
        assert [event["seq"] for event in events] == [1, 2, 513, 514]
        assert events[2]["payload"] == {
            "block_id": "crashed-block",
            "reason": "runtime_failure",
        }
        assert events[3]["payload"]["code"] == "control_restarted"
    finally:
        journal.close()
        store.close()


def test_recovery_starts_and_finishes_requested_tool_before_terminal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(190), _id(191), _id(192)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="crash between Tool request and start",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="tool.call.requested",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={
                    "call_id": "crashed-call",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": "hello"},
                },
            )
        )

        recovered = store.recover_running_as_interrupted()

        assert [turn.status for turn in recovered] == ["interrupted"]
        events = journal.events_for_run(run_id)
        assert [event["kind"] for event in events] == [
            "run.started",
            "tool.call.requested",
            "tool.call.started",
            "tool.call.finished",
            "run.failed",
        ]
        assert [event["seq"] for event in events] == [1, 2, 513, 514, 515]
        assert events[2]["payload"] == {
            "call_id": "crashed-call",
            "tool_id": "builtin/echo",
        }
        assert events[3]["payload"] == {
            "call_id": "crashed-call",
            "tool_id": "builtin/echo",
            "outcome": "failed",
            "result": "Control Plane restarted",
        }
    finally:
        journal.close()
        store.close()


def test_recovery_finishes_already_started_tool_before_terminal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(193), _id(194), _id(195)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="crash while Tool is running",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="tool.call.requested",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={
                    "call_id": "started-call",
                    "tool_id": "builtin/echo",
                    "arguments": {"text": "hello"},
                },
            )
        )
        journal.append(
            _event(
                kind="tool.call.started",
                seq=3,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={
                    "call_id": "started-call",
                    "tool_id": "builtin/echo",
                },
            )
        )

        recovered = store.recover_running_as_interrupted()

        assert [turn.status for turn in recovered] == ["interrupted"]
        events = journal.events_for_run(run_id)
        assert [event["kind"] for event in events] == [
            "run.started",
            "tool.call.requested",
            "tool.call.started",
            "tool.call.finished",
            "run.failed",
        ]
        assert [event["seq"] for event in events] == [1, 2, 3, 513, 514]
        assert events[3]["payload"]["outcome"] == "failed"
    finally:
        journal.close()
        store.close()


def test_recovery_allows_ephemeral_delta_gap_while_block_is_open(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(196), _id(197), _id(198)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="recover across an ephemeral delta gap",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "gap-block", "block_type": "content"},
            )
        )
        journal.append(
            replace(
                _event(
                    kind="assistant.block.delta",
                    seq=3,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    payload={"block_id": "gap-block", "text": "ephemeral"},
                ),
                durability="ephemeral",
            )
        )
        journal.append(
            _event(
                kind="assistant.block.finished",
                seq=4,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "gap-block", "content": "ephemeral"},
            )
        )
        assert [event["seq"] for event in journal.events_for_run(run_id)] == [
            1,
            2,
            4,
        ]

        recovered = store.recover_running_as_interrupted()

        assert [turn.status for turn in recovered] == ["interrupted"]
        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id is None
        assert restored.turns[0].status == "interrupted"
        assert store.committed_history(conversation_id) == ()
        events = journal.events_for_run(run_id)
        assert [event["kind"] for event in events] == [
            "run.started",
            "assistant.block.started",
            "assistant.block.finished",
            "run.failed",
        ]
        assert [event["seq"] for event in events] == [1, 2, 4, 513]
        assert events[-1]["payload"]["code"] == "control_restarted"
    finally:
        journal.close()
        store.close()


def test_recovery_fails_closed_on_unexplained_sequence_gap(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(195), _id(196), _id(197)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reject a journal sequence gap",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=3,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "gap-block", "block_type": "content"},
            )
        )

        with pytest.raises(ConversationConflictError, match="metadata"):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert [event["seq"] for event in journal.events_for_run(run_id)] == [1, 3]
    finally:
        journal.close()
        store.close()


def test_recovery_capacity_counts_rows_not_reserved_ephemeral_sequence_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(198), _id(199), _id(200)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="count omitted delta sequence slots",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "capacity-block", "block_type": "content"},
            )
        )
        journal.append(
            _event(
                kind="assistant.block.finished",
                seq=5,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "capacity-block", "content": "three deltas"},
            )
        )
        monkeypatch.setattr(sessions_module, "MAX_RECOVERY_EVENTS_PER_RUN", 5)

        recovered = store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert [turn.status for turn in recovered] == ["interrupted"]
        assert restored.active_run_id is None
        assert restored.turns[0].status == "interrupted"
        assert [event["seq"] for event in journal.events_for_run(run_id)] == [
            1,
            2,
            5,
            513,
        ]
    finally:
        journal.close()
        store.close()


def test_recovery_fails_closed_on_oversized_untrusted_event_json(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(200), _id(201), _id(202)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reject oversized recovery input",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        store._connection.execute(
            """
            INSERT INTO events(run_id, seq, kind, occurred_at, envelope_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                2,
                "assistant.block.started",
                "2026-07-18T00:00:02.000Z",
                "x" * (sessions_module.MAX_DURABLE_EVENT_BYTES + 1),
            ),
        )

        with pytest.raises(ConversationConflictError, match="metadata"):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert store._connection.execute(
            "SELECT kind FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall() == [("run.started",), ("assistant.block.started",)]
    finally:
        journal.close()
        store.close()


def test_recovery_fails_closed_when_synthetic_events_exceed_count_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(210), _id(211), _id(212)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reserve event capacity",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "open-block", "block_type": "content"},
            )
        )
        monkeypatch.setattr(sessions_module, "MAX_RECOVERY_EVENTS_PER_RUN", 3)

        with pytest.raises(ConversationConflictError, match="event capacity"):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert len(journal.events_for_run(run_id)) == 2
    finally:
        journal.close()
        store.close()


def test_recovery_fails_closed_when_synthetic_events_exceed_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(220), _id(221), _id(222)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reserve durable byte capacity",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        source_bytes = store._connection.execute(
            """
            SELECT SUM(length(CAST(envelope_json AS BLOB)))
            FROM events WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        assert isinstance(source_bytes, int)
        monkeypatch.setattr(
            sessions_module,
            "MAX_RECOVERY_DURABLE_BYTES_PER_RUN",
            source_bytes + 1,
        )

        with pytest.raises(ConversationConflictError, match="byte capacity"):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert len(journal.events_for_run(run_id)) == 1
    finally:
        journal.close()
        store.close()


def test_recovery_fails_closed_on_oversized_event_field(tmp_path: Path) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(230), _id(231), _id(232)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="reject oversized field",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        journal.append(
            _event(
                kind="assistant.block.started",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"block_id": "b" * 65, "block_type": "content"},
            )
        )

        with pytest.raises(ConversationConflictError, match="block_id"):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert len(journal.events_for_run(run_id)) == 2
    finally:
        journal.close()
        store.close()


def test_delete_rejects_active_then_cascades_turns_and_events(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    journal = EventJournal(database)
    conversation_id, turn_id, run_id = _id(90), _id(91), _id(92)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="delete after terminal",
            expected_revision=0,
            started_event=_event(
                kind="run.started",
                seq=1,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
            ),
        )
        with pytest.raises(ConversationConflictError, match="active Run"):
            store.delete_conversation(conversation_id)
        store.finalize_noncompleted(
            run_id,
            "cancelled",
            _event(
                kind="run.cancelled",
                seq=2,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
            ),
        )

        deleted = store.delete_conversation(conversation_id)

        assert deleted.deleted is True
        assert deleted.deleted_turns == 1
        assert deleted.deleted_events == 2
        assert journal.events_for_run(run_id) == []
        assert store.delete_conversation(conversation_id).deleted is False
        with pytest.raises(ConversationNotFoundError):
            store.get_conversation(conversation_id)
    finally:
        journal.close()
        store.close()


def test_input_and_turn_capacity_bounds_have_no_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    try:
        with pytest.raises(ValueError, match="title exceeds"):
            store.create_conversation("界" * (MAX_TITLE_BYTES // 3 + 1))
        conversation = store.create_conversation(conversation_id=_id(100))
        with pytest.raises(ValueError, match="user content exceeds"):
            store.begin_turn(
                conversation.conversation_id,
                turn_id=_id(101),
                run_id=_id(102),
                user_content="界" * (MAX_USER_CONTENT_BYTES // 3 + 1),
                expected_revision=0,
                started_event=_started(
                    conversation.conversation_id, _id(101), _id(102)
                ),
            )
        with pytest.raises(ValueError, match="limit"):
            store.list_conversations(limit=MAX_LIST_LIMIT + 1)

        monkeypatch.setattr(sessions_module, "MAX_TURNS_PER_CONVERSATION", 1)
        store.begin_turn(
            conversation.conversation_id,
            turn_id=_id(103),
            run_id=_id(104),
            user_content="fits",
            expected_revision=0,
            started_event=_started(
                conversation.conversation_id, _id(103), _id(104)
            ),
        )
        with pytest.raises(ValueError, match="assistant content exceeds"):
            store.finalize_completed(
                _id(104),
                "界" * (MAX_ASSISTANT_CONTENT_BYTES // 3 + 1),
                _completed(conversation.conversation_id, _id(103), _id(104)),
            )
        store.finalize_noncompleted(_id(104), "failed")
        with pytest.raises(ConversationConflictError, match="capacity"):
            store.begin_turn(
                conversation.conversation_id,
                turn_id=_id(105),
                run_id=_id(106),
                user_content="does not fit",
                expected_revision=2,
                started_event=_started(
                    conversation.conversation_id, _id(105), _id(106)
                ),
            )
        restored = store.get_conversation(conversation.conversation_id)
        assert len(restored.turns) == 1
        assert restored.turns[0].status == "failed"
    finally:
        store.close()


def test_mismatched_boundary_event_is_rejected_before_writing(tmp_path: Path) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id = _id(110)
    try:
        store.create_conversation(conversation_id=conversation_id)
        mismatched = _event(
            kind="run.started",
            seq=1,
            conversation_id=conversation_id,
            turn_id=_id(111),
            run_id=_id(999),
        )
        with pytest.raises(ValueError, match="does not match"):
            store.begin_turn(
                conversation_id,
                turn_id=_id(111),
                run_id=_id(112),
                user_content="no write",
                expected_revision=0,
                started_event=mismatched,
            )
        wrong_sequence = _event(
            kind="run.started",
            seq=2,
            conversation_id=conversation_id,
            turn_id=_id(111),
            run_id=_id(112),
        )
        with pytest.raises(ValueError, match="does not match"):
            store.begin_turn(
                conversation_id,
                turn_id=_id(111),
                run_id=_id(112),
                user_content="still no write",
                expected_revision=0,
                started_event=wrong_sequence,
            )
        assert store.get_conversation(conversation_id).turns == ()
    finally:
        store.close()


def test_normal_begin_and_completed_transitions_require_boundary_events(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(115), _id(116), _id(117)
    try:
        store.create_conversation(conversation_id=conversation_id)
        with pytest.raises(TypeError):
            store.begin_turn(  # type: ignore[call-arg]
                conversation_id,
                turn_id=turn_id,
                run_id=run_id,
                user_content="missing canonical start",
                expected_revision=0,
            )
        assert store.get_conversation(conversation_id).turns == ()

        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="has canonical start",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        with pytest.raises(TypeError):
            store.finalize_completed(run_id, "missing terminal")  # type: ignore[call-arg]
        assert store.get_conversation(conversation_id).turns[0].status == "running"
        store.finalize_noncompleted(run_id, "interrupted")
    finally:
        store.close()


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_store_rejects_linked_database_without_touching_target(
    tmp_path: Path, link_kind: str
) -> None:
    database = _database(tmp_path)
    target = tmp_path / "outside.sqlite"
    target.write_text("keep me\n", encoding="utf-8")
    if link_kind == "symbolic":
        database.symlink_to(target)
    else:
        database.hardlink_to(target)

    with pytest.raises(ConversationStoreUnavailableError):
        ConversationStore(database, AGENT_ID)

    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_store_rejects_symlink_agent_data_root(tmp_path: Path) -> None:
    database = _database(tmp_path)
    real_root = database.parent
    linked_root = tmp_path / AGENT_ID
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ConversationStoreUnavailableError, match="unsafe"):
        ConversationStore(linked_root / DATABASE_NAME, AGENT_ID)


def test_store_rejects_non_private_agent_data_root(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.parent.chmod(0o750)

    with pytest.raises(ConversationStoreUnavailableError, match="unsafe"):
        ConversationStore(database, AGENT_ID)


def test_store_rejects_an_existing_database_over_the_configured_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    journal = EventJournal(database)
    journal.close()
    monkeypatch.setattr(sessions_module, "MAX_DATABASE_BYTES", 1)

    with pytest.raises(ConversationStoreUnavailableError, match="initialize"):
        ConversationStore(database, AGENT_ID)


def test_database_is_private_wal_and_bounded(tmp_path: Path) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    try:
        mode = os.stat(store.database_path, follow_symlinks=False).st_mode
        assert mode & 0o777 == 0o600
        assert store._connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert store._connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        page_size = store._connection.execute("PRAGMA page_size").fetchone()[0]
        maximum_pages = store._connection.execute("PRAGMA max_page_count").fetchone()[0]
        assert maximum_pages * page_size <= sessions_module.MAX_DATABASE_BYTES
    finally:
        store.close()


def test_database_path_is_fixed_to_the_agent_state_journal(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with pytest.raises(ValueError, match="named state.sqlite"):
        ConversationStore(database.with_name("conversations.sqlite3"), AGENT_ID)
    with pytest.raises(ValueError, match="belong to its Agent"):
        ConversationStore(tmp_path / DATABASE_NAME, AGENT_ID)

    assert not (database.parent / "conversations.sqlite3").exists()


def test_delete_removes_rows_without_vacuum_or_per_conversation_files(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    conversation_id = _id(120)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=_id(121),
            run_id=_id(122),
            user_content="logical deletion",
            expected_revision=0,
            started_event=_started(conversation_id, _id(121), _id(122)),
        )
        store.finalize_completed(
            _id(122), "done", _completed(conversation_id, _id(121), _id(122))
        )
        store.delete_conversation(conversation_id)

        connection = sqlite3.connect(database)
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM conversation_turns"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM completed_turn_contexts"
            ).fetchone() == (0,)
        finally:
            connection.close()
        assert {path.name for path in database.parent.iterdir()} <= {
            DATABASE_NAME,
            f"{DATABASE_NAME}-wal",
            f"{DATABASE_NAME}-shm",
        }
    finally:
        store.close()


def test_turn_capacity_is_rejected_by_snapshot_and_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_module, "MAX_TURNS_PER_CONVERSATION", 1)
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id = _id(900)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=_id(901),
            run_id=_id(902),
            user_content="first",
            expected_revision=0,
            started_event=_started(conversation_id, _id(901), _id(902)),
        )
        store.finalize_noncompleted(_id(902), "failed")

        with pytest.raises(
            ConversationTurnCapacityError,
            match="turn capacity is exhausted",
        ):
            store.snapshot_for_turn(conversation_id)
        with pytest.raises(ConversationTurnCapacityError):
            store.begin_turn(
                conversation_id,
                turn_id=_id(903),
                run_id=_id(904),
                user_content="must not start",
                expected_revision=2,
                started_event=_started(conversation_id, _id(903), _id(904)),
            )
        assert store.get_conversation(conversation_id).turns[0].status == "failed"
        assert store._connection.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ?", (_id(904),)
        ).fetchone() == (0,)
    finally:
        store.close()


def test_two_connections_competing_for_last_turn_only_commit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions_module, "MAX_TURNS_PER_CONVERSATION", 1)
    database = _database(tmp_path)
    first = ConversationStore(database, AGENT_ID)
    second = ConversationStore(database, AGENT_ID)
    conversation_id = _id(910)
    try:
        first.create_conversation(conversation_id=conversation_id)
        assert first.snapshot_for_turn(conversation_id).turns_remaining == 1
        assert second.snapshot_for_turn(conversation_id).turns_remaining == 1
        first.begin_turn(
            conversation_id,
            turn_id=_id(911),
            run_id=_id(912),
            user_content="winner",
            expected_revision=0,
            started_event=_started(conversation_id, _id(911), _id(912)),
        )
        with pytest.raises(ConversationConflictError):
            second.begin_turn(
                conversation_id,
                turn_id=_id(913),
                run_id=_id(914),
                user_content="loser",
                expected_revision=0,
                started_event=_started(conversation_id, _id(913), _id(914)),
            )
        assert len(first.get_conversation(conversation_id).turns) == 1
    finally:
        if first._turn_for_run(_id(912)) is not None:
            first.finalize_noncompleted(_id(912), "interrupted")
        first.close()
        second.close()


def test_summary_projection_is_single_row_restart_safe_and_delete_cascades(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    bundle = _summary_bundle()
    snapshot = SemanticSummaryV2Snapshot.create(
        source_bundles=(bundle,),
        model_profile_digest="d" * 64,
        renderer_version=CONTEXT_RENDERER_VERSION,
        section_registry_version=PROMPT_SECTION_REGISTRY_VERSION,
        content=SemanticSummaryContent(facts=("FACT-17",)),
        provider_request_digest="e" * 64,
        input_tokens=100,
        output_tokens=10,
    )
    source_digest = summary_v2_source_digest((bundle,))
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=bundle.conversation_id)
        generated = store.write_summary_projection(
            bundle.conversation_id,
            status="generated",
            source_digest=source_digest,
            snapshot=snapshot,
        )
        assert generated.snapshot == snapshot
        store.write_summary_projection(
            bundle.conversation_id,
            status="reused",
            source_digest=source_digest,
            snapshot=snapshot,
        )
        assert store._connection.execute(
            "SELECT COUNT(*) FROM conversation_summary_projections"
        ).fetchone() == (1,)
    finally:
        store.close()

    reopened = ConversationStore(database, AGENT_ID)
    try:
        restored = reopened.read_summary_projection(bundle.conversation_id)
        assert restored is not None
        assert restored.status == "reused"
        assert restored.snapshot == snapshot
        reopened.delete_conversation(bundle.conversation_id)
        assert reopened._connection.execute(
            "SELECT COUNT(*) FROM conversation_summary_projections"
        ).fetchone() == (0,)
    finally:
        reopened.close()


def test_explicit_continuation_preserves_source_and_carries_bounded_projection(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    source_id = _id(970)
    try:
        store.create_conversation("source", conversation_id=source_id)
        store.begin_turn(
            source_id,
            turn_id=_id(971),
            run_id=_id(972),
            user_content="remember CONT-17",
            expected_revision=0,
            started_event=_started(source_id, _id(971), _id(972)),
        )
        store.finalize_completed(
            _id(972), "CONT-17 remembered", _completed(source_id, _id(971), _id(972))
        )

        continued, included, omitted = store.create_continuation(
            source_id, title="continued", conversation_id=_id(973)
        )
        assert (included, omitted) == (1, 0)
        snapshot = store.snapshot_for_turn(continued.conversation_id)
        assert snapshot.turn_count == 0
        assert snapshot.continuation_context is not None
        value = json.loads(snapshot.continuation_context)
        assert value["semantic_boundary"] == "untrusted_continuation_projection"
        assert value["completed_turns"][0]["items"][-1]["content"] == (
            "CONT-17 remembered"
        )

        store.delete_conversation(source_id)
        assert store.get_conversation(continued.conversation_id).title == "continued"
        assert store.snapshot_for_turn(continued.conversation_id).continuation_context
        store.delete_conversation(continued.conversation_id)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM conversation_continuations"
        ).fetchone() == (0,)
    finally:
        store.close()


def test_failed_turn_terminal_summary_is_bounded_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    timestamps = iter(
        (
            "2026-07-22T00:00:00.000Z",
            "2026-07-22T00:00:01.000Z",
            "2026-07-22T00:00:04.000Z",
        )
    )
    monkeypatch.setattr(sessions_module, "utc_now", lambda: next(timestamps))
    conversation_id, turn_id, run_id = _id(980), _id(981), _id(982)
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="write a poem",
            expected_revision=0,
            started_event=replace(
                _started(conversation_id, turn_id, run_id),
                occurred_at="2026-07-22T00:00:01.000Z",
            ),
        )
        store.finalize_noncompleted(
            run_id,
            "failed",
            replace(
                _event(
                    kind="run.failed",
                    seq=2,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    payload={
                        "code": "model_first_frame_timeout",
                        "message": "provider detail must remain private",
                        "retryable": True,
                        "usage": _usage(complete=False),
                    },
                ),
                occurred_at="2026-07-22T00:00:04.000Z",
            ),
        )
        terminal = store.get_conversation(conversation_id).turns[0].terminal
        assert terminal is not None
        assert terminal.to_dict() == {
            "version": "turn-terminal-v1",
            "code": "model_first_frame_timeout",
            "stage": "model",
            "retryable": True,
            "duration_ms": 3_000,
        }
        assert "provider detail" not in json.dumps(terminal.to_dict())
    finally:
        store.close()

    reopened = ConversationStore(database, AGENT_ID)
    try:
        terminal = reopened.get_conversation(conversation_id).turns[0].terminal
        assert terminal is not None
        assert terminal.code == "model_first_frame_timeout"
        assert terminal.duration_ms == 3_000
    finally:
        reopened.close()


def test_first_turn_auto_title_and_explicit_rename_are_deterministic(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ConversationStore(database, AGENT_ID)
    conversation_id, turn_id, run_id = _id(983), _id(984), _id(985)
    explicit_id = _id(986)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="  研究   2026 年的\nAgent UX  ",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        automatic = store.get_conversation(conversation_id)
        assert automatic.title == "研究 2026 年的 Agent UX"
        assert automatic.revision == 1

        renamed = store.rename_conversation(
            conversation_id,
            "长期体验研究",
            expected_revision=automatic.revision,
        )
        assert renamed.title == "长期体验研究"
        assert renamed.revision == 2
        assert renamed.active_run_id == run_id

        unchanged = store.rename_conversation(
            conversation_id,
            "长期体验研究",
            expected_revision=renamed.revision,
        )
        assert unchanged.revision == renamed.revision
        assert unchanged.updated_at == renamed.updated_at

        with pytest.raises(ConversationConflictError):
            store.rename_conversation(
                conversation_id,
                "陈旧覆盖",
                expected_revision=automatic.revision,
            )

        store.create_conversation("用户标题", conversation_id=explicit_id)
        store.begin_turn(
            explicit_id,
            turn_id=_id(987),
            run_id=_id(988),
            user_content="this must not replace the title",
            expected_revision=0,
            started_event=_started(explicit_id, _id(987), _id(988)),
        )
        assert store.get_conversation(explicit_id).title == "用户标题"
    finally:
        store.finalize_noncompleted(run_id, "interrupted")
        store.finalize_noncompleted(_id(988), "interrupted")
        store.close()

    reopened = ConversationStore(database, AGENT_ID)
    try:
        restored = reopened.get_conversation(conversation_id)
        assert restored.title == "长期体验研究"
        assert restored.revision == 3
    finally:
        reopened.close()


def test_automatic_title_derivation_failure_does_not_block_turn_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(1_050), _id(1_051), _id(1_052)

    def fail_derivation(_user_content: str) -> str:
        raise RuntimeError("injected title derivation failure")

    monkeypatch.setattr(
        "agent_builder_v2.sessions._automatic_title", fail_derivation
    )
    try:
        store.create_conversation(conversation_id=conversation_id)
        accepted = store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="这条消息仍须成功准入",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        restored = store.get_conversation(conversation_id)
        assert accepted.turn.status == "running"
        assert restored.active_run_id == run_id
        assert restored.revision == 1
        assert restored.title == "New conversation"
    finally:
        store.finalize_noncompleted(run_id, "interrupted")
        store.close()


def test_automatic_title_persistence_failure_does_not_rollback_admitted_turn(
    tmp_path: Path,
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id, turn_id, run_id = _id(1_053), _id(1_054), _id(1_055)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store._connection.execute(  # noqa: SLF001 - deliberate SQLite fault injection
            """
            CREATE TRIGGER reject_automatic_title
            BEFORE UPDATE OF title ON conversations
            BEGIN
                SELECT RAISE(ABORT, 'injected automatic title failure');
            END
            """
        )
        accepted = store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="标题写入失败也不能丢失这轮消息",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
        )
        restored = store.get_conversation(conversation_id)
        assert accepted.turn.status == "running"
        assert restored.active_run_id == run_id
        assert restored.revision == 1
        assert restored.title == "New conversation"
        assert restored.turns == (accepted.turn,)
    finally:
        store.finalize_noncompleted(run_id, "interrupted")
        store.close()


def test_recent_provider_calibration_samples_are_scope_bound_and_restart_safe(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id, turn_id, run_id = _id(989), _id(990), _id(991)
    boundary = _calibration_boundary(conversation_id, turn_id, run_id)
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=conversation_id)
        store.begin_turn(
            conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            user_content="calibration turn",
            expected_revision=0,
            started_event=_started(conversation_id, turn_id, run_id),
            context_projection=boundary,
        )
        for call_index, estimated, actual in (
            (1, 1_000, 250),
            (2, 1_200, 300),
        ):
            store.start_provider_usage(
                run_id,
                call_index,
                provider="ollama",
                model="qwen3.5:2b",
                profile_digest=boundary.model_profile_digest,
                context_plan_id=boundary.context_plan_id,
                toolset_digest=boundary.toolset_digest,
                estimated_input_tokens=estimated,
                hard_input_tokens=28_672,
            )
            store.complete_provider_usage(
                run_id, call_index, input_tokens=actual, output_tokens=10
            )
        store.start_provider_usage(
            run_id,
            3,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest=boundary.model_profile_digest,
            context_plan_id="context-" + "f" * 24,
            toolset_digest=boundary.toolset_digest,
            estimated_input_tokens=1_500,
            hard_input_tokens=28_672,
        )
        store.complete_provider_usage(
            run_id, 3, input_tokens=400, output_tokens=10
        )
        scope = {
            "profile_digest": boundary.model_profile_digest,
            "renderer_version": boundary.renderer_version,
            "toolset_digest": boundary.toolset_digest,
            "policy_digest": boundary.compression_policy_digest,
        }
        assert store.recent_provider_calibration_samples(**scope) == (
            (1_000, 250),
            (1_200, 300),
        )
        assert store.recent_provider_calibration_samples(
            **{**scope, "toolset_digest": "0" * 64}
        ) == ()
        with pytest.raises(ValueError, match="between 1 and 16"):
            store.recent_provider_calibration_samples(**scope, limit=17)
    finally:
        store.finalize_noncompleted(run_id, "interrupted")
        store.close()

    reopened = ConversationStore(database, AGENT_ID)
    try:
        assert reopened.recent_provider_calibration_samples(**scope, limit=1) == (
            (1_200, 300),
        )
    finally:
        reopened.close()


def test_provider_calibration_exact_sql_scope_is_not_starved_by_older_rows(
    tmp_path: Path,
) -> None:
    """More than 64 newer calls in another ToolSet cannot hide one match."""

    store = ConversationStore(_database(tmp_path), AGENT_ID)
    first_ids = (_id(1_100), _id(1_101), _id(1_102))
    second_ids = (_id(1_103), _id(1_104), _id(1_105))
    target_boundary = _calibration_boundary(*first_ids)
    other_boundary = _calibration_boundary(*second_ids)
    other_toolset = "0" * 64
    scope = {
        "profile_digest": target_boundary.model_profile_digest,
        "renderer_version": target_boundary.renderer_version,
        "toolset_digest": target_boundary.toolset_digest,
        "policy_digest": target_boundary.compression_policy_digest,
    }
    try:
        store.create_conversation(conversation_id=first_ids[0])
        store.begin_turn(
            first_ids[0],
            turn_id=first_ids[1],
            run_id=first_ids[2],
            user_content="old exact calibration sample",
            expected_revision=0,
            started_event=_started(*first_ids),
            context_projection=target_boundary,
        )
        for call_index in range(1, 65):
            is_target = call_index == 1
            store.start_provider_usage(
                first_ids[2],
                call_index,
                provider="ollama",
                model="qwen3.5:2b",
                profile_digest=target_boundary.model_profile_digest,
                context_plan_id=target_boundary.context_plan_id,
                toolset_digest=(
                    target_boundary.toolset_digest if is_target else other_toolset
                ),
                estimated_input_tokens=1_000 + call_index,
                hard_input_tokens=28_672,
            )
            store.complete_provider_usage(
                first_ids[2],
                call_index,
                input_tokens=200 + call_index,
                output_tokens=5,
            )
        store.finalize_noncompleted(first_ids[2], "interrupted")

        store.create_conversation(conversation_id=second_ids[0])
        store.begin_turn(
            second_ids[0],
            turn_id=second_ids[1],
            run_id=second_ids[2],
            user_content="newer foreign-scope samples",
            expected_revision=0,
            started_event=_started(*second_ids),
            context_projection=other_boundary,
        )
        for call_index in (1, 2):
            store.start_provider_usage(
                second_ids[2],
                call_index,
                provider="ollama",
                model="qwen3.5:2b",
                profile_digest=other_boundary.model_profile_digest,
                context_plan_id=other_boundary.context_plan_id,
                toolset_digest=other_toolset,
                estimated_input_tokens=2_000 + call_index,
                hard_input_tokens=28_672,
            )
            store.complete_provider_usage(
                second_ids[2],
                call_index,
                input_tokens=400 + call_index,
                output_tokens=5,
            )

        assert store.recent_provider_calibration_samples(**scope) == (
            (1_001, 201),
        )
    finally:
        store.finalize_noncompleted(second_ids[2], "interrupted")
        store.close()


def test_provider_usage_scope_migration_is_fail_closed_and_restart_safe(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    old_ids = (_id(1_110), _id(1_111), _id(1_112))
    old_boundary = _calibration_boundary(*old_ids)
    old_scope = {
        "profile_digest": old_boundary.model_profile_digest,
        "renderer_version": old_boundary.renderer_version,
        "toolset_digest": old_boundary.toolset_digest,
        "policy_digest": old_boundary.compression_policy_digest,
    }
    store = ConversationStore(database, AGENT_ID)
    try:
        store.create_conversation(conversation_id=old_ids[0])
        store.begin_turn(
            old_ids[0],
            turn_id=old_ids[1],
            run_id=old_ids[2],
            user_content="legacy provider usage",
            expected_revision=0,
            started_event=_started(*old_ids),
            context_projection=old_boundary,
        )
        store.start_provider_usage(
            old_ids[2],
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest=old_boundary.model_profile_digest,
            context_plan_id=old_boundary.context_plan_id,
            toolset_digest=old_boundary.toolset_digest,
            estimated_input_tokens=900,
            hard_input_tokens=28_672,
        )
        store.complete_provider_usage(
            old_ids[2], 1, input_tokens=300, output_tokens=5
        )
        store.finalize_noncompleted(old_ids[2], "interrupted")
    finally:
        store.close()

    legacy = sqlite3.connect(database)
    try:
        legacy.execute("PRAGMA foreign_keys = OFF")
        legacy.execute("DROP INDEX provider_usage_calibration_scope")
        for column in (
            "count_scope_digest",
            "policy_digest",
            "toolset_digest",
            "renderer_version",
        ):
            legacy.execute(f"ALTER TABLE provider_usage DROP COLUMN {column}")
        legacy.commit()
    finally:
        legacy.close()

    migrated = ConversationStore(database, AGENT_ID)
    new_ids = (_id(1_113), _id(1_114), _id(1_115))
    new_boundary = _calibration_boundary(*new_ids)
    try:
        columns = {
            row[1]
            for row in migrated._connection.execute(  # noqa: SLF001
                "PRAGMA table_info(provider_usage)"
            )
        }
        assert {
            "renderer_version",
            "toolset_digest",
            "policy_digest",
            "count_scope_digest",
        } <= columns
        assert migrated.recent_provider_calibration_samples(**old_scope) == ()

        migrated.create_conversation(conversation_id=new_ids[0])
        migrated.begin_turn(
            new_ids[0],
            turn_id=new_ids[1],
            run_id=new_ids[2],
            user_content="post-migration provider usage",
            expected_revision=0,
            started_event=_started(*new_ids),
            context_projection=new_boundary,
        )
        migrated.start_provider_usage(
            new_ids[2],
            1,
            provider="ollama",
            model="qwen3.5:2b",
            profile_digest=new_boundary.model_profile_digest,
            context_plan_id=new_boundary.context_plan_id,
            toolset_digest=new_boundary.toolset_digest,
            estimated_input_tokens=1_200,
            hard_input_tokens=28_672,
        )
        migrated.complete_provider_usage(
            new_ids[2], 1, input_tokens=400, output_tokens=5
        )
        assert migrated.recent_provider_calibration_samples(**old_scope) == (
            (1_200, 400),
        )
        migrated.finalize_noncompleted(new_ids[2], "interrupted")
    finally:
        migrated.close()

    reopened = ConversationStore(database, AGENT_ID)
    try:
        assert reopened.recent_provider_calibration_samples(**old_scope) == (
            (1_200, 400),
        )
    finally:
        reopened.close()


def test_conversation_page_never_loads_out_of_page_turn_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConversationStore(_database(tmp_path), AGENT_ID)
    conversation_id = _id(1_120)
    try:
        created = store.create_conversation(conversation_id=conversation_id)
        rows = []
        for position in range(1, MAX_TURNS_PER_CONVERSATION + 1):
            rows.append(
                (
                    _id(2_000 + position),
                    conversation_id,
                    _id(3_000 + position),
                    position,
                    "completed",
                    f"user-{position}-" + "u" * 7_900,
                    f"assistant-{position}-" + "a" * 20_000,
                    created.created_at,
                    created.created_at,
                )
            )
        store._connection.execute("BEGIN IMMEDIATE")  # noqa: SLF001
        store._connection.executemany(  # noqa: SLF001
            """
            INSERT INTO conversation_turns(
                turn_id, conversation_id, run_id, position, status,
                user_content, assistant_content, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        # A selected full-history loader would choke on these deliberately
        # non-text legacy values; the latest page must never materialize them.
        store._connection.execute(  # noqa: SLF001
            """
            UPDATE conversation_turns
            SET user_content = X'FF', assistant_content = X'FE'
            WHERE conversation_id = ? AND position = 1
            """,
            (conversation_id,),
        )
        store._connection.execute(  # noqa: SLF001
            """
            UPDATE conversations
            SET revision = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            (
                MAX_TURNS_PER_CONVERSATION,
                created.created_at,
                conversation_id,
            ),
        )
        store._connection.commit()  # noqa: SLF001

        def forbidden_full_loader(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("full conversation body loader was called")

        monkeypatch.setattr(store, "_conversation_turns_locked", forbidden_full_loader)
        monkeypatch.setattr(store, "_turn_rows", forbidden_full_loader)
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)  # noqa: SLF001
        page = store.get_conversation_page(
            conversation_id,
            limit=4,
            expected_revision=MAX_TURNS_PER_CONVERSATION,
        )
        store._connection.set_trace_callback(None)  # noqa: SLF001

        assert [turn.position for turn in page.turns] == [125, 126, 127, 128]
        assert page.summary.turn_count == MAX_TURNS_PER_CONVERSATION
        assert page.eligible_turn_count == MAX_TURNS_PER_CONVERSATION
        assert all("user-1-" not in turn.user_content for turn in page.turns)
        assert any(
            "ORDER BY position DESC LIMIT 4" in statement
            for statement in statements
        )

        older = store.get_conversation_page(
            conversation_id,
            limit=4,
            before_position=125,
            expected_revision=MAX_TURNS_PER_CONVERSATION,
        )
        assert [turn.position for turn in older.turns] == [121, 122, 123, 124]
        with pytest.raises(ConversationConflictError, match="revision changed"):
            store.get_conversation_page(
                conversation_id,
                limit=4,
                expected_revision=MAX_TURNS_PER_CONVERSATION - 1,
            )
    finally:
        store.close()

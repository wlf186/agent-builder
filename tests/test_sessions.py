"""Conversation state, recovery, transaction, and containment boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
from threading import Event

import pytest

import agent_builder_v2.sessions as sessions_module
from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.sessions import (
    DATABASE_NAME,
    MAX_ASSISTANT_CONTENT_BYTES,
    MAX_LIST_LIMIT,
    MAX_TITLE_BYTES,
    MAX_USER_CONTENT_BYTES,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStore,
    ConversationStoreUnavailableError,
    conversation_message_id,
)
from agent_builder_v2.state import EventJournal


AGENT_ID = "00000000-0000-4000-8000-000000000001"


def _id(value: int) -> str:
    return f"{value:032x}"


def _database(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "agents" / AGENT_ID
    root.mkdir(parents=True, mode=0o700)
    return root / DATABASE_NAME


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
        payload=payload if payload is not None else {"kind": kind},
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

        store.finalize_completed(run_id, "done", terminal)
        assert [event["kind"] for event in journal.events_for_run(run_id)] == [
            "run.started",
            "run.completed",
        ]
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
        assert [event["seq"] for event in events] == [1, 2, 3, 4]
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
        assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
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
        assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
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
        assert [event["seq"] for event in events] == [1, 2, 4, 5]
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


def test_recovery_capacity_counts_ephemeral_sequence_gaps(
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

        with pytest.raises(ConversationConflictError, match="event capacity"):
            store.recover_running_as_interrupted()

        restored = store.get_conversation(conversation_id)
        assert restored.active_run_id == run_id
        assert restored.turns[0].status == "running"
        assert [event["seq"] for event in journal.events_for_run(run_id)] == [
            1,
            2,
            5,
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
        finally:
            connection.close()
        assert {path.name for path in database.parent.iterdir()} <= {
            DATABASE_NAME,
            f"{DATABASE_NAME}-wal",
            f"{DATABASE_NAME}-shm",
        }
    finally:
        store.close()

"""Persistence boundaries for the agent-scoped semantic EventJournal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.contracts import RUN_CURSOR_RESERVED_THROUGH, EventEnvelope
from agent_builder_v2.replay import RunIdentity
from agent_builder_v2.state import (
    EventJournal,
    JournalCorruptionError,
    JournalUnavailableError,
)
from agent_builder_v2.tools import prototype_tool_specs, toolset_digest


PLAN_DIGEST = "a" * 64


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


def _usage(*, complete: bool = True) -> dict[str, object]:
    return {
        "input_tokens": 1,
        "output_tokens": 1,
        "last_input_tokens": 1,
        "complete": complete,
    }


def _completed() -> dict[str, object]:
    return {
        "reason": "end_turn",
        "model_iterations": 1,
        "usage": _usage(),
    }


def _failed() -> dict[str, object]:
    return {
        "code": "control_restarted",
        "message": "Control Plane restarted before terminal publication.",
        "retryable": True,
        "usage": _usage(complete=False),
    }


def _event(
    *,
    seq: int,
    durability: str,
    kind: str,
    run_id: str = "run-a",
    payload: dict[str, object] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"event-{seq}",
        agent_id="agent-a",
        conversation_id="conversation-a",
        turn_id="turn-a",
        run_id=run_id,
        seq=seq,
        occurred_at=f"2026-07-17T00:00:0{seq}.000Z",
        kind=kind,
        durability=durability,  # type: ignore[arg-type]
        payload=payload if payload is not None else {"seq": seq},
    )


def _canonical_event(
    seq: int,
    kind: str,
    payload: dict[str, object],
    *,
    conversation_id: str = "1" * 32,
    turn_id: str = "2" * 32,
    run_id: str = "3" * 32,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"{seq:032x}",
        agent_id=PROTOTYPE_AGENT_ID,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        seq=seq,
        occurred_at=f"2026-07-18T00:00:00.{seq:03d}Z",
        kind=kind,
        durability="durable",
        payload=payload,
    )


def _install_managed_run(
    journal: EventJournal,
    events: tuple[EventEnvelope, ...],
    *,
    reserved_through: int = RUN_CURSOR_RESERVED_THROUGH,
) -> None:
    first = events[0]
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.kind in {"run.completed", "run.failed", "run.cancelled"}
        ),
        None,
    )
    encoded = [
        json.dumps(
            event.to_dict(), ensure_ascii=False, separators=(",", ":")
        )
        for event in events
    ]
    with journal._connection:
        journal._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS run_journal_state (
                run_id TEXT PRIMARY KEY,
                oldest_available_seq INTEGER NOT NULL,
                latest_durable_seq INTEGER NOT NULL,
                reserved_through INTEGER NOT NULL,
                terminal_seq INTEGER,
                terminal_kind TEXT,
                availability TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                durable_bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_snapshots (
                run_id TEXT PRIMARY KEY,
                projection_version TEXT NOT NULL,
                through_seq INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                ephemeral_loss INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        journal._connection.execute(
            "INSERT INTO conversations(conversation_id, agent_id) VALUES (?, ?)",
            (first.conversation_id, first.agent_id),
        )
        journal._connection.execute(
            """
            INSERT INTO conversation_turns(turn_id, conversation_id, run_id)
            VALUES (?, ?, ?)
            """,
            (first.turn_id, first.conversation_id, first.run_id),
        )
        for event, raw in zip(events, encoded, strict=True):
            journal._connection.execute(
                """
                INSERT INTO events(run_id, seq, kind, occurred_at, envelope_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event.run_id, event.seq, event.kind, event.occurred_at, raw),
            )
        journal._connection.execute(
            """
            INSERT INTO run_journal_state(
                run_id, oldest_available_seq, latest_durable_seq,
                reserved_through, terminal_seq, terminal_kind, availability,
                event_count, durable_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, 'full', ?, ?)
            """,
            (
                first.run_id,
                events[0].seq,
                events[-1].seq,
                reserved_through,
                terminal.seq if terminal is not None else None,
                terminal.kind if terminal is not None else None,
                len(events),
                sum(len(raw.encode("utf-8")) for raw in encoded),
            ),
        )


def _install_completed_snapshot_only_run(journal: EventJournal) -> str:
    old_ids = ("1" * 32, "2" * 32, "3" * 32)
    new_ids = ("4" * 32, "5" * 32, "6" * 32)
    for identity in (old_ids, new_ids):
        _install_managed_run(
            journal,
            (
                _canonical_event(
                    1,
                    "run.started",
                    _started_payload(),
                    conversation_id=identity[0],
                    turn_id=identity[1],
                    run_id=identity[2],
                ),
                _canonical_event(
                    2,
                    "run.completed",
                    _completed(),
                    conversation_id=identity[0],
                    turn_id=identity[1],
                    run_id=identity[2],
                ),
            ),
        )
    assert journal.prune_to_recent_runs(1) == 2
    return old_ids[2]


def test_event_envelope_uses_the_current_protocol_schema() -> None:
    assert _event(
        seq=1, durability="durable", kind="run.started"
    ).to_dict()["schema_version"] == "2.2-prototype"


def test_journal_persists_only_durable_events_across_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "agent" / "state.sqlite3"
    events = [
        _event(seq=1, durability="durable", kind="assistant.block.started"),
        _event(seq=2, durability="ephemeral", kind="assistant.block.delta"),
        _event(seq=3, durability="durable", kind="assistant.block.finished"),
    ]

    journal = EventJournal(database_path)
    try:
        for event in events:
            journal.append(event)
        persisted = journal.events_for_run("run-a")
    finally:
        journal.close()

    assert database_path.is_file()
    assert [event["seq"] for event in persisted] == [1, 3]
    assert [event["kind"] for event in persisted] == [
        "assistant.block.started",
        "assistant.block.finished",
    ]
    assert all(event["durability"] == "durable" for event in persisted)

    reopened = EventJournal(database_path)
    try:
        assert reopened.events_for_run("run-a") == persisted
        assert reopened.events_for_run("another-run") == []
    finally:
        reopened.close()


def test_journal_prunes_whole_runs_by_most_recent_append(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "state.sqlite3")
    try:
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="old")
        )
        journal.append(
            _event(seq=2, durability="durable", kind="run.completed", run_id="old")
        )
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="middle")
        )
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="new")
        )
        # The most recently appended row determines Run recency, even when a
        # different Run was first observed later.
        journal.append(
            _event(
                seq=2,
                durability="durable",
                kind="run.completed",
                run_id="middle",
            )
        )

        deleted_rows = journal.prune_to_recent_runs(2)

        assert deleted_rows == 2
        assert journal.events_for_run("old") == []
        assert [event["seq"] for event in journal.events_for_run("middle")] == [1, 2]
        assert [event["seq"] for event in journal.events_for_run("new")] == [1]
    finally:
        journal.close()


def test_journal_prune_never_removes_a_protected_active_run(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "state.sqlite3")
    try:
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="active")
        )
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="old")
        )
        journal.append(
            _event(seq=2, durability="durable", kind="run.completed", run_id="old")
        )
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="new")
        )
        journal.append(
            _event(seq=2, durability="durable", kind="run.completed", run_id="new")
        )

        journal.prune_to_recent_runs(1, ("active",))

        assert [event["kind"] for event in journal.events_for_run("active")] == [
            "run.started"
        ]
        assert journal.events_for_run("old") == []
        assert [event["kind"] for event in journal.events_for_run("new")] == [
            "run.started",
            "run.completed",
        ]
    finally:
        journal.close()


@pytest.mark.parametrize("maximum_runs", [0, -1, 10_001])
def test_journal_rejects_invalid_prune_limits(
    tmp_path: Path, maximum_runs: int
) -> None:
    journal = EventJournal(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(ValueError, match="maximum_runs"):
            journal.prune_to_recent_runs(maximum_runs)
    finally:
        journal.close()


def test_journal_rejects_oversized_durable_event_without_partial_write(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "state.sqlite3")
    oversized = _event(
        seq=1,
        durability="durable",
        kind="assistant.block.finished",
        payload={"content": "x" * 65_536},
    )
    try:
        with pytest.raises(ValueError, match="journal limit"):
            journal.append(oversized)
        assert journal.events_for_run("run-a") == []
    finally:
        journal.close()


def test_read_only_journal_and_late_prune_fault_leave_no_partial_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent" / "state.sqlite"
    journal = EventJournal(database)
    old_events = (
        _canonical_event(
            1,
            "run.started",
            _started_payload(),
            conversation_id="1" * 32,
            turn_id="2" * 32,
            run_id="3" * 32,
        ),
        _canonical_event(
            2,
            "run.completed",
            _completed(),
            conversation_id="1" * 32,
            turn_id="2" * 32,
            run_id="3" * 32,
        ),
    )
    new_events = (
        _canonical_event(
            1,
            "run.started",
            _started_payload(),
            conversation_id="4" * 32,
            turn_id="5" * 32,
            run_id="6" * 32,
        ),
        _canonical_event(
            2,
            "run.completed",
            _completed(),
            conversation_id="4" * 32,
            turn_id="5" * 32,
            run_id="6" * 32,
        ),
    )
    try:
        journal.append(
            _event(seq=1, durability="durable", kind="run.started", run_id="readonly")
        )
        journal._connection.execute("PRAGMA query_only=ON")
        with pytest.raises(JournalUnavailableError, match="append"):
            journal.append(
                _event(
                    seq=2,
                    durability="durable",
                    kind="run.completed",
                    run_id="readonly",
                )
            )
        journal._connection.execute("PRAGMA query_only=OFF")
        assert [event["seq"] for event in journal.events_for_run("readonly")] == [1]

        _install_managed_run(journal, old_events)
        _install_managed_run(journal, new_events)
        journal._connection.executescript(
            """
            CREATE TRIGGER qualification_abort_prune_tail
            BEFORE UPDATE OF availability ON run_journal_state
            WHEN NEW.availability = 'snapshot_only'
            BEGIN
                SELECT RAISE(ABORT, 'qualification prune fault');
            END;
            """
        )

        with pytest.raises(JournalUnavailableError, match="prune"):
            journal.prune_to_recent_runs(1)

        assert [event["kind"] for event in journal.events_for_run("3" * 32)] == [
            "run.started",
            "run.completed",
        ]
        assert journal._connection.execute(
            "SELECT availability FROM run_journal_state WHERE run_id = ?",
            ("3" * 32,),
        ).fetchone() == ("full",)
        assert journal._connection.execute(
            "SELECT COUNT(*) FROM run_snapshots WHERE run_id = ?", ("3" * 32,)
        ).fetchone() == (0,)

        journal._connection.execute("DROP TRIGGER qualification_abort_prune_tail")
        journal._connection.commit()
        # The earlier unmanaged read-only fixture contributes one additional
        # deleted row; the managed Run contributes its two semantic events.
        assert journal.prune_to_recent_runs(1) == 3
        assert journal.events_for_run("3" * 32) == []
        assert journal._connection.execute(
            "SELECT availability FROM run_journal_state WHERE run_id = ?",
            ("3" * 32,),
        ).fetchone() == ("snapshot_only",)
    finally:
        journal.close()


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_journal_rejects_linked_database_without_touching_target(
    tmp_path: Path, link_kind: str
) -> None:
    target = tmp_path / "outside-state"
    target.write_text("keep me\n", encoding="utf-8")
    database = tmp_path / "agent" / "state.sqlite"
    database.parent.mkdir()
    if link_kind == "symbolic":
        database.symlink_to(target)
    else:
        database.hardlink_to(target)

    with pytest.raises(JournalUnavailableError):
        EventJournal(database)

    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_journal_rejects_linked_wal_sidecar_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-wal"
    target.write_text("keep me\n", encoding="utf-8")
    database = tmp_path / "agent" / "state.sqlite"
    database.parent.mkdir()
    Path(f"{database}-wal").symlink_to(target)

    with pytest.raises(JournalUnavailableError):
        EventJournal(database)

    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_replay_returns_bounded_pages_gap_and_deterministic_snapshot(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    events = (
        _canonical_event(1, "run.started", _started_payload()),
        _canonical_event(
            2,
            "assistant.block.started",
            {"block_id": "answer", "block_type": "content"},
        ),
        _canonical_event(
            4,
            "assistant.block.finished",
            {"block_id": "answer", "content": "durable full content"},
        ),
        _canonical_event(5, "run.completed", _completed()),
    )
    try:
        for event in events:
            journal.append(event)

        first = journal.replay(events[0].run_id, limit=2)
        second = journal.replay(events[0].run_id, after=2, limit=1)

        assert first is not None and second is not None
        assert [event.seq for event in first.events] == [1, 2]
        assert first.next_cursor == 2
        assert first.latest_cursor == 5
        assert first.has_more is True
        assert [event.seq for event in second.events] == [4]
        assert [gap.to_dict() for gap in second.gaps] == [
            {
                "from_seq": 3,
                "to_seq": 3,
                "reason": "ephemeral_not_durable",
            }
        ]
        assert second.snapshot.complete is True
        assert second.snapshot.digest == first.snapshot.digest
    finally:
        journal.close()


def test_replay_checks_expected_identity_and_future_cursor(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    start = _canonical_event(1, "run.started", _started_payload())
    try:
        journal.append(start)
        with pytest.raises(KeyError, match="expected identity"):
            journal.replay(
                start.run_id,
                expected_identity=RunIdentity(
                    start.agent_id,
                    "9" * 32,
                    start.turn_id,
                    start.run_id,
                ),
            )
        with pytest.raises(ValueError, match="newer"):
            journal.replay(start.run_id, after=2)
    finally:
        journal.close()


def test_replay_rejects_whole_batch_when_a_later_row_is_corrupt(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    start = _canonical_event(1, "run.started", _started_payload())
    terminal = _canonical_event(2, "run.completed", _completed())
    raw = json.dumps(
        terminal.to_dict(), ensure_ascii=False, separators=(",", ":")
    ).replace('"seq":2', '"seq":2,"seq":2')
    try:
        journal.append(start)
        with journal._connection:  # fault injection below the public boundary
            journal._connection.execute(
                """
                INSERT INTO events(run_id, seq, kind, occurred_at, envelope_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (terminal.run_id, 2, terminal.kind, terminal.occurred_at, raw),
            )

        with pytest.raises(JournalCorruptionError):
            journal.replay(start.run_id)
    finally:
        journal.close()


def test_journal_fails_closed_if_named_parent_is_swapped_during_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_builder_v2 import state

    database = tmp_path / "agent" / "state.sqlite"
    moved = tmp_path / "anchored-agent"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    original_connect = state.sqlite3.connect
    swapped = False

    def connect_after_swap(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        if not swapped:
            swapped = True
            database.parent.rename(moved)
            database.parent.symlink_to(outside, target_is_directory=True)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(state.sqlite3, "connect", connect_after_swap)

    with pytest.raises(JournalUnavailableError, match="directory path changed"):
        EventJournal(database)

    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert not (outside / "state.sqlite").exists()


def test_managed_append_updates_journal_state_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    start = _canonical_event(1, "run.started", _started_payload())
    block = _canonical_event(
        2,
        "assistant.block.started",
        {"block_id": "managed", "block_type": "content"},
    )
    try:
        _install_managed_run(journal, (start,))
        before = journal._connection.execute(
            """
            SELECT event_count, durable_bytes FROM run_journal_state
            WHERE run_id = ?
            """,
            (start.run_id,),
        ).fetchone()

        journal.append(block)

        after = journal._connection.execute(
            """
            SELECT latest_durable_seq, event_count, durable_bytes
            FROM run_journal_state WHERE run_id = ?
            """,
            (start.run_id,),
        ).fetchone()
        assert before is not None and after is not None
        encoded_block = json.dumps(
            block.to_dict(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        assert after == (2, 2, before[1] + len(encoded_block))

        with journal._connection:
            journal._connection.execute(
                """
                UPDATE run_journal_state SET terminal_seq = 2,
                    terminal_kind = 'run.failed' WHERE run_id = ?
                """,
                (start.run_id,),
            )
        rejected = _canonical_event(
            3,
            "assistant.block.discarded",
            {"block_id": "managed", "reason": "runtime_failure"},
        )
        with pytest.raises(JournalCorruptionError):
            journal.append(rejected)
        assert [item["seq"] for item in journal.events_for_run(start.run_id)] == [
            1,
            2,
        ]
    finally:
        journal.close()


def test_managed_reserved_recovery_replay_and_snapshot_aware_prune(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    old_ids = ("1" * 32, "2" * 32, "3" * 32)
    new_ids = ("4" * 32, "5" * 32, "6" * 32)
    old_events = (
        _canonical_event(
            1,
            "run.started",
            _started_payload(),
            conversation_id=old_ids[0],
            turn_id=old_ids[1],
            run_id=old_ids[2],
        ),
        _canonical_event(
            513,
            "run.failed",
            _failed(),
            conversation_id=old_ids[0],
            turn_id=old_ids[1],
            run_id=old_ids[2],
        ),
    )
    new_events = (
        _canonical_event(
            1,
            "run.started",
            _started_payload(),
            conversation_id=new_ids[0],
            turn_id=new_ids[1],
            run_id=new_ids[2],
        ),
        _canonical_event(
            2,
            "run.completed",
            _completed(),
            conversation_id=new_ids[0],
            turn_id=new_ids[1],
            run_id=new_ids[2],
        ),
    )
    try:
        _install_managed_run(journal, old_events)
        _install_managed_run(journal, new_events)

        retained = journal.replay(old_ids[2], after=1)
        assert retained is not None
        assert [gap.to_dict() for gap in retained.gaps] == [
            {
                "from_seq": 2,
                "to_seq": 512,
                "reason": "ephemeral_not_durable",
            }
        ]

        assert journal.prune_to_recent_runs(1) == 2
        assert journal.events_for_run(old_ids[2]) == []
        state = journal._connection.execute(
            """
            SELECT availability, event_count, durable_bytes
            FROM run_journal_state WHERE run_id = ?
            """,
            (old_ids[2],),
        ).fetchone()
        assert state == ("snapshot_only", 0, 0)

        snapshotted = journal.replay(old_ids[2], after=0)
        assert snapshotted is not None
        assert snapshotted.availability == "snapshot_only"
        assert snapshotted.events == ()
        assert snapshotted.snapshot.complete is True
        assert [gap.to_dict() for gap in snapshotted.gaps] == [
            {"from_seq": 1, "to_seq": 513, "reason": "retention"}
        ]
        assert [item["seq"] for item in journal.events_for_run(new_ids[2])] == [
            1,
            2,
        ]
    finally:
        journal.close()


def test_snapshot_only_rejects_invalid_document_with_recomputed_digests(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    old_ids = ("1" * 32, "2" * 32, "3" * 32)
    new_ids = ("4" * 32, "5" * 32, "6" * 32)
    try:
        _install_managed_run(
            journal,
            (
                _canonical_event(
                    1,
                    "run.started",
                    _started_payload(),
                    conversation_id=old_ids[0],
                    turn_id=old_ids[1],
                    run_id=old_ids[2],
                ),
                _canonical_event(
                    2,
                    "run.completed",
                    _completed(),
                    conversation_id=old_ids[0],
                    turn_id=old_ids[1],
                    run_id=old_ids[2],
                ),
            ),
        )
        _install_managed_run(
            journal,
            (
                _canonical_event(
                    1,
                    "run.started",
                    _started_payload(),
                    conversation_id=new_ids[0],
                    turn_id=new_ids[1],
                    run_id=new_ids[2],
                ),
                _canonical_event(
                    2,
                    "run.completed",
                    _completed(),
                    conversation_id=new_ids[0],
                    turn_id=new_ids[1],
                    run_id=new_ids[2],
                ),
            ),
        )
        assert journal.prune_to_recent_runs(1) == 2
        row = journal._connection.execute(
            "SELECT snapshot_json FROM run_snapshots WHERE run_id = ?",
            (old_ids[2],),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        forged = json.loads(row[0])
        forged["document"]["terminal"]["payload"] = {"reason": "end_turn"}
        unsigned = {
            key: value for key, value in forged.items() if key != "digest"
        }
        forged["digest"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        forged_raw = json.dumps(
            forged,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with journal._connection:
            journal._connection.execute(
                """
                UPDATE run_snapshots SET snapshot_json = ?, source_digest = ?
                WHERE run_id = ?
                """,
                (
                    forged_raw,
                    hashlib.sha256(forged_raw.encode("utf-8")).hexdigest(),
                    old_ids[2],
                ),
            )

        with pytest.raises(JournalCorruptionError, match="projection is corrupt"):
            journal.replay(old_ids[2])
    finally:
        journal.close()


@pytest.mark.parametrize(
    "tamper",
    ["oldest_available_seq", "terminal_kind", "ephemeral_loss"],
)
def test_snapshot_only_binds_recomputed_snapshot_to_outer_sql_metadata(
    tmp_path: Path, tamper: str
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    try:
        run_id = _install_completed_snapshot_only_run(journal)
        row = journal._connection.execute(
            "SELECT snapshot_json FROM run_snapshots WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        forged = json.loads(row[0])
        unsigned = {
            key: value for key, value in forged.items() if key != "digest"
        }
        forged["digest"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        forged_raw = json.dumps(
            forged,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with journal._connection:
            journal._connection.execute(
                """
                UPDATE run_snapshots SET snapshot_json = ?, source_digest = ?
                WHERE run_id = ?
                """,
                (
                    forged_raw,
                    hashlib.sha256(forged_raw.encode("utf-8")).hexdigest(),
                    run_id,
                ),
            )
            if tamper == "oldest_available_seq":
                journal._connection.execute(
                    """
                    UPDATE run_journal_state SET oldest_available_seq = 1
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
            elif tamper == "terminal_kind":
                journal._connection.execute(
                    """
                    UPDATE run_journal_state SET terminal_kind = 'run.failed'
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
            else:
                assert tamper == "ephemeral_loss"
                journal._connection.execute(
                    """
                    UPDATE run_snapshots SET ephemeral_loss = 1
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )

        with pytest.raises(JournalCorruptionError, match="metadata is corrupt"):
            journal.replay(run_id)
    finally:
        journal.close()


def test_full_replay_and_retention_reject_tampered_cursor_reservation(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    old_ids = ("7" * 32, "8" * 32, "9" * 32)
    new_ids = ("a" * 32, "b" * 32, "c" * 32)
    try:
        for conversation_id, turn_id, run_id in (old_ids, new_ids):
            _install_managed_run(
                journal,
                (
                    _canonical_event(
                        1,
                        "run.started",
                        _started_payload(),
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        run_id=run_id,
                    ),
                    _canonical_event(
                        2,
                        "run.completed",
                        _completed(),
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        run_id=run_id,
                    ),
                ),
            )
        with journal._connection:
            journal._connection.execute(
                """
                UPDATE run_journal_state SET reserved_through = ?
                WHERE run_id = ?
                """,
                (RUN_CURSOR_RESERVED_THROUGH + 1, old_ids[2]),
            )

        with pytest.raises(JournalCorruptionError, match="cursor reservation"):
            journal.replay(old_ids[2])
        with pytest.raises(JournalCorruptionError, match="retention state"):
            journal.prune_to_recent_runs(1)

        assert len(journal.events_for_run(old_ids[2])) == 2
        assert journal._connection.execute(
            "SELECT COUNT(*) FROM run_snapshots WHERE run_id = ?", (old_ids[2],)
        ).fetchone()[0] == 0
    finally:
        journal.close()


def test_snapshot_only_replay_rejects_tampered_cursor_reservation(
    tmp_path: Path,
) -> None:
    journal = EventJournal(tmp_path / "agent" / "state.sqlite")
    try:
        run_id = _install_completed_snapshot_only_run(journal)
        with journal._connection:
            journal._connection.execute(
                """
                UPDATE run_journal_state SET reserved_through = ?
                WHERE run_id = ?
                """,
                (RUN_CURSOR_RESERVED_THROUGH + 1, run_id),
            )

        with pytest.raises(JournalCorruptionError, match="cursor reservation"):
            journal.replay(run_id)
    finally:
        journal.close()

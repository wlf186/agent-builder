"""Persistence boundaries for the agent-scoped semantic EventJournal."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.state import EventJournal, JournalUnavailableError


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

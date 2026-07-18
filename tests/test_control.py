"""Capacity and byte-budget invariants for the control-plane RunService."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import agent_builder_v2.control as control_module
from agent_builder_v2.capsule import AgentCapsule, PROTOTYPE_AGENT_ID
from agent_builder_v2.contracts import TERMINAL_KINDS, EventEnvelope, StartRunCommand
from agent_builder_v2.control import (
    MAX_ACTIVE_RUNS,
    MAX_DURABLE_BYTES_PER_RUN,
    MAX_LIVE_EVENT_BYTES,
    MAX_LIVE_EVENTS,
    TERMINAL_EVENT_RESERVE,
    RunRecord,
    RunService,
    _atomic_worker_pid_record,
    _measure_run_tree,
    _marker_from_proc_stat,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class _MemoryJournal:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    def append(self, event: EventEnvelope) -> None:
        self.events.append(event)

    def prune_to_recent_runs(
        self,
        _maximum_runs: int,
        _protected_run_ids: tuple[str, ...] = (),
    ) -> int:
        return 0


class _FailingJournal(_MemoryJournal):
    def __init__(self, successful_appends: int) -> None:
        super().__init__()
        self.successful_appends = successful_appends

    def append(self, event: EventEnvelope) -> None:
        if len(self.events) >= self.successful_appends:
            raise OSError("simulated durable storage failure")
        super().append(event)


class _UnusedModelBroker:
    def new_run(self) -> object:
        return object()

    async def close(self) -> None:
        return None


def _record(run_id: str = "1" * 32) -> RunRecord:
    return RunRecord(
        agent_id=PROTOTYPE_AGENT_ID,
        conversation_id="2" * 32,
        turn_id="3" * 32,
        run_id=run_id,
    )


def _service(tmp_path: Path) -> tuple[RunService, _MemoryJournal]:
    service = RunService(
        tmp_path,
        SOURCE_ROOT,
        model_broker=_UnusedModelBroker(),  # type: ignore[arg-type]
    )
    journal = _MemoryJournal()
    service.journal = journal  # type: ignore[assignment]
    service.capsule = AgentCapsule(
        agent_id=PROTOTYPE_AGENT_ID,
        data_root=tmp_path / "data",
        runtime_root=tmp_path / "runtime",
        interpreter=Path(sys.executable),
    )
    return service, journal


def _placeholder_event() -> EventEnvelope:
    return EventEnvelope(
        event_id="4" * 32,
        agent_id=PROTOTYPE_AGENT_ID,
        conversation_id="2" * 32,
        turn_id="3" * 32,
        run_id="1" * 32,
        seq=1,
        occurred_at="2026-07-17T00:00:00.000Z",
        kind="assistant.block.started",
        durability="durable",
        payload={},
    )


def _install_fake_capsule_io(
    service: RunService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def create_run_root(capsule: AgentCapsule, run_id: str) -> Path:
        root = capsule.runtime_root / "runs" / run_id
        for child in ("home", "tmp", "xdg", "input", "work", "output"):
            (root / child).mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def remove_run_root(capsule: AgentCapsule, run_id: str) -> None:
        shutil.rmtree(capsule.runtime_root / "runs" / run_id, ignore_errors=False)

    monkeypatch.setattr(service.capsules, "create_run_root", create_run_root)
    monkeypatch.setattr(service.capsules, "remove_run_root", remove_run_root)
    monkeypatch.setattr(control_module, "_validate_sandbox_ready", lambda *_args: None)


def test_active_run_capacity_rejects_before_publishing(tmp_path: Path) -> None:
    service, journal = _service(tmp_path)
    for index in range(MAX_ACTIVE_RUNS):
        run_id = f"{index + 1:032x}"
        service.runs[run_id] = _record(run_id)

    with pytest.raises(ValueError, match="active Run capacity exhausted"):
        asyncio.run(
            service.start(
                StartRunCommand(agent_id=PROTOTYPE_AGENT_ID, message="one too many")
            )
        )

    assert len(service.runs) == MAX_ACTIVE_RUNS
    assert journal.events == []


def test_start_rejects_message_that_exceeds_utf8_command_budget(
    tmp_path: Path,
) -> None:
    service, journal = _service(tmp_path)

    with pytest.raises(ValueError, match="8192 UTF-8 bytes"):
        asyncio.run(
            service.start(
                StartRunCommand(
                    agent_id=PROTOTYPE_AGENT_ID,
                    message="界" * 3_000,
                )
            )
        )

    assert service.runs == {}
    assert journal.events == []


def test_event_count_reserves_final_slot_for_terminal(tmp_path: Path) -> None:
    service, journal = _service(tmp_path)
    record = _record()
    record.events = [_placeholder_event()] * (MAX_LIVE_EVENTS - 1)

    async def exercise() -> EventEnvelope:
        with pytest.raises(RuntimeError, match="live event capacity exhausted"):
            await service._publish(
                record, "assistant.block.started", "durable", {"block_id": "late"}
            )
        return await service._publish(
            record, "run.failed", "durable", {"code": "capacity"}
        )

    terminal = asyncio.run(exercise())

    assert terminal.seq == MAX_LIVE_EVENTS
    assert terminal.kind == "run.failed"
    assert record.terminal_kind == "run.failed"
    assert journal.events == [terminal]


@pytest.mark.parametrize(
    ("counter_name", "limit", "message"),
    [
        (
            "live_event_bytes",
            MAX_LIVE_EVENT_BYTES,
            "live event byte capacity exhausted",
        ),
        (
            "durable_event_bytes",
            MAX_DURABLE_BYTES_PER_RUN,
            "durable event byte capacity exhausted",
        ),
    ],
)
def test_byte_budget_reserves_space_for_terminal(
    tmp_path: Path, counter_name: str, limit: int, message: str
) -> None:
    service, journal = _service(tmp_path)
    record = _record()
    setattr(record, counter_name, limit - TERMINAL_EVENT_RESERVE)

    async def exercise() -> EventEnvelope:
        with pytest.raises(RuntimeError, match=message):
            await service._publish(
                record, "assistant.block.started", "durable", {"block_id": "late"}
            )
        return await service._publish(
            record, "run.failed", "durable", {"code": "byte_capacity"}
        )

    terminal = asyncio.run(exercise())

    assert terminal.kind == "run.failed"
    assert record.terminal_kind == "run.failed"
    assert journal.events == [terminal]
    assert getattr(record, counter_name) <= limit


def test_process_marker_tolerates_spaces_and_closing_parenthesis() -> None:
    fields = ["S", *("0" for _index in range(18)), "987654"]
    raw = f"123 (Worker name ) with spaces) {' '.join(fields)}\n"

    assert _marker_from_proc_stat(raw) == "linux:987654"


def test_run_tree_quota_counts_files_and_rejects_unsafe_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run"
    nested = root / "work"
    nested.mkdir(parents=True)
    (nested / "one.txt").write_text("1234", encoding="utf-8")

    entries, logical, allocated = _measure_run_tree(root)

    assert entries == 2
    assert logical == 4
    assert allocated >= logical

    monkeypatch.setattr(control_module, "MAX_RUN_LOGICAL_BYTES", 3)
    with pytest.raises(RuntimeError, match="logical-byte quota"):
        _measure_run_tree(root)

    monkeypatch.setattr(control_module, "MAX_RUN_LOGICAL_BYTES", 1024)
    (nested / "unsafe-link").symlink_to(nested / "one.txt")
    with pytest.raises(RuntimeError, match="unsafe entry"):
        _measure_run_tree(root)


def test_worker_pid_record_is_atomic_private_and_complete(tmp_path: Path) -> None:
    path = tmp_path / "worker.pid"
    values: dict[str, str | int] = {
        "schema": 1,
        "role": "worker",
        "pid": 123,
        "pgid": 123,
        "marker": "linux:456",
        "root": str(tmp_path),
        "agent_id": PROTOTYPE_AGENT_ID,
        "run": "1" * 32,
        "run_root": str(tmp_path / "run"),
        "module": "agent_builder_v2.worker",
        "interpreter": str(tmp_path / "worker-env" / "bin" / "python"),
        "cwd": str(tmp_path / "run" / "work"),
        "command": f"{tmp_path}/worker-env/bin/python -m agent_builder_v2.worker",
    }

    _atomic_worker_pid_record(path, values)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    parsed = dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    assert parsed == {key: str(value) for key, value in values.items()}
    assert list(tmp_path.glob(".worker.pid.*.tmp")) == []


def test_worker_wall_deadline_kills_reaps_and_publishes_one_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, journal = _service(tmp_path)
    fake_interpreter = tmp_path / "hanging-worker"
    fake_interpreter.write_text(
        "#!/bin/sh\ntrap '' TERM INT\nprintf '%s\\n' '{\"internal\":\"sandbox.ready\"}'\nIFS= read -r _command\n/bin/sleep 60\n",
        encoding="utf-8",
    )
    fake_interpreter.chmod(0o700)
    assert service.capsule is not None
    service.capsule = AgentCapsule(
        agent_id=PROTOTYPE_AGENT_ID,
        data_root=tmp_path / "data",
        runtime_root=tmp_path / "runtime",
        interpreter=fake_interpreter,
    )
    _install_fake_capsule_io(service, monkeypatch)
    captured: dict[str, object] = {}
    original_write = control_module._write_worker_pid_record

    def capture_record(**kwargs: object) -> None:
        original_write(**kwargs)  # type: ignore[arg-type]
        record_path = kwargs["path"]
        assert isinstance(record_path, Path)
        captured["pid"] = kwargs["pid"]
        captured["text"] = record_path.read_text(encoding="utf-8")
        captured["mode"] = stat.S_IMODE(record_path.stat().st_mode)

    monkeypatch.setattr(control_module, "_write_worker_pid_record", capture_record)

    async def exercise() -> RunRecord:
        record = _record()
        service.runs[record.run_id] = record
        await service._publish(
            record,
            "run.started",
            "durable",
            {"prototype": True},
        )
        record.deadline_at = asyncio.get_running_loop().time() + 0.2
        await asyncio.wait_for(service._run_worker(record, "hang"), timeout=2.0)
        return record

    started = time.monotonic()
    record = asyncio.run(exercise())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert captured["mode"] == 0o600
    assert "marker=linux:" in str(captured["text"])
    worker_pid = captured["pid"]
    assert isinstance(worker_pid, int)
    assert not Path(f"/proc/{worker_pid}").exists()
    assert record.process is None
    assert not (service.capsule.runtime_root / "runs" / record.run_id).exists()
    terminals = [event for event in record.events if event.kind in {"run.failed", "run.cancelled", "run.completed"}]
    assert [event.kind for event in terminals] == ["run.failed"]
    assert terminals[0].payload["code"] == "worker_deadline_exceeded"
    journal_terminals = [event for event in journal.events if event.kind.startswith("run.") and event.kind != "run.started"]
    assert journal_terminals == terminals


def test_worker_crash_closes_open_block_and_tool_before_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, journal = _service(tmp_path)
    fake_interpreter = tmp_path / "crashing-worker"
    fake_interpreter.write_text(
        """#!/bin/sh
printf '%s\n' '{"internal":"sandbox.ready"}'
IFS= read -r _command
printf '%s\n' '{"kind":"assistant.block.started","durability":"durable","payload":{"block_id":"open-block","block_type":"content"}}'
printf '%s\n' '{"kind":"tool.call.requested","durability":"durable","payload":{"call_id":"open-call","tool_id":"builtin/echo","arguments":{"text":"hello"}}}'
printf '%s\n' '{"kind":"tool.call.started","durability":"durable","payload":{"call_id":"open-call","tool_id":"builtin/echo"}}'
exit 7
""",
        encoding="utf-8",
    )
    fake_interpreter.chmod(0o700)
    service.capsule = AgentCapsule(
        agent_id=PROTOTYPE_AGENT_ID,
        data_root=tmp_path / "data",
        runtime_root=tmp_path / "runtime",
        interpreter=fake_interpreter,
    )
    _install_fake_capsule_io(service, monkeypatch)

    async def exercise() -> RunRecord:
        record = _record()
        service.runs[record.run_id] = record
        await service._publish(record, "run.started", "durable", {"prototype": True})
        await service._run_worker(record, "crash")
        return record

    record = asyncio.run(exercise())
    kinds = [event.kind for event in record.events]

    assert kinds == [
        "run.started",
        "assistant.block.started",
        "tool.call.requested",
        "tool.call.started",
        "assistant.block.discarded",
        "tool.call.finished",
        "run.failed",
    ]
    assert record.open_blocks == set()
    assert record.pending_tools == {}
    assert record.events[-2].payload["outcome"] == "failed"
    assert record.events[-1].payload["code"] == "worker_crash"
    assert [event.kind for event in journal.events] == kinds
    assert record.process is None
    assert not (service.capsule.runtime_root / "runs" / record.run_id).exists()


def test_invalid_worker_terminal_is_replaced_by_one_control_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, journal = _service(tmp_path)
    fake_interpreter = tmp_path / "invalid-terminal-worker"
    fake_interpreter.write_text(
        """#!/bin/sh
printf '%s\n' '{"internal":"sandbox.ready"}'
IFS= read -r _command
printf '%s\n' '{"kind":"run.failed","durability":"durable","payload":{"code":"bad","message":"unexpected extra terminal field","retryable":false,"extra":"not allowed"}}'
exit 0
""",
        encoding="utf-8",
    )
    fake_interpreter.chmod(0o700)
    service.capsule = AgentCapsule(
        agent_id=PROTOTYPE_AGENT_ID,
        data_root=tmp_path / "data",
        runtime_root=tmp_path / "runtime",
        interpreter=fake_interpreter,
    )
    _install_fake_capsule_io(service, monkeypatch)

    async def exercise() -> RunRecord:
        record = _record()
        service.runs[record.run_id] = record
        await service._publish(record, "run.started", "durable", {"prototype": True})
        await service._run_worker(record, "invalid terminal")
        return record

    record = asyncio.run(exercise())
    terminals = [event for event in record.events if event.kind in TERMINAL_KINDS]

    assert len(terminals) == 1
    assert terminals[0].kind == "run.failed"
    assert terminals[0].payload["code"] == "invalid_worker_event"
    assert journal.events[-1] == terminals[0]


def test_journal_failure_converges_stream_with_honest_ephemeral_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _journal = _service(tmp_path)
    failing_journal = _FailingJournal(successful_appends=2)
    service.journal = failing_journal  # type: ignore[assignment]
    fake_interpreter = tmp_path / "journal-failure-worker"
    fake_interpreter.write_text(
        """#!/bin/sh
printf '%s\n' '{"internal":"sandbox.ready"}'
IFS= read -r _command
printf '%s\n' '{"kind":"assistant.block.started","durability":"durable","payload":{"block_id":"open-block","block_type":"content"}}'
printf '%s\n' '{"kind":"tool.call.requested","durability":"durable","payload":{"call_id":"not-published","tool_id":"builtin/echo","arguments":{"text":"hello"}}}'
/bin/sleep 1
""",
        encoding="utf-8",
    )
    fake_interpreter.chmod(0o700)
    service.capsule = AgentCapsule(
        agent_id=PROTOTYPE_AGENT_ID,
        data_root=tmp_path / "data",
        runtime_root=tmp_path / "runtime",
        interpreter=fake_interpreter,
    )
    _install_fake_capsule_io(service, monkeypatch)

    async def exercise() -> tuple[RunRecord, bool]:
        record = _record()
        service.runs[record.run_id] = record
        await service._publish(record, "run.started", "durable", {"prototype": True})
        await service._run_worker(record, "journal failure")
        _events, done = await record.events_after(0, timeout=0.01)
        return record, done

    record, done = asyncio.run(exercise())

    assert done is True
    assert record.journal_failed is True
    assert [event.kind for event in record.events] == [
        "run.started",
        "assistant.block.started",
        "assistant.block.discarded",
        "run.failed",
    ]
    assert record.events[-2].durability == "ephemeral"
    assert record.events[-1].durability == "ephemeral"
    assert record.events[-1].payload["code"] == "journal_unavailable"
    assert record.open_blocks == set()
    assert record.terminal_kind == "run.failed"
    assert [event.kind for event in failing_journal.events] == [
        "run.started",
        "assistant.block.started",
    ]

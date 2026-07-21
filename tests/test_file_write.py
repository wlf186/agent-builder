"""Security and atomicity matrix for receipt-bound workspace mutations."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent_builder_v2.file_write as file_write
from agent_builder_v2.capsule import AgentCapsule, PROTOTYPE_AGENT_ID
from agent_builder_v2.file_read import capture_workspace_file, file_receipt
from agent_builder_v2.file_write import (
    FileMutationExecutor,
    FileWriteError,
    FileWriteOutcomeUnknownError,
    FullReadReceipt,
)
from agent_builder_v2.permissions import CapabilityRequest
from agent_builder_v2.tools import runtime_effective_toolset


def _capsule(tmp_path: Path) -> AgentCapsule:
    data_root = tmp_path / "data" / "agents" / PROTOTYPE_AGENT_ID
    workspace = data_root / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    os.chmod(data_root, 0o700)
    os.chmod(workspace, 0o700)
    return AgentCapsule(
        PROTOTYPE_AGENT_ID,
        data_root,
        tmp_path / ".runtime" / "agents" / PROTOTYPE_AGENT_ID,
        Path("/usr/bin/python3"),
        generation=3,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o600)


def _receipt(capsule: AgentCapsule, path: str) -> FullReadReceipt:
    captured = capture_workspace_file(capsule, path)
    value = {
        "kind": "file_read_text",
        "receipt": file_receipt(captured),
        "range": {
            "start_byte": 0,
            "returned_bytes": captured.metadata.st_size,
        },
        "truncated": False,
    }
    return FullReadReceipt.from_result(value)


def _request(
    capsule: AgentCapsule,
    tool_id: str,
    prepared: object,
    preview: str,
) -> CapabilityRequest:
    return CapabilityRequest.create(
        agent_id=capsule.agent_id,
        capsule_generation=capsule.generation,
        conversation_id="1" * 32,
        run_id="2" * 32,
        call_id="write-call",
        capability_id=tool_id,
        toolset_digest=runtime_effective_toolset().toolset_digest,
        policy_digest="3" * 64,
        arguments=prepared,
        preview=preview,
        expires_at_milliseconds=31_000,
        now_milliseconds=1_000,
    )


def test_edit_requires_exact_full_receipt_and_commits_one_match(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "notes.txt"
    _write(target, "alpha\nbeta\ngamma\n")
    receipt = _receipt(capsule, "notes.txt")
    executor = FileMutationExecutor(capsule)
    prepared, preview = executor.prepare(
        "file/edit",
        {
            "path": "notes.txt",
            "old_text": "beta",
            "new_text": "BETA",
            "path_identity": receipt.path_identity,
            "content_digest": receipt.content_digest,
        },
        {"notes.txt": receipt},
    )

    assert "-beta" in preview and "+BETA" in preview
    result = json.loads(
        executor.execute(
            _request(capsule, "file/edit", prepared, preview), lambda: False
        )
    )
    assert target.read_text() == "alpha\nBETA\ngamma\n"
    assert result["outcome"] == "committed"
    assert result["receipt"]["content_digest"] == prepared["new_content_digest"]
    assert not list(target.parent.glob(".agent-builder-write-*.tmp"))


def test_replace_and_edit_fail_on_missing_stale_or_ambiguous_receipts(
    tmp_path: Path,
) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "notes.txt"
    _write(target, "same same")
    receipt = _receipt(capsule, "notes.txt")
    executor = FileMutationExecutor(capsule)
    common = {
        "path": "notes.txt",
        "path_identity": receipt.path_identity,
        "content_digest": receipt.content_digest,
    }
    with pytest.raises(FileWriteError, match="complete read"):
        executor.prepare(
            "file/write",
            {**common, "content": "new", "create": False},
            {},
        )
    with pytest.raises(FileWriteError, match="exactly once"):
        executor.prepare(
            "file/edit",
            {**common, "old_text": "same", "new_text": "new"},
            {"notes.txt": receipt},
        )
    _write(target, "changed elsewhere")
    with pytest.raises(FileWriteError, match="stale"):
        executor.prepare(
            "file/write",
            {**common, "content": "new", "create": False},
            {"notes.txt": receipt},
        )


def test_create_uses_parent_and_absence_receipt_without_clobber(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    (workspace / "sub").mkdir(mode=0o700)
    executor = FileMutationExecutor(capsule)
    prepared, preview = executor.prepare(
        "file/write",
        {"path": "sub/new.txt", "content": "created\n", "create": True},
        {},
    )
    target = workspace / "sub" / "new.txt"
    _write(target, "racer")
    with pytest.raises(FileWriteError, match="stale|changed"):
        executor.execute(
            _request(capsule, "file/write", prepared, preview), lambda: False
        )
    assert target.read_text() == "racer"

    target.unlink()
    prepared, preview = executor.prepare(
        "file/write",
        {"path": "sub/new.txt", "content": "created\n", "create": True},
        {},
    )
    result = json.loads(
        executor.execute(
            _request(capsule, "file/write", prepared, preview), lambda: False
        )
    )
    assert target.read_text() == "created\n"
    assert result["receipt"]["path"] == "sub/new.txt"


def test_cancel_and_symlink_parent_fail_before_commit(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    executor = FileMutationExecutor(capsule)
    prepared, preview = executor.prepare(
        "file/write",
        {"path": "cancelled.txt", "content": "new", "create": True},
        {},
    )
    with pytest.raises(FileWriteError, match="cancelled"):
        executor.execute(
            _request(capsule, "file/write", prepared, preview), lambda: True
        )
    assert not (workspace / "cancelled.txt").exists()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (workspace / "link").symlink_to(foreign, target_is_directory=True)
    with pytest.raises((FileWriteError, OSError)):
        executor.prepare(
            "file/write",
            {"path": "link/new.txt", "content": "escape", "create": True},
            {},
        )
    assert not (foreign / "new.txt").exists()


def test_exchange_detects_last_moment_content_race_and_restores_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "race.txt"
    _write(target, "old")
    receipt = _receipt(capsule, "race.txt")
    executor = FileMutationExecutor(capsule)
    prepared, preview = executor.prepare(
        "file/write",
        {
            "path": "race.txt",
            "content": "approved",
            "create": False,
            "path_identity": receipt.path_identity,
            "content_digest": receipt.content_digest,
        },
        {"race.txt": receipt},
    )
    original = file_write._renameat2
    calls = 0

    def race(
        source_fd: int, source: str, target_fd: int, name: str, flags: int
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("racer")
        original(source_fd, source, target_fd, name, flags)

    monkeypatch.setattr(file_write, "_renameat2", race)
    with pytest.raises(FileWriteError, match="raced"):
        executor.execute(
            _request(capsule, "file/write", prepared, preview), lambda: False
        )
    assert target.read_text() == "racer"
    assert calls == 2


def test_unprovable_exchange_rollback_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "unknown.txt"
    _write(target, "old")
    receipt = _receipt(capsule, "unknown.txt")
    executor = FileMutationExecutor(capsule)
    prepared, preview = executor.prepare(
        "file/write",
        {
            "path": "unknown.txt",
            "content": "approved",
            "create": False,
            "path_identity": receipt.path_identity,
            "content_digest": receipt.content_digest,
        },
        {"unknown.txt": receipt},
    )
    original = file_write._renameat2
    calls = 0

    def uncertain(
        source_fd: int, source: str, target_fd: int, name: str, flags: int
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("racer")
            original(source_fd, source, target_fd, name, flags)
            return
        raise OSError(5, "simulated rollback failure")

    monkeypatch.setattr(file_write, "_renameat2", uncertain)
    with pytest.raises(FileWriteOutcomeUnknownError):
        executor.execute(
            _request(capsule, "file/write", prepared, preview), lambda: False
        )
    assert target.read_text() == "approved"


def test_create_has_two_sync_points_and_precommit_sync_failure_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    executor = FileMutationExecutor(capsule)
    prepared, preview = executor.prepare(
        "file/write",
        {"path": "sync.txt", "content": "content", "create": True},
        {},
    )
    original = os.fsync
    calls: list[int] = []

    def counted(descriptor: int) -> None:
        calls.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", counted)
    executor.execute(
        _request(capsule, "file/write", prepared, preview), lambda: False
    )
    assert len(calls) == 2
    assert (workspace / "sync.txt").read_text() == "content"

    prepared, preview = executor.prepare(
        "file/write",
        {"path": "fail-sync.txt", "content": "content", "create": True},
        {},
    )
    calls.clear()

    def failed_sync(descriptor: int) -> None:
        calls.append(descriptor)
        raise OSError(5, "simulated fsync failure")

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(FileWriteError, match="atomic file mutation"):
        executor.execute(
            _request(capsule, "file/write", prepared, preview), lambda: False
        )
    assert len(calls) == 1
    assert not (workspace / "fail-sync.txt").exists()
    assert not list(workspace.glob(".agent-builder-write-*.tmp"))


def test_startup_cleanup_is_bounded_and_only_uses_reserved_temp_namespace(
    tmp_path: Path,
) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    stale = workspace / (".agent-builder-write-" + "a" * 32 + ".tmp")
    _write(stale, "stale")
    ordinary = workspace / "ordinary.tmp"
    _write(ordinary, "keep")
    FileMutationExecutor(capsule)
    assert not stale.exists()
    assert ordinary.read_text() == "keep"


def test_reserved_internal_temp_namespace_cannot_be_a_mutation_target(
    tmp_path: Path,
) -> None:
    capsule = _capsule(tmp_path)
    executor = FileMutationExecutor(capsule)

    with pytest.raises(FileWriteError, match="reserved internal namespace"):
        executor.prepare(
            "file/write",
            {
                "path": ".agent-builder-write-user.tmp",
                "content": "must not survive startup cleanup",
                "create": True,
            },
            {},
        )

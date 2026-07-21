"""Negative security matrix for descriptor-anchored workspace reads."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent_builder_v2.file_read as file_read
from agent_builder_v2.capsule import AgentCapsule, PROTOTYPE_AGENT_ID
from agent_builder_v2.file_read import FileReadError, FileReadExecutor
from agent_builder_v2.permissions import CapabilityRequest
from agent_builder_v2.tools import runtime_effective_toolset


def _capsule(tmp_path: Path, agent_id: str = PROTOTYPE_AGENT_ID) -> AgentCapsule:
    data_root = tmp_path / "data" / "agents" / agent_id
    workspace = data_root / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    os.chmod(data_root, 0o700)
    os.chmod(workspace, 0o700)
    return AgentCapsule(
        agent_id,
        data_root,
        tmp_path / ".runtime" / "agents" / agent_id,
        Path("/usr/bin/python3"),
        generation=3,
    )


def _write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    os.chmod(path, mode)


def _request(capsule: AgentCapsule, tool_id: str, arguments: object) -> CapabilityRequest:
    effective = runtime_effective_toolset()
    return CapabilityRequest.create(
        agent_id=capsule.agent_id,
        capsule_generation=capsule.generation,
        conversation_id="1" * 32,
        run_id="2" * 32,
        call_id="read-call",
        capability_id=tool_id,
        toolset_digest=effective.toolset_digest,
        policy_digest="3" * 64,
        arguments=arguments,
        preview="Read a bounded workspace file",
        expires_at_milliseconds=31_000,
        now_milliseconds=1_000,
    )


def test_stat_and_text_read_return_stable_content_bound_receipts(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "notes" / "answer.txt"
    _write(target, "第一行\nsecond line\nthird\n".encode())
    executor = FileReadExecutor(capsule)

    stat_result = json.loads(
        executor.execute(_request(capsule, "file/stat", {"path": "notes/answer.txt"}), lambda: False)
    )
    read_request = _request(
        capsule,
        "file/read_text",
        {"path": "notes/answer.txt", "line_offset": 1, "max_lines": 1},
    )
    first = executor.execute(read_request, lambda: False)
    second = executor.execute(read_request, lambda: False)
    result = json.loads(first)

    assert first == second
    assert stat_result["receipt"] == result["receipt"]
    assert result["content"] == "second line\n"
    assert result["truncated"] is True
    assert result["truncation_reason"] == "bounded_range"
    assert len(result["receipt"]["path_identity"]) == 64
    assert len(result["receipt"]["content_digest"]) == 64
    assert result["receipt"]["path"] == "notes/answer.txt"


@pytest.mark.parametrize(
    "path",
    ("", "/etc/passwd", "../foreign", "a/../b", "./file", "a//b", "a/"),
)
def test_paths_fail_closed_before_ambient_resolution(tmp_path: Path, path: str) -> None:
    capsule = _capsule(tmp_path)
    executor = FileReadExecutor(capsule)
    with pytest.raises(FileReadError):
        executor.execute(_request(capsule, "file/stat", {"path": path}), lambda: False)


def test_links_special_binary_sparse_and_unsafe_modes_are_rejected(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    executor = FileReadExecutor(capsule)
    foreign = tmp_path / "foreign.txt"
    _write(foreign, b"secret")
    (workspace / "link").symlink_to(foreign)
    _write(workspace / "hard-source", b"hard")
    os.link(workspace / "hard-source", workspace / "hard-link")
    os.mkfifo(workspace / "fifo", 0o600)
    _write(workspace / "binary", b"text\x00secret")
    _write(workspace / "unsafe", b"open", 0o666)
    sparse = workspace / "sparse"
    with sparse.open("wb") as stream:
        stream.seek(1024 * 1024 - 1)
        stream.write(b"x")
    os.chmod(sparse, 0o600)

    for name in ("link", "hard-source", "hard-link", "fifo", "binary", "unsafe", "sparse"):
        with pytest.raises(FileReadError):
            executor.execute(
                _request(capsule, "file/read_text", {"path": name}),
                lambda: False,
            )


def test_cross_agent_symlink_and_unsafe_parent_are_rejected(tmp_path: Path) -> None:
    first = _capsule(tmp_path)
    second = _capsule(tmp_path, "00000000-0000-4000-8000-000000000002")
    _write(second.data_root / "workspace" / "secret", b"second")
    (first.data_root / "workspace" / "foreign").symlink_to(
        second.data_root / "workspace", target_is_directory=True
    )
    unsafe = first.data_root / "workspace" / "unsafe"
    unsafe.mkdir(mode=0o700)
    _write(unsafe / "file", b"data")
    os.chmod(unsafe, 0o777)
    executor = FileReadExecutor(first)

    for path in ("foreign/secret", "unsafe/file"):
        with pytest.raises(FileReadError):
            executor.execute(_request(first, "file/stat", {"path": path}), lambda: False)


def test_utf8_offset_limits_cancellation_and_growth_race_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "utf8.txt"
    _write(target, "界abc".encode())
    executor = FileReadExecutor(capsule)
    with pytest.raises(FileReadError, match="UTF-8"):
        executor.execute(
            _request(capsule, "file/read_text", {"path": "utf8.txt", "offset_bytes": 1}),
            lambda: False,
        )
    with pytest.raises(FileReadError, match="cancelled"):
        executor.execute(_request(capsule, "file/stat", {"path": "utf8.txt"}), lambda: True)

    real_read = file_read.os.read
    changed = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        result = real_read(descriptor, size)
        if result and not changed:
            changed = True
            target.write_bytes(b"changed")
        return result

    monkeypatch.setattr(file_read.os, "read", mutate_after_read)
    with pytest.raises(FileReadError, match="changed"):
        executor.execute(_request(capsule, "file/stat", {"path": "utf8.txt"}), lambda: False)


def test_read_has_no_content_sidecar_or_temporary_index(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    target = capsule.data_root / "workspace" / "plain.txt"
    _write(target, b"plain text")
    before = sorted(str(path.relative_to(capsule.data_root)) for path in capsule.data_root.rglob("*"))
    result = FileReadExecutor(capsule).execute(
        _request(capsule, "file/read_text", {"path": "plain.txt"}),
        lambda: False,
    )
    after = sorted(str(path.relative_to(capsule.data_root)) for path in capsule.data_root.rglob("*"))

    assert json.loads(result)["content"] == "plain text"
    assert after == before

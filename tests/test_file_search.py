"""Bounded Glob/Grep behavior and adversarial workspace traversal tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket

import pytest

import agent_builder_v2.file_search as file_search
from agent_builder_v2.capsule import AgentCapsule, PROTOTYPE_AGENT_ID
from agent_builder_v2.file_search import FileSearchError, FileSearchExecutor
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
        generation=4,
    )


def _write(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, mode)


def _request(capsule: AgentCapsule, tool_id: str, arguments: object) -> CapabilityRequest:
    return CapabilityRequest.create(
        agent_id=capsule.agent_id,
        capsule_generation=capsule.generation,
        conversation_id="1" * 32,
        run_id="2" * 32,
        call_id="search-call",
        capability_id=tool_id,
        toolset_digest=runtime_effective_toolset().toolset_digest,
        policy_digest="3" * 64,
        arguments=arguments,
        preview="Search a bounded workspace",
        expires_at_milliseconds=31_000,
        now_milliseconds=1_000,
    )


def test_glob_is_stable_receipt_bound_and_protocol_safe(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    _write(workspace / "zeta.txt", "last")
    _write(workspace / "nested" / "alpha.txt", "first")
    _write(workspace / "nested" / "line\nbreak.txt", "escaped")
    _write(workspace / "nested" / "ignored.md", "ignored")
    _write(workspace / ".git" / "config", "must not be traversed")
    executor = FileSearchExecutor(capsule)
    request = _request(capsule, "file/glob", {"pattern": "**/*.txt"})

    first = executor.execute(request, lambda: False)
    second = executor.execute(request, lambda: False)
    decoded = json.loads(first)

    assert first == second
    assert [item["receipt"]["path"] for item in decoded["matches"]] == [
        "nested/alpha.txt",
        "nested/line\nbreak.txt",
        "zeta.txt",
    ]
    assert "line\\nbreak.txt" in first
    assert decoded["truncated"] is False
    assert decoded["provenance"].endswith(":workspace")
    assert all(len(item["receipt"]["content_digest"]) == 64 for item in decoded["matches"])


def test_grep_literal_safe_regex_case_and_bounded_excerpt(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    _write(workspace / "a.txt", "Alpha needle\nnumber 42\n")
    _write(workspace / "b.txt", "alpha NEEDLE\nnumber 7\n")
    executor = FileSearchExecutor(capsule)

    literal = json.loads(
        executor.execute(
            _request(
                capsule,
                "file/grep",
                {
                    "pattern": "**/*.txt",
                    "query": "needle",
                    "case_sensitive": False,
                },
            ),
            lambda: False,
        )
    )
    regex = json.loads(
        executor.execute(
            _request(
                capsule,
                "file/grep",
                {"pattern": "**/*.txt", "query": "^number [0-9][0-9]?$", "regex": True},
            ),
            lambda: False,
        )
    )

    assert [(item["path"], item["line"]) for item in literal["matches"]] == [
        ("a.txt", 1),
        ("b.txt", 1),
    ]
    assert [(item["path"], item["excerpt"]) for item in regex["matches"]] == [
        ("a.txt", "number 42"),
        ("b.txt", "number 7"),
    ]
    assert all(item["column"] >= 1 for item in regex["matches"])


@pytest.mark.parametrize(
    "expression",
    (
        "(a+)+$",
        "a|b",
        "a{1,100}",
        r"(a)\1",
        "a*a*a*",
        "?" * 9,
    ),
)
def test_pathological_regex_is_rejected_before_traversal(
    tmp_path: Path, expression: str
) -> None:
    capsule = _capsule(tmp_path)
    _write(capsule.data_root / "workspace" / "file.txt", "content")
    with pytest.raises(FileSearchError, match="safe regex subset"):
        FileSearchExecutor(capsule).execute(
            _request(
                capsule,
                "file/grep",
                {"pattern": "**/*.txt", "query": expression, "regex": True},
            ),
            lambda: False,
        )


@pytest.mark.parametrize(
    "pattern",
    ("", "/tmp/*", "../*", "a//*.txt", "a/**x", "*" * 33),
)
def test_pathological_glob_is_rejected_before_traversal(
    tmp_path: Path, pattern: str
) -> None:
    capsule = _capsule(tmp_path)
    with pytest.raises(FileSearchError, match="glob pattern"):
        FileSearchExecutor(capsule).execute(
            _request(capsule, "file/glob", {"pattern": pattern}),
            lambda: False,
        )


def test_symlink_special_hardlink_and_cross_agent_paths_fail_closed(tmp_path: Path) -> None:
    first = _capsule(tmp_path)
    second = _capsule(tmp_path, "00000000-0000-4000-8000-000000000002")
    workspace = first.data_root / "workspace"
    _write(second.data_root / "workspace" / "secret.txt", "second")
    (workspace / "foreign").symlink_to(second.data_root / "workspace", target_is_directory=True)
    executor = FileSearchExecutor(first)
    request = _request(first, "file/glob", {"pattern": "**/*.txt"})
    with pytest.raises(FileSearchError, match="special"):
        executor.execute(request, lambda: False)

    (workspace / "foreign").unlink()
    _write(workspace / "hard.txt", "hard")
    os.link(workspace / "hard.txt", workspace / "hard-2.txt")
    with pytest.raises(FileSearchError, match="unsafe workspace file"):
        executor.execute(request, lambda: False)

    (workspace / "hard-2.txt").unlink()
    (workspace / "hard.txt").unlink()
    os.mkfifo(workspace / "pipe", 0o600)
    with pytest.raises(FileSearchError, match="special"):
        executor.execute(request, lambda: False)
    (workspace / "pipe").unlink()
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous_directory = Path.cwd()
    try:
        os.chdir(workspace)
        unix_socket.bind("socket")
        with pytest.raises(FileSearchError, match="special"):
            executor.execute(request, lambda: False)
    finally:
        os.chdir(previous_directory)
        unix_socket.close()


def test_depth_entry_byte_match_and_result_limits_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    for index in range(5):
        _write(workspace / f"{index}.txt", f"needle {index}\n")
    executor = FileSearchExecutor(capsule)

    limited = json.loads(
        executor.execute(
            _request(
                capsule,
                "file/grep",
                {"pattern": "*.txt", "query": "needle", "max_results": 2},
            ),
            lambda: False,
        )
    )
    assert [item["path"] for item in limited["matches"]] == ["0.txt", "1.txt"]
    assert limited["truncation_reason"] == "match_limit"

    monkeypatch.setattr(file_search, "MAX_SEARCH_ENTRIES", 3)
    entry_limited = json.loads(
        executor.execute(
            _request(capsule, "file/glob", {"pattern": "*.txt"}),
            lambda: False,
        )
    )
    assert entry_limited["truncated"] is True
    assert entry_limited["truncation_reason"] == "entry_limit"

    monkeypatch.setattr(file_search, "MAX_SEARCH_ENTRIES", 4_096)
    monkeypatch.setattr(file_search, "MAX_SEARCH_BYTES", 1)
    byte_limited = json.loads(
        executor.execute(
            _request(capsule, "file/glob", {"pattern": "*.txt"}),
            lambda: False,
        )
    )
    assert byte_limited["truncation_reason"] == "byte_limit"

    nested = workspace / "nested" / "deeper"
    _write(nested / "value.txt", "value")
    monkeypatch.setattr(file_search, "MAX_SEARCH_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(file_search, "MAX_SEARCH_DEPTH", 1)
    depth_limited = json.loads(
        executor.execute(
            _request(capsule, "file/glob", {"pattern": "**/*.txt"}),
            lambda: False,
        )
    )
    assert depth_limited["truncation_reason"] == "depth_limit"

    monkeypatch.setattr(file_search, "MAX_SEARCH_DEPTH", 16)
    monkeypatch.setattr(file_search, "MAX_SEARCH_RESULT_BYTES", 600)
    result_limited = json.loads(
        executor.execute(
            _request(capsule, "file/glob", {"pattern": "*.txt"}),
            lambda: False,
        )
    )
    assert result_limited["truncation_reason"] == "result_bytes_limit"


def test_cancel_delete_race_and_no_index_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capsule(tmp_path)
    workspace = capsule.data_root / "workspace"
    _write(workspace / "a.txt", "needle")
    executor = FileSearchExecutor(capsule)
    request = _request(
        capsule, "file/grep", {"pattern": "*.txt", "query": "needle"}
    )
    before = sorted(str(path.relative_to(capsule.data_root)) for path in capsule.data_root.rglob("*"))
    result = executor.execute(request, lambda: False)
    after = sorted(str(path.relative_to(capsule.data_root)) for path in capsule.data_root.rglob("*"))
    assert json.loads(result)["matches"]
    assert after == before

    with pytest.raises(FileSearchError, match="cancelled"):
        executor.execute(request, lambda: True)

    cancel_checks = 0

    def cancel_during_walk() -> bool:
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks > 3

    with pytest.raises(FileSearchError, match="cancelled"):
        executor.execute(request, cancel_during_walk)

    real_capture = file_search.capture_workspace_file
    deleted = False

    def delete_then_capture(value: AgentCapsule, path: object) -> object:
        nonlocal deleted
        if not deleted:
            deleted = True
            shutil.rmtree(value.data_root)
        return real_capture(value, path)

    monkeypatch.setattr(file_search, "capture_workspace_file", delete_then_capture)
    with pytest.raises((FileSearchError, OSError)):
        executor.execute(request, lambda: False)

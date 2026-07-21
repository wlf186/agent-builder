"""Agent-scoped research environment lifecycle and sandbox tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

from agent_builder_v2.capsule import CapsuleManager
from agent_builder_v2.command_exec import CommandExecutor
from agent_builder_v2.permissions import CapabilityRequest
from agent_builder_v2.research import (
    RESEARCH_REQUIREMENTS,
    ResearchDocumentExecutor,
    ResearchEnvironmentError,
    ResearchEnvironmentManager,
)
from agent_builder_v2.tools import runtime_effective_toolset


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _fake_installer(interpreter: Path, calls: list[Path]):
    def install(target: Path) -> tuple[str, ...]:
        calls.append(target)
        subprocess.run(
            [
                os.fspath(interpreter),
                "-m",
                "venv",
                "--without-pip",
                "--copies",
                os.fspath(target),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        os.chmod(target, 0o700, follow_symlinks=False)
        return RESEARCH_REQUIREMENTS

    return install


def test_research_environment_reuses_per_agent_and_deletes_without_cross_talk(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    capsules = CapsuleManager(repository)
    first = capsules.ensure_agent(
        "11111111-1111-4111-8111-111111111111",
        display_name="Research one",
        generation=1,
    )
    second = capsules.ensure_agent(
        "22222222-2222-4222-8222-222222222222",
        display_name="Research two",
        generation=1,
    )
    first_calls: list[Path] = []
    second_calls: list[Path] = []
    first_manager = ResearchEnvironmentManager(
        repository,
        first,
        installer=_fake_installer(first.interpreter, first_calls),
    )
    second_manager = ResearchEnvironmentManager(
        repository,
        second,
        installer=_fake_installer(second.interpreter, second_calls),
    )

    first_record = first_manager.install()
    assert first_manager.install() == first_record
    assert len(first_calls) == 1
    assert second_manager.status() is None

    second_manager.install()
    assert len(second_calls) == 1
    assert first_manager.runtime_root != second_manager.runtime_root
    assert first_manager.runtime_root.is_dir()
    assert second_manager.runtime_root.is_dir()

    first_manager.delete()
    assert first_manager.status() is None
    assert second_manager.status() is not None
    assert second_manager.runtime_root.is_dir()
    assert not list(first.data_root.parent.glob(".research-staging-*"))
    assert not list(first.runtime_root.parent.glob(".research-staging-*"))


def test_research_environment_partial_or_redirected_state_fails_closed(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    capsule = CapsuleManager(repository).ensure_agent(
        "33333333-3333-4333-8333-333333333333",
        display_name="Research",
        generation=1,
    )
    manager = ResearchEnvironmentManager(repository, capsule, installer=lambda _: ())
    manager.data_root.mkdir(mode=0o700)
    with pytest.raises(ResearchEnvironmentError, match="partially installed"):
        manager.status()
    recovered = ResearchEnvironmentManager(repository, capsule, installer=lambda _: ())
    assert recovered.status() is None
    assert not manager.data_root.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    manager.data_root.symlink_to(outside, target_is_directory=True)
    manager.runtime_root.mkdir(mode=0o700)
    with pytest.raises(ResearchEnvironmentError):
        manager.status()
    assert list(outside.iterdir()) == []


def test_text_document_runs_in_agent_environment_and_staging_is_removed(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    capsule_manager = CapsuleManager(repository)
    capsule = capsule_manager.ensure_agent(
        "44444444-4444-4444-8444-444444444444",
        display_name="Research",
        generation=1,
    )
    calls: list[Path] = []
    manager = ResearchEnvironmentManager(
        repository,
        capsule,
        installer=_fake_installer(capsule.interpreter, calls),
    )
    manager.install()
    document = capsule.data_root / "workspace" / "notes.md"
    document.write_text("alpha beta gamma delta", encoding="utf-8")
    run_id = uuid.uuid4().hex
    run_root = capsule_manager.create_run_root(capsule, run_id)
    commands = CommandExecutor(repository, REPOSITORY_ROOT / "src", capsule)
    research = ResearchDocumentExecutor(manager, commands)
    prepared, preview, executor = research.prepare(
        {"path": "notes.md", "offset_chars": 6, "max_chars": 10}, run_root
    )
    now = int(time.time() * 1000)
    request = CapabilityRequest.create(
        agent_id=capsule.agent_id,
        capsule_generation=capsule.generation,
        conversation_id="1" * 32,
        run_id=run_id,
        call_id="document-call",
        capability_id="document/extract_text",
        toolset_digest=runtime_effective_toolset().toolset_digest,
        policy_digest="2" * 64,
        arguments=prepared,
        preview=preview,
        expires_at_milliseconds=now + 30_000,
        now_milliseconds=now,
    )
    try:
        result = json.loads(executor.execute(request, lambda: False))
        assert result["kind"] == "document_text"
        assert result["path"] == "notes.md"
        assert result["content"] == "beta gamma"
        assert result["parser"] == "utf-8"
        assert not list((run_root / "work").glob("research-input-*.bin"))
        assert manager.status() is not None
    finally:
        capsule_manager.remove_run_root(capsule, run_id)


def test_document_capture_rejects_workspace_symlink(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    capsule = CapsuleManager(repository).ensure_agent(
        "55555555-5555-4555-8555-555555555555",
        display_name="Research",
        generation=1,
    )
    calls: list[Path] = []
    manager = ResearchEnvironmentManager(
        repository,
        capsule,
        installer=_fake_installer(capsule.interpreter, calls),
    )
    manager.install()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-secret")
    (capsule.data_root / "workspace" / "secret.pdf").symlink_to(outside)
    commands = CommandExecutor(repository, REPOSITORY_ROOT / "src", capsule)
    executor = ResearchDocumentExecutor(manager, commands)
    run_id = uuid.uuid4().hex
    run_root = CapsuleManager(repository).create_run_root(capsule, run_id)
    try:
        with pytest.raises(ResearchEnvironmentError, match="safely"):
            executor.prepare({"path": "secret.pdf"}, run_root)
    finally:
        CapsuleManager(repository).remove_run_root(capsule, run_id)


def test_agent_capsule_delete_removes_persistent_research_environment(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    capsule_manager = CapsuleManager(repository)
    capsule = capsule_manager.ensure_agent(
        "66666666-6666-4666-8666-666666666666",
        display_name="Research",
        generation=1,
    )
    manager = ResearchEnvironmentManager(
        repository,
        capsule,
        installer=_fake_installer(capsule.interpreter, []),
    )
    manager.install()
    data_root = capsule.data_root
    runtime_root = capsule.runtime_root

    shutil.rmtree(data_root)
    shutil.rmtree(runtime_root)

    assert not data_root.exists()
    assert not runtime_root.exists()

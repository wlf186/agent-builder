"""Release source archive reproducibility and containment checks."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release_artifact.py"
SPEC = importlib.util.spec_from_file_location("agent_builder_release_artifact", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_artifact = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_artifact
SPEC.loader.exec_module(release_artifact)


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "VERSION").write_text("0.2.0\n", encoding="ascii")
    (root / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    (root / "AGENTS.md").symlink_to("CLAUDE.md")
    (root / ".gitignore").write_text(".runtime/\n", encoding="ascii")
    (root / "data").mkdir()
    (root / "data" / ".gitkeep").write_bytes(b"")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "add", "VERSION", "CLAUDE.md", "AGENTS.md", ".gitignore", "data/.gitkeep"],
        cwd=root,
        check=True,
    )
    rr_id = "RR-REL-TEST"
    evidence = root / ".runtime" / "qualification" / rr_id
    evidence.mkdir(parents=True)
    (evidence / "summary.json").write_text(
        json.dumps({"rr_id": rr_id, "result": "pass"}), encoding="utf-8"
    )
    return root, rr_id


def _output(root: Path, name: str) -> Path:
    output = root / ".runtime" / "release" / name
    output.mkdir(parents=True)
    (output / "sbom.cdx.json").write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.4"}),
        encoding="utf-8",
    )
    return output


def test_release_archive_is_deterministic_and_preserves_only_expected_symlink(
    tmp_path: Path,
) -> None:
    root, rr_id = _repository(tmp_path)
    first = _output(root, "first")
    second = _output(root, "second")

    archive_one, manifest_one = release_artifact.build(root, first, rr_id)
    archive_two, _ = release_artifact.build(root, second, rr_id)

    assert archive_one.read_bytes() == archive_two.read_bytes()
    manifest = json.loads(manifest_one.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.2.0"
    assert manifest["qualification_rr_id"] == rr_id
    with tarfile.open(archive_one, mode="r:gz") as archive:
        members = {item.name: item for item in archive.getmembers()}
    assert set(members) == {
        "agent-builder-0.2.0/AGENTS.md",
        "agent-builder-0.2.0/CLAUDE.md",
        "agent-builder-0.2.0/VERSION",
        "agent-builder-0.2.0/.gitignore",
        "agent-builder-0.2.0/data/.gitkeep",
    }
    assert members["agent-builder-0.2.0/AGENTS.md"].issym()
    assert members["agent-builder-0.2.0/AGENTS.md"].linkname == "CLAUDE.md"


def test_release_inventory_rejects_unexpected_symlink(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    (root / "bad").symlink_to("CLAUDE.md")
    subprocess.run(["git", "add", "bad"], cwd=root, check=True)

    with pytest.raises(release_artifact.ReleaseError):
        release_artifact.inventory(root)

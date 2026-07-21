"""Private backup/restore safety and atomicity checks."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backup_data = _load("backup_data", ROOT / "scripts" / "backup_data.py")
restore_data = _load("agent_builder_restore_data", ROOT / "scripts" / "restore_data.py")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / ".runtime").mkdir()
    (repository / "data" / "agents" / "one").mkdir(parents=True)
    return repository


def _archive(
    path: Path,
    *,
    member_name: str = "data/value.txt",
    content: bytes = b"value\n",
    declared: bytes | None = None,
    symbolic: bool = False,
) -> None:
    declared = content if declared is None else declared
    manifest = json.dumps(
        {
            "schema": backup_data.BACKUP_SCHEMA,
            "backup_id": "fixture",
            "created_at_unix": 1,
            "files": [
                {
                    "path": member_name,
                    "size": len(declared),
                    "mode": 0o600,
                    "sha256": hashlib.sha256(declared).hexdigest(),
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with tarfile.open(path, mode="x:") as archive:
        header = tarfile.TarInfo("backup-manifest.json")
        header.mode = 0o600
        header.size = len(manifest)
        archive.addfile(header, io.BytesIO(manifest))
        data_dir = tarfile.TarInfo("data/")
        data_dir.type = tarfile.DIRTYPE
        data_dir.mode = 0o700
        archive.addfile(data_dir)
        item = tarfile.TarInfo(member_name)
        item.mode = 0o600
        if symbolic:
            item.type = tarfile.SYMTYPE
            item.linkname = "../../outside"
            item.size = 0
            archive.addfile(item)
        else:
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    os.chmod(path, 0o600)


def test_backup_round_trip_is_private_and_retains_previous_data(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    value = repository / "data" / "agents" / "one" / "state.sqlite"
    value.write_bytes(b"before\0state")
    os.chmod(value, 0o600)

    archive = backup_data.create_backup(repository, "release-1")

    assert archive.parent == repository / "backups"
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.stat().st_nlink == 1
    assert backup_data.validate_archive(archive)["backup_id"] == "release-1"
    value.write_bytes(b"after")
    previous = restore_data.restore_backup(repository, archive)
    assert value.read_bytes() == b"before\0state"
    assert (previous / "agents" / "one" / "state.sqlite").read_bytes() == b"after"
    assert list((repository / ".runtime" / "restore-staging").iterdir()) == []


def test_backup_rejects_source_symlink_and_hardlink(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    (repository / "data" / "linked").symlink_to(outside)
    with pytest.raises(backup_data.BackupError):
        backup_data.create_backup(repository, "linked")
    (repository / "data" / "linked").unlink()
    source = repository / "data" / "source"
    source.write_text("owned", encoding="utf-8")
    os.link(source, repository / "data" / "second-name")
    with pytest.raises(backup_data.BackupError):
        backup_data.create_backup(repository, "hardlinked")


@pytest.mark.parametrize(
    ("member_name", "content", "declared", "symbolic"),
    [
        ("../outside", b"bad", None, False),
        ("data/value.txt", b"changed", b"expected", False),
        ("data/value.txt", b"", b"expected", True),
    ],
)
def test_archive_validation_rejects_escape_digest_and_links(
    tmp_path: Path,
    member_name: str,
    content: bytes,
    declared: bytes | None,
    symbolic: bool,
) -> None:
    path = tmp_path / "bad.tar"
    _archive(
        path,
        member_name=member_name,
        content=content,
        declared=declared,
        symbolic=symbolic,
    )
    with pytest.raises(backup_data.BackupError):
        backup_data.validate_archive(path)


def test_restore_rejects_archive_outside_private_backup_directory(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "backups").mkdir(mode=0o700)
    outside = tmp_path / "fixture.tar"
    _archive(outside)

    with pytest.raises(backup_data.BackupError):
        restore_data._safe_archive(repository, str(outside))

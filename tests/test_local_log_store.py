"""Regression tests for contained diagnostic-log persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.local_log_store import (
    LocalLogStoreError,
    append_rotating_log,
    write_client_log,
)


def test_rotating_log_rejects_final_and_backup_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("sentinel", encoding="utf-8")
    target = root / "frontend_logs.txt"
    target.symlink_to(external)

    with pytest.raises(LocalLogStoreError, match="symlink"):
        append_rotating_log(root, target, "unsafe\n", max_bytes=1)
    assert external.read_text(encoding="utf-8") == "sentinel"

    target.unlink()
    target.write_text("existing", encoding="utf-8")
    backup = root / "frontend_logs.txt.1"
    backup.symlink_to(external)
    with pytest.raises(LocalLogStoreError, match="symlink"):
        append_rotating_log(root, target, "rotate\n", max_bytes=1)
    assert external.read_text(encoding="utf-8") == "sentinel"


def test_rotating_log_rejects_hard_link_without_modifying_external_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("sentinel", encoding="utf-8")
    target = root / "frontend_logs.txt"
    target.hardlink_to(external)

    with pytest.raises(LocalLogStoreError, match="private regular file"):
        append_rotating_log(root, target, "unsafe\n")

    assert external.read_text(encoding="utf-8") == "sentinel"


def test_client_log_rejects_parent_target_and_temp_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    external_dir = tmp_path / "external-dir"
    external_dir.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("sentinel", encoding="utf-8")

    linked_logs = root / "logs"
    linked_logs.symlink_to(external_dir, target_is_directory=True)
    linked_target = linked_logs / "client_log_linked.json"
    with pytest.raises(LocalLogStoreError, match="symlink"):
        write_client_log(root, linked_target, {"safe": 1}, linked_logs)
    assert list(external_dir.iterdir()) == []

    linked_logs.unlink()
    linked_logs.mkdir()
    target = linked_logs / "client_log_target.json"
    target.symlink_to(external)
    with pytest.raises(LocalLogStoreError, match="symlink"):
        write_client_log(root, target, {"safe": 1}, linked_logs)
    assert external.read_text(encoding="utf-8") == "sentinel"

    target.unlink()
    temporary = target.with_suffix(".tmp")
    temporary.symlink_to(external)
    with pytest.raises(LocalLogStoreError, match="symlink"):
        write_client_log(root, target, {"safe": 1}, linked_logs)
    assert external.read_text(encoding="utf-8") == "sentinel"


def test_normal_log_writes_stay_below_root(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    rotating = root / "frontend_logs.txt"
    append_rotating_log(root, rotating, "one\n", max_bytes=5, backups=2)
    append_rotating_log(root, rotating, "two\n", max_bytes=5, backups=2)
    assert rotating.read_text(encoding="utf-8") == "two\n"
    assert (root / "frontend_logs.txt.1").read_text(encoding="utf-8") == "one\n"

    logs_dir = root / "logs"
    target = logs_dir / "client_log_normal.json"
    write_client_log(root, target, {"payload_length": 12}, logs_dir)
    assert json.loads(target.read_text(encoding="utf-8")) == {"payload_length": 12}
    target.resolve().relative_to(root.resolve())

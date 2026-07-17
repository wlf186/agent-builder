"""Regression coverage for persistent-directory containment primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage_paths import (
    UnsafeStoragePathError,
    ensure_real_directory,
    validate_regular_file,
)


def test_directory_creation_rejects_nested_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "data"
    root.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeStoragePathError, match="link or non-directory"):
        ensure_real_directory(root / "nested" / "child")

    assert list(outside.iterdir()) == []


def test_regular_file_validation_rejects_symlink_and_hardlink(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    original.write_bytes(b"sentinel")

    symlink = tmp_path / "symlink"
    symlink.symlink_to(original)
    with pytest.raises(UnsafeStoragePathError, match="non-regular"):
        validate_regular_file(symlink)

    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(original)
    with pytest.raises(UnsafeStoragePathError, match="multiple hard links"):
        validate_regular_file(hardlink)

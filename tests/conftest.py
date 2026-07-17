"""Repository-wide pytest isolation settings."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_ROOT = PROJECT_ROOT / ".runtime" / "tests" / "tmp"


def _configure_test_temp_root() -> Path:
    """Configure tempfile before pytest imports any test modules."""

    if TEST_TEMP_ROOT.is_symlink():
        raise RuntimeError(f"pytest temporary root is a symlink: {TEST_TEMP_ROOT}")

    resolved_root = TEST_TEMP_ROOT.resolve(strict=False)
    try:
        relative = resolved_root.relative_to(PROJECT_ROOT.resolve(strict=True))
    except ValueError as exc:
        raise RuntimeError(
            f"pytest temporary root escapes the project: {resolved_root}"
        ) from exc
    if not relative.parts:
        raise RuntimeError("pytest temporary root must be below the project root")

    resolved_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if TEST_TEMP_ROOT.is_symlink():
        raise RuntimeError(f"pytest temporary root is a symlink: {TEST_TEMP_ROOT}")

    resolved_root = TEST_TEMP_ROOT.resolve(strict=True)
    if not resolved_root.is_dir() or stat.S_ISLNK(os.lstat(resolved_root).st_mode):
        raise RuntimeError(
            f"pytest temporary root is not a real directory: {resolved_root}"
        )

    os.chmod(resolved_root, 0o700, follow_symlinks=False)
    if stat.S_IMODE(os.lstat(resolved_root).st_mode) != 0o700:
        raise RuntimeError(
            f"failed to secure pytest temporary root: {resolved_root}"
        )

    for variable in ("TMPDIR", "TEMP", "TMP"):
        os.environ[variable] = str(resolved_root)
    tempfile.tempdir = str(resolved_root)
    return resolved_root


# conftest modules load before test-module collection, so this also covers
# tempfile calls made at module import time rather than only during fixtures.
_configure_test_temp_root()

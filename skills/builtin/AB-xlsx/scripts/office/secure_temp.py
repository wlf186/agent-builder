"""Create Office helper temporary directories inside the current workspace."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafeTempRootError(RuntimeError):
    """Raised when the configured temporary root is not workspace-contained."""


def secure_temp_root() -> Path:
    """Return a mode-0700 temporary root strictly below the current directory.

    ``TMPDIR`` is authoritative when it is present.  Otherwise ``.tmp`` below
    the current working directory is used.  Unsafe configuration is rejected
    instead of silently falling back to a system temporary directory.
    """

    cwd = Path.cwd().resolve(strict=True)
    configured = os.environ.get("TMPDIR")
    if configured is None:
        candidate = cwd / ".tmp"
    elif not configured.strip():
        raise UnsafeTempRootError("TMPDIR is set but empty")
    else:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = cwd / candidate

    if candidate.is_symlink():
        raise UnsafeTempRootError(f"temporary root must not be a symlink: {candidate}")

    resolved_candidate = candidate.resolve(strict=False)
    _require_workspace_child(resolved_candidate, cwd)

    resolved_candidate.mkdir(parents=True, mode=0o700, exist_ok=True)

    if candidate.is_symlink():
        raise UnsafeTempRootError(f"temporary root must not be a symlink: {candidate}")

    resolved_candidate = candidate.resolve(strict=True)
    _require_workspace_child(resolved_candidate, cwd)
    status = os.lstat(resolved_candidate)
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise UnsafeTempRootError(
            f"temporary root must be a real directory: {resolved_candidate}"
        )

    os.chmod(resolved_candidate, 0o700, follow_symlinks=False)
    if stat.S_IMODE(os.lstat(resolved_candidate).st_mode) != 0o700:
        raise UnsafeTempRootError(
            f"failed to secure temporary root permissions: {resolved_candidate}"
        )

    return resolved_candidate


def _require_workspace_child(candidate: Path, cwd: Path) -> None:
    try:
        relative = candidate.relative_to(cwd)
    except ValueError as exc:
        raise UnsafeTempRootError(
            f"temporary root must be inside the current directory: {candidate}"
        ) from exc
    if not relative.parts:
        raise UnsafeTempRootError(
            f"temporary root must be below, not equal to, the current directory: {cwd}"
        )

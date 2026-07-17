"""Fail-closed primitives for project-managed persistent paths.

``Path.mkdir(parents=True)`` follows pre-existing directory symlinks.  That is
convenient for ordinary applications, but it is the wrong default for storage
whose containment is a security invariant.  These helpers inspect every path
component with ``lstat`` and create missing directories one level at a time.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafeStoragePathError(RuntimeError):
    """Raised when a managed path is a link or a non-regular filesystem node."""


def absolute_path(path: Path) -> Path:
    """Return a normalized absolute path without resolving symbolic links."""

    return Path(os.path.abspath(os.fspath(path)))


def ensure_real_directory(path: Path, *, mode: int = 0o700) -> Path:
    """Create *path* without following any pre-existing symlink component."""

    target = absolute_path(path)
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, mode)
            except FileExistsError:
                # A concurrent creator must pass the same no-link validation.
                pass
            try:
                metadata = os.lstat(current)
            except FileNotFoundError as exc:
                raise UnsafeStoragePathError(
                    f"managed directory disappeared during creation: {current}"
                ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeStoragePathError(
                f"managed directory contains a link or non-directory: {current}"
            )
    return target


def validate_regular_file(
    path: Path,
    *,
    allow_missing: bool = True,
    reject_hardlinks: bool = True,
) -> Path:
    """Validate a managed file without following links.

    Rejecting multiply-linked files prevents an append or in-place database
    update from modifying an inode that is also reachable outside the managed
    directory.
    """

    target = absolute_path(path)
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        if allow_missing:
            return target
        raise UnsafeStoragePathError(f"managed file does not exist: {target}")
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeStoragePathError(
            f"managed file is a link or non-regular node: {target}"
        )
    if reject_hardlinks and metadata.st_nlink != 1:
        raise UnsafeStoragePathError(
            f"managed file has multiple hard links: {target}"
        )
    return target

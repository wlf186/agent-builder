#!/usr/bin/env python3
"""Validate and restore one checkout-local Agent Builder data backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile

from backup_data import BackupError, ROOT, validate_archive


def _safe_archive(root: Path, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    parent = (root / "backups").resolve(strict=True)
    resolved_parent = candidate.parent.resolve(strict=True)
    if resolved_parent != parent:
        raise BackupError("restore archive must be directly inside backups")
    metadata = os.lstat(candidate)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise BackupError("restore archive is unsafe")
    return candidate


def restore_backup(root: Path, archive_path: Path) -> Path:
    root = root.resolve(strict=True)
    manifest = validate_archive(archive_path)
    restore_parent = root / ".runtime" / "restore-staging"
    restore_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(restore_parent, 0o700)
    staging = Path(tempfile.mkdtemp(prefix="restore-", dir=restore_parent))
    expected = {item["path"]: item for item in manifest["files"]}  # type: ignore[index]
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive.getmembers()[1:]:
                relative = PurePosixPath(member.name.rstrip("/"))
                target = staging.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(target, 0o700)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                item = expected[member.name]
                if stream is None:
                    raise BackupError("restore file is unreadable")
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    item["mode"] or 0o600,
                )
                digest = hashlib.sha256()
                size = 0
                try:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise BackupError("restore write made no progress")
                            view = view[written:]
                    os.fchmod(descriptor, item["mode"] or 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise BackupError("restored file digest changed")
        restored_data = staging / "data"
        if not restored_data.is_dir() or restored_data.is_symlink():
            raise BackupError("restored data root is missing")
        recovery = root / ".runtime" / "recovery"
        recovery.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(recovery, 0o700)
        backup_id = manifest["backup_id"]
        previous = recovery / f"pre-restore-{backup_id}"
        if previous.exists() or previous.is_symlink():
            raise BackupError("restore recovery destination exists")
        current = root / "data"
        os.replace(current, previous)
        try:
            os.replace(restored_data, current)
        except BaseException:
            os.replace(previous, current)
            raise
        directory = os.open(root, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return previous
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not args.yes:
        print("restore failed: --yes is required", file=os.sys.stderr)
        return 2
    try:
        archive = _safe_archive(ROOT, args.archive)
        previous = restore_backup(ROOT, archive)
    except (BackupError, OSError, tarfile.TarError, KeyError, TypeError) as exc:
        print(f"restore failed: {type(exc).__name__}", file=os.sys.stderr)
        return 1
    print(f"restore complete; previous data retained at {previous.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

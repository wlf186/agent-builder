#!/usr/bin/env python3
"""Create and validate one private, checkout-local persistent-data backup."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
from typing import BinaryIO, Iterator


BACKUP_SCHEMA = "agent-builder-data-backup-v1"
BACKUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_BACKUP_ENTRIES = 100_000
MAX_BACKUP_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


class BackupError(RuntimeError):
    """Backup input, source or archive validation failed closed."""


@dataclass(frozen=True, slots=True)
class InventoryFile:
    path: str
    size: int
    mode: int
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "mode": self.mode,
            "sha256": self.digest,
        }


def _digest_file(path: Path, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
        ):
            raise BackupError("unsafe backup source file")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise BackupError("backup source exceeds its byte limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or size != before.st_size
        ):
            raise BackupError("backup source changed while reading")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


@contextmanager
def _verified_stream(path: Path, item: InventoryFile) -> Iterator[BinaryIO]:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    stream: BinaryIO | None = None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size != item.size
        ):
            raise BackupError("unsafe backup source file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        if size != item.size or digest.hexdigest() != item.digest:
            raise BackupError("backup source changed after inventory")
        os.lseek(descriptor, 0, os.SEEK_SET)
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        yield stream
        after = os.fstat(stream.fileno())
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise BackupError("backup source changed while archiving")
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


def build_inventory(root: Path) -> tuple[tuple[str, ...], tuple[InventoryFile, ...]]:
    data = root / "data"
    metadata = os.lstat(data)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise BackupError("persistent data root is unsafe")
    directories: list[str] = ["data"]
    files: list[InventoryFile] = []
    pending = [data]
    entries = 1
    total = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            ordered = sorted(children, key=lambda item: os.fsencode(item.name))
        for child in ordered:
            entries += 1
            if entries > MAX_BACKUP_ENTRIES:
                raise BackupError("backup entry limit exceeded")
            path = Path(child.path)
            item = os.lstat(path)
            relative = path.relative_to(root).as_posix()
            if item.st_uid != os.getuid() or stat.S_ISLNK(item.st_mode):
                raise BackupError("unsafe backup source entry")
            if stat.S_ISDIR(item.st_mode):
                directories.append(relative)
                pending.append(path)
            elif stat.S_ISREG(item.st_mode):
                digest, size = _digest_file(path, MAX_BACKUP_BYTES - total)
                total += size
                files.append(
                    InventoryFile(
                        relative,
                        size,
                        stat.S_IMODE(item.st_mode) & 0o700,
                        digest,
                    )
                )
            else:
                raise BackupError("special backup source entry")
    return tuple(sorted(directories)), tuple(sorted(files, key=lambda item: item.path))


def _manifest(backup_id: str, files: tuple[InventoryFile, ...]) -> bytes:
    payload = json.dumps(
        {
            "schema": BACKUP_SCHEMA,
            "backup_id": backup_id,
            "created_at_unix": int(time.time()),
            "files": [item.to_dict() for item in files],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise BackupError("backup manifest exceeds its byte limit")
    return payload


def _tar_info(name: str, *, size: int, mode: int, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory and not name.endswith("/") else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def validate_archive(path: Path, *, expected_id: str | None = None) -> dict[str, object]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_BACKUP_BYTES + MAX_MANIFEST_BYTES + 64 * 1024 * 1024
    ):
        raise BackupError("backup archive is unsafe")
    with tarfile.open(path, mode="r:") as archive:
        members = archive.getmembers()
        if not 2 <= len(members) <= MAX_BACKUP_ENTRIES + 2:
            raise BackupError("backup archive entry count is invalid")
        if members[0].name != "backup-manifest.json" or not members[0].isreg():
            raise BackupError("backup manifest is missing")
        manifest_stream = archive.extractfile(members[0])
        if manifest_stream is None or members[0].size > MAX_MANIFEST_BYTES:
            raise BackupError("backup manifest is invalid")
        try:
            manifest = json.loads(manifest_stream.read(MAX_MANIFEST_BYTES + 1))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("backup manifest is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema", "backup_id", "created_at_unix", "files"}
            or manifest.get("schema") != BACKUP_SCHEMA
            or not isinstance(manifest.get("backup_id"), str)
            or BACKUP_ID.fullmatch(manifest["backup_id"]) is None
            or (expected_id is not None and manifest["backup_id"] != expected_id)
            or not isinstance(manifest.get("created_at_unix"), int)
            or isinstance(manifest.get("created_at_unix"), bool)
            or not isinstance(manifest.get("files"), list)
        ):
            raise BackupError("backup manifest schema is invalid")
        expected: dict[str, dict[str, object]] = {}
        for item in manifest["files"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "size", "mode", "sha256"}
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or not 0 <= item["size"] <= MAX_BACKUP_BYTES
                or not isinstance(item.get("mode"), int)
                or item["mode"] not in {0, 0o100, 0o200, 0o300, 0o400, 0o500, 0o600, 0o700}
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None
                or item["path"] in expected
            ):
                raise BackupError("backup file manifest is invalid")
            parts = PurePosixPath(item["path"])
            if parts.is_absolute() or parts.parts[0] != "data" or ".." in parts.parts:
                raise BackupError("backup path escapes data root")
            expected[item["path"]] = item
        seen: set[str] = set()
        seen_files: set[str] = set()
        total = 0
        for member in members[1:]:
            parts = PurePosixPath(member.name.rstrip("/"))
            if (
                parts.is_absolute()
                or not parts.parts
                or parts.parts[0] != "data"
                or any(part in {"", ".", ".."} for part in parts.parts)
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isreg())
                or member.name in seen
            ):
                raise BackupError("backup archive contains an unsafe entry")
            seen.add(member.name)
            if member.isreg():
                seen_files.add(member.name)
                item = expected.get(member.name)
                if item is None or member.size != item["size"]:
                    raise BackupError("backup file does not match manifest")
                stream = archive.extractfile(member)
                if stream is None:
                    raise BackupError("backup file is unreadable")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if total > MAX_BACKUP_BYTES:
                        raise BackupError("backup expanded size limit exceeded")
                    digest.update(chunk)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise BackupError("backup file digest changed")
        if set(expected) != seen_files:
            raise BackupError("backup file set does not match manifest")
        return manifest


def create_backup(root: Path, backup_id: str) -> Path:
    root = root.resolve(strict=True)
    if BACKUP_ID.fullmatch(backup_id) is None or not (root / ".git").is_dir():
        raise BackupError("invalid backup request")
    backups = root / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backups, 0o700)
    destination = backups / f"{backup_id}.tar"
    temporary = backups / f".{backup_id}.{os.getpid()}.tmp"
    if destination.exists() or destination.is_symlink() or temporary.exists():
        raise BackupError("backup destination already exists")
    directories, files = build_inventory(root)
    manifest = _manifest(backup_id, files)
    try:
        with tarfile.open(temporary, mode="x:") as archive:
            archive.addfile(
                _tar_info("backup-manifest.json", size=len(manifest), mode=0o600),
                io.BytesIO(manifest),
            )
            for directory in directories:
                archive.addfile(_tar_info(directory, size=0, mode=0o700, directory=True))
            for item in files:
                source = root / item.path
                with _verified_stream(source, item) as stream:
                    archive.addfile(
                        _tar_info(item.path, size=item.size, mode=item.mode or 0o600),
                        stream,
                    )
        os.chmod(temporary, 0o600)
        validate_archive(temporary, expected_id=backup_id)
        os.link(temporary, destination)
        temporary.unlink()
        directory = os.open(backups, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_id")
    args = parser.parse_args()
    try:
        destination = create_backup(ROOT, args.backup_id)
    except (BackupError, OSError, tarfile.TarError) as exc:
        print(f"backup failed: {type(exc).__name__}", file=os.sys.stderr)
        return 1
    print(f"backup complete: {destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

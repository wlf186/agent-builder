#!/usr/bin/env python3
"""Build a deterministic, checkout-local release source archive and manifest."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
MAX_SOURCE_FILES = 2_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024


class ReleaseError(RuntimeError):
    """Release input or source identity is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class SourceItem:
    path: str
    kind: str
    size: int
    mode: int
    digest: str

    def manifest(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "mode": self.mode,
            "sha256": self.digest,
        }


def _sha256(path: Path, maximum: int) -> tuple[str, int]:
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
            raise ReleaseError("unsafe release source file")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise ReleaseError("release source byte limit exceeded")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or size != before.st_size
        ):
            raise ReleaseError("release source changed while reading")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _source_paths(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise ReleaseError("cannot enumerate release sources")
    try:
        values = tuple(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise ReleaseError("release source path is not UTF-8") from exc
    if not values or len(values) > MAX_SOURCE_FILES or len(set(values)) != len(values):
        raise ReleaseError("release source file count is invalid")
    for value in values:
        parts = PurePosixPath(value)
        if (
            parts.is_absolute()
            or not parts.parts
            or any(part in {"", ".", ".."} for part in parts.parts)
            or parts.parts[0] in {".git", ".runtime", ".tools", ".venv", "backups"}
            or (parts.parts[0] == "data" and value != "data/.gitkeep")
        ):
            raise ReleaseError("release source path is unsafe")
    return tuple(sorted(values, key=os.fsencode))


def inventory(root: Path) -> tuple[SourceItem, ...]:
    items: list[SourceItem] = []
    total = 0
    for relative in _source_paths(root):
        path = root / relative
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            if relative != "AGENTS.md" or os.readlink(path) != "CLAUDE.md":
                raise ReleaseError("unexpected release source symlink")
            target = b"CLAUDE.md"
            items.append(
                SourceItem(relative, "symlink", len(target), 0o777, hashlib.sha256(target).hexdigest())
            )
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ReleaseError("unsafe release source entry")
        digest, size = _sha256(path, MAX_SOURCE_BYTES - total)
        total += size
        mode = 0o755 if metadata.st_mode & 0o111 else 0o644
        items.append(SourceItem(relative, "file", size, mode, digest))
    return tuple(items)


def _tar_header(name: str, item: SourceItem) -> tarfile.TarInfo:
    header = tarfile.TarInfo(name)
    header.uid = 0
    header.gid = 0
    header.uname = ""
    header.gname = ""
    header.mtime = 0
    header.mode = item.mode
    if item.kind == "symlink":
        header.type = tarfile.SYMTYPE
        header.linkname = "CLAUDE.md"
        header.size = 0
    else:
        header.type = tarfile.REGTYPE
        header.size = item.size
    return header


def build(root: Path, output: Path, rr_id: str) -> tuple[Path, Path]:
    root = root.resolve(strict=True)
    version = (root / "VERSION").read_text(encoding="ascii").strip()
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseError("VERSION is invalid")
    qualification = root / ".runtime" / "qualification" / rr_id / "summary.json"
    sbom = output / "sbom.cdx.json"
    try:
        qualification_payload = json.loads(qualification.read_text(encoding="utf-8"))
        sbom_payload = json.loads(sbom.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("release evidence is missing or invalid") from exc
    if qualification_payload.get("result") != "pass" or qualification_payload.get("rr_id") != rr_id:
        raise ReleaseError("qualification evidence did not pass")
    if not isinstance(sbom_payload, dict) or sbom_payload.get("bomFormat") != "CycloneDX":
        raise ReleaseError("SBOM is not CycloneDX JSON")
    items = inventory(root)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    archive = output / f"agent-builder-{version}.tar.gz"
    temporary = output / f".{archive.name}.{os.getpid()}.tmp"
    if archive.exists() or archive.is_symlink() or temporary.exists():
        raise ReleaseError("release archive already exists")
    prefix = f"agent-builder-{version}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w:", format=tarfile.PAX_FORMAT) as archive_file:
                    for item in items:
                        header = _tar_header(f"{prefix}/{item.path}", item)
                        if item.kind == "symlink":
                            archive_file.addfile(header)
                            continue
                        source = root / item.path
                        descriptor = os.open(
                            source,
                            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                        )
                        with os.fdopen(descriptor, "rb", closefd=True) as stream:
                            before = os.fstat(stream.fileno())
                            if (
                                not stat.S_ISREG(before.st_mode)
                                or before.st_uid != os.getuid()
                                or before.st_nlink != 1
                                or before.st_size != item.size
                            ):
                                raise ReleaseError("unsafe release source file")
                            digest = hashlib.sha256()
                            while True:
                                chunk = stream.read(1024 * 1024)
                                if not chunk:
                                    break
                                digest.update(chunk)
                            if digest.hexdigest() != item.digest:
                                raise ReleaseError("release source changed after inventory")
                            stream.seek(0)
                            archive_file.addfile(header, stream)
                            after = os.fstat(stream.fileno())
                            if (
                                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                            ):
                                raise ReleaseError("release source changed while archiving")
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, archive)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=False
    ).stdout != ""
    manifest = {
        "schema": "agent-builder-release-v1",
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision if re.fullmatch(r"[a-f0-9]{40}", revision) else None,
        "source_dirty": dirty,
        "qualification_rr_id": rr_id,
        "qualification_sha256": _sha256(qualification, 4 * 1024 * 1024)[0],
        "sbom_sha256": _sha256(sbom, 16 * 1024 * 1024)[0],
        "archive": archive.name,
        "archive_sha256": _sha256(archive, MAX_SOURCE_BYTES)[0],
        "sources": [item.manifest() for item in items],
    }
    manifest_path = output / "release-manifest.json"
    manifest_temporary = output / f".{manifest_path.name}.{os.getpid()}.tmp"
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    descriptor = os.open(
        manifest_temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ReleaseError("release manifest write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(manifest_temporary, manifest_path)
    directory = os.open(output, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return archive, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rr-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        archive, manifest = build(ROOT, args.output, args.rr_id)
    except (ReleaseError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"release artifact failed: {type(exc).__name__}", file=os.sys.stderr)
        return 1
    print(f"release archive: {archive.relative_to(ROOT)}")
    print(f"release manifest: {manifest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

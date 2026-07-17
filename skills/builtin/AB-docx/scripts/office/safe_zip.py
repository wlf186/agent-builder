"""Fail-closed ZIP validation and extraction for untrusted Office archives."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile

MAX_MEMBERS = 2048
MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

_CHUNK_SIZE = 1024 * 1024
_MAX_NAME_BYTES = 4096
_MAX_COMPONENT_BYTES = 255
_MAX_DEPTH = 64
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class SafeZipError(ValueError):
    """Raised when an archive violates a safety invariant."""


@dataclass(frozen=True)
class _Member:
    info: zipfile.ZipInfo
    relative_path: PurePosixPath
    is_directory: bool


def _validated_member(info: zipfile.ZipInfo) -> _Member:
    name = info.filename
    if not name or "\x00" in name:
        raise SafeZipError("ZIP member has an empty or NUL-containing name")
    if unicodedata.normalize("NFC", name) != name:
        raise SafeZipError(f"ZIP member name is not NFC-normalized: {name!r}")
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        raise SafeZipError("ZIP member name is too long")
    if "\\" in name:
        raise SafeZipError(f"ZIP member uses a backslash path: {name!r}")

    is_directory = info.is_dir()
    logical_name = name[:-1] if is_directory and name.endswith("/") else name
    if not logical_name or logical_name.startswith("/"):
        raise SafeZipError(f"ZIP member has an absolute path: {name!r}")
    if _DRIVE_PREFIX.match(logical_name) or PurePosixPath(logical_name).is_absolute():
        raise SafeZipError(f"ZIP member has a drive or absolute path: {name!r}")

    parts = logical_name.split("/")
    if len(parts) > _MAX_DEPTH:
        raise SafeZipError(f"ZIP member path is too deep: {name!r}")
    if any(part in {"", ".", ".."} for part in parts):
        raise SafeZipError(f"ZIP member path is not canonical: {name!r}")
    if any(len(part.encode("utf-8")) > _MAX_COMPONENT_BYTES for part in parts):
        raise SafeZipError(f"ZIP member has an overlong path component: {name!r}")

    if info.flag_bits & 0x41:
        raise SafeZipError(f"Encrypted ZIP member is not allowed: {name!r}")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise SafeZipError(f"Unsupported ZIP compression method: {name!r}")
    if info.file_size < 0 or info.compress_size < 0:
        raise SafeZipError(f"ZIP member has an invalid size: {name!r}")
    if info.file_size > MAX_MEMBER_UNCOMPRESSED:
        raise SafeZipError(f"ZIP member exceeds the size limit: {name!r}")
    if info.file_size and (
        info.compress_size == 0
        or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO
    ):
        raise SafeZipError(f"ZIP member exceeds the compression-ratio limit: {name!r}")
    if is_directory and info.file_size:
        raise SafeZipError(f"ZIP directory member contains data: {name!r}")

    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.create_system == 3:
        expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
        if file_type not in {0, expected_type}:
            raise SafeZipError(f"ZIP links and special files are not allowed: {name!r}")
    elif (info.external_attr & 0x10) and not is_directory:
        raise SafeZipError(f"ZIP member has inconsistent directory metadata: {name!r}")

    return _Member(info, PurePosixPath(*parts), is_directory)


def _validated_members(archive: zipfile.ZipFile) -> tuple[list[_Member], int]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise SafeZipError(f"ZIP archive contains more than {MAX_MEMBERS} members")

    members: list[_Member] = []
    by_path: dict[str, _Member] = {}
    total = 0
    for info in infos:
        member = _validated_member(info)
        key = member.relative_path.as_posix().casefold()
        if key in by_path:
            raise SafeZipError(f"ZIP archive contains a duplicate member: {info.filename!r}")
        by_path[key] = member
        members.append(member)
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise SafeZipError("ZIP archive exceeds the total uncompressed-size limit")

    for key, member in by_path.items():
        parts = key.split("/")
        for depth in range(1, len(parts)):
            ancestor = by_path.get("/".join(parts[:depth]))
            if ancestor is not None and not ancestor.is_directory:
                raise SafeZipError(
                    f"ZIP member is nested below a file: {member.info.filename!r}"
                )
    return members, total


def _read_member_stream(
    archive: zipfile.ZipFile,
    member: _Member,
    destination,
    *,
    running_total: int = 0,
) -> tuple[int, int]:
    written = 0
    try:
        with archive.open(member.info, "r") as source:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                running_total += len(chunk)
                if written > member.info.file_size or written > MAX_MEMBER_UNCOMPRESSED:
                    raise SafeZipError(
                        f"ZIP member expanded beyond its declared size: {member.info.filename!r}"
                    )
                if running_total > MAX_TOTAL_UNCOMPRESSED:
                    raise SafeZipError(
                        "ZIP archive expanded beyond the total uncompressed-size limit"
                    )
                destination.write(chunk)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError) as exc:
        raise SafeZipError(
            f"Unable to safely read ZIP member {member.info.filename!r}"
        ) from exc
    if written != member.info.file_size:
        raise SafeZipError(
            f"ZIP member size mismatch for {member.info.filename!r}: "
            f"expected {member.info.file_size}, read {written}"
        )
    return written, running_total


def safe_extract_zip(archive_path: str | Path, destination: str | Path) -> Path:
    """Validate and atomically extract an archive into an absent or empty directory."""
    destination = Path(destination)
    if destination.is_symlink():
        raise SafeZipError("ZIP extraction destination must not be a symlink")
    if destination.exists() and not destination.is_dir():
        raise SafeZipError("ZIP extraction destination must be a directory")
    if destination.exists() and any(destination.iterdir()):
        raise SafeZipError("ZIP extraction destination must be empty")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve() / destination.name
    staging = Path(
        tempfile.mkdtemp(prefix=".safe-zip-", dir=str(destination.parent))
    )
    try:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                members, declared_total = _validated_members(archive)
                actual_total = 0
                for member in members:
                    target = staging.joinpath(*member.relative_path.parts)
                    if member.is_directory:
                        _, actual_total = _read_member_stream(
                            archive, member, BytesIO(), running_total=actual_total
                        )
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with target.open("xb") as output:
                        _, actual_total = _read_member_stream(
                            archive, member, output, running_total=actual_total
                        )
                    os.chmod(target, 0o600)
                if actual_total != declared_total:
                    raise SafeZipError(
                        f"ZIP archive size mismatch: expected {declared_total}, read {actual_total}"
                    )
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise SafeZipError("Invalid ZIP archive") from exc

        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def safe_read_zip_member(archive_path: str | Path, member_name: str) -> bytes:
    """Read one member only after applying the same archive-wide safety checks."""
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members, _ = _validated_members(archive)
            match = next(
                (m for m in members if m.relative_path.as_posix() == member_name),
                None,
            )
            if match is None or match.is_directory:
                raise SafeZipError(f"ZIP member not found: {member_name!r}")
            chunks = BytesIO()
            _read_member_stream(archive, match, chunks, running_total=0)
            return chunks.getvalue()
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SafeZipError("Invalid ZIP archive") from exc

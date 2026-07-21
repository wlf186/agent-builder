"""Versioned, Agent-scoped Skill packages and fail-closed execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import zipfile
from typing import Callable, Mapping

from .capsule import AgentCapsule, SAFE_ID
from .command_exec import CommandExecutionError, CommandExecutor
from .permissions import CapabilityRequest
from .contracts import utc_now


MAX_SKILLS = 16
MAX_SKILL_ARCHIVE_BYTES = 12 * 1024
MAX_SKILL_EXPANDED_BYTES = 16 * 1024
MAX_SKILL_SOURCE_BYTES = 8 * 1024
MAX_SKILL_INPUT_BYTES = 4 * 1024
MAX_SKILL_FILES = 2
SKILL_FILES = frozenset({"skill.json", "main.py"})
VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")


class SkillError(RuntimeError):
    """A Skill package, lifecycle or execution boundary failed closed."""


@dataclass(frozen=True, slots=True)
class SkillRecord:
    skill_id: str
    version: str
    display_name: str
    package_digest: str
    content_digest: str
    capabilities_json: str
    installed_at: str
    updated_at: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "display_name": self.display_name,
            "package_digest": self.package_digest,
            "capabilities": json.loads(self.capabilities_json),
            "execution": "singleton-landlock-seccomp-v1",
            "network": "denied",
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
        }


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SkillError("Skill JSON is invalid") from exc


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Skill file write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_private(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > maximum
        ):
            raise SkillError("Skill package file is unsafe")
        content = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        if (
            len(content) > maximum
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise SkillError("Skill package file changed while reading")
        return content
    finally:
        os.close(descriptor)


def _content_digest(manifest: bytes, source: bytes) -> str:
    digest = hashlib.sha256(b"agent-builder-skill-content-v1\0")
    for name, content in ((b"skill.json", manifest), (b"main.py", source)):
        digest.update(len(name).to_bytes(2, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def inspect_skill_archive(raw: bytes, expected_digest: str) -> tuple[dict[str, object], bytes, bytes, str]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_SKILL_ARCHIVE_BYTES:
        raise SkillError("Skill archive exceeds its byte limit")
    actual_digest = hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise SkillError("Skill archive digest does not match")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        infos = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise SkillError("Skill archive is invalid") from exc
    if len(infos) != MAX_SKILL_FILES or {item.filename for item in infos} != SKILL_FILES:
        raise SkillError("Skill archive has an unexpected file set")
    if sum(item.file_size for item in infos) > MAX_SKILL_EXPANDED_BYTES:
        raise SkillError("Skill archive expanded size exceeds its limit")
    contents: dict[str, bytes] = {}
    for info in infos:
        mode = (info.external_attr >> 16) & 0xFFFF
        if (
            info.flag_bits & 0x1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or info.filename.startswith(("/", "\\"))
            or "/" in info.filename
            or "\\" in info.filename
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
        ):
            raise SkillError("Skill archive entry is unsafe")
        try:
            content = archive.read(info)
        except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
            raise SkillError("Skill archive entry failed integrity validation") from exc
        if len(content) != info.file_size:
            raise SkillError("Skill archive entry size changed")
        contents[info.filename] = content
    source = contents["main.py"]
    manifest_raw = contents["skill.json"]
    if not 1 <= len(source) <= MAX_SKILL_SOURCE_BYTES or b"\x00" in source:
        raise SkillError("Skill source is invalid")
    try:
        source.decode("utf-8")
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError("Skill package is not valid UTF-8 JSON/Python") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {
            "schema_version", "skill_id", "version", "display_name",
            "entrypoint", "capabilities", "dependencies",
        }
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("skill_id"), str)
        or SAFE_ID.fullmatch(manifest["skill_id"]) is None
        or not isinstance(manifest.get("version"), str)
        or VERSION.fullmatch(manifest["version"]) is None
        or not isinstance(manifest.get("display_name"), str)
        or not manifest["display_name"].strip()
        or len(manifest["display_name"].encode("utf-8")) > 128
        or manifest.get("entrypoint") != "main.py"
        or manifest.get("capabilities") != []
        or manifest.get("dependencies") != []
    ):
        raise SkillError("Skill manifest violates the v1 contract")
    try:
        compile(source, "main.py", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise SkillError("Skill source does not compile") from exc
    canonical_manifest = _canonical(manifest)
    return manifest, canonical_manifest, source, _content_digest(canonical_manifest, source)


class SkillRegistry:
    def __init__(self, repository_root: Path, capsule: AgentCapsule, database: Path) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.capsule = capsule
        self.data_root = capsule.data_root / "skills"
        self.runtime_root = capsule.runtime_root / "skills"
        self._lock = threading.RLock()
        self._active: dict[str, int] = {}
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS skills (
                skill_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                display_name TEXT NOT NULL,
                package_digest TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                installed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self._connection.commit()
        self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        for root in (self.data_root, self.runtime_root):
            entries = list(root.iterdir())
            if len(entries) > MAX_SKILLS * 3:
                raise SkillError("Skill root exceeds its scan limit")
            for entry in entries:
                if entry.name.startswith(".staging-"):
                    metadata = os.lstat(entry)
                    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                        raise SkillError("Skill staging entry is unsafe")
                    shutil.rmtree(entry)

    @staticmethod
    def _record(row: sqlite3.Row) -> SkillRecord:
        return SkillRecord(**dict(row))

    def list(self) -> tuple[SkillRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM skills ORDER BY skill_id"
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def get(self, skill_id: str) -> SkillRecord:
        if SAFE_ID.fullmatch(skill_id) is None:
            raise KeyError("Skill not found")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM skills WHERE skill_id=?", (skill_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Skill not found")
        return self._record(row)

    def install(self, raw: bytes, expected_digest: str) -> SkillRecord:
        manifest, manifest_raw, source, content_digest = inspect_skill_archive(raw, expected_digest)
        skill_id = str(manifest["skill_id"])
        version = str(manifest["version"])
        token = os.urandom(8).hex()
        data_stage = self.data_root / f".staging-{token}"
        runtime_stage = self.runtime_root / f".staging-{token}"
        data_target = self.data_root / skill_id
        runtime_target = self.runtime_root / skill_id
        old_data = self.data_root / f".staging-old-{token}"
        old_runtime = self.runtime_root / f".staging-old-{token}"
        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM skills WHERE skill_id=?", (skill_id,)
            ).fetchone()
            if existing is None and self._connection.execute(
                "SELECT COUNT(*) FROM skills"
            ).fetchone()[0] >= MAX_SKILLS:
                raise SkillError("Skill registry capacity exhausted")
            if self._active.get(skill_id, 0):
                raise SkillError("Skill is currently executing")
            if existing is not None and existing["version"] == version:
                raise SkillError("Skill version is already installed")
            committed = False
            data_published = False
            runtime_published = False
            try:
                os.mkdir(data_stage, 0o700)
                _write_private(data_stage / "skill.json", manifest_raw)
                _write_private(data_stage / "main.py", source)
                subprocess.run(
                    [
                        os.fspath(self.capsule.interpreter), "-m", "venv",
                        "--without-pip", "--copies", os.fspath(runtime_stage),
                    ],
                    cwd=self.repository_root,
                    env={
                        "HOME": str(self.repository_root / ".runtime" / "home"),
                        "TMPDIR": str(self.repository_root / ".runtime" / "tmp"),
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                    check=True,
                )
                if data_target.exists():
                    os.rename(data_target, old_data)
                if runtime_target.exists():
                    os.rename(runtime_target, old_runtime)
                os.rename(data_stage, data_target)
                data_published = True
                os.rename(runtime_stage, runtime_target)
                runtime_published = True
                now = utc_now()
                installed = now if existing is None else existing["installed_at"]
                with self._connection:
                    self._connection.execute(
                        """INSERT INTO skills VALUES (?, ?, ?, ?, ?, '[]', ?, ?)
                           ON CONFLICT(skill_id) DO UPDATE SET
                           version=excluded.version, display_name=excluded.display_name,
                           package_digest=excluded.package_digest,
                           content_digest=excluded.content_digest,
                           capabilities_json=excluded.capabilities_json,
                           updated_at=excluded.updated_at""",
                        (
                            skill_id, version, manifest["display_name"], expected_digest,
                            content_digest, installed, now,
                        ),
                    )
                committed = True
                for old in (old_data, old_runtime):
                    if old.exists():
                        shutil.rmtree(old)
            except BaseException:
                if not committed:
                    for target, old in (
                        (data_target, old_data),
                        (runtime_target, old_runtime),
                    ):
                        if old.exists():
                            if target.exists():
                                shutil.rmtree(target)
                            os.rename(old, target)
                    if existing is None:
                        if data_published and data_target.exists():
                            shutil.rmtree(data_target)
                        if runtime_published and runtime_target.exists():
                            shutil.rmtree(runtime_target)
                for stage in (data_stage, runtime_stage):
                    if stage.exists():
                        shutil.rmtree(stage)
                raise
        return self.get(skill_id)

    def delete(self, skill_id: str) -> None:
        record = self.get(skill_id)
        del record
        with self._lock:
            if self._active.get(skill_id, 0):
                raise SkillError("Skill is currently executing")
            data_target = self.data_root / skill_id
            runtime_target = self.runtime_root / skill_id
            token = os.urandom(8).hex()
            data_stage = self.data_root / f".staging-delete-{token}"
            runtime_stage = self.runtime_root / f".staging-delete-{token}"
            for target in (data_target, runtime_target):
                metadata = os.lstat(target)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise SkillError("Skill root is unsafe")
            os.rename(data_target, data_stage)
            try:
                os.rename(runtime_target, runtime_stage)
            except BaseException:
                os.rename(data_stage, data_target)
                raise
            committed = False
            try:
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM skills WHERE skill_id=?", (skill_id,)
                    )
                committed = True
                shutil.rmtree(data_stage)
                shutil.rmtree(runtime_stage)
            except BaseException:
                if not committed:
                    if data_stage.exists():
                        os.rename(data_stage, data_target)
                    if runtime_stage.exists():
                        os.rename(runtime_stage, runtime_target)
                raise

    def acquire(self, skill_id: str) -> SkillRecord:
        with self._lock:
            record = self.get(skill_id)
            self._active[skill_id] = self._active.get(skill_id, 0) + 1
            return record

    def release(self, skill_id: str) -> None:
        with self._lock:
            count = self._active.get(skill_id, 0)
            if count <= 1:
                self._active.pop(skill_id, None)
            else:
                self._active[skill_id] = count - 1

    def close(self) -> None:
        with self._lock:
            if self._active:
                raise SkillError("Skill registry closed with active execution")
            self._connection.close()


class PreparedSkillExecutor:
    executor_kind = "skill-singleton-v1"

    def __init__(
        self,
        registry: SkillRegistry,
        skill_id: str,
        prepared: Mapping[str, object],
        delegate: object,
    ) -> None:
        self._registry = registry
        self._skill_id = skill_id
        self._prepared = dict(prepared)
        self._delegate = delegate
        self.identity_digest = getattr(delegate, "identity_digest")

    def execute(self, request: CapabilityRequest, cancelled: Callable[[], bool]) -> str:
        try:
            if json.loads(request.arguments_json) != self._prepared:
                raise SkillError("Skill request binding changed")
        except json.JSONDecodeError as exc:
            raise SkillError("Skill request is invalid") from exc
        record = self._registry.acquire(self._skill_id)
        try:
            package = self._registry.data_root / record.skill_id
            manifest = _read_private(package / "skill.json", 4 * 1024)
            source = _read_private(package / "main.py", MAX_SKILL_SOURCE_BYTES)
            if _content_digest(manifest, source) != record.content_digest:
                raise SkillError("Skill package changed after approval")
            return self._delegate.execute_prepared(cancelled)
        finally:
            self._registry.release(self._skill_id)


class SkillExecutor:
    def __init__(self, registry: SkillRegistry, commands: CommandExecutor) -> None:
        self.registry = registry
        self._commands = commands

    def prepare(self, arguments: Mapping[str, object], run_root: Path):
        if set(arguments) != {"skill_id", "input_json"}:
            raise SkillError("Skill arguments are invalid")
        skill_id = arguments.get("skill_id")
        input_json = arguments.get("input_json")
        if not isinstance(skill_id, str) or not isinstance(input_json, str):
            raise SkillError("Skill arguments are invalid")
        if len(input_json.encode("utf-8")) > MAX_SKILL_INPUT_BYTES:
            raise SkillError("Skill input exceeds its byte limit")
        try:
            input_value = json.loads(input_json)
        except json.JSONDecodeError as exc:
            raise SkillError("Skill input is invalid") from exc
        if not isinstance(input_value, dict):
            raise SkillError("Skill input must be an object")
        record = self.registry.get(skill_id)
        package = self.registry.data_root / skill_id
        manifest = _read_private(package / "skill.json", 4 * 1024)
        source = _read_private(package / "main.py", MAX_SKILL_SOURCE_BYTES)
        if _content_digest(manifest, source) != record.content_digest:
            raise SkillError("Skill package integrity changed")
        try:
            prepared, preview, delegate = self._commands.prepare_skill(
                skill_id=skill_id,
                skill_version=record.version,
                package_digest=record.package_digest,
                package_root=package,
                interpreter=self.registry.runtime_root / skill_id / "bin" / "python",
                input_value=input_value,
                run_root=run_root,
            )
        except CommandExecutionError as exc:
            raise SkillError("Skill execution preparation failed") from exc
        return prepared, preview, PreparedSkillExecutor(
            self.registry, skill_id, prepared, delegate
        )


__all__ = [
    "MAX_SKILLS",
    "SkillError",
    "SkillExecutor",
    "SkillRecord",
    "SkillRegistry",
    "inspect_skill_archive",
]

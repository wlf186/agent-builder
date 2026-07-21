"""Curated, persistent Agent research environment and document capability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
from typing import Callable, Mapping
import zipfile

from .capsule import AgentCapsule
from .command_exec import CommandExecutionError, CommandExecutor
from .file_read import FileReadError, validate_workspace_relative_path
from .permissions import CapabilityRequest


RESEARCH_ENVIRONMENT_ID = "research-documents"
RESEARCH_ENVIRONMENT_VERSION = "1"
RESEARCH_PACKAGE_ID = "7265736561726368646f63756d656e74"
RESEARCH_REQUIREMENTS = (
    "lxml==6.1.1",
    "pypdf==6.14.2",
    "python-docx==1.2.0",
    "typing-extensions==4.16.0",
)
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_DOCX_ENTRIES = 4_096
MAX_DOCX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ENVIRONMENT_METADATA_BYTES = 8 * 1024
INSTALL_TIMEOUT_SECONDS = 180
MAX_ENVIRONMENT_ENTRIES = 4_096
MAX_ENVIRONMENT_LOGICAL_BYTES = 256 * 1024 * 1024
MAX_ENVIRONMENT_ALLOCATED_BYTES = 512 * 1024 * 1024
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SUPPORTED_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md", ".html", ".htm"})


class ResearchEnvironmentError(RuntimeError):
    """The curated research environment failed a lifecycle or safety check."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchEnvironmentError("research metadata is invalid") from exc


def _source() -> bytes:
    path = Path(__file__).with_name("research_bundle.py")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > 64 * 1024
        ):
            raise ResearchEnvironmentError("research bundle source is unsafe")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size or os.read(descriptor, 1):
            raise ResearchEnvironmentError("research bundle source changed")
    finally:
        os.close(descriptor)
    try:
        compile(raw, "research_bundle.py", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise ResearchEnvironmentError("research bundle source is invalid") from exc
    return raw


def _source_digest(raw: bytes) -> str:
    return hashlib.sha256(b"agent-builder-research-source-v1\0" + raw).hexdigest()


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
                raise OSError("research metadata write failed")
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
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= maximum
        ):
            raise ResearchEnvironmentError("research metadata file is unsafe")
        raw = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ResearchEnvironmentError("research metadata changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> os.stat_result:
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ResearchEnvironmentError("research environment directory is unsafe")
    return metadata


def _remove_environment_tree(path: Path) -> None:
    root = _private_directory(path)
    pending = [path]
    entries = 0
    logical = 0
    allocated = 0
    while pending:
        directory = pending.pop()
        for child in os.scandir(directory):
            entries += 1
            if entries > MAX_ENVIRONMENT_ENTRIES:
                raise ResearchEnvironmentError(
                    "research environment cleanup entry limit exceeded"
                )
            metadata = child.stat(follow_symlinks=False)
            if metadata.st_uid != os.getuid() or metadata.st_dev != root.st_dev:
                raise ResearchEnvironmentError(
                    "research environment cleanup identity changed"
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(child.path))
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ResearchEnvironmentError(
                        "research environment cleanup found a hardlink"
                    )
                logical += metadata.st_size
                allocated += metadata.st_blocks * 512
            elif stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(child.path)
                candidate = Path(target)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ResearchEnvironmentError(
                        "research environment cleanup found an unsafe symlink"
                    )
            else:
                raise ResearchEnvironmentError(
                    "research environment cleanup found a special file"
                )
            if (
                logical > MAX_ENVIRONMENT_LOGICAL_BYTES
                or allocated > MAX_ENVIRONMENT_ALLOCATED_BYTES
            ):
                raise ResearchEnvironmentError(
                    "research environment cleanup byte limit exceeded"
                )
    shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_interpreter(root: Path) -> Path:
    _private_directory(root)
    interpreter = root / "bin" / "python"
    metadata = os.stat(interpreter, follow_symlinks=True)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(interpreter, os.X_OK)
        or not interpreter.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    ):
        raise ResearchEnvironmentError("research environment interpreter is unsafe")
    return interpreter


@dataclass(frozen=True, slots=True)
class ResearchEnvironmentRecord:
    environment_id: str
    version: str
    requirements: tuple[str, ...]
    source_digest: str
    installed_at: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "environment_id": self.environment_id,
            "version": self.version,
            "requirements": list(self.requirements),
            "source_digest": self.source_digest,
            "installed_at": self.installed_at,
            "reuse_scope": "agent-generation-across-conversations",
            "installer": "trusted-binary-only-uv-v1",
            "network_at_runtime": "denied",
        }


EnvironmentInstaller = Callable[[Path], tuple[str, ...]]


class ResearchEnvironmentManager:
    """Own one curated dependency environment inside exactly one Capsule."""

    def __init__(
        self,
        repository_root: Path,
        capsule: AgentCapsule,
        *,
        installer: EnvironmentInstaller | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.capsule = capsule
        self.data_parent = capsule.data_root / "dependencies"
        self.runtime_parent = capsule.runtime_root / "dependencies"
        self.data_root = self.data_parent / RESEARCH_ENVIRONMENT_ID
        self.runtime_root = self.runtime_parent / RESEARCH_ENVIRONMENT_ID
        self._installer = installer or self._install_environment
        self._lock = threading.RLock()
        self._active = 0
        _private_directory(self.data_parent)
        _private_directory(self.runtime_parent)
        self._cleanup_staging()
        self._recover_partial_install()

    def _cleanup_staging(self) -> None:
        for parent in (self.data_parent, self.runtime_parent):
            entries = list(parent.iterdir())
            if len(entries) > 16:
                raise ResearchEnvironmentError("research environment root exceeds its limit")
            for entry in entries:
                if not entry.name.startswith(".research-staging-"):
                    continue
                _remove_environment_tree(entry)

    def _recover_partial_install(self) -> None:
        data_exists = self.data_root.exists() or self.data_root.is_symlink()
        runtime_exists = self.runtime_root.exists() or self.runtime_root.is_symlink()
        if data_exists == runtime_exists:
            return
        orphan = self.data_root if data_exists else self.runtime_root
        _remove_environment_tree(orphan)
        _fsync_directory(orphan.parent)

    def _metadata(self) -> ResearchEnvironmentRecord | None:
        data_exists = self.data_root.exists() or self.data_root.is_symlink()
        runtime_exists = self.runtime_root.exists() or self.runtime_root.is_symlink()
        if not data_exists and not runtime_exists:
            return None
        if not data_exists or not runtime_exists:
            raise ResearchEnvironmentError("research environment is only partially installed")
        _private_directory(self.data_root)
        _private_directory(self.runtime_root)
        raw = _read_private(
            self.data_root / "environment.json", MAX_ENVIRONMENT_METADATA_BYTES
        )
        source = _read_private(self.data_root / "main.py", 64 * 1024)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchEnvironmentError("research metadata is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {
                "schema_version",
                "environment_id",
                "version",
                "requirements",
                "source_digest",
                "installed_at",
            }
            or value.get("schema_version") != 1
            or value.get("environment_id") != RESEARCH_ENVIRONMENT_ID
            or value.get("version") != RESEARCH_ENVIRONMENT_VERSION
            or value.get("requirements") != list(RESEARCH_REQUIREMENTS)
            or not isinstance(value.get("source_digest"), str)
            or _DIGEST.fullmatch(value["source_digest"]) is None
            or value["source_digest"] != _source_digest(source)
            or not isinstance(value.get("installed_at"), str)
            or not value["installed_at"]
        ):
            raise ResearchEnvironmentError("research metadata identity changed")
        _safe_interpreter(self.runtime_root)
        return ResearchEnvironmentRecord(
            RESEARCH_ENVIRONMENT_ID,
            RESEARCH_ENVIRONMENT_VERSION,
            RESEARCH_REQUIREMENTS,
            value["source_digest"],
            value["installed_at"],
        )

    def status(self) -> ResearchEnvironmentRecord | None:
        with self._lock:
            return self._metadata()

    def _install_environment(self, target: Path) -> tuple[str, ...]:
        uv = self.repository_root / ".tools" / "uv"
        uv_metadata = os.stat(uv, follow_symlinks=False)
        if (
            not stat.S_ISREG(uv_metadata.st_mode)
            or uv_metadata.st_uid != os.getuid()
            or not stat.S_IMODE(uv_metadata.st_mode) & 0o111
        ):
            raise ResearchEnvironmentError("checkout-local uv is unavailable")
        environment = {
            "HOME": str(self.repository_root / ".runtime" / "home"),
            "TMPDIR": str(self.repository_root / ".runtime" / "tmp"),
            "XDG_CACHE_HOME": str(self.repository_root / ".runtime" / "cache"),
            "XDG_CONFIG_HOME": str(self.repository_root / ".runtime" / "config"),
            "XDG_DATA_HOME": str(self.repository_root / ".runtime" / "share"),
            "UV_CACHE_DIR": str(self.repository_root / ".runtime" / "cache" / "uv"),
            "UV_LINK_MODE": "copy",
            "UV_NO_PROGRESS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PATH": str(self.repository_root / ".tools") + ":/usr/bin:/bin",
        }
        try:
            subprocess.run(
                [
                    os.fspath(self.capsule.interpreter),
                    "-m",
                    "venv",
                    "--without-pip",
                    "--copies",
                    os.fspath(target),
                ],
                cwd=self.repository_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=True,
            )
            os.chmod(target, 0o700, follow_symlinks=False)
            subprocess.run(
                [
                    os.fspath(uv),
                    "pip",
                    "install",
                    "--python",
                    os.fspath(target / "bin" / "python"),
                    "--only-binary",
                    ":all:",
                    "--no-deps",
                    *RESEARCH_REQUIREMENTS,
                ],
                cwd=self.repository_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=INSTALL_TIMEOUT_SECONDS,
                check=True,
            )
            completed = subprocess.run(
                [
                    os.fspath(target / "bin" / "python"),
                    "-I",
                    "-c",
                    (
                        "import importlib.metadata as m, json; "
                        "import docx, lxml, pypdf, typing_extensions; "
                        "print(json.dumps([m.version(x) for x in "
                        "['lxml','pypdf','python-docx','typing-extensions']]))"
                    ),
                ],
                cwd=self.repository_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ResearchEnvironmentError(
                "research dependency installation failed closed"
            ) from exc
        if len(completed.stdout) > 1_024:
            raise ResearchEnvironmentError("research dependency verification overflowed")
        try:
            versions = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchEnvironmentError(
                "research dependency verification failed"
            ) from exc
        expected_versions = [item.split("==", 1)[1] for item in RESEARCH_REQUIREMENTS]
        if versions != expected_versions:
            raise ResearchEnvironmentError("research dependency versions changed")
        _safe_interpreter(target)
        return RESEARCH_REQUIREMENTS

    def install(self) -> ResearchEnvironmentRecord:
        with self._lock:
            existing = self._metadata()
            if existing is not None:
                return existing
            token = os.urandom(16).hex()
            data_stage = self.data_parent / f".research-staging-{token}"
            runtime_stage = self.runtime_parent / f".research-staging-{token}"
            source = _source()
            installed_at = _now()
            published_data = False
            published_runtime = False
            try:
                os.mkdir(data_stage, 0o700)
                _write_private(data_stage / "main.py", source)
                metadata = _canonical(
                    {
                        "schema_version": 1,
                        "environment_id": RESEARCH_ENVIRONMENT_ID,
                        "version": RESEARCH_ENVIRONMENT_VERSION,
                        "requirements": list(RESEARCH_REQUIREMENTS),
                        "source_digest": _source_digest(source),
                        "installed_at": installed_at,
                    }
                )
                _write_private(data_stage / "environment.json", metadata)
                installed = self._installer(runtime_stage)
                if installed != RESEARCH_REQUIREMENTS:
                    raise ResearchEnvironmentError(
                        "research installer returned an unexpected package set"
                    )
                os.rename(data_stage, self.data_root)
                published_data = True
                _fsync_directory(self.data_parent)
                os.rename(runtime_stage, self.runtime_root)
                published_runtime = True
                _fsync_directory(self.runtime_parent)
            except BaseException:
                for stage in (data_stage, runtime_stage):
                    if stage.exists():
                        _remove_environment_tree(stage)
                if published_runtime and self.runtime_root.exists():
                    _remove_environment_tree(self.runtime_root)
                if published_data and self.data_root.exists():
                    _remove_environment_tree(self.data_root)
                raise
            return self._metadata() or ResearchEnvironmentRecord(
                RESEARCH_ENVIRONMENT_ID,
                RESEARCH_ENVIRONMENT_VERSION,
                RESEARCH_REQUIREMENTS,
                _source_digest(source),
                installed_at,
            )

    def delete(self) -> None:
        with self._lock:
            if self._metadata() is None:
                return
            if self._active:
                raise ResearchEnvironmentError("research environment is executing")
            token = os.urandom(16).hex()
            data_stage = self.data_parent / f".research-staging-delete-{token}"
            runtime_stage = self.runtime_parent / f".research-staging-delete-{token}"
            os.rename(self.data_root, data_stage)
            try:
                os.rename(self.runtime_root, runtime_stage)
            except BaseException:
                os.rename(data_stage, self.data_root)
                raise
            try:
                _remove_environment_tree(runtime_stage)
                _fsync_directory(self.runtime_parent)
                _remove_environment_tree(data_stage)
                _fsync_directory(self.data_parent)
            except BaseException:
                # A failed physical cleanup is explicit.  Staging is retained
                # for bounded startup recovery rather than silently re-enabled.
                raise

    def acquire(self) -> ResearchEnvironmentRecord:
        with self._lock:
            record = self._metadata()
            if record is None:
                raise ResearchEnvironmentError("research environment is not installed")
            self._active += 1
            return record

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
    )


def _capture_document(capsule: AgentCapsule, relative_path: object) -> tuple[str, bytes, str]:
    try:
        path, parts = validate_workspace_relative_path(relative_path)
    except FileReadError as exc:
        raise ResearchEnvironmentError("document path is invalid") from exc
    if Path(path).suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ResearchEnvironmentError("document type is not supported")
    workspace = capsule.data_root / "workspace"
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        raise ResearchEnvironmentError("descriptor-anchored document reads are unavailable")
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        named_root = os.lstat(workspace)
        root_descriptor = os.open(
            workspace,
            os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
        )
        descriptors.append(root_descriptor)
        opened_root = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or opened_root.st_uid != os.getuid()
            or stat.S_IMODE(opened_root.st_mode) & 0o022
            or _identity(opened_root) != _identity(named_root)
        ):
            raise ResearchEnvironmentError("workspace changed during document read")
        current = root_descriptor
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
                dir_fd=current,
            )
            descriptors.append(child)
            child_metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or child_metadata.st_uid != os.getuid()
                or stat.S_IMODE(child_metadata.st_mode) & 0o022
                or child_metadata.st_dev != opened_root.st_dev
            ):
                raise ResearchEnvironmentError("document parent is unsafe")
            current = child
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=current,
        )
        before = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_dev != opened_root.st_dev
            or not 1 <= before.st_size <= MAX_DOCUMENT_BYTES
            or before.st_blocks * 512 < before.st_size
        ):
            raise ResearchEnvironmentError("document file is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(file_descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file_descriptor)
        named_file = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or os.read(file_descriptor, 1)
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(named_file)
            or _identity(opened_root) != _identity(os.fstat(root_descriptor))
            or _identity(opened_root) != _identity(os.lstat(workspace))
        ):
            raise ResearchEnvironmentError("document changed while reading")
    except OSError as exc:
        raise ResearchEnvironmentError("document could not be read safely") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf" and not raw.startswith(b"%PDF-"):
        raise ResearchEnvironmentError("PDF signature is invalid")
    if suffix == ".docx":
        _validate_docx(raw)
    if suffix in {".txt", ".md", ".html", ".htm"}:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResearchEnvironmentError("text document is not UTF-8") from exc
    return path, raw, hashlib.sha256(raw).hexdigest()


def _validate_docx(raw: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ResearchEnvironmentError("DOCX archive is invalid") from exc
    if not 1 <= len(entries) <= MAX_DOCX_ENTRIES:
        raise ResearchEnvironmentError("DOCX entry count exceeds its limit")
    expanded = 0
    names: set[str] = set()
    for entry in entries:
        candidate = Path(entry.filename)
        mode = (entry.external_attr >> 16) & 0xFFFF
        expanded += entry.file_size
        if (
            entry.filename in names
            or entry.flag_bits & 0x1
            or entry.file_size < 0
            or expanded > MAX_DOCX_EXPANDED_BYTES
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in entry.filename
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
        ):
            raise ResearchEnvironmentError("DOCX archive contains an unsafe entry")
        names.add(entry.filename)
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise ResearchEnvironmentError("DOCX document parts are incomplete")


def _stage_document(run_root: Path, raw: bytes) -> Path:
    work = run_root / "work"
    _private_directory(work)
    path = work / f"research-input-{os.urandom(16).hex()}.bin"
    _write_private(path, raw)
    return path


class PreparedResearchDocumentExecutor:
    executor_kind = "research-document-singleton-v1"

    def __init__(
        self,
        manager: ResearchEnvironmentManager,
        delegate: object,
        staged_path: Path,
    ) -> None:
        self._manager = manager
        self._delegate = delegate
        self._staged_path = staged_path
        self.identity_digest = hashlib.sha256(
            b"agent-builder-research-executor-v1\0"
            + str(getattr(delegate, "identity_digest")).encode("ascii")
        ).hexdigest()

    def execute(self, request: CapabilityRequest, cancelled: Callable[[], bool]) -> str:
        acquired = False
        try:
            self._manager.acquire()
            acquired = True
            execute = getattr(self._delegate, "execute")
            outer_raw = execute(request, cancelled)
            outer = json.loads(outer_raw)
            if (
                not isinstance(outer, dict)
                or outer.get("kind") != "command_result"
                or outer.get("command_id") != "skill-run"
                or outer.get("exit_code") != 0
                or outer.get("timed_out") is not False
                or outer.get("cancelled") is not False
                or not isinstance(outer.get("stdout"), str)
            ):
                raise ResearchEnvironmentError("document parser failed closed")
            stdout = outer["stdout"].strip()
            value = json.loads(stdout)
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != 1
                or value.get("kind") != "document_text"
                or not isinstance(value.get("content"), str)
                or len(stdout.encode("utf-8")) > 12 * 1024
            ):
                raise ResearchEnvironmentError("document parser result is invalid")
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (CommandExecutionError, json.JSONDecodeError) as exc:
            raise ResearchEnvironmentError("document parser failed closed") from exc
        finally:
            if acquired:
                self._manager.release()
            try:
                self._staged_path.unlink()
            except FileNotFoundError:
                pass


class ResearchDocumentExecutor:
    def __init__(
        self,
        manager: ResearchEnvironmentManager,
        commands: CommandExecutor,
    ) -> None:
        self.manager = manager
        self._commands = commands

    def prepare(
        self,
        arguments: Mapping[str, object],
        run_root: Path,
    ) -> tuple[dict[str, object], str, PreparedResearchDocumentExecutor]:
        if not set(arguments).issubset({"path", "offset_chars", "max_chars"}) or "path" not in arguments:
            raise ResearchEnvironmentError("document extraction arguments are invalid")
        offset = arguments.get("offset_chars", 0)
        maximum = arguments.get("max_chars", 4_096)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or not 0 <= offset <= 1_000_000
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or not 1 <= maximum <= 4_096
        ):
            raise ResearchEnvironmentError("document extraction limits are invalid")
        record = self.manager.status()
        if record is None:
            raise ResearchEnvironmentError("research environment is not installed")
        path, raw, content_digest = _capture_document(
            self.manager.capsule, arguments.get("path")
        )
        staged = _stage_document(run_root, raw)
        input_value = {
            "original_path": path,
            "staged_name": staged.name,
            "offset_chars": offset,
            "max_chars": maximum,
            "content_digest": content_digest,
        }
        try:
            prepared, _skill_preview, delegate = self._commands.prepare_skill(
                skill_id=RESEARCH_PACKAGE_ID,
                skill_version=record.version,
                package_digest=record.source_digest,
                package_root=self.manager.data_root,
                interpreter=self.manager.runtime_root / "bin" / "python",
                input_value=input_value,
                run_root=run_root,
                package_namespace="dependencies",
                environment_id=RESEARCH_ENVIRONMENT_ID,
            )
        except CommandExecutionError as exc:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            raise ResearchEnvironmentError(
                "document parser preparation failed closed"
            ) from exc
        except BaseException:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            raise
        preview = json.dumps(
            {
                "action": "document/extract_text",
                "path": path,
                "content_digest": content_digest,
                "offset_chars": offset,
                "max_chars": maximum,
                "environment_id": record.environment_id,
                "requirements": list(record.requirements),
                "network": "denied",
                "write_scope": "transient-run-work",
                "sandbox": "singleton-landlock-seccomp-v1",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return prepared, preview, PreparedResearchDocumentExecutor(
            self.manager, delegate, staged
        )


__all__ = [
    "MAX_DOCUMENT_BYTES",
    "RESEARCH_ENVIRONMENT_ID",
    "RESEARCH_REQUIREMENTS",
    "ResearchDocumentExecutor",
    "ResearchEnvironmentError",
    "ResearchEnvironmentManager",
    "ResearchEnvironmentRecord",
]

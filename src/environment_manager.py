"""Project-local Python runtime management powered by uv.

The manager keeps interpreters, caches, virtual environments, and metadata under
the repository root.  It intentionally does not discover or modify user-level
Python installations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple

from .models import AgentEnvironment, EnvironmentStatus, EnvironmentType
from .process_sandbox import (
    apply_skill_sandbox,
    landlock_abi,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(
    os.environ.get("AGENT_BUILDER_RUNTIME_DIR", PROJECT_ROOT / ".runtime")
).resolve()
LOCAL_UV = PROJECT_ROOT / ".tools" / "uv"
PACKAGE_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?"
    r"(?:\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9*+!._-]+)?"
    r"(?:\s*;\s*[A-Za-z0-9_ .<>=!\"'()-]+)?$"
)
MAX_PACKAGES_PER_REQUEST = 64
DEFAULT_ALLOWED_PACKAGES = {
    "aiofiles", "chromadb", "cryptography", "httpx", "langchain",
    "langchain-core", "langchain-ollama", "langchain-openai",
    "langchain-text-splitters", "langgraph", "lxml", "mcp", "openpyxl",
    "pandas", "pdfplumber", "pillow", "pypdf", "pypdf2", "pypdfium2",
    "python-docx", "reportlab", "defusedxml",
}


def get_uv_path() -> Optional[str]:
    """Return a uv executable contained by this project, if available."""
    configured = os.environ.get("AGENT_BUILDER_UV")
    candidates = [Path(configured)] if configured else []
    candidates.append(LOCAL_UV)
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    return None


class EnvironmentError(Exception):
    """Raised when a managed Python environment operation fails."""


class _AgentOperationGate:
    """Small writer-preferring async gate for one Agent's environment.

    Skill processes are readers: several may execute concurrently.  Environment
    creation, package mutation and deletion are writers: they must not change an
    interpreter while a Skill is using it.  Waiting writers block new readers so
    a busy Agent cannot starve lifecycle operations indefinitely.
    """

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.active_readers = 0
        self.waiting_writers = 0
        self.writer_active = False


class EnvironmentManager:
    """Manage one project-local uv virtual environment per agent."""

    ENV_PREFIX = "env_"
    MAX_CAPTURE_BYTES = int(
        os.environ.get("AGENT_BUILDER_EXECUTION_OUTPUT_LIMIT", str(1024 * 1024))
    )
    MAX_METADATA_BYTES = 1024 * 1024
    MAX_ERROR_MESSAGE_CHARS = 2_000
    MAX_WORKDIR_ENTRIES = 10_000
    MAX_EXECUTION_PROCESSES = 64
    MAX_EXECUTION_AGGREGATE_MEMORY = 16 * 1024**3
    MAX_EXECUTION_FILE_SIZE = 1024**3
    MAX_EXECUTION_WORKDIR_SIZE = 2 * 1024**3
    PROCESS_GROUP_POLL_INTERVAL = 0.05

    def __init__(self, data_dir: Path, environments_dir: Path):
        raw_data_dir = Path(data_dir)
        raw_environments_dir = Path(environments_dir)
        if raw_data_dir.is_symlink() or raw_environments_dir.is_symlink():
            raise EnvironmentError("受管运行目录不能是软链接")
        self.data_dir = raw_data_dir.resolve()
        self.environments_dir = raw_environments_dir.resolve()
        for label, path in (
            ("data_dir", self.data_dir),
            ("environments_dir", self.environments_dir),
        ):
            try:
                path.relative_to(PROJECT_ROOT)
            except ValueError as exc:
                raise EnvironmentError(
                    f"{label} 必须位于项目目录内: {path}"
                ) from exc
        self.metadata_dir = self.data_dir / "environments"
        self._running_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._running_process_agents: Dict[str, str] = {}
        self._uv_processes: set[asyncio.subprocess.Process] = set()
        self._agent_processes: Dict[str, set[asyncio.subprocess.Process]] = {}
        self._agent_uv_processes: Dict[str, set[asyncio.subprocess.Process]] = {}
        self._operation_gates: Dict[str, _AgentOperationGate] = {}
        self._ensure_dirs()

    def _validate_managed_roots(self) -> None:
        for label, path in (
            ("data_dir", self.data_dir),
            ("environments_dir", self.environments_dir),
            ("metadata_dir", self.metadata_dir),
        ):
            if path.is_symlink():
                raise EnvironmentError(f"{label} 不能是软链接")
            try:
                path.resolve(strict=False).relative_to(PROJECT_ROOT)
            except ValueError as exc:
                raise EnvironmentError(f"{label} 必须位于项目目录内") from exc

    def _validate_agent_paths(self, agent_name: str) -> Tuple[Path, Path]:
        """Reject planted per-Agent root/metadata links before any I/O."""
        self._validate_managed_roots()
        env_path = self.get_env_path(agent_name)
        metadata_path = self.get_metadata_path(agent_name)
        for label, path, root in (
            ("Agent 环境目录", env_path, self.environments_dir),
            ("Agent 环境元数据", metadata_path, self.metadata_dir),
        ):
            if path.is_symlink():
                raise EnvironmentError(f"{label}不能是软链接")
            try:
                path.resolve(strict=False).relative_to(root.resolve(strict=False))
            except ValueError as exc:
                raise EnvironmentError(f"{label}超出受管目录") from exc
        return env_path, metadata_path

    @staticmethod
    def _validate_environment_executable(env_path: Path, candidate: Path) -> Path:
        """Require a venv entry point to resolve to a project-managed file.

        uv normally creates ``bin/python`` as a link into
        ``.runtime/python``.  That contained link is expected, while a planted
        link to a system or user-home interpreter would silently defeat the
        project-local dependency boundary.
        """
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise EnvironmentError("受管环境解释器或命令不存在") from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise EnvironmentError("受管环境解释器或命令不可执行")

        allowed_roots = (
            env_path.resolve(strict=True),
            (RUNTIME_ROOT / "python").resolve(strict=False),
        )
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            raise EnvironmentError("受管环境命令指向项目目录之外")
        return candidate

    def _operation_gate(self, agent_name: str) -> _AgentOperationGate:
        return self._operation_gates.setdefault(agent_name, _AgentOperationGate())

    @asynccontextmanager
    async def _shared_agent_operation(
        self, agent_name: str
    ) -> AsyncIterator[None]:
        """Protect an execution/read operation from lifecycle mutation."""
        gate = self._operation_gate(agent_name)
        async with gate.condition:
            await gate.condition.wait_for(
                lambda: not gate.writer_active and gate.waiting_writers == 0
            )
            gate.active_readers += 1
        try:
            yield
        finally:
            async with gate.condition:
                gate.active_readers -= 1
                gate.condition.notify_all()

    @asynccontextmanager
    async def _exclusive_agent_operation(
        self,
        agent_name: str,
        *,
        cancel_running: bool = False,
    ) -> AsyncIterator[None]:
        """Serialize interpreter mutations, optionally stopping active work."""
        gate = self._operation_gate(agent_name)
        registered = False
        acquired = False
        async with gate.condition:
            gate.waiting_writers += 1
            registered = True
            gate.condition.notify_all()
        try:
            # Marking a writer as waiting above prevents a new Skill process from
            # entering while deletion is terminating the current process set.
            if cancel_running:
                # A reader can have acquired its lease just before deletion and
                # still be between validation and subprocess registration. Poll
                # the tracked set until every such reader has released its lease.
                while True:
                    await self.cancel_agent_processes(agent_name)
                    async with gate.condition:
                        if not gate.writer_active and gate.active_readers == 0:
                            gate.waiting_writers -= 1
                            registered = False
                            gate.writer_active = True
                            acquired = True
                            break
                    await asyncio.sleep(0.01)
            else:
                async with gate.condition:
                    await gate.condition.wait_for(
                        lambda: not gate.writer_active and gate.active_readers == 0
                    )
                    gate.waiting_writers -= 1
                    registered = False
                    gate.writer_active = True
                    acquired = True
            yield
        finally:
            async with gate.condition:
                if registered:
                    gate.waiting_writers -= 1
                if acquired:
                    gate.writer_active = False
                gate.condition.notify_all()

    @staticmethod
    async def check_runtime_available() -> Dict[str, object]:
        """Return availability and version information for the local runtime."""
        uv_exe = get_uv_path()
        if not uv_exe:
            return {
                "available": False,
                "path": None,
                "version": None,
                "error": "UV_NOT_FOUND",
                "message": "未找到项目内 uv，请先运行 ./bootstrap.sh",
            }

        process: Optional[asyncio.subprocess.Process] = None
        try:
            process = await asyncio.create_subprocess_exec(
                uv_exe,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=EnvironmentManager._runtime_env(),
                start_new_session=(os.name != "nt"),
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            if process is not None:
                await EnvironmentManager._terminate_process(process)
            return {
                "available": False,
                "path": uv_exe,
                "version": None,
                "error": "UV_TIMEOUT",
                "message": "uv 命令执行超时",
            }
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(EnvironmentManager._terminate_process(process))
            raise
        except Exception as exc:
            if process is not None and process.returncode is None:
                await EnvironmentManager._terminate_process(process)
            return {
                "available": False,
                "path": uv_exe,
                "version": None,
                "error": "UV_ERROR",
                "message": f"uv 检测失败 ({type(exc).__name__})",
            }

        assert process is not None
        if process.returncode == 0:
            return {
                "available": True,
                "path": uv_exe,
                "version": stdout.decode("utf-8", errors="replace").strip(),
                "error": None,
                "message": "uv 运行环境正常",
            }
        return {
            "available": False,
            "path": uv_exe,
            "version": None,
            "error": "UV_EXECUTION_FAILED",
            "message": "uv 执行失败",
        }

    @staticmethod
    def _runtime_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        exact = {
            "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY",
            "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
            "https_proxy", "all_proxy", "no_proxy",
        }
        env = {key: value for key, value in os.environ.items() if key in exact}
        runtime = RUNTIME_ROOT
        paths = {
            "AGENT_BUILDER_RUNTIME_DIR": runtime,
            "HOME": runtime / "home",
            "TMPDIR": runtime / "tmp",
            "TEMP": runtime / "tmp",
            "TMP": runtime / "tmp",
            "XDG_CACHE_HOME": runtime / "cache",
            "XDG_CONFIG_HOME": runtime / "config",
            "XDG_DATA_HOME": runtime / "share",
            "XDG_STATE_HOME": runtime / "state",
            "XDG_RUNTIME_DIR": runtime / "xdg-runtime",
            "UV_CACHE_DIR": runtime / "cache" / "uv",
            "UV_PYTHON_INSTALL_DIR": runtime / "python",
            "UV_TOOL_DIR": runtime / "tools",
            "PIP_CACHE_DIR": runtime / "cache" / "pip",
            "HF_HOME": runtime / "cache" / "huggingface",
            "HUGGINGFACE_HUB_CACHE": runtime / "cache" / "huggingface" / "hub",
            "SENTENCE_TRANSFORMERS_HOME": runtime / "cache" / "huggingface" / "sentence-transformers",
            "TORCH_HOME": runtime / "cache" / "torch",
            "TORCH_EXTENSIONS_DIR": runtime / "cache" / "torch" / "extensions",
            "TORCHINDUCTOR_CACHE_DIR": runtime / "cache" / "torch" / "inductor",
            "TRITON_CACHE_DIR": runtime / "cache" / "triton",
            "NUMBA_CACHE_DIR": runtime / "cache" / "numba",
            "TRANSFORMERS_CACHE": runtime / "cache" / "huggingface" / "transformers",
            "PLAYWRIGHT_BROWSERS_PATH": runtime / "cache" / "playwright",
            "MPLCONFIGDIR": runtime / "config" / "matplotlib",
            "PYTHONPYCACHEPREFIX": runtime / "cache" / "pycache",
        }
        for key, path in paths.items():
            Path(path).mkdir(parents=True, exist_ok=True)
            if key == "XDG_RUNTIME_DIR":
                os.chmod(path, 0o700)
            env[key] = str(path)
        env["PYTHONNOUSERSITE"] = "1"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["DO_NOT_TRACK"] = "1"
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        env.pop("AGENT_BUILDER_API_TOKEN", None)
        return env

    @staticmethod
    def _execution_env(cwd: Optional[str], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Build a minimal environment for untrusted Skill processes."""
        source = os.environ
        safe_names = {
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
        }
        env = {
            key: value
            for key, value in source.items()
            if key in safe_names or key.startswith("LC_")
        }
        sandbox_root = Path(cwd).resolve() if cwd else RUNTIME_ROOT / "tmp" / "executions"
        home = sandbox_root / ".home"
        temporary = sandbox_root / ".tmp"
        cache = sandbox_root / ".cache"
        config = sandbox_root / ".config"
        for path in (home, temporary, cache, config):
            path.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "XDG_CACHE_HOME": str(cache),
                "XDG_CONFIG_HOME": str(config),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "XDG_RUNTIME_DIR": str(sandbox_root / ".xdg-runtime"),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "HF_HOME": str(cache / "huggingface"),
                "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface" / "hub"),
                "SENTENCE_TRANSFORMERS_HOME": str(cache / "huggingface" / "sentence-transformers"),
                "TRANSFORMERS_CACHE": str(cache / "huggingface" / "transformers"),
                "TORCH_HOME": str(cache / "torch"),
                "TORCH_EXTENSIONS_DIR": str(cache / "torch" / "extensions"),
                "TORCHINDUCTOR_CACHE_DIR": str(cache / "torch" / "inductor"),
                "TRITON_CACHE_DIR": str(cache / "triton"),
                "NUMBA_CACHE_DIR": str(cache / "numba"),
                "MPLCONFIGDIR": str(config / "matplotlib"),
                "AGENT_BUILDER_RUNTIME_DIR": str(RUNTIME_ROOT),
            }
        )
        Path(env["XDG_RUNTIME_DIR"]).mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(env["XDG_RUNTIME_DIR"], 0o700)
        if extra:
            for key, value in extra.items():
                key_text, value_text = str(key), str(value)
                if (
                    key_text == "AGENT_BUILDER_API_TOKEN"
                    or "\x00" in key_text
                    or "\x00" in value_text
                ):
                    raise EnvironmentError(f"不允许的执行环境变量: {key_text}")
                env[key_text] = value_text
        return env

    @staticmethod
    def _configured_limit(
        name: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        raw_value = os.environ.get(name, str(default))
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise EnvironmentError(f"{name} 必须是整数") from exc
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _resource_limit_callback(
        timeout: int,
        work_directory: Path,
        environment_directory: Path,
        additional_readable_paths: Tuple[Path, ...] = (),
    ):
        """Return a child hook applying resource, filesystem and network limits."""
        if os.name == "nt":
            return None

        allow_network = (
            os.environ.get("AGENT_BUILDER_SKILL_NETWORK", "deny").strip().lower()
            == "allow"
        )
        address_space = EnvironmentManager._configured_limit(
            "AGENT_BUILDER_EXECUTION_MEMORY_LIMIT",
            4 * 1024**3,
            minimum=64 * 1024**2,
            maximum=EnvironmentManager.MAX_EXECUTION_AGGREGATE_MEMORY,
        )
        file_size = EnvironmentManager._configured_limit(
            "AGENT_BUILDER_EXECUTION_FILE_LIMIT",
            100 * 1024**2,
            minimum=1024**2,
            maximum=EnvironmentManager.MAX_EXECUTION_FILE_SIZE,
        )

        def apply_limits() -> None:
            import resource

            cpu_seconds = max(1, min(int(timeout) + 5, 305))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            apply_skill_sandbox(
                work_directory=work_directory,
                environment_directory=environment_directory,
                runtime_root=RUNTIME_ROOT,
                allow_network=allow_network,
                additional_readable_paths=additional_readable_paths,
            )

        return apply_limits

    @staticmethod
    def _directory_exceeds_limit(path: Path, limit: int) -> bool:
        total = 0
        entries = 0
        try:
            for root, directories, files in os.walk(path):
                directories[:] = [name for name in directories if not Path(root, name).is_symlink()]
                entries += len(directories) + len(files)
                if entries > EnvironmentManager.MAX_WORKDIR_ENTRIES:
                    return True
                for name in files:
                    candidate = Path(root, name)
                    if candidate.is_symlink():
                        continue
                    total += candidate.stat().st_size
                    if total > limit:
                        return True
        except (FileNotFoundError, PermissionError, OSError):
            return False
        return False

    @classmethod
    async def _watch_directory_quota(
        cls,
        process: asyncio.subprocess.Process,
        path: Path,
        limit: int,
    ) -> bool:
        while process.returncode is None:
            if await asyncio.to_thread(cls._directory_exceeds_limit, path, limit):
                return True
            # Directory walks are metadata-heavy. Once per second still bounds
            # runaway writes promptly without pointlessly churning the SSD.
            await asyncio.sleep(1.0)
        return False

    @staticmethod
    def _process_group_usage(process_group_id: int) -> Tuple[int, int]:
        """Return process count and aggregate RSS for one Linux process group.

        ``/proc`` is a virtual filesystem, so this polling does not write to or
        wear persistent storage.  RSS deliberately counts shared pages once per
        process, making the aggregate limit conservative.
        """
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        process_count = 0
        aggregate_rss = 0
        try:
            entries = os.scandir("/proc")
        except OSError as exc:
            raise EnvironmentError("无法读取 /proc 以监控 Skill 进程组") from exc
        with entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    stat_text = Path(entry.path, "stat").read_text(
                        encoding="utf-8"
                    )
                    # comm may contain spaces and closing parentheses; the last
                    # ')' terminates field 2.  pgrp and rss are fields 5 and 24.
                    closing = stat_text.rfind(")")
                    fields = stat_text[closing + 2 :].split()
                    if closing < 0 or len(fields) < 22:
                        continue
                    if int(fields[2]) != process_group_id:
                        continue
                    process_count += 1
                    aggregate_rss += max(0, int(fields[21])) * page_size
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    # Processes can exit between scandir and reading stat.
                    continue
                except (OSError, ValueError):
                    continue
        return process_count, aggregate_rss

    @classmethod
    async def _watch_process_group_quota(
        cls,
        process: asyncio.subprocess.Process,
        process_limit: int,
        aggregate_memory_limit: int,
    ) -> Optional[Tuple[str, int]]:
        """Watch a sandbox process group until exit or a hard quota violation."""
        while process.returncode is None:
            try:
                process_count, aggregate_rss = await asyncio.to_thread(
                    cls._process_group_usage, process.pid
                )
            except EnvironmentError:
                return "monitor", 0
            if process_count > process_limit:
                return "processes", process_count
            if aggregate_rss > aggregate_memory_limit:
                return "memory", aggregate_rss
            await asyncio.sleep(cls.PROCESS_GROUP_POLL_INTERVAL)
        return None

    def _ensure_dirs(self) -> None:
        self._validate_managed_roots()
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.environments_dir.mkdir(parents=True, exist_ok=True)
        self._validate_managed_roots()

    @staticmethod
    def _safe_name(agent_name: str) -> str:
        readable = re.sub(r"[^A-Za-z0-9._-]+", "_", agent_name).strip("._-")
        readable = (readable or "agent")[:48]
        digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()[:10]
        return f"{readable}_{digest}"

    def get_env_path(self, agent_name: str) -> Path:
        return self.environments_dir / f"{self.ENV_PREFIX}{self._safe_name(agent_name)}"

    def get_metadata_path(self, agent_name: str) -> Path:
        return self.metadata_dir / f"{self._safe_name(agent_name)}.json"

    @staticmethod
    def _validate_packages(packages: List[str]) -> List[str]:
        if not packages:
            return []
        if len(packages) > MAX_PACKAGES_PER_REQUEST:
            raise EnvironmentError(
                f"单次最多允许安装 {MAX_PACKAGES_PER_REQUEST} 个包"
            )
        validated: List[str] = []
        configured = os.environ.get("AGENT_BUILDER_PACKAGE_ALLOWLIST", "")
        allowed = DEFAULT_ALLOWED_PACKAGES | {
            re.sub(r"[-_.]+", "-", name.strip().lower())
            for name in configured.split(",")
            if name.strip()
        }
        for raw_spec in packages:
            spec = raw_spec.strip()
            if not spec or not PACKAGE_SPEC_RE.fullmatch(spec):
                raise EnvironmentError(
                    "不允许的包规格；仅允许受控 PyPI 名称和版本约束"
                )
            name_match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", spec)
            normalized_name = re.sub(
                r"[-_.]+", "-", name_match.group(0).lower() if name_match else ""
            )
            if normalized_name not in allowed:
                raise EnvironmentError(
                    f"包 {normalized_name!r} 不在 AGENT_BUILDER_PACKAGE_ALLOWLIST 中"
                )
            validated.append(spec)
        return validated

    async def _run_uv_command(
        self,
        args: List[str],
        timeout: int = 300,
        env: Optional[Dict[str, str]] = None,
        agent_name: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        uv_exe = get_uv_path()
        if not uv_exe:
            raise EnvironmentError("uv 不可用，请先运行 ./bootstrap.sh")
        process: Optional[asyncio.subprocess.Process] = None
        try:
            process = await asyncio.create_subprocess_exec(
                uv_exe,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._runtime_env(env),
                start_new_session=(os.name != "nt"),
            )
            self._uv_processes.add(process)
            if agent_name:
                self._agent_uv_processes.setdefault(agent_name, set()).add(process)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            if process is not None:
                await self._terminate_process(process)
            raise EnvironmentError(f"uv 命令超时: {' '.join(args[:3])}") from exc
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(self._terminate_process(process))
            raise
        finally:
            if process is not None:
                self._uv_processes.discard(process)
                if agent_name:
                    processes = self._agent_uv_processes.get(agent_name)
                    if processes is not None:
                        processes.discard(process)
                        if not processes:
                            self._agent_uv_processes.pop(agent_name, None)
        assert process is not None
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def create_environment(
        self, agent_name: str, python_version: str = "3.11"
    ) -> AgentEnvironment:
        async with self._exclusive_agent_operation(agent_name):
            return await self._create_environment_locked(agent_name, python_version)

    async def _create_environment_locked(
        self, agent_name: str, python_version: str
    ) -> AgentEnvironment:
        """Create an environment while holding the Agent lifecycle writer gate."""
        if python_version != "3.11":
            raise EnvironmentError("仅支持项目锁定的 Python 3.11 运行环境")
        env_path, _ = self._validate_agent_paths(agent_name)
        existing = await self.get_environment_status(agent_name)
        if (
            existing
            and existing.status == EnvironmentStatus.READY
            and self._environment_python(env_path).is_file()
        ):
            return existing

        environment = AgentEnvironment(
            agent_name=agent_name,
            environment_type=EnvironmentType.UV,
            status=EnvironmentStatus.CREATING,
            python_version=python_version,
            packages=[],
        )
        self._save_metadata(environment)
        try:
            env_path, _ = self._validate_agent_paths(agent_name)
            exit_code, _, stderr = await self._run_uv_command(
                [
                    "venv",
                    "--no-project",
                    "--managed-python",
                    "--clear",
                    "--python",
                    python_version,
                    str(env_path),
                ],
                timeout=600,
                agent_name=agent_name,
            )
            if exit_code != 0:
                raise EnvironmentError(
                    f"uv 创建环境失败（退出码 {exit_code}）"
                )
            self._validate_environment_executable(
                env_path, self._environment_python(env_path)
            )
            environment.status = EnvironmentStatus.READY
            environment.updated_at = datetime.now().isoformat()
            environment.error_message = None
            self._save_metadata(environment)
            return environment
        except asyncio.CancelledError:
            # Cancellation must not leave a durable CREATING record that can
            # never make progress after a backend restart.
            environment.status = EnvironmentStatus.ERROR
            environment.error_message = "环境创建已取消"
            environment.updated_at = datetime.now().isoformat()
            try:
                self._save_metadata(environment)
            except Exception:
                pass
            raise
        except Exception as exc:
            environment.status = EnvironmentStatus.ERROR
            environment.error_message = (
                str(exc)
                if isinstance(exc, EnvironmentError)
                else f"环境创建失败 ({type(exc).__name__})"
            )
            environment.updated_at = datetime.now().isoformat()
            self._save_metadata(environment)
            if isinstance(exc, EnvironmentError):
                raise
            raise EnvironmentError(
                f"创建环境时发生错误 ({type(exc).__name__})"
            ) from exc

    async def delete_environment(self, agent_name: str) -> bool:
        # Stop both Skill children and uv jobs first.  Registering this writer
        # blocks new executions until deletion has completed.
        async with self._exclusive_agent_operation(
            agent_name, cancel_running=True
        ):
            env_path = self.get_env_path(agent_name)
            metadata_path = self.get_metadata_path(agent_name)
            self._validate_managed_roots()
            removed_link = False
            # Deletion may safely unlink a planted leaf link, but must never
            # recurse into or read its target.
            if env_path.is_symlink():
                env_path.unlink()
                removed_link = True
            if metadata_path.is_symlink():
                metadata_path.unlink()
                removed_link = True
            if not env_path.exists() and not metadata_path.exists():
                return removed_link
            try:
                if env_path.exists():
                    shutil.rmtree(env_path)
                metadata_path.unlink(missing_ok=True)
                return True
            except Exception as exc:
                raise EnvironmentError(
                    f"删除环境时发生错误 ({type(exc).__name__})"
                ) from exc

    async def get_environment_status(
        self, agent_name: str
    ) -> Optional[AgentEnvironment]:
        env_path, metadata_path = self._validate_agent_paths(agent_name)
        if not metadata_path.exists():
            return None
        if metadata_path.stat(follow_symlinks=False).st_size > self.MAX_METADATA_BYTES:
            raise EnvironmentError("Agent 环境元数据超过 1MB 上限")
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            # Migrate metadata written by the legacy runtime without requiring a
            # destructive, one-shot migration.
            if data.get("environment_type") != EnvironmentType.UV.value:
                data["environment_type"] = EnvironmentType.UV.value
            environment = AgentEnvironment(**data)
            if environment.status == EnvironmentStatus.READY:
                try:
                    self._validate_environment_executable(
                        env_path, self._environment_python(env_path)
                    )
                except EnvironmentError:
                    environment.status = EnvironmentStatus.ERROR
                    environment.error_message = "环境解释器无效，请重新创建环境"
                    self._save_metadata(environment)
            return environment
        except Exception as exc:
            print(f"读取环境元数据失败: error_type={type(exc).__name__}")
            return None

    @staticmethod
    def _environment_python(env_path: Path) -> Path:
        if os.name == "nt":
            return env_path / "Scripts" / "python.exe"
        return env_path / "bin" / "python"

    async def install_packages(
        self, agent_name: str, packages: List[str]
    ) -> Tuple[bool, str]:
        async with self._exclusive_agent_operation(agent_name):
            return await self._install_packages_locked(agent_name, packages)

    async def _install_packages_locked(
        self, agent_name: str, packages: List[str]
    ) -> Tuple[bool, str]:
        """Install packages while holding the Agent lifecycle writer gate."""
        try:
            validated = self._validate_packages(packages)
        except EnvironmentError as exc:
            return False, str(exc)
        if not validated:
            return True, "没有需要安装的包"

        environment = await self.get_environment_status(agent_name)
        if not environment:
            return False, "环境不存在"
        if environment.status != EnvironmentStatus.READY:
            return False, f"环境状态异常: {environment.status}"

        env_path, _ = self._validate_agent_paths(agent_name)
        python = self._validate_environment_executable(
            env_path, self._environment_python(env_path)
        )
        try:
            exit_code, stdout, stderr = await self._run_uv_command(
                [
                    "pip",
                    "install",
                    "--only-binary",
                    ":all:",
                    "--python",
                    str(python),
                    *validated,
                ],
                timeout=600,
                agent_name=agent_name,
            )
            if exit_code != 0:
                return False, f"安装失败（uv 退出码 {exit_code}）"
            for package in validated:
                if package not in environment.packages:
                    environment.packages.append(package)
            environment.updated_at = datetime.now().isoformat()
            self._save_metadata(environment)
            return True, f"成功安装 {len(validated)} 个包"
        except Exception as exc:
            return False, f"安装时发生错误 ({type(exc).__name__})"

    async def list_packages(self, agent_name: str) -> List[Dict[str, str]]:
        async with self._shared_agent_operation(agent_name):
            environment = await self.get_environment_status(agent_name)
            if not environment or environment.status != EnvironmentStatus.READY:
                return []
            env_path, _ = self._validate_agent_paths(agent_name)
            python = self._validate_environment_executable(
                env_path, self._environment_python(env_path)
            )
            try:
                exit_code, stdout, _ = await self._run_uv_command(
                    ["pip", "list", "--python", str(python), "--format=json"],
                    timeout=30,
                    agent_name=agent_name,
                )
                if exit_code != 0:
                    return []
                packages = json.loads(stdout)
                return [{"name": p["name"], "version": p["version"]} for p in packages]
            except Exception as exc:
                print(f"获取包列表失败: error_type={type(exc).__name__}")
                return []

    @staticmethod
    async def _read_limited(
        stream: Optional[asyncio.StreamReader], limit: int
    ) -> bytes:
        if stream is None:
            return b""
        captured = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            remaining = max(0, limit - len(captured))
            if remaining:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        if truncated:
            marker = b"\n...[output truncated by Agent Builder]...\n"[:limit]
            keep = max(0, limit - len(marker))
            if len(captured) > keep:
                del captured[keep:]
            captured.extend(marker)
        return bytes(captured)

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    async def _wait_for_process_group_exit(
        cls, process_group_id: int, timeout: float
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while await asyncio.to_thread(cls._process_group_exists, process_group_id):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(cls.PROCESS_GROUP_POLL_INTERVAL)
        return True

    @classmethod
    async def _terminate_process(
        cls,
        process: asyncio.subprocess.Process,
        grace_seconds: float = 3.0,
    ) -> None:
        if os.name == "nt":
            if process.returncode is not None:
                return
            try:
                process.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    return
                await process.wait()
            return

        process_group_id = process.pid
        if await asyncio.to_thread(cls._process_group_exists, process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # Waiting only for the leader is insufficient: it may exit on SIGTERM
        # while an ignoring descendant remains in the same process group.
        leader_wait = (
            asyncio.create_task(process.wait())
            if process.returncode is None
            else None
        )
        group_cleared = await cls._wait_for_process_group_exit(
            process_group_id, grace_seconds
        )
        if not group_cleared:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            group_cleared = await cls._wait_for_process_group_exit(
                process_group_id, grace_seconds
            )

        if leader_wait is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(leader_wait), timeout=max(0.1, grace_seconds)
                )
            except asyncio.TimeoutError:
                leader_wait.cancel()
        if not group_cleared:
            raise EnvironmentError("无法彻底终止 Skill 进程组")

    async def execute_in_environment(
        self,
        agent_name: str,
        command: List[str],
        cwd: Optional[str] = None,
        timeout: int = 60,
        env_vars: Optional[Dict[str, str]] = None,
        execution_id: Optional[str] = None,
        additional_readable_paths: Optional[List[Path]] = None,
    ) -> Tuple[int, str, str, int]:
        async with self._shared_agent_operation(agent_name):
            return await self._execute_in_environment_shared(
                agent_name=agent_name,
                command=command,
                cwd=cwd,
                timeout=timeout,
                env_vars=env_vars,
                execution_id=execution_id,
                additional_readable_paths=additional_readable_paths,
            )

    async def _execute_in_environment_shared(
        self,
        agent_name: str,
        command: List[str],
        cwd: Optional[str] = None,
        timeout: int = 60,
        env_vars: Optional[Dict[str, str]] = None,
        execution_id: Optional[str] = None,
        additional_readable_paths: Optional[List[Path]] = None,
    ) -> Tuple[int, str, str, int]:
        """Execute while holding a shared lease on the Agent interpreter."""
        environment = await self.get_environment_status(agent_name)
        if not environment:
            raise EnvironmentError("环境不存在")
        if environment.status != EnvironmentStatus.READY:
            raise EnvironmentError(f"环境状态异常: {environment.status}")
        if not command:
            raise EnvironmentError("执行命令不能为空")

        env_path, _ = self._validate_agent_paths(agent_name)
        env_python = self._validate_environment_executable(
            env_path, self._environment_python(env_path)
        )
        executable = command[0]
        if executable in {"python", "python3", "pip", "pip3"}:
            if executable.startswith("pip"):
                resolved_command = [str(env_python), "-m", "pip", *command[1:]]
            else:
                resolved_command = [str(env_python), *command[1:]]
        else:
            candidate = env_path / ("Scripts" if os.name == "nt" else "bin") / executable
            try:
                candidate = self._validate_environment_executable(env_path, candidate)
            except EnvironmentError as exc:
                raise EnvironmentError(
                    f"命令不属于受管环境，拒绝执行: {executable}"
                ) from exc
            resolved_command = [str(candidate), *command[1:]]

        process_env = self._execution_env(cwd, env_vars)
        bin_dir = str(env_python.parent)
        process_env["VIRTUAL_ENV"] = str(env_path)
        process_env["PATH"] = f"{bin_dir}{os.pathsep}{process_env.get('PATH', '')}"
        start_time = time.monotonic()
        process_options = {}
        work_directory = Path(cwd).resolve() if cwd else RUNTIME_ROOT / "tmp" / "executions"
        readable_paths: List[Path] = []
        skills_root = (PROJECT_ROOT / "skills").resolve()
        for raw_path in additional_readable_paths or []:
            candidate = Path(raw_path)
            if candidate.is_symlink():
                raise EnvironmentError("Skill 读取目录不能是软链接")
            resolved = candidate.resolve()
            try:
                resolved.relative_to(skills_root)
            except ValueError as exc:
                raise EnvironmentError("Skill 读取目录必须位于项目 skills 目录内") from exc
            if not resolved.is_dir():
                raise EnvironmentError("Skill 读取目录不存在")
            readable_paths.append(resolved)
        if os.name != "nt" and landlock_abi() < 1:
            raise EnvironmentError(
                "当前 Linux 内核不支持 Landlock，已拒绝运行未隔离的 Skill"
            )
        # Parse every operator-controlled quota before spawning.  Invalid text
        # must fail without creating a child or any stream-monitoring tasks.
        workdir_limit = self._configured_limit(
            "AGENT_BUILDER_EXECUTION_WORKDIR_LIMIT",
            512 * 1024**2,
            minimum=1024**2,
            maximum=self.MAX_EXECUTION_WORKDIR_SIZE,
        )
        process_limit = self._configured_limit(
            "AGENT_BUILDER_EXECUTION_PROCESS_LIMIT",
            self.MAX_EXECUTION_PROCESSES,
            minimum=1,
            maximum=self.MAX_EXECUTION_PROCESSES,
        )
        per_process_memory_limit = self._configured_limit(
            "AGENT_BUILDER_EXECUTION_MEMORY_LIMIT",
            4 * 1024**3,
            minimum=64 * 1024**2,
            maximum=self.MAX_EXECUTION_AGGREGATE_MEMORY,
        )
        aggregate_memory_limit = self._configured_limit(
            "AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT",
            per_process_memory_limit,
            minimum=64 * 1024**2,
            maximum=self.MAX_EXECUTION_AGGREGATE_MEMORY,
        )
        limiter = self._resource_limit_callback(
            timeout,
            work_directory,
            env_path,
            tuple(readable_paths),
        )
        if limiter is not None:
            process_options["preexec_fn"] = limiter
        # Revalidate immediately before exec after potentially expensive sandbox
        # preparation, narrowing the window for a planted env_<agent> link.
        self._validate_agent_paths(agent_name)
        process = await asyncio.create_subprocess_exec(
            *resolved_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=process_env,
            start_new_session=(os.name != "nt"),
            **process_options,
        )
        self._agent_processes.setdefault(agent_name, set()).add(process)
        if execution_id:
            self._running_processes[execution_id] = process
            self._running_process_agents[execution_id] = agent_name
        stdout_task = asyncio.create_task(
            self._read_limited(process.stdout, self.MAX_CAPTURE_BYTES)
        )
        stderr_task = asyncio.create_task(
            self._read_limited(process.stderr, self.MAX_CAPTURE_BYTES)
        )
        wait_task = asyncio.create_task(process.wait())
        quota_task = asyncio.create_task(
            self._watch_directory_quota(process, work_directory, workdir_limit)
        )
        process_group_quota_task = asyncio.create_task(
            self._watch_process_group_quota(
                process,
                process_limit,
                aggregate_memory_limit,
            )
        )
        try:
            done, _ = await asyncio.wait(
                {wait_task, quota_task, process_group_quota_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if quota_task in done and quota_task.result():
                await self._terminate_process(process)
                await wait_task
                await asyncio.gather(stdout_task, stderr_task)
                raise EnvironmentError(
                    f"工作目录超过限制 ({workdir_limit // (1024 * 1024)}MB)"
                )
            if process_group_quota_task in done:
                violation = process_group_quota_task.result()
                if violation is not None:
                    await self._terminate_process(process)
                    await wait_task
                    await asyncio.gather(stdout_task, stderr_task)
                    violation_type, observed = violation
                    if violation_type == "processes":
                        raise EnvironmentError(
                            "Skill 进程数量超过限制 "
                            f"({observed}/{process_limit})"
                        )
                    if violation_type == "monitor":
                        raise EnvironmentError(
                            "无法监控 Skill 进程组，已终止执行"
                        )
                    raise EnvironmentError(
                        "Skill 进程组聚合内存超过限制 "
                        f"({observed // (1024 * 1024)}MB/"
                        f"{aggregate_memory_limit // (1024 * 1024)}MB)"
                    )
            await wait_task
            if os.name != "nt" and await asyncio.to_thread(
                self._process_group_exists, process.pid
            ):
                await self._terminate_process(process)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            raise EnvironmentError(
                f"命令执行超时 ({timeout}秒, {duration_ms}ms)"
            ) from exc
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_process(process))
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            background_tasks = (wait_task, quota_task, process_group_quota_task)
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            if execution_id:
                self._running_processes.pop(execution_id, None)
                self._running_process_agents.pop(execution_id, None)
            agent_processes = self._agent_processes.get(agent_name)
            if agent_processes is not None:
                agent_processes.discard(process)
                if not agent_processes:
                    self._agent_processes.pop(agent_name, None)

        duration_ms = int((time.monotonic() - start_time) * 1000)
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            duration_ms,
        )

    async def cancel_process(self, execution_id: str) -> bool:
        process = self._running_processes.get(execution_id)
        if not process:
            return False
        await self._terminate_process(process)
        return True

    async def cancel_agent_processes(self, agent_name: str) -> int:
        """Terminate every active Skill and uv child owned by one Agent."""
        processes = {
            process
            for process in (
                *self._agent_processes.get(agent_name, set()),
                *self._agent_uv_processes.get(agent_name, set()),
            )
            if process.returncode is None
        }
        if processes:
            await asyncio.gather(
                *(self._terminate_process(process) for process in processes),
                return_exceptions=True,
            )
        return len(processes)

    async def shutdown(self) -> None:
        """Terminate every project-managed child before backend exit."""
        processes = {
            process
            for process in (*self._running_processes.values(), *self._uv_processes)
            if process.returncode is None
        }
        if processes:
            await asyncio.gather(
                *(self._terminate_process(process) for process in processes),
                return_exceptions=True,
            )
        self._running_processes.clear()
        self._running_process_agents.clear()
        self._uv_processes.clear()
        self._agent_processes.clear()
        self._agent_uv_processes.clear()

    def _save_metadata(self, environment: AgentEnvironment) -> None:
        _, metadata_path = self._validate_agent_paths(environment.agent_name)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        payload = environment.model_dump()
        if payload.get("error_message") is not None:
            payload["error_message"] = str(payload["error_message"])[
                : self.MAX_ERROR_MESSAGE_CHARS
            ]
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > self.MAX_METADATA_BYTES:
            raise EnvironmentError("Agent 环境元数据超过 1MB 上限")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{metadata_path.name}.", dir=metadata_path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, metadata_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    async def get_or_create_environment(
        self, agent_name: str, python_version: str = "3.11"
    ) -> AgentEnvironment:
        existing = await self.get_environment_status(agent_name)
        if existing and existing.status == EnvironmentStatus.READY:
            return existing
        # create_environment's writer gate waits for an in-progress creator and
        # then re-checks READY state.  Polling metadata here used to time out
        # after 60 seconds even though the managed uv operation allows 10 minutes.
        return await self.create_environment(agent_name, python_version)

    async def install_skill_dependencies(
        self, agent_name: str, skill_path: Path, skill_name: str
    ) -> Tuple[bool, str, List[str]]:
        async with self._exclusive_agent_operation(agent_name):
            return await self._install_skill_dependencies_locked(
                agent_name, skill_path, skill_name
            )

    async def _install_skill_dependencies_locked(
        self, agent_name: str, skill_path: Path, skill_name: str
    ) -> Tuple[bool, str, List[str]]:
        requirements_path = Path(skill_path) / "scripts" / "requirements.txt"
        if not requirements_path.exists():
            return True, "No requirements.txt found", []
        try:
            packages = [
                line.strip()
                for line in requirements_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            packages = self._validate_packages(packages)
        except EnvironmentError as exc:
            # Package validation errors are deliberately bounded, user-facing
            # messages produced by this module.
            return False, str(exc), []
        except Exception:
            return False, "Invalid requirements.txt", []
        if not packages:
            return True, "No packages to install", []

        environment = await self.get_environment_status(agent_name)
        if not environment or environment.status != EnvironmentStatus.READY:
            return False, "Environment not ready", []
        installed = environment.installed_dependencies.get(skill_name, [])
        new_packages = [package for package in packages if package not in installed]
        if not new_packages:
            return True, f"Dependencies for '{skill_name}' already installed", []

        success, message = await self._install_packages_locked(agent_name, new_packages)
        if not success:
            return False, message, []
        latest = await self.get_environment_status(agent_name)
        if latest:
            latest.installed_dependencies.setdefault(skill_name, [])
            for package in new_packages:
                if package not in latest.installed_dependencies[skill_name]:
                    latest.installed_dependencies[skill_name].append(package)
            latest.updated_at = datetime.now().isoformat()
            self._save_metadata(latest)
        return True, f"Installed {len(new_packages)} packages", new_packages

    async def check_skill_dependencies_installed(
        self, agent_name: str, skill_name: str
    ) -> bool:
        environment = await self.get_environment_status(agent_name)
        return bool(environment and skill_name in environment.installed_dependencies)

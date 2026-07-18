"""Minimal Agent Capsule ownership for the greenfield prototype."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROTOTYPE_AGENT_ID = "00000000-0000-4000-8000-000000000001"
SAFE_ID = re.compile(r"^[a-f0-9-]{32,36}$")


@dataclass(frozen=True)
class AgentCapsule:
    agent_id: str
    data_root: Path
    runtime_root: Path
    interpreter: Path


class CapsuleManager:
    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.data_agents = self.repository_root / "data" / "agents"
        self.runtime_agents = self.repository_root / ".runtime" / "agents"

    def _relative_to_repository(self, path: Path) -> Path:
        if path != Path(os.path.abspath(path)):
            raise ValueError("managed Capsule paths must be absolute")
        try:
            relative = path.relative_to(self.repository_root)
        except ValueError as exc:
            raise ValueError("managed Capsule path escaped the checkout") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("managed Capsule path is invalid")
        return relative

    def _require_real_directory(self, path: Path) -> None:
        """Reject links and non-owned components before using a managed path."""

        relative = self._relative_to_repository(path)
        current = self.repository_root
        for component in relative.parts:
            current /= component
            metadata = os.lstat(current)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RuntimeError(f"Capsule directory is unsafe: {current}")

    def _ensure_real_directory(self, path: Path) -> None:
        relative = self._relative_to_repository(path)
        current = self.repository_root
        for component in relative.parts:
            current /= component
            try:
                os.mkdir(current, mode=0o700)
            except FileExistsError:
                pass
            metadata = os.lstat(current)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise RuntimeError(f"Capsule directory is unsafe: {current}")
        os.chmod(path, 0o700)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write Capsule metadata")
            view = view[written:]

    @staticmethod
    def _no_follow_flag() -> int:
        flag = getattr(os, "O_NOFOLLOW", None)
        if flag is None:
            raise RuntimeError("secure Capsule metadata requires O_NOFOLLOW")
        return flag

    def _read_manifest(self, path: Path, agent_id: str) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | self._no_follow_flag())
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 4_096
            ):
                raise RuntimeError("prototype Agent manifest is unsafe")
            raw = os.read(descriptor, 4_097)
            if len(raw) > 4_096 or os.read(descriptor, 1):
                raise RuntimeError("prototype Agent manifest is too large")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("prototype Agent manifest is invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("agent_id") != agent_id
            or value.get("generation") != 1
        ):
            raise RuntimeError("prototype Agent manifest has an unexpected identity")

    def _ensure_manifest(self, path: Path, agent_id: str) -> None:
        try:
            self._read_manifest(path, agent_id)
            return
        except FileNotFoundError:
            pass

        payload = (
            json.dumps(
                {
                    "schema_version": 1,
                    "agent_id": agent_id,
                    "display_name": "Harness V2 Prototype Agent",
                    "generation": 1,
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        temporary = path.parent / f".manifest.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | self._no_follow_flag(),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, path, follow_symlinks=False)
                published = True
            except FileExistsError:
                pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if published:
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        self._read_manifest(path, agent_id)

    def _validate_interpreter(self, environment: Path) -> Path:
        self._require_real_directory(environment)
        self._require_real_directory(environment / "bin")
        interpreter = environment / "bin" / "python"
        try:
            resolved = interpreter.resolve(strict=True)
            expected = Path(sys.executable).resolve(strict=True)
            metadata = resolved.stat()
        except FileNotFoundError as exc:
            raise RuntimeError("prototype Agent interpreter is missing") from exc
        if (
            resolved != expected
            or not stat.S_ISREG(metadata.st_mode)
            or not os.access(resolved, os.X_OK)
        ):
            raise RuntimeError("prototype Agent environment has no safe interpreter")
        return interpreter

    def _ensure_environment(self, runtime_root: Path) -> Path:
        environment = runtime_root / "worker-env"
        try:
            os.lstat(environment)
        except FileNotFoundError:
            pass
        else:
            return self._validate_interpreter(environment)

        staging = runtime_root / (
            f".worker-env.staging.{os.getpid()}.{secrets.token_hex(8)}"
        )
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(staging)],
                cwd=self.repository_root,
                check=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(self.repository_root / ".runtime" / "home"),
                    "TMPDIR": str(self.repository_root / ".runtime" / "tmp"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            self._validate_interpreter(staging)
            os.rename(staging, environment)
        finally:
            try:
                self._require_real_directory(staging)
            except FileNotFoundError:
                pass
            else:
                shutil.rmtree(staging)
        return self._validate_interpreter(environment)

    def _validate_capsule(self, capsule: AgentCapsule) -> None:
        expected_data = self.data_agents / capsule.agent_id
        expected_runtime = self.runtime_agents / capsule.agent_id
        expected_interpreter = expected_runtime / "worker-env" / "bin" / "python"
        if (
            not SAFE_ID.fullmatch(capsule.agent_id)
            or capsule.data_root != expected_data
            or capsule.runtime_root != expected_runtime
            or capsule.interpreter != expected_interpreter
        ):
            raise ValueError("Capsule identity is invalid")
        self._require_real_directory(capsule.data_root)
        self._require_real_directory(capsule.runtime_root)
        self._validate_interpreter(expected_runtime / "worker-env")

    @staticmethod
    def _run_root_in_use(root: Path) -> bool:
        inspected = 0
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            inspected += 1
            if inspected > 32_768:
                raise RuntimeError("process scan exceeded its safety bound")
            try:
                working_directory = Path(os.readlink(process / "cwd"))
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if working_directory == root / "work" or working_directory == root:
                return True
        return False

    @staticmethod
    def _process_identity(pid: int) -> tuple[str, int, str, str] | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            closing = raw.rfind(")")
            fields = raw[closing + 1 :].split()
            if closing < 0 or len(fields) < 20:
                raise ValueError("invalid process stat")
            marker = f"linux:{int(fields[19])}"
            process_group = int(fields[2])
            cwd = os.readlink(f"/proc/{pid}/cwd")
            command_descriptor = os.open(
                f"/proc/{pid}/cmdline", os.O_RDONLY | os.O_CLOEXEC
            )
            try:
                command_raw = os.read(command_descriptor, 4_097)
            finally:
                os.close(command_descriptor)
            if len(command_raw) > 4_096:
                raise ValueError("process command exceeded its safety bound")
            command = command_raw.replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
            return marker, process_group, cwd, command
        except (FileNotFoundError, ProcessLookupError):
            return None

    @staticmethod
    def _process_group_members(process_group: int) -> list[int]:
        members: list[int] = []
        inspected = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            inspected += 1
            if inspected > 32_768:
                raise RuntimeError("process group scan exceeded its safety bound")
            try:
                raw = (entry / "stat").read_text(encoding="ascii")
                closing = raw.rfind(")")
                fields = raw[closing + 1 :].split()
                if closing >= 0 and len(fields) >= 3 and int(fields[2]) == process_group:
                    members.append(int(entry.name))
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                continue
        return members

    def _validated_worker_record(
        self,
        path: Path,
        capsule: AgentCapsule,
        run_root: Path,
    ) -> dict[str, str]:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | self._no_follow_flag(),
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 4_096
            ):
                raise RuntimeError(f"unsafe Worker PID record: {path}")
            raw = os.read(descriptor, 4_097)
            if len(raw) > 4_096 or os.read(descriptor, 1):
                raise RuntimeError(f"unsafe Worker PID record: {path}")
        finally:
            os.close(descriptor)
        try:
            lines = raw.decode("utf-8").splitlines()
            values = dict(line.split("=", 1) for line in lines)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"unsafe Worker PID record: {path}") from exc
        expected_keys = {
            "schema",
            "role",
            "pid",
            "pgid",
            "marker",
            "root",
            "agent_id",
            "run",
            "run_root",
            "module",
            "interpreter",
            "cwd",
            "command",
        }
        if len(lines) != len(expected_keys) or set(values) != expected_keys:
            raise RuntimeError(f"unsafe Worker PID record: {path}")
        expected_interpreter = str(capsule.interpreter)
        expected_command = f"{expected_interpreter} -m agent_builder_v2.worker"
        try:
            pid = int(values["pid"])
            process_group = int(values["pgid"])
        except ValueError as exc:
            raise RuntimeError(f"unsafe Worker PID record: {path}") from exc
        if (
            values["schema"] != "1"
            or values["role"] != "worker"
            or pid <= 1
            or process_group != pid
            or not values["marker"].startswith("linux:")
            or not values["marker"][6:].isdigit()
            or values["root"] != str(self.repository_root)
            or values["agent_id"] != capsule.agent_id
            or values["run"] != run_root.name
            or values["run_root"] != str(run_root)
            or values["module"] != "agent_builder_v2.worker"
            or values["interpreter"] != expected_interpreter
            or values["cwd"] != str(run_root / "work")
            or values["command"] != expected_command
        ):
            raise RuntimeError(f"unsafe Worker PID record: {path}")
        return values

    def cleanup_orphan_run_roots(
        self, capsule: AgentCapsule, maximum_roots: int = 256
    ) -> int:
        """Remove bounded, owner-validated Run roots that have no PID record."""

        self._validate_capsule(capsule)
        if not 1 <= maximum_roots <= 4_096:
            raise ValueError("maximum_roots is invalid")
        runs_root = capsule.runtime_root / "runs"
        self._require_real_directory(runs_root)
        removed = 0
        entries = list(runs_root.iterdir())
        if len(entries) > maximum_roots:
            raise RuntimeError("orphan Run scan exceeded its safety bound")
        for root in entries:
            metadata = os.lstat(root)
            if (
                not SAFE_ID.fullmatch(root.name)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise RuntimeError(f"unsafe Run root found during recovery: {root}")
            pid_file = root / "worker.pid"
            try:
                pid_metadata = os.lstat(pid_file)
            except FileNotFoundError:
                pid_metadata = None
            if pid_metadata is not None:
                values = self._validated_worker_record(
                    pid_file, capsule, root
                )
                pid = int(values["pid"])
                process_group = int(values["pgid"])
                identity = self._process_identity(pid)
                if identity is not None:
                    marker, live_group, cwd, command = identity
                    if (
                        marker == values["marker"]
                        and live_group == process_group
                        and cwd == values["cwd"]
                        and command == values["command"]
                    ):
                        raise RuntimeError(
                            f"residual Worker is still alive: {pid_file}"
                        )
                if self._process_group_members(process_group):
                    raise RuntimeError(
                        f"residual Worker process group is still alive: {pid_file}"
                    )
            if self._run_root_in_use(root):
                raise RuntimeError(f"orphan Run root is still in use: {root}")
            shutil.rmtree(root)
            removed += 1
        return removed

    def ensure_prototype_agent(self) -> AgentCapsule:
        agent_id = PROTOTYPE_AGENT_ID
        data_root = self.data_agents / agent_id
        runtime_root = self.runtime_agents / agent_id
        self._ensure_real_directory(data_root)
        self._ensure_real_directory(runtime_root)
        for child in ("workspace", "artifacts"):
            self._ensure_real_directory(data_root / child)
        for child in ("runs", "logs"):
            self._ensure_real_directory(runtime_root / child)

        manifest = data_root / "manifest.json"
        self._ensure_manifest(manifest, agent_id)
        interpreter = self._ensure_environment(runtime_root)
        return AgentCapsule(agent_id, data_root, runtime_root, interpreter)

    def create_run_root(self, capsule: AgentCapsule, run_id: str) -> Path:
        self._validate_capsule(capsule)
        if not SAFE_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        root = capsule.runtime_root / "runs" / run_id
        try:
            os.lstat(root)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("Run root already exists")
        self._ensure_real_directory(root)
        for child in ("home", "tmp", "xdg", "input", "work", "output"):
            self._ensure_real_directory(root / child)
        return root

    def remove_run_root(self, capsule: AgentCapsule, run_id: str) -> None:
        self._validate_capsule(capsule)
        if not SAFE_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        root = capsule.runtime_root / "runs" / run_id
        try:
            self._require_real_directory(root)
        except FileNotFoundError:
            return
        shutil.rmtree(root)

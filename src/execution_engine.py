"""
脚本执行引擎 - 在隔离环境中执行Skill脚本
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from functools import partial
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

from .models import ExecutionRecord, ExecutionStatus
from .environment_manager import EnvironmentManager, EnvironmentError
from .file_storage_manager import FileStorageManager
from .blocking_work import run_blocking_with_semaphore
from .storage_paths import UnsafeStoragePathError, ensure_real_directory


class ExecutionError(Exception):
    """执行相关错误"""
    pass


class ExecutionEngine:
    """脚本执行引擎"""

    DEFAULT_TIMEOUT = 60  # 默认超时60秒
    MAX_CONCURRENT_PER_AGENT = 3  # 每个Agent最多3个并发执行
    MAX_RECORDS_PER_AGENT = 500
    MAX_CONCURRENT_DISK_OPERATIONS = 4

    def __init__(
        self,
        environment_manager: EnvironmentManager,
        file_storage: FileStorageManager,
        data_dir: Path
    ):
        """
        初始化执行引擎

        Args:
            environment_manager: 环境管理器
            file_storage: 文件存储管理器
            data_dir: 数据目录
        """
        self.environment_manager = environment_manager
        self.file_storage = file_storage
        self.data_dir = Path(data_dir)
        self.executions_dir = self.data_dir / "executions"
        runtime_dir = Path(
            os.environ.get(
                "AGENT_BUILDER_RUNTIME_DIR",
                self.data_dir.resolve().parent / ".runtime",
            )
        )
        self.work_root = runtime_dir / "tmp" / "executions"
        self._cancel_requested: set[str] = set()
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._disk_slots = asyncio.Semaphore(self.MAX_CONCURRENT_DISK_OPERATIONS)
        self._storage_lock = threading.RLock()
        self._ensure_dirs()

    async def _run_disk(self, function, *args, **kwargs):
        return await run_blocking_with_semaphore(
            self._disk_slots,
            partial(function, *args, **kwargs),
        )

    def _ensure_dirs(self):
        """确保目录存在"""
        try:
            self.executions_dir = ensure_real_directory(self.executions_dir)
            self.work_root = ensure_real_directory(self.work_root)
        except UnsafeStoragePathError as exc:
            raise ExecutionError("执行存储目录不安全") from exc

    def get_executions_dir(self, agent_name: str) -> Path:
        """获取Agent执行记录目录"""
        if not isinstance(agent_name, str) or not agent_name or "\x00" in agent_name:
            raise ExecutionError("Agent 名称无效")
        normalized = unicodedata.normalize("NFKC", agent_name).strip()
        slug = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip(".-_")
        if not slug:
            raise ExecutionError("Agent 名称无效")
        digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()[:16]
        safe_name = f"{slug[:80]}-{digest}"
        target = self.executions_dir / safe_name

        # Migrate the former slash-only directory name only when every record
        # proves ownership by this exact Agent.  Ambiguous legacy directories
        # are left untouched rather than merged.
        legacy_name = agent_name.replace("/", "_").replace("\\", "_")
        legacy = self.executions_dir / legacy_name
        if (
            legacy != target
            and not legacy.is_symlink()
            and legacy.is_dir()
            and not target.exists()
        ):
            with self._storage_lock:
                if legacy.is_dir() and not target.exists():
                    try:
                        records = list(legacy.glob("*.json"))
                        if records and all(
                            json.loads(path.read_text(encoding="utf-8")).get("agent_name")
                            == agent_name
                            for path in records
                        ):
                            legacy.replace(target)
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        pass
        if target.exists() and target.is_symlink():
            raise ExecutionError("Agent 执行记录目录不安全")
        return target

    def get_execution_path(self, agent_name: str, execution_id: str) -> Path:
        """获取执行记录文件路径"""
        if re.fullmatch(r"(?:[0-9a-f]{8}|[0-9a-f]{32})", execution_id or "") is None:
            raise ExecutionError("执行 ID 无效")
        return self.get_executions_dir(agent_name) / f"{execution_id}.json"

    def _get_semaphore(self, agent_name: str) -> asyncio.Semaphore:
        """获取Agent的并发控制信号量"""
        if agent_name not in self._semaphores:
            self._semaphores[agent_name] = asyncio.Semaphore(
                self.MAX_CONCURRENT_PER_AGENT
            )
        return self._semaphores[agent_name]

    async def execute_script(
        self,
        agent_name: str,
        skill_name: str,
        script_path: str,
        args: List[str] = None,
        input_file_ids: List[str] = None,
        timeout: int = None,
        skill_base_path: str = None
    ) -> ExecutionRecord:
        """
        执行Skill脚本

        Args:
            agent_name: Agent名称
            skill_name: Skill名称
            script_path: 脚本路径（相对于skill目录）
            args: 命令行参数
            input_file_ids: 输入文件ID列表
            timeout: 超时时间(秒)
            skill_base_path: Skill基础路径

        Returns:
            ExecutionRecord: 执行记录
        """
        if args is None:
            args = []
        if input_file_ids is None:
            input_file_ids = []
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT

        # 创建执行记录
        import uuid
        execution_id = uuid.uuid4().hex

        record = ExecutionRecord(
            execution_id=execution_id,
            agent_name=agent_name,
            skill_name=skill_name,
            script_path=script_path,
            arguments=args,
            input_file_ids=input_file_ids,
            status=ExecutionStatus.PENDING
        )

        # 保存初始记录
        await self._run_disk(self._save_record, record)

        # 获取并发控制
        semaphore = self._get_semaphore(agent_name)

        work_dir: Optional[Path] = None
        async with semaphore:
            try:
                # 更新状态为运行中
                record.status = ExecutionStatus.RUNNING
                record.started_at = datetime.now().isoformat()
                await self._run_disk(self._save_record, record)

                # 获取或创建环境
                env = await self.environment_manager.get_or_create_environment(agent_name)

                # 安装 Skill 依赖（如果有 requirements.txt）
                if skill_base_path:
                    skill_path = Path(skill_base_path)
                    if skill_path.name == "scripts":
                        skill_path = skill_path.parent
                    success, message, packages = await self.environment_manager.install_skill_dependencies(
                        agent_name=agent_name,
                        skill_path=skill_path,
                        skill_name=skill_name
                    )
                    if not success:
                        print(
                            "[EXEC] 警告: 依赖安装失败 "
                            f"message_length={len(message or '')}"
                        )
                    elif packages:
                        print(f"[EXEC] 已安装依赖: package_count={len(packages)}")

                # 准备工作目录（包含复制技能文件）
                work_dir = await self._prepare_work_dir_with_skill(
                    agent_name=agent_name,
                    execution_id=execution_id,
                    input_file_ids=input_file_ids,
                    skill_base_path=skill_base_path
                )

                # 复制输入文件到工作目录
                if input_file_ids:
                    await self._copy_input_files(agent_name, input_file_ids, work_dir)

                # 执行脚本
                exit_code, stdout, stderr, duration_ms = await self._execute_in_environment(
                    agent_name=agent_name,
                    execution_id=execution_id,
                    script_path=script_path,
                    args=args,
                    work_dir=work_dir,
                    timeout=timeout,
                    skill_base_path=skill_base_path,
                )

                # 更新执行记录
                record.exit_code = exit_code
                record.stdout = stdout
                record.stderr = stderr
                record.duration_ms = duration_ms
                record.finished_at = datetime.now().isoformat()

                if execution_id in self._cancel_requested:
                    record.status = ExecutionStatus.CANCELLED
                elif exit_code == 0:
                    record.status = ExecutionStatus.SUCCESS
                else:
                    record.status = ExecutionStatus.FAILED

                await self._run_disk(self._save_record, record)
                return record

            except asyncio.TimeoutError:
                record.status = ExecutionStatus.TIMEOUT
                record.stderr = f"执行超时 ({timeout}秒)"
                record.finished_at = datetime.now().isoformat()
                await self._run_disk(self._save_record, record)
                return record

            except EnvironmentError as e:
                record.status = ExecutionStatus.FAILED
                record.stderr = f"执行失败 ({type(e).__name__})"
                record.finished_at = datetime.now().isoformat()
                await self._run_disk(self._save_record, record)
                return record

            except Exception as e:
                record.status = ExecutionStatus.FAILED
                record.stderr = f"执行失败 ({type(e).__name__})"
                record.finished_at = datetime.now().isoformat()
                await self._run_disk(self._save_record, record)
                return record
            finally:
                self._cancel_requested.discard(execution_id)
                await self._run_disk(
                    self._cleanup_execution_artifacts,
                    work_dir,
                    agent_name,
                )

    async def _execute_in_environment(
        self,
        agent_name: str,
        execution_id: str,
        script_path: str,
        args: List[str],
        work_dir: Path,
        timeout: int,
        skill_base_path: Optional[str] = None,
    ) -> tuple:
        """
        在项目内 uv 环境中执行脚本

        Returns:
            (exit_code, stdout, stderr, duration_ms)
        """
        # Skill sources are mounted read-only by Landlock.  Executing them in
        # place avoids copying and deleting megabytes of identical assets for
        # every call, while all mutable input/output remains in this workdir.
        source_root, candidate = await self._run_disk(
            self._resolve_script_paths,
            skill_base_path,
            work_dir,
            script_path,
        )
        command = ["python", str(candidate), *args]

        try:
            return await self.environment_manager.execute_in_environment(
                agent_name=agent_name,
                command=command,
                cwd=str(work_dir),
                timeout=timeout,
                execution_id=execution_id,
                additional_readable_paths=(
                    [source_root] if skill_base_path is not None else []
                ),
            )
        except EnvironmentError as e:
            if "超时" in str(e):
                raise asyncio.TimeoutError from e
            raise

    @staticmethod
    def _resolve_script_paths(
        skill_base_path: Optional[str],
        work_dir: Path,
        script_path: str,
    ) -> tuple[Path, Path]:
        source_root = (
            Path(skill_base_path).resolve()
            if skill_base_path
            else work_dir.resolve()
        )
        candidate = (source_root / script_path).resolve()
        try:
            candidate.relative_to(source_root)
        except ValueError as exc:
            raise ExecutionError("脚本路径超出 Skill 源目录") from exc
        if not candidate.is_file():
            raise ExecutionError(f"脚本不存在: {script_path}")
        return source_root, candidate

    def _prepare_work_dir(
        self,
        agent_name: str,
        execution_id: str,
        input_file_ids: List[str]
    ) -> Path:
        """
        准备工作目录（修复版）

        Args:
            agent_name: Agent名称
            execution_id: 执行ID
            input_file_ids: 输入文件ID列表

        Returns:
            工作目录路径
        """
        # 创建临时工作目录
        work_dir = Path(
            tempfile.mkdtemp(
                prefix=f"exec_{execution_id}_",
                dir=self.work_root,
            )
        )

        # 创建输入文件目录
        input_dir = work_dir / "input"
        input_dir.mkdir(exist_ok=True)

        return work_dir

    async def _prepare_work_dir_with_skill(
        self,
        agent_name: str,
        execution_id: str,
        input_file_ids: List[str],
        skill_base_path: str = None
    ) -> Path:
        """
        准备工作目录并复制技能文件

        Args:
            agent_name: Agent名称
            execution_id: 执行ID
            input_file_ids: 输入文件ID列表
            skill_base_path: Skill基础路径

        Returns:
            工作目录路径
        """
        return await self._run_disk(
            self._prepare_work_dir_with_skill_sync,
            execution_id,
            skill_base_path,
        )

    def _prepare_work_dir_with_skill_sync(
        self,
        execution_id: str,
        skill_base_path: Optional[str],
    ) -> Path:
        """Create and validate one execution directory in a disk worker."""
        # Skill source files are exposed read-only to the sandbox at execution
        # time. Validate before allocating a work directory so failure leaves
        # no orphan on disk.
        if skill_base_path:
            skill_path = Path(skill_base_path)
            if (
                not skill_path.exists()
                or not skill_path.is_dir()
                or skill_path.is_symlink()
            ):
                raise ExecutionError("Skill 源目录不存在或不安全")

        work_dir = Path(
            tempfile.mkdtemp(
                prefix=f"exec_{execution_id}_",
                dir=self.work_root,
            )
        )

        # 创建输入文件目录
        input_dir = work_dir / "input"
        input_dir.mkdir(exist_ok=True)

        return work_dir

    async def _copy_input_files(
        self,
        agent_name: str,
        input_file_ids: List[str],
        work_dir: Path
    ):
        """复制输入文件到工作目录（修复版）

        Args:
            agent_name: Agent名称
            input_file_ids: 输入文件ID列表
            work_dir: 工作目录路径
        """
        if not input_file_ids:
            return

        input_dir = work_dir / "input"
        await self._run_disk(input_dir.mkdir, parents=True, exist_ok=True)

        for file_id in input_file_ids:
            try:
                target_path = await self.file_storage.copy_file_to_workdir(
                    agent_name=agent_name,
                    file_id=file_id,
                    workdir=input_dir
                )
                if target_path:
                    print("[EXEC] 已复制输入文件")
                else:
                    print("[EXEC] 警告: 无法复制输入文件")
            except Exception as e:
                print(
                    "[EXEC] 复制输入文件失败: "
                    f"error_type={type(e).__name__}"
                )

    def _cleanup_work_dir(self, work_dir: Path):
        """清理工作目录"""
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as e:
            print(f"清理工作目录失败: error_type={type(e).__name__}")

    def _cleanup_execution_artifacts(
        self,
        work_dir: Optional[Path],
        agent_name: str,
    ) -> None:
        if work_dir is not None:
            self._cleanup_work_dir(work_dir)
        self._enforce_record_limit(agent_name)

    async def get_execution_status(self, agent_name: str, execution_id: str) -> Optional[ExecutionRecord]:
        """
        获取执行状态

        Args:
            agent_name: Agent名称
            execution_id: 执行ID

        Returns:
            ExecutionRecord或None
        """
        return await self._run_disk(
            self._get_execution_status_sync,
            agent_name,
            execution_id,
        )

    def _get_execution_status_sync(
        self,
        agent_name: str,
        execution_id: str,
    ) -> Optional[ExecutionRecord]:
        try:
            execution_path = self.get_execution_path(agent_name, execution_id)
        except ExecutionError:
            return None
        if not execution_path.exists():
            return None
        try:
            with open(execution_path, "r", encoding="utf-8") as handle:
                return ExecutionRecord(**json.load(handle))
        except Exception as exc:
            print(f"读取执行记录失败: error_type={type(exc).__name__}")
            return None

    async def list_executions(self, agent_name: str, limit: int = 50) -> List[ExecutionRecord]:
        """
        列出执行记录

        Args:
            agent_name: Agent名称
            limit: 最大数量

        Returns:
            执行记录列表
        """
        return await self._run_disk(self._list_executions_sync, agent_name, limit)

    def _list_executions_sync(
        self,
        agent_name: str,
        limit: int,
    ) -> List[ExecutionRecord]:
        executions_dir = self.get_executions_dir(agent_name)
        if not executions_dir.exists():
            return []
        records = []
        for json_file in executions_dir.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as handle:
                    records.append(ExecutionRecord(**json.load(handle)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[: max(0, min(int(limit), self.MAX_RECORDS_PER_AGENT))]

    async def cancel_execution(self, agent_name: str, execution_id: str) -> bool:
        """
        取消执行

        Args:
            agent_name: Agent名称
            execution_id: 执行ID

        Returns:
            是否取消成功
        """
        if re.fullmatch(r"(?:[0-9a-f]{8}|[0-9a-f]{32})", execution_id or "") is None:
            return False
        self._cancel_requested.add(execution_id)
        process_stopped = await self.environment_manager.cancel_process(execution_id)

        # 更新状态
        record = await self.get_execution_status(agent_name, execution_id)
        if record and record.status == ExecutionStatus.RUNNING:
            record.status = ExecutionStatus.CANCELLED
            record.finished_at = datetime.now().isoformat()
            await self._run_disk(self._save_record, record)
            return True

        self._cancel_requested.discard(execution_id)
        return process_stopped

    def _save_record(self, record: ExecutionRecord):
        """保存执行记录"""
        executions_dir = self.get_executions_dir(record.agent_name)
        try:
            executions_dir = ensure_real_directory(executions_dir)
        except UnsafeStoragePathError as exc:
            raise ExecutionError("Agent 执行记录目录不安全") from exc

        execution_path = self.get_execution_path(record.agent_name, record.execution_id)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{execution_path.name}.", dir=executions_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    record.model_dump(),
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, execution_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _enforce_record_limit(self, agent_name: str) -> None:
        """Keep execution metadata bounded without rewriting existing records."""
        executions_dir = self.get_executions_dir(agent_name)
        try:
            records = sorted(
                executions_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for stale in records[self.MAX_RECORDS_PER_AGENT :]:
                stale.unlink(missing_ok=True)
        except OSError as exc:
            print(f"清理执行记录失败: error_type={type(exc).__name__}")

    async def cleanup_old_executions(self, agent_name: str, days: int = 7) -> int:
        """
        清理旧的执行记录

        Args:
            agent_name: Agent名称
            days: 保留天数

        Returns:
            清理的记录数
        """
        return await self._run_disk(
            self._cleanup_old_executions_sync,
            agent_name,
            days,
        )

    def _cleanup_old_executions_sync(self, agent_name: str, days: int) -> int:
        executions_dir = self.get_executions_dir(agent_name)
        if not executions_dir.exists():
            return 0
        cutoff_time = time.time() - (max(0, days) * 24 * 60 * 60)
        cleaned = 0
        for json_file in executions_dir.glob("*.json"):
            try:
                if json_file.stat().st_mtime < cutoff_time:
                    json_file.unlink()
                    cleaned += 1
            except OSError:
                continue
        return cleaned

    async def cleanup_agent_executions(self, agent_name: str) -> bool:
        """Remove all bounded execution metadata owned by an Agent."""
        try:
            await self._run_disk(self._cleanup_agent_executions_sync, agent_name)
            self._semaphores.pop(agent_name, None)
            return True
        except OSError:
            return False

    def _cleanup_agent_executions_sync(self, agent_name: str) -> None:
        executions_dir = self.get_executions_dir(agent_name)
        if executions_dir.exists():
            shutil.rmtree(executions_dir)

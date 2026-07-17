"""
文件存储模块 - 管理Agent的文件上传和存储
"""
import asyncio
import json
import hashlib
import os
import shutil
import re
import tempfile
import threading
import unicodedata
import uuid
from functools import partial
from pathlib import Path
from typing import Optional, List, Tuple
from datetime import datetime

from .models import FileInfo
from .security import SecurityValidationError, resolve_contained_path, sanitise_filename
from .blocking_work import run_blocking_with_semaphore
from .storage_paths import UnsafeStoragePathError, ensure_real_directory


class FileStorageError(Exception):
    """文件存储相关错误"""
    pass


class FileStorageManager:
    """Agent文件存储管理器"""

    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
    MAX_FILES_PER_AGENT = 1_000
    MAX_TOTAL_BYTES_PER_AGENT = 1024 * 1024 * 1024  # 1GB
    MAX_CONCURRENT_UPLOADS = 4
    MAX_CONCURRENT_DISK_OPERATIONS = 4

    def __init__(self, storage_dir: Path):
        """
        初始化文件存储管理器

        Args:
            storage_dir: 文件存储根目录
        """
        self.storage_dir = Path(storage_dir)
        self.metadata_dir = self.storage_dir / ".metadata"
        self._index_lock = threading.RLock()
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._upload_slots = asyncio.Semaphore(self.MAX_CONCURRENT_UPLOADS)
        self._disk_slots = asyncio.Semaphore(self.MAX_CONCURRENT_DISK_OPERATIONS)
        self._ensure_dirs()

    async def _run_disk(self, function, *args, **kwargs):
        return await run_blocking_with_semaphore(
            self._disk_slots,
            partial(function, *args, **kwargs),
        )

    def _ensure_dirs(self):
        """确保目录存在"""
        try:
            self.storage_dir = ensure_real_directory(self.storage_dir)
            self.metadata_dir = ensure_real_directory(self.metadata_dir)
        except UnsafeStoragePathError as exc:
            raise FileStorageError("文件存储目录不能包含链接") from exc

    def _agent_lock(self, agent_name: str) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_name)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_name] = lock
        return lock

    def _enforce_agent_quota(self, agent_name: str, incoming_bytes: int) -> None:
        metadata_path = self.get_metadata_path(agent_name)
        with self._index_lock:
            if metadata_path.exists():
                try:
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                    files = payload.get("files", [])
                    if not isinstance(files, list):
                        raise ValueError
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise FileStorageError("文件索引损坏，拒绝继续写入") from exc
            else:
                files = []
            if len(files) >= self.MAX_FILES_PER_AGENT:
                raise FileStorageError("每个 Agent 最多存储 1000 个文件")
            current_bytes = sum(
                max(0, int(item.get("file_size", 0)))
                for item in files
                if isinstance(item, dict)
            )
            if current_bytes + incoming_bytes > self.MAX_TOTAL_BYTES_PER_AGENT:
                raise FileStorageError("每个 Agent 的文件总量不能超过 1GB")

    def get_agent_storage_path(self, agent_name: str) -> Path:
        """获取Agent存储目录"""
        self._migrate_legacy_storage(agent_name)
        safe_name = self._safe_agent_component(agent_name)
        target = self.storage_dir / safe_name
        if target.is_symlink():
            raise FileStorageError("Agent 文件目录不能是软链接")
        return target

    def get_metadata_path(self, agent_name: str) -> Path:
        """获取元数据文件路径"""
        self._migrate_legacy_storage(agent_name)
        safe_name = self._safe_agent_component(agent_name)
        target = self.metadata_dir / f"{safe_name}.json"
        if target.is_symlink():
            raise FileStorageError("Agent 文件索引不能是软链接")
        return target

    def _safe_agent_component(self, agent_name: str) -> str:
        if not isinstance(agent_name, str) or not agent_name or "\x00" in agent_name:
            raise FileStorageError("Agent名称无效")
        normalized = unicodedata.normalize("NFKC", agent_name).strip()
        slug = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip(".-_")
        if not slug:
            raise FileStorageError("Agent名称无效")
        digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()[:16]
        return f"{slug[:80]}-{digest}"

    @staticmethod
    def _legacy_agent_component(agent_name: str) -> str:
        return re.sub(r"[^\w.-]", "_", agent_name, flags=re.UNICODE)[:120]

    def _migrate_legacy_storage(self, agent_name: str) -> None:
        """Move an unambiguous legacy directory/index to the collision-safe key."""
        new_component = self._safe_agent_component(agent_name)
        legacy_component = self._legacy_agent_component(agent_name)
        if legacy_component in {"", ".", ".."} or legacy_component == new_component:
            return
        legacy_metadata = self.metadata_dir / f"{legacy_component}.json"
        new_metadata = self.metadata_dir / f"{new_component}.json"
        legacy_directory = self.storage_dir / legacy_component
        new_directory = self.storage_dir / new_component
        if (
            new_metadata.is_symlink()
            or legacy_metadata.is_symlink()
            or legacy_directory.is_symlink()
            or new_directory.is_symlink()
            or new_metadata.exists()
            or not legacy_metadata.exists()
        ):
            return

        with self._index_lock:
            if new_metadata.exists() or not legacy_metadata.exists():
                return
            try:
                payload = json.loads(legacy_metadata.read_text(encoding="utf-8"))
                files = payload.get("files", [])
                if any(item.get("agent_name") != agent_name for item in files):
                    return
                if legacy_directory.exists() and not new_directory.exists():
                    legacy_directory.replace(new_directory)
                    for item in files:
                        relative = Path(item.get("file_path", ""))
                        if relative.parts and relative.parts[0] == legacy_component:
                            item["file_path"] = str(
                                Path(new_component).joinpath(*relative.parts[1:])
                            )
                self._write_index_atomic(new_metadata, payload)
                legacy_metadata.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return

    def _safe_filename(self, filename: str) -> str:
        try:
            return sanitise_filename(filename)
        except SecurityValidationError as exc:
            raise FileStorageError(str(exc)) from exc

    def _resolve_stored_path(self, relative_path: str, *, must_exist: bool = True) -> Path:
        try:
            return resolve_contained_path(
                self.storage_dir,
                relative_path,
                must_exist=must_exist,
                require_file=must_exist,
            )
        except SecurityValidationError as exc:
            raise FileStorageError("文件索引包含不安全的存储路径") from exc

    def _calculate_checksum(self, content: bytes) -> str:
        """计算文件MD5校验和"""
        return hashlib.sha256(content).hexdigest()

    def _detect_mime_type(self, filename: str) -> str:
        """检测文件MIME类型"""
        ext = Path(filename).suffix.lower()
        mime_types = {
            '.pdf': 'application/pdf',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.xls': 'application/vnd.ms-excel',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.csv': 'text/csv',
            '.txt': 'text/plain',
            '.json': 'application/json',
            '.xml': 'application/xml',
            '.html': 'text/html',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.zip': 'application/zip',
            '.py': 'text/x-python',
            '.js': 'text/javascript',
            '.md': 'text/markdown',
        }
        return mime_types.get(ext, 'application/octet-stream')

    async def upload_file(
        self,
        agent_name: str,
        file_content: bytes,
        filename: str,
        mime_type: Optional[str] = None
    ) -> FileInfo:
        """
        上传文件

        Args:
            agent_name: Agent名称
            file_content: 文件内容
            filename: 原始文件名
            mime_type: MIME类型（可选，自动检测）

        Returns:
            FileInfo: 文件信息
        """
        filename = self._safe_filename(filename)

        # 检查文件大小
        if len(file_content) > self.MAX_FILE_SIZE:
            raise FileStorageError(f"文件过大，最大支持 {self.MAX_FILE_SIZE // (1024*1024)}MB")

        # 生成文件ID
        file_id = uuid.uuid4().hex

        # 检测MIME类型
        if not mime_type:
            mime_type = self._detect_mime_type(filename)

        async with self._upload_slots, self._agent_lock(agent_name):
            try:
                return await self._run_disk(
                    self._store_file_content_sync,
                    agent_name,
                    file_id,
                    file_content,
                    filename,
                    mime_type,
                )
            except asyncio.CancelledError:
                await self._run_disk(
                    self._rollback_upload_sync,
                    agent_name,
                    file_id,
                    filename,
                )
                raise

    def _store_file_content_sync(
        self,
        agent_name: str,
        file_id: str,
        file_content: bytes,
        filename: str,
        mime_type: str,
    ) -> FileInfo:
        self._enforce_agent_quota(agent_name, len(file_content))
        file_path, temporary_path = self._prepare_upload_paths_sync(
            agent_name, file_id, filename
        )
        file_info = FileInfo(
            file_id=file_id,
            agent_name=agent_name,
            filename=filename,
            file_size=len(file_content),
            mime_type=mime_type,
            checksum=self._calculate_checksum(file_content),
            file_path=str(file_path.relative_to(self.storage_dir)),
        )
        try:
            self._write_bytes_exclusive(temporary_path, file_content)
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(file_path)
            self._add_file_to_index(agent_name, file_info)
            return file_info
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            file_path.unlink(missing_ok=True)
            raise

    async def upload_stream(
        self,
        agent_name: str,
        upload,
        filename: str,
        mime_type: Optional[str] = None,
    ) -> FileInfo:
        """Stream an uploaded file to disk while enforcing a hard byte limit."""
        async with self._upload_slots:
            return await self._upload_stream_bounded(
                agent_name,
                upload,
                filename,
                mime_type,
            )

    async def _upload_stream_bounded(
        self,
        agent_name: str,
        upload,
        filename: str,
        mime_type: Optional[str],
    ) -> FileInfo:
        import aiofiles

        filename = self._safe_filename(filename)
        file_id = uuid.uuid4().hex
        if not mime_type:
            mime_type = self._detect_mime_type(filename)

        file_path, temporary_path = await self._run_disk(
            self._prepare_upload_paths_sync,
            agent_name,
            file_id,
            filename,
        )

        checksum = hashlib.sha256()
        total = 0
        try:
            async with aiofiles.open(temporary_path, "xb") as destination:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.MAX_FILE_SIZE:
                        raise FileStorageError(
                            f"文件过大，最大支持 {self.MAX_FILE_SIZE // (1024 * 1024)}MB"
                        )
                    checksum.update(chunk)
                    await destination.write(chunk)
        except BaseException:
            await self._run_disk(temporary_path.unlink, missing_ok=True)
            raise

        file_info = FileInfo(
            file_id=file_id,
            agent_name=agent_name,
            filename=filename,
            file_size=total,
            mime_type=mime_type,
            checksum=checksum.hexdigest(),
            file_path=str(file_path.relative_to(self.storage_dir)),
        )
        try:
            async with self._agent_lock(agent_name):
                await self._run_disk(
                    self._commit_stream_upload_sync,
                    agent_name,
                    total,
                    temporary_path,
                    file_path,
                    file_info,
                )
        except BaseException:
            await self._run_disk(
                self._rollback_upload_sync,
                agent_name,
                file_id,
                filename,
            )
            raise
        return file_info

    def _prepare_upload_paths_sync(
        self,
        agent_name: str,
        file_id: str,
        filename: str,
    ) -> tuple[Path, Path]:
        agent_dir = self.get_agent_storage_path(agent_name)
        try:
            agent_dir = ensure_real_directory(agent_dir)
        except UnsafeStoragePathError as exc:
            raise FileStorageError("Agent 文件目录不能包含链接") from exc
        extension = Path(filename).suffix
        stored_filename = f"{file_id}{extension}" if extension else file_id
        return (
            agent_dir / stored_filename,
            agent_dir / f".{stored_filename}.uploading",
        )

    def _commit_stream_upload_sync(
        self,
        agent_name: str,
        total: int,
        temporary_path: Path,
        file_path: Path,
        file_info: FileInfo,
    ) -> None:
        self._enforce_agent_quota(agent_name, total)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(file_path)
        self._add_file_to_index(agent_name, file_info)

    def _rollback_upload_sync(
        self,
        agent_name: str,
        file_id: str,
        filename: str,
    ) -> None:
        file_path, temporary_path = self._prepare_upload_paths_sync(
            agent_name,
            file_id,
            filename,
        )
        temporary_path.unlink(missing_ok=True)
        file_path.unlink(missing_ok=True)
        self._remove_file_from_index(agent_name, file_id)

    async def list_files(self, agent_name: str) -> List[FileInfo]:
        """
        列出Agent的所有文件

        Args:
            agent_name: Agent名称

        Returns:
            文件信息列表
        """
        return await self._run_disk(self._list_files_sync, agent_name)

    def _list_files_sync(self, agent_name: str) -> List[FileInfo]:
        metadata_path = self.get_metadata_path(agent_name)
        if not metadata_path.exists():
            return []
        try:
            with self._index_lock, open(
                metadata_path, "r", encoding="utf-8"
            ) as handle:
                data = json.load(handle)
            return [FileInfo(**item) for item in data.get("files", [])]
        except Exception as exc:
            raise FileStorageError("文件索引损坏，拒绝继续访问") from exc

    async def get_file_path(self, agent_name: str, file_id: str) -> Optional[Path]:
        """
        获取文件物理路径

        Args:
            agent_name: Agent名称
            file_id: 文件ID

        Returns:
            文件路径或None
        """
        return await self._run_disk(
            self._get_file_path_sync,
            agent_name,
            file_id,
        )

    def _get_file_path_sync(
        self,
        agent_name: str,
        file_id: str,
    ) -> Optional[Path]:
        file_info = self._get_file_info_sync(agent_name, file_id)
        if file_info is None:
            return None
        try:
            return self._resolve_stored_path(file_info.file_path)
        except FileStorageError:
            return None

    async def get_file_info(self, agent_name: str, file_id: str) -> Optional[FileInfo]:
        """
        获取文件信息

        Args:
            agent_name: Agent名称
            file_id: 文件ID

        Returns:
            FileInfo或None
        """
        files = await self.list_files(agent_name)
        for file_info in files:
            if file_info.file_id == file_id:
                return file_info
        return None

    def _get_file_info_sync(
        self,
        agent_name: str,
        file_id: str,
    ) -> Optional[FileInfo]:
        for file_info in self._list_files_sync(agent_name):
            if file_info.file_id == file_id:
                return file_info
        return None

    async def delete_file(self, agent_name: str, file_id: str) -> bool:
        """
        删除文件

        Args:
            agent_name: Agent名称
            file_id: 文件ID

        Returns:
            是否删除成功
        """
        async with self._agent_lock(agent_name):
            return await self._run_disk(
                self._delete_file_sync,
                agent_name,
                file_id,
            )

    def _delete_file_sync(self, agent_name: str, file_id: str) -> bool:
        file_info = self._get_file_info_sync(agent_name, file_id)
        if file_info is None:
            return False
        try:
            file_path = self._resolve_stored_path(file_info.file_path)
            quarantine = file_path.with_name(
                f".{file_path.name}.{uuid.uuid4().hex}.deleting"
            )
            file_path.replace(quarantine)
            try:
                self._remove_file_from_index(agent_name, file_id)
            except Exception:
                quarantine.replace(file_path)
                raise
            try:
                quarantine.unlink()
            except OSError:
                quarantine.replace(file_path)
                self._add_file_to_index(agent_name, file_info)
                return False
        except (FileNotFoundError, FileStorageError):
            return False
        return True

    @staticmethod
    def _write_bytes_exclusive(path: Path, content: bytes) -> None:
        with open(path, "xb") as destination:
            destination.write(content)

    async def get_file_content(self, agent_name: str, file_id: str) -> Optional[bytes]:
        """
        获取文件内容

        Args:
            agent_name: Agent名称
            file_id: 文件ID

        Returns:
            文件内容或None
        """
        return await self._run_disk(
            self._get_file_content_sync,
            agent_name,
            file_id,
        )

    def _get_file_content_sync(
        self,
        agent_name: str,
        file_id: str,
    ) -> Optional[bytes]:
        file_info = self._get_file_info_sync(agent_name, file_id)
        if file_info is None:
            return None
        try:
            return self._resolve_stored_path(file_info.file_path).read_bytes()
        except (FileNotFoundError, FileStorageError):
            return None

    def _add_file_to_index(self, agent_name: str, file_info: FileInfo):
        """添加文件到索引"""
        metadata_path = self.get_metadata_path(agent_name)

        with self._index_lock:
            data = {"files": []}
            if metadata_path.exists():
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            data["files"].append(file_info.model_dump())
            data["updated_at"] = datetime.now().isoformat()
            self._write_index_atomic(metadata_path, data)

    def _write_index_atomic(self, metadata_path: Path, data: dict) -> None:
        try:
            ensure_real_directory(self.metadata_dir)
            ensure_real_directory(metadata_path.parent)
        except UnsafeStoragePathError as exc:
            raise FileStorageError("Agent 文件索引目录不安全") from exc
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{metadata_path.name}.", suffix=".tmp", dir=metadata_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, metadata_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _remove_file_from_index(self, agent_name: str, file_id: str):
        """从索引中移除文件"""
        metadata_path = self.get_metadata_path(agent_name)

        if not metadata_path.exists():
            return

        try:
            with self._index_lock:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                current_files = data.get("files", [])
                remaining_files = [
                    f for f in data.get("files", [])
                    if f.get("file_id") != file_id
                ]
                if len(remaining_files) == len(current_files):
                    return
                data["files"] = remaining_files
                data["updated_at"] = datetime.now().isoformat()
                self._write_index_atomic(metadata_path, data)
        except Exception as exc:
            raise FileStorageError("更新文件索引失败") from exc

    async def cleanup_agent_files(self, agent_name: str) -> bool:
        """
        清理Agent的所有文件

        Args:
            agent_name: Agent名称

        Returns:
            是否成功
        """
        try:
            async with self._agent_lock(agent_name):
                await self._run_disk(self._cleanup_agent_files_sync, agent_name)
            return True
        except Exception:
            return False

    def _cleanup_agent_files_sync(self, agent_name: str) -> None:
        agent_dir = self.get_agent_storage_path(agent_name)
        metadata_path = self.get_metadata_path(agent_name)
        if agent_dir.exists():
            shutil.rmtree(agent_dir)
        metadata_path.unlink(missing_ok=True)

    async def copy_file_to_workdir(
        self,
        agent_name: str,
        file_id: str,
        workdir: Path,
        new_name: Optional[str] = None
    ) -> Optional[Path]:
        """
        复制文件到工作目录

        Args:
            agent_name: Agent名称
            file_id: 文件ID
            workdir: 目标工作目录
            new_name: 新文件名（可选）

        Returns:
            目标文件路径或None
        """
        async with self._agent_lock(agent_name):
            return await self._run_disk(
                self._copy_file_to_workdir_sync,
                agent_name,
                file_id,
                workdir,
                new_name,
            )

    def _copy_file_to_workdir_sync(
        self,
        agent_name: str,
        file_id: str,
        workdir: Path,
        new_name: Optional[str],
    ) -> Optional[Path]:
        file_info = self._get_file_info_sync(agent_name, file_id)
        if file_info is None:
            return None
        try:
            source_path = self._resolve_stored_path(file_info.file_path)
        except FileStorageError:
            return None
        workdir.mkdir(parents=True, exist_ok=True)
        resolved_workdir = workdir.resolve()
        target_name = self._safe_filename(new_name or file_info.filename)
        try:
            target_path = resolve_contained_path(
                resolved_workdir,
                target_name,
                must_exist=False,
                require_file=False,
            )
        except SecurityValidationError as exc:
            raise FileStorageError("目标文件名不安全") from exc
        shutil.copy2(source_path, target_path)
        return target_path

"""
知识库管理器
负责知识库 CRUD、文档管理、向量集合管理
"""
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import List, Optional, Dict, Any

from src.models import (
    KnowledgeBase,
    Document,
    DocumentStatus,
    Chunk
)
from src.document_processor import DocumentProcessor
from src.embedder import Embedder
from src.storage_paths import (
    UnsafeStoragePathError,
    ensure_real_directory,
    validate_regular_file,
)

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class KnowledgeBaseQuotaError(ValueError):
    """Raised before a knowledge-base write would exceed a hard quota."""


class KnowledgeBaseManager:
    """知识库管理器

    负责：
    - 知识库的 CRUD 操作
    - 知识库元数据持久化
    - 向量集合管理
    - 文档上传与处理
    """

    MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
    MAX_DOCUMENTS_PER_KB = 200
    MAX_SOURCE_BYTES_PER_KB = 1024 * 1024 * 1024
    MAX_KNOWLEDGE_BASES = 50
    MAX_FAILED_DOCUMENTS_PER_KB = 20
    MAX_CHUNKS_PER_KB = 100_000
    MAX_TOTAL_CHUNKS = 500_000
    MAX_DIRECTORY_BYTES_PER_KB = 2 * 1024 * 1024 * 1024
    MAX_TOTAL_DIRECTORY_BYTES = 10 * 1024 * 1024 * 1024
    MAX_DIRECTORY_ENTRIES = 50_000
    MAX_CONFIG_BYTES = 1024 * 1024
    METADATA_RESERVE_BYTES = 16 * 1024
    VECTOR_OVERHEAD_BYTES_PER_CHUNK = 2 * 1024

    def __init__(self, data_dir: Path, embedder: Optional[Embedder] = None):
        """初始化知识库管理器

        Args:
            data_dir: 数据目录根路径
        """
        raw_data_dir = Path(data_dir)
        try:
            self.data_dir = ensure_real_directory(raw_data_dir).resolve(strict=True)
        except UnsafeStoragePathError as exc:
            raise ValueError("知识库数据目录不能是软链接或包含其它链接") from exc
        try:
            self.data_dir.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError("知识库数据目录必须位于项目目录内") from exc
        self.kb_dir = self.data_dir / "knowledge_bases"
        try:
            self.kb_dir = ensure_real_directory(self.kb_dir)
        except UnsafeStoragePathError as exc:
            raise ValueError("知识库根目录不能包含链接") from exc

        self.config_file = self.kb_dir / "knowledge_bases.json"
        try:
            validate_regular_file(self.config_file, allow_missing=True)
        except UnsafeStoragePathError as exc:
            raise ValueError("知识库配置文件不能是软链接或硬链接") from exc

        # 组件
        self.processor = DocumentProcessor()
        self.embedder: Optional[Embedder] = embedder
        self._lock = threading.RLock()
        # Storage-changing transactions are serialized across KBs.  This makes
        # global byte/chunk quotas race-free and avoids concurrent Chroma writers
        # amplifying random I/O on the same SSD.
        self._resource_lock = threading.RLock()
        self._clients: Dict[str, Any] = {}
        self._collections: Dict[str, Any] = {}
        self._kb_locks: Dict[str, threading.RLock] = {}

        self._load_configs()

    def _load_configs(self):
        """加载知识库配置"""
        try:
            validate_regular_file(self.config_file, allow_missing=True)
        except UnsafeStoragePathError as exc:
            raise ValueError("知识库配置文件不能是软链接或硬链接") from exc
        if self.config_file.exists():
            if (
                self.config_file.stat(follow_symlinks=False).st_size
                > self.MAX_CONFIG_BYTES
            ):
                raise ValueError("知识库配置文件超过 1MB 上限")
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if not isinstance(payload, dict):
                    raise ValueError("知识库配置必须是对象")
                self._configs = {}
                for kb_id, config in payload.items():
                    if (
                        self._valid_kb_id(kb_id)
                        and isinstance(config, dict)
                        and config.get("kb_id") == kb_id
                    ):
                        # Validate the persisted shape before it can influence
                        # filesystem paths or collection names.
                        KnowledgeBase(**config)
                        self._configs[kb_id] = config
                    else:
                        logger.warning("忽略无效的知识库配置项")
                logger.info(f"已加载 {len(self._configs)} 个知识库配置")
            except Exception as e:
                logger.error("加载配置失败: error_type=%s", type(e).__name__)
                self._configs = {}
        else:
            self._configs = {}

    def _save_configs(self):
        """原子保存知识库配置，避免并发/崩溃留下半个 JSON。"""
        try:
            ensure_real_directory(self.config_file.parent)
            validate_regular_file(self.config_file, allow_missing=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.config_file.name}.",
                suffix=".tmp",
                dir=self.config_file.parent,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._configs, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, self.config_file)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except Exception as e:
            logger.error("保存配置失败: error_type=%s", type(e).__name__)
            raise

    @staticmethod
    def _valid_kb_id(kb_id: str) -> bool:
        return (
            isinstance(kb_id, str)
            and re.fullmatch(r"kb_(?:[0-9a-f]{8}|[0-9a-f]{32})", kb_id) is not None
        )

    @staticmethod
    def _valid_doc_id(doc_id: str) -> bool:
        return (
            isinstance(doc_id, str)
            and re.fullmatch(r"doc_(?:[0-9a-f]{8}|[0-9a-f]{32})", doc_id) is not None
        )

    def _get_kb_dir(self, kb_id: str) -> Path:
        """获取知识库目录"""
        if not self._valid_kb_id(kb_id):
            raise ValueError("无效知识库 ID")
        kb_dir = self.kb_dir / kb_id
        try:
            ensure_real_directory(self.kb_dir)
            kb_dir = ensure_real_directory(kb_dir)
        except UnsafeStoragePathError as exc:
            raise ValueError("知识库目录不能包含链接") from exc
        try:
            kb_dir.resolve().relative_to(self.kb_dir.resolve())
        except ValueError as exc:
            raise ValueError("知识库目录超出受管根目录") from exc
        return kb_dir

    def _get_documents_dir(self, kb_id: str) -> Path:
        """获取文档存储目录"""
        docs_dir = self._get_kb_dir(kb_id) / "documents"
        try:
            return ensure_real_directory(docs_dir)
        except UnsafeStoragePathError as exc:
            raise ValueError("知识库文档目录不能包含链接") from exc

    def _get_document_metadata_dir(self, kb_id: str) -> Path:
        metadata_dir = self._get_kb_dir(kb_id) / "document_metadata"
        try:
            return ensure_real_directory(metadata_dir)
        except UnsafeStoragePathError as exc:
            raise ValueError("文档元数据目录不能包含链接") from exc

    def _document_metadata_file(self, kb_id: str, doc_id: str) -> Path:
        if not self._valid_doc_id(doc_id):
            raise ValueError("无效文档 ID")
        return self._get_document_metadata_dir(kb_id) / f"{doc_id}.json"

    def _save_document_metadata(self, document: Document) -> None:
        target = self._document_metadata_file(document.kb_id, document.doc_id)
        self._directory_size(target.parent)
        data = document.model_dump(mode="json")
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > self.METADATA_RESERVE_BYTES:
            raise ValueError("文档元数据超过 16KB 上限")
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(encoded.decode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _ensure_embedder(self):
        """确保嵌入模型已加载"""
        if self.embedder is None:
            with self._lock:
                if self.embedder is None:
                    self.embedder = Embedder()

    def _kb_lock(self, kb_id: str) -> threading.RLock:
        with self._lock:
            return self._kb_locks.setdefault(kb_id, threading.RLock())

    def _directory_size(self, path: Path, stop_after: Optional[int] = None) -> int:
        """Measure a managed tree without following symlinks."""
        if path.is_symlink():
            raise ValueError("知识库目录不能是软链接")
        if not path.exists():
            return 0
        total = 0
        seen_entries = 0
        pending = [path]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    seen_entries += 1
                    if seen_entries > self.MAX_DIRECTORY_ENTRIES:
                        raise KnowledgeBaseQuotaError(
                            "知识库目录项数量超过 "
                            f"{self.MAX_DIRECTORY_ENTRIES} 上限"
                        )
                    if entry.is_symlink():
                        raise ValueError("知识库目录内不允许软链接")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        if stop_after is not None and total > stop_after:
                            return total
        return total

    def _assert_storage_capacity(
        self, kb_id: Optional[str], additional_bytes: int = 0
    ) -> None:
        """Enforce per-KB and deployment-wide on-disk byte ceilings."""
        additional_bytes = max(0, int(additional_bytes))
        global_size = self._directory_size(
            self.kb_dir,
            stop_after=max(0, self.MAX_TOTAL_DIRECTORY_BYTES - additional_bytes),
        )
        if global_size + additional_bytes > self.MAX_TOTAL_DIRECTORY_BYTES:
            raise KnowledgeBaseQuotaError(
                f"知识库总目录容量超过 {self.MAX_TOTAL_DIRECTORY_BYTES} 字节上限"
            )
        if kb_id is None:
            return
        kb_path = self.kb_dir / kb_id
        kb_size = self._directory_size(
            kb_path,
            stop_after=max(0, self.MAX_DIRECTORY_BYTES_PER_KB - additional_bytes),
        )
        if kb_size + additional_bytes > self.MAX_DIRECTORY_BYTES_PER_KB:
            raise KnowledgeBaseQuotaError(
                f"单个知识库目录容量超过 {self.MAX_DIRECTORY_BYTES_PER_KB} 字节上限"
            )

    def _document_metadata_counts(self, kb_id: str) -> tuple[int, int, set[str]]:
        """Return active/failed counts and IDs persisted as metadata."""
        metadata_dir = self.kb_dir / kb_id / "document_metadata"
        if not metadata_dir.exists():
            return 0, 0, set()
        if metadata_dir.is_symlink():
            raise ValueError("文档元数据目录不能是软链接")
        self._directory_size(metadata_dir)
        active = 0
        failed = 0
        document_ids: set[str] = set()
        for metadata_file in metadata_dir.glob("doc_*.json"):
            if self._valid_doc_id(metadata_file.stem):
                document_ids.add(metadata_file.stem)
            if metadata_file.is_symlink() or not metadata_file.is_file():
                failed += 1
                continue
            try:
                if (
                    metadata_file.stat(follow_symlinks=False).st_size
                    > self.METADATA_RESERVE_BYTES
                ):
                    failed += 1
                    continue
                payload = json.loads(metadata_file.read_text(encoding="utf-8"))
                if payload.get("status") == DocumentStatus.FAILED.value:
                    failed += 1
                else:
                    active += 1
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Corrupt records consume the smaller failure budget rather than
                # creating a quota bypass that can grow metadata without bound.
                failed += 1
        return active, failed, document_ids

    def _assert_chunk_capacity(self, kb_id: str, incoming_chunks: int) -> None:
        incoming_chunks = max(0, int(incoming_chunks))
        with self._lock:
            configured = {
                item_id: max(0, int(config.get("chunk_count", 0) or 0))
                for item_id, config in self._configs.items()
            }
            loaded_collections = dict(self._collections)
        current_collection = self._get_collection(kb_id)
        current_chunks = max(0, int(current_collection.count()))
        if current_chunks + incoming_chunks > self.MAX_CHUNKS_PER_KB:
            raise KnowledgeBaseQuotaError(
                f"单个知识库分块总数超过 {self.MAX_CHUNKS_PER_KB} 上限"
            )
        actual_or_configured = dict(configured)
        for item_id, collection in loaded_collections.items():
            if item_id in actual_or_configured:
                actual_or_configured[item_id] = max(0, int(collection.count()))
        actual_or_configured[kb_id] = current_chunks
        if sum(actual_or_configured.values()) + incoming_chunks > self.MAX_TOTAL_CHUNKS:
            raise KnowledgeBaseQuotaError(
                f"知识库分块总数超过 {self.MAX_TOTAL_CHUNKS} 上限"
            )

    def _estimated_vector_growth(self, chunks: List[Chunk]) -> int:
        """Conservatively reserve vector/text/index bytes before Chroma writes."""
        dimension = int(getattr(self.embedder, "dimension", 512) or 512)
        raw_vectors = len(chunks) * max(1, dimension) * 4
        text_bytes = sum(len(chunk.content.encode("utf-8")) for chunk in chunks)
        overhead = len(chunks) * self.VECTOR_OVERHEAD_BYTES_PER_CHUNK
        return raw_vectors + text_bytes + overhead

    def create_kb(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "BAAI/bge-small-zh-v1.5"
    ) -> KnowledgeBase:
        """创建知识库

        Args:
            name: 知识库名称
            description: 知识库描述
            embedding_model: 嵌入模型名称

        Returns:
            KnowledgeBase: 创建的知识库对象
        """
        with self._resource_lock:
            with self._lock:
                self._ensure_embedder()
                if embedding_model != self.embedder.model_name:
                    raise ValueError(
                        f"当前部署仅支持嵌入模型 {self.embedder.model_name}"
                    )
                if len(self._configs) >= self.MAX_KNOWLEDGE_BASES:
                    raise KnowledgeBaseQuotaError(
                        f"知识库数量已达到 {self.MAX_KNOWLEDGE_BASES} 个上限"
                    )
                self._assert_storage_capacity(None)
                # 检查名称重复
                for kb in self._configs.values():
                    if kb["name"] == name:
                        raise ValueError(f"知识库名称已存在: {name}")

                kb_id = f"kb_{uuid.uuid4().hex}"
                now = datetime.now().isoformat()

                kb_config = {
                    "kb_id": kb_id,
                    "name": name,
                    "description": description,
                    "embedding_model": embedding_model,
                    "created_at": now,
                    "updated_at": now,
                    "doc_count": 0,
                    "chunk_count": 0,
                    "total_size": 0
                }

                self._configs[kb_id] = kb_config
                self._save_configs()

                # 创建目录结构。知识库配置仅保存一份，避免每次更新双写。
                self._get_kb_dir(kb_id)
                self._get_documents_dir(kb_id)

            try:
                # 创建 ChromaDB 集合（初始化向量数据库）
                self._get_collection(kb_id)
                self._assert_storage_capacity(kb_id)
            except Exception:
                # Failed initialization must not consume a quota slot or leave a
                # same-name ghost that prevents a clean retry.
                with self._lock:
                    self._collections.pop(kb_id, None)
                    self._clients.pop(kb_id, None)
                    self._configs.pop(kb_id, None)
                    self._save_configs()
                shutil.rmtree(self.kb_dir / kb_id, ignore_errors=True)
                raise

            logger.info("知识库创建成功: %s", kb_id)
            return KnowledgeBase(**kb_config)

    def delete_kb(self, kb_id: str) -> bool:
        """删除知识库

        删除向量集合、元数据和所有文档

        Args:
            kb_id: 知识库 ID

        Returns:
            bool: 是否删除成功
        """
        if not self._valid_kb_id(kb_id):
            return False
        try:
            with self._resource_lock, self._kb_lock(kb_id), self._lock:
                if kb_id not in self._configs:
                    logger.warning(f"知识库不存在: {kb_id}")
                    return False
                # 删除目录
                kb_dir = self.kb_dir / kb_id
                if kb_dir.exists():
                    self._directory_size(kb_dir)
                    shutil.rmtree(kb_dir)

                # 删除配置
                del self._configs[kb_id]
                self._save_configs()
                self._collections.pop(kb_id, None)
                self._clients.pop(kb_id, None)
                self._kb_locks.pop(kb_id, None)

            logger.info(f"知识库已删除: {kb_id}")
            return True

        except Exception as e:
            logger.error("删除知识库失败: error_type=%s", type(e).__name__)
            return False

    def list_kb(self) -> List[KnowledgeBase]:
        """列出所有知识库

        Returns:
            List[KnowledgeBase]: 知识库列表
        """
        # 读取接口不得触发 Chroma 查询或磁盘写入；统计在增删事务后刷新。
        with self._lock:
            return [KnowledgeBase(**dict(config)) for config in self._configs.values()]

    def get_kb(self, kb_id: str) -> Optional[KnowledgeBase]:
        """获取知识库

        Args:
            kb_id: 知识库 ID

        Returns:
            Optional[KnowledgeBase]: 知识库对象，不存在返回 None
        """
        with self._lock:
            if not self._valid_kb_id(kb_id) or kb_id not in self._configs:
                return None
            return KnowledgeBase(**dict(self._configs[kb_id]))

    def _refresh_kb_stats(self, kb_id: str) -> Dict[str, Any]:
        """在写事务结束后刷新统计；只有值改变时才持久化。"""
        if not self._valid_kb_id(kb_id):
            return {}
        with self._kb_lock(kb_id):
            with self._lock:
                if kb_id not in self._configs:
                    return {}
                kb_config = dict(self._configs[kb_id])

            try:
                # 计算文档数和总大小
                docs_dir = self._get_documents_dir(kb_id)
                self._directory_size(docs_dir)
                doc_count = 0
                total_size = 0

                for file_path in docs_dir.iterdir():
                    if file_path.is_symlink():
                        raise ValueError("知识库文档目录内不允许软链接")
                    if file_path.is_file():
                        doc_count += 1
                        total_size += file_path.stat().st_size

                # 获取文档块数
                collection = self._get_collection(kb_id)
                chunk_count = collection.count()

                kb_config["doc_count"] = doc_count
                kb_config["chunk_count"] = chunk_count
                kb_config["total_size"] = total_size

                with self._lock:
                    if kb_id in self._configs and kb_config != self._configs[kb_id]:
                        self._configs[kb_id] = kb_config
                        self._save_configs()

            except Exception as exc:
                logger.warning(
                    "更新知识库统计信息失败: error_type=%s",
                    type(exc).__name__,
                )

            return kb_config

    def _get_collection(self, kb_id: str):
        """获取知识库的向量集合

        Args:
            kb_id: 知识库 ID

        Returns:
            chromadb.Collection: ChromaDB 集合
        """
        if not self._valid_kb_id(kb_id):
            raise ValueError("无效知识库 ID")
        with self._lock:
            if kb_id not in self._configs:
                raise ValueError(f"知识库不存在: {kb_id}")
            if kb_id in self._collections:
                return self._collections[kb_id]

        try:
            import chromadb

            with self._lock:
                if kb_id not in self._configs:
                    raise ValueError(f"知识库不存在: {kb_id}")
                if kb_id in self._collections:
                    return self._collections[kb_id]
                vectordb_dir = self._get_kb_dir(kb_id) / "vectordb"
                try:
                    vectordb_dir = ensure_real_directory(vectordb_dir)
                except UnsafeStoragePathError as exc:
                    raise ValueError("知识库向量目录不能包含链接") from exc
                # PersistentClient may recursively open its existing tree; reject
                # any planted link before handing that path to Chroma.
                self._directory_size(vectordb_dir)

                client = chromadb.PersistentClient(path=str(vectordb_dir))
                collection = client.get_or_create_collection(
                    name="documents",
                    metadata={"hnsw:space": "cosine"}
                )

                self._clients[kb_id] = client
                self._collections[kb_id] = collection
                return collection

        except ImportError as e:
            raise ImportError(
                "chromadb 未安装。请运行: pip install chromadb"
            ) from e
        except Exception as e:
            logger.error("获取向量集合失败: error_type=%s", type(e).__name__)
            raise

    def add_document(
        self,
        kb_id: str,
        file_path: Path,
        filename: str
    ) -> Document:
        """添加文档到知识库

        处理流程：验证 → 解析 → 分块 → 向量化 → 存储

        Args:
            kb_id: 知识库 ID
            file_path: 文件路径（临时文件）
            filename: 原始文件名

        Returns:
            Document: 文档元数据对象
        """
        if not self._valid_kb_id(kb_id):
            raise ValueError("无效知识库 ID")

        doc_id = f"doc_{uuid.uuid4().hex}"
        safe_filename = PurePosixPath(filename.replace("\\", "/")).name
        if (
            not safe_filename
            or safe_filename in {".", ".."}
            or len(safe_filename) > 255
            or any(ord(character) < 32 for character in safe_filename)
        ):
            raise ValueError("无效文件名")
        file_path = Path(file_path)
        if not file_path.is_file() or file_path.is_symlink():
            raise ValueError("文档源文件无效")
        file_size = file_path.stat().st_size
        if file_size > self.MAX_DOCUMENT_BYTES:
            raise ValueError("文档超过 10MB 上限")
        mime_type = self._get_mime_type(safe_filename)

        # 创建文档记录
        document = Document(
            doc_id=doc_id,
            kb_id=kb_id,
            filename=safe_filename,
            file_size=file_size,
            file_path="",
            mime_type=mime_type,
            status=DocumentStatus.PROCESSING
        )

        target_path: Optional[Path] = None
        with self._resource_lock, self._kb_lock(kb_id):
            with self._lock:
                if kb_id not in self._configs:
                    raise ValueError(f"知识库不存在: {kb_id}")

            # This scan is also the fail-closed symlink/entry-count validation
            # for every managed KB subtree before any source path is inspected.
            self._assert_storage_capacity(
                kb_id, file_size + self.METADATA_RESERVE_BYTES
            )
            docs_dir = self._get_documents_dir(kb_id)
            existing_files = []
            for path in docs_dir.iterdir():
                if path.is_symlink():
                    raise ValueError("知识库文档目录内不允许软链接")
                if path.is_file():
                    existing_files.append(path)
            active_metadata, failed_metadata, metadata_ids = (
                self._document_metadata_counts(kb_id)
            )
            legacy_or_unindexed_sources = 0
            for source_path in existing_files:
                match = re.match(
                    r"^(doc_(?:[0-9a-f]{8}|[0-9a-f]{32}))_", source_path.name
                )
                if match is None or match.group(1) not in metadata_ids:
                    legacy_or_unindexed_sources += 1
            persisted_documents = (
                active_metadata + failed_metadata + legacy_or_unindexed_sources
            )
            if persisted_documents >= self.MAX_DOCUMENTS_PER_KB:
                raise KnowledgeBaseQuotaError(
                    f"知识库文档数量已达到 {self.MAX_DOCUMENTS_PER_KB} 个上限"
                )
            if failed_metadata >= self.MAX_FAILED_DOCUMENTS_PER_KB:
                raise KnowledgeBaseQuotaError(
                    "知识库失败文档记录已达到 "
                    f"{self.MAX_FAILED_DOCUMENTS_PER_KB} 个上限，请先删除失败记录"
                )
            existing_bytes = sum(path.stat().st_size for path in existing_files)
            if existing_bytes + file_size > self.MAX_SOURCE_BYTES_PER_KB:
                raise KnowledgeBaseQuotaError("知识库源文档总量超过 1GB 上限")

            try:
                # 1. 复制文件到知识库目录
                docs_dir = self._get_documents_dir(kb_id)
                target_path = docs_dir / f"{doc_id}_{safe_filename}"
                shutil.copyfile(file_path, target_path)
                os.chmod(target_path, 0o600)
                document.file_path = str(target_path)

                # 2. 解析并分块
                text, chunks = self.processor.process(target_path, doc_id)
                document.char_count = len(text)
                document.chunk_count = len(chunks)

                # Reject before embedding so an over-quota request cannot leave
                # a partially grown HNSW index behind.
                self._assert_chunk_capacity(kb_id, len(chunks))
                self._assert_storage_capacity(
                    kb_id,
                    self._estimated_vector_growth(chunks)
                    + self.METADATA_RESERVE_BYTES,
                )

                # 3. 向量化并存储
                self._embed_and_store(kb_id, doc_id, safe_filename, chunks)
                self._assert_storage_capacity(kb_id, self.METADATA_RESERVE_BYTES)

                # 4. 更新状态
                document.status = DocumentStatus.READY
                document.processed_at = datetime.now().isoformat()

                logger.info(
                    "文档处理完成: %s (%d 块)",
                    doc_id,
                    len(chunks),
                )

            except Exception as exc:
                document.status = DocumentStatus.FAILED
                document.error_message = (
                    f"文档处理失败 ({type(exc).__name__})"
                )
                logger.error(
                    "文档处理失败: doc_id=%s error_type=%s",
                    doc_id,
                    type(exc).__name__,
                )
                try:
                    collection = self._collections.get(kb_id)
                    if collection is not None:
                        collection.delete(where={"doc_id": doc_id})
                except Exception:
                    logger.warning("清理失败文档的部分向量失败: %s", doc_id)
                if target_path is not None:
                    target_path.unlink(missing_ok=True)
                document.file_path = ""

                if isinstance(exc, KnowledgeBaseQuotaError):
                    self._refresh_kb_stats(kb_id)
                    raise

            # Keep metadata and statistics in the same per-KB transaction so a
            # concurrent delete cannot be followed by a late write that
            # resurrects the directory.
            self._save_document_metadata(document)
            self._refresh_kb_stats(kb_id)
        return document

    def _get_mime_type(self, filename: str) -> str:
        """获取文件的 MIME 类型"""
        suffix = Path(filename).suffix.lower()
        mime_types = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".txt": "text/plain",
            ".md": "text/markdown"
        }
        return mime_types.get(suffix, "application/octet-stream")

    def _embed_and_store(
        self,
        kb_id: str,
        doc_id: str,
        filename: str,
        chunks: List[Chunk]
    ):
        """向量化并存储文档块

        Args:
            kb_id: 知识库 ID
            doc_id: 文档 ID
            filename: 文件名
            chunks: 文档块列表
        """
        self._ensure_embedder()

        collection = self._get_collection(kb_id)
        batch_size = 64
        for offset in range(0, len(chunks), batch_size):
            batch = chunks[offset:offset + batch_size]
            texts = [chunk.content for chunk in batch]
            embeddings = self.embedder.encode(texts, batch_size=batch_size)
            collection.add(
                ids=[chunk.chunk_id for chunk in batch],
                documents=texts,
                embeddings=embeddings,
                metadatas=[
                    {
                        "doc_id": doc_id,
                        "chunk_index": chunk.chunk_index,
                        "filename": filename,
                        "chunk_length": len(chunk.content),
                    }
                    for chunk in batch
                ],
            )

        logger.info(f"向量存储完成: {len(chunks)} 块")

    def list_documents(self, kb_id: str) -> List[Document]:
        """列出知识库中的所有文档

        Args:
            kb_id: 知识库 ID

        Returns:
            List[Document]: 文档列表
        """
        if not self._valid_kb_id(kb_id):
            return []

        with self._kb_lock(kb_id):
            with self._lock:
                if kb_id not in self._configs:
                    return []

            documents = []
            metadata_dir = self.kb_dir / kb_id / "document_metadata"
            if metadata_dir.exists():
                self._directory_size(metadata_dir)
                for metadata_file in sorted(metadata_dir.glob("doc_*.json")):
                    if metadata_file.is_symlink():
                        raise ValueError("文档元数据目录内不允许软链接")
                    try:
                        if (
                            metadata_file.stat(follow_symlinks=False).st_size
                            > self.METADATA_RESERVE_BYTES
                        ):
                            raise ValueError("文档元数据超过 16KB 上限")
                        with open(metadata_file, "r", encoding="utf-8") as f:
                            documents.append(Document(**json.load(f)))
                    except Exception as exc:
                        logger.warning(
                            "忽略损坏的文档元数据: error_type=%s",
                            type(exc).__name__,
                        )

            if documents:
                return documents

            # 兼容旧数据：只读重建视图，不在 GET 路径写回元数据。
            docs_dir = self.kb_dir / kb_id / "documents"
            if not docs_dir.exists():
                return []
            self._directory_size(docs_dir)
            for file_path in docs_dir.iterdir():
                if file_path.is_symlink():
                    raise ValueError("知识库文档目录内不允许软链接")
                if not file_path.is_file():
                    continue
                stem = file_path.stem
                if not stem.startswith("doc_"):
                    continue
                underscore_pos = stem.find("_", 4)
                if underscore_pos <= 0:
                    continue
                doc_id = stem[:underscore_pos]
                if not self._valid_doc_id(doc_id):
                    continue
                original_filename = stem[underscore_pos + 1:] + file_path.suffix
                documents.append(Document(
                    doc_id=doc_id,
                    kb_id=kb_id,
                    filename=original_filename,
                    file_size=file_path.stat().st_size,
                    file_path=str(file_path),
                    mime_type=self._get_mime_type(original_filename),
                    chunk_count=0,
                    status=DocumentStatus.READY
                ))

            return documents

    def delete_document(self, kb_id: str, doc_id: str) -> bool:
        """从知识库中删除文档

        Args:
            kb_id: 知识库 ID
            doc_id: 文档 ID

        Returns:
            bool: 是否删除成功
        """
        if not self._valid_kb_id(kb_id) or not self._valid_doc_id(doc_id):
            return False

        try:
            with self._resource_lock, self._kb_lock(kb_id):
                with self._lock:
                    if kb_id not in self._configs:
                        return False
                self._directory_size(self.kb_dir / kb_id)
                # doc_id is a strict generated identifier, so this glob cannot
                # inject metacharacters or escape the documents directory.
                docs_dir = self._get_documents_dir(kb_id)
                matching_files = list(docs_dir.glob(f"{doc_id}_*"))
                metadata_file = self._document_metadata_file(kb_id, doc_id)
                if not matching_files and not metadata_file.exists():
                    return False
                for file_path in matching_files:
                    file_path.unlink()

                collection = self._get_collection(kb_id)
                collection.delete(where={"doc_id": doc_id})
                metadata_file.unlink(missing_ok=True)
                self._refresh_kb_stats(kb_id)

            logger.info(f"文档已删除: {doc_id}")
            return True

        except Exception as e:
            logger.error("删除文档失败: error_type=%s", type(e).__name__)
            return False

    def get_retriever(self, kb_id: str):
        """获取知识库的检索器

        Args:
            kb_id: 知识库 ID

        Returns:
            Retriever: 检索器实例
        """
        if not self._valid_kb_id(kb_id):
            raise ValueError("无效知识库 ID")

        from src.retriever import Retriever

        with self._kb_lock(kb_id):
            with self._lock:
                if kb_id not in self._configs:
                    raise ValueError(f"知识库不存在: {kb_id}")
            self._ensure_embedder()
            collection = self._get_collection(kb_id)

            return Retriever(collection, self.embedder)

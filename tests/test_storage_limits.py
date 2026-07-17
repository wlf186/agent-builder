"""Offline regressions for storage quotas, identifiers, and path isolation."""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.execution_engine import ExecutionEngine, ExecutionError
from src.environment_manager import EnvironmentError
from src.file_storage_manager import FileStorageError, FileStorageManager
from src.knowledge_base_manager import (
    KnowledgeBaseManager,
    KnowledgeBaseQuotaError,
)
from src.models import (
    Chunk,
    Document,
    DocumentStatus,
    ExecutionRecord,
    ExecutionStatus,
    FileInfo,
)
from src.retriever import Retriever


class _Upload:
    def __init__(self, content: bytes, barrier: asyncio.Barrier | None = None):
        self.content = content
        self.barrier = barrier
        self.sent = False

    async def read(self, _size: int) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        if self.barrier is not None:
            await self.barrier.wait()
        return self.content


class _FakeEmbedder:
    model_name = "BAAI/bge-small-zh-v1.5"

    def encode(self, texts, **_kwargs):
        return [[float(index), 1.0] for index, _text in enumerate(texts)]

    def encode_single(self, _text):
        return [0.0, 1.0]


class _FakeCollection:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def add(self, *, ids, documents, embeddings, metadatas):
        for item_id, document, embedding, metadata in zip(
            ids, documents, embeddings, metadatas
        ):
            self.rows[item_id] = {
                "document": document,
                "embedding": embedding,
                "metadata": metadata,
            }

    def delete(self, *, where):
        doc_id = where.get("doc_id")
        self.rows = {
            key: value
            for key, value in self.rows.items()
            if value["metadata"].get("doc_id") != doc_id
        }

    def count(self):
        return len(self.rows)


class _FakeProcessor:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure

    def process(self, path: Path, doc_id: str):
        if self.failure is not None:
            raise self.failure
        text = path.read_text(encoding="utf-8")
        return text, [
            Chunk(
                chunk_id=f"{doc_id}_0",
                doc_id=doc_id,
                content=text,
                chunk_index=0,
                start_pos=0,
                end_pos=len(text),
            )
        ]


def _knowledge_base_manager(
    root: Path, *, processor: _FakeProcessor | None = None
) -> tuple[KnowledgeBaseManager, _FakeCollection]:
    collection = _FakeCollection()
    manager = KnowledgeBaseManager(root, embedder=_FakeEmbedder())
    manager.processor = processor or _FakeProcessor()
    manager._get_collection = lambda _kb_id: collection
    return manager, collection


class FileStorageLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = FileStorageManager(self.root / "files")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_new_ids_are_32_hex_and_legacy_8_hex_records_still_work(self):
        generated = await self.manager.upload_file("agent", b"new", "new.txt")
        self.assertRegex(generated.file_id, r"^[0-9a-f]{32}$")

        legacy_id = "deadbeef"
        legacy_dir = self.manager.get_agent_storage_path("legacy")
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / f"{legacy_id}.txt"
        legacy_path.write_bytes(b"legacy")
        self.manager._add_file_to_index(
            "legacy",
            FileInfo(
                file_id=legacy_id,
                agent_name="legacy",
                filename="legacy.txt",
                file_size=6,
                mime_type="text/plain",
                checksum="unused",
                file_path=str(legacy_path.relative_to(self.manager.storage_dir)),
            ),
        )
        self.assertEqual(
            await self.manager.get_file_content("legacy", legacy_id), b"legacy"
        )
        self.assertTrue(await self.manager.delete_file("legacy", legacy_id))
        self.assertFalse(legacy_path.exists())

    async def test_colliding_legacy_agent_slugs_remain_isolated(self):
        first_agent = "team/a"
        second_agent = "team\\a"
        first = await self.manager.upload_file(first_agent, b"first", "same.txt")
        second = await self.manager.upload_file(second_agent, b"second", "same.txt")

        first_dir = self.manager.get_agent_storage_path(first_agent)
        second_dir = self.manager.get_agent_storage_path(second_agent)
        self.assertNotEqual(first_dir, second_dir)
        first_dir.resolve().relative_to(self.manager.storage_dir.resolve())
        second_dir.resolve().relative_to(self.manager.storage_dir.resolve())
        self.assertNotEqual(
            self.manager.get_metadata_path(first_agent),
            self.manager.get_metadata_path(second_agent),
        )
        self.assertEqual(
            await self.manager.get_file_content(first_agent, first.file_id), b"first"
        )
        self.assertEqual(
            await self.manager.get_file_content(second_agent, second.file_id), b"second"
        )

    async def test_file_count_and_total_byte_quotas_leave_no_partial_files(self):
        with patch.object(self.manager, "MAX_FILES_PER_AGENT", 1):
            await self.manager.upload_file("count", b"one", "one.txt")
            with self.assertRaises(FileStorageError):
                await self.manager.upload_file("count", b"two", "two.txt")
            self.assertEqual(len(await self.manager.list_files("count")), 1)

        with patch.object(self.manager, "MAX_TOTAL_BYTES_PER_AGENT", 5):
            await self.manager.upload_file("bytes", b"123", "one.txt")
            with self.assertRaises(FileStorageError):
                await self.manager.upload_file("bytes", b"456", "two.txt")
            stored = await self.manager.list_files("bytes")
            self.assertEqual(len(stored), 1)
            self.assertEqual(sum(item.file_size for item in stored), 3)

        for agent_name in ("count", "bytes"):
            agent_dir = self.manager.get_agent_storage_path(agent_name)
            self.assertFalse(any(path.name.endswith(".uploading") for path in agent_dir.iterdir()))

    async def test_concurrent_stream_commits_cannot_oversubscribe_quota(self):
        barrier = asyncio.Barrier(2)
        with (
            patch.object(self.manager, "MAX_FILES_PER_AGENT", 1),
            patch.object(self.manager, "MAX_TOTAL_BYTES_PER_AGENT", 4),
        ):
            results = await asyncio.gather(
                self.manager.upload_stream(
                    "concurrent", _Upload(b"1234", barrier), "first.txt"
                ),
                self.manager.upload_stream(
                    "concurrent", _Upload(b"5678", barrier), "second.txt"
                ),
                return_exceptions=True,
            )

        successes = [item for item in results if isinstance(item, FileInfo)]
        failures = [item for item in results if isinstance(item, FileStorageError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        committed = await self.manager.list_files("concurrent")
        self.assertEqual(len(committed), 1)
        self.assertEqual(sum(item.file_size for item in committed), 4)
        agent_dir = self.manager.get_agent_storage_path("concurrent")
        self.assertEqual(len([path for path in agent_dir.iterdir() if path.is_file()]), 1)
        self.assertFalse(any(path.name.endswith(".uploading") for path in agent_dir.iterdir()))


class KnowledgeBaseLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_new_and_legacy_kb_and_document_id_shapes_are_supported(self):
        manager, _collection = _knowledge_base_manager(self.root / "ids")
        kb = manager.create_kb("generated")
        self.assertRegex(kb.kb_id, r"^kb_[0-9a-f]{32}$")

        document = manager.add_document(
            kb.kb_id, self._source("new.txt", "content"), "new.txt"
        )
        self.assertEqual(document.status, DocumentStatus.READY)
        self.assertRegex(document.doc_id, r"^doc_[0-9a-f]{32}$")

        legacy_kb_id = "kb_deadbeef"
        now = datetime.now().isoformat()
        manager._configs[legacy_kb_id] = {
            "kb_id": legacy_kb_id,
            "name": "legacy",
            "description": "",
            "embedding_model": _FakeEmbedder.model_name,
            "created_at": now,
            "updated_at": now,
            "doc_count": 0,
            "chunk_count": 0,
            "total_size": 0,
        }
        manager._save_configs()
        self.assertEqual(manager.get_kb(legacy_kb_id).kb_id, legacy_kb_id)
        self.assertTrue(manager._valid_doc_id("doc_cafebabe"))
        self.assertTrue(manager._valid_doc_id(document.doc_id))

    def test_document_count_and_total_source_byte_quotas(self):
        count_manager, _ = _knowledge_base_manager(self.root / "count")
        count_kb = count_manager.create_kb("count")
        with patch.object(count_manager, "MAX_DOCUMENTS_PER_KB", 1):
            first = count_manager.add_document(
                count_kb.kb_id,
                self._source("count-one.txt", "one"),
                "one.txt",
            )
            self.assertEqual(first.status, DocumentStatus.READY)
            with self.assertRaises(ValueError):
                count_manager.add_document(
                    count_kb.kb_id,
                    self._source("count-two.txt", "two"),
                    "two.txt",
                )
        self.assertEqual(
            len(list(count_manager._get_documents_dir(count_kb.kb_id).iterdir())), 1
        )

        byte_manager, _ = _knowledge_base_manager(self.root / "bytes")
        byte_kb = byte_manager.create_kb("bytes")
        with patch.object(byte_manager, "MAX_SOURCE_BYTES_PER_KB", 5):
            first = byte_manager.add_document(
                byte_kb.kb_id,
                self._source("byte-one.txt", "123"),
                "one.txt",
            )
            self.assertEqual(first.status, DocumentStatus.READY)
            with self.assertRaises(ValueError):
                byte_manager.add_document(
                    byte_kb.kb_id,
                    self._source("byte-two.txt", "456"),
                    "two.txt",
                )
        stored = list(byte_manager._get_documents_dir(byte_kb.kb_id).iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(sum(path.stat().st_size for path in stored), 3)

    def test_global_kb_limit_rejects_without_config_or_directory_residue(self):
        manager, _collection = _knowledge_base_manager(self.root / "kb-count")
        with patch.object(manager, "MAX_KNOWLEDGE_BASES", 1):
            first = manager.create_kb("first")
            before_dirs = sorted(path.name for path in manager.kb_dir.glob("kb_*"))
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "1 个上限"):
                manager.create_kb("second")

        self.assertEqual(list(manager._configs), [first.kb_id])
        self.assertEqual(
            sorted(path.name for path in manager.kb_dir.glob("kb_*")), before_dirs
        )

    def test_collection_initialization_failure_rolls_back_and_allows_retry(self):
        manager = KnowledgeBaseManager(
            self.root / "kb-init-failure", embedder=_FakeEmbedder()
        )

        def fail_collection(_kb_id):
            raise RuntimeError("chroma unavailable")

        manager._get_collection = fail_collection
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            manager.create_kb("retryable")
        self.assertEqual(manager._configs, {})
        self.assertEqual(list(manager.kb_dir.glob("kb_*")), [])

        collection = _FakeCollection()
        manager._get_collection = lambda _kb_id: collection
        retried = manager.create_kb("retryable")
        self.assertEqual(manager.get_kb(retried.kb_id).name, "retryable")

    def test_failed_metadata_consumes_total_and_failure_quotas(self):
        manager, _collection = _knowledge_base_manager(
            self.root / "failed-quota",
            processor=_FakeProcessor(RuntimeError("processor failed")),
        )
        kb = manager.create_kb("failed-quota")
        first_source = self._source("failed-one.txt", "one")
        second_source = self._source("failed-two.txt", "two")

        with (
            patch.object(manager, "MAX_DOCUMENTS_PER_KB", 1),
            patch.object(manager, "MAX_FAILED_DOCUMENTS_PER_KB", 10),
        ):
            first = manager.add_document(kb.kb_id, first_source, "one.txt")
            self.assertEqual(first.status, DocumentStatus.FAILED)
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "文档数量"):
                manager.add_document(kb.kb_id, second_source, "two.txt")

        metadata_dir = manager._get_document_metadata_dir(kb.kb_id)
        self.assertEqual(len(list(metadata_dir.glob("doc_*.json"))), 1)

        # The dedicated failure ceiling is independently enforced even when the
        # total document budget is larger.
        with (
            patch.object(manager, "MAX_DOCUMENTS_PER_KB", 10),
            patch.object(manager, "MAX_FAILED_DOCUMENTS_PER_KB", 1),
        ):
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "失败文档记录"):
                manager.add_document(kb.kb_id, second_source, "two.txt")

    def test_chunk_quotas_reject_before_embedding_without_partial_files(self):
        manager, collection = _knowledge_base_manager(self.root / "chunk-quota")
        kb = manager.create_kb("chunk-quota")
        source = self._source("chunk.txt", "content")

        with patch.object(manager, "MAX_CHUNKS_PER_KB", 0):
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "分块总数"):
                manager.add_document(kb.kb_id, source, "chunk.txt")

        self.assertEqual(collection.rows, {})
        self.assertEqual(list(manager._get_documents_dir(kb.kb_id).iterdir()), [])
        metadata_dir = manager.kb_dir / kb.kb_id / "document_metadata"
        self.assertFalse(metadata_dir.exists())

        # Persisted chunks in another KB count toward the deployment-wide cap.
        other_id = "kb_deadbeef"
        now = datetime.now().isoformat()
        manager._configs[other_id] = {
            "kb_id": other_id,
            "name": "other",
            "description": "",
            "embedding_model": _FakeEmbedder.model_name,
            "created_at": now,
            "updated_at": now,
            "doc_count": 1,
            "chunk_count": 1,
            "total_size": 1,
        }
        with patch.object(manager, "MAX_TOTAL_CHUNKS", 1):
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "分块总数"):
                manager.add_document(kb.kb_id, source, "chunk.txt")
        self.assertEqual(collection.rows, {})

    def test_per_kb_and_global_directory_byte_quotas_reject_before_copy(self):
        per_manager, _ = _knowledge_base_manager(self.root / "per-dir")
        per_kb = per_manager.create_kb("per-dir")
        source = self._source("per-dir.txt", "123")
        current = per_manager._directory_size(per_manager.kb_dir / per_kb.kb_id)
        with patch.object(
            per_manager,
            "MAX_DIRECTORY_BYTES_PER_KB",
            current + per_manager.METADATA_RESERVE_BYTES + 2,
        ):
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "目录容量"):
                per_manager.add_document(per_kb.kb_id, source, "per-dir.txt")
        self.assertEqual(list(per_manager._get_documents_dir(per_kb.kb_id).iterdir()), [])

        global_manager, _ = _knowledge_base_manager(self.root / "global-dir")
        global_kb = global_manager.create_kb("global-dir")
        current = global_manager._directory_size(global_manager.kb_dir)
        with patch.object(
            global_manager,
            "MAX_TOTAL_DIRECTORY_BYTES",
            current + global_manager.METADATA_RESERVE_BYTES + 2,
        ):
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "总目录容量"):
                global_manager.add_document(global_kb.kb_id, source, "global-dir.txt")
        self.assertEqual(
            list(global_manager._get_documents_dir(global_kb.kb_id).iterdir()), []
        )

    def test_managed_kb_tree_rejects_config_vectordb_and_metadata_symlinks(self):
        external_data = self.root / "external-kb-data"
        external_data.mkdir()
        linked_data = self.root / "linked-kb-data"
        linked_data.symlink_to(external_data, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "数据目录不能是软链接"):
            KnowledgeBaseManager(linked_data, embedder=_FakeEmbedder())

        config_root = self.root / "config-link"
        kb_root = config_root / "knowledge_bases"
        kb_root.mkdir(parents=True)
        external_config = self.root / "external-kbs.json"
        external_config.write_text("{}", encoding="utf-8")
        (kb_root / "knowledge_bases.json").symlink_to(external_config)
        with self.assertRaisesRegex(ValueError, "配置文件不能是软链接"):
            KnowledgeBaseManager(config_root, embedder=_FakeEmbedder())

        manager, _collection = _knowledge_base_manager(self.root / "tree-links")
        kb = manager.create_kb("tree-links")
        vectordb = manager.kb_dir / kb.kb_id / "vectordb"
        vectordb.mkdir()
        external_vector = self.root / "external-vector.bin"
        external_vector.write_bytes(b"external")
        (vectordb / "index.bin").symlink_to(external_vector)
        original_get_collection = KnowledgeBaseManager._get_collection.__get__(
            manager, KnowledgeBaseManager
        )
        with self.assertRaisesRegex(ValueError, "不允许软链接"):
            original_get_collection(kb.kb_id)

        (vectordb / "index.bin").unlink()
        metadata_dir = manager._get_document_metadata_dir(kb.kb_id)
        external_metadata = self.root / "external-document.json"
        external_metadata.write_text("{}", encoding="utf-8")
        (metadata_dir / "doc_deadbeef.json").symlink_to(external_metadata)
        with self.assertRaisesRegex(ValueError, "不允许软链接"):
            manager.list_documents(kb.kb_id)

    def test_directory_entry_limit_bounds_quota_scans(self):
        manager, _collection = _knowledge_base_manager(self.root / "entry-limit")
        kb = manager.create_kb("entry-limit")
        docs = manager._get_documents_dir(kb.kb_id)
        (docs / "one.txt").write_text("1", encoding="utf-8")
        (docs / "two.txt").write_text("2", encoding="utf-8")
        with patch.object(manager, "MAX_DIRECTORY_ENTRIES", 1):
            with self.assertRaisesRegex(KnowledgeBaseQuotaError, "目录项数量"):
                manager._directory_size(manager.kb_dir / kb.kb_id)

    def test_invalid_ids_and_glob_metacharacters_cannot_delete_files(self):
        manager, _collection = _knowledge_base_manager(self.root / "delete")
        kb = manager.create_kb("delete")
        docs_dir = manager._get_documents_dir(kb.kb_id)
        legacy_doc_id = "doc_deadbeef"
        target = docs_dir / f"{legacy_doc_id}_report.txt"
        target.write_text("delete only this", encoding="utf-8")
        keep = docs_dir / "doc_cafebabe_keep.txt"
        keep.write_text("keep", encoding="utf-8")
        manager._save_document_metadata(
            Document(
                doc_id=legacy_doc_id,
                kb_id=kb.kb_id,
                filename="report.txt",
                file_size=target.stat().st_size,
                file_path=str(target),
                mime_type="text/plain",
                status=DocumentStatus.READY,
            )
        )

        for invalid_doc_id in (
            "doc_*",
            "doc_deadbeef*",
            "doc_????????",
            "../doc_deadbeef",
            "doc_DEADBEEF",
        ):
            with self.subTest(doc_id=invalid_doc_id):
                self.assertFalse(manager.delete_document(kb.kb_id, invalid_doc_id))
                self.assertTrue(target.exists())
                self.assertTrue(keep.exists())

        self.assertFalse(manager.delete_document("kb_*", legacy_doc_id))
        self.assertFalse(manager.delete_kb("kb_*"))
        self.assertTrue(target.exists())
        self.assertTrue(keep.exists())

        self.assertTrue(manager.delete_document(kb.kb_id, legacy_doc_id))
        self.assertFalse(target.exists())
        self.assertTrue(keep.exists())

    def test_failed_document_processing_leaves_no_kb_source_file(self):
        manager, _collection = _knowledge_base_manager(
            self.root / "failure",
            processor=_FakeProcessor(RuntimeError("processor backend failed")),
        )
        kb = manager.create_kb("failure")
        original = self._source("failure-source.txt", "source")
        document = manager.add_document(kb.kb_id, original, "source.txt")

        self.assertEqual(document.status, DocumentStatus.FAILED)
        self.assertEqual(document.file_path, "")
        self.assertTrue(original.exists())
        self.assertEqual(list(manager._get_documents_dir(kb.kb_id).iterdir()), [])
        metadata = manager._document_metadata_file(kb.kb_id, document.doc_id)
        self.assertTrue(metadata.is_file())
        listed = manager.list_documents(kb.kb_id)
        self.assertEqual([item.status for item in listed], [DocumentStatus.FAILED])


class _BackendFailure(RuntimeError):
    pass


class _FailingCollection:
    def query(self, **_kwargs):
        raise _BackendFailure("vector backend unavailable")

    def count(self):
        raise _BackendFailure("vector backend unavailable")


class RetrieverFailureTests(unittest.TestCase):
    def test_backend_failures_are_raised_instead_of_becoming_empty_results(self):
        retriever = Retriever(_FailingCollection(), _FakeEmbedder())
        with self.assertRaisesRegex(_BackendFailure, "unavailable"):
            retriever.search("query")
        with self.assertRaisesRegex(_BackendFailure, "unavailable"):
            retriever.search_with_embeddings([0.0, 1.0])
        with self.assertRaisesRegex(_BackendFailure, "unavailable"):
            retriever.get_collection_size()


class _ExecutionRuntime:
    def __init__(self):
        self.calls = []

    async def get_or_create_environment(self, _agent_name):
        return object()

    async def install_skill_dependencies(self, **_kwargs):
        return True, "ok", []

    async def execute_in_environment(self, **kwargs):
        self.calls.append(kwargs)
        return 0, "ok", "", 1

    async def cancel_process(self, _execution_id):
        return False


class _ExecutionFileStorage:
    async def copy_file_to_workdir(self, **_kwargs):
        raise AssertionError("test does not provide input files")


class ExecutionStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment_patch = patch.dict(
            os.environ,
            {"AGENT_BUILDER_RUNTIME_DIR": str(self.root / ".runtime")},
        )
        self.environment_patch.start()
        self.runtime = _ExecutionRuntime()
        self.engine = ExecutionEngine(
            self.runtime, _ExecutionFileStorage(), self.root / "data"
        )

    async def asyncTearDown(self) -> None:
        self.environment_patch.stop()
        self.temporary.cleanup()

    async def test_generated_id_is_32_hex_and_legacy_8_hex_record_is_readable(self):
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "main.py").write_text("print('ok')\n", encoding="utf-8")
        generated = await self.engine.execute_script(
            agent_name="agent",
            skill_name="skill",
            script_path="main.py",
            skill_base_path=str(skill),
        )
        self.assertEqual(generated.status, ExecutionStatus.SUCCESS)
        self.assertRegex(generated.execution_id, r"^[0-9a-f]{32}$")
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(list(self.engine.work_root.iterdir()), [])

        legacy = ExecutionRecord(
            execution_id="deadbeef",
            agent_name="agent",
            skill_name="legacy",
            script_path="legacy.py",
            status=ExecutionStatus.SUCCESS,
        )
        self.engine._save_record(legacy)
        loaded = await self.engine.get_execution_status("agent", "deadbeef")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.execution_id, "deadbeef")

    async def test_execution_paths_reject_injection_and_isolate_agent_collisions(self):
        first_agent = "team/a"
        second_agent = "team\\a"
        first_dir = self.engine.get_executions_dir(first_agent)
        second_dir = self.engine.get_executions_dir(second_agent)
        self.assertNotEqual(first_dir, second_dir)
        first_dir.resolve().relative_to(self.engine.executions_dir.resolve())
        second_dir.resolve().relative_to(self.engine.executions_dir.resolve())

        shared_legacy_id = "cafebabe"
        for agent_name in (first_agent, second_agent):
            self.engine._save_record(
                ExecutionRecord(
                    execution_id=shared_legacy_id,
                    agent_name=agent_name,
                    skill_name="skill",
                    script_path="main.py",
                    status=ExecutionStatus.SUCCESS,
                )
            )
        first_path = self.engine.get_execution_path(first_agent, shared_legacy_id)
        second_path = self.engine.get_execution_path(second_agent, shared_legacy_id)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(
            (await self.engine.get_execution_status(first_agent, shared_legacy_id)).agent_name,
            first_agent,
        )
        self.assertEqual(
            (await self.engine.get_execution_status(second_agent, shared_legacy_id)).agent_name,
            second_agent,
        )

        valid_32 = "a" * 32
        self.assertEqual(
            self.engine.get_execution_path(first_agent, valid_32).name,
            f"{valid_32}.json",
        )
        for invalid_id in (
            "*",
            "../cafebabe",
            "cafebabe*",
            "cafebabe.json",
            "CAFEBABE",
            "short",
        ):
            with self.subTest(execution_id=invalid_id):
                with self.assertRaises(ExecutionError):
                    self.engine.get_execution_path(first_agent, invalid_id)
                self.assertIsNone(
                    await self.engine.get_execution_status(first_agent, invalid_id)
                )
        self.assertTrue(first_path.exists())
        self.assertTrue(second_path.exists())

    async def test_internal_exceptions_are_not_persisted_or_returned(self):
        skill = self.root / "private-error-skill"
        skill.mkdir()
        (skill / "main.py").write_text("print('ok')\n", encoding="utf-8")
        environment_secret = (
            "https://" + "user" + ":" + "not-a-real-secret" + "@private.invalid/internal"
        )

        async def fail_environment(_agent_name):
            raise EnvironmentError(environment_secret)

        self.runtime.get_or_create_environment = fail_environment
        failed = await self.engine.execute_script(
            agent_name="agent",
            skill_name="skill",
            script_path="main.py",
            skill_base_path=str(skill),
        )
        self.assertEqual(failed.status, ExecutionStatus.FAILED)
        self.assertEqual(failed.stderr, "执行失败 (EnvironmentError)")
        stored = self.engine.get_execution_path("agent", failed.execution_id).read_text(
            encoding="utf-8"
        )
        self.assertNotIn(environment_secret, stored)

        self.runtime.get_or_create_environment = _ExecutionRuntime().get_or_create_environment
        internal_secret = "private path /outside/workspace/token-value"
        with patch.object(
            self.engine,
            "_prepare_work_dir_with_skill",
            side_effect=RuntimeError(internal_secret),
        ):
            unexpected = await self.engine.execute_script(
                agent_name="agent",
                skill_name="skill",
                script_path="main.py",
                skill_base_path=str(skill),
            )
        self.assertEqual(unexpected.stderr, "执行失败 (RuntimeError)")
        stored = self.engine.get_execution_path(
            "agent", unexpected.execution_id
        ).read_text(encoding="utf-8")
        self.assertNotIn(internal_secret, stored)


if __name__ == "__main__":
    unittest.main()

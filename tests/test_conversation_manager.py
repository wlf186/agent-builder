import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import src.conversation_manager as conversation_module
from src.conversation_manager import ConversationManager


RUNTIME_TEST_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "tests"


class ConversationManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_DIR.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_DIR)
        self.data_dir = Path(self.temporary_directory.name) / "data"
        self.manager = ConversationManager(self.data_dir)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_crud_and_idempotent_sync(self) -> None:
        conversation = self.manager.create_conversation("agent-a")
        first = self.manager.add_message("agent-a", conversation.id, "user", "hello")
        self.assertIsNotNone(first)
        loaded = self.manager.get_conversation("agent-a", conversation.id)
        self.assertEqual(loaded.title, "hello")
        self.assertEqual(len(loaded.messages), 1)

        original_updated_at = loaded.updated_at
        unchanged = self.manager.save_messages("agent-a", conversation.id, loaded.messages)
        self.assertEqual(unchanged.updated_at, original_updated_at)

        messages = loaded.messages + [{"id": "second", "role": "assistant", "content": "hi"}]
        saved = self.manager.save_messages("agent-a", conversation.id, messages)
        self.assertEqual(saved.get_message_count(), 2)
        self.assertEqual(self.manager.list_conversations("agent-a")[0]["message_count"], 2)

        database = self.data_dir / "conversations" / "conversations.db"
        with sqlite3.connect(database) as connection:
            row_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(row_count, 2)
        self.assertFalse(list((self.data_dir / "conversations").glob("*/*.json")))

        self.assertTrue(self.manager.delete_conversation("agent-a", conversation.id))
        self.assertIsNone(self.manager.get_conversation("agent-a", conversation.id))

    def test_concurrent_appends_are_serialized(self) -> None:
        conversation = self.manager.create_conversation("agent-a")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda number: self.manager.add_message(
                        "agent-a", conversation.id, "assistant", str(number)
                    ),
                    range(40),
                )
            )
        self.assertTrue(all(results))
        loaded = self.manager.get_conversation("agent-a", conversation.id)
        self.assertEqual(len(loaded.messages), 40)

    def test_manual_title_is_not_overwritten_by_message_sync(self) -> None:
        conversation = self.manager.create_conversation("agent-a")
        renamed = self.manager.update_conversation(
            "agent-a", conversation.id, "Pinned title"
        )
        self.assertEqual(renamed.title, "Pinned title")

        synced = self.manager.sync_messages(
            "agent-a",
            conversation.id,
            [
                {"id": "user-1", "role": "user", "content": "first user content"},
                {"id": "assistant-1", "role": "assistant", "content": "answer"},
            ],
        )
        self.assertEqual(synced.title, "Pinned title")
        self.assertEqual(
            self.manager.list_conversations("agent-a")[0]["preview"],
            "first user content",
        )

    def test_incremental_sync_uses_message_id_index_without_full_history_scan(self) -> None:
        conversation = self.manager.create_conversation("agent-a")
        self.assertEqual(len(conversation.id), 32)
        pair = [
            {"id": "user-1", "role": "user", "content": "hello"},
            {"id": "assistant-1", "role": "assistant", "content": "hi"},
        ]
        with patch.object(
            ConversationManager,
            "_deserialize_messages",
            side_effect=AssertionError("incremental sync scanned full history"),
        ):
            first = self.manager.sync_messages("agent-a", conversation.id, pair)
            unchanged = self.manager.sync_messages("agent-a", conversation.id, pair)

        self.assertEqual(first.message_count, 2)
        self.assertEqual(unchanged.updated_at, first.updated_at)
        database = self.data_dir / "conversations" / "conversations.db"
        with sqlite3.connect(database) as connection:
            ids = connection.execute(
                "SELECT message_id FROM messages ORDER BY position"
            ).fetchall()
        self.assertEqual(ids, [("user-1",), ("assistant-1",)])

    def test_storage_count_and_message_size_limits_fail_closed(self) -> None:
        with patch.object(conversation_module, "MAX_CONVERSATIONS_PER_AGENT", 1):
            conversation = self.manager.create_conversation("agent-a")
            with self.assertRaises(ValueError):
                self.manager.create_conversation("agent-a")

        with patch.object(conversation_module, "MAX_SERIALIZED_MESSAGE_BYTES", 32):
            with self.assertRaises(ValueError):
                self.manager.add_message(
                    "agent-a", conversation.id, "assistant", "x" * 100
                )

    def test_delete_agent_conversations_cascades_messages(self) -> None:
        first = self.manager.create_conversation("agent-a")
        second = self.manager.create_conversation("agent-a")
        self.manager.add_message("agent-a", first.id, "user", "one")
        self.manager.add_message("agent-a", second.id, "user", "two")
        self.assertEqual(self.manager.delete_agent_conversations("agent-a"), 2)
        self.assertEqual(self.manager.list_conversations("agent-a"), [])

        database = self.data_dir / "conversations" / "conversations.db"
        with sqlite3.connect(database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                0,
            )

    def test_legacy_json_is_imported_once(self) -> None:
        legacy_data_dir = Path(self.temporary_directory.name) / "legacy-data"
        legacy_agent_dir = legacy_data_dir / "conversations" / "old-agent"
        legacy_agent_dir.mkdir(parents=True)
        (legacy_agent_dir / "legacy-id.json").write_text(
            json.dumps(
                {
                    "id": "legacy-id",
                    "agent_name": "old-agent",
                    "title": "old",
                    "messages": [{"role": "user", "content": "migrated"}],
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                }
            ),
            encoding="utf-8",
        )
        manager = ConversationManager(legacy_data_dir)
        migrated = manager.get_conversation("old-agent", "legacy-id")
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.messages[0]["content"], "migrated")

    def test_precreated_conversation_directory_symlink_is_rejected(self) -> None:
        unsafe_data = Path(self.temporary_directory.name) / "unsafe-data"
        outside = Path(self.temporary_directory.name) / "outside"
        unsafe_data.mkdir()
        outside.mkdir()
        (unsafe_data / "conversations").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "会话存储目录不安全"):
            ConversationManager(unsafe_data)

        self.assertFalse((outside / "conversations.db").exists())

    def test_sqlite_database_and_sidecars_reject_links_and_non_files(self) -> None:
        for unsafe_name in (
            "conversations.db",
            "conversations.db-wal",
            "conversations.db-shm",
        ):
            with self.subTest(unsafe_name=unsafe_name):
                data_dir = Path(self.temporary_directory.name) / f"unsafe-{unsafe_name}"
                conversations_dir = data_dir / "conversations"
                conversations_dir.mkdir(parents=True)
                outside = data_dir / "outside-sentinel"
                outside.write_bytes(b"unchanged")
                (conversations_dir / unsafe_name).symlink_to(outside)

                with self.assertRaisesRegex(ValueError, "会话数据库路径不安全"):
                    ConversationManager(data_dir)

                self.assertEqual(outside.read_bytes(), b"unchanged")

        data_dir = Path(self.temporary_directory.name) / "unsafe-node"
        database = data_dir / "conversations" / "conversations.db"
        database.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "会话数据库路径不安全"):
            ConversationManager(data_dir)

    def test_hard_linked_database_is_rejected_without_modifying_inode(self) -> None:
        data_dir = Path(self.temporary_directory.name) / "hardlink-data"
        conversations_dir = data_dir / "conversations"
        conversations_dir.mkdir(parents=True)
        outside = Path(self.temporary_directory.name) / "outside.db"
        with sqlite3.connect(outside) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
        (conversations_dir / "conversations.db").hardlink_to(outside)

        with self.assertRaisesRegex(ValueError, "会话数据库路径不安全"):
            ConversationManager(data_dir)

        with sqlite3.connect(outside) as connection:
            value = connection.execute("SELECT value FROM sentinel").fetchone()[0]
        self.assertEqual(value, "unchanged")


if __name__ == "__main__":
    unittest.main()

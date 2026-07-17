"""SQLite-backed conversation storage.

The previous JSON implementation rewrote the complete conversation and a second
index file after every message.  Apart from being unsafe under concurrent
requests, that made a long streaming conversation generate quadratic disk I/O.
This implementation keeps the public API unchanged while storing each message
as one SQLite row in a project-local database.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.models import ConversationConfig
from src.storage_paths import (
    UnsafeStoragePathError,
    ensure_real_directory,
    validate_regular_file,
)


logger = logging.getLogger(__name__)

MAX_CONVERSATIONS_PER_AGENT = 500
MAX_MESSAGES_PER_CONVERSATION = 5_000
MAX_SERIALIZED_MESSAGE_BYTES = 1024 * 1024
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class ConversationSyncResult:
    id: str
    agent_name: str
    title: str
    message_count: int
    created_at: str
    updated_at: str


class ConversationManager:
    """Persist conversations in a concurrency-safe, append-friendly database."""

    def __init__(self, data_dir: Path):
        try:
            self.data_dir = ensure_real_directory(Path(data_dir))
        except UnsafeStoragePathError as exc:
            raise ValueError("会话数据目录不安全") from exc
        self.conversations_dir = self.data_dir / "conversations"
        try:
            self.conversations_dir = ensure_real_directory(self.conversations_dir)
        except UnsafeStoragePathError as exc:
            raise ValueError("会话存储目录不安全") from exc
        self.database_path = self.conversations_dir / "conversations.db"
        self._lock = threading.RLock()
        self._initialize_database()
        self._migrate_legacy_json_once()

    def _connect(self) -> sqlite3.Connection:
        self._validate_database_paths()
        # ``nofollow=1`` maps to SQLite's SQLITE_OPEN_NOFOLLOW protection for
        # the main database.  The explicit checks also cover its WAL/SHM files.
        database_uri = f"{self.database_path.as_uri()}?mode=rwc&nofollow=1"
        connection = sqlite3.connect(database_uri, timeout=10, uri=True)
        try:
            self._validate_database_paths(require_database=True)
        except Exception:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA temp_store = MEMORY")
        return connection

    def _validate_database_paths(self, *, require_database: bool = False) -> None:
        try:
            ensure_real_directory(self.data_dir)
            ensure_real_directory(self.conversations_dir)
            validate_regular_file(
                self.database_path,
                allow_missing=not require_database,
            )
            for suffix in ("-wal", "-shm", "-journal"):
                validate_regular_file(
                    Path(f"{self.database_path}{suffix}"),
                    allow_missing=True,
                )
        except UnsafeStoragePathError as exc:
            raise ValueError("会话数据库路径不安全") from exc

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA journal_size_limit = 16777216")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count = {MAX_DATABASE_BYTES // page_size}")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    agent_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    title_is_manual INTEGER NOT NULL DEFAULT 0,
                    preview TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_name, id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    agent_name TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    message_id TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (agent_name, conversation_id, position),
                    FOREIGN KEY (agent_name, conversation_id)
                        REFERENCES conversations (agent_name, id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS conversations_recent
                    ON conversations (agent_name, updated_at DESC);

                CREATE TABLE IF NOT EXISTS storage_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "title_is_manual" not in columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN title_is_manual INTEGER NOT NULL DEFAULT 0"
                )
            message_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "message_id" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
                rows = connection.execute(
                    "SELECT rowid, payload FROM messages WHERE message_id IS NULL"
                ).fetchall()
                updates = []
                for row in rows:
                    try:
                        payload = json.loads(row["payload"])
                        message_id = payload.get("id") if isinstance(payload, dict) else None
                    except (TypeError, json.JSONDecodeError):
                        message_id = None
                    if isinstance(message_id, str) and message_id:
                        updates.append((message_id[:100], row["rowid"]))
                connection.executemany(
                    "UPDATE messages SET message_id = ? WHERE rowid = ?", updates
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS messages_by_id "
                "ON messages (agent_name, conversation_id, message_id)"
            )
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            logger.warning("Unable to restrict permissions on %s", self.database_path)

    @staticmethod
    def _serialize_message(message: Dict[str, Any]) -> str:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > MAX_SERIALIZED_MESSAGE_BYTES:
            raise ValueError("单条会话消息超过 1MB 存储上限")
        return encoded

    @staticmethod
    def _content_text(message: Dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _title_and_preview(
        cls,
        messages: Sequence[Dict[str, Any]],
        fallback_title: str,
    ) -> tuple[str, str]:
        for message in messages:
            if message.get("role") != "user":
                continue
            content = cls._content_text(message)
            if not content:
                continue
            title = content[:30] + ("..." if len(content) > 30 else "")
            preview = content[:50] + ("..." if len(content) > 50 else "")
            return title, preview
        return fallback_title, ""

    @staticmethod
    def _deserialize_messages(rows: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                logger.error("Skipping corrupt conversation message at position %s", row["position"])
                continue
            if isinstance(payload, dict):
                messages.append(payload)
        return messages

    def _load_conversation(
        self,
        connection: sqlite3.Connection,
        agent_name: str,
        conversation_id: str,
    ) -> Optional[ConversationConfig]:
        row = connection.execute(
            """
            SELECT id, agent_name, title, created_at, updated_at
            FROM conversations WHERE agent_name = ? AND id = ?
            """,
            (agent_name, conversation_id),
        ).fetchone()
        if row is None:
            return None
        message_rows = connection.execute(
            """
            SELECT position, payload FROM messages
            WHERE agent_name = ? AND conversation_id = ?
            ORDER BY position
            """,
            (agent_name, conversation_id),
        ).fetchall()
        return ConversationConfig(
            id=row["id"],
            agent_name=row["agent_name"],
            title=row["title"],
            messages=self._deserialize_messages(message_rows),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _insert_conversation(
        self,
        connection: sqlite3.Connection,
        conversation: ConversationConfig,
        *,
        ignore_existing: bool = False,
    ) -> bool:
        messages = [dict(message) for message in conversation.messages]
        if len(messages) > MAX_MESSAGES_PER_CONVERSATION:
            raise ValueError("单个会话最多保存 5000 条消息")
        serialized_messages = [self._serialize_message(message) for message in messages]
        title, preview = self._title_and_preview(messages, conversation.title)
        verb = "INSERT OR IGNORE" if ignore_existing else "INSERT"
        cursor = connection.execute(
            f"""
            {verb} INTO conversations
                (agent_name, id, title, title_is_manual, preview, message_count, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                conversation.agent_name,
                conversation.id,
                title,
                preview,
                len(messages),
                conversation.created_at,
                conversation.updated_at,
            ),
        )
        if cursor.rowcount == 0:
            return False
        connection.executemany(
            """
            INSERT INTO messages
                (agent_name, conversation_id, position, message_id, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    conversation.agent_name,
                    conversation.id,
                    position,
                    (
                        str(message.get("id"))[:100]
                        if message.get("id") is not None
                        else None
                    ),
                    serialized_messages[position],
                )
                for position, message in enumerate(messages)
            ],
        )
        return True

    def _migrate_legacy_json_once(self) -> None:
        """Import the old per-conversation JSON layout without modifying it."""
        with self._lock, self._connect() as connection:
            completed = connection.execute(
                "SELECT value FROM storage_metadata WHERE key = 'legacy_json_migration'"
            ).fetchone()
            if completed is not None:
                return

            migration_failed = False
            imported = 0
            for agent_dir in self.conversations_dir.iterdir():
                if not agent_dir.is_dir() or agent_dir.is_symlink():
                    continue
                for path in agent_dir.glob("*.json"):
                    if path.name == "index.json" or not path.is_file() or path.is_symlink():
                        continue
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        data.setdefault("agent_name", agent_dir.name)
                        conversation = ConversationConfig(**data)
                        imported += int(
                            self._insert_conversation(
                                connection,
                                conversation,
                                ignore_existing=True,
                            )
                        )
                    except Exception as exc:  # Keep legacy data available for recovery.
                        migration_failed = True
                        logger.error(
                            "Unable to migrate legacy conversation: error_type=%s",
                            type(exc).__name__,
                        )

            if not migration_failed:
                connection.execute(
                    """
                    INSERT INTO storage_metadata (key, value) VALUES ('legacy_json_migration', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (datetime.now().isoformat(),),
                )
            if imported:
                logger.info("Migrated %d legacy conversations to SQLite", imported)

    def create_conversation(
        self,
        agent_name: str,
        title: Optional[str] = None,
    ) -> ConversationConfig:
        now = datetime.now().isoformat()
        with self._lock, self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()[0]
            if count >= MAX_CONVERSATIONS_PER_AGENT:
                raise ValueError("每个 Agent 最多保存 500 个会话")
            while True:
                conversation = ConversationConfig(
                    id=uuid.uuid4().hex,
                    agent_name=agent_name,
                    title=title or "新对话",
                    messages=[],
                    created_at=now,
                    updated_at=now,
                )
                try:
                    self._insert_conversation(connection, conversation)
                    return conversation
                except sqlite3.IntegrityError:
                    continue

    def get_conversation(
        self,
        agent_name: str,
        conversation_id: str,
    ) -> Optional[ConversationConfig]:
        with self._lock, self._connect() as connection:
            return self._load_conversation(connection, agent_name, conversation_id)

    def update_conversation(
        self,
        agent_name: str,
        conversation_id: str,
        title: str,
    ) -> Optional[ConversationConfig]:
        now = datetime.now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversations SET title = ?, title_is_manual = 1, updated_at = ?
                WHERE agent_name = ? AND id = ?
                """,
                (title, now, agent_name, conversation_id),
            )
            if cursor.rowcount == 0:
                return None
            return self._load_conversation(connection, agent_name, conversation_id)

    def delete_conversation(self, agent_name: str, conversation_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE agent_name = ? AND id = ?",
                (agent_name, conversation_id),
            )
            return cursor.rowcount > 0

    def delete_agent_conversations(self, agent_name: str) -> int:
        """Delete all conversation rows owned by an Agent in one transaction."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE agent_name = ?", (agent_name,)
            )
            return max(0, cursor.rowcount)

    def add_message(
        self,
        agent_name: str,
        conversation_id: str,
        role: str,
        content: str,
        thinking: Optional[str] = None,
        tool_calls: Optional[List[Dict]] = None,
        metrics: Optional[Dict] = None,
    ) -> Optional[Dict[str, Any]]:
        now = datetime.now().isoformat()
        message: Dict[str, Any] = {
            "id": f"msg-{uuid.uuid4().hex}",
            "role": role,
            "content": content,
            "timestamp": now,
        }
        if thinking:
            message["thinking"] = thinking
        if tool_calls:
            message["tool_calls"] = tool_calls
        if metrics:
            message["metrics"] = metrics

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT title, title_is_manual, preview, message_count FROM conversations
                WHERE agent_name = ? AND id = ?
                """,
                (agent_name, conversation_id),
            ).fetchone()
            if row is None:
                return None

            position = row["message_count"]
            if position >= MAX_MESSAGES_PER_CONVERSATION:
                raise ValueError("单个会话最多保存 5000 条消息")
            connection.execute(
                """
                INSERT INTO messages
                    (agent_name, conversation_id, position, message_id, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    agent_name,
                    conversation_id,
                    position,
                    message["id"],
                    self._serialize_message(message),
                ),
            )

            title = row["title"]
            preview = row["preview"]
            if role == "user" and position == 0:
                generated_title, preview = self._title_and_preview([message], title)
                if not row["title_is_manual"]:
                    title = generated_title
            connection.execute(
                """
                UPDATE conversations
                SET title = ?, preview = ?, message_count = ?, updated_at = ?
                WHERE agent_name = ? AND id = ?
                """,
                (title, preview, position + 1, now, agent_name, conversation_id),
            )
        return message

    def list_conversations(self, agent_name: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, preview, message_count, created_at, updated_at
                FROM conversations WHERE agent_name = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (agent_name, MAX_CONVERSATIONS_PER_AGENT),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_messages(
        self,
        agent_name: str,
        conversation_id: str,
        messages: List[Dict[str, Any]],
    ) -> Optional[ConversationConfig]:
        """Synchronize messages while writing only rows that actually changed."""
        normalized = [dict(message) for message in messages]
        if len(normalized) > MAX_MESSAGES_PER_CONVERSATION:
            raise ValueError("单个会话最多保存 5000 条消息")
        serialized = [self._serialize_message(message) for message in normalized]
        now = datetime.now().isoformat()

        with self._lock, self._connect() as connection:
            conversation_row = connection.execute(
                """
                SELECT title, title_is_manual, updated_at FROM conversations
                WHERE agent_name = ? AND id = ?
                """,
                (agent_name, conversation_id),
            ).fetchone()
            if conversation_row is None:
                return None

            existing_rows = connection.execute(
                """
                SELECT position, payload FROM messages
                WHERE agent_name = ? AND conversation_id = ? ORDER BY position
                """,
                (agent_name, conversation_id),
            ).fetchall()
            existing = [row["payload"] for row in existing_rows]
            generated_title, preview = self._title_and_preview(
                normalized, conversation_row["title"]
            )
            title = (
                conversation_row["title"]
                if conversation_row["title_is_manual"]
                else generated_title
            )

            changed_positions = [
                position
                for position in range(min(len(existing), len(serialized)))
                if existing[position] != serialized[position]
            ]
            if changed_positions:
                connection.executemany(
                    """
                    UPDATE messages SET message_id = ?, payload = ?
                    WHERE agent_name = ? AND conversation_id = ? AND position = ?
                    """,
                    [
                        (
                            str(normalized[position].get("id"))[:100]
                            if normalized[position].get("id") is not None
                            else None,
                            serialized[position],
                            agent_name,
                            conversation_id,
                            position,
                        )
                        for position in changed_positions
                    ],
                )
            if len(serialized) > len(existing):
                connection.executemany(
                    """
                    INSERT INTO messages
                        (agent_name, conversation_id, position, message_id, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            agent_name,
                            conversation_id,
                            position,
                            str(normalized[position].get("id"))[:100]
                            if normalized[position].get("id") is not None
                            else None,
                            serialized[position],
                        )
                        for position in range(len(existing), len(serialized))
                    ],
                )
            elif len(serialized) < len(existing):
                connection.execute(
                    """
                    DELETE FROM messages
                    WHERE agent_name = ? AND conversation_id = ? AND position >= ?
                    """,
                    (agent_name, conversation_id, len(serialized)),
                )

            data_changed = bool(changed_positions) or len(serialized) != len(existing)
            metadata_changed = title != conversation_row["title"]
            updated_at = now if data_changed or metadata_changed else conversation_row["updated_at"]
            if data_changed or metadata_changed:
                connection.execute(
                    """
                    UPDATE conversations
                    SET title = ?, preview = ?, message_count = ?, updated_at = ?
                    WHERE agent_name = ? AND id = ?
                    """,
                    (title, preview, len(normalized), updated_at, agent_name, conversation_id),
                )

            row = connection.execute(
                """
                SELECT id, agent_name, title, created_at, updated_at
                FROM conversations WHERE agent_name = ? AND id = ?
                """,
                (agent_name, conversation_id),
            ).fetchone()
            assert row is not None
            return ConversationConfig(
                id=row["id"],
                agent_name=row["agent_name"],
                title=row["title"],
                messages=normalized,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def sync_messages(
        self,
        agent_name: str,
        conversation_id: str,
        messages: List[Dict[str, Any]],
    ) -> Optional[ConversationSyncResult]:
        """Upsert a small message tail by stable message ID.

        Streaming clients call this once per completed turn with only the new
        user/assistant pair.  This keeps request bodies bounded and avoids
        retransmitting an ever-growing conversation.  Existing rows are only
        rewritten when their serialized payload actually changes.
        """
        incoming = [dict(message) for message in messages]
        if len(incoming) > 10:
            raise ValueError("单次增量同步最多接受 10 条消息")
        now = datetime.now().isoformat()

        with self._lock, self._connect() as connection:
            conversation_row = connection.execute(
                """
                SELECT title, title_is_manual, preview, message_count, created_at, updated_at
                FROM conversations WHERE agent_name = ? AND id = ?
                """,
                (agent_name, conversation_id),
            ).fetchone()
            if conversation_row is None:
                return None

            changed = False
            message_count = int(conversation_row["message_count"])
            title = conversation_row["title"]
            preview = conversation_row["preview"]

            for message in incoming:
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    message_id = f"msg-{uuid.uuid4().hex}"
                    message["id"] = message_id
                if len(message_id) > 100 or any(ord(char) < 32 for char in message_id):
                    raise ValueError("消息 ID 无效")
                serialized = self._serialize_message(message)
                existing = connection.execute(
                    """
                    SELECT position, payload FROM messages
                    WHERE agent_name = ? AND conversation_id = ? AND message_id = ?
                    ORDER BY position LIMIT 1
                    """,
                    (agent_name, conversation_id, message_id),
                ).fetchone()
                if existing is None:
                    if message_count >= MAX_MESSAGES_PER_CONVERSATION:
                        raise ValueError("单个会话最多保存 5000 条消息")
                    position = message_count
                    connection.execute(
                        """
                        INSERT INTO messages
                            (agent_name, conversation_id, position, message_id, payload)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            agent_name,
                            conversation_id,
                            position,
                            message_id,
                            serialized,
                        ),
                    )
                    message_count += 1
                    changed = True
                else:
                    position = int(existing["position"])
                if existing is not None and existing["payload"] != serialized:
                    connection.execute(
                        """
                        UPDATE messages SET payload = ?, message_id = ?
                        WHERE agent_name = ? AND conversation_id = ? AND position = ?
                        """,
                        (
                            serialized,
                            message_id,
                            agent_name,
                            conversation_id,
                            position,
                        ),
                    )
                    changed = True

                if message.get("role") == "user" and (not preview or position == 0):
                    generated_title, generated_preview = self._title_and_preview(
                        [message], title
                    )
                    preview = generated_preview
                    if not conversation_row["title_is_manual"]:
                        title = generated_title

            metadata_changed = (
                title != conversation_row["title"]
                or preview != conversation_row["preview"]
                or message_count != conversation_row["message_count"]
            )
            updated_at = now if changed or metadata_changed else conversation_row["updated_at"]
            if changed or metadata_changed:
                connection.execute(
                    """
                    UPDATE conversations
                    SET title = ?, preview = ?, message_count = ?, updated_at = ?
                    WHERE agent_name = ? AND id = ?
                    """,
                    (title, preview, message_count, updated_at, agent_name, conversation_id),
                )

            return ConversationSyncResult(
                id=conversation_id,
                agent_name=agent_name,
                title=title,
                message_count=message_count,
                created_at=conversation_row["created_at"],
                updated_at=updated_at,
            )

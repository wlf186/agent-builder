"""Agent-scoped conversation and turn persistence in the canonical journal.

The store shares an Agent's ``state.sqlite`` with :class:`EventJournal`.  Turn
acceptance and terminal state can therefore be committed with their canonical
boundary event instead of relying on a recoverably inconsistent dual write.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import Lock
from typing import Literal
from uuid import uuid4

from .contracts import SCHEMA_VERSION, EventEnvelope, utc_now
from .tools import ToolSpec, prototype_tool_specs


DATABASE_NAME = "state.sqlite"
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_CONVERSATIONS_PER_AGENT = 100
# Keep one restored transcript bounded for the current non-paginated prototype
# API and ContextCompiler.  This is a semantic Turn cap, not a token budget;
# each Run still applies the active model's dynamic context-window policy.
MAX_TURNS_PER_CONVERSATION = 128
MAX_TITLE_BYTES = 256
MAX_USER_CONTENT_BYTES = 8_192
MAX_ASSISTANT_CONTENT_BYTES = 16_384
MAX_DURABLE_EVENT_BYTES = 65_536
# Startup recovery replays only a bounded durable prefix.  These limits mirror
# the live Control Plane quotas without importing ``control`` (which imports
# this module), and leave room for the closure events plus the terminal.
MAX_RECOVERY_EVENTS_PER_RUN = 512
MAX_RECOVERY_DURABLE_BYTES_PER_RUN = 256 * 1024
MAX_RECOVERY_SEQUENCE = 1_000_000
MAX_RECOVERY_JSON_DEPTH = 16
MAX_RECOVERY_JSON_NODES = 4_096
MAX_RECOVERY_OBJECT_FIELDS = 128
MAX_RECOVERY_ARRAY_ITEMS = 256
MAX_RECOVERY_STRING_BYTES = 16_384
MAX_RECOVERY_FIELD_NAME_BYTES = 128
MAX_RECOVERY_WORKER_TEXT_BYTES = 12_288
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 100_000

_SAFE_ID = re.compile(r"^[a-f0-9-]{32,36}$")
_RECOVERY_EVENT_ID = re.compile(r"^[a-f0-9]{32}$")
_RECOVERY_WORKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_RECOVERY_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_RECOVERY_DURABLE_KINDS = frozenset(
    {
        "run.started",
        "assistant.block.started",
        "assistant.block.finished",
        "assistant.block.discarded",
        "tool.call.requested",
        "tool.call.started",
        "tool.call.finished",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
)
_RECOVERY_BLOCK_REASONS = frozenset(
    {"cancelled", "runtime_failure", "worker_failure"}
)
_RECOVERY_TOOL_SPECS: dict[str, ToolSpec] = {
    spec.tool_id: spec for spec in prototype_tool_specs()
}
TurnStatus = Literal["running", "completed", "failed", "cancelled", "interrupted"]
TerminalTurnStatus = Literal["failed", "cancelled", "interrupted"]
MessageRole = Literal["user", "assistant"]


class ConversationStoreError(RuntimeError):
    """Base class for a safe, user-actionable store failure."""


class ConversationStoreUnavailableError(ConversationStoreError):
    """The durable store cannot be opened or updated safely."""


class ConversationNotFoundError(ConversationStoreError, KeyError):
    """The requested conversation does not exist for this Agent."""


class TurnNotFoundError(ConversationStoreError, KeyError):
    """The requested Run has no persisted turn for this Agent."""


class ConversationConflictError(ConversationStoreError):
    """The requested transition conflicts with persisted state."""


def _strict_recovery_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len(pairs) > MAX_RECOVERY_OBJECT_FIELDS:
        raise ValueError("JSON object has too many fields")
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object has duplicate fields")
        result[key] = value
    return result


def _reject_recovery_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_recovery_json_shape(value: object) -> None:
    """Bound the decoded object graph before any event fields are trusted."""

    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_RECOVERY_JSON_NODES or depth > MAX_RECOVERY_JSON_DEPTH:
            raise ValueError("JSON structure exceeds its recovery limit")
        if isinstance(current, dict):
            if len(current) > MAX_RECOVERY_OBJECT_FIELDS:
                raise ValueError("JSON object has too many fields")
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object has a non-text field name")
                if len(key.encode("utf-8")) > MAX_RECOVERY_FIELD_NAME_BYTES:
                    raise ValueError("JSON field name exceeds its recovery limit")
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_RECOVERY_ARRAY_ITEMS:
                raise ValueError("JSON array exceeds its recovery limit")
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_RECOVERY_STRING_BYTES:
                raise ValueError("JSON string exceeds its recovery limit")
        elif current is None or isinstance(current, bool):
            continue
        elif isinstance(current, int):
            if not -(2**63) <= current <= 2**63 - 1:
                raise ValueError("JSON integer exceeds its recovery limit")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("JSON number is not finite")
        else:
            raise ValueError("JSON contains an unsupported value")


def _decode_recovery_envelope(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_recovery_object,
            parse_constant=_reject_recovery_json_constant,
        )
        _validate_recovery_json_shape(value)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ConversationConflictError(
            "running turn has an invalid canonical event envelope"
        ) from exc
    if not isinstance(value, dict):
        raise ConversationConflictError(
            "running turn has a non-object canonical event envelope"
        )
    return value


def _bounded_recovery_string(
    value: object,
    *,
    maximum_bytes: int,
    field: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ConversationConflictError(f"canonical {field} is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ConversationConflictError(f"canonical {field} is invalid") from exc
    if size > maximum_bytes:
        raise ConversationConflictError(f"canonical {field} exceeds its limit")
    return value


def _recovery_worker_id(value: object, field: str) -> str:
    candidate = _bounded_recovery_string(
        value, maximum_bytes=64, field=field
    )
    if _RECOVERY_WORKER_ID.fullmatch(candidate) is None:
        raise ConversationConflictError(f"canonical {field} is invalid")
    return candidate


def _exact_recovery_payload(
    payload: object, expected_keys: set[str]
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ConversationConflictError("canonical event payload is invalid")
    return payload


@dataclass(frozen=True, slots=True)
class CommittedMessage:
    role: MessageRole
    content: str
    turn_id: str
    run_id: str

    @property
    def message_id(self) -> str:
        return conversation_message_id(self.turn_id, self.role)


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    turn_id: str
    conversation_id: str
    run_id: str
    position: int
    status: TurnStatus
    user_content: str
    assistant_content: str | None
    created_at: str
    updated_at: str

    @property
    def user_message_id(self) -> str:
        return conversation_message_id(self.turn_id, "user")

    @property
    def assistant_message_id(self) -> str | None:
        if self.assistant_content is None:
            return None
        return conversation_message_id(self.turn_id, "assistant")


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    conversation_id: str
    agent_id: str
    title: str
    created_at: str
    updated_at: str
    revision: int
    active_run_id: str | None
    turn_count: int
    completed_turn_count: int
    last_run_id: str | None


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    agent_id: str
    title: str
    created_at: str
    updated_at: str
    revision: int
    active_run_id: str | None
    turns: tuple[ConversationTurn, ...]


@dataclass(frozen=True, slots=True)
class BeginTurnResult:
    turn: ConversationTurn
    committed_history: tuple[CommittedMessage, ...]


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation_id: str
    revision: int
    committed_history: tuple[CommittedMessage, ...]


@dataclass(frozen=True, slots=True)
class ConversationDeleteResult:
    deleted: bool
    deleted_turns: int
    deleted_events: int


@dataclass(frozen=True, slots=True)
class _RecoveryScan:
    last_seq: int
    event_count: int
    durable_bytes: int
    closure_events: tuple[tuple[str, dict[str, object]], ...]


def _validate_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def conversation_message_id(turn_id: str, role: MessageRole) -> str:
    """Derive a stable, non-secret message identity without another DB write."""

    turn_id = _validate_id(turn_id, "turn_id")
    if role not in {"user", "assistant"}:
        raise ValueError("invalid message role")
    digest = hashlib.sha256(
        b"agent-builder-v2:conversation-message:v1\0"
        + turn_id.encode("ascii")
        + b"\0"
        + role.encode("ascii")
    ).hexdigest()
    return digest[:32]


def _bounded_text(value: object, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or "\x00" in value or not value.strip():
        raise ValueError(f"invalid {field}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {field}") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} UTF-8 bytes")
    return value


def _validate_private_regular(
    path: Path,
    identity: tuple[int, int] | None = None,
) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConversationStoreUnavailableError(
            f"could not inspect conversation state file: {path.name}"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or (identity is not None and (metadata.st_dev, metadata.st_ino) != identity)
    ):
        raise ConversationStoreUnavailableError(
            f"unsafe conversation state file: {path.name}"
        )
    os.chmod(path, 0o600, follow_symlinks=False)


def _open_private_database(
    path: Path, directory_descriptor: int
) -> tuple[int, tuple[int, int]]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ConversationStoreUnavailableError(
            "secure conversation state requires O_NOFOLLOW"
        )
    flags = os.O_RDWR | os.O_CLOEXEC | no_follow
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        try:
            descriptor = os.open(
                path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise ConversationStoreUnavailableError(
                "could not create conversation state safely"
            ) from exc
    except OSError as exc:
        raise ConversationStoreUnavailableError(
            "could not open conversation state safely"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise ConversationStoreUnavailableError(
                "conversation state is not a private regular file"
            )
        os.fchmod(descriptor, 0o600)
        identity = (metadata.st_dev, metadata.st_ino)
        path_metadata = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (path_metadata.st_dev, path_metadata.st_ino) != identity:
            raise ConversationStoreUnavailableError(
                "conversation state path changed while opening"
            )
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _turn_from_row(row: tuple[object, ...]) -> ConversationTurn:
    return ConversationTurn(*row)  # type: ignore[arg-type]


def _encode_boundary_event(
    event: EventEnvelope,
    *,
    expected_kind: str,
    agent_id: str,
    conversation_id: str,
    turn_id: str,
    run_id: str,
    minimum_seq: int,
    exact_seq: int | None = None,
) -> str:
    if (
        not isinstance(event, EventEnvelope)
        or event.kind != expected_kind
        or event.durability != "durable"
        or event.agent_id != agent_id
        or event.conversation_id != conversation_id
        or event.turn_id != turn_id
        or event.run_id != run_id
        or not isinstance(event.seq, int)
        or isinstance(event.seq, bool)
        or event.seq < minimum_seq
        or (exact_seq is not None and event.seq != exact_seq)
    ):
        raise ValueError("canonical boundary event does not match its turn")
    encoded = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DURABLE_EVENT_BYTES:
        raise ValueError("canonical boundary event exceeds durable event limit")
    return encoded


class ConversationStore:
    """Thread-safe Agent conversation state sharing the canonical SQLite DB."""

    def __init__(self, database_path: Path, agent_id: str) -> None:
        self.agent_id = _validate_id(agent_id, "agent_id")
        if database_path != Path(os.path.abspath(database_path)):
            raise ValueError("conversation database path must be absolute")
        if database_path.name != DATABASE_NAME:
            raise ValueError(f"conversation database must be named {DATABASE_NAME}")
        if database_path.parent.name != self.agent_id:
            raise ValueError("conversation database must belong to its Agent data root")
        try:
            parent_metadata = os.lstat(database_path.parent)
        except OSError as exc:
            raise ConversationStoreUnavailableError(
                "Agent data root is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise ConversationStoreUnavailableError("Agent data root is unsafe")
        self.database_path = database_path
        parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
        for suffix in ("-wal", "-shm"):
            _validate_private_regular(Path(f"{database_path}{suffix}"))
        directory_flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._directory_descriptor = os.open(
                database_path.parent, directory_flags
            )
        except OSError as exc:
            raise ConversationStoreUnavailableError(
                "could not anchor the Agent data root safely"
            ) from exc
        anchored_parent = os.fstat(self._directory_descriptor)
        if (
            (anchored_parent.st_dev, anchored_parent.st_ino) != parent_identity
            or not stat.S_ISDIR(anchored_parent.st_mode)
            or anchored_parent.st_uid != os.getuid()
            or stat.S_IMODE(anchored_parent.st_mode) & 0o077
        ):
            os.close(self._directory_descriptor)
            raise ConversationStoreUnavailableError("Agent data root changed")
        try:
            database_descriptor, database_identity = _open_private_database(
                database_path, self._directory_descriptor
            )
        except BaseException:
            os.close(self._directory_descriptor)
            raise
        try:
            self._connection = sqlite3.connect(
                f"/proc/self/fd/{self._directory_descriptor}/{DATABASE_NAME}",
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            os.close(database_descriptor)
            os.close(self._directory_descriptor)
            raise ConversationStoreUnavailableError(
                "could not connect to conversation state"
            ) from exc
        self._lock = Lock()
        self._closed = False
        try:
            opened_metadata = os.fstat(database_descriptor)
            anchored_database = os.stat(
                DATABASE_NAME,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
            if (
                opened_metadata.st_nlink != 1
                or (opened_metadata.st_dev, opened_metadata.st_ino)
                != database_identity
                or (anchored_database.st_dev, anchored_database.st_ino)
                != database_identity
            ):
                raise ConversationStoreUnavailableError(
                    "conversation state changed while connecting"
                )
            os.close(database_descriptor)
            database_descriptor = -1
            self._initialize_schema()
            after_metadata = os.lstat(database_path.parent)
            if (after_metadata.st_dev, after_metadata.st_ino) != parent_identity:
                raise ConversationStoreUnavailableError(
                    "Agent data root changed while opening conversation state"
                )
            _validate_private_regular(database_path, database_identity)
            for suffix in ("-wal", "-shm"):
                _validate_private_regular(Path(f"{database_path}{suffix}"))
        except BaseException:
            if database_descriptor >= 0:
                os.close(database_descriptor)
            self._connection.close()
            os.close(self._directory_descriptor)
            self._closed = True
            raise

    def _initialize_schema(self) -> None:
        try:
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA secure_delete=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA journal_size_limit=16777216")
            self._connection.execute("PRAGMA wal_autocheckpoint=1000")
            page_size_row = self._connection.execute("PRAGMA page_size").fetchone()
            page_count_row = self._connection.execute("PRAGMA page_count").fetchone()
            if (
                page_size_row is None
                or page_count_row is None
                or not isinstance(page_size_row[0], int)
                or isinstance(page_size_row[0], bool)
                or page_size_row[0] <= 0
                or not isinstance(page_count_row[0], int)
                or isinstance(page_count_row[0], bool)
                or page_count_row[0] < 0
                or page_size_row[0] * page_count_row[0] > MAX_DATABASE_BYTES
            ):
                raise sqlite3.DatabaseError(
                    "existing conversation state exceeds its size limit"
                )
            maximum_pages = MAX_DATABASE_BYTES // page_size_row[0]
            applied_row = self._connection.execute(
                f"PRAGMA max_page_count={maximum_pages}"
            ).fetchone()
            if applied_row is None or applied_row[0] > maximum_pages:
                raise sqlite3.DatabaseError(
                    "could not enforce conversation state size limit"
                )
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
                    active_run_id TEXT,
                    CHECK (length(conversation_id) BETWEEN 32 AND 36),
                    CHECK (length(agent_id) BETWEEN 32 AND 36)
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL UNIQUE,
                    position INTEGER NOT NULL CHECK (position > 0),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'running', 'completed', 'failed',
                            'cancelled', 'interrupted'
                        )
                    ),
                    user_content TEXT NOT NULL,
                    assistant_content TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (conversation_id, position),
                    CHECK (
                        (status = 'completed' AND assistant_content IS NOT NULL)
                        OR (status != 'completed' AND assistant_content IS NULL)
                    )
                );
                CREATE UNIQUE INDEX IF NOT EXISTS conversation_one_running_turn
                    ON conversation_turns(conversation_id)
                    WHERE status = 'running';
                CREATE INDEX IF NOT EXISTS conversations_agent_updated
                    ON conversations(agent_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS conversation_turns_history
                    ON conversation_turns(conversation_id, status, position);
                COMMIT;
                """
            )
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise ConversationStoreUnavailableError(
                "could not initialize conversation state"
            ) from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConversationStoreUnavailableError("conversation store is closed")

    def _begin_write(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _begin_read(self) -> None:
        self._connection.execute("BEGIN")

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    def _insert_boundary_event(self, event: EventEnvelope, encoded: str) -> None:
        self._connection.execute(
            """
            INSERT INTO events(run_id, seq, kind, occurred_at, envelope_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.run_id, event.seq, event.kind, event.occurred_at, encoded),
        )

    def create_conversation(
        self,
        title: str = "New conversation",
        *,
        conversation_id: str | None = None,
    ) -> Conversation:
        title = _bounded_text(title, "title", MAX_TITLE_BYTES)
        conversation_id = _validate_id(
            conversation_id if conversation_id is not None else uuid4().hex,
            "conversation_id",
        )
        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                count = self._connection.execute(
                    "SELECT COUNT(*) FROM conversations WHERE agent_id = ?",
                    (self.agent_id,),
                ).fetchone()[0]
                if count >= MAX_CONVERSATIONS_PER_AGENT:
                    raise ConversationConflictError(
                        "Agent conversation capacity is exhausted"
                    )
                self._connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, agent_id, title, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (conversation_id, self.agent_id, title, timestamp, timestamp),
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ConversationConflictError(
                    "conversation already exists"
                ) from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not create conversation"
                ) from exc
        return Conversation(
            conversation_id,
            self.agent_id,
            title,
            timestamp,
            timestamp,
            0,
            None,
            (),
        )

    def list_conversations(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[ConversationSummary, ...]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or not 0 <= offset <= MAX_LIST_OFFSET
        ):
            raise ValueError(f"offset must be between 0 and {MAX_LIST_OFFSET}")
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT
                        c.conversation_id, c.agent_id, c.title,
                        c.created_at, c.updated_at, c.revision, c.active_run_id,
                        COUNT(t.turn_id),
                        COALESCE(SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END), 0),
                        (
                            SELECT last.run_id FROM conversation_turns AS last
                            WHERE last.conversation_id = c.conversation_id
                            ORDER BY last.position DESC LIMIT 1
                        )
                    FROM conversations AS c
                    LEFT JOIN conversation_turns AS t
                        ON t.conversation_id = c.conversation_id
                    WHERE c.agent_id = ?
                    GROUP BY c.conversation_id
                    ORDER BY c.updated_at DESC, c.conversation_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (self.agent_id, limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise ConversationStoreUnavailableError(
                    "could not list conversations"
                ) from exc
        return tuple(ConversationSummary(*row) for row in rows)

    def get_conversation(self, conversation_id: str) -> Conversation:
        conversation_id = _validate_id(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            try:
                self._begin_read()
                row = self._connection.execute(
                    """
                    SELECT conversation_id, agent_id, title, created_at, updated_at,
                           revision, active_run_id
                    FROM conversations
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (conversation_id, self.agent_id),
                ).fetchone()
                if row is None:
                    raise ConversationNotFoundError("conversation not found")
                turn_rows = self._turn_rows(conversation_id)
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not read conversation"
                ) from exc
        return Conversation(*row, tuple(_turn_from_row(item) for item in turn_rows))

    def _turn_rows(self, conversation_id: str) -> list[tuple[object, ...]]:
        return self._connection.execute(
            """
            SELECT turn_id, conversation_id, run_id, position, status,
                   user_content, assistant_content, created_at, updated_at
            FROM conversation_turns
            WHERE conversation_id = ? ORDER BY position
            """,
            (conversation_id,),
        ).fetchall()

    def _committed_history_rows(
        self, conversation_id: str
    ) -> list[tuple[object, ...]]:
        return self._connection.execute(
            """
            SELECT turn_id, run_id, user_content, assistant_content
            FROM conversation_turns
            WHERE conversation_id = ? AND status = 'completed'
            ORDER BY position
            """,
            (conversation_id,),
        ).fetchall()

    @staticmethod
    def _history_from_rows(
        rows: list[tuple[object, ...]],
    ) -> tuple[CommittedMessage, ...]:
        history: list[CommittedMessage] = []
        for turn_id, run_id, user_content, assistant_content in rows:
            history.append(
                CommittedMessage("user", user_content, turn_id, run_id)  # type: ignore[arg-type]
            )
            history.append(
                CommittedMessage("assistant", assistant_content, turn_id, run_id)  # type: ignore[arg-type]
            )
        return tuple(history)

    def committed_history(
        self, conversation_id: str
    ) -> tuple[CommittedMessage, ...]:
        conversation_id = _validate_id(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            try:
                self._begin_read()
                exists = self._connection.execute(
                    """
                    SELECT 1 FROM conversations
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (conversation_id, self.agent_id),
                ).fetchone()
                if exists is None:
                    raise ConversationNotFoundError("conversation not found")
                rows = self._committed_history_rows(conversation_id)
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not read committed conversation history"
                ) from exc
        return self._history_from_rows(rows)

    def snapshot_for_turn(self, conversation_id: str) -> ConversationSnapshot:
        """Read history and its CAS revision from one stable WAL snapshot."""

        conversation_id = _validate_id(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            try:
                self._begin_read()
                row = self._connection.execute(
                    """
                    SELECT revision, active_run_id FROM conversations
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (conversation_id, self.agent_id),
                ).fetchone()
                if row is None:
                    raise ConversationNotFoundError("conversation not found")
                if row[1] is not None:
                    raise ConversationConflictError(
                        "conversation already has an active Run"
                    )
                history = self._history_from_rows(
                    self._committed_history_rows(conversation_id)
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not snapshot conversation history"
                ) from exc
        return ConversationSnapshot(conversation_id, row[0], history)

    def begin_turn(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        run_id: str,
        user_content: str,
        expected_revision: int,
        started_event: EventEnvelope,
    ) -> BeginTurnResult:
        """Accept a turn iff the compiled history revision is still current.

        ``started_event`` is required so acceptance and ``run.started`` are
        always one commit.  Recovery never creates a new running turn.
        """

        conversation_id = _validate_id(conversation_id, "conversation_id")
        turn_id = _validate_id(turn_id, "turn_id")
        run_id = _validate_id(run_id, "run_id")
        user_content = _bounded_text(
            user_content, "user content", MAX_USER_CONTENT_BYTES
        )
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        encoded_event = _encode_boundary_event(
            started_event,
            expected_kind="run.started",
            agent_id=self.agent_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            minimum_seq=1,
            exact_seq=1,
        )
        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                conversation = self._connection.execute(
                    """
                    SELECT active_run_id, revision FROM conversations
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (conversation_id, self.agent_id),
                ).fetchone()
                if conversation is None:
                    raise ConversationNotFoundError("conversation not found")
                if conversation[0] is not None:
                    raise ConversationConflictError(
                        "conversation already has an active Run"
                    )
                if conversation[1] != expected_revision:
                    raise ConversationConflictError(
                        "conversation changed after its history snapshot"
                    )
                total, maximum_position = self._connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(MAX(position), 0)
                    FROM conversation_turns WHERE conversation_id = ?
                    """,
                    (conversation_id,),
                ).fetchone()
                if total >= MAX_TURNS_PER_CONVERSATION:
                    raise ConversationConflictError(
                        "conversation turn capacity is exhausted"
                    )
                history = self._history_from_rows(
                    self._committed_history_rows(conversation_id)
                )
                self._connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        turn_id, conversation_id, run_id, position, status,
                        user_content, assistant_content, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'running', ?, NULL, ?, ?)
                    """,
                    (
                        turn_id,
                        conversation_id,
                        run_id,
                        maximum_position + 1,
                        user_content,
                        timestamp,
                        timestamp,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE conversations
                    SET active_run_id = ?, updated_at = ?, revision = revision + 1
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (run_id, timestamp, conversation_id, self.agent_id),
                )
                self._insert_boundary_event(started_event, encoded_event)
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ConversationConflictError(
                    "turn or Run already exists"
                ) from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not begin conversation turn"
                ) from exc
        return BeginTurnResult(
            ConversationTurn(
                turn_id,
                conversation_id,
                run_id,
                maximum_position + 1,
                "running",
                user_content,
                None,
                timestamp,
                timestamp,
            ),
            history,
        )

    def _turn_for_run(self, run_id: str) -> tuple[object, ...] | None:
        return self._connection.execute(
            """
            SELECT t.turn_id, t.conversation_id, t.run_id, t.position, t.status,
                   t.user_content, t.assistant_content, t.created_at, t.updated_at
            FROM conversation_turns AS t
            JOIN conversations AS c ON c.conversation_id = t.conversation_id
            WHERE t.run_id = ? AND c.agent_id = ?
            """,
            (run_id, self.agent_id),
        ).fetchone()

    def finalize_completed(
        self,
        run_id: str,
        assistant_content: str,
        terminal_event: EventEnvelope,
    ) -> ConversationTurn:
        """Commit a completed pair; normal calls include ``terminal_event``."""

        run_id = _validate_id(run_id, "run_id")
        assistant_content = _bounded_text(
            assistant_content, "assistant content", MAX_ASSISTANT_CONTENT_BYTES
        )
        return self._finalize(
            run_id,
            status="completed",
            assistant_content=assistant_content,
            terminal_event=terminal_event,
            expected_event_kind="run.completed",
        )

    def finalize_noncompleted(
        self,
        run_id: str,
        status: TerminalTurnStatus,
        terminal_event: EventEnvelope | None = None,
    ) -> ConversationTurn:
        """Clear an active Run without committing its partial output.

        ``terminal_event=None`` is reserved for startup recovery or a degraded
        persistence path where no new canonical durable event can be written.
        """

        if status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("invalid non-completed turn status")
        expected_kind = "run.cancelled" if status == "cancelled" else "run.failed"
        return self._finalize(
            _validate_id(run_id, "run_id"),
            status=status,
            assistant_content=None,
            terminal_event=terminal_event,
            expected_event_kind=expected_kind,
        )

    def _finalize(
        self,
        run_id: str,
        *,
        status: TurnStatus,
        assistant_content: str | None,
        terminal_event: EventEnvelope | None,
        expected_event_kind: str,
    ) -> ConversationTurn:
        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row = self._turn_for_run(run_id)
                if row is None:
                    raise TurnNotFoundError("Run has no conversation turn")
                existing = _turn_from_row(row)
                if existing.status != "running":
                    if (
                        existing.status == status
                        and existing.assistant_content == assistant_content
                        and terminal_event is None
                    ):
                        self._connection.commit()
                        return existing
                    raise ConversationConflictError("turn is already terminal")
                encoded_event = (
                    _encode_boundary_event(
                        terminal_event,
                        expected_kind=expected_event_kind,
                        agent_id=self.agent_id,
                        conversation_id=existing.conversation_id,
                        turn_id=existing.turn_id,
                        run_id=run_id,
                        minimum_seq=2,
                    )
                    if terminal_event is not None
                    else None
                )
                cursor = self._connection.execute(
                    """
                    UPDATE conversation_turns
                    SET status = ?, assistant_content = ?, updated_at = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (status, assistant_content, timestamp, run_id),
                )
                if cursor.rowcount != 1:
                    raise ConversationConflictError("turn transition was lost")
                cursor = self._connection.execute(
                    """
                    UPDATE conversations
                    SET active_run_id = NULL, updated_at = ?, revision = revision + 1
                    WHERE conversation_id = ? AND agent_id = ? AND active_run_id = ?
                    """,
                    (timestamp, existing.conversation_id, self.agent_id, run_id),
                )
                if cursor.rowcount != 1:
                    raise ConversationConflictError(
                        "conversation active Run does not match its turn"
                    )
                if terminal_event is not None and encoded_event is not None:
                    self._insert_boundary_event(terminal_event, encoded_event)
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ConversationConflictError(
                    "canonical terminal event already exists"
                ) from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not finalize conversation turn"
                ) from exc
        return ConversationTurn(
            existing.turn_id,
            existing.conversation_id,
            run_id,
            existing.position,
            status,
            existing.user_content,
            assistant_content,
            existing.created_at,
            timestamp,
        )

    def _scan_recovery_events(self, existing: ConversationTurn) -> _RecoveryScan:
        """Replay one untrusted durable stream with fixed memory/byte bounds."""

        cursor = self._connection.execute(
            """
            SELECT seq,
                   CASE
                     WHEN typeof(kind) = 'text'
                      AND length(CAST(kind AS BLOB)) BETWEEN 1 AND 64
                     THEN CAST(kind AS BLOB)
                   END,
                   CASE
                     WHEN typeof(occurred_at) = 'text'
                      AND length(CAST(occurred_at AS BLOB)) BETWEEN 1 AND 64
                     THEN CAST(occurred_at AS BLOB)
                   END,
                   CASE
                     WHEN typeof(envelope_json) = 'text'
                      AND length(CAST(envelope_json AS BLOB))
                          BETWEEN 2 AND ?
                     THEN CAST(envelope_json AS BLOB)
                   END
            FROM events
            WHERE run_id = ?
            ORDER BY seq
            LIMIT ?
            """,
            (
                MAX_DURABLE_EVENT_BYTES,
                existing.run_id,
                MAX_RECOVERY_EVENTS_PER_RUN + 1,
            ),
        )
        event_count = 0
        durable_bytes = 0
        last_seq = 0
        seen_blocks: set[str] = set()
        open_block_id: str | None = None
        seen_calls: set[str] = set()
        pending_call_id: str | None = None
        pending_tool_id: str | None = None
        pending_tool_started = False

        while True:
            row = cursor.fetchone()
            if row is None:
                break
            event_count += 1
            if event_count > MAX_RECOVERY_EVENTS_PER_RUN:
                raise ConversationConflictError(
                    "running turn event count exceeds its recovery limit"
                )
            seq, raw_kind, raw_occurred_at, raw_envelope = row
            if (
                not isinstance(seq, int)
                or isinstance(seq, bool)
                or not 1 <= seq <= MAX_RECOVERY_SEQUENCE
                or seq <= last_seq
                or (seq > last_seq + 1 and open_block_id is None)
                or not isinstance(raw_kind, bytes)
                or not isinstance(raw_occurred_at, bytes)
                or not isinstance(raw_envelope, bytes)
            ):
                raise ConversationConflictError(
                    "running turn has invalid canonical event metadata"
                )
            try:
                kind = raw_kind.decode("utf-8", errors="strict")
                occurred_at = raw_occurred_at.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ConversationConflictError(
                    "running turn has invalid canonical event metadata"
                ) from exc
            if (
                kind not in _RECOVERY_DURABLE_KINDS
                or _RECOVERY_TIMESTAMP.fullmatch(occurred_at) is None
            ):
                raise ConversationConflictError(
                    "running turn has invalid canonical event metadata"
                )
            durable_bytes += len(raw_envelope)
            if durable_bytes > MAX_RECOVERY_DURABLE_BYTES_PER_RUN:
                raise ConversationConflictError(
                    "running turn durable events exceed their recovery byte limit"
                )

            envelope = _decode_recovery_envelope(raw_envelope)
            if set(envelope) != {
                "schema_version",
                "event_id",
                "agent_id",
                "conversation_id",
                "turn_id",
                "run_id",
                "parent_run_id",
                "seq",
                "occurred_at",
                "kind",
                "durability",
                "payload",
            }:
                raise ConversationConflictError(
                    "running turn has an invalid canonical event envelope"
                )
            event_id = envelope.get("event_id")
            if (
                envelope.get("schema_version") != SCHEMA_VERSION
                or not isinstance(event_id, str)
                or _RECOVERY_EVENT_ID.fullmatch(event_id) is None
                or envelope.get("agent_id") != self.agent_id
                or envelope.get("conversation_id") != existing.conversation_id
                or envelope.get("turn_id") != existing.turn_id
                or envelope.get("run_id") != existing.run_id
                or envelope.get("parent_run_id") is not None
                or envelope.get("seq") != seq
                or isinstance(envelope.get("seq"), bool)
                or envelope.get("occurred_at") != occurred_at
                or envelope.get("kind") != kind
                or envelope.get("durability") != "durable"
                or not isinstance(envelope.get("payload"), dict)
            ):
                raise ConversationConflictError(
                    "running turn has an invalid canonical event envelope"
                )
            payload = envelope["payload"]
            assert isinstance(payload, dict)

            if event_count == 1:
                if seq != 1 or kind != "run.started":
                    raise ConversationConflictError(
                        "running turn has no canonical start event"
                    )
            elif kind == "run.started":
                raise ConversationConflictError(
                    "running turn has more than one canonical start event"
                )

            if kind in {"run.completed", "run.failed", "run.cancelled"}:
                raise ConversationConflictError(
                    "running turn already has a terminal event"
                )
            if kind == "assistant.block.started":
                block_payload = _exact_recovery_payload(
                    payload, {"block_id", "block_type"}
                )
                block_id = _recovery_worker_id(
                    block_payload.get("block_id"), "block_id"
                )
                if (
                    block_payload.get("block_type") != "content"
                    or block_id in seen_blocks
                    or open_block_id is not None
                ):
                    raise ConversationConflictError(
                        "running turn has an invalid assistant block start"
                    )
                seen_blocks.add(block_id)
                open_block_id = block_id
            elif kind == "assistant.block.finished":
                block_payload = _exact_recovery_payload(
                    payload, {"block_id", "content"}
                )
                block_id = _recovery_worker_id(
                    block_payload.get("block_id"), "block_id"
                )
                _bounded_recovery_string(
                    block_payload.get("content"),
                    maximum_bytes=MAX_RECOVERY_WORKER_TEXT_BYTES,
                    field="assistant content",
                    allow_empty=True,
                )
                if block_id != open_block_id:
                    raise ConversationConflictError(
                        "running turn closes an unknown assistant block"
                    )
                open_block_id = None
            elif kind == "assistant.block.discarded":
                block_payload = _exact_recovery_payload(
                    payload, {"block_id", "reason"}
                )
                block_id = _recovery_worker_id(
                    block_payload.get("block_id"), "block_id"
                )
                if (
                    block_id != open_block_id
                    or block_payload.get("reason") not in _RECOVERY_BLOCK_REASONS
                ):
                    raise ConversationConflictError(
                        "running turn discards an unknown assistant block"
                    )
                open_block_id = None
            elif kind == "tool.call.requested":
                tool_payload = _exact_recovery_payload(
                    payload, {"call_id", "tool_id", "arguments"}
                )
                call_id = _recovery_worker_id(
                    tool_payload.get("call_id"), "call_id"
                )
                tool_id = _bounded_recovery_string(
                    tool_payload.get("tool_id"),
                    maximum_bytes=128,
                    field="tool_id",
                )
                spec = _RECOVERY_TOOL_SPECS.get(tool_id)
                try:
                    if spec is None:
                        raise ValueError("unknown Tool")
                    spec.validate_arguments(tool_payload.get("arguments"))
                except (UnicodeError, ValueError) as exc:
                    raise ConversationConflictError(
                        "running turn has invalid Tool arguments"
                    ) from exc
                if call_id in seen_calls or pending_call_id is not None:
                    raise ConversationConflictError(
                        "running turn has an invalid Tool request"
                    )
                seen_calls.add(call_id)
                pending_call_id = call_id
                pending_tool_id = tool_id
                pending_tool_started = False
            elif kind == "tool.call.started":
                tool_payload = _exact_recovery_payload(
                    payload, {"call_id", "tool_id"}
                )
                call_id = _recovery_worker_id(
                    tool_payload.get("call_id"), "call_id"
                )
                tool_id = _bounded_recovery_string(
                    tool_payload.get("tool_id"),
                    maximum_bytes=128,
                    field="tool_id",
                )
                if (
                    call_id != pending_call_id
                    or tool_id != pending_tool_id
                    or pending_tool_started
                ):
                    raise ConversationConflictError(
                        "running turn has an invalid Tool start"
                    )
                pending_tool_started = True
            elif kind == "tool.call.finished":
                if not isinstance(payload, dict) or set(payload) not in (
                    {"call_id", "outcome", "result"},
                    {"call_id", "tool_id", "outcome", "result"},
                ):
                    raise ConversationConflictError(
                        "canonical event payload is invalid"
                    )
                call_id = _recovery_worker_id(payload.get("call_id"), "call_id")
                if (
                    call_id != pending_call_id
                    or pending_tool_id is None
                    or not pending_tool_started
                    or payload.get("outcome")
                    not in {"succeeded", "failed", "cancelled"}
                ):
                    raise ConversationConflictError(
                        "running turn has an invalid Tool finish"
                    )
                if "tool_id" in payload:
                    tool_id = _bounded_recovery_string(
                        payload.get("tool_id"),
                        maximum_bytes=128,
                        field="tool_id",
                    )
                    if tool_id != pending_tool_id:
                        raise ConversationConflictError(
                            "running turn finishes the wrong Tool"
                        )
                spec = _RECOVERY_TOOL_SPECS.get(pending_tool_id)
                try:
                    if spec is None:
                        raise ValueError("unknown Tool")
                    spec.validate_result(payload.get("result"))
                except (UnicodeError, ValueError) as exc:
                    raise ConversationConflictError(
                        "running turn has an invalid Tool result"
                    ) from exc
                pending_call_id = None
                pending_tool_id = None
                pending_tool_started = False
            elif kind != "run.started":
                raise ConversationConflictError(
                    "running turn has an unsupported durable event"
                )
            last_seq = seq

        closure_events: list[tuple[str, dict[str, object]]] = []
        if open_block_id is not None:
            closure_events.append(
                (
                    "assistant.block.discarded",
                    {"block_id": open_block_id, "reason": "runtime_failure"},
                )
            )
        if pending_call_id is not None and pending_tool_id is not None:
            if not pending_tool_started:
                closure_events.append(
                    (
                        "tool.call.started",
                        {"call_id": pending_call_id, "tool_id": pending_tool_id},
                    )
                )
            closure_events.append(
                (
                    "tool.call.finished",
                    {
                        "call_id": pending_call_id,
                        "tool_id": pending_tool_id,
                        "outcome": "failed",
                        "result": "Control Plane restarted",
                    },
                )
            )
        return _RecoveryScan(
            last_seq,
            event_count,
            durable_bytes,
            tuple(closure_events),
        )

    def recover_running_as_interrupted(self) -> tuple[ConversationTurn, ...]:
        """Fail closed after restart; interrupted partial output is not history."""

        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                rows = self._connection.execute(
                    """
                    SELECT t.turn_id, t.conversation_id, t.run_id, t.position,
                           t.status, t.user_content, t.assistant_content,
                           t.created_at, t.updated_at
                    FROM conversation_turns AS t
                    JOIN conversations AS c
                      ON c.conversation_id = t.conversation_id
                    WHERE c.agent_id = ? AND t.status = 'running'
                    ORDER BY t.conversation_id, t.position
                    """,
                    (self.agent_id,),
                ).fetchall()
                for row in rows:
                    existing = _turn_from_row(row)
                    recovery = self._scan_recovery_events(existing)
                    if recovery.event_count:
                        synthetic = [
                            *recovery.closure_events,
                            (
                                "run.failed",
                                {
                                    "code": "control_restarted",
                                    "message": (
                                        "Control Plane restarted before the Run "
                                        "reached a terminal state."
                                    ),
                                    "retryable": True,
                                    "usage": {
                                        "input_tokens": 0,
                                        "output_tokens": 0,
                                        "last_input_tokens": 0,
                                        "complete": False,
                                    },
                                },
                            ),
                        ]
                        if (
                            recovery.last_seq + len(synthetic)
                            > MAX_RECOVERY_EVENTS_PER_RUN
                            or recovery.last_seq + len(synthetic)
                            > MAX_RECOVERY_SEQUENCE
                        ):
                            raise ConversationConflictError(
                                "running turn has no recovery event capacity"
                            )
                        prepared: list[tuple[EventEnvelope, str]] = []
                        total_bytes = recovery.durable_bytes
                        for offset, (kind, payload) in enumerate(synthetic, start=1):
                            recovery_event = EventEnvelope(
                                event_id=uuid4().hex,
                                agent_id=self.agent_id,
                                conversation_id=existing.conversation_id,
                                turn_id=existing.turn_id,
                                run_id=existing.run_id,
                                seq=recovery.last_seq + offset,
                                occurred_at=timestamp,
                                kind=kind,
                                durability="durable",
                                payload=payload,
                            )
                            encoded = _encode_boundary_event(
                                recovery_event,
                                expected_kind=kind,
                                agent_id=self.agent_id,
                                conversation_id=existing.conversation_id,
                                turn_id=existing.turn_id,
                                run_id=existing.run_id,
                                minimum_seq=2,
                                exact_seq=recovery.last_seq + offset,
                            )
                            total_bytes += len(encoded.encode("utf-8"))
                            if total_bytes > MAX_RECOVERY_DURABLE_BYTES_PER_RUN:
                                raise ConversationConflictError(
                                    "running turn has no recovery byte capacity"
                                )
                            prepared.append((recovery_event, encoded))
                        for recovery_event, encoded in prepared:
                            self._insert_boundary_event(recovery_event, encoded)
                    cursor = self._connection.execute(
                        """
                        UPDATE conversations
                        SET active_run_id = NULL, updated_at = ?,
                            revision = revision + 1
                        WHERE conversation_id = ? AND agent_id = ?
                          AND active_run_id = ?
                        """,
                        (
                            timestamp,
                            existing.conversation_id,
                            self.agent_id,
                            existing.run_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationConflictError(
                            "running turn is not bound to its conversation"
                        )
                self._connection.execute(
                    """
                    UPDATE conversation_turns
                    SET status = 'interrupted', updated_at = ?
                    WHERE status = 'running'
                      AND conversation_id IN (
                          SELECT conversation_id FROM conversations WHERE agent_id = ?
                      )
                    """,
                    (timestamp, self.agent_id),
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not recover interrupted conversation turns"
                ) from exc
        return tuple(
            ConversationTurn(
                existing.turn_id,
                existing.conversation_id,
                existing.run_id,
                existing.position,
                "interrupted",
                existing.user_content,
                None,
                existing.created_at,
                timestamp,
            )
            for existing in (_turn_from_row(row) for row in rows)
        )

    def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        conversation_id = _validate_id(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row = self._connection.execute(
                    """
                    SELECT active_run_id FROM conversations
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (conversation_id, self.agent_id),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return ConversationDeleteResult(False, 0, 0)
                if row[0] is not None:
                    raise ConversationConflictError(
                        "cannot delete a conversation with an active Run"
                    )
                turn_count = self._connection.execute(
                    """
                    SELECT COUNT(*) FROM conversation_turns
                    WHERE conversation_id = ?
                    """,
                    (conversation_id,),
                ).fetchone()[0]
                events_cursor = self._connection.execute(
                    """
                    DELETE FROM events
                    WHERE run_id IN (
                        SELECT run_id FROM conversation_turns
                        WHERE conversation_id = ?
                    )
                    """,
                    (conversation_id,),
                )
                cursor = self._connection.execute(
                    """
                    DELETE FROM conversations
                    WHERE conversation_id = ? AND agent_id = ?
                    """,
                    (conversation_id, self.agent_id),
                )
                if cursor.rowcount != 1:
                    raise ConversationConflictError("conversation deletion was lost")
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not delete conversation"
                ) from exc
        return ConversationDeleteResult(
            True,
            turn_count,
            max(events_cursor.rowcount, 0),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                self._connection.close()
                os.close(self._directory_descriptor)
                self._closed = True

    def __enter__(self) -> ConversationStore:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "BeginTurnResult",
    "CommittedMessage",
    "Conversation",
    "ConversationConflictError",
    "ConversationDeleteResult",
    "ConversationNotFoundError",
    "ConversationStore",
    "ConversationStoreError",
    "ConversationStoreUnavailableError",
    "ConversationSnapshot",
    "ConversationSummary",
    "ConversationTurn",
    "conversation_message_id",
    "DATABASE_NAME",
    "MAX_ASSISTANT_CONTENT_BYTES",
    "MAX_CONVERSATIONS_PER_AGENT",
    "MAX_DATABASE_BYTES",
    "MAX_LIST_LIMIT",
    "MAX_TITLE_BYTES",
    "MAX_TURNS_PER_CONVERSATION",
    "MAX_USER_CONTENT_BYTES",
    "MessageRole",
    "TerminalTurnStatus",
    "TurnNotFoundError",
    "TurnStatus",
]

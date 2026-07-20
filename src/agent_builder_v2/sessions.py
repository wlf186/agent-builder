"""Agent-scoped conversation and turn persistence in the canonical journal.

The store shares an Agent's ``state.sqlite`` with :class:`EventJournal`.  Turn
acceptance and terminal state can therefore be committed with their canonical
boundary event instead of relying on a recoverably inconsistent dual write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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

from .contracts import (
    RUN_CURSOR_RESERVED_THROUGH,
    SCHEMA_VERSION,
    EventEnvelope,
    utc_now,
)
from .replay import (
    LEGACY_PROJECTION_VERSION,
    PROJECTION_VERSION,
    ProjectionSnapshot,
    ReplayCorruptionError,
    RunIdentity,
    decode_projection_snapshot,
    decode_durable_event,
    encode_projection_snapshot,
    project_durable_run,
)
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
MAX_RECOVERY_EVENTS_PER_RUN = RUN_CURSOR_RESERVED_THROUGH
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
MAX_OPERATION_RECORDS_PER_AGENT = 4_096
MAX_PROVIDER_CALLS_PER_RUN = 64
MAX_SNAPSHOT_BYTES = 65_536
MAX_LEDGER_TEXT_BYTES = 128
MAX_USAGE_TOKENS = 1_000_000_000

_SAFE_ID = re.compile(r"^[a-f0-9-]{32,36}$")
_RECOVERY_EVENT_ID = re.compile(r"^[a-f0-9]{32}$")
_RECOVERY_WORKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_RECOVERY_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_LEDGER_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_LEDGER_NAME = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_CURRENCY = re.compile(r"^[A-Z]{3,12}$")
_RECOVERY_DURABLE_KINDS = frozenset(
    {
        "run.started",
        "model.request.started",
        "model.response.finished",
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
OperationStatus = Literal[
    "intent", "dispatched", "succeeded", "failed", "cancelled", "outcome_unknown"
]
ProviderUsageStatus = Literal["started", "complete", "incomplete"]


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
class RunJournalState:
    run_id: str
    oldest_available_seq: int
    latest_durable_seq: int
    reserved_through: int
    terminal_seq: int | None
    terminal_kind: str | None
    availability: str
    event_count: int
    durable_bytes: int
    input_tokens: int
    output_tokens: int
    last_input_tokens: int
    usage_complete: bool


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    agent_id: str
    conversation_id: str | None
    turn_id: str | None
    run_id: str | None
    call_id: str | None
    capability_id: str
    policy_revision: str
    idempotency_key_hash: str
    request_digest: str
    status: OperationStatus
    executor_kind: str | None
    executor_identity_digest: str | None
    outcome_digest: str | None
    created_at: str
    dispatched_at: str | None
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class OperationMutation:
    record: OperationRecord
    changed: bool


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    run_id: str
    call_index: int
    provider: str
    model: str
    profile_digest: str
    context_plan_id: str
    estimated_input_tokens: int
    hard_input_tokens: int
    status: ProviderUsageStatus
    input_tokens: int | None
    output_tokens: int | None
    cost_minor_units: int | None
    currency: str | None
    pricing_profile_digest: str | None
    started_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ProviderUsageMutation:
    record: ProviderUsage
    changed: bool


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


def _ledger_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _LEDGER_DIGEST.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def _ledger_name(value: object, field: str) -> str:
    if not isinstance(value, str) or _LEDGER_NAME.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    return value


def _optional_ledger_id(value: object, field: str) -> str | None:
    return None if value is None else _validate_id(value, field)


def _usage_count(value: object, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= MAX_USAGE_TOKENS
    ):
        raise ValueError(f"invalid {field}")
    return value


def _run_journal_state_from_row(row: tuple[object, ...]) -> RunJournalState:
    values = list(row)
    values[-1] = bool(values[-1])
    return RunJournalState(*values)  # type: ignore[arg-type]


def _operation_from_row(row: tuple[object, ...]) -> OperationRecord:
    return OperationRecord(*row)  # type: ignore[arg-type]


def _provider_usage_from_row(row: tuple[object, ...]) -> ProviderUsage:
    return ProviderUsage(*row)  # type: ignore[arg-type]


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
                f"""
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
                CREATE UNIQUE INDEX IF NOT EXISTS events_one_terminal_per_run
                    ON events(run_id)
                    WHERE kind IN ('run.completed', 'run.failed', 'run.cancelled');
                CREATE TABLE IF NOT EXISTS run_journal_state (
                    run_id TEXT PRIMARY KEY
                        REFERENCES conversation_turns(run_id) ON DELETE CASCADE,
                    oldest_available_seq INTEGER NOT NULL CHECK (oldest_available_seq > 0),
                    latest_durable_seq INTEGER NOT NULL CHECK (latest_durable_seq > 0),
                    reserved_through INTEGER NOT NULL CHECK (reserved_through >= 1),
                    terminal_seq INTEGER,
                    terminal_kind TEXT CHECK (
                        terminal_kind IS NULL OR terminal_kind IN (
                            'run.completed', 'run.failed', 'run.cancelled'
                        )
                    ),
                    availability TEXT NOT NULL CHECK (
                        availability IN ('full', 'snapshot_only', 'pruned', 'corrupt')
                    ),
                    event_count INTEGER NOT NULL CHECK (event_count >= 0),
                    durable_bytes INTEGER NOT NULL CHECK (durable_bytes >= 0),
                    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
                    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
                    last_input_tokens INTEGER NOT NULL DEFAULT 0
                        CHECK (last_input_tokens >= 0),
                    usage_complete INTEGER NOT NULL DEFAULT 0
                        CHECK (usage_complete IN (0, 1)),
                    CHECK (
                        (terminal_seq IS NULL AND terminal_kind IS NULL)
                        OR (terminal_seq > 0 AND terminal_kind IS NOT NULL)
                    )
                );
                CREATE TABLE IF NOT EXISTS run_snapshots (
                    run_id TEXT PRIMARY KEY
                        REFERENCES run_journal_state(run_id) ON DELETE CASCADE,
                    projection_version TEXT NOT NULL,
                    through_seq INTEGER NOT NULL CHECK (through_seq > 0),
                    snapshot_json TEXT NOT NULL,
                    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
                    ephemeral_loss INTEGER NOT NULL CHECK (ephemeral_loss IN (0, 1)),
                    created_at TEXT NOT NULL,
                    CHECK (length(CAST(snapshot_json AS BLOB)) <= 65536)
                );
                CREATE TABLE IF NOT EXISTS operation_ledger (
                    operation_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    turn_id TEXT REFERENCES conversation_turns(turn_id) ON DELETE CASCADE,
                    run_id TEXT REFERENCES conversation_turns(run_id) ON DELETE CASCADE,
                    call_id TEXT,
                    capability_id TEXT NOT NULL,
                    policy_revision TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL CHECK (
                        length(idempotency_key_hash) = 64
                    ),
                    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'intent', 'dispatched', 'succeeded', 'failed',
                            'cancelled', 'outcome_unknown'
                        )
                    ),
                    executor_kind TEXT,
                    executor_identity_digest TEXT,
                    outcome_digest TEXT,
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    resolved_at TEXT,
                    UNIQUE (agent_id, idempotency_key_hash),
                    CHECK (
                        (status = 'intent' AND executor_kind IS NULL
                            AND executor_identity_digest IS NULL
                            AND dispatched_at IS NULL AND resolved_at IS NULL
                            AND outcome_digest IS NULL)
                        OR (status = 'dispatched' AND executor_kind IS NOT NULL
                            AND executor_identity_digest IS NOT NULL
                            AND dispatched_at IS NOT NULL AND resolved_at IS NULL
                            AND outcome_digest IS NULL)
                        OR (status IN ('succeeded', 'failed', 'cancelled')
                            AND executor_kind IS NOT NULL
                            AND executor_identity_digest IS NOT NULL
                            AND dispatched_at IS NOT NULL AND resolved_at IS NOT NULL
                            AND outcome_digest IS NOT NULL)
                        OR (status = 'outcome_unknown' AND executor_kind IS NOT NULL
                            AND executor_identity_digest IS NOT NULL
                            AND dispatched_at IS NOT NULL AND resolved_at IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS operation_ledger_run
                    ON operation_ledger(run_id, call_id);
                CREATE TABLE IF NOT EXISTS provider_usage (
                    run_id TEXT NOT NULL
                        REFERENCES run_journal_state(run_id) ON DELETE CASCADE,
                    call_index INTEGER NOT NULL CHECK (
                        call_index BETWEEN 1 AND 64
                    ),
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    profile_digest TEXT NOT NULL CHECK (length(profile_digest) = 64),
                    context_plan_id TEXT NOT NULL,
                    estimated_input_tokens INTEGER NOT NULL CHECK (
                        estimated_input_tokens BETWEEN 0 AND 1000000000
                    ),
                    hard_input_tokens INTEGER NOT NULL CHECK (
                        hard_input_tokens BETWEEN 1 AND 1000000000
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('started', 'complete', 'incomplete')
                    ),
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_minor_units INTEGER,
                    currency TEXT,
                    pricing_profile_digest TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (run_id, call_index),
                    CHECK (estimated_input_tokens <= hard_input_tokens),
                    CHECK (
                        (status = 'started' AND input_tokens IS NULL
                            AND output_tokens IS NULL AND completed_at IS NULL)
                        OR (status = 'incomplete' AND input_tokens IS NULL
                            AND output_tokens IS NULL AND completed_at IS NOT NULL)
                        OR (status = 'complete' AND input_tokens BETWEEN 0 AND 1000000000
                            AND output_tokens BETWEEN 0 AND 1000000000
                            AND completed_at IS NOT NULL)
                    ),
                    CHECK (
                        (cost_minor_units IS NULL AND currency IS NULL
                            AND pricing_profile_digest IS NULL)
                        OR (cost_minor_units >= 0 AND currency IS NOT NULL
                            AND pricing_profile_digest IS NOT NULL
                            AND length(pricing_profile_digest) = 64)
                    )
                );
                INSERT OR IGNORE INTO run_journal_state(
                    run_id, oldest_available_seq, latest_durable_seq,
                    reserved_through, terminal_seq, terminal_kind, availability,
                    event_count, durable_bytes, input_tokens, output_tokens,
                    last_input_tokens, usage_complete
                )
                SELECT
                    t.run_id,
                    COALESCE(MIN(e.seq), 1),
                    COALESCE(MAX(e.seq), 1),
                    {RUN_CURSOR_RESERVED_THROUGH},
                    MAX(CASE WHEN e.kind IN (
                        'run.completed', 'run.failed', 'run.cancelled'
                    ) THEN e.seq END),
                    MAX(CASE WHEN e.kind IN (
                        'run.completed', 'run.failed', 'run.cancelled'
                    ) THEN e.kind END),
                    'full',
                    COUNT(e.seq),
                    COALESCE(SUM(length(CAST(e.envelope_json AS BLOB))), 0),
                    0, 0, 0, 0
                FROM conversation_turns AS t
                LEFT JOIN events AS e ON e.run_id = t.run_id
                GROUP BY t.run_id;
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

    def _owned_run_exists(self, run_id: str) -> bool:
        return self._connection.execute(
            """
            SELECT 1
            FROM conversation_turns AS t
            JOIN conversations AS c ON c.conversation_id = t.conversation_id
            WHERE t.run_id = ? AND c.agent_id = ?
            """,
            (run_id, self.agent_id),
        ).fetchone() is not None

    def _run_journal_row(self, run_id: str) -> tuple[object, ...] | None:
        return self._connection.execute(
            """
            SELECT s.run_id, s.oldest_available_seq, s.latest_durable_seq,
                   s.reserved_through, s.terminal_seq, s.terminal_kind,
                   s.availability, s.event_count, s.durable_bytes,
                   s.input_tokens, s.output_tokens, s.last_input_tokens,
                   s.usage_complete
            FROM run_journal_state AS s
            JOIN conversation_turns AS t ON t.run_id = s.run_id
            JOIN conversations AS c ON c.conversation_id = t.conversation_id
            WHERE s.run_id = ? AND c.agent_id = ?
            """,
            (run_id, self.agent_id),
        ).fetchone()

    def _append_running_boundary_locked(
        self,
        event: EventEnvelope,
        *,
        expected_kind: str,
    ) -> str:
        """Append one nonterminal boundary and advance its journal atomically."""

        identity = self._resolve_run_identity_locked(event.run_id)
        turn_row = self._turn_for_run(event.run_id)
        state_row = self._run_journal_row(event.run_id)
        if identity is None or turn_row is None or state_row is None:
            raise TurnNotFoundError("Run has no conversation turn")
        turn = _turn_from_row(turn_row)
        state = _run_journal_state_from_row(state_row)
        encoded = _encode_boundary_event(
            event,
            expected_kind=expected_kind,
            agent_id=identity.agent_id,
            conversation_id=identity.conversation_id,
            turn_id=identity.turn_id,
            run_id=identity.run_id,
            minimum_seq=2,
        )
        encoded_bytes = len(encoded.encode("utf-8"))
        if (
            turn.status != "running"
            or state.oldest_available_seq != 1
            or state.reserved_through != RUN_CURSOR_RESERVED_THROUGH
            or state.terminal_seq is not None
            or state.terminal_kind is not None
            or state.availability != "full"
            or not 1 <= state.latest_durable_seq < event.seq <= MAX_RECOVERY_SEQUENCE
            or not 1 <= state.event_count < MAX_RECOVERY_EVENTS_PER_RUN
            or not 1 <= state.durable_bytes
            or state.durable_bytes + encoded_bytes
            > MAX_RECOVERY_DURABLE_BYTES_PER_RUN
        ):
            raise ConversationConflictError(
                "Run journal rejects provider boundary append"
            )
        self._insert_boundary_event(event, encoded)
        cursor = self._connection.execute(
            """
            UPDATE run_journal_state
            SET latest_durable_seq = ?, event_count = event_count + 1,
                durable_bytes = durable_bytes + ?
            WHERE run_id = ? AND latest_durable_seq = ?
              AND terminal_seq IS NULL AND availability = 'full'
            """,
            (
                event.seq,
                encoded_bytes,
                event.run_id,
                state.latest_durable_seq,
            ),
        )
        if cursor.rowcount != 1:
            raise ConversationConflictError(
                "Run journal provider boundary update was lost"
            )
        return encoded

    def get_run_journal_state(self, run_id: str) -> RunJournalState:
        run_id = _validate_id(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            try:
                row = self._run_journal_row(run_id)
            except sqlite3.Error as exc:
                raise ConversationStoreUnavailableError(
                    "could not read Run journal state"
                ) from exc
        if row is None:
            raise TurnNotFoundError("Run has no journal state")
        return _run_journal_state_from_row(row)

    def _resolve_run_identity_locked(self, run_id: str) -> RunIdentity | None:
        row = self._connection.execute(
            """
            SELECT c.agent_id, t.conversation_id, t.turn_id, t.run_id
            FROM conversation_turns AS t
            JOIN conversations AS c ON c.conversation_id = t.conversation_id
            WHERE t.run_id = ? AND c.agent_id = ?
            """,
            (run_id, self.agent_id),
        ).fetchone()
        return None if row is None else RunIdentity(*row)

    def resolve_run_identity(self, run_id: str) -> RunIdentity:
        run_id = _validate_id(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            try:
                identity = self._resolve_run_identity_locked(run_id)
            except sqlite3.Error as exc:
                raise ConversationStoreUnavailableError(
                    "could not resolve Run identity"
                ) from exc
        if identity is None:
            raise TurnNotFoundError("Run has no conversation turn")
        return identity

    def read_run_snapshot(self, run_id: str) -> ProjectionSnapshot | None:
        run_id = _validate_id(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            try:
                identity = self._resolve_run_identity_locked(run_id)
                if identity is None:
                    raise TurnNotFoundError("Run has no conversation turn")
                row = self._connection.execute(
                    """
                    SELECT p.projection_version, p.through_seq, p.snapshot_json,
                           p.source_digest, s.latest_durable_seq, s.terminal_seq,
                           s.terminal_kind
                    FROM run_snapshots AS p
                    JOIN run_journal_state AS s ON s.run_id = p.run_id
                    WHERE p.run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
            except ConversationStoreError:
                raise
            except sqlite3.Error as exc:
                raise ConversationStoreUnavailableError(
                    "could not read Run snapshot"
                ) from exc
        if row is None:
            return None
        (
            version,
            through_seq,
            raw_snapshot,
            stored_digest,
            latest_durable_seq,
            terminal_seq,
            terminal_kind,
        ) = row
        if (
            version not in {LEGACY_PROJECTION_VERSION, PROJECTION_VERSION}
            or not isinstance(through_seq, int)
            or isinstance(through_seq, bool)
            or through_seq != latest_durable_seq
            or through_seq != terminal_seq
            or terminal_kind not in {
                "run.completed",
                "run.failed",
                "run.cancelled",
            }
            or not isinstance(stored_digest, str)
            or _LEDGER_DIGEST.fullmatch(stored_digest) is None
        ):
            raise ConversationConflictError("Run snapshot is invalid")
        if not isinstance(raw_snapshot, str):
            raise ConversationConflictError("Run snapshot is invalid")
        try:
            encoded_snapshot = raw_snapshot.encode("utf-8")
            if hashlib.sha256(encoded_snapshot).hexdigest() != stored_digest:
                raise ConversationConflictError("Run snapshot digest is invalid")
            snapshot = decode_projection_snapshot(
                encoded_snapshot,
                expected_identity=identity,
                expected_through_seq=through_seq,
            )
        except ReplayCorruptionError as exc:
            raise ConversationConflictError("Run snapshot is invalid") from exc
        document = snapshot.document
        if (
            snapshot.version != version
            or
            not isinstance(document.get("started"), dict)
            or not isinstance(document.get("blocks"), list)
            or not isinstance(document.get("tools"), list)
            or not isinstance(document.get("terminal"), dict)
            or set(document["terminal"]) != {"kind", "payload"}  # type: ignore[arg-type]
            or document["terminal"].get("kind") != terminal_kind  # type: ignore[union-attr]
            or not isinstance(document["terminal"].get("payload"), dict)  # type: ignore[union-attr]
        ):
            raise ConversationConflictError("Run snapshot is invalid")
        return snapshot

    def get_run_snapshot(self, run_id: str) -> dict[str, object] | None:
        snapshot = self.read_run_snapshot(run_id)
        return None if snapshot is None else snapshot.to_dict()

    def _operation_row(
        self, *, operation_id: str | None = None, idempotency_key_hash: str | None = None
    ) -> tuple[object, ...] | None:
        if (operation_id is None) == (idempotency_key_hash is None):
            raise ValueError("one operation lookup identity is required")
        field = "operation_id" if operation_id is not None else "idempotency_key_hash"
        value = operation_id if operation_id is not None else idempotency_key_hash
        return self._connection.execute(
            f"""
            SELECT operation_id, agent_id, conversation_id, turn_id, run_id,
                   call_id, capability_id, policy_revision, idempotency_key_hash,
                   request_digest, status, executor_kind,
                   executor_identity_digest, outcome_digest, created_at,
                   dispatched_at, resolved_at
            FROM operation_ledger
            WHERE {field} = ? AND agent_id = ?
            """,
            (value, self.agent_id),
        ).fetchone()

    def _validate_operation_target(
        self,
        conversation_id: str | None,
        turn_id: str | None,
        run_id: str | None,
    ) -> None:
        if run_id is not None:
            row = self._connection.execute(
                """
                SELECT t.conversation_id, t.turn_id
                FROM conversation_turns AS t
                JOIN conversations AS c ON c.conversation_id = t.conversation_id
                WHERE t.run_id = ? AND c.agent_id = ?
                """,
                (run_id, self.agent_id),
            ).fetchone()
            if row is None or (conversation_id is not None and row[0] != conversation_id) or (
                turn_id is not None and row[1] != turn_id
            ):
                raise TurnNotFoundError("operation target Run was not found")
            return
        if turn_id is not None:
            row = self._connection.execute(
                """
                SELECT t.conversation_id
                FROM conversation_turns AS t
                JOIN conversations AS c ON c.conversation_id = t.conversation_id
                WHERE t.turn_id = ? AND c.agent_id = ?
                """,
                (turn_id, self.agent_id),
            ).fetchone()
            if row is None or (conversation_id is not None and row[0] != conversation_id):
                raise TurnNotFoundError("operation target Turn was not found")
            return
        if conversation_id is not None and self._connection.execute(
            """
            SELECT 1 FROM conversations WHERE conversation_id = ? AND agent_id = ?
            """,
            (conversation_id, self.agent_id),
        ).fetchone() is None:
            raise ConversationNotFoundError("operation target conversation was not found")

    def record_operation_intent(
        self,
        *,
        operation_id: str,
        capability_id: str,
        policy_revision: str,
        idempotency_key_hash: str,
        request_digest: str,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
    ) -> OperationMutation:
        operation_id = _validate_id(operation_id, "operation_id")
        capability_id = _ledger_name(capability_id, "capability_id")
        policy_revision = _ledger_name(policy_revision, "policy_revision")
        idempotency_key_hash = _ledger_digest(
            idempotency_key_hash, "idempotency_key_hash"
        )
        request_digest = _ledger_digest(request_digest, "request_digest")
        conversation_id = _optional_ledger_id(conversation_id, "conversation_id")
        turn_id = _optional_ledger_id(turn_id, "turn_id")
        run_id = _optional_ledger_id(run_id, "run_id")
        call_id = None if call_id is None else _ledger_name(call_id, "call_id")
        timestamp = utc_now()
        immutable = (
            self.agent_id,
            conversation_id,
            turn_id,
            run_id,
            call_id,
            capability_id,
            policy_revision,
            idempotency_key_hash,
            request_digest,
        )
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                existing_row = self._operation_row(
                    idempotency_key_hash=idempotency_key_hash
                )
                if existing_row is not None:
                    existing = _operation_from_row(existing_row)
                    if (
                        existing.agent_id,
                        existing.conversation_id,
                        existing.turn_id,
                        existing.run_id,
                        existing.call_id,
                        existing.capability_id,
                        existing.policy_revision,
                        existing.idempotency_key_hash,
                        existing.request_digest,
                    ) != immutable:
                        raise ConversationConflictError(
                            "idempotency key was reused with a different operation"
                        )
                    self._connection.commit()
                    return OperationMutation(existing, False)
                self._validate_operation_target(conversation_id, turn_id, run_id)
                count = self._connection.execute(
                    "SELECT COUNT(*) FROM operation_ledger WHERE agent_id = ?",
                    (self.agent_id,),
                ).fetchone()[0]
                if count >= MAX_OPERATION_RECORDS_PER_AGENT:
                    raise ConversationConflictError("operation ledger capacity is exhausted")
                self._connection.execute(
                    """
                    INSERT INTO operation_ledger(
                        operation_id, agent_id, conversation_id, turn_id, run_id,
                        call_id, capability_id, policy_revision,
                        idempotency_key_hash, request_digest, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?)
                    """,
                    (operation_id, *immutable, timestamp),
                )
                row = self._operation_row(operation_id=operation_id)
                assert row is not None
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ConversationConflictError("operation identity already exists") from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not record operation intent"
                ) from exc
        return OperationMutation(_operation_from_row(row), True)

    def mark_operation_dispatched(
        self,
        operation_id: str,
        *,
        executor_kind: str,
        executor_identity_digest: str,
    ) -> OperationMutation:
        operation_id = _validate_id(operation_id, "operation_id")
        executor_kind = _ledger_name(executor_kind, "executor_kind")
        executor_identity_digest = _ledger_digest(
            executor_identity_digest, "executor_identity_digest"
        )
        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row = self._operation_row(operation_id=operation_id)
                if row is None:
                    raise TurnNotFoundError("operation was not found")
                existing = _operation_from_row(row)
                changed = False
                if existing.status == "intent":
                    cursor = self._connection.execute(
                        """
                        UPDATE operation_ledger
                        SET status = 'dispatched', executor_kind = ?,
                            executor_identity_digest = ?, dispatched_at = ?
                        WHERE operation_id = ? AND agent_id = ? AND status = 'intent'
                        """,
                        (
                            executor_kind,
                            executor_identity_digest,
                            timestamp,
                            operation_id,
                            self.agent_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationConflictError("operation dispatch transition was lost")
                    changed = True
                elif (
                    existing.executor_kind != executor_kind
                    or existing.executor_identity_digest != executor_identity_digest
                ):
                    raise ConversationConflictError(
                        "operation is bound to a different executor"
                    )
                row = self._operation_row(operation_id=operation_id)
                assert row is not None
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not record operation dispatch"
                ) from exc
        return OperationMutation(_operation_from_row(row), changed)

    def record_operation_outcome(
        self,
        operation_id: str,
        status: Literal["succeeded", "failed", "cancelled", "outcome_unknown"],
        *,
        outcome_digest: str | None = None,
    ) -> OperationMutation:
        operation_id = _validate_id(operation_id, "operation_id")
        if status not in {"succeeded", "failed", "cancelled", "outcome_unknown"}:
            raise ValueError("invalid operation outcome")
        if status == "outcome_unknown":
            if outcome_digest is not None:
                raise ValueError("outcome_unknown cannot claim an outcome digest")
        else:
            outcome_digest = _ledger_digest(outcome_digest, "outcome_digest")
        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row = self._operation_row(operation_id=operation_id)
                if row is None:
                    raise TurnNotFoundError("operation was not found")
                existing = _operation_from_row(row)
                changed = False
                if existing.status == "dispatched":
                    cursor = self._connection.execute(
                        """
                        UPDATE operation_ledger
                        SET status = ?, outcome_digest = ?, resolved_at = ?
                        WHERE operation_id = ? AND agent_id = ?
                          AND status = 'dispatched'
                        """,
                        (
                            status,
                            outcome_digest,
                            timestamp,
                            operation_id,
                            self.agent_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationConflictError("operation outcome transition was lost")
                    changed = True
                elif existing.status != status or existing.outcome_digest != outcome_digest:
                    raise ConversationConflictError("operation already has another outcome")
                row = self._operation_row(operation_id=operation_id)
                assert row is not None
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not record operation outcome"
                ) from exc
        return OperationMutation(_operation_from_row(row), changed)

    def _recover_dispatched_operations_locked(self, timestamp: str) -> int:
        cursor = self._connection.execute(
            """
            UPDATE operation_ledger
            SET status = 'outcome_unknown', outcome_digest = NULL, resolved_at = ?
            WHERE agent_id = ? AND status = 'dispatched'
            """,
            (timestamp, self.agent_id),
        )
        return max(cursor.rowcount, 0)

    def recover_dispatched_operations(self) -> int:
        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                count = self._recover_dispatched_operations_locked(timestamp)
                self._connection.commit()
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not recover dispatched operations"
                ) from exc
        return count

    def _provider_usage_row(
        self, run_id: str, call_index: int
    ) -> tuple[object, ...] | None:
        return self._connection.execute(
            """
            SELECT u.run_id, u.call_index, u.provider, u.model,
                   u.profile_digest, u.context_plan_id,
                   u.estimated_input_tokens, u.hard_input_tokens, u.status,
                   u.input_tokens, u.output_tokens, u.cost_minor_units,
                   u.currency, u.pricing_profile_digest,
                   u.started_at, u.completed_at
            FROM provider_usage AS u
            JOIN run_journal_state AS s ON s.run_id = u.run_id
            JOIN conversation_turns AS t ON t.run_id = s.run_id
            JOIN conversations AS c ON c.conversation_id = t.conversation_id
            WHERE u.run_id = ? AND u.call_index = ? AND c.agent_id = ?
            """,
            (run_id, call_index, self.agent_id),
        ).fetchone()

    def _start_provider_usage_locked(
        self,
        immutable: tuple[object, ...],
        *,
        timestamp: str,
    ) -> tuple[tuple[object, ...], bool]:
        run_id = immutable[0]
        call_index = immutable[1]
        assert isinstance(run_id, str) and isinstance(call_index, int)
        if not self._owned_run_exists(run_id):
            raise TurnNotFoundError("Run has no conversation turn")
        row = self._provider_usage_row(run_id, call_index)
        if row is not None:
            existing = _provider_usage_from_row(row)
            if (
                existing.run_id,
                existing.call_index,
                existing.provider,
                existing.model,
                existing.profile_digest,
                existing.context_plan_id,
                existing.estimated_input_tokens,
                existing.hard_input_tokens,
            ) != immutable:
                raise ConversationConflictError(
                    "provider call identity was reused with different metadata"
                )
            return row, False
        self._connection.execute(
            """
            INSERT INTO provider_usage(
                run_id, call_index, provider, model, profile_digest,
                context_plan_id, estimated_input_tokens, hard_input_tokens,
                status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)
            """,
            (*immutable, timestamp),
        )
        row = self._provider_usage_row(run_id, call_index)
        assert row is not None
        return row, True

    def _complete_provider_usage_locked(
        self,
        run_id: str,
        call_index: int,
        outcome: tuple[object, ...],
        *,
        timestamp: str,
    ) -> tuple[tuple[object, ...], bool]:
        row = self._provider_usage_row(run_id, call_index)
        if row is None:
            raise TurnNotFoundError("provider call was not found")
        existing = _provider_usage_from_row(row)
        input_tokens = outcome[0]
        assert isinstance(input_tokens, int)
        if input_tokens > existing.hard_input_tokens:
            raise ConversationConflictError(
                "provider usage exceeds the hard input budget"
            )
        changed = False
        if existing.status == "started":
            cursor = self._connection.execute(
                """
                UPDATE provider_usage
                SET status = 'complete', input_tokens = ?, output_tokens = ?,
                    cost_minor_units = ?, currency = ?,
                    pricing_profile_digest = ?, completed_at = ?
                WHERE run_id = ? AND call_index = ? AND status = 'started'
                """,
                (
                    *outcome,
                    timestamp,
                    run_id,
                    call_index,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationConflictError("provider usage transition was lost")
            changed = True
        elif existing.status != "complete" or (
            existing.input_tokens,
            existing.output_tokens,
            existing.cost_minor_units,
            existing.currency,
            existing.pricing_profile_digest,
        ) != outcome:
            raise ConversationConflictError(
                "provider call already has another usage outcome"
            )
        row = self._provider_usage_row(run_id, call_index)
        assert row is not None
        return row, changed

    def start_provider_usage(
        self,
        run_id: str,
        call_index: int,
        *,
        provider: str,
        model: str,
        profile_digest: str,
        context_plan_id: str,
        estimated_input_tokens: int,
        hard_input_tokens: int,
    ) -> ProviderUsageMutation:
        run_id = _validate_id(run_id, "run_id")
        if (
            not isinstance(call_index, int)
            or isinstance(call_index, bool)
            or not 1 <= call_index <= MAX_PROVIDER_CALLS_PER_RUN
        ):
            raise ValueError("invalid provider call_index")
        provider = _ledger_name(provider, "provider")
        model = _ledger_name(model, "model")
        profile_digest = _ledger_digest(profile_digest, "profile_digest")
        context_plan_id = _ledger_name(context_plan_id, "context_plan_id")
        estimated_input_tokens = _usage_count(
            estimated_input_tokens, "estimated_input_tokens"
        )
        hard_input_tokens = _usage_count(
            hard_input_tokens, "hard_input_tokens", positive=True
        )
        if estimated_input_tokens > hard_input_tokens:
            raise ValueError("estimated input exceeds the hard input budget")
        timestamp = utc_now()
        immutable = (
            run_id,
            call_index,
            provider,
            model,
            profile_digest,
            context_plan_id,
            estimated_input_tokens,
            hard_input_tokens,
        )
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row, changed = self._start_provider_usage_locked(
                    immutable,
                    timestamp=timestamp,
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not start provider usage record"
                ) from exc
        return ProviderUsageMutation(_provider_usage_from_row(row), changed)

    def complete_provider_usage(
        self,
        run_id: str,
        call_index: int,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_minor_units: int | None = None,
        currency: str | None = None,
        pricing_profile_digest: str | None = None,
    ) -> ProviderUsageMutation:
        run_id = _validate_id(run_id, "run_id")
        if (
            not isinstance(call_index, int)
            or isinstance(call_index, bool)
            or not 1 <= call_index <= MAX_PROVIDER_CALLS_PER_RUN
        ):
            raise ValueError("invalid provider call_index")
        input_tokens = _usage_count(input_tokens, "input_tokens")
        output_tokens = _usage_count(output_tokens, "output_tokens")
        if cost_minor_units is None:
            if currency is not None or pricing_profile_digest is not None:
                raise ValueError("incomplete provider cost metadata")
        else:
            cost_minor_units = _usage_count(cost_minor_units, "cost_minor_units")
            if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
                raise ValueError("invalid currency")
            pricing_profile_digest = _ledger_digest(
                pricing_profile_digest, "pricing_profile_digest"
            )
        timestamp = utc_now()
        outcome = (
            input_tokens,
            output_tokens,
            cost_minor_units,
            currency,
            pricing_profile_digest,
        )
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row, changed = self._complete_provider_usage_locked(
                    run_id,
                    call_index,
                    outcome,
                    timestamp=timestamp,
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not complete provider usage record"
                ) from exc
        return ProviderUsageMutation(_provider_usage_from_row(row), changed)

    def start_provider_usage_with_event(
        self,
        run_id: str,
        call_index: int,
        *,
        provider: str,
        model: str,
        profile_digest: str,
        context_plan_id: str,
        estimated_input_tokens: int,
        hard_input_tokens: int,
        boundary_event: EventEnvelope,
    ) -> ProviderUsageMutation:
        """Atomically admit provider usage and its canonical request boundary."""

        run_id = _validate_id(run_id, "run_id")
        if (
            not isinstance(call_index, int)
            or isinstance(call_index, bool)
            or not 1 <= call_index <= MAX_PROVIDER_CALLS_PER_RUN
        ):
            raise ValueError("invalid provider call_index")
        provider = _ledger_name(provider, "provider")
        model = _ledger_name(model, "model")
        profile_digest = _ledger_digest(profile_digest, "profile_digest")
        context_plan_id = _ledger_name(context_plan_id, "context_plan_id")
        estimated_input_tokens = _usage_count(
            estimated_input_tokens, "estimated_input_tokens"
        )
        hard_input_tokens = _usage_count(
            hard_input_tokens, "hard_input_tokens", positive=True
        )
        if estimated_input_tokens > hard_input_tokens:
            raise ValueError("estimated input exceeds the hard input budget")
        payload = (
            boundary_event.payload
            if isinstance(boundary_event, EventEnvelope)
            else None
        )
        if (
            not isinstance(payload, dict)
            or payload.get("iteration") != call_index
            or payload.get("context_plan_id") != context_plan_id
            or payload.get("estimated_input_tokens") != estimated_input_tokens
        ):
            raise ValueError("provider request boundary disagrees with its usage")
        immutable = (
            run_id,
            call_index,
            provider,
            model,
            profile_digest,
            context_plan_id,
            estimated_input_tokens,
            hard_input_tokens,
        )
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row, changed = self._start_provider_usage_locked(
                    immutable,
                    timestamp=boundary_event.occurred_at,
                )
                self._append_running_boundary_locked(
                    boundary_event,
                    expected_kind="model.request.started",
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ConversationConflictError(
                    "provider request boundary already exists"
                ) from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not commit provider request boundary"
                ) from exc
        return ProviderUsageMutation(_provider_usage_from_row(row), changed)

    def complete_provider_usage_with_event(
        self,
        run_id: str,
        call_index: int,
        *,
        input_tokens: int,
        output_tokens: int,
        boundary_event: EventEnvelope,
    ) -> ProviderUsageMutation:
        """Atomically complete provider usage and its canonical response boundary."""

        run_id = _validate_id(run_id, "run_id")
        if (
            not isinstance(call_index, int)
            or isinstance(call_index, bool)
            or not 1 <= call_index <= MAX_PROVIDER_CALLS_PER_RUN
        ):
            raise ValueError("invalid provider call_index")
        input_tokens = _usage_count(input_tokens, "input_tokens")
        output_tokens = _usage_count(output_tokens, "output_tokens")
        payload = (
            boundary_event.payload
            if isinstance(boundary_event, EventEnvelope)
            else None
        )
        if (
            not isinstance(payload, dict)
            or payload.get("iteration") != call_index
            or payload.get("input_tokens") != input_tokens
            or payload.get("output_tokens") != output_tokens
            or payload.get("usage_complete") is not True
            or payload.get("error_code") is not None
            or payload.get("outcome") not in {"tool_use", "end_turn"}
        ):
            raise ValueError("provider response boundary disagrees with its usage")
        outcome = (input_tokens, output_tokens, None, None, None)
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                row, changed = self._complete_provider_usage_locked(
                    run_id,
                    call_index,
                    outcome,
                    timestamp=boundary_event.occurred_at,
                )
                self._append_running_boundary_locked(
                    boundary_event,
                    expected_kind="model.response.finished",
                )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                raise ConversationConflictError(
                    "provider response boundary already exists"
                ) from exc
            except sqlite3.Error as exc:
                self._rollback()
                raise ConversationStoreUnavailableError(
                    "could not commit provider response boundary"
                ) from exc
        return ProviderUsageMutation(_provider_usage_from_row(row), changed)

    def provider_usage_for_run(self, run_id: str) -> tuple[ProviderUsage, ...]:
        run_id = _validate_id(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            try:
                if not self._owned_run_exists(run_id):
                    raise TurnNotFoundError("Run has no conversation turn")
                rows = self._connection.execute(
                    """
                    SELECT run_id, call_index, provider, model, profile_digest,
                           context_plan_id, estimated_input_tokens,
                           hard_input_tokens, status, input_tokens, output_tokens,
                           cost_minor_units, currency, pricing_profile_digest,
                           started_at, completed_at
                    FROM provider_usage
                    WHERE run_id = ? ORDER BY call_index
                    LIMIT ?
                    """,
                    (run_id, MAX_PROVIDER_CALLS_PER_RUN + 1),
                ).fetchall()
            except ConversationStoreError:
                raise
            except sqlite3.Error as exc:
                raise ConversationStoreUnavailableError(
                    "could not read provider usage"
                ) from exc
        if len(rows) > MAX_PROVIDER_CALLS_PER_RUN:
            raise ConversationConflictError("provider usage exceeds its capacity")
        return tuple(_provider_usage_from_row(row) for row in rows)

    def _mark_started_usage_incomplete_locked(self, timestamp: str) -> int:
        cursor = self._connection.execute(
            """
            UPDATE provider_usage
            SET status = 'incomplete', completed_at = ?
            WHERE status = 'started' AND run_id IN (
                SELECT t.run_id
                FROM conversation_turns AS t
                JOIN conversations AS c ON c.conversation_id = t.conversation_id
                WHERE c.agent_id = ?
            )
            """,
            (timestamp, self.agent_id),
        )
        return max(cursor.rowcount, 0)

    def _usage_aggregate_locked(
        self, run_id: str, *, force_incomplete: bool = False
    ) -> tuple[int, int, int, bool]:
        row = self._connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN status = 'complete' THEN input_tokens ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'complete' THEN output_tokens ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status != 'complete' THEN 1 ELSE 0 END), 0)
            FROM provider_usage WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        total, input_tokens, output_tokens, incomplete = row
        last = self._connection.execute(
            """
            SELECT input_tokens FROM provider_usage
            WHERE run_id = ? AND status = 'complete'
            ORDER BY call_index DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        last_input_tokens = 0 if last is None else last[0]
        complete = total > 0 and incomplete == 0 and not force_incomplete
        return input_tokens, output_tokens, last_input_tokens, complete

    def _project_run_locked(
        self, run_id: str, *, allow_reserved_recovery_gap: bool
    ) -> tuple[ProjectionSnapshot, int, int, int, int, bool]:
        identity = self._resolve_run_identity_locked(run_id)
        state_row = self._run_journal_row(run_id)
        if identity is None or state_row is None:
            raise ConversationConflictError("Run projection has no durable identity")
        state = _run_journal_state_from_row(state_row)
        if state.reserved_through != RUN_CURSOR_RESERVED_THROUGH:
            raise ConversationConflictError(
                "Run projection has an invalid cursor reservation"
            )
        rows = self._connection.execute(
            """
            SELECT run_id, seq, kind, occurred_at,
                   CASE
                     WHEN typeof(envelope_json) = 'text'
                      AND length(CAST(envelope_json AS BLOB)) BETWEEN 2 AND ?
                     THEN CAST(envelope_json AS BLOB)
                   END
            FROM events WHERE run_id = ? ORDER BY seq LIMIT ?
            """,
            (
                MAX_DURABLE_EVENT_BYTES,
                run_id,
                MAX_RECOVERY_EVENTS_PER_RUN + 1,
            ),
        ).fetchall()
        if not rows or len(rows) > MAX_RECOVERY_EVENTS_PER_RUN:
            raise ConversationConflictError("Run projection event count is invalid")
        events: list[EventEnvelope] = []
        durable_bytes = 0
        try:
            for column_run_id, seq, kind, occurred_at, raw in rows:
                if not isinstance(raw, bytes):
                    raise ReplayCorruptionError("durable event has an invalid size")
                durable_bytes += len(raw)
                if durable_bytes > MAX_RECOVERY_DURABLE_BYTES_PER_RUN:
                    raise ReplayCorruptionError("durable Run exceeds its byte limit")
                event = decode_durable_event(
                    raw,
                    column_run_id=column_run_id,
                    column_seq=seq,
                    column_kind=kind,
                    column_occurred_at=occurred_at,
                )
                if RunIdentity.from_event(event) != identity:
                    raise ReplayCorruptionError("durable Run changes identity")
                events.append(event)
            snapshot, gaps = project_durable_run(
                events,
                reserved_through=(
                    state.reserved_through if allow_reserved_recovery_gap else None
                ),
            )
        except ReplayCorruptionError as exc:
            raise ConversationConflictError("Run projection is invalid") from exc
        if not snapshot.complete:
            raise ConversationConflictError("terminal Run projection is incomplete")
        return (
            snapshot,
            events[0].seq,
            events[-1].seq,
            len(events),
            durable_bytes,
            bool(gaps),
        )

    def _validate_terminal_usage_locked(
        self,
        run_id: str,
        terminal_event: EventEnvelope,
        usage: tuple[int, int, int, bool],
    ) -> None:
        count = self._connection.execute(
            "SELECT COUNT(*) FROM provider_usage WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        if count == 0:
            return
        value = terminal_event.payload.get("usage")
        expected = {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "last_input_tokens": usage[2],
            "complete": usage[3],
        }
        if (
            not isinstance(value, dict)
            or set(value) != set(expected)
            or any(
                not isinstance(value.get(field), int)
                or isinstance(value.get(field), bool)
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "last_input_tokens",
                )
            )
            or not isinstance(value.get("complete"), bool)
            or value != expected
        ):
            raise ConversationConflictError(
                "terminal usage does not match the provider ledger"
            )

    def _refresh_terminal_projection_locked(
        self,
        run_id: str,
        terminal_event: EventEnvelope,
        *,
        force_usage_incomplete: bool,
        ephemeral_loss: bool,
    ) -> None:
        self._connection.execute(
            """
            UPDATE provider_usage
            SET status = 'incomplete', completed_at = ?
            WHERE run_id = ? AND status = 'started'
            """,
            (terminal_event.occurred_at, run_id),
        )
        usage = self._usage_aggregate_locked(
            run_id, force_incomplete=force_usage_incomplete
        )
        self._validate_terminal_usage_locked(run_id, terminal_event, usage)
        (
            snapshot,
            oldest,
            latest,
            event_count,
            durable_bytes,
            projected_ephemeral_loss,
        ) = self._project_run_locked(
            run_id, allow_reserved_recovery_gap=ephemeral_loss
        )
        if (
            snapshot.through_seq != terminal_event.seq
            or snapshot.identity.run_id != run_id
        ):
            raise ConversationConflictError("Run terminal projection is inconsistent")
        cursor = self._connection.execute(
            """
            UPDATE run_journal_state
            SET oldest_available_seq = ?, latest_durable_seq = ?,
                terminal_seq = ?, terminal_kind = ?, availability = 'full',
                event_count = ?, durable_bytes = ?, input_tokens = ?,
                output_tokens = ?, last_input_tokens = ?, usage_complete = ?
            WHERE run_id = ? AND terminal_seq IS NULL
            """,
            (
                oldest,
                latest,
                terminal_event.seq,
                terminal_event.kind,
                event_count,
                durable_bytes,
                *usage[:3],
                int(usage[3]),
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ConversationConflictError("Run terminal projection already exists")
        encoded = encode_projection_snapshot(snapshot)
        encoded_bytes = encoded.encode("utf-8")
        if len(encoded_bytes) > MAX_SNAPSHOT_BYTES:
            raise ConversationConflictError("Run snapshot exceeds its limit")
        source_digest = hashlib.sha256(encoded_bytes).hexdigest()
        self._connection.execute(
            """
            INSERT INTO run_snapshots(
                run_id, projection_version, through_seq, snapshot_json,
                source_digest, ephemeral_loss, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                projection_version = excluded.projection_version,
                through_seq = excluded.through_seq,
                snapshot_json = excluded.snapshot_json,
                source_digest = excluded.source_digest,
                ephemeral_loss = excluded.ephemeral_loss,
                created_at = excluded.created_at
            """,
            (
                run_id,
                PROJECTION_VERSION,
                snapshot.through_seq,
                encoded,
                source_digest,
                int(ephemeral_loss or projected_ephemeral_loss),
                terminal_event.occurred_at,
            ),
        )

    def _tombstone_unavailable_run_locked(
        self,
        run_id: str,
        *,
        timestamp: str,
    ) -> None:
        """Atomically discard a partial journal that cannot reach durability.

        A memory-only terminal event is deliberately not promoted to canonical
        history.  Keeping the durable prefix would leave a terminal Turn whose
        Run still looked active to retention and recovery.  Instead we retain
        only the bounded Run identity/usage tombstone and make replay fail
        explicitly through the existing ``pruned`` availability state.
        """

        state_row = self._run_journal_row(run_id)
        if state_row is None:
            raise ConversationConflictError("Run has no journal state")
        state = _run_journal_state_from_row(state_row)
        if (
            state.availability != "full"
            or state.terminal_seq is not None
            or state.terminal_kind is not None
        ):
            raise ConversationConflictError(
                "Run journal cannot become an unavailable tombstone"
            )
        self._connection.execute(
            """
            UPDATE operation_ledger
            SET status = 'outcome_unknown', outcome_digest = NULL,
                resolved_at = ?
            WHERE agent_id = ? AND run_id = ? AND status = 'dispatched'
            """,
            (timestamp, self.agent_id, run_id),
        )
        self._connection.execute(
            """
            UPDATE provider_usage
            SET status = 'incomplete', completed_at = ?
            WHERE run_id = ? AND status = 'started'
            """,
            (timestamp, run_id),
        )
        usage = self._usage_aggregate_locked(run_id, force_incomplete=True)
        self._connection.execute(
            "DELETE FROM run_snapshots WHERE run_id = ?",
            (run_id,),
        )
        self._connection.execute(
            "DELETE FROM events WHERE run_id = ?",
            (run_id,),
        )
        cursor = self._connection.execute(
            """
            UPDATE run_journal_state
            SET oldest_available_seq = latest_durable_seq,
                terminal_seq = NULL, terminal_kind = NULL,
                availability = 'pruned', event_count = 0, durable_bytes = 0,
                input_tokens = ?, output_tokens = ?, last_input_tokens = ?,
                usage_complete = 0
            WHERE run_id = ? AND availability = 'full'
              AND terminal_seq IS NULL AND terminal_kind IS NULL
            """,
            (*usage[:3], run_id),
        )
        if cursor.rowcount != 1:
            raise ConversationConflictError(
                "Run unavailable tombstone transition was lost"
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
                self._connection.execute(
                    """
                    INSERT INTO run_journal_state(
                        run_id, oldest_available_seq, latest_durable_seq,
                        reserved_through, terminal_seq, terminal_kind,
                        availability, event_count, durable_bytes,
                        input_tokens, output_tokens, last_input_tokens,
                        usage_complete
                    ) VALUES (?, 1, 1, ?, NULL, NULL, 'full', 1, ?, 0, 0, 0, 0)
                    """,
                    (
                        run_id,
                        RUN_CURSOR_RESERVED_THROUGH,
                        len(encoded_event.encode("utf-8")),
                    ),
                )
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
                    self._refresh_terminal_projection_locked(
                        run_id,
                        terminal_event,
                        force_usage_incomplete=False,
                        ephemeral_loss=False,
                    )
                else:
                    self._tombstone_unavailable_run_locked(
                        run_id,
                        timestamp=timestamp,
                    )
                self._connection.commit()
            except ConversationStoreError:
                self._rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self._rollback()
                if terminal_event is None:
                    raise ConversationStoreUnavailableError(
                        "could not finalize unavailable Run tombstone"
                    ) from exc
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
        finished_call_ids: list[str] = []
        model_request_count = 0
        open_model_request: tuple[str, int] | None = None
        last_model_outcome: str | None = None

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
            if kind == "model.request.started":
                model_payload = _exact_recovery_payload(
                    payload,
                    {
                        "request_id",
                        "iteration",
                        "context_plan_id",
                        "context_plan_digest",
                        "request_digest",
                        "request_bytes",
                        "estimated_input_tokens",
                        "message_count",
                        "tool_count",
                        "tool_result_call_ids",
                    },
                )
                iteration = model_payload.get("iteration")
                request_id = _recovery_worker_id(
                    model_payload.get("request_id"), "model request_id"
                )
                result_ids = model_payload.get("tool_result_call_ids")
                if (
                    not isinstance(iteration, int)
                    or isinstance(iteration, bool)
                    or not 1 <= iteration <= 3
                    or request_id != f"model-{iteration}"
                    or iteration != model_request_count + 1
                    or open_model_request is not None
                    or open_block_id is not None
                    or pending_call_id is not None
                    or (model_request_count and last_model_outcome != "tool_use")
                    or not isinstance(model_payload.get("context_plan_id"), str)
                    or len(str(model_payload["context_plan_id"]).encode("utf-8"))
                    > 128
                    or not isinstance(model_payload.get("context_plan_digest"), str)
                    or _LEDGER_DIGEST.fullmatch(
                        str(model_payload["context_plan_digest"])
                    )
                    is None
                    or not isinstance(model_payload.get("request_digest"), str)
                    or _LEDGER_DIGEST.fullmatch(str(model_payload["request_digest"]))
                    is None
                    or any(
                        not isinstance(model_payload.get(field), int)
                        or isinstance(model_payload.get(field), bool)
                        or not 1 <= int(model_payload[field]) <= MAX_USAGE_TOKENS
                        for field in (
                            "request_bytes",
                            "estimated_input_tokens",
                            "message_count",
                        )
                    )
                    or not isinstance(model_payload.get("tool_count"), int)
                    or isinstance(model_payload.get("tool_count"), bool)
                    or not 0 <= int(model_payload["tool_count"]) <= 16
                    or not isinstance(result_ids, list)
                    or len(result_ids) > 3
                ):
                    raise ConversationConflictError(
                        "running turn has an invalid model request"
                    )
                validated_result_ids = [
                    _recovery_worker_id(item, "model Tool result call_id")
                    for item in result_ids
                ]
                if (
                    validated_result_ids != finished_call_ids
                    or (
                        iteration == 1
                        and model_payload["tool_count"] != len(_RECOVERY_TOOL_SPECS)
                    )
                    or (iteration > 1 and model_payload["tool_count"] != 0)
                ):
                    raise ConversationConflictError(
                        "running turn has inconsistent model capabilities"
                    )
                model_request_count = iteration
                open_model_request = (request_id, iteration)
                last_model_outcome = None
            elif kind == "model.response.finished":
                model_payload = _exact_recovery_payload(
                    payload,
                    {
                        "request_id",
                        "iteration",
                        "outcome",
                        "input_tokens",
                        "output_tokens",
                        "usage_complete",
                        "error_code",
                    },
                )
                request_id = _recovery_worker_id(
                    model_payload.get("request_id"), "model request_id"
                )
                iteration = model_payload.get("iteration")
                outcome = model_payload.get("outcome")
                input_tokens = model_payload.get("input_tokens")
                output_tokens = model_payload.get("output_tokens")
                usage_complete = model_payload.get("usage_complete")
                error_code = model_payload.get("error_code")
                successful = outcome in {"tool_use", "end_turn"}
                if (
                    open_model_request != (request_id, iteration)
                    or outcome not in {"tool_use", "end_turn", "error", "cancelled"}
                    or not isinstance(input_tokens, int)
                    or isinstance(input_tokens, bool)
                    or not 0 <= input_tokens <= MAX_USAGE_TOKENS
                    or not isinstance(output_tokens, int)
                    or isinstance(output_tokens, bool)
                    or not 0 <= output_tokens <= MAX_USAGE_TOKENS
                    or not isinstance(usage_complete, bool)
                    or (
                        successful
                        and (usage_complete is not True or error_code is not None)
                    )
                    or (
                        not successful
                        and (
                            usage_complete is not False
                            or input_tokens != 0
                            or output_tokens != 0
                            or not isinstance(error_code, str)
                            or _RECOVERY_WORKER_ID.fullmatch(error_code) is None
                        )
                    )
                ):
                    raise ConversationConflictError(
                        "running turn has an invalid model response"
                    )
                open_model_request = None
                last_model_outcome = str(outcome)
            elif kind == "assistant.block.started":
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
                    or open_model_request is not None
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
                if (
                    call_id in seen_calls
                    or pending_call_id is not None
                    or open_model_request is not None
                    or (model_request_count and last_model_outcome != "tool_use")
                ):
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
                finished_call_ids.append(call_id)
            elif kind != "run.started":
                raise ConversationConflictError(
                    "running turn has an unsupported durable event"
                )
            last_seq = seq

        closure_events: list[tuple[str, dict[str, object]]] = []
        if open_model_request is not None:
            request_id, iteration = open_model_request
            closure_events.append(
                (
                    "model.response.finished",
                    {
                        "request_id": request_id,
                        "iteration": iteration,
                        "outcome": "error",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "usage_complete": False,
                        "error_code": "control_restarted",
                    },
                )
            )
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

    def _validated_running_journal_state_locked(
        self,
        existing: ConversationTurn,
        recovery: _RecoveryScan,
    ) -> RunJournalState:
        state_row = self._run_journal_row(existing.run_id)
        if state_row is None:
            raise ConversationConflictError(
                "running turn has no Run journal state"
            )
        state = _run_journal_state_from_row(state_row)
        snapshot_exists = self._connection.execute(
            "SELECT 1 FROM run_snapshots WHERE run_id = ?",
            (existing.run_id,),
        ).fetchone()
        if (
            recovery.event_count < 1
            or state.oldest_available_seq != 1
            or state.latest_durable_seq != recovery.last_seq
            or state.reserved_through != RUN_CURSOR_RESERVED_THROUGH
            or state.terminal_seq is not None
            or state.terminal_kind is not None
            or state.availability != "full"
            or state.event_count != recovery.event_count
            or state.durable_bytes != recovery.durable_bytes
            or state.input_tokens != 0
            or state.output_tokens != 0
            or state.last_input_tokens != 0
            or state.usage_complete
            or snapshot_exists is not None
        ):
            raise ConversationConflictError(
                "running Run journal metadata is inconsistent"
            )
        return state

    def recover_running_as_interrupted(self) -> tuple[ConversationTurn, ...]:
        """Fail closed after restart; interrupted partial output is not history."""

        timestamp = utc_now()
        with self._lock:
            self._ensure_open()
            try:
                self._begin_write()
                self._recover_dispatched_operations_locked(timestamp)
                self._mark_started_usage_incomplete_locked(timestamp)
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
                    state = self._validated_running_journal_state_locked(
                        existing,
                        recovery,
                    )
                    if recovery.event_count:
                        recovery_base = max(
                            recovery.last_seq, state.reserved_through
                        )
                        # Every still-started call was changed to incomplete at
                        # the beginning of this transaction.  Fully paired
                        # response boundaries therefore retain exact provider
                        # usage, while an open call keeps the terminal rollup
                        # incomplete without discarding earlier exact calls.
                        usage = self._usage_aggregate_locked(
                            existing.run_id, force_incomplete=False
                        )
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
                                        "input_tokens": usage[0],
                                        "output_tokens": usage[1],
                                        "last_input_tokens": usage[2],
                                        "complete": usage[3],
                                    },
                                },
                            ),
                        ]
                        if (
                            recovery.event_count + len(synthetic)
                            > MAX_RECOVERY_EVENTS_PER_RUN
                            or recovery_base + len(synthetic)
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
                                seq=recovery_base + offset,
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
                                exact_seq=recovery_base + offset,
                            )
                            total_bytes += len(encoded.encode("utf-8"))
                            if total_bytes > MAX_RECOVERY_DURABLE_BYTES_PER_RUN:
                                raise ConversationConflictError(
                                    "running turn has no recovery byte capacity"
                                )
                            prepared.append((recovery_event, encoded))
                        for recovery_event, encoded in prepared:
                            self._insert_boundary_event(recovery_event, encoded)
                        terminal_event = prepared[-1][0]
                        self._refresh_terminal_projection_locked(
                            existing.run_id,
                            terminal_event,
                            force_usage_incomplete=False,
                            ephemeral_loss=True,
                        )
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

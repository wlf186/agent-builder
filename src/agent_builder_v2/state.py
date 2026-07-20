"""Agent-scoped semantic event journal for the walking skeleton."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import stat
from pathlib import Path
from threading import Lock

from .contracts import (
    RUN_CURSOR_RESERVED_THROUGH,
    TERMINAL_KINDS,
    EventEnvelope,
)
from .replay import (
    DurableReplay,
    LEGACY_PROJECTION_VERSION,
    MAX_DURABLE_EVENT_BYTES,
    MAX_REPLAY_BYTES,
    MAX_REPLAY_EVENTS,
    MAX_REPLAY_PAGE,
    MAX_REPLAY_SEQUENCE,
    PROJECTION_VERSION,
    ProjectionSnapshot,
    ReplayCorruptionError,
    ReplayGap,
    RunIdentity,
    decode_durable_event,
    decode_projection_snapshot,
    encode_projection_snapshot,
    project_durable_run,
)


class JournalUnavailableError(RuntimeError):
    """Durable state cannot be opened or updated safely."""


class JournalCorruptionError(JournalUnavailableError):
    """Stored canonical events failed bounded integrity validation."""


_RUN_ID = re.compile(r"^[a-f0-9]{32}$")


def _snapshot_has_ephemeral_loss(snapshot: ProjectionSnapshot) -> bool:
    """Recover the retained projection's original sequence-loss bit.

    A complete projection contains two durable Run boundaries, two events per
    assistant block, and three events per Tool call.  Canonical sequence IDs
    start at one and only skip for non-durable deltas or the reserved recovery
    band, so a larger terminal cursor proves that at least one cursor was lost.
    """

    document = snapshot.document
    blocks = document.get("blocks")
    tools = document.get("tools")
    model_calls = document.get("model_calls", [])
    if (
        not isinstance(blocks, list)
        or not isinstance(tools, list)
        or not isinstance(model_calls, list)
    ):
        raise ReplayCorruptionError("projection collections are invalid")
    durable_event_count = (
        2
        + (2 * len(blocks))
        + (3 * len(tools))
        + (2 * len(model_calls))
    )
    if snapshot.through_seq < durable_event_count:
        raise ReplayCorruptionError("projection sequence cannot contain its events")
    return snapshot.through_seq > durable_event_count


def _open_private_regular_at(
    name: str, directory_descriptor: int
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise JournalUnavailableError("secure journal files require O_NOFOLLOW")
    try:
        descriptor = os.open(
            name, flags | no_follow, dir_fd=directory_descriptor
        )
    except FileNotFoundError:
        try:
            descriptor = os.open(
                name,
                flags | no_follow | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise JournalUnavailableError("could not create the journal safely") from exc
    except OSError as exc:
        raise JournalUnavailableError("could not open the journal safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise JournalUnavailableError("journal is not a private regular file")
        os.fchmod(descriptor, 0o600)
        identity = (metadata.st_dev, metadata.st_ino)
        path_metadata = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (path_metadata.st_dev, path_metadata.st_ino) != identity:
            raise JournalUnavailableError("journal path changed while opening")
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _validate_sqlite_file(path: Path, identity: tuple[int, int] | None = None) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or (identity is not None and (metadata.st_dev, metadata.st_ino) != identity)
    ):
        raise JournalUnavailableError(f"unsafe SQLite state file: {path.name}")
    os.chmod(path, 0o600, follow_symlinks=False)


class EventJournal:
    """Persist only durable semantic events, never token deltas."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        parent_metadata = os.lstat(database_path.parent)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
        ):
            raise JournalUnavailableError("journal directory is unsafe")
        parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
        for suffix in ("-wal", "-shm"):
            _validate_sqlite_file(Path(f"{database_path}{suffix}"))
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
            raise JournalUnavailableError(
                "could not anchor the journal directory safely"
            ) from exc
        anchored_parent = os.fstat(self._directory_descriptor)
        if (
            not stat.S_ISDIR(anchored_parent.st_mode)
            or anchored_parent.st_uid != os.getuid()
            or (anchored_parent.st_dev, anchored_parent.st_ino) != parent_identity
        ):
            os.close(self._directory_descriptor)
            raise JournalUnavailableError("journal directory changed while opening")
        try:
            descriptor, identity = _open_private_regular_at(
                database_path.name, self._directory_descriptor
            )
        except BaseException:
            os.close(self._directory_descriptor)
            raise
        os.close(descriptor)
        anchored_database_path = (
            f"/proc/self/fd/{self._directory_descriptor}/{database_path.name}"
        )
        try:
            self._connection = sqlite3.connect(
                anchored_database_path,
                check_same_thread=False,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            os.close(self._directory_descriptor)
            raise JournalUnavailableError("could not connect to the journal") from exc
        self._lock = Lock()
        try:
            with self._connection:
                self._connection.execute("PRAGMA busy_timeout=5000")
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=NORMAL")
                self._connection.execute("PRAGMA journal_size_limit=16777216")
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        run_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        envelope_json TEXT NOT NULL,
                        PRIMARY KEY (run_id, seq)
                    )
                    """
                )
            anchored_after = os.fstat(self._directory_descriptor)
            database_after = os.stat(
                database_path.name,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
            if (
                (anchored_after.st_dev, anchored_after.st_ino) != parent_identity
                or (database_after.st_dev, database_after.st_ino) != identity
            ):
                raise JournalUnavailableError(
                    "journal path changed while connecting"
                )
            named_parent_after = os.lstat(database_path.parent)
            if (
                not stat.S_ISDIR(named_parent_after.st_mode)
                or (named_parent_after.st_dev, named_parent_after.st_ino)
                != parent_identity
            ):
                raise JournalUnavailableError(
                    "journal directory path changed while connecting"
                )
            _validate_sqlite_file(database_path, identity)
            for suffix in ("-wal", "-shm"):
                _validate_sqlite_file(Path(f"{database_path}{suffix}"))
        except (OSError, sqlite3.Error) as exc:
            self._connection.close()
            os.close(self._directory_descriptor)
            raise JournalUnavailableError("could not initialize the journal") from exc
        except Exception:
            self._connection.close()
            os.close(self._directory_descriptor)
            raise

    def append(self, event: EventEnvelope) -> None:
        if event.durability != "durable":
            return
        encoded = json.dumps(
            event.to_dict(), ensure_ascii=False, separators=(",", ":")
        )
        encoded_bytes = encoded.encode("utf-8")
        if len(encoded_bytes) > 65_536:
            raise ValueError("durable event exceeds prototype journal limit")
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                managed = self._managed_state_locked(event.run_id)
                if managed is not None:
                    (
                        _agent_id,
                        _conversation_id,
                        _turn_id,
                        _oldest,
                        latest,
                        reserved_through,
                        terminal_seq,
                        _terminal_kind,
                        availability,
                        event_count,
                        durable_bytes,
                    ) = managed
                    if (
                        availability != "full"
                        or reserved_through != RUN_CURSOR_RESERVED_THROUGH
                        or terminal_seq is not None
                        or event.kind in TERMINAL_KINDS
                        or event.agent_id != _agent_id
                        or event.conversation_id != _conversation_id
                        or event.turn_id != _turn_id
                        or not isinstance(latest, int)
                        or isinstance(latest, bool)
                        or event.seq <= latest
                        or not isinstance(event_count, int)
                        or event_count >= MAX_REPLAY_EVENTS
                        or not isinstance(durable_bytes, int)
                        or durable_bytes + len(encoded_bytes) > MAX_REPLAY_BYTES
                    ):
                        raise JournalCorruptionError(
                            "managed Run journal state rejects append"
                        )
                self._connection.execute(
                    """
                    INSERT INTO events(run_id, seq, kind, occurred_at, envelope_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (event.run_id, event.seq, event.kind, event.occurred_at, encoded),
                )
                if managed is not None:
                    cursor = self._connection.execute(
                        """
                        UPDATE run_journal_state
                        SET latest_durable_seq = ?, event_count = event_count + 1,
                            durable_bytes = durable_bytes + ?
                        WHERE run_id = ? AND latest_durable_seq = ?
                          AND terminal_seq IS NULL AND availability = 'full'
                        """,
                        (event.seq, len(encoded_bytes), event.run_id, latest),
                    )
                    if cursor.rowcount != 1:
                        raise JournalCorruptionError(
                            "managed Run journal update was lost"
                        )
                self._connection.commit()
        except JournalCorruptionError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise JournalUnavailableError("could not append to the journal") from exc

    def _managed_schema_locked(self) -> bool:
        rows = self._connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN (
                  'conversations', 'conversation_turns',
                  'run_journal_state', 'run_snapshots'
              )
            """
        ).fetchall()
        return len(rows) == 4

    def _managed_state_locked(self, run_id: str) -> tuple[object, ...] | None:
        if not self._managed_schema_locked():
            return None
        return self._connection.execute(
            """
            SELECT c.agent_id, t.conversation_id, t.turn_id,
                   s.oldest_available_seq, s.latest_durable_seq,
                   s.reserved_through, s.terminal_seq, s.terminal_kind,
                   s.availability, s.event_count, s.durable_bytes
            FROM run_journal_state AS s
            JOIN conversation_turns AS t ON t.run_id = s.run_id
            JOIN conversations AS c ON c.conversation_id = t.conversation_id
            WHERE s.run_id = ?
            """,
            (run_id,),
        ).fetchone()

    def _validated_run_locked(
        self, run_id: str, *, reserved_through: int
    ) -> tuple[
        tuple[EventEnvelope, ...],
        ProjectionSnapshot,
        tuple[ReplayGap, ...],
        int,
    ]:
        if reserved_through != RUN_CURSOR_RESERVED_THROUGH:
            raise ReplayCorruptionError(
                "managed Run cursor reservation is invalid"
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
            (MAX_DURABLE_EVENT_BYTES, run_id, MAX_REPLAY_EVENTS + 1),
        ).fetchall()
        if not rows or len(rows) > MAX_REPLAY_EVENTS:
            raise ReplayCorruptionError("managed Run event count is invalid")
        decoded: list[EventEnvelope] = []
        durable_bytes = 0
        for column_run_id, seq, kind, occurred_at, raw in rows:
            if not isinstance(raw, bytes):
                raise ReplayCorruptionError("managed Run event row is invalid")
            durable_bytes += len(raw)
            if durable_bytes > MAX_REPLAY_BYTES:
                raise ReplayCorruptionError("managed Run bytes exceed their limit")
            decoded.append(
                decode_durable_event(
                    raw,
                    column_run_id=column_run_id,
                    column_seq=seq,
                    column_kind=kind,
                    column_occurred_at=occurred_at,
                )
            )
        snapshot, gaps = project_durable_run(
            decoded, reserved_through=reserved_through
        )
        return tuple(decoded), snapshot, gaps, durable_bytes

    def events_for_run(self, run_id: str) -> list[dict[str, object]]:
        try:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT envelope_json FROM events WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise JournalUnavailableError("could not read from the journal") from exc
        return [json.loads(row[0]) for row in rows]

    def replay(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = MAX_REPLAY_PAGE,
        expected_identity: RunIdentity | None = None,
    ) -> DurableReplay | None:
        """Read, validate and project one retained durable Run atomically.

        The complete bounded stream is validated inside one SQLite read
        transaction before this method returns any page.  A caller can
        therefore safely serialize the result without leaking a valid prefix
        of a corrupt journal.  Missing rows return ``None``; distinguishing a
        deleted Run from a retention tombstone belongs to the higher-level
        Conversation store until durable Run metadata is integrated there.
        """

        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid run_id")
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or not 0 <= after <= MAX_REPLAY_SEQUENCE
        ):
            raise ValueError("invalid replay cursor")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_REPLAY_PAGE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_REPLAY_PAGE}")
        if expected_identity is not None and expected_identity.run_id != run_id:
            raise ValueError("expected identity does not match run_id")

        managed: tuple[object, ...] | None = None
        stored_snapshot: tuple[object, ...] | None = None
        try:
            with self._lock:
                self._connection.execute("BEGIN")
                try:
                    managed = self._managed_state_locked(run_id)
                    rows = self._connection.execute(
                        """
                        SELECT run_id, seq, kind, occurred_at,
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
                            run_id,
                            MAX_REPLAY_EVENTS + 1,
                        ),
                    ).fetchall()
                    if managed is not None and managed[8] == "snapshot_only":
                        stored_snapshot = self._connection.execute(
                            """
                            SELECT projection_version, through_seq,
                                   CASE
                                     WHEN typeof(snapshot_json) = 'text'
                                      AND length(CAST(snapshot_json AS BLOB))
                                          BETWEEN 2 AND 65536
                                     THEN CAST(snapshot_json AS BLOB)
                                   END,
                                   source_digest, ephemeral_loss
                            FROM run_snapshots WHERE run_id = ?
                            """,
                            (run_id,),
                        ).fetchone()
                    self._connection.commit()
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.rollback()
                    raise
        except sqlite3.Error as exc:
            raise JournalUnavailableError(
                "could not read durable Run replay"
            ) from exc

        managed_identity: RunIdentity | None = None
        reserved_through: int | None = None
        if managed is not None:
            (
                agent_id,
                conversation_id,
                turn_id,
                oldest_available,
                managed_latest,
                reserved_through,
                terminal_seq,
                terminal_kind,
                managed_availability,
                managed_count,
                managed_bytes,
            ) = managed
            if not all(
                isinstance(value, str)
                for value in (agent_id, conversation_id, turn_id)
            ):
                raise JournalCorruptionError("managed Run identity is corrupt")
            managed_identity = RunIdentity(
                agent_id, conversation_id, turn_id, run_id  # type: ignore[arg-type]
            )
            if expected_identity is not None and managed_identity != expected_identity:
                raise KeyError("durable Run does not match expected identity")
            if reserved_through != RUN_CURSOR_RESERVED_THROUGH:
                raise JournalCorruptionError(
                    "managed Run cursor reservation is corrupt"
                )
            if managed_availability == "snapshot_only":
                if rows or stored_snapshot is None:
                    raise JournalCorruptionError(
                        "snapshot-only Run retention state is corrupt"
                    )
                (
                    projection_version,
                    through_seq,
                    raw_snapshot,
                    source_digest,
                    ephemeral_loss,
                ) = stored_snapshot
                if (
                    projection_version
                    not in {LEGACY_PROJECTION_VERSION, PROJECTION_VERSION}
                    or not isinstance(oldest_available, int)
                    or isinstance(oldest_available, bool)
                    or not isinstance(managed_latest, int)
                    or isinstance(managed_latest, bool)
                    or not isinstance(terminal_seq, int)
                    or isinstance(terminal_seq, bool)
                    or not isinstance(through_seq, int)
                    or isinstance(through_seq, bool)
                    or oldest_available != managed_latest
                    or through_seq != managed_latest
                    or terminal_seq != managed_latest
                    or terminal_kind not in TERMINAL_KINDS
                    or not isinstance(raw_snapshot, bytes)
                    or not isinstance(source_digest, str)
                    or hashlib.sha256(raw_snapshot).hexdigest() != source_digest
                    or not isinstance(ephemeral_loss, int)
                    or isinstance(ephemeral_loss, bool)
                    or ephemeral_loss not in {0, 1}
                    or managed_count != 0
                    or managed_bytes != 0
                ):
                    raise JournalCorruptionError(
                        "snapshot-only Run metadata is corrupt"
                    )
                try:
                    snapshot = decode_projection_snapshot(
                        raw_snapshot,
                        expected_identity=managed_identity,
                        expected_through_seq=managed_latest,  # type: ignore[arg-type]
                    )
                except ReplayCorruptionError as exc:
                    raise JournalCorruptionError(
                        "snapshot-only Run projection is corrupt"
                    ) from exc
                if not snapshot.complete:
                    raise JournalCorruptionError(
                        "snapshot-only Run has no terminal projection"
                    )
                try:
                    snapshot_terminal = snapshot.document.get("terminal")
                    snapshot_lost_ephemeral = _snapshot_has_ephemeral_loss(
                        snapshot
                    )
                except ReplayCorruptionError as exc:
                    raise JournalCorruptionError(
                        "snapshot-only Run projection is corrupt"
                    ) from exc
                if (
                    snapshot.version != projection_version
                    or
                    not isinstance(snapshot_terminal, dict)
                    or snapshot_terminal.get("kind") != terminal_kind
                    or bool(ephemeral_loss) != snapshot_lost_ephemeral
                ):
                    raise JournalCorruptionError(
                        "snapshot-only Run metadata is corrupt"
                    )
                if after > managed_latest:
                    raise ValueError("replay cursor is newer than the durable Run")
                gaps = (
                    (ReplayGap(after + 1, managed_latest, "retention"),)
                    if after < managed_latest
                    else ()
                )
                return DurableReplay(
                    identity=managed_identity,
                    availability="snapshot_only",
                    oldest_cursor=oldest_available,
                    latest_cursor=managed_latest,
                    next_cursor=managed_latest,
                    has_more=False,
                    events=(),
                    gaps=gaps,
                    snapshot=snapshot,
                )
            if managed_availability != "full":
                raise JournalCorruptionError("managed Run events are unavailable")

        if not rows:
            if managed is not None:
                raise JournalCorruptionError("managed Run has no retained events")
            return None
        if len(rows) > MAX_REPLAY_EVENTS:
            raise JournalCorruptionError(
                "durable Run event count exceeds its replay limit"
            )
        durable_bytes = 0
        decoded: list[EventEnvelope] = []
        try:
            for column_run_id, seq, kind, occurred_at, raw in rows:
                if not isinstance(raw, bytes):
                    raise ReplayCorruptionError(
                        "durable event storage metadata is invalid"
                    )
                durable_bytes += len(raw)
                if durable_bytes > MAX_REPLAY_BYTES:
                    raise ReplayCorruptionError(
                        "durable Run bytes exceed their replay limit"
                    )
                decoded.append(
                    decode_durable_event(
                        raw,
                        column_run_id=column_run_id,
                        column_seq=seq,
                        column_kind=kind,
                        column_occurred_at=occurred_at,
                    )
                )
            snapshot, all_gaps = project_durable_run(
                decoded, reserved_through=reserved_through
            )
        except ReplayCorruptionError as exc:
            raise JournalCorruptionError("durable Run replay is corrupt") from exc

        if snapshot.identity.run_id != run_id:
            raise JournalCorruptionError("durable Run identity is corrupt")
        if managed is not None:
            assert managed_identity is not None
            terminal_event = next(
                (
                    event
                    for event in reversed(decoded)
                    if event.kind
                    in {"run.completed", "run.failed", "run.cancelled"}
                ),
                None,
            )
            if (
                snapshot.identity != managed_identity
                or managed[3] != decoded[0].seq
                or managed[4] != decoded[-1].seq
                or managed[9] != len(decoded)
                or managed[10] != durable_bytes
                or (
                    managed[6] is None
                    and (managed[7] is not None or terminal_event is not None)
                )
                or (
                    managed[6] is not None
                    and (
                        terminal_event is None
                        or managed[6] != terminal_event.seq
                        or managed[7] != terminal_event.kind
                    )
                )
            ):
                raise JournalCorruptionError(
                    "managed Run replay metadata is inconsistent"
                )
        if expected_identity is not None and snapshot.identity != expected_identity:
            raise KeyError("durable Run does not match expected identity")
        latest = snapshot.through_seq
        if after > latest:
            raise ValueError("replay cursor is newer than the durable Run")
        page = tuple(event for event in decoded if event.seq > after)[:limit]
        next_cursor = page[-1].seq if page else after
        gaps = tuple(
            gap
            for gap in all_gaps
            if gap.to_seq > after and gap.from_seq <= next_cursor
        )
        return DurableReplay(
            identity=snapshot.identity,
            availability="complete" if snapshot.complete else "partial",
            oldest_cursor=0,
            latest_cursor=latest,
            next_cursor=next_cursor,
            has_more=next_cursor < latest,
            events=page,
            gaps=gaps,
            snapshot=snapshot,
        )

    def snapshot_for_run(
        self,
        run_id: str,
        *,
        expected_identity: RunIdentity | None = None,
    ) -> ProjectionSnapshot | None:
        """Return a validated projection for an upper-layer atomic checkpoint.

        Snapshot persistence and retention tombstones intentionally remain in
        the Conversation transaction owner: this journal facade cannot make a
        snapshot plus a separate store's prune metadata atomic on its own.
        """

        replay = self.replay(
            run_id,
            after=0,
            limit=1,
            expected_identity=expected_identity,
        )
        return replay.snapshot if replay is not None else None

    def prune_to_recent_runs(
        self,
        maximum_runs: int,
        protected_run_ids: tuple[str, ...] = (),
    ) -> int:
        """Keep bounded history without deleting an in-memory active Run."""

        if maximum_runs <= 0 or maximum_runs > 10_000:
            raise ValueError("maximum_runs must be between 1 and 10000")
        if len(protected_run_ids) > 64 or any(
            not run_id or len(run_id) > 64 for run_id in protected_run_ids
        ):
            raise ValueError("protected Run IDs are invalid")
        placeholders = ",".join("?" for _run_id in protected_run_ids)
        exclusion = (
            f"WHERE run_id NOT IN ({placeholders})" if protected_run_ids else ""
        )
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                candidates = self._connection.execute(
                    f"""
                    SELECT run_id FROM events
                    {exclusion}
                    GROUP BY run_id
                    ORDER BY MAX(rowid) DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (*protected_run_ids, maximum_runs),
                ).fetchall()
                deleted_rows = 0
                managed_schema = self._managed_schema_locked()
                for candidate in candidates:
                    run_id = candidate[0]
                    managed = (
                        self._managed_state_locked(run_id)
                        if managed_schema
                        else None
                    )
                    if managed is None:
                        cursor = self._connection.execute(
                            "DELETE FROM events WHERE run_id = ?", (run_id,)
                        )
                        deleted_rows += max(cursor.rowcount, 0)
                        continue

                    terminal_seq = managed[6]
                    terminal_kind = managed[7]
                    if terminal_seq is None:
                        # A durable active Run is never sacrificed to satisfy
                        # retention, even if an in-memory protection set was
                        # stale or incomplete.
                        continue
                    if (
                        managed[8] != "full"
                        or managed[5] != RUN_CURSOR_RESERVED_THROUGH
                    ):
                        raise JournalCorruptionError(
                            "managed Run retention state is inconsistent"
                        )
                    try:
                        events, snapshot, gaps, durable_bytes = (
                            self._validated_run_locked(
                                run_id,
                                reserved_through=managed[5],  # type: ignore[arg-type]
                            )
                        )
                    except ReplayCorruptionError as exc:
                        raise JournalCorruptionError(
                            "managed Run cannot be snapshotted safely"
                        ) from exc
                    if (
                        not snapshot.complete
                        or snapshot.identity
                        != RunIdentity(
                            managed[0], managed[1], managed[2], run_id  # type: ignore[arg-type]
                        )
                        or terminal_seq != snapshot.through_seq
                        or events[-1].kind != terminal_kind
                        or managed[3] != events[0].seq
                        or managed[4] != events[-1].seq
                        or managed[9] != len(events)
                        or managed[10] != durable_bytes
                    ):
                        raise JournalCorruptionError(
                            "managed Run snapshot metadata is inconsistent"
                        )
                    encoded_snapshot = encode_projection_snapshot(snapshot)
                    snapshot_bytes = encoded_snapshot.encode("utf-8")
                    if len(snapshot_bytes) > 65_536:
                        raise JournalCorruptionError(
                            "managed Run snapshot exceeds its storage limit"
                        )
                    source_digest = hashlib.sha256(snapshot_bytes).hexdigest()
                    self._connection.execute(
                        """
                        INSERT INTO run_snapshots(
                            run_id, projection_version, through_seq,
                            snapshot_json, source_digest, ephemeral_loss,
                            created_at
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
                            snapshot.version,
                            snapshot.through_seq,
                            encoded_snapshot,
                            source_digest,
                            int(bool(gaps)),
                            events[-1].occurred_at,
                        ),
                    )
                    cursor = self._connection.execute(
                        "DELETE FROM events WHERE run_id = ?", (run_id,)
                    )
                    if cursor.rowcount != len(events):
                        raise JournalCorruptionError(
                            "managed Run prune lost an event"
                        )
                    state_cursor = self._connection.execute(
                        """
                        UPDATE run_journal_state
                        SET oldest_available_seq = latest_durable_seq,
                            availability = 'snapshot_only',
                            event_count = 0, durable_bytes = 0
                        WHERE run_id = ? AND availability = 'full'
                          AND terminal_seq = ?
                        """,
                        (run_id, terminal_seq),
                    )
                    if state_cursor.rowcount != 1:
                        raise JournalCorruptionError(
                            "managed Run prune state update was lost"
                        )
                    deleted_rows += max(cursor.rowcount, 0)
                self._connection.commit()
                return deleted_rows
        except JournalCorruptionError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise JournalUnavailableError("could not prune the journal") from exc

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            finally:
                os.close(self._directory_descriptor)


__all__ = [
    "EventJournal",
    "JournalCorruptionError",
    "JournalUnavailableError",
]

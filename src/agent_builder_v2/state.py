"""Agent-scoped semantic event journal for the walking skeleton."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from threading import Lock

from .contracts import EventEnvelope


class JournalUnavailableError(RuntimeError):
    """Durable state cannot be opened or updated safely."""


def _open_private_regular(path: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise JournalUnavailableError("secure journal files require O_NOFOLLOW")
    try:
        descriptor = os.open(path, flags | no_follow)
    except FileNotFoundError:
        try:
            descriptor = os.open(
                path,
                flags | no_follow | os.O_CREAT | os.O_EXCL,
                0o600,
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
        path_metadata = os.stat(path, follow_symlinks=False)
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
        for suffix in ("-wal", "-shm"):
            _validate_sqlite_file(Path(f"{database_path}{suffix}"))
        descriptor, identity = _open_private_regular(database_path)
        os.close(descriptor)
        try:
            self._connection = sqlite3.connect(database_path, check_same_thread=False)
        except sqlite3.Error as exc:
            raise JournalUnavailableError("could not connect to the journal") from exc
        self._lock = Lock()
        try:
            with self._connection:
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
            _validate_sqlite_file(database_path, identity)
            for suffix in ("-wal", "-shm"):
                _validate_sqlite_file(Path(f"{database_path}{suffix}"))
        except (OSError, sqlite3.Error) as exc:
            self._connection.close()
            raise JournalUnavailableError("could not initialize the journal") from exc
        except Exception:
            self._connection.close()
            raise

    def append(self, event: EventEnvelope) -> None:
        if event.durability != "durable":
            return
        encoded = json.dumps(
            event.to_dict(), ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > 65_536:
            raise ValueError("durable event exceeds prototype journal limit")
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO events(run_id, seq, kind, occurred_at, envelope_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (event.run_id, event.seq, event.kind, event.occurred_at, encoded),
                )
        except sqlite3.Error as exc:
            raise JournalUnavailableError("could not append to the journal") from exc

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
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    f"""
                    DELETE FROM events
                    WHERE run_id IN (
                        SELECT run_id
                        FROM events
                        {exclusion}
                        GROUP BY run_id
                        ORDER BY MAX(rowid) DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (*protected_run_ids, maximum_runs),
                )
        except sqlite3.Error as exc:
            raise JournalUnavailableError("could not prune the journal") from exc
        return max(cursor.rowcount, 0)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

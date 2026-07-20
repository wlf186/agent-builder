"""Independent operator authorization and bounded audit for context reveal."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from .auth import ProjectTokenStore, is_valid_project_token
from .contracts import utc_now


MAX_CONTEXT_AUDIT_ROWS = 4_096


class ContextRevealPolicy:
    def __init__(self, repository_root: Path, *, enabled: bool) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.enabled = bool(enabled)
        self._verifier: bytes | None = None
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if not self.enabled:
            return
        token = ProjectTokenStore(
            self.repository_root,
            ".runtime/secrets/context-reveal-token",
        ).load_or_create()
        self._verifier = hashlib.sha256(token.encode("ascii")).digest()
        data_root = self.repository_root / "data"
        data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(data_root, 0o700)
        path = data_root / "context-reveal-audit.sqlite"
        self._connection = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS context_reveal_audit (
                audit_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                availability TEXT NOT NULL,
                exposed_sections INTEGER NOT NULL
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(context_reveal_audit)"
            ).fetchall()
        }
        if "agent_id" not in columns:
            self._connection.execute(
                "ALTER TABLE context_reveal_audit "
                "ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''"
            )
        self._connection.commit()

    def authorize(self, candidate: object) -> bool:
        if (
            not self.enabled
            or self._verifier is None
            or not isinstance(candidate, str)
            or not is_valid_project_token(candidate)
        ):
            return False
        return hmac.compare_digest(
            hashlib.sha256(candidate.encode("ascii")).digest(),
            self._verifier,
        )

    def record(
        self,
        *,
        agent_id: str,
        run_id: str,
        availability: str,
        exposed_sections: int,
    ) -> str:
        connection = self._connection
        if connection is None or not self.enabled:
            raise RuntimeError("context reveal policy is disabled")
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or len(agent_id) > 64
            or availability != "exact"
            or not 0 <= exposed_sections <= 128
        ):
            raise ValueError("invalid context reveal audit")
        audit_id = uuid4().hex
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO context_reveal_audit "
                    "(audit_id,agent_id,run_id,occurred_at,availability,exposed_sections) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        audit_id,
                        agent_id,
                        run_id,
                        utc_now(),
                        availability,
                        exposed_sections,
                    ),
                )
                connection.execute(
                    "DELETE FROM context_reveal_audit WHERE audit_id IN ("
                    "SELECT audit_id FROM context_reveal_audit "
                    "ORDER BY occurred_at DESC,audit_id DESC LIMIT -1 OFFSET ?)",
                    (MAX_CONTEXT_AUDIT_ROWS,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return audit_id

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


__all__ = ["ContextRevealPolicy", "MAX_CONTEXT_AUDIT_ROWS"]

"""Bounded, credential-free context reveal audit tests."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

import agent_builder_v2.context_audit as audit_module
from agent_builder_v2.context_audit import ContextRevealPolicy


def test_context_reveal_audit_is_bounded_and_never_persists_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_module, "MAX_CONTEXT_AUDIT_ROWS", 3)
    policy = ContextRevealPolicy(tmp_path, enabled=True)
    token_path = tmp_path / ".runtime" / "secrets" / "context-reveal-token"
    token = token_path.read_text(encoding="ascii").strip()
    try:
        assert policy.authorize(token)
        assert not policy.authorize("0" * 64)
        for index in range(5):
            policy.record(
                agent_id="agent-1",
                run_id=f"{index:032x}",
                availability="exact",
                exposed_sections=2,
            )
    finally:
        policy.close()

    database = tmp_path / "data" / "context-reveal-audit.sqlite"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT agent_id,run_id,availability,exposed_sections "
            "FROM context_reveal_audit ORDER BY occurred_at,audit_id"
        ).fetchall()
    assert len(rows) == 3
    assert all(row[0] == "agent-1" and row[2:] == ("exact", 2) for row in rows)
    assert token.encode("ascii") not in database.read_bytes()


def test_disabled_context_reveal_policy_creates_no_secret_or_database(
    tmp_path: Path,
) -> None:
    policy = ContextRevealPolicy(tmp_path, enabled=False)
    try:
        assert not policy.authorize("0" * 64)
        with pytest.raises(RuntimeError, match="disabled"):
            policy.record(
                agent_id="agent-1",
                run_id="1" * 32,
                availability="exact",
                exposed_sections=0,
            )
    finally:
        policy.close()
    assert not (tmp_path / ".runtime").exists()
    assert not (tmp_path / "data").exists()

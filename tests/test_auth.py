"""Authentication-session lifecycle tests with deterministic clocks."""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_builder_v2.auth import (
    AuthenticationError,
    CsrfError,
    SessionCapacityError,
    SessionService,
    ProjectTokenStore,
)


PROJECT_TOKEN = "a" * 64


def test_project_token_is_atomic_private_and_stable(tmp_path: Path) -> None:
    store = ProjectTokenStore(tmp_path)

    with ThreadPoolExecutor(max_workers=16) as executor:
        tokens = list(executor.map(lambda _index: store.load_or_create(), range(32)))

    assert len(set(tokens)) == 1
    assert len(tokens[0]) == 64
    metadata = store.path.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    assert store.path.read_text(encoding="ascii") == f"{tokens[0]}\n"
    assert store.load_or_create() == tokens[0]


def test_project_token_rejects_symlinked_secret_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    os.symlink(outside, runtime / "secrets")

    with pytest.raises(RuntimeError, match="not a real directory"):
        ProjectTokenStore(tmp_path).load_or_create()


def test_session_capacity_csrf_expiry_and_reuse() -> None:
    now = [100.0]
    service = SessionService(
        PROJECT_TOKEN,
        ttl_seconds=10,
        max_sessions=1,
        monotonic_clock=lambda: now[0],
        utc_clock=lambda: datetime(2026, 7, 17, tzinfo=timezone.utc),
    )

    session = service.create(PROJECT_TOKEN)

    assert service.validate(session.session_id).session_id == session.session_id
    assert (
        service.validate_csrf(session.session_id, session.csrf_token).session_id
        == session.session_id
    )
    with pytest.raises(CsrfError, match="CSRF validation failed"):
        service.validate_csrf(session.session_id, "wrong-csrf-token")
    with pytest.raises(SessionCapacityError, match="capacity exhausted"):
        service.create(PROJECT_TOKEN)

    now[0] = 110.0
    with pytest.raises(AuthenticationError, match="authentication required"):
        service.validate(session.session_id)
    assert service.active_count == 0

    replacement = service.create(PROJECT_TOKEN)
    assert replacement.session_id != session.session_id
    assert service.revoke(replacement.session_id) is True
    assert service.revoke(replacement.session_id) is False
    assert service.active_count == 0

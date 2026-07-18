"""Negative-security and authenticated-flow tests for the Web Gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder_v2.auth import SessionService
from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.web import (
    CSRF_COOKIE,
    MAX_JSON_BODY_BYTES,
    SESSION_COOKIE,
    LoginLimiter,
    create_app,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:20815"
SAME_ORIGIN = {"origin": BASE_URL}
PROJECT_TOKEN = "b" * 64
RUN_ID = "1" * 32


@dataclass(frozen=True)
class _RunRecord:
    agent_id: str = PROTOTYPE_AGENT_ID
    conversation_id: str = "2" * 32
    turn_id: str = "3" * 32
    run_id: str = RUN_ID


class _Commands:
    def __init__(self) -> None:
        self.started: list[Any] = []

    async def start(self, command: Any) -> _RunRecord:
        self.started.append(command)
        return _RunRecord()


class _RunService:
    capsule = object()
    model_qualification = SimpleNamespace(model="qwen3.5:2b")
    sandbox_qualification = object()


@pytest.fixture
def web_client() -> tuple[TestClient, _Commands]:
    app = create_app(REPOSITORY_ROOT)
    commands = _Commands()
    app.state.sessions = SessionService(PROJECT_TOKEN)
    app.state.run_service = _RunService()
    app.state.commands = commands
    app.state.login_limiter = LoginLimiter()
    app.state.cookie_secure = False
    # Deliberately do not enter TestClient as a context manager: the production
    # lifespan provisions a real Capsule, while these boundary tests inject all
    # route collaborators explicitly and perform no project writes.
    client = TestClient(app, base_url=BASE_URL)
    try:
        yield client, commands
    finally:
        client.close()


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "127.0.0.1@attacker.example",
        "127.0.0.1:0",
        "[::1",
    ],
)
def test_invalid_host_is_rejected_with_security_headers(
    web_client: tuple[TestClient, _Commands], host: str
) -> None:
    client, _commands = web_client

    response = client.get("/health", headers={"host": host})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid Host header"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_health_reports_qualified_runtime_dependencies(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "prototype": True,
        "agent_ready": True,
        "model": "qwen3.5:2b",
        "sandbox": "landlock+seccomp",
    }


def test_timeline_exposes_complete_events_as_inert_text(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    index = client.get("/")
    script = client.get("/assets/app.js")

    assert index.status_code == 200
    assert 'id="event-detail-dialog"' in index.text
    assert 'aria-labelledby="event-detail-title"' in index.text
    assert script.status_code == 200
    assert "JSON.stringify(envelope, null, 2)" in script.text
    assert "eventDetailJson.textContent =" in script.text
    assert ".innerHTML" not in script.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "http://localhost:20815"},
        {"origin": "https://127.0.0.1:20815"},
        {"origin": "http://127.0.0.1:20816"},
        {"origin": "http://127.0.0.1:20815/path"},
    ],
)
def test_state_change_requires_exact_same_origin(
    web_client: tuple[TestClient, _Commands], headers: dict[str, str]
) -> None:
    client, _commands = web_client

    response = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=headers
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin rejected"}
    assert SESSION_COOKIE not in client.cookies


def test_login_csrf_run_and_logout_session_flow(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, commands = web_client

    assert client.get("/api/auth/status").json() == {"authenticated": False}

    rejected = client.post(
        "/api/auth/login", json={"token": "0" * 64}, headers=SAME_ORIGIN
    )
    assert rejected.status_code == 401
    assert "set-cookie" not in rejected.headers

    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )

    assert login.status_code == 200
    assert PROJECT_TOKEN not in login.text
    csrf_token = login.json()["csrf_token"]
    assert client.cookies.get(SESSION_COOKIE)
    assert client.cookies.get(CSRF_COOKIE) == csrf_token
    set_cookies = login.headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    assert all("HttpOnly" in value for value in set_cookies)
    assert all("SameSite=strict" in value for value in set_cookies)

    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json()["csrf_token"] == csrf_token
    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json()["authenticated"] is True
    assert status.json()["csrf_token"] == csrf_token

    missing_csrf = client.post(
        "/api/runs", json={"message": "hello"}, headers=SAME_ORIGIN
    )
    assert missing_csrf.status_code == 403
    wrong_csrf = client.post(
        "/api/runs",
        json={"message": "hello"},
        headers={**SAME_ORIGIN, "x-csrf-token": "wrong-token"},
    )
    assert wrong_csrf.status_code == 403
    assert commands.started == []

    accepted = client.post(
        "/api/runs",
        json={"message": "hello"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    )
    assert accepted.status_code == 202
    assert accepted.json()["run_id"] == RUN_ID
    assert accepted.json()["events_url"] == f"/api/runs/{RUN_ID}/events"
    assert len(commands.started) == 1
    assert commands.started[0].message == "hello"

    logout = client.post(
        "/api/auth/logout",
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    )
    assert logout.status_code == 204
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_failed_login_rate_limit_is_bounded_per_client(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    for _attempt in range(5):
        response = client.post(
            "/api/auth/login", json={"token": "0" * 64}, headers=SAME_ORIGIN
        )
        assert response.status_code == 401

    limited = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert SESSION_COOKIE not in client.cookies


def test_anonymous_event_and_cancel_routes_are_rejected(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    events = client.get(f"/api/runs/{RUN_ID}/events")
    cancel = client.post(
        f"/api/runs/{RUN_ID}/cancel",
        headers=SAME_ORIGIN,
    )

    assert events.status_code == 401
    assert cancel.status_code == 403


def test_json_body_limit_accepts_boundary_and_rejects_overflow(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    prefix = b'{"token":"'
    suffix = b'"}'
    exact = prefix + b"x" * (MAX_JSON_BODY_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(exact) == MAX_JSON_BODY_BYTES

    at_limit = client.post(
        "/api/auth/login",
        content=exact,
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )
    assert at_limit.status_code == 401

    over_limit = client.post(
        "/api/auth/login",
        content=exact + b" ",
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )
    assert over_limit.status_code == 413
    assert over_limit.json() == {"detail": "request body too large"}


def test_streamed_json_body_cannot_bypass_limit_without_content_length(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    def chunks() -> Any:
        yield b'{"token":"'
        yield b"x" * MAX_JSON_BODY_BYTES
        yield b'"}'

    response = client.post(
        "/api/auth/login",
        content=chunks(),
        headers={**SAME_ORIGIN, "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_json_routes_reject_wrong_media_type_and_allow_parameters(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    body = ('{"token":"' + "0" * 64 + '"}').encode()

    wrong_type = client.post(
        "/api/auth/login",
        content=body,
        headers={**SAME_ORIGIN, "content-type": "text/plain"},
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json() == {
        "detail": "Content-Type must be application/json"
    }

    parameterized_json = client.post(
        "/api/auth/login",
        content=body,
        headers={
            **SAME_ORIGIN,
            "content-type": "application/json; charset=utf-8",
        },
    )
    assert parameterized_json.status_code == 401


def test_negative_content_length_is_rejected_at_boundary(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client

    response = client.post(
        "/api/auth/login",
        content=b"{}",
        headers={
            **SAME_ORIGIN,
            "content-type": "application/json",
            "content-length": "-1",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid content length"}

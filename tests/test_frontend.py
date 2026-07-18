"""Static contract checks for the dependency-free browser client."""

from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPOSITORY_ROOT / "src" / "agent_builder_v2" / "static"
INDEX = STATIC_ROOT.joinpath("index.html").read_text(encoding="utf-8")
SCRIPT = STATIC_ROOT.joinpath("app.js").read_text(encoding="utf-8")


def test_session_ui_exposes_create_restore_delete_and_multiturn_surfaces() -> None:
    required_ids = {
        "new-session-button",
        "session-list",
        "active-session-title",
        "session-id",
        "conversation-messages",
        "message-input",
        "run-form",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))

    assert required_ids <= html_ids
    assert 'api("/api/sessions"' in SCRIPT
    assert "`/api/sessions/${encodeURIComponent(sessionId)}`" in SCRIPT
    assert "`/api/sessions/${encodeURIComponent(sessionId)}/runs`" in SCRIPT
    assert 'method: "DELETE"' in SCRIPT
    assert "preserveTimeline: true" in SCRIPT


def test_every_javascript_element_reference_exists_in_the_page() -> None:
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))
    referenced_ids = set(re.findall(r'querySelector\("#([a-z0-9-]+)"\)', SCRIPT))

    assert referenced_ids
    assert referenced_ids - html_ids == set()


def test_browser_api_keeps_cookie_csrf_and_inert_rendering_boundaries() -> None:
    assert 'credentials: "same-origin"' in SCRIPT
    assert 'cache: "no-store"' in SCRIPT
    assert '"X-CSRF-Token": state.csrfToken' in SCRIPT
    assert "localStorage" not in SCRIPT
    assert "sessionStorage" not in SCRIPT
    assert ".innerHTML" not in SCRIPT
    assert "body.textContent =" in SCRIPT


def test_complete_timeline_event_body_remains_available() -> None:
    assert 'id="event-detail-dialog"' in INDEX
    assert "JSON.stringify(envelope, null, 2)" in SCRIPT
    assert "elements.eventDetailJson.textContent =" in SCRIPT
    assert "addTimelineEvent(envelope)" in SCRIPT


def _function_body(name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{", SCRIPT)
    assert match is not None, f"missing JavaScript function: {name}"
    start = match.end()
    depth = 1
    for index in range(start, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start:index]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def test_terminal_event_keeps_the_owning_run_locked_until_reconciliation() -> None:
    renderer = _function_body("renderEnvelope")
    completer = _function_body("completeRunContext")

    assert "runContext.terminalSeen = true" in renderer
    assert "state.activeRun = null" not in renderer
    assert "state.settling = false" not in renderer
    assert "state.activeRun = null" in completer
    assert "state.settling = false" in completer


def test_sse_reconnect_is_bounded_and_resumes_from_the_last_sequence() -> None:
    consumer = _function_body("consumeEventStream")
    reconnect = _function_body("streamWithReconnect")

    assert 'headers["Last-Event-ID"] = String(runContext.lastSeq)' in consumer
    assert "attempt <= MAX_SSE_RECONNECTS" in reconnect
    assert "runContext.terminalSeen" in reconnect
    assert "MAX_SSE_RECONNECTS = 3" in SCRIPT


def test_preserved_timeline_uses_fetch_then_commit_and_survives_refresh_error() -> None:
    selector = _function_body("selectSession")

    fetch_position = selector.index("await api(")
    render_position = selector.index("renderMessages(detail.messages)")
    assert fetch_position < render_position
    assert "if (!preserveTimeline) clearTimeline()" in selector
    assert "clearSelectedSession" not in selector


def test_turn_status_running_recovery_and_auth_expiry_are_explicit() -> None:
    assert "turnStatus: message.turn_status" in SCRIPT
    assert 'message.turn_status === "running"' in SCRIPT
    assert "attachRecoveredRun(session.session_id, recoverableRunId)" in SCRIPT
    assert 'response.status === 401' in SCRIPT
    assert 'setUnauthenticated("登录已过期，请重新认证")' in SCRIPT


def test_mutations_lock_controls_and_conflicts_refresh_server_state() -> None:
    controls = _function_body("setRunControls")
    deletion = _function_body("deleteSession")

    assert "state.mutationPending" in controls
    assert "state.mutationPending = true" in deletion
    assert "state.mutationPending = false" in deletion
    assert "error.status === 409" in deletion
    assert "await refreshSessions(state.sessionId)" in deletion

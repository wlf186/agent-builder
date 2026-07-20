"""Negative-security and authenticated-flow tests for the Web Gateway."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from agent_builder_v2.auth import SessionService
from agent_builder_v2.agents import AgentRegistry
from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.commands import CommandBus
from agent_builder_v2.context import ContextCompiler, ContextPlan, ModelProfile
from agent_builder_v2.context_audit import ContextRevealPolicy
from agent_builder_v2.contracts import EventEnvelope, StartRunCommand
from agent_builder_v2.query_engine import QueryEngineRegistry
from agent_builder_v2.replay import (
    DurableReplay,
    ReplayGap,
    RunIdentity,
    project_durable_run,
)
from agent_builder_v2.web import (
    CSRF_COOKIE,
    MAX_JSON_BODY_BYTES,
    SESSION_COOKIE,
    LoginLimiter,
    create_app,
)
from agent_builder_v2.sessions import (
    Conversation,
    ConversationConflictError,
    ConversationDeleteResult,
    ConversationNotFoundError,
    ConversationStoreUnavailableError,
    ConversationSummary,
    ConversationTurn,
)
from agent_builder_v2.tools import prototype_tool_specs, toolset_digest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:20815"
SAME_ORIGIN = {"origin": BASE_URL}
PROJECT_TOKEN = "b" * 64
RUN_ID = "1" * 32
PLAN_DIGEST = "a" * 64


def _started_payload(
    context_plan: dict[str, object] | None = None,
) -> dict[str, object]:
    if context_plan is None:
        context_plan = {
            "plan_id": f"context-{PLAN_DIGEST[:24]}",
            "digest": PLAN_DIGEST,
            "toolset_digest": toolset_digest(prototype_tool_specs()),
            "section_count": 3,
            "history_message_count": 0,
            "included_history_message_count": 0,
            "omitted_history_message_count": 0,
            "history_source_digest": "b" * 64,
            "windowing_strategy": "full",
            "estimated_input_tokens": 1_024,
            "native_context_tokens": 262_144,
            "operational_context_tokens": 32_768,
            "input_budget_tokens": 30_720,
            "compact_at_tokens": 24_576,
            "compact_target_tokens": 18_432,
            "output_reserve_tokens": 2_048,
            "template_reserve_tokens": 256,
            "estimator": "utf8-bytes-upper-bound-v1",
        }
    return {
        "prototype": True,
        "model": "qwen3.5:2b",
        "visible_tools": ["builtin/echo"],
        "sandbox": "harness-v2-worker-v1",
        "context_plan": dict(context_plan),
    }


def _completed_payload() -> dict[str, object]:
    return {
        "reason": "end_turn",
        "model_iterations": 1,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "last_input_tokens": 100,
            "complete": True,
        },
    }


@dataclass(frozen=True)
class _RunRecord:
    agent_id: str = PROTOTYPE_AGENT_ID
    conversation_id: str = "2" * 32
    turn_id: str = "3" * 32
    run_id: str = RUN_ID
    context_plan: ContextPlan | None = None
    events: tuple[EventEnvelope, ...] = ()


def _context_plan(
    message: str, agent_id: str = PROTOTYPE_AGENT_ID
) -> ContextPlan:
    return ContextCompiler().compile(
        message,
        model_profile=ModelProfile(
            provider="ollama",
            model="qwen3.5:2b",
            model_digest="c" * 64,
            native_context_tokens=262_144,
            operational_context_tokens=32_768,
            max_output_tokens=2_048,
            profile_source="test-profile",
        ),
        tools=prototype_tool_specs(),
        agent_id=agent_id,
        capsule_generation=1,
    )


class _Commands(CommandBus):
    def __init__(self, query_engines: QueryEngineRegistry) -> None:
        super().__init__(query_engines)
        self.started: list[Any] = []

    async def start(self, command: StartRunCommand) -> Any:
        self.started.append(command)
        return await super().start(command)


class _LifecycleManager:
    """Boundary-test lifecycle fence; execution is injected separately."""

    def __init__(self) -> None:
        self.draining: set[str] = set()
        self.runtimes: dict[str, object] = {}

    def register(
        self,
        agent_id: str,
        run_service: object,
        query_engines: QueryEngineRegistry,
        commands: CommandBus,
    ) -> None:
        self.runtimes[agent_id] = SimpleNamespace(
            agent_id=agent_id,
            generation=1,
            run_service=run_service,
            query_engines=query_engines,
            commands=commands,
        )

    async def for_agent(self, agent_id: str) -> object:
        if agent_id in self.draining:
            raise RuntimeError("Agent runtime is draining")
        try:
            return self.runtimes[agent_id]
        except KeyError as exc:
            raise KeyError("Agent not found") from exc

    async def begin_drain(self, agent_id: str) -> None:
        self.draining.add(agent_id)

    async def end_drain(self, agent_id: str) -> None:
        self.draining.discard(agent_id)


class _RunService:
    capsule = object()
    model_qualification = SimpleNamespace(model="qwen3.5:2b")
    sandbox_qualification = object()

    def __init__(self, agent_id: str = PROTOTYPE_AGENT_ID) -> None:
        self.agent_id = agent_id
        self.conversations: dict[str, Conversation] = {}
        self.runs: dict[str, _RunRecord] = {}
        self.run_identities: dict[str, RunIdentity] = {}
        self.durable_events: dict[str, tuple[EventEnvelope, ...]] = {}
        self.cancelled: list[str] = []
        self.streamed: list[tuple[str, int]] = []
        self.replayed: list[tuple[str, int, int]] = []
        self.resolve_calls = 0
        self.delete_identity_on_resolve_call: int | None = None
        self.get_calls = 0
        self.evict_on_get_call: int | None = None
        self.foreign_on_get_call: int | None = None
        self.snapshot_only = False
        self.replay_error: BaseException | None = None

    async def create_conversation(self, title: str) -> Conversation:
        conversation = Conversation(
            conversation_id="4" * 32,
            agent_id=self.agent_id,
            title=title,
            created_at="2026-07-18T00:00:00.000Z",
            updated_at="2026-07-18T00:00:00.000Z",
            revision=0,
            active_run_id=None,
            turns=(),
        )
        self.conversations[conversation.conversation_id] = conversation
        return conversation

    async def list_conversations(self) -> tuple[ConversationSummary, ...]:
        return tuple(
            ConversationSummary(
                conversation_id=value.conversation_id,
                agent_id=value.agent_id,
                title=value.title,
                created_at=value.created_at,
                updated_at=value.updated_at,
                revision=value.revision,
                active_run_id=value.active_run_id,
                turn_count=len(value.turns),
                completed_turn_count=sum(
                    turn.status == "completed" for turn in value.turns
                ),
                last_run_id=value.turns[-1].run_id if value.turns else None,
            )
            for value in self.conversations.values()
        )

    async def get_conversation(self, conversation_id: str) -> Conversation:
        try:
            return self.conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError("conversation not found") from exc

    async def delete_conversation(
        self, conversation_id: str
    ) -> ConversationDeleteResult:
        value = self.conversations.pop(conversation_id, None)
        return ConversationDeleteResult(
            deleted=value is not None,
            deleted_turns=len(value.turns) if value is not None else 0,
            deleted_events=0,
        )

    async def start(self, command: StartRunCommand) -> _RunRecord:
        command.validate()
        if command.agent_id != self.agent_id:
            raise ValueError("unknown Agent")
        conversation_id = command.conversation_id or "2" * 32
        if conversation_id not in self.conversations:
            if command.conversation_id is not None:
                raise ConversationNotFoundError("conversation not found")
            self.conversations[conversation_id] = Conversation(
                conversation_id=conversation_id,
                agent_id=self.agent_id,
                title="新会话",
                created_at="2026-07-18T00:00:00.000Z",
                updated_at="2026-07-18T00:00:00.000Z",
                revision=0,
                active_run_id=None,
                turns=(),
            )
        plan = _context_plan(command.message, self.agent_id)
        started_event = EventEnvelope(
            event_id="7" * 32,
            agent_id=self.agent_id,
            conversation_id=conversation_id,
            turn_id="3" * 32,
            run_id=RUN_ID,
            seq=1,
            occurred_at="2026-07-18T00:00:00.000Z",
            kind="run.started",
            durability="durable",
            payload=_started_payload(plan.public_metadata()),
        )
        record = _RunRecord(
            agent_id=self.agent_id,
            conversation_id=conversation_id,
            context_plan=plan,
            events=(started_event,),
        )
        self.runs[record.run_id] = record
        identity = RunIdentity(
            record.agent_id,
            record.conversation_id,
            record.turn_id,
            record.run_id,
        )
        self.run_identities[record.run_id] = identity
        self.durable_events[record.run_id] = (
            started_event,
            EventEnvelope(
                event_id="8" * 32,
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=2,
                occurred_at="2026-07-18T00:00:00.001Z",
                kind="assistant.block.started",
                durability="durable",
                payload={"block_id": "answer", "block_type": "content"},
            ),
            EventEnvelope(
                event_id="9" * 32,
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=3,
                occurred_at="2026-07-18T00:00:00.002Z",
                kind="assistant.block.finished",
                durability="durable",
                payload={"block_id": "answer", "content": "durable answer"},
            ),
            EventEnvelope(
                event_id="a" * 32,
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
                seq=4,
                occurred_at="2026-07-18T00:00:00.003Z",
                kind="run.completed",
                durability="durable",
                payload=_completed_payload(),
            ),
        )
        return record

    def get(self, run_id: str) -> _RunRecord:
        self.get_calls += 1
        if self.evict_on_get_call == self.get_calls:
            self.runs.pop(run_id, None)
        if self.foreign_on_get_call == self.get_calls and run_id in self.runs:
            self.runs[run_id] = replace(
                self.runs[run_id], agent_id="foreign-agent"
            )
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise KeyError("run not found") from exc

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def stream(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[EventEnvelope | None]:
        self.streamed.append((run_id, after))
        record = self.get(run_id)
        event = EventEnvelope(
            event_id="6" * 32,
            agent_id=record.agent_id,
            conversation_id=record.conversation_id,
            turn_id=record.turn_id,
            run_id=record.run_id,
            seq=1,
            occurred_at="2026-07-18T00:00:01.000Z",
            kind="run.completed",
            durability="durable",
            payload={"reason": "end_turn", "model_iterations": 1},
        )
        if event.seq > after:
            yield event

    async def resolve_run_identity(self, run_id: str) -> RunIdentity:
        self.resolve_calls += 1
        if self.delete_identity_on_resolve_call == self.resolve_calls:
            self.run_identities.pop(run_id, None)
            raise KeyError("run not found")
        try:
            return self.run_identities[run_id]
        except KeyError as exc:
            raise KeyError("run not found") from exc

    async def replay_run(
        self,
        run_id: str,
        *,
        after: int,
        limit: int,
        expected_identity: RunIdentity,
    ) -> DurableReplay:
        self.replayed.append((run_id, after, limit))
        if self.replay_error is not None:
            raise self.replay_error
        if self.run_identities.get(run_id) != expected_identity:
            raise KeyError("run not found")
        events = self.durable_events[run_id]
        snapshot, gaps = project_durable_run(events)
        if after > snapshot.through_seq:
            raise ValueError("replay cursor is newer than the durable Run")
        if self.snapshot_only:
            return DurableReplay(
                identity=expected_identity,
                availability="snapshot_only",
                oldest_cursor=snapshot.through_seq,
                latest_cursor=snapshot.through_seq,
                next_cursor=snapshot.through_seq,
                has_more=False,
                events=(),
                gaps=(
                    ()
                    if after == snapshot.through_seq
                    else (
                        ReplayGap(
                            after + 1,
                            snapshot.through_seq,
                            "retention",
                        ),
                    )
                ),
                snapshot=snapshot,
            )
        page = tuple(event for event in events if event.seq > after)[:limit]
        next_cursor = page[-1].seq if page else after
        return DurableReplay(
            identity=expected_identity,
            availability="complete",
            oldest_cursor=0,
            latest_cursor=snapshot.through_seq,
            next_cursor=next_cursor,
            has_more=next_cursor < snapshot.through_seq,
            events=page,
            gaps=tuple(
                gap
                for gap in gaps
                if gap.to_seq > after and gap.from_seq <= next_cursor
            ),
            snapshot=snapshot,
        )


@pytest.fixture
def web_client() -> tuple[TestClient, _Commands]:
    app = create_app(REPOSITORY_ROOT)
    run_service = _RunService()
    app.state.sessions = SessionService(PROJECT_TOKEN)
    app.state.run_service = run_service
    query_engines = QueryEngineRegistry(
        run_service, PROTOTYPE_AGENT_ID
    )  # type: ignore[arg-type]
    commands = _Commands(query_engines)
    runtime_manager = _LifecycleManager()
    runtime_manager.register(
        PROTOTYPE_AGENT_ID, run_service, query_engines, commands
    )
    app.state.runtime_manager = runtime_manager
    app.state.query_engines = query_engines
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
    assert 'id="replay-workbench"' in index.text
    assert 'id="sequence-lane-header"' in index.text
    assert 'id="turn-conversation-panel"' in index.text
    assert 'id="event-inspector"' in index.text
    assert 'data-inspector-tab="business"' in index.text
    assert 'data-inspector-tab="envelope"' in index.text
    assert "<canvas" not in index.text.lower()
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


def test_authenticated_agent_lifecycle_api_is_csrf_protected_and_isolated(
    web_client: tuple[TestClient, _Commands], tmp_path: Path
) -> None:
    client, _commands = web_client
    registry = AgentRegistry(tmp_path)
    registry.initialize()
    client.app.state.agent_registry = registry
    try:
        login = client.post(
            "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
        )
        csrf = login.json()["csrf_token"]
        mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}
        assert client.post(
            "/api/agents", json={"display_name": "No CSRF"}, headers=SAME_ORIGIN
        ).status_code == 403
        created = client.post(
            "/api/agents",
            json={"display_name": "API Agent"},
            headers=mutation,
        )
        assert created.status_code == 201
        agent_id = created.json()["agent_id"]
        assert created.json()["generation"] == 1
        assert client.get(f"/api/agents/{agent_id}").status_code == 200
        upgraded = client.post(
            f"/api/agents/{agent_id}/upgrade",
            json={"display_name": "API Agent v2"},
            headers=mutation,
        )
        assert upgraded.status_code == 200
        assert upgraded.json()["generation"] == 2
        assert client.delete(
            f"/api/agents/{PROTOTYPE_AGENT_ID}", headers=mutation
        ).status_code == 409
        assert client.delete(f"/api/agents/{agent_id}", headers=mutation).status_code == 204
        assert client.get(f"/api/agents/{agent_id}").status_code == 404
    finally:
        registry.close()


def test_agent_scoped_session_run_stream_and_context_do_not_cross_agents(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    agent_id = "a" * 32
    service = _RunService(agent_id)
    engines = QueryEngineRegistry(service, agent_id)  # type: ignore[arg-type]
    commands = _Commands(engines)
    manager = client.app.state.runtime_manager
    manager.register(agent_id, service, engines, commands)
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf = login.json()["csrf_token"]
    mutation = {**SAME_ORIGIN, "x-csrf-token": csrf}

    created = client.post(
        f"/api/agents/{agent_id}/sessions",
        json={"title": "Scoped"},
        headers=mutation,
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    started = client.post(
        f"/api/agents/{agent_id}/sessions/{session_id}/runs",
        json={"message": "isolated"},
        headers=mutation,
    )
    assert started.status_code == 202
    assert started.json()["agent_id"] == agent_id
    assert started.json()["events_url"].startswith(
        f"/api/agents/{agent_id}/runs/"
    )
    streamed = client.get(started.json()["events_url"])
    assert streamed.status_code == 200
    assert '"agent_id":"' + agent_id + '"' in streamed.text
    context = client.get(f"/api/agents/{agent_id}/runs/{RUN_ID}/context")
    assert context.status_code == 200
    assert context.json()["identity"]["agent_id"] == agent_id

    assert client.get(f"/api/runs/{RUN_ID}/context").status_code == 404
    assert client.get(
        f"/api/agents/{PROTOTYPE_AGENT_ID}/sessions/{session_id}"
    ).status_code == 404


def test_operator_chosen_access_token_can_create_a_web_session(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    operator_token = "operator-token_2026"
    client.app.state.sessions = SessionService(operator_token)

    rejected = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert rejected.status_code == 401
    accepted = client.post(
        "/api/auth/login", json={"token": operator_token}, headers=SAME_ORIGIN
    )
    assert accepted.status_code == 200
    assert operator_token not in accepted.text


def test_authenticated_conversation_create_restore_multiturn_and_delete_api(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, commands = web_client
    assert client.get("/api/sessions").status_code == 401
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}

    created = client.post(
        "/api/sessions",
        json={"title": "持续会话"},
        headers=mutation_headers,
    )
    assert created.status_code == 201
    conversation_id = created.json()["session_id"]
    assert created.json()["state"] == "idle"

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert [item["session_id"] for item in listed.json()["sessions"]] == [
        conversation_id
    ]

    run = client.post(
        f"/api/sessions/{conversation_id}/runs",
        json={"message": "第一轮"},
        headers=mutation_headers,
    )
    assert run.status_code == 202
    assert run.json() == {
        "run_id": RUN_ID,
        "session_id": conversation_id,
        "events_url": f"/api/runs/{RUN_ID}/events",
    }
    assert commands.started[-1].conversation_id == conversation_id

    service = client.app.state.run_service
    service.conversations[conversation_id] = Conversation(
        conversation_id=conversation_id,
        agent_id=PROTOTYPE_AGENT_ID,
        title="持续会话",
        created_at="2026-07-18T00:00:00.000Z",
        updated_at="2026-07-18T00:00:00.500Z",
        revision=1,
        active_run_id=RUN_ID,
        turns=(
            ConversationTurn(
                turn_id="5" * 32,
                conversation_id=conversation_id,
                run_id=RUN_ID,
                position=1,
                status="running",
                user_content="第一轮",
                assistant_content=None,
                created_at="2026-07-18T00:00:00.100Z",
                updated_at="2026-07-18T00:00:00.100Z",
            ),
        ),
    )
    running = client.get(f"/api/sessions/{conversation_id}")
    assert running.status_code == 200
    assert running.json()["session"]["state"] == "running"
    assert running.json()["messages"] == [
        {
            "message_id": running.json()["messages"][0]["message_id"],
            "role": "user",
            "content": "第一轮",
            "created_at": "2026-07-18T00:00:00.100Z",
            "turn_id": "5" * 32,
            "run_id": RUN_ID,
            "turn_status": "running",
        }
    ]

    service.conversations[conversation_id] = Conversation(
        conversation_id=conversation_id,
        agent_id=PROTOTYPE_AGENT_ID,
        title="持续会话",
        created_at="2026-07-18T00:00:00.000Z",
        updated_at="2026-07-18T00:00:01.000Z",
        revision=2,
        active_run_id=None,
        turns=(
            ConversationTurn(
                turn_id="5" * 32,
                conversation_id=conversation_id,
                run_id=RUN_ID,
                position=1,
                status="completed",
                user_content="第一轮",
                assistant_content="第一轮回答",
                created_at="2026-07-18T00:00:00.100Z",
                updated_at="2026-07-18T00:00:01.000Z",
            ),
        ),
    )
    restored = client.get(f"/api/sessions/{conversation_id}")
    assert restored.status_code == 200
    assert [message["role"] for message in restored.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert restored.json()["messages"][1]["content"] == "第一轮回答"
    assert {
        message["turn_status"] for message in restored.json()["messages"]
    } == {"completed"}

    missing_csrf = client.delete(
        f"/api/sessions/{conversation_id}", headers=SAME_ORIGIN
    )
    assert missing_csrf.status_code == 403
    deleted = client.delete(
        f"/api/sessions/{conversation_id}", headers=mutation_headers
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/sessions/{conversation_id}").status_code == 404


def test_authenticated_sse_and_cancel_use_query_engine_ownership(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
    started = client.post(
        "/api/runs",
        json={"message": "query engine route"},
        headers=mutation_headers,
    )
    assert started.status_code == 202

    events = client.get(started.json()["events_url"])
    assert events.status_code == 200
    assert "event: run.completed" in events.text
    assert f'"run_id":"{RUN_ID}"' in events.text
    assert client.app.state.run_service.streamed == [(RUN_ID, 0)]
    assert client.app.state.run_service.replayed == []

    cancelled = client.post(
        f"/api/runs/{RUN_ID}/cancel", headers=mutation_headers
    )
    assert cancelled.status_code == 202
    assert cancelled.json() == {"accepted": True}
    assert client.app.state.run_service.cancelled == [RUN_ID]


def test_authenticated_durable_replay_pages_without_live_run_record(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    started = client.post(
        "/api/runs",
        json={"message": "durable replay"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    )
    assert started.status_code == 202
    client.app.state.run_service.runs.clear()

    first = client.get(f"/api/runs/{RUN_ID}/replay?after=0&limit=2")
    second = client.get(f"/api/runs/{RUN_ID}/replay?after=2&limit=2")

    assert first.status_code == 200
    assert first.json()["identity"] == {
        "agent_id": PROTOTYPE_AGENT_ID,
        "conversation_id": "2" * 32,
        "turn_id": "3" * 32,
        "run_id": RUN_ID,
    }
    assert first.json()["availability"] == "complete"
    assert first.json()["oldest_cursor"] == 0
    assert first.json()["latest_cursor"] == 4
    assert first.json()["next_cursor"] == 2
    assert first.json()["has_more"] is True
    assert [event["seq"] for event in first.json()["events"]] == [1, 2]
    assert first.json()["snapshot"]["through_seq"] == 4
    assert first.json()["snapshot"]["document"]["terminal"]["kind"] == (
        "run.completed"
    )
    assert second.status_code == 200
    assert second.json()["next_cursor"] == 4
    assert second.json()["has_more"] is False
    assert [event["seq"] for event in second.json()["events"]] == [3, 4]


def test_authenticated_retained_context_is_exact_no_store_and_withheld(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    secret = "operator-prompt-secret-83"
    assert client.post(
        "/api/runs",
        json={"message": secret},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["identity"] == {
        "agent_id": PROTOTYPE_AGENT_ID,
        "conversation_id": "2" * 32,
        "turn_id": "3" * 32,
        "run_id": RUN_ID,
    }
    assert payload["availability"] == "exact"
    assert payload["content_exposure"] == "withheld"
    assert payload["provider_message_count"] == 2
    assert "provider_messages" not in payload
    assert payload["renderer"]["version"] == "ordered-sections-v2"
    assert (
        payload["renderer"]["section_registry_version"]
        == "prompt-section-registry-v1"
    )
    assert payload["renderer"]["leading_system_sections_merged"] is True
    assert payload["renderer"]["leading_system_section_count"] == 2
    assert [section["id"] for section in payload["sections"]] == [
        "platform.contract",
        "agent.instructions",
        "turn.user",
    ]
    assert all("content" not in section for section in payload["sections"])
    assert all(
        "content_bytes" in section
        and "content_digest" in section
        and "dependency_digest" in section
        and "budget_tokens" in section
        and "truncation_reason" in section
        for section in payload["sections"]
    )
    turn_section = next(
        section for section in payload["sections"] if section["id"] == "turn.user"
    )
    digest_input = (
        b"agent-builder-context-section-inspection-v1\0"
        + secret.encode("utf-8")
    )
    assert turn_section["content_digest"] != hashlib.sha256(
        digest_input
    ).hexdigest()
    assert turn_section["content_digest"] != hmac.new(
        PROJECT_TOKEN.encode("utf-8"), digest_input, hashlib.sha256
    ).hexdigest()
    assert "keyed inspection digests" in payload["notice"]
    assert secret not in response.text

    elevated = client.get(f"/api/runs/{RUN_ID}/context?include_content=true")
    assert elevated.status_code == 400


def test_context_reveal_requires_independent_token_audits_and_redacts(
    web_client: tuple[TestClient, _Commands], tmp_path: Path
) -> None:
    client, _commands = web_client
    policy = ContextRevealPolicy(tmp_path, enabled=True)
    client.app.state.context_reveal = policy
    try:
        login = client.post(
            "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
        )
        csrf_token = login.json()["csrf_token"]
        secret = "token=0123456789abcdef0123456789abcdef"
        assert client.post(
            "/api/runs",
            json={"message": f"{secret} visible"},
            headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
        ).status_code == 202
        endpoint = f"/api/runs/{RUN_ID}/context/reveal"
        mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
        assert client.post(endpoint, headers=mutation_headers).status_code == 403
        token = tmp_path.joinpath(
            ".runtime", "secrets", "context-reveal-token"
        ).read_text(encoding="ascii").strip()
        response = client.post(
            endpoint,
            headers={
                **mutation_headers,
                "x-context-operator-token": token,
            },
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["content_exposure"] == "redacted_excerpt"
        assert len(payload["audit_id"]) == 32
        assert secret not in response.text
        assert "[REDACTED]" in response.text
        assert payload["sections"][0]["exposure"] == "withheld"
        assert payload["sections"][1]["exposure"] == "withheld"
    finally:
        policy.close()


def test_context_reveal_is_hidden_when_disabled_and_rejects_evicted_plan(
    web_client: tuple[TestClient, _Commands], tmp_path: Path
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    mutation_headers = {**SAME_ORIGIN, "x-csrf-token": csrf_token}
    assert client.post(
        "/api/runs",
        json={"message": "bounded reveal"},
        headers=mutation_headers,
    ).status_code == 202
    endpoint = f"/api/runs/{RUN_ID}/context/reveal"

    disabled = ContextRevealPolicy(tmp_path, enabled=False)
    client.app.state.context_reveal = disabled
    try:
        assert client.post(endpoint, headers=mutation_headers).status_code == 404
    finally:
        disabled.close()

    enabled = ContextRevealPolicy(tmp_path, enabled=True)
    client.app.state.context_reveal = enabled
    try:
        token = tmp_path.joinpath(
            ".runtime", "secrets", "context-reveal-token"
        ).read_text(encoding="ascii").strip()
        client.app.state.run_service.runs.clear()
        response = client.post(
            endpoint,
            headers={
                **mutation_headers,
                "x-context-operator-token": token,
            },
        )
        assert response.status_code == 409
        assert token not in response.text
    finally:
        enabled.close()


def test_historical_context_falls_back_to_validated_summary_only(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "evicted exact context"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    expected_context_plan = dict(
        service.durable_events[RUN_ID][0].payload["context_plan"]
    )
    service.runs.clear()
    service.snapshot_only = True

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["availability"] == "summary_only"
    assert payload["context_plan"] == expected_context_plan
    assert payload["provider_message_count"] is None
    assert payload["renderer"]["version"] is None
    assert payload["sections"] == []
    assert payload["content_exposure"] == "unavailable"
    assert "no longer resident" in payload["notice"]
    assert service.replayed == [(RUN_ID, 0, 1)]


def test_context_retention_race_falls_back_but_foreign_record_does_not(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "second lookup eviction"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.evict_on_get_call = 2

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 200
    assert response.json()["availability"] == "summary_only"
    assert service.replayed == [(RUN_ID, 0, 1)]


def test_context_second_lookup_foreign_record_stays_not_found(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "second lookup foreign replacement"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.foreign_on_get_call = 2

    response = client.get(f"/api/runs/{RUN_ID}/context")

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}
    assert service.replayed == []


def test_context_route_maps_missing_conflict_and_unavailable_states(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.get(f"/api/runs/{RUN_ID}/context").status_code == 404
    assert client.post(
        "/api/runs",
        json={"message": "context failures"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service

    original = service.runs[RUN_ID]
    service.runs[RUN_ID] = replace(
        original, context_plan=_context_plan("different valid plan")
    )
    swapped_plan = client.get(f"/api/runs/{RUN_ID}/context")
    assert swapped_plan.status_code == 503
    assert swapped_plan.json() == {"detail": "run context is unavailable"}

    service.runs[RUN_ID] = replace(original, context_plan=None)
    unavailable_exact = client.get(f"/api/runs/{RUN_ID}/context")
    assert unavailable_exact.status_code == 503
    assert unavailable_exact.json() == {"detail": "run context is unavailable"}

    service.runs.clear()
    service.replay_error = ConversationConflictError("snapshot unavailable")
    conflict = client.get(f"/api/runs/{RUN_ID}/context")
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "run context is unavailable"}

    service.replay_error = ConversationStoreUnavailableError("store offline")
    unavailable_summary = client.get(f"/api/runs/{RUN_ID}/context")
    assert unavailable_summary.status_code == 503
    assert unavailable_summary.json() == {
        "detail": "run context is unavailable"
    }

    service.replay_error = None
    service.run_identities.clear()
    assert client.get(f"/api/runs/{RUN_ID}/context").status_code == 404
    assert client.get("/api/runs/not-a-run/context").status_code == 404


def test_sse_falls_back_to_paginated_durable_replay_without_terminal_repeat(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "long durable stream"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    identity = service.run_identities[RUN_ID]

    events = [
        EventEnvelope(
            event_id=f"{1_000:032x}",
            agent_id=identity.agent_id,
            conversation_id=identity.conversation_id,
            turn_id=identity.turn_id,
            run_id=identity.run_id,
            seq=1,
            occurred_at="2026-07-18T00:00:00.001Z",
            kind="run.started",
            durability="durable",
            payload=_started_payload(),
        )
    ]
    sequence = 2
    for index in range(65):
        block_id = f"block-{index}"
        events.append(
            EventEnvelope(
                event_id=f"{1_000 + sequence:032x}",
                agent_id=identity.agent_id,
                conversation_id=identity.conversation_id,
                turn_id=identity.turn_id,
                run_id=identity.run_id,
                seq=sequence,
                occurred_at=f"2026-07-18T00:00:00.{sequence:03d}Z",
                kind="assistant.block.started",
                durability="durable",
                payload={"block_id": block_id, "block_type": "content"},
            )
        )
        sequence += 1
        events.append(
            EventEnvelope(
                event_id=f"{1_000 + sequence:032x}",
                agent_id=identity.agent_id,
                conversation_id=identity.conversation_id,
                turn_id=identity.turn_id,
                run_id=identity.run_id,
                seq=sequence,
                occurred_at=f"2026-07-18T00:00:00.{sequence:03d}Z",
                kind="assistant.block.finished",
                durability="durable",
                payload={"block_id": block_id, "content": str(index)},
            )
        )
        sequence += 1
    events.append(
        EventEnvelope(
            event_id=f"{1_000 + sequence:032x}",
            agent_id=identity.agent_id,
            conversation_id=identity.conversation_id,
            turn_id=identity.turn_id,
            run_id=identity.run_id,
            seq=sequence,
            occurred_at=f"2026-07-18T00:00:00.{sequence:03d}Z",
            kind="run.completed",
            durability="durable",
            payload=_completed_payload(),
        )
    )
    terminal_seq = sequence
    service.durable_events[RUN_ID] = tuple(events)
    service.runs.clear()

    replayed = client.get(f"/api/runs/{RUN_ID}/events")

    assert replayed.status_code == 200
    assert replayed.text.count("event: run.completed") == 1
    assert f"id: {terminal_seq}\nevent: run.completed" in replayed.text
    assert service.streamed == []
    assert service.replayed[:2] == [
        (RUN_ID, 0, 128),
        (RUN_ID, 128, 128),
    ]

    acknowledged = client.get(
        f"/api/runs/{RUN_ID}/events",
        headers={"last-event-id": str(terminal_seq)},
    )
    assert acknowledged.status_code == 200
    assert "event: run.completed" not in acknowledged.text
    assert acknowledged.text == ""

    before_terminal = client.get(
        f"/api/runs/{RUN_ID}/events",
        headers={"last-event-id": str(terminal_seq - 1)},
    )
    assert before_terminal.text.count("event: run.completed") == 1


def test_sse_durable_replay_interleaves_explicit_gap_control_frame(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "durable gap"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    original = service.durable_events[RUN_ID]
    service.durable_events[RUN_ID] = (
        original[0],
        original[1],
        replace(original[2], seq=4),
        replace(original[3], seq=5),
    )
    service.runs.clear()

    response = client.get(f"/api/runs/{RUN_ID}/events")

    assert response.status_code == 200
    gap_frame = "id: 3\nevent: stream.gap"
    assert response.text.count("event: stream.gap") == 1
    assert '"from_seq":3,"to_seq":3' in response.text
    assert '"reason":"ephemeral_not_durable"' in response.text
    assert response.text.index("id: 2\n") < response.text.index(gap_frame)
    assert response.text.index(gap_frame) < response.text.index("id: 4\n")
    assert '"seq":4' in response.text
    assert response.text.count("event: run.completed") == 1
    assert service.streamed == []


def test_sse_snapshot_only_emits_once_then_respects_last_event_id(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "retained snapshot"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.snapshot_only = True
    service.runs.clear()

    first = client.get(f"/api/runs/{RUN_ID}/events")

    assert first.status_code == 200
    assert first.text.count("event: stream.gap") == 1
    assert "id: 4\nevent: stream.gap" not in first.text
    assert first.text.count("event: stream.snapshot") == 1
    assert "id: 4\nevent: stream.snapshot" in first.text
    assert first.text.index("event: stream.gap") < first.text.index(
        "id: 4\nevent: stream.snapshot"
    )
    assert '"control_version":"stream-control-v1"' in first.text
    assert '"through_seq":4' in first.text
    assert "event: run.completed" not in first.text
    assert service.streamed == []

    resumed = client.get(
        f"/api/runs/{RUN_ID}/events", headers={"last-event-id": "4"}
    )
    assert resumed.status_code == 200
    assert resumed.text == ""


def test_sse_durable_delete_race_ends_before_prefetched_identity_leaks(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "delete race"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service
    service.runs.clear()
    service.delete_identity_on_resolve_call = 2

    response = client.get(f"/api/runs/{RUN_ID}/events")

    assert response.status_code == 200
    assert response.text == ""
    assert RUN_ID not in response.text
    assert service.streamed == []
    assert service.replayed == [(RUN_ID, 0, 128)]


@pytest.mark.parametrize(
    "query",
    [
        "after=-1",
        "after=1000001",
        "after=not-a-number",
        "after=0&after=1",
        "limit=0",
        "limit=129",
        "limit=not-a-number",
        "unknown=1",
    ],
)
def test_replay_query_boundaries_fail_before_durable_lookup(
    web_client: tuple[TestClient, _Commands], query: str
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    assert login.status_code == 200

    response = client.get(f"/api/runs/{RUN_ID}/replay?{query}")

    assert response.status_code == 400


def test_replay_maps_missing_conflict_cursor_and_unavailable_states(
    web_client: tuple[TestClient, _Commands],
) -> None:
    client, _commands = web_client
    login = client.post(
        "/api/auth/login", json={"token": PROJECT_TOKEN}, headers=SAME_ORIGIN
    )
    csrf_token = login.json()["csrf_token"]
    assert client.post(
        "/api/runs",
        json={"message": "error mapping"},
        headers={**SAME_ORIGIN, "x-csrf-token": csrf_token},
    ).status_code == 202
    service = client.app.state.run_service

    newer = client.get(f"/api/runs/{RUN_ID}/replay?after=5")
    assert newer.status_code == 416
    assert newer.json() == {
        "detail": "replay cursor is outside the durable Run"
    }

    service.replay_error = ConversationConflictError("snapshot unavailable")
    conflict = client.get(f"/api/runs/{RUN_ID}/replay")
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "run replay is unavailable"}

    service.replay_error = ConversationStoreUnavailableError("store offline")
    unavailable = client.get(f"/api/runs/{RUN_ID}/replay")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "durable replay is unavailable"}

    service.replay_error = None
    service.run_identities.clear()
    missing = client.get(f"/api/runs/{RUN_ID}/replay")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "run not found"}

    malformed = client.get("/api/runs/not-a-run/replay")
    assert malformed.status_code == 404


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
    replay = client.get(f"/api/runs/{RUN_ID}/replay")
    context = client.get(f"/api/runs/{RUN_ID}/context")
    cancel = client.post(
        f"/api/runs/{RUN_ID}/cancel",
        headers=SAME_ORIGIN,
    )

    assert events.status_code == 401
    assert replay.status_code == 401
    assert context.status_code == 401
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

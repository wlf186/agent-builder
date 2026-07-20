"""Authenticated all-interface Web Gateway for the greenfield prototype."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .auth import (
    AuthenticationError,
    CsrfError,
    ProjectTokenStore,
    SessionCapacityError,
    SessionService,
)
from .agents import AgentRegistry
from .agent_runtime import AgentRuntime, AgentRuntimeManager
from .capsule import PROTOTYPE_AGENT_ID, SAFE_ID
from .commands import RUN_ID, CommandBus
from .contracts import StartRunCommand
from .control import RunService
from .context_audit import ContextRevealPolicy
from .query_engine import (
    QueryContextUnavailableError,
    QueryEngineOwnershipError,
    QueryEngineRegistry,
    QueryReplayCursorError,
    QueryReplayUnavailableError,
    QueryRunNotRetainedError,
)
from .replay import DurableReplay, MAX_REPLAY_PAGE, MAX_REPLAY_SEQUENCE
from .sessions import (
    Conversation,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStoreUnavailableError,
    ConversationSummary,
)
from .workspace_context import WorkspaceContextError


SESSION_COOKIE = "abv2_session"
CSRF_COOKIE = "abv2_csrf_seed"
MAX_JSON_BODY_BYTES = 16_384
SESSION_SECONDS = 8 * 60 * 60
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
STREAM_CONTROL_VERSION = "stream-control-v1"
_CONTEXT_METADATA_FIELDS = frozenset(
    {
        "plan_id",
        "digest",
        "toolset_digest",
        "section_count",
        "history_message_count",
        "included_history_message_count",
        "omitted_history_message_count",
        "history_source_digest",
        "windowing_strategy",
        "estimated_input_tokens",
        "native_context_tokens",
        "operational_context_tokens",
        "input_budget_tokens",
        "compact_at_tokens",
        "compact_target_tokens",
        "output_reserve_tokens",
        "template_reserve_tokens",
        "estimator",
    }
)


class LoginLimiter:
    """Small bounded failed-login window; no attacker-controlled map growth."""

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        per_client: int = 5,
        global_limit: int = 128,
    ) -> None:
        self._window = window_seconds
        self._per_client = per_client
        self._global_limit = global_limit
        self._failures: deque[tuple[float, str]] = deque(maxlen=global_limit)
        self._lock = threading.Lock()

    def allowed(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._failures) >= self._global_limit:
                return False
            return sum(1 for _, seen in self._failures if seen == client) < self._per_client

    def failed(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            self._failures.append((now, client))

    def succeeded(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            self._failures = deque(
                (item for item in self._failures if item[1] != client),
                maxlen=self._global_limit,
            )

    def _purge(self, now: float) -> None:
        cutoff = now - self._window
        while self._failures and self._failures[0][0] <= cutoff:
            self._failures.popleft()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _valid_host_header(value: str | None) -> bool:
    if not value or len(value) > 255 or any(character in value for character in "/\\@\r\n\0"):
        return False
    host = value
    port: str | None = None
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return False
        host = value[1:closing]
        suffix = value[closing + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                return False
            port = suffix[1:]
    elif value.count(":") == 1:
        host, port = value.rsplit(":", 1)
    elif value.count(":") > 1:
        return False
    if port is not None and (not port.isdigit() or not 0 < int(port) <= 65_535):
        return False
    if host.lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host or len(origin) > 512:
        return False
    try:
        parsed = urlsplit(origin)
        parsed_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != request.url.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    default_port = 443 if parsed.scheme == "https" else 80
    origin_port = parsed_port or default_port
    try:
        request_host = host
        if request_host.startswith("["):
            closing = request_host.find("]")
            request_name = request_host[1:closing]
            request_port = (
                int(request_host[closing + 2 :])
                if request_host[closing + 1 :].startswith(":")
                else default_port
            )
        elif request_host.count(":") == 1:
            request_name, raw_port = request_host.rsplit(":", 1)
            request_port = int(raw_port)
        else:
            request_name = request_host
            request_port = default_port
    except (ValueError, IndexError):
        return False
    return parsed.hostname == request_name.lower() and origin_port == request_port


async def _read_json_object(request: Request) -> dict[str, Any]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
            if parsed_length < 0:
                raise HTTPException(400, "invalid content length")
            if parsed_length > MAX_JSON_BODY_BYTES:
                raise HTTPException(413, "request body too large")
        except ValueError as exc:
            raise HTTPException(400, "invalid content length") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_JSON_BODY_BYTES:
            raise HTTPException(413, "request body too large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON body must be an object")
    return value


def _session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def _require_session(request: Request) -> None:
    try:
        request.app.state.sessions.validate(_session_id(request))
    except AuthenticationError as exc:
        raise HTTPException(401, "authentication required") from exc


def _require_csrf(request: Request) -> None:
    header_token = request.headers.get("x-csrf-token")
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if (
        not isinstance(header_token, str)
        or not isinstance(cookie_token, str)
        or not hmac.compare_digest(header_token, cookie_token)
    ):
        raise HTTPException(403, "CSRF validation failed")
    try:
        request.app.state.sessions.validate_csrf(
            _session_id(request), header_token
        )
    except AuthenticationError as exc:
        raise HTTPException(401, "authentication required") from exc
    except CsrfError as exc:
        raise HTTPException(403, "CSRF validation failed") from exc


def _set_session_cookies(
    response: Response,
    *,
    session_id: str,
    csrf_token: str,
    secure: bool,
) -> None:
    common = {
        "max_age": SESSION_SECONDS,
        "httponly": True,
        "secure": secure,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE, session_id, **common)
    response.set_cookie(CSRF_COOKIE, csrf_token, **common)


def _clear_session_cookies(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="strict"
    )
    response.delete_cookie(
        CSRF_COOKIE, path="/", secure=secure, httponly=True, samesite="strict"
    )


def _conversation_state(active_run_id: str | None) -> str:
    return "running" if active_run_id is not None else "idle"


def _summary_response(
    value: Conversation | ConversationSummary,
) -> dict[str, object]:
    if isinstance(value, Conversation):
        turn_count = len(value.turns)
        completed_turn_count = sum(
            turn.status == "completed" for turn in value.turns
        )
    else:
        turn_count = value.turn_count
        completed_turn_count = value.completed_turn_count
    return {
        "session_id": value.conversation_id,
        "title": value.title,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "message_count": turn_count + completed_turn_count,
        "state": _conversation_state(value.active_run_id),
    }


def _conversation_response(value: Conversation) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    for turn in value.turns:
        messages.append(
            {
                "message_id": turn.user_message_id,
                "role": "user",
                "content": turn.user_content,
                "created_at": turn.created_at,
                "turn_id": turn.turn_id,
                "run_id": turn.run_id,
                "turn_status": turn.status,
            }
        )
        if turn.assistant_content is not None:
            messages.append(
                {
                    "message_id": turn.assistant_message_id,
                    "role": "assistant",
                    "content": turn.assistant_content,
                    "created_at": turn.updated_at,
                    "turn_id": turn.turn_id,
                    "run_id": turn.run_id,
                    "turn_status": turn.status,
                }
            )
    return {"session": _summary_response(value), "messages": messages}


async def _runtime_for_agent(request: Request, agent_id: str) -> AgentRuntime:
    """Resolve only an active Agent without disclosing malformed identities."""

    if SAFE_ID.fullmatch(agent_id) is None:
        raise HTTPException(404, "Agent not found")
    manager: AgentRuntimeManager = request.app.state.runtime_manager
    try:
        return await manager.for_agent(agent_id)
    except KeyError as exc:
        raise HTTPException(404, "Agent not found") from exc
    except RuntimeError as exc:
        raise HTTPException(409, "Agent runtime is unavailable") from exc


def _replay_query_integer(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    values = request.query_params.getlist(name)
    if not values:
        return default
    raw = values[0]
    if (
        len(values) != 1
        or not raw
        or len(raw) > len(str(maximum))
        or not raw.isascii()
        or not raw.isdigit()
    ):
        raise HTTPException(400, f"invalid {name}")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise HTTPException(400, f"invalid {name}")
    return value


def _event_cursor(request: Request) -> int:
    raw = request.headers.get("last-event-id", "0")
    if (
        not raw
        or len(raw) > len(str(MAX_REPLAY_SEQUENCE))
        or not raw.isascii()
        or not raw.isdigit()
    ):
        raise HTTPException(400, "invalid event cursor")
    cursor = int(raw)
    if cursor > MAX_REPLAY_SEQUENCE:
        raise HTTPException(400, "invalid event cursor")
    return cursor


def _replay_response(value: DurableReplay) -> dict[str, object]:
    return {
        "identity": {
            "agent_id": value.identity.agent_id,
            "conversation_id": value.identity.conversation_id,
            "turn_id": value.identity.turn_id,
            "run_id": value.identity.run_id,
        },
        "availability": value.availability,
        "oldest_cursor": value.oldest_cursor,
        "latest_cursor": value.latest_cursor,
        "next_cursor": value.next_cursor,
        "has_more": value.has_more,
        "events": [event.to_dict() for event in value.events],
        "gaps": [gap.to_dict() for gap in value.gaps],
        "snapshot": value.snapshot.to_dict(),
    }


def _summary_context_response(value: DurableReplay) -> dict[str, object]:
    """Project only the public run.started metadata from validated replay."""

    try:
        document = value.snapshot.document
    except (AssertionError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("durable context summary is invalid") from exc
    started = document.get("started")
    context_plan = (
        started.get("context_plan") if isinstance(started, dict) else None
    )
    if (
        not isinstance(context_plan, dict)
        or set(context_plan) != _CONTEXT_METADATA_FIELDS
        or any(
            not isinstance(key, str)
            or not isinstance(item, (str, int))
            or isinstance(item, bool)
            for key, item in context_plan.items()
        )
    ):
        raise ValueError("durable context summary is invalid")
    return {
        "identity": {
            "agent_id": value.identity.agent_id,
            "conversation_id": value.identity.conversation_id,
            "turn_id": value.identity.turn_id,
            "run_id": value.identity.run_id,
        },
        "availability": "summary_only",
        "context_plan": dict(context_plan),
        "renderer": {
            "version": None,
            "section_registry_version": None,
            "leading_system_sections_merged": None,
            "leading_system_section_count": None,
            "description": (
                "Exact renderer and section metadata are not retained in the "
                "durable run.started summary."
            ),
        },
        "provider_message_count": None,
        "sections": [],
        "content_exposure": "unavailable",
        "notice": (
            "The exact ContextPlan and section content are no longer resident; "
            "only validated public metadata from durable run.started is available."
        ),
    }


def _sse_frame(
    event: str,
    data: dict[str, object],
    *,
    event_id: int | None = None,
) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {encoded}\n\n"


def _canonical_sse_frame(event: Any) -> str:
    return _sse_frame(event.kind, event.to_dict(), event_id=event.seq)


def _durable_replay_frames(
    value: DurableReplay,
    *,
    after: int,
) -> tuple[str, ...]:
    """Interleave explicit gap controls with canonical events by sequence."""

    ordered: list[tuple[int, int, object]] = []
    for gap in value.gaps:
        effective_from = max(after + 1, gap.from_seq)
        if effective_from <= gap.to_seq:
            ordered.append((effective_from, 0, gap))
    ordered.extend((event.seq, 1, event) for event in value.events)
    ordered.sort(key=lambda item: (item[0], item[1]))

    frames: list[str] = []
    for _sequence, item_type, item in ordered:
        if item_type == 0:
            gap = item
            frames.append(
                _sse_frame(
                    "stream.gap",
                    {
                        "control_version": STREAM_CONTROL_VERSION,
                        "run_id": value.identity.run_id,
                        "from_seq": max(after + 1, gap.from_seq),
                        "to_seq": gap.to_seq,
                        "reason": gap.reason,
                        "resume_cursor": gap.to_seq,
                    },
                    # A retention gap is followed by the authoritative
                    # snapshot at the same cursor.  Only that snapshot may
                    # acknowledge the cursor; otherwise a disconnect between
                    # the two controls could make a reconnect skip it.
                    event_id=(
                        None if gap.reason == "retention" else gap.to_seq
                    ),
                )
            )
        else:
            frames.append(_canonical_sse_frame(item))

    if (
        value.availability == "snapshot_only"
        and after < value.snapshot.through_seq
    ):
        frames.append(
            _sse_frame(
                "stream.snapshot",
                {
                    "control_version": STREAM_CONTROL_VERSION,
                    "run_id": value.identity.run_id,
                    "cursor": value.snapshot.through_seq,
                    "availability": value.availability,
                    "snapshot": value.snapshot.to_dict(),
                },
                event_id=value.snapshot.through_seq,
            )
        )
    return tuple(frames)


def create_app(repository_root: Path | None = None) -> FastAPI:
    root = (repository_root or _repository_root()).resolve(strict=True)
    source = _source_root().resolve(strict=True)
    static_root = Path(__file__).resolve().parent / "static"
    assets = {
        "index": static_root.joinpath("index.html").read_text(encoding="utf-8"),
        "script": static_root.joinpath("app.js").read_text(encoding="utf-8"),
        "style": static_root.joinpath("styles.css").read_text(encoding="utf-8"),
    }
    cookie_secure = os.environ.get("HARNESS_V2_COOKIE_SECURE") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        project_token = ProjectTokenStore(root).load_or_create()
        sessions = SessionService(project_token, ttl_seconds=SESSION_SECONDS)
        agent_registry = AgentRegistry(root)
        await asyncio.to_thread(agent_registry.initialize)
        runtime_manager = AgentRuntimeManager(root, source, agent_registry)
        await runtime_manager.initialize()
        prototype_runtime = await runtime_manager.for_agent(PROTOTYPE_AGENT_ID)
        run_service = prototype_runtime.run_service
        query_engines = prototype_runtime.query_engines
        context_reveal = ContextRevealPolicy(
            root,
            enabled=os.environ.get("HARNESS_V2_CONTEXT_REVEAL") == "1",
        )
        app.state.sessions = sessions
        app.state.agent_registry = agent_registry
        app.state.runtime_manager = runtime_manager
        app.state.run_service = run_service
        app.state.query_engines = query_engines
        app.state.context_reveal = context_reveal
        app.state.commands = CommandBus(query_engines)
        app.state.login_limiter = LoginLimiter()
        app.state.cookie_secure = cookie_secure
        try:
            yield
        finally:
            sessions.revoke_all()
            await runtime_manager.close()
            context_reveal.close()
            agent_registry.close()

    app = FastAPI(
        title="Harness V2 Prototype",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def protect_boundary(request: Request, call_next: Any) -> Response:
        response: Response
        if not _valid_host_header(request.headers.get("host")):
            response = JSONResponse(
                {"detail": "invalid Host header"}, status_code=400
            )
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _same_origin(
            request
        ):
            response = JSONResponse({"detail": "origin rejected"}, status_code=403)
        else:
            content_length = request.headers.get("content-length")
            try:
                parsed_length = int(content_length) if content_length else 0
                invalid_length = parsed_length < 0
                too_large = parsed_length > MAX_JSON_BODY_BYTES
            except ValueError:
                response = JSONResponse(
                    {"detail": "invalid content length"}, status_code=400
                )
            else:
                if invalid_length:
                    response = JSONResponse(
                        {"detail": "invalid content length"}, status_code=400
                    )
                elif too_large:
                    response = JSONResponse(
                        {"detail": "request body too large"}, status_code=413
                    )
                else:
                    response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(assets["index"])

    @app.get("/assets/app.js")
    async def script() -> Response:
        return Response(assets["script"], media_type="text/javascript")

    @app.get("/assets/styles.css")
    async def style() -> Response:
        return Response(assets["style"], media_type="text/css")

    @app.get("/health")
    async def health(request: Request) -> dict[str, object]:
        run_service: RunService = request.app.state.run_service
        ready = (
            run_service.capsule is not None
            and run_service.model_qualification is not None
            and run_service.sandbox_qualification is not None
        )
        return {
            "status": "ok",
            "prototype": True,
            "agent_ready": ready,
            "model": (
                run_service.model_qualification.model
                if run_service.model_qualification is not None
                else None
            ),
            "sandbox": "landlock+seccomp" if ready else None,
        }

    @app.post("/api/auth/login")
    async def login(request: Request) -> Response:
        client = request.client.host if request.client is not None else "unknown"
        limiter: LoginLimiter = request.app.state.login_limiter
        if not limiter.allowed(client):
            raise HTTPException(429, "too many authentication attempts", {"Retry-After": "60"})
        body = await _read_json_object(request)
        token = body.get("token")
        try:
            session = request.app.state.sessions.create(
                token if isinstance(token, str) else None
            )
        except AuthenticationError as exc:
            limiter.failed(client)
            raise HTTPException(401, "authentication failed") from exc
        except SessionCapacityError as exc:
            raise HTTPException(503, "session capacity exhausted") from exc
        limiter.succeeded(client)
        response = JSONResponse(
            {
                "agent_id": PROTOTYPE_AGENT_ID,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at.isoformat(),
            }
        )
        _set_session_cookies(
            response,
            session_id=session.session_id,
            csrf_token=session.csrf_token,
            secure=request.app.state.cookie_secure,
        )
        return response

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, str]:
        csrf_token = request.cookies.get(CSRF_COOKIE)
        try:
            authenticated = request.app.state.sessions.validate_csrf(
                _session_id(request), csrf_token
            )
        except (AuthenticationError, CsrfError) as exc:
            raise HTTPException(401, "authentication required") from exc
        assert csrf_token is not None
        return {
            "agent_id": PROTOTYPE_AGENT_ID,
            "csrf_token": csrf_token,
            "expires_at": authenticated.expires_at.isoformat(),
        }

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, str | bool]:
        """Browser-friendly session probe that does not log an expected 401."""

        csrf_token = request.cookies.get(CSRF_COOKIE)
        try:
            authenticated = request.app.state.sessions.validate_csrf(
                _session_id(request), csrf_token
            )
        except (AuthenticationError, CsrfError):
            return {"authenticated": False}
        assert csrf_token is not None
        return {
            "authenticated": True,
            "agent_id": PROTOTYPE_AGENT_ID,
            "csrf_token": csrf_token,
            "expires_at": authenticated.expires_at.isoformat(),
        }

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        _require_csrf(request)
        request.app.state.sessions.revoke(_session_id(request))
        response = Response(status_code=204)
        _clear_session_cookies(
            response, secure=request.app.state.cookie_secure
        )
        return response

    @app.get("/api/agents")
    async def list_agents(request: Request) -> dict[str, object]:
        _require_session(request)
        registry: AgentRegistry = request.app.state.agent_registry
        records = await asyncio.to_thread(registry.list)
        return {"agents": [record.to_dict() for record in records]}

    @app.post("/api/agents", status_code=201)
    async def create_agent(request: Request) -> dict[str, object]:
        _require_csrf(request)
        body = await _read_json_object(request)
        if set(body) != {"display_name"} or not isinstance(
            body.get("display_name"), str
        ):
            raise HTTPException(400, "display_name is required")
        registry: AgentRegistry = request.app.state.agent_registry
        try:
            record = await asyncio.to_thread(
                registry.create, body["display_name"]
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return record.to_dict()

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str, request: Request) -> dict[str, object]:
        _require_session(request)
        registry: AgentRegistry = request.app.state.agent_registry
        try:
            record = await asyncio.to_thread(registry.get, agent_id)
        except KeyError as exc:
            raise HTTPException(404, "Agent not found") from exc
        return record.to_dict()

    @app.post("/api/agents/{agent_id}/upgrade")
    async def upgrade_agent(agent_id: str, request: Request) -> dict[str, object]:
        _require_csrf(request)
        body = await _read_json_object(request)
        if set(body) - {"display_name"} or (
            "display_name" in body and not isinstance(body["display_name"], str)
        ):
            raise HTTPException(400, "invalid Agent upgrade body")
        if agent_id == PROTOTYPE_AGENT_ID:
            raise HTTPException(409, "the prototype Agent cannot be upgraded")
        registry: AgentRegistry = request.app.state.agent_registry
        manager: AgentRuntimeManager = request.app.state.runtime_manager
        try:
            await manager.begin_drain(agent_id)
            record = await asyncio.to_thread(
                registry.upgrade,
                agent_id,
                display_name=body.get("display_name"),
            )
        except KeyError as exc:
            raise HTTPException(404, "Agent not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            await manager.end_drain(agent_id)
        return record.to_dict()

    @app.delete("/api/agents/{agent_id}", status_code=204)
    async def delete_agent(agent_id: str, request: Request) -> Response:
        _require_csrf(request)
        if agent_id == PROTOTYPE_AGENT_ID:
            raise HTTPException(409, "the prototype Agent cannot be deleted")
        registry: AgentRegistry = request.app.state.agent_registry
        manager: AgentRuntimeManager = request.app.state.runtime_manager
        try:
            await manager.begin_drain(agent_id)
            await asyncio.to_thread(registry.delete, agent_id)
        except KeyError as exc:
            raise HTTPException(404, "Agent not found") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            await manager.end_drain(agent_id)
        return Response(status_code=204)

    @app.get("/api/agents/{agent_id}/sessions")
    async def list_agent_conversations(
        agent_id: str, request: Request
    ) -> dict[str, object]:
        _require_session(request)
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            conversations = await runtime.query_engines.list_conversations()
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return {"sessions": [_summary_response(item) for item in conversations]}

    @app.post("/api/agents/{agent_id}/sessions", status_code=201)
    async def create_agent_conversation(
        agent_id: str, request: Request
    ) -> dict[str, object]:
        _require_csrf(request)
        body = await _read_json_object(request)
        if set(body) - {"title"}:
            raise HTTPException(400, "unsupported conversation field")
        title = body.get("title", "新会话")
        if not isinstance(title, str):
            raise HTTPException(400, "title must be a string")
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            conversation = await runtime.query_engines.create_conversation(title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "conversation capacity exhausted") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return _summary_response(conversation)

    @app.get("/api/agents/{agent_id}/sessions/{conversation_id}")
    async def get_agent_conversation(
        agent_id: str, conversation_id: str, request: Request
    ) -> dict[str, object]:
        _require_session(request)
        if not RUN_ID.fullmatch(conversation_id):
            raise HTTPException(404, "conversation not found")
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            conversation = await runtime.query_engines.get_conversation(
                conversation_id
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(404, "conversation not found") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return _conversation_response(conversation)

    @app.delete(
        "/api/agents/{agent_id}/sessions/{conversation_id}", status_code=204
    )
    async def delete_agent_conversation(
        agent_id: str, conversation_id: str, request: Request
    ) -> Response:
        _require_csrf(request)
        if not RUN_ID.fullmatch(conversation_id):
            raise HTTPException(404, "conversation not found")
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            result = await runtime.query_engines.delete_conversation(
                conversation_id
            )
        except ConversationConflictError as exc:
            raise HTTPException(409, "conversation has an active Run") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        if not result.deleted:
            raise HTTPException(404, "conversation not found")
        return Response(status_code=204)

    @app.post(
        "/api/agents/{agent_id}/sessions/{conversation_id}/runs",
        status_code=202,
    )
    async def start_agent_conversation_run(
        agent_id: str, conversation_id: str, request: Request
    ) -> dict[str, str]:
        _require_csrf(request)
        if not RUN_ID.fullmatch(conversation_id):
            raise HTTPException(404, "conversation not found")
        body = await _read_json_object(request)
        if set(body) != {"message"} or not isinstance(body.get("message"), str):
            raise HTTPException(400, "message is required")
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            record = await runtime.commands.start(
                StartRunCommand(
                    agent_id=agent_id,
                    message=body["message"],
                    conversation_id=conversation_id,
                )
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(404, "conversation not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "conversation has an active Run") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        except WorkspaceContextError as exc:
            raise HTTPException(409, "Agent workspace context is unsafe") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "agent_id": record.agent_id,
            "run_id": record.run_id,
            "session_id": record.conversation_id,
            "events_url": f"/api/agents/{agent_id}/runs/{record.run_id}/events",
        }

    @app.get("/api/agents/{agent_id}/runs/{run_id}/events")
    async def agent_run_events(
        agent_id: str, run_id: str, request: Request
    ) -> StreamingResponse:
        _require_session(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        runtime = await _runtime_for_agent(request, agent_id)
        cursor = _event_cursor(request)
        try:
            await runtime.query_engines.get_run(run_id)
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run stream is unavailable") from exc

        async def stream() -> AsyncIterator[str]:
            try:
                async for envelope in runtime.query_engines.stream(run_id, cursor):
                    if await request.is_disconnected():
                        return
                    if envelope is None:
                        yield ": heartbeat\n\n"
                    else:
                        yield _canonical_sse_frame(envelope)
            except (KeyError, ConversationNotFoundError):
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/agents/{agent_id}/runs/{run_id}/cancel", status_code=202
    )
    async def cancel_agent_run(
        agent_id: str, run_id: str, request: Request
    ) -> dict[str, bool]:
        _require_csrf(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            await runtime.commands.cancel(run_id)
        except (KeyError, QueryEngineOwnershipError) as exc:
            raise HTTPException(404, "run not found") from exc
        return {"accepted": True}

    @app.get("/api/agents/{agent_id}/runs/{run_id}/context")
    async def inspect_agent_run_context(
        agent_id: str, run_id: str, request: Request
    ) -> Response:
        _require_session(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        if request.query_params:
            raise HTTPException(400, "unsupported context query parameter")
        runtime = await _runtime_for_agent(request, agent_id)
        try:
            exact = await runtime.query_engines.inspect_retained_context(run_id)
        except QueryRunNotRetainedError:
            exact = None
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run context is unavailable") from exc
        except QueryContextUnavailableError as exc:
            raise HTTPException(503, "run context is unavailable") from exc
        if exact is not None:
            return JSONResponse(
                exact.to_dict(), headers={"Cache-Control": "no-store"}
            )
        try:
            replay = await runtime.query_engines.replay(run_id, after=0, limit=1)
            summary = _summary_context_response(replay)
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run context is unavailable") from exc
        except (
            ConversationStoreUnavailableError,
            QueryReplayCursorError,
            QueryReplayUnavailableError,
            ValueError,
        ) as exc:
            raise HTTPException(503, "run context is unavailable") from exc
        return JSONResponse(summary, headers={"Cache-Control": "no-store"})

    @app.post("/api/agents/{agent_id}/runs/{run_id}/context/reveal")
    async def reveal_agent_run_context(
        agent_id: str, run_id: str, request: Request
    ) -> Response:
        _require_csrf(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        if request.query_params:
            raise HTTPException(400, "unsupported context query parameter")
        runtime = await _runtime_for_agent(request, agent_id)
        policy: ContextRevealPolicy = request.app.state.context_reveal
        if not policy.enabled:
            raise HTTPException(404, "context reveal is disabled")
        if not policy.authorize(request.headers.get("x-context-operator-token")):
            raise HTTPException(403, "context operator authorization failed")
        try:
            reveal = await runtime.query_engines.reveal_retained_context(run_id)
        except QueryRunNotRetainedError as exc:
            raise HTTPException(409, "run context is no longer retained") from exc
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except (ConversationConflictError, QueryContextUnavailableError) as exc:
            raise HTTPException(503, "run context is unavailable") from exc
        exposed = sum(
            section.exposure == "redacted_excerpt" for section in reveal.sections
        )
        try:
            audit_id = await asyncio.to_thread(
                policy.record,
                agent_id=agent_id,
                run_id=run_id,
                availability="exact",
                exposed_sections=exposed,
            )
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            raise HTTPException(503, "context reveal audit is unavailable") from exc
        return JSONResponse(
            {**reveal.to_dict(), "audit_id": audit_id},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/sessions")
    async def list_conversations(request: Request) -> dict[str, object]:
        _require_session(request)
        try:
            conversations = await request.app.state.query_engines.list_conversations()
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return {
            "sessions": [_summary_response(item) for item in conversations]
        }

    @app.post("/api/sessions", status_code=201)
    async def create_conversation(request: Request) -> dict[str, object]:
        _require_csrf(request)
        body = await _read_json_object(request)
        if set(body) - {"title"}:
            raise HTTPException(400, "unsupported conversation field")
        title = body.get("title", "新会话")
        if not isinstance(title, str):
            raise HTTPException(400, "title must be a string")
        try:
            conversation = await request.app.state.query_engines.create_conversation(
                title
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "conversation capacity exhausted") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return _summary_response(conversation)

    @app.get("/api/sessions/{conversation_id}")
    async def get_conversation(
        conversation_id: str, request: Request
    ) -> dict[str, object]:
        _require_session(request)
        if not RUN_ID.fullmatch(conversation_id):
            raise HTTPException(404, "conversation not found")
        try:
            conversation = await request.app.state.query_engines.get_conversation(
                conversation_id
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(404, "conversation not found") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return _conversation_response(conversation)

    @app.delete("/api/sessions/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: str, request: Request
    ) -> Response:
        _require_csrf(request)
        if not RUN_ID.fullmatch(conversation_id):
            raise HTTPException(404, "conversation not found")
        try:
            result = await request.app.state.query_engines.delete_conversation(
                conversation_id
            )
        except ConversationConflictError as exc:
            raise HTTPException(409, "conversation has an active Run") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        if not result.deleted:
            raise HTTPException(404, "conversation not found")
        return Response(status_code=204)

    @app.post("/api/sessions/{conversation_id}/runs", status_code=202)
    async def start_conversation_run(
        conversation_id: str, request: Request
    ) -> dict[str, str]:
        _require_csrf(request)
        if not RUN_ID.fullmatch(conversation_id):
            raise HTTPException(404, "conversation not found")
        body = await _read_json_object(request)
        if set(body) != {"message"}:
            raise HTTPException(400, "message is required")
        message = body.get("message")
        if not isinstance(message, str):
            raise HTTPException(400, "message must be a string")
        try:
            record = await request.app.state.commands.start(
                StartRunCommand(
                    agent_id=PROTOTYPE_AGENT_ID,
                    message=message,
                    conversation_id=conversation_id,
                )
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(404, "conversation not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "conversation has an active Run") from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        except WorkspaceContextError as exc:
            raise HTTPException(409, "Agent workspace context is unsafe") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "run_id": record.run_id,
            "session_id": record.conversation_id,
            "events_url": f"/api/runs/{record.run_id}/events",
        }

    @app.post("/api/runs", status_code=202)
    async def start_run(request: Request) -> dict[str, str]:
        _require_csrf(request)
        body = await _read_json_object(request)
        message = body.get("message")
        if not isinstance(message, str):
            raise HTTPException(400, "message must be a string")
        try:
            record = await request.app.state.commands.start(
                StartRunCommand(agent_id=PROTOTYPE_AGENT_ID, message=message)
            )
        except WorkspaceContextError as exc:
            raise HTTPException(409, "Agent workspace context is unsafe") from exc
        except (ValueError, ConversationConflictError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except ConversationStoreUnavailableError as exc:
            raise HTTPException(503, "conversation state is unavailable") from exc
        return {
            "agent_id": record.agent_id,
            "conversation_id": record.conversation_id,
            "session_id": record.conversation_id,
            "turn_id": record.turn_id,
            "run_id": record.run_id,
            "events_url": f"/api/runs/{record.run_id}/events",
        }

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        _require_session(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        cursor = _event_cursor(request)
        replay_page: DurableReplay | None = None
        try:
            await request.app.state.query_engines.get_run(run_id)
            stream_source = "live"
        except QueryEngineOwnershipError:
            stream_source = "durable"
            try:
                replay_page = await request.app.state.query_engines.replay(
                    run_id,
                    after=cursor,
                    limit=MAX_REPLAY_PAGE,
                )
            except QueryEngineOwnershipError as exc:
                raise HTTPException(404, "run not found") from exc
            except ConversationConflictError as exc:
                raise HTTPException(409, "run replay is unavailable") from exc
            except QueryReplayCursorError as exc:
                raise HTTPException(
                    416, "replay cursor is outside the durable Run"
                ) from exc
            except (
                ConversationStoreUnavailableError,
                QueryReplayUnavailableError,
            ) as exc:
                raise HTTPException(
                    503, "durable replay is unavailable"
                ) from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run stream is unavailable") from exc

        async def stream() -> AsyncIterator[str]:
            if stream_source == "live":
                try:
                    async for envelope in request.app.state.query_engines.stream(
                        run_id, cursor
                    ):
                        if await request.is_disconnected():
                            return
                        if envelope is None:
                            yield ": heartbeat\n\n"
                            continue
                        yield _canonical_sse_frame(envelope)
                except (KeyError, ConversationNotFoundError):
                    # The source was fixed to live during preflight.  A
                    # retention/delete race ends this stream; it never switches
                    # to replay and therefore cannot double-publish events.
                    return
                return

            assert replay_page is not None
            expected_identity = replay_page.identity
            page = replay_page
            page_after = cursor
            while True:
                try:
                    handle = (
                        await request.app.state.query_engines.resolve_run_identity(
                            run_id
                        )
                    )
                except (
                    KeyError,
                    ConversationNotFoundError,
                    ConversationConflictError,
                    ConversationStoreUnavailableError,
                    QueryReplayUnavailableError,
                ):
                    # Revalidate immediately before publishing a prefetched
                    # durable page so a delete race does not leak stale state.
                    return
                if (
                    handle.agent_id != expected_identity.agent_id
                    or handle.conversation_id
                    != expected_identity.conversation_id
                    or handle.turn_id != expected_identity.turn_id
                    or handle.run_id != expected_identity.run_id
                ):
                    return

                for frame in _durable_replay_frames(page, after=page_after):
                    if await request.is_disconnected():
                        return
                    yield frame
                if not page.has_more:
                    return
                next_cursor = page.next_cursor
                try:
                    page = await request.app.state.query_engines.replay(
                        run_id,
                        after=next_cursor,
                        limit=MAX_REPLAY_PAGE,
                    )
                except (
                    KeyError,
                    ConversationNotFoundError,
                    ConversationConflictError,
                    ConversationStoreUnavailableError,
                    QueryReplayCursorError,
                    QueryReplayUnavailableError,
                ):
                    return
                if page.identity != expected_identity:
                    return
                page_after = next_cursor

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/context")
    async def inspect_run_context(run_id: str, request: Request) -> Response:
        """Return a no-store, content-withholding operator context view."""

        _require_session(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        if request.query_params:
            raise HTTPException(400, "unsupported context query parameter")
        try:
            exact = await request.app.state.query_engines.inspect_retained_context(
                run_id
            )
        except QueryRunNotRetainedError:
            exact = None
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run context is unavailable") from exc
        except QueryContextUnavailableError as exc:
            raise HTTPException(503, "run context is unavailable") from exc
        if exact is not None:
            return JSONResponse(
                exact.to_dict(), headers={"Cache-Control": "no-store"}
            )

        try:
            replay = await request.app.state.query_engines.replay(
                run_id,
                after=0,
                limit=1,
            )
            summary = _summary_context_response(replay)
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run context is unavailable") from exc
        except (
            ConversationStoreUnavailableError,
            QueryReplayCursorError,
            QueryReplayUnavailableError,
            ValueError,
        ) as exc:
            raise HTTPException(503, "run context is unavailable") from exc
        return JSONResponse(summary, headers={"Cache-Control": "no-store"})

    @app.post("/api/runs/{run_id}/context/reveal")
    async def reveal_run_context(run_id: str, request: Request) -> Response:
        """Independently authorized, audited and redacted diagnostic excerpts."""

        _require_csrf(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        if request.query_params:
            raise HTTPException(400, "unsupported context query parameter")
        policy: ContextRevealPolicy = request.app.state.context_reveal
        if not policy.enabled:
            raise HTTPException(404, "context reveal is disabled")
        if not policy.authorize(request.headers.get("x-context-operator-token")):
            raise HTTPException(403, "context operator authorization failed")
        try:
            reveal = await request.app.state.query_engines.reveal_retained_context(
                run_id
            )
        except QueryRunNotRetainedError as exc:
            raise HTTPException(409, "run context is no longer retained") from exc
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except (ConversationConflictError, QueryContextUnavailableError) as exc:
            raise HTTPException(503, "run context is unavailable") from exc
        exposed = sum(
            section.exposure == "redacted_excerpt" for section in reveal.sections
        )
        try:
            audit_id = await asyncio.to_thread(
                policy.record,
                agent_id=PROTOTYPE_AGENT_ID,
                run_id=run_id,
                availability="exact",
                exposed_sections=exposed,
            )
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            raise HTTPException(503, "context reveal audit is unavailable") from exc
        return JSONResponse(
            {**reveal.to_dict(), "audit_id": audit_id},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/runs/{run_id}/replay")
    async def replay_run(run_id: str, request: Request) -> dict[str, object]:
        """Return one bounded page from the durable semantic Run journal."""

        _require_session(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        if set(request.query_params.keys()) - {"after", "limit"}:
            raise HTTPException(400, "unsupported replay query parameter")
        after = _replay_query_integer(
            request,
            "after",
            default=0,
            minimum=0,
            maximum=MAX_REPLAY_SEQUENCE,
        )
        limit = _replay_query_integer(
            request,
            "limit",
            default=MAX_REPLAY_PAGE,
            minimum=1,
            maximum=MAX_REPLAY_PAGE,
        )
        try:
            value = await request.app.state.query_engines.replay(
                run_id,
                after=after,
                limit=limit,
            )
        except QueryEngineOwnershipError as exc:
            raise HTTPException(404, "run not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(409, "run replay is unavailable") from exc
        except QueryReplayCursorError as exc:
            raise HTTPException(
                416, "replay cursor is outside the durable Run"
            ) from exc
        except (
            ConversationStoreUnavailableError,
            QueryReplayUnavailableError,
        ) as exc:
            raise HTTPException(503, "durable replay is unavailable") from exc
        return _replay_response(value)

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, request: Request) -> dict[str, bool]:
        _require_csrf(request)
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "run not found")
        try:
            await request.app.state.commands.cancel(run_id)
        except KeyError as exc:
            raise HTTPException(404, "run not found") from exc
        return {"accepted": True}

    return app


app = create_app()


def main() -> None:
    host = os.environ.get("HARNESS_V2_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("HARNESS_V2_PORT", "20815"))
    except ValueError as exc:
        raise SystemExit("HARNESS_V2_PORT must be an integer") from exc
    if not 0 < port <= 65_535:
        raise SystemExit("HARNESS_V2_PORT is out of range")
    uvicorn.run(
        "agent_builder_v2.web:app",
        host=host,
        port=port,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        workers=1,
        backlog=64,
        limit_concurrency=64,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=5,
        h11_max_incomplete_event_size=16_384,
        log_level="warning",
    )


if __name__ == "__main__":
    main()

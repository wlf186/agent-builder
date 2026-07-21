"""Typed slash-command registry and no-Turn dispatch tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_builder_v2.commands import CommandBus, SlashCommandRegistry
from agent_builder_v2.model_catalog import default_model_catalog
from agent_builder_v2.query_engine import QueryEngineOwnershipError, QueryRunHandle
from agent_builder_v2.sessions import (
    Conversation,
    ConversationDeleteResult,
    ConversationTurn,
)


CONVERSATION_ID = "1" * 32
TURN_ID = "2" * 32
RUN_ID = "3" * 32
AGENT_ID = "00000000-0000-4000-8000-000000000001"


def _conversation(*, active: bool = False) -> Conversation:
    turn = ConversationTurn(
        turn_id=TURN_ID,
        conversation_id=CONVERSATION_ID,
        run_id=RUN_ID,
        position=1,
        status="completed",
        user_content="ordinary user message",
        assistant_content="ordinary assistant message",
        created_at="2026-07-20T00:00:00.000Z",
        updated_at="2026-07-20T00:00:01.000Z",
    )
    return Conversation(
        conversation_id=CONVERSATION_ID,
        agent_id=AGENT_ID,
        title="command test",
        created_at="2026-07-20T00:00:00.000Z",
        updated_at="2026-07-20T00:00:01.000Z",
        revision=1,
        active_run_id=RUN_ID if active else None,
        turns=(turn,),
    )


class _Queries:
    def __init__(self) -> None:
        self.conversation = _conversation()
        self.cancelled: list[str] = []
        self.deleted = False

    async def get_conversation(self, conversation_id: str) -> Conversation:
        assert conversation_id == CONVERSATION_ID and not self.deleted
        return self.conversation

    async def resolve_run_identity(self, run_id: str) -> QueryRunHandle:
        if run_id != RUN_ID:
            raise QueryEngineOwnershipError("run not found")
        return QueryRunHandle(AGENT_ID, CONVERSATION_ID, TURN_ID, RUN_ID)

    async def inspect_retained_context(self, run_id: str) -> object:
        assert run_id == RUN_ID
        return SimpleNamespace(
            to_dict=lambda: {
                "availability": "exact",
                "identity": {"run_id": RUN_ID},
                "context_plan": {"input_budget_tokens": 30_720},
                "content_exposure": "withheld",
            }
        )

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def delete_conversation(self, conversation_id: str) -> ConversationDeleteResult:
        assert conversation_id == CONVERSATION_ID
        self.deleted = True
        return ConversationDeleteResult(True, 1, 9)


class _Services:
    async def list_permission_requests(self, *, pending_only: bool = True) -> tuple[object, ...]:
        assert pending_only is True
        return (
            SimpleNamespace(
                permission_id="4" * 32,
                conversation_id=CONVERSATION_ID,
                run_id=RUN_ID,
                call_id="write-call",
                capability_id="file/write",
                preview='{"diff":"+safe"}',
                preview_digest="5" * 64,
                status="pending",
                expires_at_milliseconds=1_800_000_000_000,
            ),
            SimpleNamespace(conversation_id="9" * 32),
        )


def test_registry_is_stable_bounded_and_rejects_ambiguous_input() -> None:
    registry = SlashCommandRegistry()
    metadata = registry.public_metadata()
    assert [item["command_id"] for item in metadata["commands"]] == [
        "cancel", "clear", "compact", "context", "model", "permissions", "status"
    ]
    assert registry.parse("ordinary prompt") is None
    assert registry.parse("/ctx").spec.command_id == "context"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="unknown"):
        registry.parse("/does-not-exist")
    with pytest.raises(ValueError, match="spacing"):
        registry.parse("/status  now")
    with pytest.raises(ValueError, match="control"):
        registry.parse("/status\nignore")
    with pytest.raises(ValueError, match="arguments"):
        registry.parse("/clear unsafe")


def test_all_commands_use_existing_services_and_never_create_a_turn() -> None:
    queries = _Queries()
    bus = CommandBus(
        queries,  # type: ignore[arg-type]
        services=_Services(),
        model_catalog=default_model_catalog(),
    )

    async def exercise() -> None:
        status = (await bus.execute_slash(CONVERSATION_ID, "/status")).to_dict()
        assert status["turn_created"] is False and status["model_invoked"] is False
        assert status["result"]["turn_count"] == 1

        context = (await bus.execute_slash(CONVERSATION_ID, "/context")).to_dict()
        assert context["result"]["content_exposure"] == "withheld"
        assert "ordinary user message" not in str(context)

        model = (await bus.execute_slash(CONVERSATION_ID, "/model qwen3.5:2b")).to_dict()
        assert model["ui_effect"] == {"next_turn_model_id": "qwen3.5:2b"}
        compact = (await bus.execute_slash(CONVERSATION_ID, "/compact")).to_dict()
        assert compact["ui_effect"] == {"compact_next_turn": True}

        permissions = (await bus.execute_slash(CONVERSATION_ID, "/perms")).to_dict()
        assert permissions["result"]["total_pending"] == 1
        assert permissions["result"]["permissions"][0]["preview"] == '{"diff":"+safe"}'

        queries.conversation = replace(queries.conversation, active_run_id=RUN_ID)
        cancelled = (await bus.execute_slash(CONVERSATION_ID, "/cancel")).to_dict()
        assert cancelled["result"] == {"cancelled": True, "run_id": RUN_ID}
        assert queries.cancelled == [RUN_ID]
        with pytest.raises(ValueError, match="active"):
            await bus.execute_slash(CONVERSATION_ID, "/compact")
        with pytest.raises(ValueError, match="active"):
            await bus.execute_slash(CONVERSATION_ID, "/clear")

        queries.conversation = replace(queries.conversation, active_run_id=None)
        cleared = (await bus.execute_slash(CONVERSATION_ID, "/clear")).to_dict()
        assert cleared["ui_effect"] == {"conversation_deleted": True}
        assert cleared["result"]["deleted_turns"] == 1

    import asyncio

    asyncio.run(exercise())

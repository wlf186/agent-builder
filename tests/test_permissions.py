"""Capability policy, permission and at-most-once dispatch tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_builder_v2.contracts import EventEnvelope
from agent_builder_v2.permissions import (
    CapabilityBroker,
    CapabilityOutcomeUnknownError,
    CapabilityPolicy,
    CapabilityRequest,
)
from agent_builder_v2.sessions import (
    ConversationConflictError,
    ConversationStore,
    TurnNotFoundError,
)
from agent_builder_v2.tools import prototype_effective_toolset


AGENT_ID = "00000000-0000-4000-8000-000000000001"
CONVERSATION_ID = "1" * 32
TURN_ID = "2" * 32
RUN_ID = "3" * 32
CALL_ID = "call-1"
NOW = 1_800_000_000_000
PLAN_DIGEST = "a" * 64


def _store(tmp_path: Path) -> ConversationStore:
    root = tmp_path / "data" / "agents" / AGENT_ID
    root.mkdir(parents=True, mode=0o700)
    store = ConversationStore(root / "state.sqlite", AGENT_ID)
    store.create_conversation(conversation_id=CONVERSATION_ID)
    started = EventEnvelope(
        event_id="4" * 32,
        agent_id=AGENT_ID,
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
        run_id=RUN_ID,
        seq=1,
        occurred_at="2026-07-20T00:00:00.000Z",
        kind="run.started",
        durability="durable",
        payload={
            "prototype": True,
            "model": "qwen3.5:2b",
            "visible_tools": ["builtin/echo"],
            "sandbox": "harness-v2-worker-v1",
            "context_plan": {
                "plan_id": f"context-{PLAN_DIGEST[:24]}",
                "digest": PLAN_DIGEST,
                "toolset_digest": prototype_effective_toolset().toolset_digest,
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
            },
        },
    )
    store.begin_turn(
        CONVERSATION_ID,
        turn_id=TURN_ID,
        run_id=RUN_ID,
        user_content="permission test",
        expected_revision=0,
        started_event=started,
    )
    return store


def _policy(**changes: object) -> CapabilityPolicy:
    values: dict[str, object] = {
        "revision": "permission-v1",
        "ask": ("builtin/test-mutation",),
    }
    values.update(changes)
    return CapabilityPolicy(**values)  # type: ignore[arg-type]


def _request(
    policy: CapabilityPolicy,
    *,
    generation: int = 1,
    arguments: object | None = None,
) -> CapabilityRequest:
    effective = prototype_effective_toolset()
    return CapabilityRequest.create(
        agent_id=AGENT_ID,
        capsule_generation=generation,
        conversation_id=CONVERSATION_ID,
        run_id=RUN_ID,
        call_id=CALL_ID,
        capability_id="builtin/test-mutation",
        toolset_digest=effective.toolset_digest,
        policy_digest=policy.digest,
        arguments=(
            {"path": "workspace/example.txt", "content": "hello"}
            if arguments is None
            else arguments
        ),
        preview="Write 5 bytes to workspace/example.txt",
        expires_at_milliseconds=NOW + 60_000,
        now_milliseconds=NOW,
    )


def _broker(
    store: ConversationStore,
    policy: CapabilityPolicy,
    generation: list[int] | None = None,
) -> CapabilityBroker:
    current_generation = generation or [1]
    return CapabilityBroker(
        store,
        generation_provider=lambda: current_generation[0],
        toolset_digest_provider=lambda: prototype_effective_toolset().toolset_digest,
        policy=policy,
    )


def test_broker_accepts_only_enumerated_trusted_toolset_projections(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    policy = _policy()
    current = prototype_effective_toolset().toolset_digest
    broker = CapabilityBroker(
        store,
        generation_provider=lambda: 1,
        toolset_digest_provider=lambda: (current, "f" * 64),
        policy=policy,
    )
    try:
        permission = broker.request(
            _request(policy), turn_id=TURN_ID, interactive=True
        )
        assert permission.toolset_digest == current
    finally:
        store.close()


class _Executor:
    executor_kind = "test-sandbox-executor"
    identity_digest = "e" * 64

    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self, request: CapabilityRequest, cancelled: Callable[[], bool]
    ) -> str:
        if cancelled():
            raise RuntimeError("executor observed cancellation")
        self.calls += 1
        assert request.context.arguments_digest
        return "written"


class _UnknownExecutor(_Executor):
    def execute(
        self, request: CapabilityRequest, cancelled: Callable[[], bool]
    ) -> str:
        self.calls += 1
        raise CapabilityOutcomeUnknownError("commit acknowledgement was lost")


def test_policy_has_deny_precedence_and_noninteractive_ask_denies() -> None:
    policy = _policy(
        allow=("builtin/test-mutation",),
        deny=("builtin/test-mutation",),
    )
    assert policy.resolve("builtin/test-mutation", interactive=True) == "deny"
    assert _policy().resolve("builtin/test-mutation", interactive=False) == "deny"
    assert _policy(default="deny").resolve("unknown/capability", interactive=True) == "deny"


def test_pending_approval_is_bound_and_dispatches_at_most_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        policy = _policy()
        broker = _broker(store, policy)
        request = _request(policy)
        permission = broker.request(request, turn_id=TURN_ID, interactive=True)
        assert permission.status == "pending"
        assert permission.preview == "Write 5 bytes to workspace/example.txt"
        assert broker.execute(
            permission.permission_id,
            request,
            _Executor(),
            turn_id=TURN_ID,
            now_milliseconds=NOW,
        ).status == "pending"

        approved = broker.resolve(
            permission.permission_id, "approve", now_milliseconds=NOW
        )
        assert approved.status == "approved"
        executor = _Executor()
        first = broker.execute(
            permission.permission_id,
            request,
            executor,
            turn_id=TURN_ID,
            now_milliseconds=NOW,
        )
        second = broker.execute(
            permission.permission_id,
            request,
            executor,
            turn_id=TURN_ID,
            now_milliseconds=NOW,
        )
        assert first.status == "succeeded"
        assert first.result == "written"
        assert second.status == "succeeded"
        assert second.result is None
        assert executor.calls == 1
    finally:
        store.close()


def test_unprovable_executor_commit_is_durable_and_never_replayed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        policy = _policy()
        broker = _broker(store, policy)
        request = _request(policy)
        permission = broker.request(request, turn_id=TURN_ID, interactive=True)
        broker.resolve(permission.permission_id, "approve", now_milliseconds=NOW)
        executor = _UnknownExecutor()
        first = broker.execute(
            permission.permission_id,
            request,
            executor,
            turn_id=TURN_ID,
            now_milliseconds=NOW,
        )
        assert first.status == "outcome_unknown"
        assert first.operation is not None
        assert first.operation.status == "outcome_unknown"
        second = broker.execute(
            permission.permission_id,
            request,
            executor,
            turn_id=TURN_ID,
            now_milliseconds=NOW,
        )
        assert second.status == "outcome_unknown"
        assert second.operation == first.operation
        assert executor.calls == 1
    finally:
        store.close()


def test_expiry_stale_generation_and_binding_tamper_have_zero_side_effect(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        policy = _policy()
        generation = [1]
        broker = _broker(store, policy, generation)
        request = _request(policy)
        permission = broker.request(request, turn_id=TURN_ID, interactive=True)
        expired = broker.resolve(
            permission.permission_id,
            "approve",
            now_milliseconds=NOW + 60_000,
        )
        assert expired.status == "expired"
        executor = _Executor()
        assert broker.execute(
            permission.permission_id,
            request,
            executor,
            turn_id=TURN_ID,
            now_milliseconds=NOW + 60_000,
        ).status == "expired"
        assert executor.calls == 0

        request2 = CapabilityRequest.create(
            agent_id=AGENT_ID,
            capsule_generation=1,
            conversation_id=CONVERSATION_ID,
            run_id=RUN_ID,
            call_id="call-2",
            capability_id="builtin/test-mutation",
            toolset_digest=prototype_effective_toolset().toolset_digest,
            policy_digest=policy.digest,
            arguments={"path": "workspace/other"},
            preview="Write another file",
            expires_at_milliseconds=NOW + 60_000,
            now_milliseconds=NOW,
        )
        permission2 = broker.request(request2, turn_id=TURN_ID, interactive=True)
        broker.resolve(permission2.permission_id, "approve", now_milliseconds=NOW)
        generation[0] = 2
        with pytest.raises(ConversationConflictError, match="no longer valid"):
            broker.execute(
                permission2.permission_id,
                request2,
                executor,
                turn_id=TURN_ID,
                now_milliseconds=NOW,
            )
        assert executor.calls == 0
    finally:
        store.close()


def test_approve_deny_race_has_one_resolution_and_restart_cancels_pending(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        policy = _policy()
        broker = _broker(store, policy)
        request = _request(policy)
        permission = broker.request(request, turn_id=TURN_ID, interactive=True)

        def resolve(decision: str) -> str:
            try:
                return broker.resolve(
                    permission.permission_id,
                    decision,  # type: ignore[arg-type]
                    now_milliseconds=NOW,
                ).status
            except ConversationConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(resolve, ("approve", "deny")))
        assert outcomes in [["approved", "conflict"], ["conflict", "denied"]]

        request2 = CapabilityRequest.create(
            agent_id=AGENT_ID,
            capsule_generation=1,
            conversation_id=CONVERSATION_ID,
            run_id=RUN_ID,
            call_id="call-restart",
            capability_id="builtin/test-mutation",
            toolset_digest=prototype_effective_toolset().toolset_digest,
            policy_digest=policy.digest,
            arguments={"value": 1},
            preview="Mutate one value",
            expires_at_milliseconds=NOW + 60_000,
            now_milliseconds=NOW,
        )
        pending = broker.request(request2, turn_id=TURN_ID, interactive=True)
        store.recover_running_as_interrupted()
        assert store.get_permission_request(pending.permission_id).status == "cancelled"
        result = store.delete_conversation(CONVERSATION_ID)
        assert result.deleted is True
        with pytest.raises(TurnNotFoundError):
            store.get_permission_request(pending.permission_id)
        with pytest.raises(TurnNotFoundError):
            store.capability_audit_events(RUN_ID)
    finally:
        store.close()


def test_cancellation_after_dispatch_has_zero_executor_side_effect(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        policy = _policy()
        broker = _broker(store, policy)
        request = _request(policy)
        permission = broker.request(request, turn_id=TURN_ID, interactive=True)
        broker.resolve(permission.permission_id, "approve", now_milliseconds=NOW)
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        executor = _Executor()
        result = broker.execute(
            permission.permission_id,
            request,
            executor,
            turn_id=TURN_ID,
            cancelled=cancelled,
            now_milliseconds=NOW,
        )
        assert result.status == "cancelled"
        assert result.operation is not None
        assert result.operation.status == "cancelled"
        assert executor.calls == 0
    finally:
        store.close()


def test_capability_arguments_reject_deep_or_cyclic_json() -> None:
    policy = _policy()
    deep: object = "leaf"
    for _ in range(20):
        deep = {"next": deep}
    with pytest.raises(ValueError, match="invalid capability arguments"):
        _request(policy, arguments=deep)

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="invalid capability arguments"):
        _request(policy, arguments=cyclic)


def test_policy_revision_and_expiry_invalidate_pending_without_execution(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        policy = _policy()
        request = _request(policy)
        permission = _broker(store, policy).request(
            request, turn_id=TURN_ID, interactive=True
        )
        revised = CapabilityPolicy(
            revision="permission-v2",
            ask=("builtin/test-mutation",),
        )
        cancelled = _broker(store, revised).resolve(
            permission.permission_id, "approve", now_milliseconds=NOW
        )
        assert cancelled.status == "cancelled"

        request2 = CapabilityRequest.create(
            agent_id=AGENT_ID,
            capsule_generation=1,
            conversation_id=CONVERSATION_ID,
            run_id=RUN_ID,
            call_id="call-expire",
            capability_id="builtin/test-mutation",
            toolset_digest=prototype_effective_toolset().toolset_digest,
            policy_digest=policy.digest,
            arguments={"value": 2},
            preview="Mutate value two",
            expires_at_milliseconds=NOW + 1_000,
            now_milliseconds=NOW,
        )
        pending = _broker(store, policy).request(
            request2, turn_id=TURN_ID, interactive=True
        )
        assert store.expire_pending_permissions(NOW + 1_000) == 1
        assert store.get_permission_request(pending.permission_id).status == "expired"
        executor = _Executor()
        assert _broker(store, policy).execute(
            pending.permission_id,
            request2,
            executor,
            turn_id=TURN_ID,
            now_milliseconds=NOW + 1_000,
        ).status == "expired"
        assert executor.calls == 0
    finally:
        store.close()


def test_capability_audit_replays_after_restart_without_redispatch(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    database_path = store.database_path
    policy = _policy()
    broker = _broker(store, policy)
    request = _request(policy)
    permission = broker.request(request, turn_id=TURN_ID, interactive=True)
    broker.resolve(permission.permission_id, "approve", now_milliseconds=NOW)
    executor = _Executor()
    assert broker.execute(
        permission.permission_id,
        request,
        executor,
        turn_id=TURN_ID,
        now_milliseconds=NOW,
    ).status == "succeeded"
    before = store.capability_audit_events(RUN_ID)
    assert [event.kind for event in before] == [
        "permission.requested",
        "permission.resolved",
        "operation.intent",
        "operation.dispatched",
        "operation.outcome",
    ]
    assert [event.status for event in before] == [
        "ask",
        "approved",
        "intent",
        "dispatched",
        "succeeded",
    ]
    store.close()

    restored = ConversationStore(database_path, AGENT_ID)
    try:
        after = restored.capability_audit_events(RUN_ID)
        assert [event.to_dict() for event in after] == [
            event.to_dict() for event in before
        ]
        assert restored.capability_audit_events(
            RUN_ID, after_seq=after[1].audit_seq, limit=2
        ) == after[2:4]
        assert executor.calls == 1
    finally:
        restored.close()

"""Trusted capability policy and one-shot permission broker substrate."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Callable, Literal, Protocol

from .contracts import new_id
from .sessions import (
    ConversationConflictError,
    ConversationStore,
    OperationRecord,
    PermissionRecord,
)
from .tools import ToolUseContext


MIN_PERMISSION_TTL_MILLISECONDS = 1_000
MAX_PERMISSION_TTL_MILLISECONDS = 5 * 60 * 1_000
MAX_CAPABILITY_ARGUMENT_BYTES = 16 * 1024
MAX_CAPABILITY_PREVIEW_BYTES = 4 * 1024
MAX_CAPABILITY_JSON_DEPTH = 16
MAX_CAPABILITY_JSON_NODES = 2_048
MAX_CAPABILITY_COLLECTION_ITEMS = 256
MAX_CAPABILITY_STRING_BYTES = 8_192
_CAPABILITY_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_REVISION = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

PolicyDecision = Literal["allow", "ask", "deny"]


class CapabilityOutcomeUnknownError(RuntimeError):
    """An irreversible action may have committed and must not be replayed."""


def _canonical_bytes(value: object, maximum: int, field: str) -> bytes:
    pending: list[tuple[object, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_CAPABILITY_JSON_NODES or depth > MAX_CAPABILITY_JSON_DEPTH:
            raise ValueError(f"invalid {field}")
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen_containers or len(current) > MAX_CAPABILITY_COLLECTION_ITEMS:
                raise ValueError(f"invalid {field}")
            seen_containers.add(identity)
            for key, child in current.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 128:
                    raise ValueError(f"invalid {field}")
                pending.append((child, depth + 1))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen_containers or len(current) > MAX_CAPABILITY_COLLECTION_ITEMS:
                raise ValueError(f"invalid {field}")
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            if len(current.encode("utf-8")) > MAX_CAPABILITY_STRING_BYTES:
                raise ValueError(f"invalid {field}")
        elif current is None or isinstance(current, (bool, int, float)):
            continue
        else:
            raise ValueError(f"invalid {field}")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not 2 <= len(encoded) <= maximum:
        raise ValueError(f"{field} exceeds its byte limit")
    return encoded


def _digest(domain: bytes, payload: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    revision: str
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    default: PolicyDecision = "deny"

    def __post_init__(self) -> None:
        groups = (self.allow, self.ask, self.deny)
        if (
            _REVISION.fullmatch(self.revision) is None
            or self.default not in {"allow", "ask", "deny"}
            or any(tuple(sorted(set(group))) != group for group in groups)
            or any(
                _CAPABILITY_ID.fullmatch(item) is None
                for group in groups
                for item in group
            )
        ):
            raise ValueError("invalid capability policy")

    @property
    def digest(self) -> str:
        payload = _canonical_bytes(
            {
                "revision": self.revision,
                "allow": list(self.allow),
                "ask": list(self.ask),
                "deny": list(self.deny),
                "default": self.default,
                "deny_precedence": True,
            },
            16 * 1024,
            "capability policy",
        )
        return _digest(b"agent-builder-capability-policy-v1", payload)

    def resolve(self, capability_id: str, *, interactive: bool) -> PolicyDecision:
        if _CAPABILITY_ID.fullmatch(capability_id) is None:
            raise ValueError("invalid capability identity")
        if capability_id in self.deny:
            return "deny"
        decision: PolicyDecision
        if capability_id in self.allow:
            decision = "allow"
        elif capability_id in self.ask:
            decision = "ask"
        else:
            decision = self.default
        return "deny" if decision == "ask" and not interactive else decision


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    context: ToolUseContext
    arguments_json: str
    preview: str

    @classmethod
    def create(
        cls,
        *,
        agent_id: str,
        capsule_generation: int,
        conversation_id: str,
        run_id: str,
        call_id: str,
        capability_id: str,
        toolset_digest: str,
        policy_digest: str,
        arguments: object,
        preview: str,
        expires_at_milliseconds: int,
        now_milliseconds: int | None = None,
    ) -> CapabilityRequest:
        arguments_bytes = _canonical_bytes(
            arguments, MAX_CAPABILITY_ARGUMENT_BYTES, "capability arguments"
        )
        if (
            not isinstance(preview, str)
            or not preview.strip()
            or "\x00" in preview
            or len(preview.encode("utf-8")) > MAX_CAPABILITY_PREVIEW_BYTES
        ):
            raise ValueError("invalid capability preview")
        now_value = int(time.time() * 1000) if now_milliseconds is None else now_milliseconds
        if (
            not isinstance(now_value, int)
            or isinstance(now_value, bool)
            or not MIN_PERMISSION_TTL_MILLISECONDS
            <= expires_at_milliseconds - now_value
            <= MAX_PERMISSION_TTL_MILLISECONDS
        ):
            raise ValueError("invalid capability expiry")
        context = ToolUseContext(
            agent_id=agent_id,
            capsule_generation=capsule_generation,
            conversation_id=conversation_id,
            run_id=run_id,
            call_id=call_id,
            tool_id=capability_id,
            toolset_digest=toolset_digest,
            policy_digest=policy_digest,
            arguments_digest=_digest(
                b"agent-builder-capability-arguments-v1", arguments_bytes
            ),
            preview_digest=_digest(
                b"agent-builder-capability-preview-v1", preview.encode("utf-8")
            ),
            expires_at_milliseconds=expires_at_milliseconds,
        )
        return cls(context, arguments_bytes.decode("utf-8"), preview)


@dataclass(frozen=True, slots=True)
class CapabilityExecutionResult:
    status: Literal[
        "pending", "approved", "denied", "expired", "cancelled",
        "succeeded", "failed", "outcome_unknown",
    ]
    permission: PermissionRecord
    operation: OperationRecord | None = None
    result: str | None = None


class CapabilityExecutor(Protocol):
    executor_kind: str
    identity_digest: str

    def execute(
        self, request: CapabilityRequest, cancelled: Callable[[], bool]
    ) -> str: ...


class CapabilityBroker:
    """Policy gate before the existing durable operation ledger.

    The broker owns no ambient filesystem or subprocess handle. Executors are
    injected by trusted Control Plane code and are called only after a durable
    one-shot permission and operation intent have both been established.
    """

    def __init__(
        self,
        store: ConversationStore,
        *,
        generation_provider: Callable[[], int],
        toolset_digest_provider: Callable[[], str],
        policy: CapabilityPolicy,
    ) -> None:
        self._store = store
        self._generation_provider = generation_provider
        self._toolset_digest_provider = toolset_digest_provider
        self._policy = policy

    @property
    def policy(self) -> CapabilityPolicy:
        return self._policy

    def request(
        self,
        request: CapabilityRequest,
        *,
        turn_id: str,
        interactive: bool,
    ) -> PermissionRecord:
        context = request.context
        if (
            context.capsule_generation != self._generation_provider()
            or context.toolset_digest != self._toolset_digest_provider()
            or context.policy_digest != self._policy.digest
        ):
            raise ConversationConflictError("capability binding is stale")
        decision = self._policy.resolve(context.tool_id, interactive=interactive)
        mutation = self._store.create_permission_request(
            permission_id=new_id(),
            capsule_generation=context.capsule_generation,
            conversation_id=context.conversation_id,
            turn_id=turn_id,
            run_id=context.run_id,
            call_id=context.call_id,
            capability_id=context.tool_id,
            toolset_digest=context.toolset_digest,
            policy_digest=context.policy_digest,
            arguments_digest=context.arguments_digest,
            preview=request.preview,
            preview_digest=context.preview_digest,
            policy_decision=decision,
            expires_at_milliseconds=context.expires_at_milliseconds,
        )
        return mutation.record

    def resolve(
        self,
        permission_id: str,
        decision: Literal["approve", "deny"],
        *,
        now_milliseconds: int | None = None,
    ) -> PermissionRecord:
        existing = self._store.get_permission_request(permission_id)
        if (
            existing.capsule_generation != self._generation_provider()
            or existing.toolset_digest != self._toolset_digest_provider()
            or existing.policy_digest != self._policy.digest
        ):
            return self._store.resolve_permission_request(
                permission_id,
                "cancelled",
                resolution_source="system",
                now_milliseconds=now_milliseconds,
            ).record
        status = "approved" if decision == "approve" else "denied"
        mutation = self._store.resolve_permission_request(
            permission_id,
            status,
            resolution_source="operator",
            now_milliseconds=now_milliseconds,
        )
        return mutation.record

    def execute(
        self,
        permission_id: str,
        request: CapabilityRequest,
        executor: CapabilityExecutor,
        *,
        turn_id: str,
        cancelled: Callable[[], bool] = lambda: False,
        now_milliseconds: int | None = None,
    ) -> CapabilityExecutionResult:
        permission = self._store.get_permission_request(permission_id)
        now_value = int(time.time() * 1000) if now_milliseconds is None else now_milliseconds
        context = request.context
        binding = (
            permission.agent_id,
            permission.capsule_generation,
            permission.conversation_id,
            permission.turn_id,
            permission.run_id,
            permission.call_id,
            permission.capability_id,
            permission.toolset_digest,
            permission.policy_digest,
            permission.arguments_digest,
            permission.preview_digest,
        )
        expected = (
            context.agent_id,
            context.capsule_generation,
            context.conversation_id,
            turn_id,
            context.run_id,
            context.call_id,
            context.tool_id,
            context.toolset_digest,
            context.policy_digest,
            context.arguments_digest,
            context.preview_digest,
        )
        if binding != expected or permission.preview != request.preview:
            raise ConversationConflictError("capability request binding changed")
        if permission.status == "pending":
            return CapabilityExecutionResult("pending", permission)
        if permission.status != "approved":
            return CapabilityExecutionResult(permission.status, permission)
        if (
            now_value >= permission.expires_at_milliseconds
            or context.capsule_generation != self._generation_provider()
            or context.toolset_digest != self._toolset_digest_provider()
            or context.policy_digest != self._policy.digest
            or cancelled()
        ):
            raise ConversationConflictError("approved capability is no longer valid")

        idempotency = _digest(
            b"agent-builder-capability-operation-v1",
            permission.permission_id.encode("ascii")
            + b"\0"
            + context.arguments_digest.encode("ascii")
            + b"\0"
            + context.preview_digest.encode("ascii"),
        )
        operation = self._store.record_operation_intent(
            operation_id=new_id(),
            capability_id=context.tool_id,
            policy_revision=self._policy.revision,
            idempotency_key_hash=idempotency,
            request_digest=context.arguments_digest,
            conversation_id=context.conversation_id,
            turn_id=turn_id,
            run_id=context.run_id,
            call_id=context.call_id,
        )
        dispatch = self._store.mark_operation_dispatched(
            operation.record.operation_id,
            executor_kind=executor.executor_kind,
            executor_identity_digest=executor.identity_digest,
        )
        if not dispatch.changed:
            return CapabilityExecutionResult(
                dispatch.record.status, permission, dispatch.record
            )
        if cancelled():
            cancelled_digest = _digest(
                b"agent-builder-capability-cancelled-v1",
                permission.permission_id.encode("ascii"),
            )
            outcome = self._store.record_operation_outcome(
                dispatch.record.operation_id,
                "cancelled",
                outcome_digest=cancelled_digest,
            ).record
            return CapabilityExecutionResult("cancelled", permission, outcome)
        try:
            # Trusted executors must call this same callback immediately before
            # the irreversible action. This closes the broker/executor handoff
            # race without granting the Worker any ambient authority.
            result = executor.execute(request, cancelled)
            result_bytes = _canonical_bytes(
                {"result": result}, 16 * 1024, "capability result"
            )
            outcome_digest = _digest(
                b"agent-builder-capability-outcome-v1", result_bytes
            )
            outcome = self._store.record_operation_outcome(
                dispatch.record.operation_id,
                "succeeded",
                outcome_digest=outcome_digest,
            ).record
            return CapabilityExecutionResult(
                "succeeded", permission, outcome, result
            )
        except CapabilityOutcomeUnknownError:
            outcome = self._store.record_operation_outcome(
                dispatch.record.operation_id,
                "outcome_unknown",
            ).record
            return CapabilityExecutionResult(
                "outcome_unknown", permission, outcome
            )
        except Exception as exc:
            failure = _digest(
                b"agent-builder-capability-failure-v1",
                type(exc).__name__.encode("ascii", "replace"),
            )
            outcome = self._store.record_operation_outcome(
                dispatch.record.operation_id,
                "failed",
                outcome_digest=failure,
            ).record
            return CapabilityExecutionResult("failed", permission, outcome)


__all__ = [
    "CapabilityBroker",
    "CapabilityExecutionResult",
    "CapabilityExecutor",
    "CapabilityOutcomeUnknownError",
    "CapabilityPolicy",
    "CapabilityRequest",
    "MAX_CAPABILITY_ARGUMENT_BYTES",
    "MAX_CAPABILITY_COLLECTION_ITEMS",
    "MAX_CAPABILITY_JSON_DEPTH",
    "MAX_CAPABILITY_JSON_NODES",
    "MAX_CAPABILITY_PREVIEW_BYTES",
    "MAX_CAPABILITY_STRING_BYTES",
    "MAX_PERMISSION_TTL_MILLISECONDS",
    "MIN_PERMISSION_TTL_MILLISECONDS",
    "PolicyDecision",
]

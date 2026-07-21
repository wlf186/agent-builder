"""Digest-only durable boundaries for reproducible model-view projections."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from .context import (
    CONTEXT_RENDERER_VERSION,
    PROMPT_SECTION_REGISTRY_VERSION,
    ContextPlan,
)
from .runtime import TurnRuntimeSnapshot
from .semantic_summary import SemanticSummaryError, SemanticSummarySnapshot


CONTEXT_PROJECTION_VERSION = "context-projection-v2"
LEGACY_CONTEXT_PROJECTION_VERSION = "context-projection-v1"
LEGACY_CONTEXT_RENDERER_VERSION = "ordered-sections-v4"
LEGACY_SECTION_REGISTRY_VERSION = "prompt-section-registry-v3"
OLDEST_CONTEXT_RENDERER_VERSION = "ordered-sections-v3"
OLDEST_SECTION_REGISTRY_VERSION = "prompt-section-registry-v2"
MAX_CONTEXT_PROJECTION_BYTES = 16 * 1024
ProjectionReason = Literal[
    "admission", "replay", "manual_compact", "semantic_summary"
]

_RESOURCE_ID = re.compile(r"^[a-f0-9]{32}$")
_AGENT_ID = re.compile(r"^[a-f0-9-]{32,64}$")
_PLAN_ID = re.compile(r"^context-[a-f0-9]{24}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_REASONS = {
    "admission",
    "replay",
    "manual_compact",
    "semantic_summary",
}


class ContextProjectionError(ValueError):
    """A projection boundary is malformed or stale."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical(value)).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextProjectionError("projection boundary repeats a field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ContextProjectionBoundary:
    agent_id: str
    capsule_generation: int
    conversation_id: str
    turn_id: str
    run_id: str
    conversation_revision: int
    reason: ProjectionReason
    context_plan_id: str
    context_plan_digest: str
    history_source_digest: str
    instruction_digest: str
    recent_segment_digest: str
    included_history_message_ids: tuple[str, ...]
    model_profile_digest: str
    compression_policy_digest: str
    toolset_digest: str
    tool_catalog_digest: str
    tool_policy_digest: str
    renderer_version: str
    section_registry_version: str
    windowing_strategy: str
    estimated_input_tokens: int
    semantic_summary: SemanticSummarySnapshot | None
    boundary_digest: str
    version: str = CONTEXT_PROJECTION_VERSION

    def __post_init__(self) -> None:
        unsigned = self._unsigned_dict()
        if (
            self.version not in {
                CONTEXT_PROJECTION_VERSION, LEGACY_CONTEXT_PROJECTION_VERSION
            }
            or _AGENT_ID.fullmatch(self.agent_id) is None
            or any(
                _RESOURCE_ID.fullmatch(item) is None
                for item in (self.conversation_id, self.turn_id, self.run_id)
            )
            or not isinstance(self.capsule_generation, int)
            or isinstance(self.capsule_generation, bool)
            or not 1 <= self.capsule_generation <= 1_000_000_000
            or not isinstance(self.conversation_revision, int)
            or isinstance(self.conversation_revision, bool)
            or not 0 <= self.conversation_revision <= 1_000_000_000
            or self.reason not in _REASONS
            or _PLAN_ID.fullmatch(self.context_plan_id) is None
            or any(
                _DIGEST.fullmatch(item) is None
                for item in (
                    self.context_plan_digest,
                    self.history_source_digest,
                    self.instruction_digest,
                    self.recent_segment_digest,
                    self.model_profile_digest,
                    self.compression_policy_digest,
                    self.toolset_digest,
                    self.tool_catalog_digest,
                    self.tool_policy_digest,
                    self.boundary_digest,
                )
            )
            or self.context_plan_id
            != f"context-{self.context_plan_digest[:24]}"
            or not isinstance(self.included_history_message_ids, tuple)
            or len(self.included_history_message_ids) > 256
            or len(set(self.included_history_message_ids))
            != len(self.included_history_message_ids)
            or any(
                _RESOURCE_ID.fullmatch(item) is None
                for item in self.included_history_message_ids
            )
            or (self.renderer_version, self.section_registry_version)
            not in {
                (CONTEXT_RENDERER_VERSION, PROMPT_SECTION_REGISTRY_VERSION),
                (LEGACY_CONTEXT_RENDERER_VERSION, LEGACY_SECTION_REGISTRY_VERSION),
                (OLDEST_CONTEXT_RENDERER_VERSION, OLDEST_SECTION_REGISTRY_VERSION),
            }
            or self.windowing_strategy
            not in {
                "full", "completed-turn-tail-v1", "completed-turn-collapse-v2",
                "semantic-summary-v1",
            }
            or (
                self.version == LEGACY_CONTEXT_PROJECTION_VERSION
                and self.semantic_summary is not None
            )
            or (
                self.windowing_strategy == "semantic-summary-v1"
                and not isinstance(self.semantic_summary, SemanticSummarySnapshot)
            )
            or (
                self.windowing_strategy != "semantic-summary-v1"
                and self.semantic_summary is not None
            )
            or not isinstance(self.estimated_input_tokens, int)
            or isinstance(self.estimated_input_tokens, bool)
            or not 1 <= self.estimated_input_tokens <= 1_000_000_000
            or self.boundary_digest
            != _digest(
                (
                    b"agent-builder-context-projection-v2"
                    if self.version == CONTEXT_PROJECTION_VERSION
                    else b"agent-builder-context-projection-v1"
                ),
                unsigned,
            )
        ):
            raise ContextProjectionError("invalid context projection boundary")
        if len(self.to_json().encode("utf-8")) > MAX_CONTEXT_PROJECTION_BYTES:
            raise ContextProjectionError("context projection boundary is too large")

    @classmethod
    def create(
        cls,
        runtime: TurnRuntimeSnapshot,
        *,
        conversation_id: str,
        turn_id: str,
        run_id: str,
        conversation_revision: int,
        reason: ProjectionReason | None = None,
    ) -> ContextProjectionBoundary:
        if not isinstance(runtime, TurnRuntimeSnapshot):
            raise ContextProjectionError("runtime snapshot is required")
        plan: ContextPlan = runtime.context_plan
        plan.verify()
        selected_reason: ProjectionReason = (
            runtime.projection_reason if reason is None else reason
        )  # type: ignore[assignment]
        system_sections = tuple(
            {
                "id": section.section_id,
                "dependency_digest": section.dependency_digest,
            }
            for section in plan.sections
            if section.role == "system"
        )
        history_sections = tuple(
            section
            for section in plan.sections
            if section.trust == "conversation"
            and section.section_id != "conversation.window"
        )
        message_ids: list[str] = []
        for section in history_sections:
            prefix = "conversation-message:"
            if not section.provenance.startswith(prefix):
                raise ContextProjectionError("history section has no message identity")
            message_ids.append(section.provenance[len(prefix) :])
        values: dict[str, object] = {
            "version": CONTEXT_PROJECTION_VERSION,
            "agent_id": runtime.agent_id,
            "capsule_generation": runtime.capsule_generation,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "run_id": run_id,
            "conversation_revision": conversation_revision,
            "reason": selected_reason,
            "context_plan_id": plan.reference.plan_id,
            "context_plan_digest": plan.reference.digest,
            "history_source_digest": plan.history_source_digest,
            "instruction_digest": _digest(
                b"agent-builder-context-instructions-v1", system_sections
            ),
            "recent_segment_digest": _digest(
                b"agent-builder-context-recent-segment-v1",
                [section.canonical_manifest() for section in history_sections],
            ),
            "included_history_message_ids": tuple(message_ids),
            "model_profile_digest": _digest(
                b"agent-builder-model-profile-v1",
                runtime.model_profile.canonical_manifest(),
            ),
            "compression_policy_digest": _digest(
                b"agent-builder-compression-policy-v1",
                plan.policy.canonical_manifest(),
            ),
            "toolset_digest": plan.reference.toolset_digest,
            "tool_catalog_digest": runtime.tool_catalog_digest,
            "tool_policy_digest": runtime.tool_policy_digest,
            "renderer_version": CONTEXT_RENDERER_VERSION,
            "section_registry_version": PROMPT_SECTION_REGISTRY_VERSION,
            "windowing_strategy": plan.windowing_strategy,
            "estimated_input_tokens": plan.estimated_input_tokens,
            "semantic_summary": (
                plan.semantic_summary.to_dict()
                if plan.semantic_summary is not None else None
            ),
        }
        boundary_digest = _digest(
            b"agent-builder-context-projection-v2", values
        )
        return cls(
            **{key: value for key, value in values.items() if key != "semantic_summary"},
            semantic_summary=plan.semantic_summary,
            boundary_digest=boundary_digest,
        )  # type: ignore[arg-type]

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "agent_id": self.agent_id,
            "capsule_generation": self.capsule_generation,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "conversation_revision": self.conversation_revision,
            "reason": self.reason,
            "context_plan_id": self.context_plan_id,
            "context_plan_digest": self.context_plan_digest,
            "history_source_digest": self.history_source_digest,
            "instruction_digest": self.instruction_digest,
            "recent_segment_digest": self.recent_segment_digest,
            "included_history_message_ids": list(
                self.included_history_message_ids
            ),
            "model_profile_digest": self.model_profile_digest,
            "compression_policy_digest": self.compression_policy_digest,
            "toolset_digest": self.toolset_digest,
            "tool_catalog_digest": self.tool_catalog_digest,
            "tool_policy_digest": self.tool_policy_digest,
            "renderer_version": self.renderer_version,
            "section_registry_version": self.section_registry_version,
            "windowing_strategy": self.windowing_strategy,
            "estimated_input_tokens": self.estimated_input_tokens,
            **(
                {"semantic_summary": (
                    self.semantic_summary.to_dict()
                    if self.semantic_summary is not None else None
                )}
                if self.version == CONTEXT_PROJECTION_VERSION else {}
            ),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._unsigned_dict(), "boundary_digest": self.boundary_digest}

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, raw: str) -> ContextProjectionBoundary:
        if not isinstance(raw, str):
            raise ContextProjectionError("invalid context projection encoding")
        try:
            encoded = raw.encode("utf-8")
            if not 2 <= len(encoded) <= MAX_CONTEXT_PROJECTION_BYTES:
                raise ContextProjectionError("invalid context projection encoding")
            value = json.loads(raw, object_pairs_hook=_strict_object)
        except ContextProjectionError:
            raise
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise ContextProjectionError("invalid context projection encoding") from exc
        legacy_expected = {
            "version", "agent_id", "capsule_generation", "conversation_id",
            "turn_id", "run_id", "conversation_revision", "reason",
            "context_plan_id", "context_plan_digest", "history_source_digest",
            "instruction_digest", "recent_segment_digest",
            "included_history_message_ids", "model_profile_digest",
            "compression_policy_digest", "toolset_digest", "tool_catalog_digest",
            "tool_policy_digest", "renderer_version", "section_registry_version",
            "windowing_strategy", "estimated_input_tokens", "boundary_digest",
        }
        if not isinstance(value, dict):
            raise ContextProjectionError("invalid context projection fields")
        version = value.get("version")
        expected = (
            legacy_expected | {"semantic_summary"}
            if version == CONTEXT_PROJECTION_VERSION else legacy_expected
        )
        if set(value) != expected:
            raise ContextProjectionError("invalid context projection fields")
        message_ids = value.get("included_history_message_ids")
        if not isinstance(message_ids, list) or any(
            not isinstance(item, str) for item in message_ids
        ):
            raise ContextProjectionError("invalid projection history identities")
        value["included_history_message_ids"] = tuple(message_ids)
        if version == CONTEXT_PROJECTION_VERSION:
            summary = value.get("semantic_summary")
            try:
                value["semantic_summary"] = (
                    SemanticSummarySnapshot.from_object(summary)
                    if summary is not None else None
                )
            except SemanticSummaryError as exc:
                raise ContextProjectionError("invalid semantic summary snapshot") from exc
        else:
            value["semantic_summary"] = None
        try:
            return cls(**value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ContextProjectionError("invalid context projection boundary") from exc

    def require_matches(
        self,
        runtime: TurnRuntimeSnapshot,
        *,
        conversation_revision: int,
    ) -> None:
        expected = self.create(
            runtime,
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            run_id=self.run_id,
            conversation_revision=conversation_revision,
            reason=self.reason,
        )
        if expected != self:
            raise ContextProjectionError(
                "context projection binding changed; deterministic rebuild is required"
            )


__all__ = [
    "CONTEXT_PROJECTION_VERSION",
    "LEGACY_CONTEXT_PROJECTION_VERSION",
    "ContextProjectionBoundary",
    "ContextProjectionError",
    "MAX_CONTEXT_PROJECTION_BYTES",
    "ProjectionReason",
]

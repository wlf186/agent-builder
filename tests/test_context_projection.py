"""Durable context projection boundary and stale-binding tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import (
    ContextCompiler,
    ConversationMessage,
    ModelProfile,
)
from agent_builder_v2.context_projection import (
    CONTEXT_PROJECTION_VERSION,
    ContextProjectionBoundary,
    ContextProjectionError,
)
from agent_builder_v2.contracts import LoopLimits
from agent_builder_v2.runtime import TurnRuntimeSnapshot
from agent_builder_v2.tools import (
    EffectiveToolSet,
    ToolPolicy,
    prototype_tool_catalog,
    prototype_effective_toolset,
)
from agent_builder_v2.workspace_context import PromptSource, PromptSourceSnapshot


CONVERSATION_ID = "1" * 32
TURN_ID = "2" * 32
RUN_ID = "3" * 32


def _profile(*, model_digest: str = "a" * 64) -> ModelProfile:
    return ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest=model_digest,
        native_context_tokens=262_144,
        operational_context_tokens=32_768,
        max_output_tokens=2_048,
        profile_source="test",
    )


def _source(content: str) -> PromptSourceSnapshot:
    encoded = content.encode("utf-8")
    return PromptSourceSnapshot(
        workspace_instructions=PromptSource(
            content,
            hashlib.sha256(encoded).hexdigest(),
            f"capsule:{PROTOTYPE_AGENT_ID}:generation:1:workspace/CLAUDE.md",
        )
    )


def _runtime(
    *,
    user_message: str = "current turn",
    model_digest: str = "a" * 64,
    instructions: str = "Use the workspace contract.",
    effective: EffectiveToolSet | None = None,
) -> TurnRuntimeSnapshot:
    effective = effective or prototype_effective_toolset()
    plan = ContextCompiler().compile(
        user_message,
        model_profile=_profile(model_digest=model_digest),
        tools=effective.specs,
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        history=(
            ConversationMessage("4" * 32, "user", "older user"),
            ConversationMessage("5" * 32, "assistant", "older assistant"),
        ),
        prompt_sources=_source(instructions),
    )
    return TurnRuntimeSnapshot.create(
        context_plan=plan,
        effective_toolset=effective,
        loop_limits=LoopLimits(max_model_iterations=4, max_tool_calls=2),
        wall_timeout_seconds=60,
    )


def _boundary(
    runtime: TurnRuntimeSnapshot, *, reason: str = "admission"
) -> ContextProjectionBoundary:
    return ContextProjectionBoundary.create(
        runtime,
        conversation_id=CONVERSATION_ID,
        turn_id=TURN_ID,
        run_id=RUN_ID,
        conversation_revision=2,
        reason=reason,  # type: ignore[arg-type]
    )


def test_projection_boundary_is_deterministic_bounded_and_content_free() -> None:
    runtime = _runtime()
    first = _boundary(runtime)
    second = _boundary(runtime)
    assert first == second
    assert first.version == CONTEXT_PROJECTION_VERSION
    assert first.included_history_message_ids == ("4" * 32, "5" * 32)
    assert ContextProjectionBoundary.from_json(first.to_json()) == first
    assert "older user" not in first.to_json()
    assert "Use the workspace contract" not in first.to_json()
    assert len(first.to_json().encode("utf-8")) < 16 * 1024
    reason_digests = {
        _boundary(runtime, reason=reason).boundary_digest
        for reason in (
            "admission",
            "replay",
            "manual_compact",
            "semantic_summary",
        )
    }
    assert len(reason_digests) == 4
    with pytest.raises(ContextProjectionError):
        _boundary(runtime, reason="unknown")


def test_projection_boundary_rejects_every_stale_binding() -> None:
    runtime = _runtime()
    boundary = _boundary(runtime)
    boundary.require_matches(runtime, conversation_revision=2)

    stale_policy = EffectiveToolSet.resolve(
        prototype_tool_catalog(),
        ToolPolicy(
            revision="projection-policy-v2",
            allowed_tool_ids=("builtin/echo",),
            allowed_risks=("read_only",),
        ),
    )
    stale_runtimes = (
        _runtime(user_message="changed current turn"),
        _runtime(model_digest="b" * 64),
        _runtime(instructions="Changed workspace contract."),
        _runtime(effective=stale_policy),
    )
    for stale in stale_runtimes:
        with pytest.raises(ContextProjectionError, match="rebuild is required"):
            boundary.require_matches(stale, conversation_revision=2)
    with pytest.raises(ContextProjectionError, match="rebuild is required"):
        boundary.require_matches(runtime, conversation_revision=3)


def test_projection_boundary_tamper_and_unknown_fields_fail_closed() -> None:
    boundary = _boundary(_runtime())
    value = boundary.to_dict()
    value["history_source_digest"] = "f" * 64
    with pytest.raises(ContextProjectionError):
        ContextProjectionBoundary.from_json(json.dumps(value))

    value = boundary.to_dict()
    value["unexpected"] = True
    with pytest.raises(ContextProjectionError, match="fields"):
        ContextProjectionBoundary.from_json(json.dumps(value))

    with pytest.raises(ContextProjectionError):
        replace(boundary, included_history_message_ids=("not-an-id",))


def test_legacy_renderer_boundary_decodes_but_cannot_silently_reuse_v5() -> None:
    runtime = _runtime()
    value = _boundary(runtime).to_dict()
    value["renderer_version"] = "ordered-sections-v3"
    value["section_registry_version"] = "prompt-section-registry-v2"
    value["version"] = "context-projection-v1"
    value.pop("semantic_summary")
    unsigned = {key: item for key, item in value.items() if key != "boundary_digest"}
    value["boundary_digest"] = hashlib.sha256(
        b"agent-builder-context-projection-v1\0"
        + json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    legacy = ContextProjectionBoundary.from_json(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert legacy.renderer_version == "ordered-sections-v3"
    with pytest.raises(ContextProjectionError, match="rebuild is required"):
        legacy.require_matches(runtime, conversation_revision=2)

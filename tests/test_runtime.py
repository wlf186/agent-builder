"""Immutable TurnRuntimeSnapshot contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import ContextCompiler, ModelProfile
from agent_builder_v2.contracts import LoopLimits
from agent_builder_v2.runtime import TurnRuntimeSnapshot
from agent_builder_v2.tools import prototype_tool_specs


def _snapshot() -> TurnRuntimeSnapshot:
    profile = ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest="a" * 64,
        native_context_tokens=262_144,
        operational_context_tokens=32_768,
        max_output_tokens=2_048,
        profile_source="test",
    )
    plan = ContextCompiler().compile(
        "snapshot test",
        model_profile=profile,
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=7,
    )
    return TurnRuntimeSnapshot.create(
        context_plan=plan,
        loop_limits=LoopLimits(max_model_iterations=4, max_tool_calls=2),
        wall_timeout_seconds=60,
    )


def test_runtime_snapshot_freezes_every_execution_affecting_boundary() -> None:
    snapshot = _snapshot()

    assert snapshot.agent_id == PROTOTYPE_AGENT_ID
    assert snapshot.capsule_generation == 7
    assert snapshot.effective_tools == snapshot.context_plan.tools
    assert snapshot.max_total_input_tokens == 30_720 * 4
    assert snapshot.max_total_output_tokens == 2_048 * 4
    assert snapshot.public_metadata() == {
        "capsule_generation": 7,
        "model_id": "qwen3.5:2b",
        "model_profile_digest": snapshot.model_profile.profile_digest,
        "context_plan_id": snapshot.context_plan.reference.plan_id,
        "context_plan_digest": snapshot.context_plan.reference.digest,
        "toolset_digest": snapshot.context_plan.reference.toolset_digest,
        "tool_catalog_digest": snapshot.tool_catalog_digest,
        "tool_policy_digest": snapshot.tool_policy_digest,
        "loop_limits": {"max_model_iterations": 4, "max_tool_calls": 2},
        "max_total_input_tokens": 122_880,
        "max_total_output_tokens": 8_192,
        "wall_timeout_seconds": 60,
        "projection_reason": "admission",
    }
    with pytest.raises(FrozenInstanceError):
        snapshot.wall_timeout_seconds = 61  # type: ignore[misc]


def test_runtime_snapshot_rejects_drift_and_invalid_loop_limits() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValueError, match="runtime snapshot"):
        replace(snapshot, capsule_generation=8)
    with pytest.raises(ValueError, match="runtime snapshot"):
        replace(snapshot, effective_tools=())
    with pytest.raises(ValueError, match="loop limits"):
        LoopLimits(max_model_iterations=2, max_tool_calls=2)
    with pytest.raises(ValueError, match="runtime snapshot"):
        replace(snapshot, max_total_input_tokens=1)

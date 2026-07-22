"""Completed Turn bundle schema, rendering and whole-bundle compaction."""

from __future__ import annotations

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.completed_context import (
    CompletedContextItem,
    CompletedTurnContext,
)
from agent_builder_v2.context import ContextCompiler, ModelProfile
from agent_builder_v2.tools import prototype_tool_specs, project_tool_result


def _profile(window: int = 32_768) -> ModelProfile:
    return ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest="a" * 64,
        native_context_tokens=262_144,
        operational_context_tokens=window,
        max_output_tokens=2_048,
        profile_source="test",
    )


def _bundle(position: int, *, padding: int = 0) -> CompletedTurnContext:
    spec = prototype_tool_specs()[0]
    call_id = f"call_{position}"
    projection = project_tool_result(spec, call_id, "result-" + "r" * padding)
    return CompletedTurnContext(
        agent_id=PROTOTYPE_AGENT_ID,
        conversation_id="1" * 32,
        turn_id=f"{position + 10:032x}",
        run_id=f"{position + 100:032x}",
        position=position,
        model_profile_digest="b" * 64,
        context_plan_digest="c" * 64,
        items=(
            CompletedContextItem.plain(0, "user", f"question-{position}"),
            CompletedContextItem.tool_use(
                1,
                call_id=call_id,
                tool_id=spec.tool_id,
                provider_name=spec.provider_name,
                arguments={"text": f"lookup-{position}"},
            ),
            CompletedContextItem.tool_result(
                2,
                call_id=call_id,
                tool_id=spec.tool_id,
                provider_name=spec.provider_name,
                content=projection.content,
                outcome="succeeded",
                original_bytes=projection.original_bytes,
                projection_reason=projection.truncation_reason,
                projection_digest=projection.projection_digest,
            ),
            CompletedContextItem.plain(
                3, "assistant_final", f"answer-{position}"
            ),
        ),
    )


def test_completed_bundle_round_trips_and_renders_native_provider_roles() -> None:
    bundle = _bundle(1)

    assert CompletedTurnContext.from_dict(bundle.to_dict()) == bundle
    messages = bundle.provider_messages()
    assert [item["role"] for item in messages] == [
        "user", "assistant", "tool", "assistant"
    ]
    assert messages[1]["tool_calls"][0]["id"] == "call_1"  # type: ignore[index]
    assert messages[2]["tool_name"] == "builtin_echo"


def test_context_compiler_uses_bundle_tool_memory_without_pair_duplication() -> None:
    bundle = _bundle(1)
    plan = ContextCompiler().compile(
        "follow up",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        completed_turns=(bundle,),
    )

    messages = plan.provider_messages()
    assert [item["role"] for item in messages] == [
        "system", "user", "assistant", "tool", "assistant", "user"
    ]
    assert sum(item.get("content") == "question-1" for item in messages) == 1
    assert plan.public_metadata()["included_turn_bundle_count"] == 1


def test_compaction_removes_only_whole_completed_turn_bundles() -> None:
    bundles = tuple(_bundle(index, padding=2_000) for index in range(1, 5))
    plan = ContextCompiler().compile(
        "current",
        model_profile=_profile(window=8_192),
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        completed_turns=bundles,
        force_compact=True,
    )

    assert plan.windowing_strategy == "completed-turn-collapse-v2"
    assert 0 <= len(plan.completed_turns) < len(bundles)
    if plan.completed_turns:
        assert plan.completed_turns == bundles[-len(plan.completed_turns) :]
    assert plan.included_history_message_count == len(plan.completed_turns) * 2

"""Deterministic ContextPlan and model-capacity policy tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import json

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import (
    CompressionPolicy,
    ConversationMessage,
    ContextCompiler,
    ContextPlan,
    ContextPlanError,
    ContextPlanReference,
    CONTEXT_RENDERER_VERSION,
    ModelProfile,
    PROMPT_SECTION_REGISTRY_VERSION,
    PROVIDER_TEMPLATE_TOKEN_RESERVE,
    PromptSectionRegistry,
    estimate_text_tokens,
)
from agent_builder_v2.tools import prototype_tool_specs


def _profile(
    *, native: int = 262_144, operational: int = 32_768, output: int = 2_048
) -> ModelProfile:
    return ModelProfile(
        provider="ollama",
        model="qwen3.5:2b",
        model_digest="a" * 64,
        native_context_tokens=native,
        operational_context_tokens=operational,
        max_output_tokens=output,
        profile_source="test-profile",
    )


def _plan(message: str = "hello") -> ContextPlan:
    return ContextCompiler().compile(
        message,
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )


def test_qwen_operational_window_produces_dynamic_usable_budget() -> None:
    profile = _profile()
    policy = CompressionPolicy.for_profile(profile)

    assert profile.native_context_tokens == 262_144
    assert profile.operational_context_tokens == 32_768
    assert policy.hard_input_tokens == 30_720
    assert policy.compact_at_tokens == 24_576
    assert policy.compact_target_tokens == 18_432
    assert profile.request_byte_budget == 262_144

    smaller = CompressionPolicy.for_profile(
        _profile(native=16_384, operational=16_384, output=1_024)
    )
    assert smaller.hard_input_tokens == 15_360
    assert smaller.compact_at_tokens == 12_288
    assert smaller.compact_target_tokens == 9_216


def test_fallback_estimator_is_a_conservative_utf8_admission_bound() -> None:
    assert estimate_text_tokens("ascii") == 5
    assert estimate_text_tokens("中文") == 6

    with pytest.raises(ContextPlanError, match="model input budget"):
        ContextCompiler().compile(
            "x" * 3_500,
            model_profile=_profile(native=4_096, operational=4_096, output=256),
            tools=prototype_tool_specs(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
        )


def test_admission_counts_rendered_headers_tools_and_template_reserve() -> None:
    profile = _profile(native=4_096, operational=2_048, output=256)
    compiler = ContextCompiler()
    base = compiler.compile(
        "x",
        model_profile=profile,
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    rendered_bytes = len(
        json.dumps(
            {"messages": base.provider_messages(), "tools": []},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert base.estimated_input_tokens == (
        rendered_bytes + PROVIDER_TEMPLATE_TOKEN_RESERVE
    )

    fixed_overhead = base.estimated_input_tokens - 1
    exact_message = "x" * (base.policy.hard_input_tokens - fixed_overhead)
    exact = compiler.compile(
        exact_message,
        model_profile=profile,
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    assert exact.estimated_input_tokens == exact.policy.hard_input_tokens
    with pytest.raises(ContextPlanError, match="model input budget"):
        compiler.compile(
            exact_message + "x",
            model_profile=profile,
            tools=(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
        )


def test_context_plan_is_ordered_reproducible_and_provider_renderable() -> None:
    first = _plan("请回显这一条")
    second = _plan("请回显这一条")

    assert first.reference == second.reference
    assert [section.section_id for section in first.sections] == [
        "platform.contract",
        "agent.instructions",
        "turn.user",
    ]
    assert [section.trust for section in first.sections] == [
        "platform",
        "agent",
        "user",
    ]
    messages = first.provider_messages()
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "[platform.contract]" in messages[0]["content"]
    assert "[agent.instructions]" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "请回显这一条"}
    assert "请回显这一条" not in repr(first.sections[-1])


def test_prompt_section_registry_is_sealed_ordered_and_bounded() -> None:
    registry = PromptSectionRegistry(maximum_cache_entries=3)
    assert registry.provider_manifest() == (
        {"provider_id": "platform.contract", "order": 100, "cacheable": True},
        {"provider_id": "agent.instructions", "order": 200, "cacheable": True},
        {"provider_id": "workspace.instructions", "order": 300, "cacheable": True},
        {"provider_id": "runtime.environment", "order": 400, "cacheable": True},
        {"provider_id": "workspace.git", "order": 500, "cacheable": True},
        {"provider_id": "conversation.window", "order": 600, "cacheable": False},
        {"provider_id": "conversation.history", "order": 1000, "cacheable": False},
        {"provider_id": "turn.user", "order": 2000, "cacheable": False},
    )
    compiler = ContextCompiler(registry)
    base = compiler.compile(
        "first",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    changed_user = compiler.compile(
        "second",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    dependencies = {
        section.section_id: section.dependency_digest for section in base.sections
    }
    changed_dependencies = {
        section.section_id: section.dependency_digest
        for section in changed_user.sections
    }
    assert dependencies["platform.contract"] == changed_dependencies["platform.contract"]
    assert dependencies["agent.instructions"] == changed_dependencies["agent.instructions"]
    assert dependencies["turn.user"] != changed_dependencies["turn.user"]
    assert registry.cache_entries == 3

    for generation in range(2, 8):
        compiler.compile(
            "first",
            model_profile=_profile(),
            tools=prototype_tool_specs(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=generation,
        )
    assert registry.cache_entries == 3
    assert base.sections[0].trust == "platform"
    assert base.sections[0].role == "system"
    assert base.sections[0].truncation_policy == "never"
    assert base.sections[0].estimated_tokens <= base.sections[0].budget_tokens


def test_operator_inspection_is_ordered_defensive_and_withholds_content() -> None:
    history = (
        ConversationMessage("a" * 32, "user", "history-user-secret-47"),
        ConversationMessage("b" * 32, "assistant", "history-answer-secret-53"),
    )
    plan = ContextCompiler().compile(
        "current-user-secret-59",
        history=history,
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    inspection_key = bytes(range(32))
    inspection = plan.operator_inspection(inspection_key)
    payload = inspection.to_dict()
    sections = payload["sections"]
    assert isinstance(sections, list)
    assert [section["id"] for section in sections] == [
        "platform.contract",
        "agent.instructions",
        "conversation.0000.user",
        "conversation.0001.assistant",
        "turn.user",
    ]
    assert [section["role"] for section in sections] == [
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert payload["provider_message_count"] == len(plan.provider_messages())
    assert "provider_messages" not in payload
    assert payload["renderer"] == {
        "version": CONTEXT_RENDERER_VERSION,
        "section_registry_version": PROMPT_SECTION_REGISTRY_VERSION,
        "leading_system_sections_merged": True,
        "leading_system_section_count": 2,
        "description": payload["renderer"]["description"],
    }
    assert payload["content_exposure"] == "withheld"
    for section, source in zip(sections, plan.sections, strict=True):
        encoded = source.content.encode("utf-8")
        assert set(section) == {
            "id",
            "role",
            "trust",
            "provenance",
            "cache",
            "truncation",
            "dependency_digest",
            "budget_tokens",
            "truncation_reason",
            "estimated_tokens",
            "content_bytes",
            "content_digest",
        }
        assert "content" not in section
        assert section["content_bytes"] == len(encoded)
        assert section["content_digest"] == hmac.new(
            inspection_key,
            b"agent-builder-context-section-inspection-v1\0" + encoded,
            hashlib.sha256,
        ).hexdigest()
        assert section["content_digest"] != hashlib.sha256(
            b"agent-builder-context-section-inspection-v1\0" + encoded
        ).hexdigest()

    serialized = json.dumps(payload, ensure_ascii=False)
    assert all(section.content not in serialized for section in plan.sections)

    payload["context_plan"]["plan_id"] = "changed"
    payload["renderer"]["version"] = "changed"
    sections[0]["id"] = "changed"
    fresh = inspection.to_dict()
    assert fresh["context_plan"]["plan_id"] == plan.reference.plan_id
    assert fresh["renderer"]["version"] == CONTEXT_RENDERER_VERSION
    assert fresh["sections"][0]["id"] == "platform.contract"
    same_key = plan.operator_inspection(inspection_key)
    different_key = plan.operator_inspection(b"z" * 32)
    assert same_key is not inspection
    assert same_key.to_dict()["sections"] == inspection.to_dict()["sections"]
    assert (
        different_key.to_dict()["sections"][0]["content_digest"]
        != same_key.to_dict()["sections"][0]["content_digest"]
    )
    with pytest.raises(ContextPlanError, match="digest key"):
        plan.operator_inspection(b"short")


def test_operator_reveal_never_exposes_trusted_instructions_and_redacts_secrets() -> None:
    plan = ContextCompiler().compile(
        "token=0123456789abcdef0123456789abcdef and visible diagnostic text",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    sections = plan.operator_redacted_reveal(maximum_excerpt_bytes=128)

    assert [item.exposure for item in sections[:2]] == ["withheld", "withheld"]
    assert sections[0].excerpt is None
    assert sections[1].excerpt is None
    assert sections[-1].exposure == "redacted_excerpt"
    assert sections[-1].excerpt is not None
    assert "0123456789abcdef" not in sections[-1].excerpt
    assert "[REDACTED]" in sections[-1].excerpt
    assert plan.sections[0].content not in str(
        [item.to_dict() for item in sections]
    )


def test_plan_digest_covers_message_generation_profile_and_tool_contract() -> None:
    compiler = ContextCompiler()
    base = compiler.compile(
        "hello",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    changed_message = compiler.compile(
        "hello again",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    changed_generation = compiler.compile(
        "hello",
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=2,
    )
    changed_profile = compiler.compile(
        "hello",
        model_profile=_profile(native=16_384, operational=16_384, output=1_024),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    changed_tool = compiler.compile(
        "hello",
        model_profile=_profile(),
        tools=(replace(prototype_tool_specs()[0], description="Changed guidance"),),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    assert len(
        {
            base.reference.digest,
            changed_message.reference.digest,
            changed_generation.reference.digest,
            changed_profile.reference.digest,
            changed_tool.reference.digest,
        }
    ) == 5


def test_invalid_capacity_reference_and_over_budget_context_fail_closed() -> None:
    with pytest.raises(ContextPlanError, match="invalid trusted model profile"):
        _profile(native=4_096, operational=4_096, output=4_096)
    with pytest.raises(ContextPlanError, match="invalid context plan reference"):
        ContextPlanReference.from_dict(
            {"plan_id": "bad", "digest": "secret", "toolset_digest": "x"}
        )
    with pytest.raises(ContextPlanError, match="model input budget") as raised:
        ContextCompiler().compile(
            "x" * 20_000,
            model_profile=_profile(native=4_096, operational=4_096, output=256),
            tools=prototype_tool_specs(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
        )
    assert "x" * 128 not in str(raised.value)

    recent_pair_too_large = tuple(
        ConversationMessage(
            f"{index + 20:032x}",
            "user" if index % 2 == 0 else "assistant",
            f"history-{index}-" + "z" * 2_000,
        )
        for index in range(4)
    )
    with pytest.raises(ContextPlanError, match="model input budget"):
        ContextCompiler().compile(
            "current",
            history=recent_pair_too_large,
            model_profile=_profile(native=4_096, operational=4_096, output=256),
            tools=(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
        )


def test_context_plan_rejects_stale_digest_after_in_memory_tampering() -> None:
    plan = _plan("trusted message")
    changed = _plan("different trusted message")

    with pytest.raises(ContextPlanError, match="digest"):
        replace(
            plan,
            sections=changed.sections,
            estimated_input_tokens=changed.estimated_input_tokens,
        )
    with pytest.raises(ContextPlanError, match="budget or Tool set"):
        replace(
            plan,
            tools=(replace(plan.tools[0], provider_name="renamed_echo"),),
        )
    with pytest.raises(ContextPlanError, match="budget or Tool set"):
        replace(plan, estimated_input_tokens=plan.estimated_input_tokens + 1)


def test_committed_multiturn_history_keeps_native_chat_roles_and_binds_digest() -> None:
    history = (
        ConversationMessage("1" * 32, "user", "我叫小林。"),
        ConversationMessage("2" * 32, "assistant", "你好，小林。"),
        ConversationMessage("3" * 32, "user", "请记住我的名字。"),
        ConversationMessage("4" * 32, "assistant", "好的，我会在本会话中记住。"),
    )
    plan = ContextCompiler().compile(
        "我叫什么？",
        history=history,
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    assert [message["role"] for message in plan.provider_messages()] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert plan.provider_messages()[-1]["content"] == "我叫什么？"
    assert plan.history_message_count == 4
    assert plan.included_history_message_count == 4
    assert plan.windowing_strategy == "full"
    assert plan.public_metadata()["omitted_history_message_count"] == 0

    changed = ContextCompiler().compile(
        "我叫什么？",
        history=history[:-1]
        + (ConversationMessage("4" * 32, "assistant", "已经忘记。"),),
        model_profile=_profile(),
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )
    assert changed.reference.digest != plan.reference.digest
    assert changed.history_source_digest != plan.history_source_digest


def test_dynamic_context_window_drops_only_oldest_complete_turn_pairs() -> None:
    history = tuple(
        ConversationMessage(
            f"{index + 1:032x}",
            "user" if index % 2 == 0 else "assistant",
            f"message-{index}-" + "x" * 400,
        )
        for index in range(8)
    )
    plan = ContextCompiler().compile(
        "current turn",
        history=history,
        model_profile=_profile(native=4_096, operational=4_096, output=256),
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    assert plan.windowing_strategy == "completed-turn-collapse-v2"
    assert plan.collapse_projection is not None
    assert 0 < plan.included_history_message_count < len(history)
    assert plan.included_history_message_count % 2 == 0
    assert plan.estimated_input_tokens <= plan.policy.compact_target_tokens
    assert plan.provider_messages()[-1] == {
        "role": "user",
        "content": "current turn",
    }
    included_contents = [
        message["content"] for message in plan.provider_messages()[1:-1]
    ]
    assert history[-1].content in included_contents
    assert history[0].content not in included_contents
    assert plan.collapse_projection.collapsed_message_ids == tuple(
        message.message_id
        for message in history[: -plan.included_history_message_count]
    )
    assert plan.collapse_projection.preserved_message_ids == tuple(
        message.message_id
        for message in history[-plan.included_history_message_count :]
    )
    assert plan.collapse_projection.projection_digest in plan.provider_messages()[0]["content"]


def test_section_count_also_forces_a_bounded_complete_pair_tail_window() -> None:
    history = tuple(
        ConversationMessage(
            f"{index + 1:032x}",
            "user" if index % 2 == 0 else "assistant",
            f"m{index}",
        )
        for index in range(130)
    )
    plan = ContextCompiler().compile(
        "current",
        history=history,
        model_profile=_profile(),
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
    )

    assert plan.windowing_strategy == "completed-turn-collapse-v2"
    assert plan.collapse_projection is not None
    assert plan.included_history_message_count <= 124
    assert plan.included_history_message_count % 2 == 0
    assert len(plan.sections) <= 128
    assert plan.provider_messages()[-2]["content"] == history[-1].content


def test_reactive_projection_collapses_to_only_the_newest_complete_pair() -> None:
    history = tuple(
        ConversationMessage(
            f"{index + 1:032x}",
            "user" if index % 2 == 0 else "assistant",
            f"turn-{index}",
        )
        for index in range(8)
    )
    plan = ContextCompiler().compile(
        "current",
        history=history,
        model_profile=_profile(),
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        force_compact=True,
        collapse_to_recent=True,
    )

    assert plan.windowing_strategy == "completed-turn-collapse-v2"
    assert plan.included_history_message_count == 2
    assert plan.collapse_projection is not None
    assert plan.collapse_projection.preserved_message_ids == (
        history[-2].message_id,
        history[-1].message_id,
    )
    assert plan.collapse_projection.collapsed_message_ids == tuple(
        item.message_id for item in history[:-2]
    )

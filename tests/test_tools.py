"""Single-source ToolSpec contract tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_builder_v2.tools import (
    PROTOTYPE_ECHO_SPEC,
    EffectiveToolSet,
    ToolCatalog,
    ToolPolicy,
    ToolUseContext,
    prototype_effective_toolset,
    prototype_tools,
    toolset_digest,
)


def test_echo_schema_validation_and_execution_share_one_spec() -> None:
    provider = PROTOTYPE_ECHO_SPEC.ollama_definition()
    function = provider["function"]

    assert function["name"] == PROTOTYPE_ECHO_SPEC.provider_name
    assert function["description"] == PROTOTYPE_ECHO_SPEC.description
    assert function["parameters"] == PROTOTYPE_ECHO_SPEC.canonical_manifest()[
        "input_schema"
    ]
    assert prototype_tools().execute("builtin/echo", {"text": "hello"}).content == "hello"


def test_echo_utf8_byte_limit_and_toolset_digest_fail_closed() -> None:
    registry = prototype_tools()
    exact = registry.execute("builtin/echo", {"text": "x" * 8_192})
    failed = registry.execute("builtin/echo", {"text": "界" * 2_731})
    assert exact.outcome == "succeeded"
    assert len(exact.content) == 8_192
    assert failed.outcome == "failed"
    assert "byte limit" in failed.content

    base = toolset_digest((PROTOTYPE_ECHO_SPEC,))
    changed = toolset_digest(
        (replace(PROTOTYPE_ECHO_SPEC, description="Changed contract"),)
    )
    assert base != changed


def test_catalog_policy_resolver_filters_before_exposure() -> None:
    catalog = ToolCatalog.create((PROTOTYPE_ECHO_SPEC,))
    allowed = EffectiveToolSet.resolve(
        catalog,
        ToolPolicy(
            revision="test-v1",
            allowed_tool_ids=("builtin/echo",),
            allowed_risks=("read_only",),
        ),
    )
    denied = EffectiveToolSet.resolve(
        catalog,
        ToolPolicy(
            revision="test-v2",
            allowed_tool_ids=("builtin/echo",),
            denied_tool_ids=("builtin/echo",),
            allowed_risks=("read_only",),
        ),
    )
    assert allowed == prototype_effective_toolset() or allowed.specs == (
        PROTOTYPE_ECHO_SPEC,
    )
    assert denied.specs == ()
    assert denied.toolset_digest == toolset_digest(())
    with pytest.raises(ValueError, match="unknown Tool"):
        EffectiveToolSet.resolve(
            catalog,
            ToolPolicy(
                revision="test-v3",
                allowed_tool_ids=("foreign/tool",),
                allowed_risks=("read_only",),
            ),
        )


def test_tool_use_context_is_reference_only_and_fail_closed() -> None:
    effective = prototype_effective_toolset()
    context = ToolUseContext(
        agent_id="00000000-0000-4000-8000-000000000001",
        capsule_generation=1,
        conversation_id="1" * 32,
        run_id="2" * 32,
        call_id="3" * 32,
        tool_id="builtin/echo",
        toolset_digest=effective.toolset_digest,
        policy_digest=effective.policy_digest,
        arguments_digest="4" * 64,
        expires_at_milliseconds=1,
    )
    assert not hasattr(context, "environment")
    assert not hasattr(context, "executor")
    with pytest.raises(ValueError, match="ToolUseContext"):
        replace(context, toolset_digest="not-a-digest")

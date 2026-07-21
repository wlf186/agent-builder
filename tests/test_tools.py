"""Single-source ToolSpec contract tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_builder_v2.tools import (
    PROTOTYPE_ECHO_SPEC,
    PROTOTYPE_ECHO_SPEC_V2,
    EffectiveToolSet,
    ToolCatalog,
    ToolPolicy,
    ToolUseContext,
    project_tool_result,
    prototype_effective_toolset,
    prototype_tools,
    runtime_tool_specs,
    runtime_tools,
    toolset_digest,
    validate_tool_result_projection,
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
        preview_digest="5" * 64,
        expires_at_milliseconds=1,
    )
    assert not hasattr(context, "environment")
    assert not hasattr(context, "executor")
    with pytest.raises(ValueError, match="ToolUseContext"):
        replace(context, toolset_digest="not-a-digest")


def test_tool_result_projection_is_deterministic_and_preserves_receipt() -> None:
    small = project_tool_result(PROTOTYPE_ECHO_SPEC, "call-small", "hello")
    assert small.content == "hello"
    assert small.original_bytes == 5
    assert small.truncated is False
    assert small.truncation_reason == "none"
    assert validate_tool_result_projection(PROTOTYPE_ECHO_SPEC, small) == small

    canonical = "界" * 2_730
    first = project_tool_result(PROTOTYPE_ECHO_SPEC, "call-large", canonical)
    second = project_tool_result(PROTOTYPE_ECHO_SPEC, "call-large", canonical)
    assert first == second
    assert first.truncated is True
    assert first.original_bytes == len(canonical.encode("utf-8"))
    assert first.content_digest in first.content
    assert "call_id=call-large" in first.content
    assert "reason=provider_projection_limit" in first.content
    assert len(first.content.encode("utf-8")) <= 4_096
    assert canonical not in first.content
    assert validate_tool_result_projection(PROTOTYPE_ECHO_SPEC, first) == first
    with pytest.raises(ValueError, match="digest changed"):
        validate_tool_result_projection(
            PROTOTYPE_ECHO_SPEC,
            replace(first, projection_digest="0" * 64),
        )


def test_tool_v2_digest_remains_replay_stable_after_projection_contract() -> None:
    assert toolset_digest((PROTOTYPE_ECHO_SPEC_V2,)) == (
        "efff9ca9590b7aa09705b0fc5256ee0f525577f2b4888e4c1afb4d397859b818"
    )
    assert PROTOTYPE_ECHO_SPEC.canonical_manifest()[
        "result_projection"
    ] == "identity_or_digest_placeholder_v1"


def test_runtime_catalog_brokers_capabilities_and_rejects_surrogates() -> None:
    calls: list[tuple[str, dict[str, str | int | bool], str]] = []

    def brokered(
        tool_id: str, arguments: dict[str, str | int | bool], call_id: str
    ) -> object:
        calls.append((tool_id, arguments, call_id))
        from agent_builder_v2.tools import ToolResult

        return ToolResult("succeeded", "{}")

    specs = runtime_tool_specs()
    assert [item.tool_id for item in specs] == [
        "agent/delegate", "builtin/echo", "document/extract_text", "exec/run", "extension/call", "file/edit", "file/glob", "file/grep",
        "file/read_text", "file/stat", "file/write", "skill/run",
    ]
    registry = runtime_tools(specs, brokered)  # type: ignore[arg-type]
    result = registry.execute(
        "file/glob", {"pattern": "**/*.txt"}, call_id="glob-call"
    )
    assert result.outcome == "succeeded"
    assert calls == [("file/glob", {"pattern": "**/*.txt"}, "glob-call")]
    command = registry.execute(
        "exec/run", {"command_id": "runtime-compile"}, call_id="exec-call"
    )
    assert command.outcome == "succeeded"
    assert calls[-1] == (
        "exec/run", {"command_id": "runtime-compile"}, "exec-call"
    )
    failed = registry.execute(
        "file/glob", {"pattern": "\ud800"}, call_id="invalid-call"
    )
    assert failed.outcome == "failed"
    assert "valid UTF-8" in failed.content

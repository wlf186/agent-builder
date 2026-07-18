"""Single-source ToolSpec contract tests."""

from __future__ import annotations

from dataclasses import replace

from agent_builder_v2.tools import (
    PROTOTYPE_ECHO_SPEC,
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
        (replace(PROTOTYPE_ECHO_SPEC, contract_version="2"),)
    )
    assert base != changed

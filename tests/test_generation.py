"""Contracts for the trusted phase-bound generation policy."""

from __future__ import annotations

import pytest

from agent_builder_v2.generation import (
    GENERATION_POLICY_VERSION,
    RESPONSE_SAMPLED_PHASE,
    RESPONSE_TEMPERATURE,
    RESPONSE_TOP_P,
    TOOL_DETERMINISTIC_PHASE,
    generation_options_for,
    generation_policy_manifest,
)


def test_no_tool_phase_uses_bounded_response_sampling() -> None:
    options = generation_options_for(
        has_tools=False,
        deterministic_temperature=0,
        seed=0,
    )

    assert options == {
        "temperature": 0.7,
        "top_p": 0.8,
        "seed": 0,
    }
    assert options["temperature"] == RESPONSE_TEMPERATURE
    assert options["top_p"] == RESPONSE_TOP_P

    # Callers receive a fresh mapping and cannot mutate the sealed policy.
    options["temperature"] = 0
    assert generation_options_for(
        has_tools=False,
        deterministic_temperature=0,
        seed=0,
    )["temperature"] == 0.7


def test_tool_phase_is_deterministic_and_has_no_sampling_tail() -> None:
    options = generation_options_for(
        has_tools=True,
        deterministic_temperature=0,
        seed=0,
    )

    assert options == {"temperature": 0, "seed": 0}
    assert "top_p" not in options


def test_generation_manifest_binds_both_phases_and_selection_rule() -> None:
    manifest = generation_policy_manifest(
        deterministic_temperature=0,
        seed=0,
    )

    assert manifest == {
        "version": GENERATION_POLICY_VERSION,
        "phases": {
            TOOL_DETERMINISTIC_PHASE: {"temperature": 0, "seed": 0},
            RESPONSE_SAMPLED_PHASE: {
                "temperature": 0.7,
                "top_p": 0.8,
                "seed": 0,
            },
        },
        "selection": "provider-call-has-tools-v1",
    }


@pytest.mark.parametrize(
    ("has_tools", "temperature", "seed"),
    [
        ("no", 0, 0),
        (False, 1, 0),
        (True, 0, 1),
        (True, False, 0),
        (True, 0, False),
    ],
)
def test_generation_policy_rejects_untrusted_variants(
    has_tools: object,
    temperature: int,
    seed: int,
) -> None:
    with pytest.raises(ValueError, match="trusted generation policy"):
        generation_options_for(
            has_tools=has_tools,  # type: ignore[arg-type]
            deterministic_temperature=temperature,
            seed=seed,
        )

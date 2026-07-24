"""Trusted, phase-bound generation policy for Ollama requests.

Structured Tool selection benefits from deterministic decoding, while ordinary
answer generation needs bounded sampling to avoid greedy repetition loops in
small models. The phase is derived solely from the frozen effective ToolSet for
the exact provider call; request data cannot provide arbitrary model options.
"""

from __future__ import annotations

GENERATION_POLICY_VERSION = "tool-phase-generation-v2"
TOOL_DETERMINISTIC_PHASE = "tool-deterministic-v1"
RESPONSE_SAMPLED_PHASE = "response-sampled-v1"
RESPONSE_TEMPERATURE = 1.0
RESPONSE_TOP_P = 0.95
RESPONSE_TOP_K = 20
RESPONSE_PRESENCE_PENALTY = 1.5


def generation_options_for(
    *, has_tools: bool, deterministic_temperature: int, seed: int
) -> dict[str, int | float]:
    """Return a fresh options mapping for the exact frozen provider phase."""

    if (
        not isinstance(has_tools, bool)
        or not isinstance(deterministic_temperature, int)
        or isinstance(deterministic_temperature, bool)
        or deterministic_temperature != 0
        or not isinstance(seed, int)
        or isinstance(seed, bool)
        or seed != 0
    ):
        raise ValueError("invalid trusted generation policy")
    if not has_tools:
        return {
            "temperature": RESPONSE_TEMPERATURE,
            "top_p": RESPONSE_TOP_P,
            "top_k": RESPONSE_TOP_K,
            "presence_penalty": RESPONSE_PRESENCE_PENALTY,
            "seed": seed,
        }
    return {"temperature": deterministic_temperature, "seed": seed}


def generation_policy_manifest(
    *, deterministic_temperature: int, seed: int
) -> dict[str, object]:
    return {
        "version": GENERATION_POLICY_VERSION,
        "phases": {
            TOOL_DETERMINISTIC_PHASE: generation_options_for(
                has_tools=True,
                deterministic_temperature=deterministic_temperature,
                seed=seed,
            ),
            RESPONSE_SAMPLED_PHASE: generation_options_for(
                has_tools=False,
                deterministic_temperature=deterministic_temperature,
                seed=seed,
            ),
        },
        "selection": "provider-call-has-tools-v1",
    }


__all__ = [
    "GENERATION_POLICY_VERSION",
    "RESPONSE_PRESENCE_PENALTY",
    "RESPONSE_SAMPLED_PHASE",
    "RESPONSE_TEMPERATURE",
    "RESPONSE_TOP_K",
    "RESPONSE_TOP_P",
    "TOOL_DETERMINISTIC_PHASE",
    "generation_options_for",
    "generation_policy_manifest",
]

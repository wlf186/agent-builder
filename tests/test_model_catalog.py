"""Trusted ModelCatalog validation and profile-switching contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_builder_v2.capsule import PROTOTYPE_AGENT_ID
from agent_builder_v2.context import (
    ContextCompiler,
    ContextPlanError,
    ConversationMessage,
    ModelProfile,
)
from agent_builder_v2.model_catalog import (
    ModelCatalog,
    ModelCatalogEntry,
    ModelCatalogError,
    ProviderEndpoint,
    default_model_catalog,
)
from agent_builder_v2.tools import prototype_tool_specs


def _catalog() -> ModelCatalog:
    endpoint = ProviderEndpoint("trusted", "ollama", "iollama", 11_434)
    return ModelCatalog.create(
        endpoints=(endpoint,),
        models=(
            ModelCatalogEntry(
                "large-tools",
                "ollama",
                "model-large:1b",
                endpoint.endpoint_id,
                32_768,
                2_048,
            ),
            ModelCatalogEntry(
                "small-text",
                "ollama",
                "model-small:1b",
                endpoint.endpoint_id,
                8_192,
                512,
                ("completion", "streaming"),
            ),
        ),
        default_model_id="large-tools",
    )


def _profile(entry: ModelCatalogEntry, digest: str, native: int) -> ModelProfile:
    return ModelProfile(
        provider=entry.provider,
        model=entry.provider_model,
        model_digest=digest,
        native_context_tokens=native,
        operational_context_tokens=min(native, entry.operational_context_cap),
        max_output_tokens=entry.output_token_cap,
        profile_source="test-catalog",
        catalog_model_id=entry.model_id,
        supports_tools=entry.supports_tools,
        generation_options_digest=entry.generation_options_digest,
    )


def test_default_catalog_is_stable_and_withholds_network_target() -> None:
    first = default_model_catalog()
    second = default_model_catalog()

    assert first.digest == second.digest
    assert first.select().provider_model == "qwen3.5:2b"
    public = first.public_metadata()
    assert public["default_model_id"] == "qwen3.5:2b"
    assert "endpoints" not in public
    assert "host" not in str(public)
    assert "11434" not in str(public)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: replace(value, default_model_id="not-trusted"),
        lambda value: replace(value, models=value.models + (value.models[0],)),
        lambda value: replace(
            value,
            endpoints=(ProviderEndpoint("other", "ollama", "attacker.example", 80),),
        ),
    ],
)
def test_catalog_rejects_unknown_duplicate_or_rebound_entries(mutation: object) -> None:
    with pytest.raises(ModelCatalogError):
        mutation(_catalog())  # type: ignore[operator]


def test_selection_accepts_only_catalog_ids_and_never_an_endpoint() -> None:
    catalog = _catalog()

    assert catalog.select("small-text").provider_model == "model-small:1b"
    for candidate in (
        "http://attacker.invalid/model",
        "model-large:1b?host=attacker",
        "unknown",
        "../large-tools",
    ):
        with pytest.raises(ModelCatalogError):
            catalog.select(candidate)


def test_turn_recompile_uses_complete_history_and_selected_profile() -> None:
    catalog = _catalog()
    large = _profile(catalog.select("large-tools"), "a" * 64, 65_536)
    small = _profile(catalog.select("small-text"), "b" * 64, 16_384)
    history = (
        ConversationMessage("1" * 32, "user", "first question"),
        ConversationMessage("2" * 32, "assistant", "first answer"),
    )
    compiler = ContextCompiler()
    first = compiler.compile(
        "next",
        model_profile=large,
        tools=prototype_tool_specs(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        history=history,
    )
    switched = compiler.compile(
        "next",
        model_profile=small,
        tools=(),
        agent_id=PROTOTYPE_AGENT_ID,
        capsule_generation=1,
        history=history,
    )

    assert first.history_message_count == switched.history_message_count == 2
    assert first.model_profile.catalog_model_id == "large-tools"
    assert switched.model_profile.catalog_model_id == "small-text"
    assert first.model_profile.profile_digest != switched.model_profile.profile_digest
    assert first.reference.digest != switched.reference.digest
    assert first.policy.hard_input_tokens > switched.policy.hard_input_tokens
    assert first.tools and switched.tools == ()
    with pytest.raises(ContextPlanError, match="invalid context compilation input"):
        compiler.compile(
            "must not expose tools",
            model_profile=small,
            tools=prototype_tool_specs(),
            agent_id=PROTOTYPE_AGENT_ID,
            capsule_generation=1,
        )

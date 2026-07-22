"""Typed context count domains and compatibility guards."""

from __future__ import annotations

import pytest

from agent_builder_v2.context_counts import (
    AdmissionUpperBound,
    ContextCountError,
    CountScope,
    NextTurnProjection,
    ProviderObservedUsage,
    SoftContextEstimate,
)


def _scope(seed: str = "a") -> CountScope:
    return CountScope(
        profile_digest=seed * 64,
        renderer_version="ordered-sections-v5",
        toolset_digest="b" * 64,
        policy_digest="c" * 64,
    )


def test_count_scope_digest_is_canonical_and_covers_every_field() -> None:
    scope = _scope()

    assert len(scope.scope_digest) == 64
    assert scope.scope_digest == _scope().scope_digest
    for changed in (
        CountScope("e" * 64, scope.renderer_version, scope.toolset_digest, scope.policy_digest),
        CountScope(
            scope.profile_digest,
            "ordered-sections-v6",
            scope.toolset_digest,
            scope.policy_digest,
        ),
        CountScope(scope.profile_digest, scope.renderer_version, "e" * 64, scope.policy_digest),
        CountScope(scope.profile_digest, scope.renderer_version, scope.toolset_digest, "e" * 64),
    ):
        assert changed.scope_digest != scope.scope_digest


def test_admission_upper_bound_is_component_bound_and_safety_only() -> None:
    bound = AdmissionUpperBound(
        scope=_scope(),
        basis="utf8-bytes-upper-bound-v1",
        request_schema_digest="d" * 64,
        encoded_request_bytes=1_000,
        template_reserve_tokens=256,
        tool_growth_reserve_tokens=512,
        upper_bound_tokens=1_768,
        hard_input_tokens=2_000,
    )

    bound.require_fits()
    assert bound.to_dict()["basis"] == "utf8-bytes-upper-bound-v1"
    with pytest.raises(ContextCountError, match="components"):
        AdmissionUpperBound(
            scope=_scope(),
            basis="utf8-bytes-upper-bound-v1",
            request_schema_digest="d" * 64,
            encoded_request_bytes=1_000,
            template_reserve_tokens=256,
            tool_growth_reserve_tokens=512,
            upper_bound_tokens=1_767,
            hard_input_tokens=2_000,
        )


def test_provider_usage_is_exact_request_scoped_and_partial_is_zero() -> None:
    usage = ProviderObservedUsage(
        scope=_scope(),
        request_digest="d" * 64,
        input_tokens=900,
        output_tokens=100,
        complete=True,
        purpose="recovery",
        attempt=1,
    )
    assert usage.to_dict()["purpose"] == "recovery"

    with pytest.raises(ContextCountError, match="partial"):
        ProviderObservedUsage(
            scope=_scope(),
            request_digest="d" * 64,
            input_tokens=1,
            output_tokens=0,
            complete=False,
            purpose="normal",
            attempt=0,
        )


def test_soft_estimate_rejects_cross_profile_comparisons() -> None:
    estimate = SoftContextEstimate(
        scope=_scope(),
        availability="available",
        basis="provider-observed-calibration-v1",
        estimated_tokens=1_000,
        error_margin_tokens=100,
        sample_count=4,
    )

    assert estimate.upper_tokens_for(_scope()) == 1_100
    with pytest.raises(ContextCountError, match="scope mismatch"):
        estimate.upper_tokens_for(_scope("e"))
    with pytest.raises(ContextCountError, match="unavailable"):
        SoftContextEstimate.unavailable(_scope()).upper_tokens_for(_scope())


def test_next_turn_projection_excludes_uncommitted_user_content() -> None:
    projection = NextTurnProjection(
        scope=_scope(),
        conversation_revision=7,
        availability="available",
        fixed_context_tokens=2_000,
        safe_user_tokens=4_000,
        compact_before_user_tokens=3_000,
        source="provider-observed-calibration-v1",
    )

    assert set(projection.to_dict()) == {
        "version",
        "scope",
        "conversation_revision",
        "availability",
        "fixed_context_tokens",
        "safe_user_tokens",
        "compact_before_user_tokens",
        "source",
    }

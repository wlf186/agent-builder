"""Typed capacity count domains that cannot be compared accidentally."""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict, deque
import hashlib
import json
import math
import re
from typing import Literal


_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9._:/+-]{1,128}$")

CountAvailability = Literal["available", "unavailable"]
ProviderCallPurpose = Literal["normal", "recovery", "summary"]
SoftEstimateBasis = Literal[
    "provider-tokenizer-v1",
    "provider-observed-calibration-v1",
]


class ContextCountError(ValueError):
    """Count values are malformed or belong to incompatible domains."""


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContextCountError(f"invalid {field}")
    return value


def _version(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
        raise ContextCountError(f"invalid {field}")
    return value


def _count(value: int, field: str, *, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContextCountError(f"invalid {field}")
    return value


@dataclass(frozen=True, slots=True)
class CountScope:
    """Identity shared only by counts that may be compared."""

    profile_digest: str
    renderer_version: str
    toolset_digest: str
    policy_digest: str

    def __post_init__(self) -> None:
        _digest(self.profile_digest, "profile digest")
        _version(self.renderer_version, "renderer version")
        _digest(self.toolset_digest, "ToolSet digest")
        _digest(self.policy_digest, "policy digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "profile_digest": self.profile_digest,
            "renderer_version": self.renderer_version,
            "toolset_digest": self.toolset_digest,
            "policy_digest": self.policy_digest,
        }

    @property
    def scope_digest(self) -> str:
        """Compact indexed identity for the complete comparison scope."""

        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(
            b"agent-builder-context-count-scope-v1\0" + payload
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class AdmissionUpperBound:
    """Safety-only token upper bound derived from exact encoded request data."""

    scope: CountScope
    basis: str
    request_schema_digest: str
    encoded_request_bytes: int
    template_reserve_tokens: int
    tool_growth_reserve_tokens: int
    upper_bound_tokens: int
    hard_input_tokens: int
    version: str = "admission-upper-bound-v1"

    def __post_init__(self) -> None:
        _version(self.version, "admission version")
        if self.basis != "utf8-bytes-upper-bound-v1":
            raise ContextCountError("invalid admission basis")
        _digest(self.request_schema_digest, "request schema digest")
        _count(self.encoded_request_bytes, "encoded request bytes", allow_zero=False)
        _count(self.template_reserve_tokens, "template reserve")
        _count(self.tool_growth_reserve_tokens, "Tool growth reserve")
        _count(self.upper_bound_tokens, "upper bound", allow_zero=False)
        _count(self.hard_input_tokens, "hard input limit", allow_zero=False)
        if self.upper_bound_tokens != (
            self.encoded_request_bytes
            + self.template_reserve_tokens
            + self.tool_growth_reserve_tokens
        ):
            raise ContextCountError("admission components do not match total")

    def require_fits(self) -> None:
        if self.upper_bound_tokens > self.hard_input_tokens:
            raise ContextCountError("admission upper bound exceeds hard input limit")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "basis": self.basis,
            "scope": self.scope.to_dict(),
            "request_schema_digest": self.request_schema_digest,
            "encoded_request_bytes": self.encoded_request_bytes,
            "template_reserve_tokens": self.template_reserve_tokens,
            "tool_growth_reserve_tokens": self.tool_growth_reserve_tokens,
            "upper_bound_tokens": self.upper_bound_tokens,
            "hard_input_tokens": self.hard_input_tokens,
        }


@dataclass(frozen=True, slots=True)
class ProviderObservedUsage:
    """Provider token counts authoritative only for one exact request digest."""

    scope: CountScope
    request_digest: str
    input_tokens: int
    output_tokens: int
    complete: bool
    purpose: ProviderCallPurpose
    attempt: int
    version: str = "provider-observed-usage-v1"

    def __post_init__(self) -> None:
        _version(self.version, "observed usage version")
        _digest(self.request_digest, "request digest")
        _count(self.input_tokens, "provider input tokens")
        _count(self.output_tokens, "provider output tokens")
        if not isinstance(self.complete, bool):
            raise ContextCountError("invalid provider usage completeness")
        if self.purpose not in {"normal", "recovery", "summary"}:
            raise ContextCountError("invalid provider call purpose")
        if self.attempt not in {0, 1}:
            raise ContextCountError("invalid provider call attempt")
        if not self.complete and (self.input_tokens != 0 or self.output_tokens != 0):
            raise ContextCountError("partial usage cannot claim token counts")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "scope": self.scope.to_dict(),
            "request_digest": self.request_digest,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "complete": self.complete,
            "purpose": self.purpose,
            "attempt": self.attempt,
        }


@dataclass(frozen=True, slots=True)
class SoftContextEstimate:
    """Model-compatible estimate used only for soft policy and prediction."""

    scope: CountScope
    availability: CountAvailability
    basis: SoftEstimateBasis | None
    estimated_tokens: int | None
    error_margin_tokens: int | None
    sample_count: int
    version: str = "soft-context-estimate-v1"

    def __post_init__(self) -> None:
        _version(self.version, "soft estimate version")
        _count(self.sample_count, "sample count")
        if self.availability == "unavailable":
            if any(
                value is not None
                for value in (self.basis, self.estimated_tokens, self.error_margin_tokens)
            ) or self.sample_count != 0:
                raise ContextCountError("unavailable estimate carries values")
            return
        if self.availability != "available" or self.basis not in {
            "provider-tokenizer-v1",
            "provider-observed-calibration-v1",
        }:
            raise ContextCountError("invalid soft estimate basis")
        if self.estimated_tokens is None or self.error_margin_tokens is None:
            raise ContextCountError("available estimate is incomplete")
        _count(self.estimated_tokens, "soft estimate")
        _count(self.error_margin_tokens, "soft estimate margin")
        if self.sample_count < 1:
            raise ContextCountError("available estimate has no samples")

    @classmethod
    def unavailable(cls, scope: CountScope) -> SoftContextEstimate:
        return cls(scope, "unavailable", None, None, None, 0)

    def upper_tokens_for(self, scope: CountScope) -> int:
        if scope != self.scope:
            raise ContextCountError("soft estimate scope mismatch")
        if (
            self.availability != "available"
            or self.estimated_tokens is None
            or self.error_margin_tokens is None
        ):
            raise ContextCountError("soft estimate is unavailable")
        return self.estimated_tokens + self.error_margin_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "scope": self.scope.to_dict(),
            "availability": self.availability,
            "basis": self.basis,
            "estimated_tokens": self.estimated_tokens,
            "error_margin_tokens": self.error_margin_tokens,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class SoftContextCalibration:
    """Bounded profile-scoped mapping from admission units to Provider tokens."""

    scope: CountScope
    ratio_parts_per_million: int
    error_parts_per_million: int
    error_floor_tokens: int
    sample_count: int
    version: str = "provider-observed-calibration-v1"

    def __post_init__(self) -> None:
        _version(self.version, "calibration version")
        if (
            not 1 <= self.ratio_parts_per_million <= 1_000_000
            or not 0 <= self.error_parts_per_million <= 1_000_000
            or not 0 <= self.error_floor_tokens <= 65_536
            or not 1 <= self.sample_count <= 64
        ):
            raise ContextCountError("invalid soft context calibration")

    def estimate(self, admission_upper_bound_tokens: int) -> SoftContextEstimate:
        _count(admission_upper_bound_tokens, "admission upper bound", allow_zero=False)
        estimated = math.ceil(
            admission_upper_bound_tokens * self.ratio_parts_per_million / 1_000_000
        )
        margin = max(
            self.error_floor_tokens,
            math.ceil(
                estimated * self.error_parts_per_million / 1_000_000
            ),
        )
        return SoftContextEstimate(
            scope=self.scope,
            availability="available",
            basis="provider-observed-calibration-v1",
            estimated_tokens=estimated,
            error_margin_tokens=margin,
            sample_count=self.sample_count,
        )


class SoftContextCalibrationRegistry:
    """Small in-memory LRU; samples never cross a CountScope."""

    def __init__(self, maximum_scopes: int = 32, samples_per_scope: int = 16) -> None:
        if not 1 <= maximum_scopes <= 128 or not 1 <= samples_per_scope <= 64:
            raise ContextCountError("invalid calibration registry bounds")
        self._maximum_scopes = maximum_scopes
        self._samples_per_scope = samples_per_scope
        self._samples: OrderedDict[CountScope, deque[tuple[int, int]]] = OrderedDict()

    def observe(
        self, scope: CountScope, *, admission_upper_bound_tokens: int, actual_input_tokens: int
    ) -> None:
        _count(admission_upper_bound_tokens, "admission sample", allow_zero=False)
        _count(actual_input_tokens, "actual input sample", allow_zero=False)
        bucket = self._samples.setdefault(scope, deque(maxlen=self._samples_per_scope))
        bucket.append((admission_upper_bound_tokens, actual_input_tokens))
        self._samples.move_to_end(scope)
        while len(self._samples) > self._maximum_scopes:
            self._samples.popitem(last=False)

    def calibration_for(self, scope: CountScope) -> SoftContextCalibration | None:
        bucket = self._samples.get(scope)
        if not bucket:
            return None
        self._samples.move_to_end(scope)
        ratios = [
            math.ceil(actual * 1_000_000 / admission)
            for admission, actual in bucket
        ]
        # The maximum observed ratio plus a 10%/256-token margin avoids using
        # an optimistic average for a soft trigger while remaining separate
        # from hard admission.
        return SoftContextCalibration(
            scope=scope,
            ratio_parts_per_million=min(1_000_000, max(ratios)),
            error_parts_per_million=100_000,
            error_floor_tokens=256,
            sample_count=len(bucket),
        )


@dataclass(frozen=True, slots=True)
class NextTurnProjection:
    """Committed fixed context immediately before the next user message."""

    scope: CountScope
    conversation_revision: int
    availability: CountAvailability
    fixed_context_tokens: int | None
    safe_user_tokens: int | None
    compact_before_user_tokens: int | None
    source: str | None
    version: str = "next-turn-projection-v1"

    def __post_init__(self) -> None:
        _version(self.version, "next-turn projection version")
        _count(self.conversation_revision, "conversation revision")
        if self.availability == "unavailable":
            if any(
                value is not None
                for value in (
                    self.fixed_context_tokens,
                    self.safe_user_tokens,
                    self.compact_before_user_tokens,
                    self.source,
                )
            ):
                raise ContextCountError("unavailable projection carries values")
            return
        if self.availability != "available":
            raise ContextCountError("invalid next-turn availability")
        if not isinstance(self.source, str):
            raise ContextCountError("next-turn projection has no source")
        _version(self.source, "next-turn projection source")
        for value, field in (
            (self.fixed_context_tokens, "fixed context"),
            (self.safe_user_tokens, "safe user capacity"),
            (self.compact_before_user_tokens, "compact user capacity"),
        ):
            if value is None:
                raise ContextCountError(f"missing {field}")
            _count(value, field)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "scope": self.scope.to_dict(),
            "conversation_revision": self.conversation_revision,
            "availability": self.availability,
            "fixed_context_tokens": self.fixed_context_tokens,
            "safe_user_tokens": self.safe_user_tokens,
            "compact_before_user_tokens": self.compact_before_user_tokens,
            "source": self.source,
        }


__all__ = [
    "AdmissionUpperBound",
    "ContextCountError",
    "CountScope",
    "NextTurnProjection",
    "ProviderObservedUsage",
    "SoftContextEstimate",
    "SoftContextCalibration",
    "SoftContextCalibrationRegistry",
]

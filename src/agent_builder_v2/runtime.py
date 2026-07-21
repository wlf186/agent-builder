"""Immutable per-Turn runtime snapshot owned by the trusted Control Plane."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .context import ContextPlan, ModelProfile
from .contracts import LoopLimits
from .tools import (
    EffectiveToolSet,
    ToolCatalog,
    ToolPolicy,
    ToolSpec,
    toolset_digest,
)


_DIGEST = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class TurnRuntimeSnapshot:
    """All execution-affecting inputs frozen before a Turn is admitted."""

    agent_id: str
    capsule_generation: int
    model_profile: ModelProfile
    effective_tools: tuple[ToolSpec, ...]
    tool_catalog_digest: str
    tool_policy_digest: str
    context_plan: ContextPlan
    loop_limits: LoopLimits
    max_total_input_tokens: int
    max_total_output_tokens: int
    wall_timeout_seconds: int
    projection_reason: str

    def __post_init__(self) -> None:
        canonical_tools = tuple(sorted(self.effective_tools, key=lambda item: item.tool_id))
        if (
            not self.agent_id
            or self.context_plan.agent_id != self.agent_id
            or self.context_plan.capsule_generation != self.capsule_generation
            or self.context_plan.model_profile != self.model_profile
            or canonical_tools != self.effective_tools
            or self.context_plan.tools != self.effective_tools
            or self.context_plan.reference.toolset_digest
            != toolset_digest(self.effective_tools)
            or _DIGEST.fullmatch(self.tool_catalog_digest) is None
            or _DIGEST.fullmatch(self.tool_policy_digest) is None
            or not isinstance(self.max_total_input_tokens, int)
            or isinstance(self.max_total_input_tokens, bool)
            or self.max_total_input_tokens
            != self.context_plan.policy.hard_input_tokens
            * self.loop_limits.max_model_iterations
            or not isinstance(self.max_total_output_tokens, int)
            or isinstance(self.max_total_output_tokens, bool)
            or self.max_total_output_tokens
            != self.model_profile.max_output_tokens
            * self.loop_limits.max_model_iterations
            or not isinstance(self.wall_timeout_seconds, int)
            or isinstance(self.wall_timeout_seconds, bool)
            or not 1 <= self.wall_timeout_seconds <= 3_600
            or self.projection_reason not in {
                "admission", "manual_compact", "semantic_summary"
            }
        ):
            raise ValueError("invalid Turn runtime snapshot")

    @classmethod
    def create(
        cls,
        *,
        context_plan: ContextPlan,
        loop_limits: LoopLimits,
        wall_timeout_seconds: int,
        effective_toolset: EffectiveToolSet | None = None,
        projection_reason: str = "admission",
    ) -> TurnRuntimeSnapshot:
        if effective_toolset is None:
            catalog = ToolCatalog.create(context_plan.tools)
            policy = ToolPolicy(
                revision="snapshot-derived-v1",
                allowed_tool_ids=tuple(spec.tool_id for spec in context_plan.tools),
                allowed_risks=(
                    tuple(sorted({spec.risk for spec in context_plan.tools}))
                    or ("read_only",)
                ),
            )
            effective_toolset = EffectiveToolSet.resolve(catalog, policy)
        if effective_toolset.specs != context_plan.tools:
            raise ValueError("effective ToolSet does not match ContextPlan")
        return cls(
            agent_id=context_plan.agent_id,
            capsule_generation=context_plan.capsule_generation,
            model_profile=context_plan.model_profile,
            effective_tools=context_plan.tools,
            tool_catalog_digest=effective_toolset.catalog_digest,
            tool_policy_digest=effective_toolset.policy_digest,
            context_plan=context_plan,
            loop_limits=loop_limits,
            max_total_input_tokens=(
                context_plan.policy.hard_input_tokens
                * loop_limits.max_model_iterations
            ),
            max_total_output_tokens=(
                context_plan.model_profile.max_output_tokens
                * loop_limits.max_model_iterations
            ),
            wall_timeout_seconds=wall_timeout_seconds,
            projection_reason=projection_reason,
        )

    def public_metadata(self) -> dict[str, object]:
        return {
            "capsule_generation": self.capsule_generation,
            "model_id": self.model_profile.catalog_model_id or self.model_profile.model,
            "model_profile_digest": self.model_profile.profile_digest,
            "context_plan_id": self.context_plan.reference.plan_id,
            "context_plan_digest": self.context_plan.reference.digest,
            "toolset_digest": self.context_plan.reference.toolset_digest,
            "tool_catalog_digest": self.tool_catalog_digest,
            "tool_policy_digest": self.tool_policy_digest,
            "loop_limits": self.loop_limits.to_dict(),
            "max_total_input_tokens": self.max_total_input_tokens,
            "max_total_output_tokens": self.max_total_output_tokens,
            "wall_timeout_seconds": self.wall_timeout_seconds,
            "projection_reason": self.projection_reason,
        }


__all__ = ["TurnRuntimeSnapshot"]

---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-21
review_cycle: quarterly
---

# Context count domains

Context capacity uses four versioned domains. A numeric value without its domain, scope and availability
is invalid at API/UI boundaries.

| Domain | Authority and allowed use | Forbidden use |
| --- | --- | --- |
| `AdmissionUpperBound` | exact encoded Provider messages and actual Provider Tool schema, conservative UTF-8-byte fallback, template/Tool-growth reserves; hard safety admission only | soft autocompact timing or “real token” UI |
| `ProviderObservedUsage` | Provider terminal counts for one exact request digest; input/output, complete/partial and normal/recovery/summary are distinct | another request, model, renderer or future-turn total |
| `SoftContextEstimate` | tokenizer result or profile-scoped observed calibration plus explicit error margin; soft compaction and prediction only | hard safety admission or cross-profile comparison |
| `NextTurnProjection` | one committed Conversation revision immediately before the next user text; fixed context and safe/compact user capacity | uncommitted user content, current Run cumulative cost or browser-inferred arithmetic |

Every domain carries `profile_digest`, renderer version, ToolSet digest and compression-policy digest.
Comparisons reject a different scope. Provider-observed calibration/cache data also keys the model binary
digest and never survives a renderer/ToolSet/policy change. Public metadata may expose these identities,
basis/version/availability and bounded counts; it never exposes endpoint, prompt, Tool result or credentials.

The qualified Ollama interface reports terminal usage but exposes no separately qualified exact tokenizer
endpoint, so `ModelProfile.token_counting=provider-observed-only-v1`. A matching complete observation
calibrates the profile-scoped byte estimate with a bounded ratio, a 10% margin and a 256-token error floor.
Until that observation exists—or when its ratio, request completeness or scope is invalid—
`SoftContextEstimate` and `NextTurnProjection` are explicitly `unavailable`; code never substitutes the byte
upper bound. The authenticated `no-store` preview is recomputed from durable committed state and only caches
at Turn semantic boundaries, never per draft byte or token.

The preview first compiles the execution-capable EffectiveToolSet as the primary
`projection_mode=conservative_tools` scope. When that exact ToolSet lacks calibration, the service may also
compile the empty ToolSet and return a separately versioned
`next-turn-chat-only-projection-v1` object in `chat_only_projection`, but only when that empty-ToolSet scope
has its own complete calibration. Consumers must prefer an available primary projection. They may display
the independent fallback only as a labelled pure-chat baseline and must recompile before a Turn that enables
Tools. Ratios, margins, counts and capacity are never copied between the two ToolSet digests; malformed or
partially populated fallback objects are rejected rather than merged into the primary projection.

Existing `estimated_input_tokens` fields in `run.started`, context boundaries and `provider_usage` decode as
legacy `AdmissionUpperBound` values with `tool_growth_reserve_tokens=0`; they are retained for replay and
rollback but are not Provider actual. New metadata additionally emits `admission_count_version`,
`admission_basis`, `soft_count_version` and `soft_count_availability`. Decoder support precedes any writer
schema change.

Tool estimation serializes `ToolSpec.ollama_definition()`, which is the actual Provider schema. Internal
catalog IDs, risks, execution limits and policy fields remain in ToolSet/policy digests but do not masquerade
as Provider prompt tokens.

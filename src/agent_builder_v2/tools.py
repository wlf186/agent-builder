"""Project-owned Tool specifications, validation and local dispatch."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable

from .contracts import MAX_MESSAGE_BYTES


_TOOL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_CONCURRENCY = frozenset({"safe", "serialized"})
_RISK = frozenset({"read_only", "mutation", "execution"})
_VALUE_KIND = frozenset({"string", "integer", "boolean"})
_RESULT_KIND = frozenset({"text"})
_RESULT_TRUST = frozenset({"untrusted_tool_data"})
_PROGRESS_MODE = frozenset({"none", "bounded"})
_CANCELLATION = frozenset({"cooperative", "not_cancellable"})
_RESULT_PROJECTION = frozenset({"identity_or_digest_placeholder_v1"})
_POLICY_REVISION = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_RESOURCE_ID = re.compile(r"^[a-f0-9]{32}$")
_AGENT_ID = re.compile(r"^[a-f0-9-]{32,64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_PROVIDER_TOOL_RESULT_HISTORY_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class ToolInputField:
    """One field from the deliberately small Tool schema vocabulary."""

    name: str
    value_kind: str
    required: bool = True
    maximum_utf8_bytes: int | None = None
    minimum_integer: int | None = None
    maximum_integer: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _PROVIDER_NAME.fullmatch(self.name) is None
            or self.value_kind not in _VALUE_KIND
            or not isinstance(self.required, bool)
        ):
            raise ValueError("invalid Tool input field")
        if self.value_kind == "string":
            if (
                not isinstance(self.maximum_utf8_bytes, int)
                or isinstance(self.maximum_utf8_bytes, bool)
                or not 1 <= self.maximum_utf8_bytes <= 65_536
                or self.minimum_integer is not None
                or self.maximum_integer is not None
            ):
                raise ValueError("invalid Tool string field")
        elif self.value_kind == "integer":
            if (
                self.maximum_utf8_bytes is not None
                or not isinstance(self.minimum_integer, int)
                or isinstance(self.minimum_integer, bool)
                or not isinstance(self.maximum_integer, int)
                or isinstance(self.maximum_integer, bool)
                or self.minimum_integer > self.maximum_integer
                or not -(2**53) <= self.minimum_integer <= self.maximum_integer <= 2**53
            ):
                raise ValueError("invalid Tool integer field")
        elif any(
            item is not None
            for item in (
                self.maximum_utf8_bytes,
                self.minimum_integer,
                self.maximum_integer,
            )
        ):
            raise ValueError("invalid Tool boolean field")

    def validate(self, value: object) -> str | int | bool:
        if self.value_kind == "string":
            if not isinstance(value, str):
                raise ValueError(f"{self.name} must be a string")
            assert self.maximum_utf8_bytes is not None
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"{self.name} is not valid UTF-8") from exc
            if len(encoded) > self.maximum_utf8_bytes:
                raise ValueError(f"{self.name} exceeds its byte limit")
            return value
        if self.value_kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{self.name} must be an integer")
            assert self.minimum_integer is not None
            assert self.maximum_integer is not None
            if not self.minimum_integer <= value <= self.maximum_integer:
                raise ValueError(f"{self.name} is outside its integer range")
            return value
        if not isinstance(value, bool):
            raise ValueError(f"{self.name} must be a boolean")
        return value

    def json_schema(self) -> dict[str, object]:
        if self.value_kind == "string":
            assert self.maximum_utf8_bytes is not None
            return {
                "type": "string",
                "maxLength": self.maximum_utf8_bytes,
                "x-agent-builder-maxUtf8Bytes": self.maximum_utf8_bytes,
            }
        if self.value_kind == "integer":
            return {
                "type": "integer",
                "minimum": self.minimum_integer,
                "maximum": self.maximum_integer,
            }
        return {"type": "boolean"}

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value_kind": self.value_kind,
            "required": self.required,
            "schema": self.json_schema(),
        }


@dataclass(frozen=True)
class ToolSpec:
    """One immutable capability definition shared by model and executor paths.

    This capability intentionally supports only one bounded string field.
    Extending the schema vocabulary must happen here so provider exposure,
    Worker validation and execution cannot silently drift apart.
    """

    tool_id: str
    provider_name: str
    contract_version: str
    description: str
    input_fields: tuple[ToolInputField, ...]
    max_result_bytes: int
    read_only: bool
    destructive: bool
    concurrency: str
    risk: str
    timeout_seconds: int
    result_kind: str
    result_trust: str
    result_source: str
    progress_mode: str
    max_progress_events: int
    cancellation: str
    max_provider_projection_bytes: int = 4_096
    result_projection: str = "identity_or_digest_placeholder_v1"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tool_id, str)
            or _TOOL_ID.fullmatch(self.tool_id) is None
            or not isinstance(self.provider_name, str)
            or _PROVIDER_NAME.fullmatch(self.provider_name) is None
            or not isinstance(self.contract_version, str)
            or not 1 <= len(self.contract_version) <= 32
            or not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description.encode("utf-8")) > 1_024
            or self.contract_version not in {"1", "2", "3"}
            or not isinstance(self.input_fields, tuple)
            or not 1 <= len(self.input_fields) <= 32
            or any(not isinstance(item, ToolInputField) for item in self.input_fields)
            or len({item.name for item in self.input_fields}) != len(self.input_fields)
            or not isinstance(self.max_result_bytes, int)
            or isinstance(self.max_result_bytes, bool)
            or not 1 <= self.max_result_bytes <= 65_536
            or not isinstance(self.read_only, bool)
            or not isinstance(self.destructive, bool)
            or (self.read_only and self.destructive)
            or (self.read_only != (self.risk == "read_only"))
            or (self.destructive and self.risk == "read_only")
            or not isinstance(self.concurrency, str)
            or self.concurrency not in _CONCURRENCY
            or not isinstance(self.risk, str)
            or self.risk not in _RISK
            or not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 3_600
            or self.result_kind not in _RESULT_KIND
            or self.result_trust not in _RESULT_TRUST
            or not isinstance(self.result_source, str)
            or _SAFE_SOURCE.fullmatch(self.result_source) is None
            or self.progress_mode not in _PROGRESS_MODE
            or not isinstance(self.max_progress_events, int)
            or isinstance(self.max_progress_events, bool)
            or not 0 <= self.max_progress_events <= 256
            or (self.progress_mode == "none") != (self.max_progress_events == 0)
            or self.cancellation not in _CANCELLATION
            or not isinstance(self.max_provider_projection_bytes, int)
            or isinstance(self.max_provider_projection_bytes, bool)
            or not 512
            <= self.max_provider_projection_bytes
            <= min(self.max_result_bytes, MAX_PROVIDER_TOOL_RESULT_HISTORY_BYTES)
            or self.result_projection not in _RESULT_PROJECTION
        ):
            raise ValueError("invalid Tool specification")

    def validate_arguments(
        self, arguments: object
    ) -> dict[str, str | int | bool]:
        fields = {field.name: field for field in self.input_fields}
        required = {field.name for field in self.input_fields if field.required}
        if (
            not isinstance(arguments, dict)
            or not required.issubset(arguments)
            or not set(arguments).issubset(fields)
        ):
            raise ValueError(f"{self.tool_id} has invalid arguments")
        return {
            name: fields[name].validate(value)
            for name, value in arguments.items()
        }

    def validate_result(self, content: object) -> str:
        if not isinstance(content, str):
            raise ValueError(f"{self.tool_id} returned non-text content")
        if len(content.encode("utf-8")) > self.max_result_bytes:
            raise ValueError(f"{self.tool_id} result exceeds its byte limit")
        return content

    def canonical_manifest(self) -> dict[str, object]:
        required = [field.name for field in self.input_fields if field.required]
        input_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {
                field.name: field.json_schema() for field in self.input_fields
            },
        }
        if self.contract_version == "1":
            # Historical v1 manifests remain byte-identical so retained Runs
            # can still be replayed after the v2 catalog ships.
            if len(self.input_fields) != 1 or not self.input_fields[0].required:
                raise ValueError("v1 Tool contract must have one required field")
            field = self.input_fields[0]
            if field.value_kind != "string":
                raise ValueError("v1 Tool contract requires a string field")
            assert field.maximum_utf8_bytes is not None
            return {
                "tool_id": self.tool_id,
                "provider_name": self.provider_name,
                "contract_version": self.contract_version,
                "description": self.description,
                "input_schema": input_schema,
                "max_argument_bytes": field.maximum_utf8_bytes,
                "max_result_bytes": self.max_result_bytes,
                "read_only": self.read_only,
                "destructive": self.destructive,
                "concurrency": self.concurrency,
                "risk": self.risk,
                "timeout_seconds": self.timeout_seconds,
            }
        manifest = {
            "tool_id": self.tool_id,
            "provider_name": self.provider_name,
            "contract_version": self.contract_version,
            "description": self.description,
            "input_schema": input_schema,
            "input_fields": [field.canonical_manifest() for field in self.input_fields],
            "max_result_bytes": self.max_result_bytes,
            "result_kind": self.result_kind,
            "result_trust": self.result_trust,
            "result_source": self.result_source,
            "progress_mode": self.progress_mode,
            "max_progress_events": self.max_progress_events,
            "cancellation": self.cancellation,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "concurrency": self.concurrency,
            "risk": self.risk,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.contract_version == "3":
            manifest.update(
                {
                    "max_provider_projection_bytes": self.max_provider_projection_bytes,
                    "result_projection": self.result_projection,
                }
            )
        return manifest

    def ollama_definition(self) -> dict[str, object]:
        manifest = self.canonical_manifest()
        return {
            "type": "function",
            "function": {
                "name": self.provider_name,
                "description": self.description,
                "parameters": manifest["input_schema"],
            },
        }


@dataclass(frozen=True)
class ToolResult:
    outcome: str
    content: str
    trust: str = "untrusted_tool_data"
    source: str = "runtime"
    progress: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolResultProjection:
    call_id: str
    tool_id: str
    content: str
    original_bytes: int
    content_digest: str
    truncated: bool
    truncation_reason: str
    projection_digest: str


def _tool_result_content_digest(content: bytes) -> str:
    return hashlib.sha256(
        b"agent-builder-tool-result-content-v1\0" + content
    ).hexdigest()


def _tool_result_placeholder(
    call_id: str, original_bytes: int, content_digest: str
) -> str:
    return (
        "[tool-result compacted; "
        f"call_id={call_id}; original_bytes={original_bytes}; "
        f"sha256={content_digest}; reason=provider_projection_limit]"
    )


def _tool_result_projection_digest(
    spec: ToolSpec,
    *,
    call_id: str,
    content: str,
    original_bytes: int,
    content_digest: str,
    truncated: bool,
    truncation_reason: str,
) -> str:
    encoded = json.dumps(
        {
            "tool_id": spec.tool_id,
            "contract_version": spec.contract_version,
            "call_id": call_id,
            "content": content,
            "original_bytes": original_bytes,
            "content_digest": content_digest,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
            "projection_policy": spec.result_projection,
            "projection_budget_bytes": spec.max_provider_projection_bytes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(
        b"agent-builder-tool-result-projection-v1\0" + encoded
    ).hexdigest()


def project_tool_result(
    spec: ToolSpec, call_id: str, canonical_content: object
) -> ToolResultProjection:
    if _TOOL_CALL_ID.fullmatch(call_id) is None:
        raise ValueError("invalid Tool result call identity")
    content = spec.validate_result(canonical_content)
    encoded = content.encode("utf-8")
    original_bytes = len(encoded)
    content_digest = _tool_result_content_digest(encoded)
    truncated = original_bytes > spec.max_provider_projection_bytes
    projected = (
        _tool_result_placeholder(call_id, original_bytes, content_digest)
        if truncated
        else content
    )
    if len(projected.encode("utf-8")) > spec.max_provider_projection_bytes:
        raise ValueError("Tool result placeholder exceeds its projection budget")
    reason = "provider_projection_limit" if truncated else "none"
    digest = _tool_result_projection_digest(
        spec,
        call_id=call_id,
        content=projected,
        original_bytes=original_bytes,
        content_digest=content_digest,
        truncated=truncated,
        truncation_reason=reason,
    )
    return ToolResultProjection(
        call_id,
        spec.tool_id,
        projected,
        original_bytes,
        content_digest,
        truncated,
        reason,
        digest,
    )


def validate_tool_result_projection(
    spec: ToolSpec, projection: ToolResultProjection
) -> ToolResultProjection:
    try:
        projected_bytes = len(projection.content.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        raise ValueError("invalid Tool result projection") from None
    if (
        not isinstance(projection, ToolResultProjection)
        or projection.tool_id != spec.tool_id
        or _TOOL_CALL_ID.fullmatch(projection.call_id) is None
        or not isinstance(projection.original_bytes, int)
        or isinstance(projection.original_bytes, bool)
        or not 0 <= projection.original_bytes <= spec.max_result_bytes
        or not isinstance(projection.content_digest, str)
        or _DIGEST.fullmatch(projection.content_digest) is None
        or not isinstance(projection.truncated, bool)
        or not isinstance(projection.truncation_reason, str)
        or projection.truncation_reason
        not in {"none", "provider_projection_limit"}
        or not isinstance(projection.projection_digest, str)
        or projected_bytes > spec.max_provider_projection_bytes
    ):
        raise ValueError("invalid Tool result projection")
    if projection.truncated:
        expected_content = _tool_result_placeholder(
            projection.call_id,
            projection.original_bytes,
            projection.content_digest,
        )
        if (
            projection.original_bytes <= spec.max_provider_projection_bytes
            or projection.truncation_reason != "provider_projection_limit"
            or projection.content != expected_content
        ):
            raise ValueError("invalid compacted Tool result projection")
    else:
        encoded = projection.content.encode("utf-8")
        if (
            projection.truncation_reason != "none"
            or projection.original_bytes != len(encoded)
            or projection.content_digest != _tool_result_content_digest(encoded)
        ):
            raise ValueError("invalid identity Tool result projection")
    expected_digest = _tool_result_projection_digest(
        spec,
        call_id=projection.call_id,
        content=projection.content,
        original_bytes=projection.original_bytes,
        content_digest=projection.content_digest,
        truncated=projection.truncated,
        truncation_reason=projection.truncation_reason,
    )
    if projection.projection_digest != expected_digest:
        raise ValueError("Tool result projection digest changed")
    return projection


ToolArguments = dict[str, str | int | bool]
ToolHandler = Callable[[ToolArguments], ToolResult]
BrokeredToolHandler = Callable[[str, ToolArguments, str], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    specs: tuple[ToolSpec, ...]
    digest: str

    @classmethod
    def create(cls, specs: tuple[ToolSpec, ...]) -> ToolCatalog:
        ordered = tuple(sorted(specs, key=lambda item: item.tool_id))
        toolset_digest(ordered)
        encoded = json.dumps(
            [item.canonical_manifest() for item in ordered],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(b"agent-builder-tool-catalog-v1\0" + encoded).hexdigest()
        return cls(ordered, digest)

    def by_id(self) -> dict[str, ToolSpec]:
        return {spec.tool_id: spec for spec in self.specs}


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    revision: str
    allowed_tool_ids: tuple[str, ...]
    denied_tool_ids: tuple[str, ...] = ()
    allowed_risks: tuple[str, ...] = ("read_only",)

    def __post_init__(self) -> None:
        if (
            _POLICY_REVISION.fullmatch(self.revision) is None
            or tuple(sorted(set(self.allowed_tool_ids))) != self.allowed_tool_ids
            or tuple(sorted(set(self.denied_tool_ids))) != self.denied_tool_ids
            or any(_TOOL_ID.fullmatch(item) is None for item in self.allowed_tool_ids)
            or any(_TOOL_ID.fullmatch(item) is None for item in self.denied_tool_ids)
            or tuple(sorted(set(self.allowed_risks))) != self.allowed_risks
            or not self.allowed_risks
            or any(item not in _RISK for item in self.allowed_risks)
        ):
            raise ValueError("invalid Tool policy")

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "allowed_tool_ids": list(self.allowed_tool_ids),
            "denied_tool_ids": list(self.denied_tool_ids),
            "allowed_risks": list(self.allowed_risks),
            "deny_precedence": True,
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.canonical_manifest(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(b"agent-builder-tool-policy-v1\0" + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class EffectiveToolSet:
    specs: tuple[ToolSpec, ...]
    catalog_digest: str
    policy_digest: str
    toolset_digest: str

    @classmethod
    def resolve(
        cls, catalog: ToolCatalog, policy: ToolPolicy
    ) -> EffectiveToolSet:
        available = catalog.by_id()
        referenced = set(policy.allowed_tool_ids) | set(policy.denied_tool_ids)
        if not referenced.issubset(available):
            raise ValueError("Tool policy references an unknown Tool")
        specs = tuple(
            available[tool_id]
            for tool_id in policy.allowed_tool_ids
            if tool_id not in policy.denied_tool_ids
            and available[tool_id].risk in policy.allowed_risks
        )
        return cls(
            specs=specs,
            catalog_digest=catalog.digest,
            policy_digest=policy.digest,
            toolset_digest=toolset_digest(specs),
        )

    def __post_init__(self) -> None:
        if (
            tuple(sorted(self.specs, key=lambda item: item.tool_id)) != self.specs
            or not re.fullmatch(r"[a-f0-9]{64}", self.catalog_digest)
            or not re.fullmatch(r"[a-f0-9]{64}", self.policy_digest)
            or self.toolset_digest != toolset_digest(self.specs)
        ):
            raise ValueError("invalid EffectiveToolSet")


@dataclass(frozen=True, slots=True)
class ToolUseContext:
    """A narrow reference for a future privileged capability broker.

    It deliberately contains no filesystem handle, environment mapping,
    credential, executor object or callback and therefore is not ambient
    authority when passed across an internal API.
    """

    agent_id: str
    capsule_generation: int
    conversation_id: str
    run_id: str
    call_id: str
    tool_id: str
    toolset_digest: str
    policy_digest: str
    arguments_digest: str
    preview_digest: str
    expires_at_milliseconds: int

    def __post_init__(self) -> None:
        if (
            _AGENT_ID.fullmatch(self.agent_id) is None
            or not isinstance(self.capsule_generation, int)
            or isinstance(self.capsule_generation, bool)
            or not 1 <= self.capsule_generation <= 1_000_000_000
            or _RESOURCE_ID.fullmatch(self.conversation_id) is None
            or _RESOURCE_ID.fullmatch(self.run_id) is None
            or _TOOL_CALL_ID.fullmatch(self.call_id) is None
            or _TOOL_ID.fullmatch(self.tool_id) is None
            or any(
                _DIGEST.fullmatch(item) is None
                for item in (
                    self.toolset_digest,
                    self.policy_digest,
                    self.arguments_digest,
                    self.preview_digest,
                )
            )
            or not isinstance(self.expires_at_milliseconds, int)
            or isinstance(self.expires_at_milliseconds, bool)
            or self.expires_at_milliseconds <= 0
        ):
            raise ValueError("invalid ToolUseContext")


_ECHO_INPUT = ToolInputField(
    name="text",
    value_kind="string",
    maximum_utf8_bytes=MAX_MESSAGE_BYTES,
)

PROTOTYPE_ECHO_SPEC_V1 = ToolSpec(
    tool_id="builtin/echo",
    provider_name="builtin_echo",
    contract_version="1",
    description="Return one bounded string unchanged.",
    input_fields=(_ECHO_INPUT,),
    max_result_bytes=MAX_MESSAGE_BYTES,
    read_only=True,
    destructive=False,
    concurrency="safe",
    risk="read_only",
    timeout_seconds=1,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="builtin/echo",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
)

PROTOTYPE_ECHO_SPEC_V2 = ToolSpec(
    **{
        **PROTOTYPE_ECHO_SPEC_V1.__dict__,
        "contract_version": "2",
    }
)

PROTOTYPE_ECHO_SPEC = ToolSpec(
    **{
        **PROTOTYPE_ECHO_SPEC_V2.__dict__,
        "contract_version": "3",
        "max_provider_projection_bytes": 4_096,
        "result_projection": "identity_or_digest_placeholder_v1",
    }
)

FILE_STAT_SPEC = ToolSpec(
    tool_id="file/stat",
    provider_name="file_stat",
    contract_version="3",
    description=(
        "Safely inspect one UTF-8 regular file in this agent workspace. "
        "The path must be relative and symlinks are rejected."
    ),
    input_fields=(
        ToolInputField("path", "string", maximum_utf8_bytes=1_024),
    ),
    max_result_bytes=12_288,
    read_only=True,
    destructive=False,
    concurrency="safe",
    risk="read_only",
    timeout_seconds=1,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="file/stat",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

FILE_READ_TEXT_SPEC = ToolSpec(
    tool_id="file/read_text",
    provider_name="file_read_text",
    contract_version="3",
    description=(
        "Read a bounded UTF-8 range from one regular file in this agent workspace. "
        "Use a relative path; optional byte or line offset and limits are fail-closed."
    ),
    input_fields=(
        ToolInputField("path", "string", maximum_utf8_bytes=1_024),
        ToolInputField(
            "offset_bytes", "integer", required=False,
            minimum_integer=0, maximum_integer=1_048_576,
        ),
        ToolInputField(
            "line_offset", "integer", required=False,
            minimum_integer=0, maximum_integer=100_000,
        ),
        ToolInputField(
            "max_bytes", "integer", required=False,
            minimum_integer=1, maximum_integer=4_096,
        ),
        ToolInputField(
            "max_lines", "integer", required=False,
            minimum_integer=1, maximum_integer=256,
        ),
    ),
    max_result_bytes=12_288,
    read_only=True,
    destructive=False,
    concurrency="safe",
    risk="read_only",
    timeout_seconds=1,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="file/read_text",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

FILE_GLOB_SPEC = ToolSpec(
    tool_id="file/glob",
    provider_name="file_glob",
    contract_version="3",
    description=(
        "List bounded UTF-8 regular files matching a safe workspace-relative glob. "
        "Results are stable, receipt-bound, and never follow links."
    ),
    input_fields=(
        ToolInputField("pattern", "string", maximum_utf8_bytes=256),
        ToolInputField(
            "max_results", "integer", required=False,
            minimum_integer=1, maximum_integer=128,
        ),
    ),
    max_result_bytes=12_288,
    read_only=True,
    destructive=False,
    concurrency="safe",
    risk="read_only",
    timeout_seconds=2,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="file/glob",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

FILE_GREP_SPEC = ToolSpec(
    tool_id="file/grep",
    provider_name="file_grep",
    contract_version="3",
    description=(
        "Search bounded UTF-8 workspace files by literal text or a safe regex subset. "
        "Provide a relative glob pattern and query; links and special files are rejected."
    ),
    input_fields=(
        ToolInputField("pattern", "string", maximum_utf8_bytes=256),
        ToolInputField("query", "string", maximum_utf8_bytes=256),
        ToolInputField("regex", "boolean", required=False),
        ToolInputField("case_sensitive", "boolean", required=False),
        ToolInputField(
            "max_results", "integer", required=False,
            minimum_integer=1, maximum_integer=128,
        ),
    ),
    max_result_bytes=12_288,
    read_only=True,
    destructive=False,
    concurrency="safe",
    risk="read_only",
    timeout_seconds=2,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="file/grep",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

FILE_EDIT_SPEC = ToolSpec(
    tool_id="file/edit",
    provider_name="file_edit",
    contract_version="3",
    description=(
        "Replace exactly one matching UTF-8 fragment in a workspace file after a "
        "complete file/read_text receipt. Requires explicit operator approval."
    ),
    input_fields=(
        ToolInputField("path", "string", maximum_utf8_bytes=1_024),
        ToolInputField("old_text", "string", maximum_utf8_bytes=4_096),
        ToolInputField("new_text", "string", maximum_utf8_bytes=4_096),
        ToolInputField("path_identity", "string", maximum_utf8_bytes=64),
        ToolInputField("content_digest", "string", maximum_utf8_bytes=64),
    ),
    max_result_bytes=12_288,
    read_only=False,
    destructive=False,
    concurrency="serialized",
    risk="mutation",
    timeout_seconds=2,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="file/edit",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

FILE_WRITE_SPEC = ToolSpec(
    tool_id="file/write",
    provider_name="file_write",
    contract_version="3",
    description=(
        "Create or fully replace one UTF-8 workspace file atomically. Existing-file "
        "replacement requires a complete file/read_text receipt and all mutations "
        "require explicit operator approval."
    ),
    input_fields=(
        ToolInputField("path", "string", maximum_utf8_bytes=1_024),
        ToolInputField("content", "string", maximum_utf8_bytes=8_192),
        ToolInputField("create", "boolean"),
        ToolInputField(
            "path_identity", "string", required=False, maximum_utf8_bytes=64
        ),
        ToolInputField(
            "content_digest", "string", required=False, maximum_utf8_bytes=64
        ),
    ),
    max_result_bytes=12_288,
    read_only=False,
    destructive=True,
    concurrency="serialized",
    risk="mutation",
    timeout_seconds=2,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="file/write",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

EXEC_RUN_SPEC_V1 = ToolSpec(
    tool_id="exec/run",
    provider_name="exec_run",
    contract_version="3",
    description=(
        "Run one operator-approved command from the trusted project-local allowlist. "
        "The v1 allowlist contains only runtime-compile and never invokes a shell."
    ),
    input_fields=(
        ToolInputField("command_id", "string", maximum_utf8_bytes=64),
    ),
    max_result_bytes=12_288,
    read_only=False,
    destructive=False,
    concurrency="serialized",
    risk="execution",
    timeout_seconds=15,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="exec/run",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

EXEC_RUN_SPEC = ToolSpec(
    tool_id="exec/run",
    provider_name="exec_run",
    contract_version="3",
    description=(
        "Run one operator-approved command: runtime-compile or a bounded-bash "
        "single builtin command. Bash expansion, pipes, redirection, environment "
        "assignment, subprocesses and network are denied."
    ),
    input_fields=(
        ToolInputField("command_id", "string", maximum_utf8_bytes=64),
        ToolInputField(
            "script", "string", required=False, maximum_utf8_bytes=1_024
        ),
    ),
    max_result_bytes=12_288,
    read_only=False,
    destructive=False,
    concurrency="serialized",
    risk="execution",
    timeout_seconds=15,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="exec/run",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

EXTENSION_CALL_SPEC = ToolSpec(
    tool_id="extension/call",
    provider_name="extension_call",
    contract_version="3",
    description=(
        "Call one method from the operator-configured MCP/LSP catalog through "
        "pinned HTTPS JSON-RPC. Local stdio and request-defined endpoints are disabled."
    ),
    input_fields=(
        ToolInputField("extension_id", "string", maximum_utf8_bytes=36),
        ToolInputField("method", "string", maximum_utf8_bytes=128),
        ToolInputField("params_json", "string", maximum_utf8_bytes=8_192),
    ),
    max_result_bytes=16_384,
    read_only=False,
    destructive=False,
    concurrency="serialized",
    risk="execution",
    timeout_seconds=8,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="extension/call",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

SKILL_RUN_SPEC = ToolSpec(
    tool_id="skill/run",
    provider_name="skill_run",
    contract_version="3",
    description=(
        "Run one explicitly installed, versioned Agent Skill in its dedicated "
        "environment and singleton network-denied sandbox."
    ),
    input_fields=(
        ToolInputField("skill_id", "string", maximum_utf8_bytes=36),
        ToolInputField("input_json", "string", maximum_utf8_bytes=4_096),
    ),
    max_result_bytes=12_288,
    read_only=False,
    destructive=False,
    concurrency="serialized",
    risk="execution",
    timeout_seconds=15,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="skill/run",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)


DOCUMENT_EXTRACT_TEXT_SPEC = ToolSpec(
    tool_id="document/extract_text",
    provider_name="document_extract_text",
    contract_version="3",
    description=(
        "Extract one bounded UTF-8 text window from a PDF, DOCX, Markdown, "
        "HTML, or text file in this Agent's workspace. The parser runs "
        "without network access in the Agent's isolated research environment."
    ),
    input_fields=(
        ToolInputField("path", "string", maximum_utf8_bytes=1_024),
        ToolInputField(
            "offset_chars",
            "integer",
            required=False,
            minimum_integer=0,
            maximum_integer=1_000_000,
        ),
        ToolInputField(
            "max_chars",
            "integer",
            required=False,
            minimum_integer=1,
            maximum_integer=4_096,
        ),
    ),
    max_result_bytes=12_288,
    read_only=True,
    destructive=False,
    concurrency="serialized",
    risk="read_only",
    timeout_seconds=15,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="document/extract_text",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)

AGENT_DELEGATE_SPEC = ToolSpec(
    tool_id="agent/delegate",
    provider_name="agent_delegate",
    contract_version="3",
    description=(
        "Delegate one bounded message to a different active Agent. The child runs "
        "in its own Capsule, Conversation, Worker and sandbox; explicit operator "
        "approval is required and only its bounded answer returns."
    ),
    input_fields=(
        ToolInputField("child_agent_id", "string", maximum_utf8_bytes=36),
        ToolInputField("message", "string", maximum_utf8_bytes=4_096),
    ),
    max_result_bytes=12_288,
    read_only=False,
    destructive=False,
    concurrency="serialized",
    risk="execution",
    timeout_seconds=45,
    result_kind="text",
    result_trust="untrusted_tool_data",
    result_source="agent/delegate",
    progress_mode="none",
    max_progress_events=0,
    cancellation="cooperative",
    max_provider_projection_bytes=8_192,
)


def prototype_tool_catalog() -> ToolCatalog:
    return ToolCatalog.create((PROTOTYPE_ECHO_SPEC,))


def prototype_tool_policy() -> ToolPolicy:
    return ToolPolicy(
        revision="prototype-policy-v1",
        allowed_tool_ids=(PROTOTYPE_ECHO_SPEC.tool_id,),
        allowed_risks=("read_only",),
    )


def prototype_effective_toolset() -> EffectiveToolSet:
    return EffectiveToolSet.resolve(
        prototype_tool_catalog(), prototype_tool_policy()
    )


def runtime_tool_catalog() -> ToolCatalog:
    return ToolCatalog.create(
        (
            PROTOTYPE_ECHO_SPEC,
            AGENT_DELEGATE_SPEC,
            DOCUMENT_EXTRACT_TEXT_SPEC,
            EXEC_RUN_SPEC,
            EXTENSION_CALL_SPEC,
            SKILL_RUN_SPEC,
            FILE_EDIT_SPEC,
            FILE_GLOB_SPEC,
            FILE_GREP_SPEC,
            FILE_READ_TEXT_SPEC,
            FILE_STAT_SPEC,
            FILE_WRITE_SPEC,
        )
    )


def runtime_tool_policy() -> ToolPolicy:
    return ToolPolicy(
        # builtin/echo remains in the catalog only so prototype fixtures and
        # historical Run journals can still be decoded.  It is deliberately
        # absent from the release policy: it has no user-facing capability and
        # small models can mistake it for a generic answer channel, creating a
        # deterministic Tool loop on otherwise self-contained prompts.
        revision="runtime-execution-policy-v2",
        allowed_tool_ids=(
            "agent/delegate",
            "document/extract_text",
            "exec/run",
            "extension/call",
            "file/edit",
            "file/glob",
            "file/grep",
            "file/read_text",
            "file/stat",
            "file/write",
            "skill/run",
        ),
        allowed_risks=("execution", "mutation", "read_only"),
    )


def runtime_effective_toolset() -> EffectiveToolSet:
    return EffectiveToolSet.resolve(runtime_tool_catalog(), runtime_tool_policy())


def runtime_tool_specs() -> tuple[ToolSpec, ...]:
    return runtime_effective_toolset().specs


def prototype_tool_specs() -> tuple[ToolSpec, ...]:
    return prototype_effective_toolset().specs


def prototype_tool_specs_for_ids(tool_ids: object) -> tuple[ToolSpec, ...]:
    """Resolve an untrusted Worker command against the sealed local catalog."""

    if (
        not isinstance(tool_ids, list)
        or len(tool_ids) > 32
        or any(not isinstance(item, str) for item in tool_ids)
        or tool_ids != sorted(set(tool_ids))
    ):
        raise ValueError("invalid effective Tool identities")
    catalog = prototype_tool_catalog().by_id()
    if any(item not in catalog for item in tool_ids):
        raise ValueError("unknown effective Tool identity")
    return tuple(catalog[item] for item in tool_ids)


def runtime_tool_specs_for_ids(tool_ids: object) -> tuple[ToolSpec, ...]:
    """Resolve an untrusted Worker command against the runtime catalog."""

    if (
        not isinstance(tool_ids, list)
        or len(tool_ids) > 32
        or any(not isinstance(item, str) for item in tool_ids)
        or tool_ids != sorted(set(tool_ids))
    ):
        raise ValueError("invalid effective Tool identities")
    catalog = runtime_tool_catalog().by_id()
    if any(item not in catalog for item in tool_ids):
        raise ValueError("unknown effective Tool identity")
    return tuple(catalog[item] for item in tool_ids)


def toolset_digest(specs: tuple[ToolSpec, ...]) -> str:
    ordered = sorted(specs, key=lambda spec: spec.tool_id)
    tool_ids = [spec.tool_id for spec in ordered]
    provider_names = [spec.provider_name for spec in ordered]
    if len(tool_ids) != len(set(tool_ids)) or len(provider_names) != len(
        set(provider_names)
    ):
        raise ValueError("effective Tool set contains a duplicate identity")
    payload = json.dumps(
        [spec.canonical_manifest() for spec in ordered],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"agent-builder-toolset-v1\0" + payload).hexdigest()


class ToolRegistry:
    def __init__(self, brokered_handler: BrokeredToolHandler | None = None) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}
        self._brokered: dict[str, ToolSpec] = {}
        self._brokered_handler = brokered_handler

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.tool_id in self._tools:
            raise ValueError(f"duplicate tool: {spec.tool_id}")
        if any(
            existing.provider_name == spec.provider_name
            for existing, _ in self._tools.values()
        ):
            raise ValueError(f"duplicate provider tool: {spec.provider_name}")
        self._tools[spec.tool_id] = (spec, handler)

    def register_brokered(self, spec: ToolSpec) -> None:
        if spec.tool_id in self._tools or spec.tool_id in self._brokered:
            raise ValueError(f"duplicate tool: {spec.tool_id}")
        if any(
            existing.provider_name == spec.provider_name
            for existing, _ in self._tools.values()
        ) or any(
            existing.provider_name == spec.provider_name
            for existing in self._brokered.values()
        ):
            raise ValueError(f"duplicate provider tool: {spec.provider_name}")
        if self._brokered_handler is None:
            raise ValueError("brokered Tool handler is unavailable")
        self._brokered[spec.tool_id] = spec

    def spec(self, tool_id: str) -> ToolSpec:
        if tool_id in self._brokered:
            return self._brokered[tool_id]
        try:
            return self._tools[tool_id][0]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_id}") from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec, _handler in self._tools.values()) + tuple(
            self._brokered.values()
        )

    def execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        spec = self.spec(tool_id)
        try:
            validated = spec.validate_arguments(arguments)
        except ValueError as exc:
            return ToolResult("failed", str(exc))
        if tool_id in self._brokered:
            if call_id is None or self._brokered_handler is None:
                return ToolResult("failed", "Brokered Tool context is unavailable")
            result = self._brokered_handler(tool_id, validated, call_id)
        else:
            result = self._tools[tool_id][1](validated)
        try:
            content = spec.validate_result(result.content)
        except ValueError as exc:
            return ToolResult("failed", str(exc))
        if result.outcome not in {"succeeded", "failed", "cancelled"}:
            return ToolResult("failed", "Tool returned an invalid outcome")
        if (
            len(result.progress) > spec.max_progress_events
            or (result.progress and spec.progress_mode == "none")
            or any(
                not isinstance(item, str)
                or len(item.encode("utf-8")) > spec.max_result_bytes
                for item in result.progress
            )
        ):
            return ToolResult(
                "failed",
                "Tool returned invalid progress",
                trust=spec.result_trust,
                source=spec.result_source,
            )
        return ToolResult(
            result.outcome,
            content,
            trust=spec.result_trust,
            source=spec.result_source,
            progress=result.progress,
        )


def prototype_tools(
    effective_specs: tuple[ToolSpec, ...] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    specs = prototype_tool_specs() if effective_specs is None else effective_specs
    catalog = prototype_tool_catalog().by_id()
    if any(catalog.get(spec.tool_id) != spec for spec in specs):
        raise ValueError("effective Tool specification is outside the catalog")
    for spec in specs:
        if spec.tool_id == PROTOTYPE_ECHO_SPEC.tool_id:
            registry.register(
                spec,
                lambda arguments: ToolResult("succeeded", arguments["text"]),
            )
    return registry


def runtime_tools(
    effective_specs: tuple[ToolSpec, ...],
    brokered_handler: BrokeredToolHandler,
) -> ToolRegistry:
    registry = ToolRegistry(brokered_handler)
    catalog = runtime_tool_catalog().by_id()
    if any(catalog.get(spec.tool_id) != spec for spec in effective_specs):
        raise ValueError("effective Tool specification is outside the runtime catalog")
    for spec in effective_specs:
        if spec.tool_id == PROTOTYPE_ECHO_SPEC.tool_id:
            registry.register(
                spec,
                lambda arguments: ToolResult("succeeded", arguments["text"]),
            )
        else:
            registry.register_brokered(spec)
    return registry


__all__ = [
    "PROTOTYPE_ECHO_SPEC",
    "PROTOTYPE_ECHO_SPEC_V1",
    "PROTOTYPE_ECHO_SPEC_V2",
    "FILE_READ_TEXT_SPEC",
    "FILE_STAT_SPEC",
    "FILE_GLOB_SPEC",
    "FILE_GREP_SPEC",
    "FILE_EDIT_SPEC",
    "FILE_WRITE_SPEC",
    "DOCUMENT_EXTRACT_TEXT_SPEC",
    "EXEC_RUN_SPEC",
    "EXEC_RUN_SPEC_V1",
    "EXTENSION_CALL_SPEC",
    "SKILL_RUN_SPEC",
    "MAX_PROVIDER_TOOL_RESULT_HISTORY_BYTES",
    "EffectiveToolSet",
    "ToolCatalog",
    "ToolInputField",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
    "ToolResultProjection",
    "ToolSpec",
    "ToolUseContext",
    "prototype_tool_specs",
    "prototype_tool_specs_for_ids",
    "prototype_tool_catalog",
    "prototype_tool_policy",
    "prototype_effective_toolset",
    "prototype_tools",
    "runtime_effective_toolset",
    "runtime_tool_catalog",
    "runtime_tool_policy",
    "runtime_tool_specs",
    "runtime_tool_specs_for_ids",
    "runtime_tools",
    "project_tool_result",
    "toolset_digest",
    "validate_tool_result_projection",
]

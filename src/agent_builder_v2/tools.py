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
_CONCURRENCY = frozenset({"safe", "serialized"})
_RISK = frozenset({"read_only", "mutation", "execution"})


@dataclass(frozen=True)
class ToolSpec:
    """One immutable capability definition shared by model and executor paths.

    The walking skeleton intentionally supports only one bounded string field.
    Extending the schema vocabulary must happen here so provider exposure,
    Worker validation and execution cannot silently drift apart.
    """

    tool_id: str
    provider_name: str
    contract_version: str
    description: str
    argument_name: str
    max_argument_bytes: int
    max_result_bytes: int
    read_only: bool
    destructive: bool
    concurrency: str
    risk: str
    timeout_seconds: int

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
            or not isinstance(self.argument_name, str)
            or _PROVIDER_NAME.fullmatch(self.argument_name) is None
            or not isinstance(self.max_argument_bytes, int)
            or isinstance(self.max_argument_bytes, bool)
            or not 1 <= self.max_argument_bytes <= 65_536
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
        ):
            raise ValueError("invalid Tool specification")

    def validate_arguments(self, arguments: object) -> dict[str, str]:
        if not isinstance(arguments, dict) or set(arguments) != {self.argument_name}:
            raise ValueError(f"{self.tool_id} has invalid arguments")
        value = arguments.get(self.argument_name)
        if not isinstance(value, str):
            raise ValueError(f"{self.argument_name} must be a string")
        if len(value.encode("utf-8")) > self.max_argument_bytes:
            raise ValueError(f"{self.argument_name} exceeds its byte limit")
        return {self.argument_name: value}

    def validate_result(self, content: object) -> str:
        if not isinstance(content, str):
            raise ValueError(f"{self.tool_id} returned non-text content")
        if len(content.encode("utf-8")) > self.max_result_bytes:
            raise ValueError(f"{self.tool_id} result exceeds its byte limit")
        return content

    def canonical_manifest(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "provider_name": self.provider_name,
            "contract_version": self.contract_version,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [self.argument_name],
                "properties": {
                    self.argument_name: {
                        "type": "string",
                        "maxLength": self.max_argument_bytes,
                        "x-agent-builder-maxUtf8Bytes": self.max_argument_bytes,
                    }
                },
            },
            "max_argument_bytes": self.max_argument_bytes,
            "max_result_bytes": self.max_result_bytes,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "concurrency": self.concurrency,
            "risk": self.risk,
            "timeout_seconds": self.timeout_seconds,
        }

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


ToolHandler = Callable[[dict[str, str]], ToolResult]


PROTOTYPE_ECHO_SPEC = ToolSpec(
    tool_id="builtin/echo",
    provider_name="builtin_echo",
    contract_version="1",
    description="Return one bounded string unchanged.",
    argument_name="text",
    max_argument_bytes=MAX_MESSAGE_BYTES,
    max_result_bytes=MAX_MESSAGE_BYTES,
    read_only=True,
    destructive=False,
    concurrency="safe",
    risk="read_only",
    timeout_seconds=1,
)


def prototype_tool_specs() -> tuple[ToolSpec, ...]:
    return (PROTOTYPE_ECHO_SPEC,)


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
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.tool_id in self._tools:
            raise ValueError(f"duplicate tool: {spec.tool_id}")
        if any(
            existing.provider_name == spec.provider_name
            for existing, _ in self._tools.values()
        ):
            raise ValueError(f"duplicate provider tool: {spec.provider_name}")
        self._tools[spec.tool_id] = (spec, handler)

    def spec(self, tool_id: str) -> ToolSpec:
        try:
            return self._tools[tool_id][0]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_id}") from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec, _handler in self._tools.values())

    def execute(self, tool_id: str, arguments: dict[str, Any]) -> ToolResult:
        spec = self.spec(tool_id)
        try:
            validated = spec.validate_arguments(arguments)
        except ValueError as exc:
            return ToolResult("failed", str(exc))
        result = self._tools[tool_id][1](validated)
        try:
            content = spec.validate_result(result.content)
        except ValueError as exc:
            return ToolResult("failed", str(exc))
        if result.outcome not in {"succeeded", "failed", "cancelled"}:
            return ToolResult("failed", "Tool returned an invalid outcome")
        return ToolResult(result.outcome, content)


def prototype_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        PROTOTYPE_ECHO_SPEC,
        lambda arguments: ToolResult("succeeded", arguments["text"]),
    )
    return registry


__all__ = [
    "PROTOTYPE_ECHO_SPEC",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "prototype_tool_specs",
    "prototype_tools",
    "toolset_digest",
]

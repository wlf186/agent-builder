"""Small project-owned Tool contract and registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolDescriptor:
    tool_id: str
    description: str
    read_only: bool
    concurrency: str


@dataclass(frozen=True)
class ToolResult:
    outcome: str
    content: str


ToolHandler = Callable[[dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDescriptor, ToolHandler]] = {}

    def register(self, descriptor: ToolDescriptor, handler: ToolHandler) -> None:
        if descriptor.tool_id in self._tools:
            raise ValueError(f"duplicate tool: {descriptor.tool_id}")
        self._tools[descriptor.tool_id] = (descriptor, handler)

    def descriptor(self, tool_id: str) -> ToolDescriptor:
        try:
            return self._tools[tool_id][0]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_id}") from exc

    def execute(self, tool_id: str, arguments: dict[str, Any]) -> ToolResult:
        descriptor = self.descriptor(tool_id)
        if descriptor.tool_id == "builtin/echo":
            if set(arguments) != {"text"}:
                return ToolResult("failed", "echo accepts exactly one text field")
            value = arguments.get("text")
            if not isinstance(value, str):
                return ToolResult("failed", "text must be a string")
            if len(value) > 2_048:
                return ToolResult("failed", "text exceeds 2048 characters")
        return self._tools[tool_id][1](arguments)


def prototype_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            tool_id="builtin/echo",
            description="Return one bounded string unchanged.",
            read_only=True,
            concurrency="safe",
        ),
        lambda arguments: ToolResult("succeeded", str(arguments["text"])),
    )
    return registry

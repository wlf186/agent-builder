"""Typed prompt and context assembly for the prototype Kernel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    section_id: str
    provenance: str
    cache_scope: str
    content: str


class ContextCompiler:
    """Build the model view deterministically from explicit sections."""

    def compile(self, user_message: str) -> tuple[PromptSection, ...]:
        return (
            PromptSection(
                section_id="platform.contract",
                provenance="harness-v2",
                cache_scope="build",
                content=(
                    "You are the Harness V2 prototype assistant. Use only "
                    "declared structured tools and never reveal hidden reasoning."
                ),
            ),
            PromptSection(
                section_id="agent.instructions",
                provenance="prototype-agent",
                cache_scope="agent",
                content=(
                    "Demonstrate one complete model-tool-model loop through the "
                    "trusted model broker and answer the user in Chinese."
                ),
            ),
            PromptSection(
                section_id="turn.user",
                provenance="user",
                cache_scope="turn",
                content=user_message,
            ),
        )

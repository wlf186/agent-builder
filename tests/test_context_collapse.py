"""Deterministic completed-Turn collapse receipts and fail-closed bindings."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from agent_builder_v2.context import ConversationMessage
from agent_builder_v2.context_collapse import (
    ContextCollapseError,
    ContextCollapseProjection,
)


def _history(content_suffix: str = "") -> tuple[ConversationMessage, ...]:
    return tuple(
        ConversationMessage(
            f"{index + 1:032x}",
            "user" if index % 2 == 0 else "assistant",
            f"turn-{index}-{content_suffix or 'content'}",
        )
        for index in range(8)
    )


def test_projection_is_deterministic_content_free_and_preserves_recent_pairs() -> None:
    history = _history()
    first = ContextCollapseProjection.create(
        history, omitted_message_count=4, source_history_digest="a" * 64
    )
    second = ContextCollapseProjection.create(
        history, omitted_message_count=4, source_history_digest="a" * 64
    )

    assert first == second
    assert first.collapsed_turn_count == 2
    assert first.collapsed_message_ids == tuple(item.message_id for item in history[:4])
    assert first.preserved_message_ids == tuple(item.message_id for item in history[4:])
    encoded = json.dumps(first.canonical_manifest(), sort_keys=True)
    assert all(item.content not in encoded for item in history)
    assert all(item.content not in first.placeholder() for item in history)
    assert first.projection_digest in first.placeholder()
    assert "preserved in full" in first.placeholder()


def test_projection_binds_content_identity_source_and_policy_inputs() -> None:
    base = ContextCollapseProjection.create(
        _history(), omitted_message_count=4, source_history_digest="a" * 64
    )
    changed_content = ContextCollapseProjection.create(
        _history("changed"), omitted_message_count=4, source_history_digest="b" * 64
    )
    changed_boundary = ContextCollapseProjection.create(
        _history(), omitted_message_count=6, source_history_digest="a" * 64
    )

    assert changed_content.collapsed_content_digest != base.collapsed_content_digest
    assert changed_content.preserved_segment_digest != base.preserved_segment_digest
    assert changed_content.projection_digest != base.projection_digest
    assert changed_boundary.projection_digest != base.projection_digest
    with pytest.raises(ContextCollapseError, match="invalid"):
        replace(base, projection_digest="0" * 64)


@pytest.mark.parametrize("omitted", (0, 1, 7, 8))
def test_projection_never_splits_pairs_or_collapses_the_recent_tail(
    omitted: int,
) -> None:
    with pytest.raises(ContextCollapseError, match="source"):
        ContextCollapseProjection.create(
            _history(), omitted_message_count=omitted, source_history_digest="a" * 64
        )

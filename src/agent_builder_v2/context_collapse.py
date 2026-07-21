"""Content-free receipts for deterministic completed-Turn collapse."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol


CONTEXT_COLLAPSE_VERSION = "context-collapse-v1"
MAX_COLLAPSE_MESSAGES = 256
MAX_COLLAPSE_RECEIPT_BYTES = 16 * 1024
_MESSAGE_ID = re.compile(r"^[a-f0-9]{32}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class ContextCollapseError(ValueError):
    """A collapse receipt is malformed or does not bind its source."""


class CollapsibleMessage(Protocol):
    message_id: str
    role: str
    content: str

    def canonical_manifest(self) -> dict[str, str]: ...


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextCollapseProjection:
    source_history_digest: str
    collapsed_message_ids: tuple[str, ...]
    preserved_message_ids: tuple[str, ...]
    collapsed_content_digest: str
    preserved_segment_digest: str
    projection_digest: str
    version: str = CONTEXT_COLLAPSE_VERSION

    def __post_init__(self) -> None:
        unsigned = self._unsigned_manifest()
        if (
            self.version != CONTEXT_COLLAPSE_VERSION
            or any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    self.source_history_digest,
                    self.collapsed_content_digest,
                    self.preserved_segment_digest,
                    self.projection_digest,
                )
            )
            or not isinstance(self.collapsed_message_ids, tuple)
            or not isinstance(self.preserved_message_ids, tuple)
            or not 2 <= len(self.collapsed_message_ids) <= MAX_COLLAPSE_MESSAGES - 2
            or len(self.collapsed_message_ids) % 2
            or not 2 <= len(self.preserved_message_ids) <= MAX_COLLAPSE_MESSAGES - 2
            or len(self.preserved_message_ids) % 2
            or len(self.collapsed_message_ids) + len(self.preserved_message_ids)
            > MAX_COLLAPSE_MESSAGES
            or len(set((*self.collapsed_message_ids, *self.preserved_message_ids)))
            != len(self.collapsed_message_ids) + len(self.preserved_message_ids)
            or any(
                _MESSAGE_ID.fullmatch(value) is None
                for value in (*self.collapsed_message_ids, *self.preserved_message_ids)
            )
            or self.projection_digest
            != _digest(b"agent-builder-context-collapse-projection-v1", unsigned)
            or len(_canonical(self.canonical_manifest())) > MAX_COLLAPSE_RECEIPT_BYTES
        ):
            raise ContextCollapseError("invalid deterministic collapse projection")

    @classmethod
    def create(
        cls,
        history: tuple[CollapsibleMessage, ...],
        *,
        omitted_message_count: int,
        source_history_digest: str,
    ) -> ContextCollapseProjection:
        if (
            not isinstance(history, tuple)
            or len(history) > MAX_COLLAPSE_MESSAGES
            or len(history) % 2
            or not isinstance(omitted_message_count, int)
            or isinstance(omitted_message_count, bool)
            or not 2 <= omitted_message_count <= len(history) - 2
            or omitted_message_count % 2
            or _DIGEST.fullmatch(source_history_digest) is None
            or any(
                message.role != ("user" if index % 2 == 0 else "assistant")
                for index, message in enumerate(history)
            )
        ):
            raise ContextCollapseError("invalid deterministic collapse source")
        collapsed = history[:omitted_message_count]
        preserved = history[omitted_message_count:]
        values: dict[str, object] = {
            "version": CONTEXT_COLLAPSE_VERSION,
            "source_history_digest": source_history_digest,
            "collapsed_message_ids": [message.message_id for message in collapsed],
            "preserved_message_ids": [message.message_id for message in preserved],
            "collapsed_content_digest": _digest(
                b"agent-builder-collapsed-turn-content-v1",
                [message.canonical_manifest() for message in collapsed],
            ),
            "preserved_segment_digest": _digest(
                b"agent-builder-preserved-turn-segment-v1",
                [message.canonical_manifest() for message in preserved],
            ),
        }
        projection_digest = _digest(
            b"agent-builder-context-collapse-projection-v1", values
        )
        return cls(
            source_history_digest=source_history_digest,
            collapsed_message_ids=tuple(values["collapsed_message_ids"]),  # type: ignore[arg-type]
            preserved_message_ids=tuple(values["preserved_message_ids"]),  # type: ignore[arg-type]
            collapsed_content_digest=str(values["collapsed_content_digest"]),
            preserved_segment_digest=str(values["preserved_segment_digest"]),
            projection_digest=projection_digest,
        )

    @property
    def collapsed_turn_count(self) -> int:
        return len(self.collapsed_message_ids) // 2

    def _unsigned_manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "source_history_digest": self.source_history_digest,
            "collapsed_message_ids": list(self.collapsed_message_ids),
            "preserved_message_ids": list(self.preserved_message_ids),
            "collapsed_content_digest": self.collapsed_content_digest,
            "preserved_segment_digest": self.preserved_segment_digest,
        }

    def canonical_manifest(self) -> dict[str, object]:
        return {**self._unsigned_manifest(), "projection_digest": self.projection_digest}

    def placeholder(self) -> str:
        return (
            "The trusted runtime deterministically collapsed "
            f"{self.collapsed_turn_count} older completed conversation turns. "
            f"Projection receipt: {self.projection_digest}. The collapsed content is "
            "unavailable in this model view; do not infer or invent it. All following "
            "conversation turns are preserved in full."
        )


__all__ = [
    "CONTEXT_COLLAPSE_VERSION",
    "ContextCollapseError",
    "ContextCollapseProjection",
    "MAX_COLLAPSE_MESSAGES",
    "MAX_COLLAPSE_RECEIPT_BYTES",
]

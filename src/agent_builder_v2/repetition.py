"""Bounded exact-suffix detection for trusted model-stream normalization."""

from __future__ import annotations

from dataclasses import dataclass


MIN_REPEAT_UNIT_BYTES = 32
MAX_REPEAT_UNIT_BYTES = 512
MIN_REPEAT_COPIES = 3
MIN_REPEAT_EVIDENCE_BYTES = 512
REPETITION_CHECK_INTERVAL_BYTES = 64
REPETITION_SCAN_CODEPOINTS = 2_048


@dataclass(frozen=True, slots=True)
class RepetitionMatch:
    """Describe one exact suffix cycle and the prefix that retains one copy."""

    repeat_start: int
    keep_end: int
    unit_bytes: int
    repetitions: int


def detect_repeating_suffix(text: str) -> RepetitionMatch | None:
    """Return a bounded exact suffix match, or ``None`` for ordinary text."""

    if not isinstance(text, str):
        return None
    if len(text.encode("utf-8")) < MIN_REPEAT_EVIDENCE_BYTES:
        return None

    scan_start = max(0, len(text) - REPETITION_SCAN_CODEPOINTS)
    maximum_width = min(
        MAX_REPEAT_UNIT_BYTES,
        (len(text) - scan_start) // MIN_REPEAT_COPIES,
    )
    matches: list[tuple[int, int, str, int]] = []
    for width in range(1, maximum_width + 1):
        unit = text[-width:]
        unit_bytes = len(unit.encode("utf-8"))
        if not MIN_REPEAT_UNIT_BYTES <= unit_bytes <= MAX_REPEAT_UNIT_BYTES:
            continue
        cursor = len(text)
        copies = 0
        while (
            cursor - width >= scan_start
            and text[cursor - width : cursor] == unit
        ):
            cursor -= width
            copies += 1
        if (
            copies >= MIN_REPEAT_COPIES
            and copies * unit_bytes >= MIN_REPEAT_EVIDENCE_BYTES
        ):
            matches.append((unit_bytes, width, unit, copies))

    if not matches:
        return None

    unit_bytes, width, unit, copies = min(
        matches, key=lambda item: (item[0], item[1])
    )
    cursor = len(text) - width * copies
    while cursor - width >= 0 and text[cursor - width : cursor] == unit:
        cursor -= width
        copies += 1
    return RepetitionMatch(
        repeat_start=cursor,
        keep_end=cursor + width,
        unit_bytes=unit_bytes,
        repetitions=copies,
    )


__all__ = [
    "MAX_REPEAT_UNIT_BYTES",
    "MIN_REPEAT_COPIES",
    "MIN_REPEAT_EVIDENCE_BYTES",
    "MIN_REPEAT_UNIT_BYTES",
    "REPETITION_CHECK_INTERVAL_BYTES",
    "REPETITION_SCAN_CODEPOINTS",
    "RepetitionMatch",
    "detect_repeating_suffix",
]

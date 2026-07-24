"""Contract tests for bounded exact model-output repetition detection."""

from __future__ import annotations

from agent_builder_v2.repetition import (
    MAX_REPEAT_UNIT_BYTES,
    MIN_REPEAT_COPIES,
    MIN_REPEAT_EVIDENCE_BYTES,
    MIN_REPEAT_UNIT_BYTES,
    REPETITION_CHECK_INTERVAL_BYTES,
    REPETITION_SCAN_CODEPOINTS,
    detect_repeating_suffix,
)


def test_detects_exact_suffix_and_keeps_one_cycle() -> None:
    unit = "Because they make up everything!\nThat is why they are not trusted.\n"
    text = "valid joke\n" + unit * 9

    match = detect_repeating_suffix(text)

    assert match is not None
    assert match.repeat_start == len("valid joke\n")
    assert text[match.repeat_start : match.keep_end] == unit
    assert text[match.keep_end :] == unit * 8
    assert match.unit_bytes == len(unit.encode("utf-8"))
    assert match.repetitions == 9


def test_does_not_match_two_copies_or_a_nonrepeating_suffix() -> None:
    unit = "a sufficiently varied repeated sentence.\n"

    assert detect_repeating_suffix(unit * 2) is None
    assert detect_repeating_suffix(unit * 8 + "natural ending") is None


def test_detects_utf8_without_splitting_codepoints() -> None:
    unit = "中文循环🙂必须保持 UTF-8 边界。\n"
    prefix = "前缀\n"
    text = prefix + unit * 12

    match = detect_repeating_suffix(text)

    assert match is not None
    assert match.repeat_start == len(prefix)
    assert text[match.repeat_start : match.keep_end] == unit
    assert text[: match.keep_end].encode("utf-8").decode("utf-8")


def test_detector_thresholds_are_fixed_and_bounded() -> None:
    assert MIN_REPEAT_UNIT_BYTES == 32
    assert MAX_REPEAT_UNIT_BYTES == 512
    assert MIN_REPEAT_COPIES == 3
    assert MIN_REPEAT_EVIDENCE_BYTES == 512
    assert REPETITION_CHECK_INTERVAL_BYTES == 64
    assert REPETITION_SCAN_CODEPOINTS == 2_048
    assert detect_repeating_suffix("x" * (MIN_REPEAT_EVIDENCE_BYTES - 1)) is None


def test_match_expands_to_repetition_start_outside_the_scan_window() -> None:
    unit = "0123456789abcdef" * 3 + "fedcba9876543210"
    prefix = "bounded prefix\n"
    text = prefix + unit * 80

    match = detect_repeating_suffix(text)

    assert match is not None
    assert match.repeat_start == len(prefix)
    assert match.repetitions == 80
    assert text[match.repeat_start : match.keep_end] == unit


def test_candidate_larger_than_maximum_unit_is_not_detected() -> None:
    first = "a" * MAX_REPEAT_UNIT_BYTES + "x"
    second = "a" * MAX_REPEAT_UNIT_BYTES + "y"
    unit = first + second

    assert len(unit.encode("utf-8")) > MAX_REPEAT_UNIT_BYTES
    assert detect_repeating_suffix(unit * 3) is None

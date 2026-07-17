"""Bounded, defensive serialization for trace attributes."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableSet


REDACTED = "[REDACTED]"
TRUNCATED = "...[TRUNCATED]"

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_\-.])(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|token|bearer|cookie|password|passwd|secret|private[_-]?key|"
    r"client[_-]?secret)(?:$|[_\-.])",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_COMMON_SECRET_VALUE = re.compile(
    r"\b(?:sk|pk)-(?:live|test|proj)-[A-Za-z0-9_-]{6,}\b",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
_URL_QUERY_SECRET = re.compile(
    r"(?P<prefix>[?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|token|password|passwd|secret|client[_-]?secret)=)"
    r"[^&#\s]*",
    re.IGNORECASE,
)
_TEXT_SECRET_ASSIGNMENT = re.compile(
    r"(?P<prefix>\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|token|password|passwd|secret|client[_-]?secret)\b"
    r"\s*(?:=|:)\s*)(?:[^\s,;&]{3,})",
    re.IGNORECASE,
)
_JWT_VALUE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\."
    r"[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
)
_AWS_ACCESS_KEY = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")


def is_sensitive_key(key: Any) -> bool:
    """Return whether a mapping key is expected to contain credentials."""

    return bool(_SENSITIVE_KEY.search(str(key)))


def redact_text(value: str) -> str:
    """Remove common inline credential forms without changing normal text."""

    value = _URL_CREDENTIALS.sub(r"\g<scheme>" + REDACTED + "@", value)
    value = _URL_QUERY_SECRET.sub(r"\g<prefix>" + REDACTED, value)
    value = _BEARER_VALUE.sub(f"Bearer {REDACTED}", value)
    value = _COMMON_SECRET_VALUE.sub(REDACTED, value)
    value = _JWT_VALUE.sub(REDACTED, value)
    value = _AWS_ACCESS_KEY.sub(REDACTED, value)
    return _TEXT_SECRET_ASSIGNMENT.sub(r"\g<prefix>" + REDACTED, value)


def truncate_text(value: str, max_length: int) -> str:
    """Truncate text deterministically while preserving an explicit marker."""

    if max_length < len(TRUNCATED):
        return TRUNCATED[:max_length]
    if len(value) <= max_length:
        return value
    return value[: max_length - len(TRUNCATED)] + TRUNCATED


def sanitize(
    value: Any,
    *,
    max_string_length: int = 4096,
    max_items: int = 50,
    max_depth: int = 8,
    _depth: int = 0,
    _seen: MutableSet[int] | None = None,
) -> Any:
    """Return a JSON-compatible, redacted and size-bounded representation."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return truncate_text(redact_text(value), max_string_length)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{type(value).__name__}:{len(value)} bytes>"
    if isinstance(value, Path):
        return truncate_text(redact_text(str(value)), max_string_length)
    if _depth >= max_depth:
        return "[MAX_DEPTH]"

    seen = _seen if _seen is not None else set()
    object_id = id(value)
    if object_id in seen:
        return "[CYCLE]"

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            value = repr(value)

    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            result: Dict[str, Any] = {}
            items = list(islice(value.items(), max_items + 1))
            for key, item in items[:max_items]:
                key_text = truncate_text(str(key), 256)
                result[key_text] = (
                    REDACTED
                    if is_sensitive_key(key_text)
                    else sanitize(
                        item,
                        max_string_length=max_string_length,
                        max_items=max_items,
                        max_depth=max_depth,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
                )
            if len(items) > max_items:
                try:
                    omitted = max(len(value) - max_items, 1)
                except (TypeError, AttributeError):
                    omitted = 1
                result["_truncated_items"] = omitted
            return result
        finally:
            seen.discard(object_id)

    if isinstance(value, Iterable):
        seen.add(object_id)
        try:
            items = list(islice(value, max_items + 1))
            result = [
                sanitize(
                    item,
                    max_string_length=max_string_length,
                    max_items=max_items,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                    _seen=seen,
                )
                for item in items[:max_items]
            ]
            if len(items) > max_items:
                try:
                    omitted = max(len(value) - max_items, 1)
                except (TypeError, AttributeError):
                    omitted = 1
                result.append(f"[{omitted} ITEMS TRUNCATED]")
            return result
        finally:
            seen.discard(object_id)

    return truncate_text(redact_text(repr(value)), max_string_length)


def serialize_attribute(
    value: Any,
    *,
    max_length: int = 4096,
    max_items: int = 50,
    max_depth: int = 8,
) -> str:
    """Serialize data as bounded JSON suitable for OpenInference attributes."""

    sanitized = sanitize(
        value,
        max_string_length=max_length,
        max_items=max_items,
        max_depth=max_depth,
    )
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), default=str)
    return truncate_text(encoded, max_length)

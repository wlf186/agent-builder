"""Value-free summaries for logs, traces, and diagnostic placeholders."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List


_SENSITIVE_ARGUMENT_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|secret|session|token)",
    re.IGNORECASE,
)
_SAFE_ARGUMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_MAX_LOGGED_ARGUMENT_KEYS = 32


def summarize_arguments(arguments: Any) -> Dict[str, Any]:
    """Return argument keys and size without rendering any argument value."""
    if not isinstance(arguments, dict):
        return {
            "argument_keys": [],
            "argument_key_count": 0,
            "argument_length": serialized_length(arguments),
        }

    safe_keys: List[str] = []
    for raw_key in arguments.keys():
        key = str(raw_key)
        if _SENSITIVE_ARGUMENT_KEY.search(key):
            safe_key = "<redacted>"
        elif _SAFE_ARGUMENT_KEY.fullmatch(key):
            safe_key = key
        else:
            safe_key = "<redacted-key>"
        safe_keys.append(safe_key)

    safe_keys.sort()
    return {
        "argument_keys": safe_keys[:_MAX_LOGGED_ARGUMENT_KEYS],
        "argument_key_count": len(safe_keys),
        "argument_length": serialized_length(arguments),
    }


def content_length(value: Any) -> int:
    """Measure content without converting it to a loggable representation."""
    if value is None:
        return 0
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        return len(value)
    return serialized_length(value)


def serialized_length(value: Any) -> int:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda item: f"<{type(item).__name__}>",
        )
    except (TypeError, ValueError, RecursionError):
        return 0
    return len(rendered)

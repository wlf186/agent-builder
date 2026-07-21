"""Explicit parser for the deliberately tiny bounded Bash grammar."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import shlex


MAX_BASH_SCRIPT_BYTES = 1_024
MAX_BASH_ARGUMENTS = 8
MAX_BASH_ARGUMENT_BYTES = 256
ALLOWED_BASH_BUILTINS = frozenset({"false", "printf", "pwd", "true"})
_FORBIDDEN = frozenset("$`;&|<>(){}[]*?!\\\n\r\t\x00")


class BashParseError(ValueError):
    """The input is outside the auditable builtin-only grammar."""


@dataclass(frozen=True, slots=True)
class BoundedBashPlan:
    command: str
    arguments: tuple[str, ...]
    normalized_script: str
    ast_digest: str

    def ast(self) -> dict[str, object]:
        return {"command": self.command, "arguments": list(self.arguments)}


def parse_bounded_bash(script: object) -> BoundedBashPlan:
    if not isinstance(script, str) or not script or script != script.strip():
        raise BashParseError("Bash script must be one trimmed command")
    try:
        encoded = script.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BashParseError("Bash script is not valid UTF-8") from exc
    if len(encoded) > MAX_BASH_SCRIPT_BYTES or any(char in _FORBIDDEN for char in script):
        raise BashParseError("Bash script contains a denied construct")
    try:
        words = shlex.split(script, comments=False, posix=True)
    except ValueError as exc:
        raise BashParseError("Bash quoting is invalid") from exc
    if not words or len(words) > MAX_BASH_ARGUMENTS + 1:
        raise BashParseError("Bash argument count is outside its limit")
    command, *arguments = words
    if command not in ALLOWED_BASH_BUILTINS:
        raise BashParseError("Bash command is not an allowed builtin")
    if command in {"pwd", "true", "false"} and arguments:
        raise BashParseError("Bash builtin does not accept arguments")
    if any(not value or len(value.encode("utf-8")) > MAX_BASH_ARGUMENT_BYTES for value in arguments):
        raise BashParseError("Bash argument is outside its byte limit")
    normalized = shlex.join(words)
    ast_json = json.dumps(
        {"command": command, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(b"agent-builder-bounded-bash-ast-v1\0" + ast_json).hexdigest()
    return BoundedBashPlan(command, tuple(arguments), normalized, digest)


__all__ = [
    "ALLOWED_BASH_BUILTINS",
    "BashParseError",
    "BoundedBashPlan",
    "MAX_BASH_ARGUMENTS",
    "MAX_BASH_SCRIPT_BYTES",
    "parse_bounded_bash",
]

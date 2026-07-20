#!/usr/bin/env python3
"""Execute a qualification command with writes confined to this checkout."""

from __future__ import annotations

import os
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from agent_builder_v2.sandbox import apply_checkout_write_confinement  # noqa: E402


def main(arguments: list[str]) -> int:
    command = arguments[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print(
            "Usage: scripts/contained_exec.py -- COMMAND [ARG ...]",
            file=sys.stderr,
        )
        return 2
    repository_root = REPOSITORY_ROOT.resolve(strict=True)
    os.chdir(repository_root)
    apply_checkout_write_confinement(repository_root)
    os.execvpe(command[0], command, dict(os.environ))
    return 127  # pragma: no cover - exec either replaces us or raises


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

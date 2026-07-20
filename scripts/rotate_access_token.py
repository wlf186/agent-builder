#!/usr/bin/env python3
"""Interactively rotate the project token and restart the managed Gateway."""

from __future__ import annotations

import getpass
import hmac
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from agent_builder_v2.auth import (  # noqa: E402
    MAX_PROJECT_TOKEN_LENGTH,
    MIN_PROJECT_TOKEN_LENGTH,
    ProjectTokenStore,
    is_valid_project_token,
)


def _run_lifecycle(name: str) -> bool:
    result = subprocess.run(
        [str(ROOT / name)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    token = getpass.getpass("New access token: ")
    confirmation = getpass.getpass("Confirm access token: ")
    if not hmac.compare_digest(token, confirmation):
        print("Access token confirmation did not match.", file=sys.stderr)
        return 2
    if not is_valid_project_token(token):
        print(
            "Access token must be "
            f"{MIN_PROJECT_TOKEN_LENGTH}..{MAX_PROJECT_TOKEN_LENGTH} characters "
            "using only letters, digits, '.', '_', '~', '+', or '-'.",
            file=sys.stderr,
        )
        return 2

    store = ProjectTokenStore(ROOT)
    try:
        previous = store.load_or_create()
    except (OSError, RuntimeError, ValueError):
        print("The existing access-token path is unsafe.", file=sys.stderr)
        return 1

    if not _run_lifecycle("stop.sh"):
        print("Gateway shutdown failed; the access token was not changed.", file=sys.stderr)
        return 1

    try:
        store.replace(token)
    except (OSError, RuntimeError, ValueError):
        print("Access-token rotation failed before restart.", file=sys.stderr)
        _run_lifecycle("start.sh")
        return 1

    # Close the race in which another operator starts a process after the
    # first stop but before the atomic file replacement.  Any process launched
    # after this second stop necessarily reads the new token.
    if not _run_lifecycle("stop.sh"):
        try:
            store.replace(previous)
        except (OSError, RuntimeError, ValueError):
            pass
        print("Concurrent Gateway shutdown failed; token rotation aborted.", file=sys.stderr)
        return 1

    if _run_lifecycle("start.sh"):
        print("Access token rotated; all previous browser sessions are invalid.")
        return 0

    rollback_ok = False
    try:
        _run_lifecycle("stop.sh")
        store.replace(previous)
        # A concurrent process could have read the failed candidate between
        # stops, so converge once more before restoring the prior service.
        _run_lifecycle("stop.sh")
        rollback_ok = _run_lifecycle("start.sh")
    except (OSError, RuntimeError, ValueError):
        rollback_ok = False
    if rollback_ok:
        print(
            "Gateway restart failed; the previous token and service were restored.",
            file=sys.stderr,
        )
    else:
        print(
            "Gateway restart failed and automatic token rollback could not recover it.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

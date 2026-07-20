"""Landlock-confined entrypoint for the fixed Git context probe."""

from __future__ import annotations

import os
from pathlib import Path

from .sandbox import apply_read_only_command_confinement
from .workspace_context import _git_executable


def main() -> int:
    workspace = Path.cwd()
    executable = _git_executable()
    apply_read_only_command_confinement(workspace, executable)
    command = (
        str(executable),
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.worktree=.",
        "-c",
        "core.bare=false",
        "-c",
        "color.ui=false",
        "status",
        "--porcelain=v1",
        "--branch",
        "--untracked-files=no",
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CEILING_DIRECTORIES": str(workspace),
        "GIT_DIR": str(workspace / ".git"),
        "GIT_WORK_TREE": str(workspace),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "PAGER": "cat",
    }
    os.execve(executable, command, environment)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

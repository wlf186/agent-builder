"""Parser and kernel-boundary tests for builtin-only bounded Bash."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from agent_builder_v2.bounded_bash import BashParseError, parse_bounded_bash
from agent_builder_v2.capsule import CapsuleManager
from agent_builder_v2.command_exec import CommandExecutionError, CommandExecutor


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script",
    [
        "echo nope",
        "A=1 printf x",
        "printf $HOME",
        "printf `id`",
        "printf $(id)",
        "printf x > out",
        "printf x | true",
        "printf x; true",
        "printf *",
        "(pwd)",
        "source file",
        "exec bash",
        "printf x\\ny",
        " printf x",
    ],
)
def test_parser_rejects_expansion_control_and_ambient_commands(script: str) -> None:
    with pytest.raises(BashParseError):
        parse_bounded_bash(script)


def test_parser_normalizes_one_bounded_builtin_and_binds_ast() -> None:
    first = parse_bounded_bash("printf '%s' 'hello world'")
    second = parse_bounded_bash(first.normalized_script)
    assert first == second
    assert first.ast() == {
        "command": "printf",
        "arguments": ["%s", "hello world"],
    }
    assert len(first.ast_digest) == 64


def test_bounded_bash_executes_without_rc_env_write_or_residual() -> None:
    capsules = CapsuleManager(REPOSITORY_ROOT)
    capsule = capsules.ensure_prototype_agent()
    run_id = uuid.uuid4().hex
    run_root = capsules.create_run_root(capsule, run_id)
    try:
        catalog = CommandExecutor(REPOSITORY_ROOT, REPOSITORY_ROOT / "src", capsule)
        prepared, preview, executor = catalog.prepare(
            {"command_id": "bounded-bash", "script": "printf '%s' EXEC02-OK"},
            run_root,
        )
        assert prepared["command_id"] == "bounded-bash"
        assert prepared["normalized_script"] == "printf %s EXEC02-OK"
        preview_value = json.loads(preview)
        assert preview_value["argv"] == [
            "--noprofile", "--norc", "-c", "printf %s EXEC02-OK"
        ]
        assert preview_value["write_scope"] == "none"
        result = json.loads(executor.execute_prepared(lambda: False))
        assert result["exit_code"] == 0
        assert result["stdout"] == "EXEC02-OK"
        assert result["stderr"] == ""
        assert result["sandbox"] == "singleton-landlock-seccomp-v1"
        assert not list(run_root.glob("runner-*.pid"))
        assert not list((run_root / "output").iterdir())
        with pytest.raises(CommandExecutionError, match="denied construct"):
            catalog.prepare(
                {"command_id": "bounded-bash", "script": "printf $HOME"},
                run_root,
            )
    finally:
        capsules.remove_run_root(capsule, run_id)

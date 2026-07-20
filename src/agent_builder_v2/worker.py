"""One-process-per-Run Worker entrypoint for the walking skeleton."""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import signal
import sys
from typing import Any

from .context import ContextPlanError, ContextPlanReference
from .contracts import LoopLimits, MAX_MESSAGE_BYTES
from .kernel import CancellationToken, HarnessKernel
from .model import BROKER_PROTOCOL_VERSION, MAX_BROKER_FRAME_BYTES, BrokeredStreamingModel
from .sandbox import (
    SandboxAttestation,
    apply_worker_sandbox,
    apply_worker_umask,
    close_worker_file_descriptors,
    verify_worker_file_descriptors,
)
from .tools import prototype_tool_specs_for_ids, prototype_tools, toolset_digest


# JSON control characters can expand to six bytes (for example ``\u0000``).
# Reserve a fixed envelope budget in addition to the public message boundary.
MAX_COMMAND_BYTES = MAX_MESSAGE_BYTES * 6 + 4_096


def _lower_resource_limit(resource_name: int, desired: int) -> None:
    """Lower a limit without trying to raise a stricter inherited hard cap."""

    current_soft, current_hard = resource.getrlimit(resource_name)
    hard = desired
    if current_hard != resource.RLIM_INFINITY:
        hard = min(hard, current_hard)
    soft = desired
    if current_soft != resource.RLIM_INFINITY:
        soft = min(soft, current_soft)
    resource.setrlimit(resource_name, (min(soft, hard), hard))


def _apply_worker_limits() -> None:
    _lower_resource_limit(resource.RLIMIT_CORE, 0)
    _lower_resource_limit(resource.RLIMIT_CPU, 35)
    _lower_resource_limit(resource.RLIMIT_FSIZE, 2 * 1024 * 1024)
    _lower_resource_limit(resource.RLIMIT_NOFILE, 64)
    _lower_resource_limit(resource.RLIMIT_NPROC, 1)
    _lower_resource_limit(resource.RLIMIT_AS, 512 * 1024 * 1024)


def _sandbox_paths() -> tuple[Path, Path, Path]:
    try:
        run_root = Path(os.environ["HARNESS_V2_RUN_ROOT"])
        environment_root = Path(os.environ["HARNESS_V2_ENVIRONMENT_ROOT"])
        source_root = Path(os.environ["HARNESS_V2_SOURCE_ROOT"])
    except KeyError as exc:
        raise RuntimeError("Worker sandbox paths are missing") from exc
    if not all(path.is_absolute() for path in (run_root, environment_root, source_root)):
        raise RuntimeError("Worker sandbox paths must be absolute")
    expected_cwd = run_root / "work"
    if Path.cwd() != expected_cwd:
        raise RuntimeError("Worker working directory does not match its Run root")
    if not Path(sys.executable).is_relative_to(environment_root):
        raise RuntimeError("Worker interpreter escaped its Agent environment")
    module_source = Path(__file__).resolve(strict=True)
    try:
        module_source.relative_to(source_root)
    except ValueError as exc:
        raise RuntimeError("Worker source escaped the declared source root") from exc
    return run_root, environment_root, source_root


def _write_json(value: dict[str, Any]) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_BROKER_FRAME_BYTES:
        raise RuntimeError("Worker output frame exceeded its limit")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _publish_sandbox_ready(attestation: SandboxAttestation) -> None:
    _write_json(
        {
            "internal": "sandbox.ready",
            "version": BROKER_PROTOCOL_VERSION,
            "policy": "harness-v2-worker-v1",
            "landlock_abi": attestation.landlock_abi,
            "seccomp_arch": attestation.seccomp_arch,
            "seccomp_mode": attestation.seccomp_mode,
            "no_new_privileges": attestation.no_new_privileges,
            "parent_pid": attestation.parent_pid,
            "tcp_network_denied": attestation.tcp_network_denied,
            "abstract_unix_scoped": attestation.abstract_unix_scoped,
            "signal_scoped": attestation.signal_scoped,
            "process_creation_denied": attestation.process_creation_denied,
            "descriptor_isolation": attestation.descriptor_isolation,
            "filesystem_write_denied": attestation.filesystem_write_denied,
            "persistent_ipc_denied": attestation.persistent_ipc_denied,
            "dumpable": attestation.dumpable,
        }
    )


def _read_command() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(MAX_COMMAND_BYTES + 1)
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {
        "message",
        "context_plan",
        "loop_limits",
        "effective_tool_ids",
    }:
        raise ValueError("command must be an object")
    message = value.get("message")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > MAX_MESSAGE_BYTES
        or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES
    ):
        raise ValueError("invalid message")
    try:
        context_reference = ContextPlanReference.from_dict(value.get("context_plan"))
    except ContextPlanError as exc:
        raise ValueError("invalid context plan reference") from exc
    value["context_plan"] = context_reference
    try:
        value["loop_limits"] = LoopLimits.from_dict(value.get("loop_limits"))
    except ValueError as exc:
        raise ValueError("invalid loop limits") from exc
    try:
        effective_tools = prototype_tool_specs_for_ids(
            value.get("effective_tool_ids")
        )
    except ValueError as exc:
        raise ValueError("invalid effective Tool set") from exc
    if toolset_digest(effective_tools) != context_reference.toolset_digest:
        raise ValueError("effective Tool set changed")
    value["effective_tools"] = effective_tools
    del value["effective_tool_ids"]
    return value


def main() -> int:
    # Fail closed before reading attacker-controlled input or constructing the
    # Kernel. preexec_fn is deliberately avoided in the asyncio control plane.
    apply_worker_umask()
    close_worker_file_descriptors()
    verify_worker_file_descriptors()
    _apply_worker_limits()
    cancellation = CancellationToken()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: cancellation.cancel())
    signal.signal(signal.SIGINT, lambda _signum, _frame: cancellation.cancel())
    run_root, environment_root, source_root = _sandbox_paths()
    attestation = apply_worker_sandbox(run_root, environment_root, source_root)
    _publish_sandbox_ready(attestation)

    try:
        command = _read_command()
    except (ValueError, json.JSONDecodeError):
        event = {
            "kind": "run.failed",
            "durability": "durable",
            "payload": {
                "code": "invalid_worker_command",
                "message": "The Worker command was invalid.",
                "retryable": False,
            },
        }
        _write_json(event)
        return 2

    effective_tools = command["effective_tools"]
    model = BrokeredStreamingModel(
        sys.stdin.buffer, sys.stdout.buffer, effective_tools=effective_tools
    )
    kernel = HarnessKernel(
        model=model,
        tools=prototype_tools(effective_tools),
        cancellation=cancellation,
        loop_limits=command["loop_limits"],
    )
    for event in kernel.run(command["message"], command["context_plan"]):
        _write_json(event.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

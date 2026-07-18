"""Worker-entry resource and fail-closed ordering tests."""

from __future__ import annotations

import resource
from pathlib import Path
from typing import Any

import pytest

from agent_builder_v2 import worker


def test_lower_resource_limit_preserves_stricter_inherited_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _name: (10, 20))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda name, limits: applied.append((name, limits)),
    )

    worker._lower_resource_limit(resource.RLIMIT_NOFILE, 64)

    assert applied == [(resource.RLIMIT_NOFILE, (10, 20))]


def test_worker_applies_all_limits_before_input_or_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker, "apply_worker_umask", lambda: calls.append("umask"))
    monkeypatch.setattr(
        worker, "close_worker_file_descriptors", lambda: calls.append("close_fds")
    )
    monkeypatch.setattr(
        worker, "verify_worker_file_descriptors", lambda: calls.append("fds")
    )
    monkeypatch.setattr(worker, "_apply_worker_limits", lambda: calls.append("limits"))
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: calls.append("signal"))
    monkeypatch.setattr(
        worker,
        "_sandbox_paths",
        lambda: calls.append("paths")
        or (Path("/run"), Path("/environment"), Path("/source")),
    )
    monkeypatch.setattr(
        worker,
        "apply_worker_sandbox",
        lambda *_args: calls.append("sandbox") or object(),
    )
    monkeypatch.setattr(
        worker, "_publish_sandbox_ready", lambda _value: calls.append("ready")
    )
    monkeypatch.setattr(
        worker,
        "_read_command",
        lambda: calls.append("read") or {"message": "hello"},
    )

    class _Kernel:
        def __init__(self, **_kwargs: Any) -> None:
            calls.append("kernel")

        def run(self, _message: str) -> list[Any]:
            return []

    monkeypatch.setattr(worker, "HarnessKernel", _Kernel)
    monkeypatch.setattr(
        worker,
        "BrokeredStreamingModel",
        lambda *_args: calls.append("model") or object(),
    )

    assert worker.main() == 0
    assert calls[:4] == ["umask", "close_fds", "fds", "limits"]
    assert calls.index("limits") < calls.index("signal") < calls.index("sandbox")
    assert calls.index("sandbox") < calls.index("ready") < calls.index("read")
    assert calls.index("read") < calls.index("kernel")


def test_worker_limit_failure_does_not_read_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = False

    def fail_limits() -> None:
        raise OSError("limit unavailable")

    def read_command() -> dict[str, str]:
        nonlocal read
        read = True
        return {"message": "must not run"}

    monkeypatch.setattr(worker, "_apply_worker_limits", fail_limits)
    monkeypatch.setattr(worker, "_read_command", read_command)
    monkeypatch.setattr(worker, "apply_worker_umask", lambda: None)
    monkeypatch.setattr(worker, "close_worker_file_descriptors", lambda: None)
    monkeypatch.setattr(worker, "verify_worker_file_descriptors", lambda: None)

    with pytest.raises(OSError, match="limit unavailable"):
        worker.main()
    assert read is False


def test_worker_sandbox_failure_does_not_publish_or_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker, "apply_worker_umask", lambda: None)
    monkeypatch.setattr(worker, "close_worker_file_descriptors", lambda: None)
    monkeypatch.setattr(worker, "verify_worker_file_descriptors", lambda: None)
    monkeypatch.setattr(worker, "_apply_worker_limits", lambda: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        worker,
        "_sandbox_paths",
        lambda: (Path("/run"), Path("/environment"), Path("/source")),
    )

    def fail_sandbox(*_args: object) -> object:
        raise OSError("sandbox unavailable")

    monkeypatch.setattr(worker, "apply_worker_sandbox", fail_sandbox)
    monkeypatch.setattr(
        worker, "_publish_sandbox_ready", lambda _value: calls.append("ready")
    )
    monkeypatch.setattr(
        worker, "_read_command", lambda: calls.append("read") or {"message": "bad"}
    )

    with pytest.raises(OSError, match="sandbox unavailable"):
        worker.main()
    assert calls == []

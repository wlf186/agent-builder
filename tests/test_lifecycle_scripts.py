"""Regression tests for checkout-local lifecycle containment."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = ROOT / "scripts" / "run_with_rotating_log.py"
SPEC = importlib.util.spec_from_file_location("run_with_rotating_log", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_service_environment_uses_an_exact_allowlist() -> None:
    source = {
        "PATH": "/usr/bin",
        "TEMP": "/project/tmp",
        "AGENT_BUILDER_ROOT": str(ROOT),
        "AGENT_BUILDER_RUNTIME_DIR": str(ROOT / ".runtime"),
        "AGENT_BUILDER_UV": str(ROOT / ".tools" / "uv"),
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:6006/v1/traces",
        "OBSERVABILITY_MAX_SPANS_PER_TRACE": "512",
        "AGENT_BUILDER_SSRF_ALLOWLIST": "example.test:443",
        "AGENT_BUILDER_EXECUTION_PROCESS_LIMIT": "32",
        "AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT": "2147483648",
        "ANONYMIZED_TELEMETRY": "False",
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=must-not-leak",
        "OTEL_UNREVIEWED_SETTING": "must-not-pass",
        "OBSERVABILITY_UNREVIEWED_SETTING": "must-not-pass",
        "XDG_UNREVIEWED_SETTING": "must-not-pass",
        "UV_UNREVIEWED_SETTING": "must-not-pass",
        "AWS_SECRET_ACCESS_KEY": "must-not-pass",
        "CONDA_PREFIX": "/outside/conda",
        "PIP_TARGET": "/outside/site-packages",
    }

    clean = RUNNER.sanitised_environment(source)

    assert clean["AGENT_BUILDER_UV"] == source["AGENT_BUILDER_UV"]
    assert clean["TEMP"] == "/project/tmp"
    assert clean["AGENT_BUILDER_SSRF_ALLOWLIST"] == "example.test:443"
    assert clean["AGENT_BUILDER_EXECUTION_PROCESS_LIMIT"] == "32"
    assert clean["AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT"] == "2147483648"
    assert clean["ANONYMIZED_TELEMETRY"] == "False"
    assert clean["OBSERVABILITY_MAX_SPANS_PER_TRACE"] == "512"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in clean
    assert not any("UNREVIEWED" in key for key in clean)
    assert "AWS_SECRET_ACCESS_KEY" not in clean
    assert "CONDA_PREFIX" not in clean
    assert "PIP_TARGET" not in clean


def test_log_path_must_be_local_and_must_not_be_a_symlink() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / "checkout"
        runtime = checkout / ".runtime"
        logs = runtime / "logs"
        logs.mkdir(parents=True)
        source = {
            "AGENT_BUILDER_ROOT": str(checkout),
            "AGENT_BUILDER_RUNTIME_DIR": str(runtime),
        }

        expected = logs / "backend.log"
        assert RUNNER.managed_log_path(str(expected), source) == expected
        with pytest.raises(ValueError, match="inside .runtime/logs"):
            RUNNER.managed_log_path(str(checkout / "backend.log"), source)

        outside = Path(temporary) / "outside.log"
        expected.symlink_to(outside)
        with pytest.raises(ValueError, match="symlink"):
            RUNNER.managed_log_path(str(expected), source)
        expected.unlink()
        outside.write_text("outside", encoding="utf-8")
        os.link(outside, expected)
        with pytest.raises(ValueError, match="hard-linked"):
            RUNNER.managed_log_path(str(expected), source)


def test_log_capture_batches_small_pipe_writes_and_flushes_eof() -> None:
    class CountingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.write_count = 0

        def write(self, value: str) -> int:
            self.write_count += 1
            return super().write(value)

    class CountingHandler:
        def __init__(self) -> None:
            self.stream = CountingStream()

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

        def shouldRollover(self, _record: object) -> bool:
            return False

        def doRollover(self) -> None:
            raise AssertionError("unexpected rollover")

        def flush(self) -> None:
            return None

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, time\n"
                "for _ in range(128):\n"
                " os.write(1, b'x')\n"
                " time.sleep(0.002)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert child.stdout is not None
    handler = CountingHandler()
    RUNNER.capture_output(child.stdout, handler, max_bytes=20 * 1024 * 1024)
    assert child.wait(timeout=2) == 0

    assert handler.stream.getvalue() == "x" * 128
    assert handler.stream.write_count == 1


def test_log_supervisor_flushes_child_tail_after_termination_signal() -> None:
    log_path = ROOT / ".runtime" / "logs" / f"signal-test-{os.getpid()}.log"
    log_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_BUILDER_ROOT": str(ROOT),
            "AGENT_BUILDER_RUNTIME_DIR": str(ROOT / ".runtime"),
        }
    )
    child_code = (
        "import signal, time\n"
        "def stop(*_):\n"
        " print('tail-after-term', flush=True)\n"
        " raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.1)\n"
    )
    supervisor = subprocess.Popen(
        [
            sys.executable,
            str(RUNNER_PATH),
            "--new-session",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            child_code,
        ],
        env=environment,
    )
    try:
        # The log writer intentionally batches, so wait for process startup
        # rather than polling the not-yet-flushed "ready" line.
        time.sleep(0.3)
        supervisor.terminate()
        assert supervisor.wait(timeout=3) == 0
        assert "tail-after-term" in log_path.read_text(encoding="utf-8")
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=2)
        log_path.unlink(missing_ok=True)


def test_env_script_rejects_nested_managed_symlink() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / "checkout"
        checkout.mkdir()
        shutil.copy2(ROOT / "env.sh", checkout / "env.sh")
        runtime = checkout / ".runtime"
        runtime.mkdir()
        outside = Path(temporary) / "outside-cache"
        outside.mkdir()
        (runtime / "cache").symlink_to(outside, target_is_directory=True)

        result = subprocess.run(
            ["bash", "-c", "source ./env.sh"],
            cwd=checkout,
            env={"PATH": os.environ["PATH"], "HOME": str(Path(temporary) / "home")},
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert "refusing managed symlink path" in result.stderr
        assert list(outside.iterdir()) == []


def test_purge_removes_a_build_symlink_without_following_it() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / "checkout"
        checkout.mkdir()
        shutil.copy2(ROOT / "env.sh", checkout / "env.sh")
        shutil.copy2(ROOT / "purge.sh", checkout / "purge.sh")
        stop_script = checkout / "stop.sh"
        stop_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stop_script.chmod(0o700)
        frontend = checkout / "frontend"
        frontend.mkdir()
        docs_output = checkout / "docs-site" / ".vitepress" / "dist"
        docs_output.mkdir(parents=True)
        outside = Path(temporary) / "outside-build"
        outside.mkdir()
        sentinel = outside / "must-survive"
        sentinel.write_text("safe", encoding="utf-8")
        (frontend / ".next").symlink_to(outside, target_is_directory=True)

        result = subprocess.run(
            ["bash", "./purge.sh", "build", "--yes"],
            cwd=checkout,
            env={"PATH": os.environ["PATH"], "HOME": str(Path(temporary) / "home")},
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert not (frontend / ".next").exists()
        assert not docs_output.exists()


def test_env_forces_local_paths_and_binds_phoenix_cap_to_byte_limit() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": "/outside/imports",
            "AGENT_BUILDER_SSRF_ALLOWLIST": "operator.example:443",
            "AGENT_BUILDER_BACKEND_URL": "https://attacker.invalid/collect",
            "AGENT_BUILDER_CORS_ORIGINS": "https://attacker.invalid",
            "AGENT_BUILDER_FRONTEND_ORIGINS": "https://attacker.invalid",
            "OBSERVABILITY_STORAGE_WARN_BYTES": "268435456",
            "OBSERVABILITY_STORAGE_MAX_BYTES": "536870912",
            "PHOENIX_DATABASE_ALLOCATED_STORAGE_CAPACITY_GIBIBYTES": "999",
            "CONDA_PREFIX": "/outside/conda",
            "VIRTUAL_ENV": "/outside/venv",
            "PYTHONHOME": "/outside/python",
            "PYTHONUSERBASE": "/outside/python-user",
            "PIP_TARGET": "/outside/site-packages",
            "UV_CONFIG_FILE": "/outside/uv.toml",
            "NODE_PATH": "/outside/node_modules",
            "LD_LIBRARY_PATH": "/outside/libraries",
            "npm_config_prefix": "/outside/npm",
            "TEMP": "/outside/temp",
            "TMP": "/outside/tmp",
            "XDG_RUNTIME_DIR": "/outside/xdg-runtime",
        }
    )
    command = (
        "source ./env.sh && printf '%s\\n' "
        '"$PYTHONPATH" "$HOME" "$TMPDIR" '
        '"$AGENT_BUILDER_SSRF_ALLOWLIST" '
        '"$PHOENIX_DATABASE_ALLOCATED_STORAGE_CAPACITY_GIBIBYTES" '
        '"$TEMP" "$TMP" "$XDG_RUNTIME_DIR" '
        '"$npm_config_prefix" "$TRANSFORMERS_CACHE" '
        '"$AGENT_BUILDER_BACKEND_URL" "$AGENT_BUILDER_CORS_ORIGINS" '
        '"$AGENT_BUILDER_FRONTEND_ORIGINS" '
        '"${CONDA_PREFIX-unset}|${VIRTUAL_ENV-unset}|${PYTHONHOME-unset}|'
        '${PYTHONUSERBASE-unset}|${PIP_TARGET-unset}|${UV_CONFIG_FILE-unset}|'
        '${NODE_PATH-unset}|${LD_LIBRARY_PATH-unset}"'
    )

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.splitlines() == [
        str(ROOT),
        str(ROOT / ".runtime" / "home"),
        str(ROOT / ".runtime" / "tmp"),
        "operator.example:443",
        "0.500000000",
        str(ROOT / ".runtime" / "tmp"),
        str(ROOT / ".runtime" / "tmp"),
        str(ROOT / ".runtime" / "xdg-runtime"),
        str(ROOT / ".runtime" / "npm-prefix"),
        str(ROOT / ".runtime" / "cache" / "huggingface" / "transformers"),
        "http://127.0.0.1:20881",
        "http://127.0.0.1:20815,http://localhost:20815",
        "http://127.0.0.1:20815,http://localhost:20815",
        "unset|unset|unset|unset|unset|unset|unset|unset",
    ]


def test_env_derives_proxy_and_origins_for_custom_ports_and_rejects_external_hosts() -> None:
    custom = subprocess.run(
        [
            "bash",
            "-c",
            (
                "source ./env.sh && printf '%s\\n' "
                '"$AGENT_BUILDER_BACKEND_URL" "$AGENT_BUILDER_CORS_ORIGINS" '
                '"$AGENT_BUILDER_FRONTEND_ORIGINS"'
            ),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "BACKEND_PORT": "31881",
            "FRONTEND_PORT": "31880",
            "AGENT_BUILDER_BACKEND_URL": "https://attacker.invalid/collect",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert custom.stdout.splitlines() == [
        "http://127.0.0.1:31881",
        "http://127.0.0.1:31880,http://localhost:31880",
        "http://127.0.0.1:31880,http://localhost:31880",
    ]

    rejected = subprocess.run(
        ["bash", "-c", "source ./env.sh"],
        cwd=ROOT,
        env={**os.environ, "BACKEND_HOST": "0.0.0.0"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "must be 127.0.0.1 or localhost" in rejected.stderr

    invalid_limit = subprocess.run(
        ["bash", "-c", "source ./env.sh"],
        cwd=ROOT,
        env={**os.environ, "AGENT_BUILDER_EXECUTION_PROCESS_LIMIT": "65"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid_limit.returncode != 0
    assert "AGENT_BUILDER_EXECUTION_PROCESS_LIMIT" in invalid_limit.stderr


def test_bootstrap_reexec_does_not_forward_hostile_secrets_or_install_targets() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / "checkout"
        checkout.mkdir()
        shutil.copy2(ROOT / "env.sh", checkout / "env.sh")
        shutil.copy2(ROOT / "bootstrap.sh", checkout / "bootstrap.sh")
        (checkout / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        tools_dir = checkout / ".tools"
        tools_dir.mkdir()
        fake_uv = tools_dir / "uv"
        fake_uv.write_text(
            """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "--version" ]]; then
  printf 'uv 0.11.7 (test)\\n'
  exit 0
fi
/usr/bin/env | /usr/bin/sort > "$AGENT_BUILDER_RUNTIME_DIR/uv-child.env"
mkdir -p "$AGENT_BUILDER_ROOT/.venv/bin"
printf '#!/usr/bin/env sh\\nexit 0\\n' > "$AGENT_BUILDER_ROOT/.venv/bin/python"
chmod 0700 "$AGENT_BUILDER_ROOT/.venv/bin/python"
""",
            encoding="utf-8",
        )
        fake_uv.chmod(0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "GHTK_AB": "clone-token-must-not-pass",
                "AWS_SECRET_ACCESS_KEY": "aws-secret-must-not-pass",
                "AGENT_BUILDER_API_TOKEN": "api-token-must-not-pass",
                "CONDA_PREFIX": "/outside/conda",
                "VIRTUAL_ENV": "/outside/venv",
                "PIP_TARGET": "/outside/site-packages",
                "UV_CONFIG_FILE": "/outside/uv.toml",
                "NODE_PATH": "/outside/node_modules",
                "LD_LIBRARY_PATH": "/outside/libraries",
                "npm_config_prefix": "/outside/npm",
            }
        )

        result = subprocess.run(
            ["bash", "./bootstrap.sh", "--offline", "--skip-node", "--no-build"],
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        child_environment = dict(
            line.split("=", 1)
            for line in (checkout / ".runtime" / "uv-child.env")
            .read_text(encoding="utf-8")
            .splitlines()
            if "=" in line
        )
        for forbidden in (
            "GHTK_AB",
            "AWS_SECRET_ACCESS_KEY",
            "AGENT_BUILDER_API_TOKEN",
            "CONDA_PREFIX",
            "VIRTUAL_ENV",
            "PIP_TARGET",
            "UV_CONFIG_FILE",
            "NODE_PATH",
            "LD_LIBRARY_PATH",
        ):
            assert forbidden not in child_environment
        assert child_environment["HOME"] == str(checkout / ".runtime" / "home")
        assert child_environment["TEMP"] == str(checkout / ".runtime" / "tmp")
        assert child_environment["TMP"] == str(checkout / ".runtime" / "tmp")
        assert child_environment["NPM_CONFIG_PREFIX"] == str(
            checkout / ".runtime" / "npm-prefix"
        )
        assert "/outside" not in child_environment["PATH"]

#!/usr/bin/env python3
"""Run a command and capture its combined output in bounded rotating logs."""

from __future__ import annotations

import argparse
import codecs
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--clean-env", action="store_true")
    parser.add_argument("log_file")
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    parser.add_argument("--backups", type=int, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def sanitised_environment(source: dict[str, str]) -> dict[str, str]:
    """Return only non-secret configuration required by managed services."""
    exact = {
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LANGUAGE",
        "LC_ALL", "LC_CTYPE", "TZ",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONPYCACHEPREFIX",
        "PIP_CACHE_DIR", "PIP_DISABLE_PIP_VERSION_CHECK", "MPLCONFIGDIR",
        "PLAYWRIGHT_BROWSERS_PATH", "SENTENCE_TRANSFORMERS_HOME",
        "TRANSFORMERS_CACHE", "TORCH_HOME", "TORCH_EXTENSIONS_DIR",
        "TORCHINDUCTOR_CACHE_DIR", "TRITON_CACHE_DIR", "NUMBA_CACHE_DIR",
        "NEXT_TELEMETRY_DISABLED", "DO_NOT_TRACK", "GRADIO_ANALYTICS_ENABLED",
        "ANONYMIZED_TELEMETRY",
        "HF_HOME", "HUGGINGFACE_HUB_CACHE", "HF_HUB_DISABLE_TELEMETRY",
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT",
        "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
        "npm_config_cache", "npm_config_update_notifier", "npm_config_audit",
        "npm_config_fund", "npm_config_userconfig", "npm_config_globalconfig",
        "npm_config_prefix", "NPM_CONFIG_CACHE", "NPM_CONFIG_USERCONFIG",
        "NPM_CONFIG_GLOBALCONFIG", "NPM_CONFIG_PREFIX",
        "PHOENIX_HOST", "PHOENIX_PORT", "PHOENIX_WORKING_DIR",
        "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS", "PHOENIX_TELEMETRY_ENABLED",
        "PHOENIX_ALLOW_EXTERNAL_RESOURCES", "PHOENIX_ALLOWED_SANDBOX_PROVIDERS",
        "PHOENIX_DATABASE_ALLOCATED_STORAGE_CAPACITY_GIBIBYTES",
        "PHOENIX_DATABASE_USAGE_INSERTION_BLOCKING_THRESHOLD_PERCENTAGE",
        "AGENT_BUILDER_ROOT",
        "AGENT_BUILDER_RUNTIME_DIR",
        "AGENT_BUILDER_TOOLS_DIR",
        "AGENT_BUILDER_UV",
        "AGENT_BUILDER_NODE_HOME",
        "AGENT_BUILDER_ENVIRONMENTS_DIR",
        "AGENT_BUILDER_TOKEN_FILE",
        "AGENT_BUILDER_PACKAGE_ALLOWLIST",
        "AGENT_BUILDER_CORS_ORIGINS",
        "AGENT_BUILDER_HOST",
        "AGENT_BUILDER_BACKEND_URL",
        "AGENT_BUILDER_FRONTEND_ORIGINS",
        "AGENT_BUILDER_EXECUTION_OUTPUT_LIMIT",
        "AGENT_BUILDER_EXECUTION_MEMORY_LIMIT",
        "AGENT_BUILDER_EXECUTION_FILE_LIMIT",
        "AGENT_BUILDER_EXECUTION_WORKDIR_LIMIT",
        "AGENT_BUILDER_EXECUTION_PROCESS_LIMIT",
        "AGENT_BUILDER_EXECUTION_AGGREGATE_MEMORY_LIMIT",
        "AGENT_BUILDER_BLOCKING_WORKERS",
        "AGENT_BUILDER_SSRF_ALLOWLIST",
        "AGENT_BUILDER_ALLOW_STDIO_MCP",
        "AGENT_BUILDER_SKILL_NETWORK",
        "BACKEND_HOST", "BACKEND_PORT", "FRONTEND_HOST", "FRONTEND_PORT",
        "DOCS_HOST", "DOCS_PORT", "MCP_SSE_HOST", "MCP_SSE_PORT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_SERVICE_NAME",
        "OBSERVABILITY_ENABLED", "OBSERVABILITY_BACKEND",
        "OBSERVABILITY_SUCCESS_SAMPLE_RATE", "OBSERVABILITY_SLOW_REQUEST_MS",
        "OBSERVABILITY_KEEP_ERRORS", "OBSERVABILITY_KEEP_SLOW",
        "OBSERVABILITY_BATCH_DELAY_MS", "OBSERVABILITY_BATCH_SIZE",
        "OBSERVABILITY_BATCH_QUEUE_SIZE", "OBSERVABILITY_EXPORT_TIMEOUT_MS",
        "OBSERVABILITY_MAX_PENDING_TRACES", "OBSERVABILITY_MAX_SPANS_PER_TRACE",
        "OBSERVABILITY_MAX_TRACE_BYTES", "OBSERVABILITY_PRIORITY_QUEUE_TRACES",
        "OBSERVABILITY_PRIORITY_BATCH_DELAY_MS",
        "OBSERVABILITY_MAX_ATTRIBUTE_LENGTH", "OBSERVABILITY_MAX_COLLECTION_ITEMS",
        "OBSERVABILITY_MAX_ATTRIBUTE_DEPTH", "OBSERVABILITY_STORAGE_WARN_BYTES",
        "OBSERVABILITY_STORAGE_MAX_BYTES", "OBSERVABILITY_PRICING_JSON",
        "APP_VERSION", "APP_ENV",
    }
    return {key: value for key, value in source.items() if key in exact}


def managed_log_path(raw_path: str, source: dict[str, str]) -> Path:
    """Resolve a log path without following managed symlinks outside the checkout."""
    try:
        root = Path(source["AGENT_BUILDER_ROOT"]).resolve(strict=True)
        runtime = Path(os.path.abspath(source["AGENT_BUILDER_RUNTIME_DIR"]))
    except (KeyError, OSError) as exc:
        raise ValueError("project runtime environment is missing or invalid") from exc
    try:
        runtime.relative_to(root)
    except ValueError as exc:
        raise ValueError("runtime directory is outside the checkout") from exc

    log_root = runtime / "logs"
    candidate = Path(os.path.abspath(raw_path))
    try:
        candidate.relative_to(log_root)
    except ValueError as exc:
        raise ValueError("log file must be inside .runtime/logs") from exc

    for path in (runtime, log_root, candidate.parent, candidate):
        current = root
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError("managed log path is outside the checkout") from exc
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ValueError(f"managed log path contains a symlink: {current}")
    if candidate.exists():
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed log path is not a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("managed log file must not be hard-linked")
    return candidate


def _write_log_record(handler: RotatingFileHandler, text: str) -> None:
    if not text:
        return
    record = logging.LogRecord("process", logging.INFO, "", 0, text, (), None)
    handler.acquire()
    try:
        if handler.shouldRollover(record):
            handler.doRollover()
        assert handler.stream is not None
        handler.stream.write(text)
        handler.flush()
    finally:
        handler.release()


def capture_output(
    stream: object,
    handler: RotatingFileHandler,
    *,
    max_bytes: int,
    flush_interval: float = 0.75,
) -> None:
    """Batch pipe output by size/time before writing it to rotating storage."""
    file_descriptor = stream.fileno()  # type: ignore[attr-defined]
    selector = selectors.DefaultSelector()
    selector.register(file_descriptor, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = bytearray()
    flush_bytes = min(64 * 1024, max(1024, max_bytes // 2))
    last_flush = time.monotonic()

    def flush(count: int | None = None, *, final: bool = False) -> None:
        nonlocal last_flush
        if count is None:
            payload = bytes(pending)
            pending.clear()
        else:
            payload = bytes(pending[:count])
            del pending[:count]
        text = decoder.decode(payload, final=final)
        _write_log_record(handler, text)
        last_flush = time.monotonic()

    try:
        while True:
            elapsed = time.monotonic() - last_flush
            timeout = max(0.0, flush_interval - elapsed)
            try:
                events = selector.select(timeout)
            except InterruptedError:
                events = []
            if events:
                try:
                    chunk = os.read(file_descriptor, 64 * 1024)
                except InterruptedError:
                    continue
                if not chunk:
                    while len(pending) > flush_bytes:
                        flush(flush_bytes)
                    flush(final=True)
                    break
                pending.extend(chunk)
            while len(pending) >= flush_bytes:
                flush(flush_bytes)
            if pending and time.monotonic() - last_flush >= flush_interval:
                flush()
    finally:
        selector.close()


def main() -> int:
    args = parse_args()
    os.umask(0o077)
    if args.new_session and os.name != "nt" and os.getpid() != os.getsid(0):
        os.setsid()
    child_env = os.environ.copy()
    if args.clean_env:
        already_clean = os.environ.get("_AGENT_BUILDER_CLEAN_ENV") == "1"
        child_env = sanitised_environment(os.environ)
        child_env["_AGENT_BUILDER_CLEAN_ENV"] = "1"
        if not already_clean:
            # execve replaces the supervisor's initial environment too, so
            # unrelated caller secrets do not remain visible through /proc.
            os.execve(
                sys.executable,
                [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                child_env,
            )
        os.environ.clear()
        os.environ.update(child_env)

    try:
        log_path = managed_log_path(args.log_file, os.environ)
    except ValueError as exc:
        print(f"log supervisor: {exc}", file=sys.stderr)
        return 2
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max(1024, args.max_bytes),
        backupCount=max(1, args.backups),
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600, follow_symlinks=False)
    handler.setFormatter(logging.Formatter("%(message)s"))
    child = subprocess.Popen(
        args.command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env,
        bufsize=0,
    )

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)

    try:
        assert child.stdout is not None
        capture_output(child.stdout, handler, max_bytes=max(1024, args.max_bytes))
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        handler.close()


if __name__ == "__main__":
    sys.exit(main())

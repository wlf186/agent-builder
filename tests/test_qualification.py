"""Qualification evidence, containment, redaction, metrics, and HTTP flow."""

from __future__ import annotations

import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import sys
import threading
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qualify_runtime.py"
SPEC = importlib.util.spec_from_file_location("agent_builder_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualification
SPEC.loader.exec_module(qualification)


def _repository(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _token(repository: Path, value: str = "a" * 64) -> Path:
    parent = repository / ".runtime" / "secrets"
    parent.mkdir(parents=True, mode=0o700)
    path = parent / "web-bootstrap-token"
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def test_rr_output_is_contained_private_and_no_replace(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    directory = qualification.prepare_rr_directory(repository, "RR-QUA-20260718-01")
    assert directory.is_relative_to(repository / ".runtime" / "qualification")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    final = qualification.publish_summary(directory, b'{"result":"pass"}\n')
    assert final.read_bytes() == b'{"result":"pass"}\n'
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    with pytest.raises(qualification.QualificationError, match="summary_already_exists"):
        qualification.publish_summary(directory, b"{}\n")
    with pytest.raises(qualification.QualificationError, match="rr_already_exists"):
        qualification.prepare_rr_directory(repository, "RR-QUA-20260718-01")
    with pytest.raises(qualification.QualificationError, match="invalid_rr_id"):
        qualification.prepare_rr_directory(repository, "../../outside")


def test_qualification_output_symlink_cannot_redirect_writes(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    outside = _repository(tmp_path / "outside")
    runtime = repository / ".runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "qualification").symlink_to(outside, target_is_directory=True)

    with pytest.raises(qualification.QualificationError, match="unsafe_directory"):
        qualification.prepare_rr_directory(repository, "RR-QUA-20260718-01")
    assert list(outside.iterdir()) == []


def test_token_is_private_nofollow_and_summary_redaction_is_fail_closed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    token_path = _token(repository)
    token, identity = qualification.read_project_token(repository)
    assert token == "a" * 64
    assert qualification.token_identity(repository) == identity

    safe = {"result": "pass", "counts": {"runs": 2}}
    encoded = qualification.encode_summary(safe, {token}, repository)
    assert token.encode() not in encoded
    with pytest.raises(qualification.QualificationError, match="summary_redaction_failed"):
        qualification.encode_summary({"leak": token}, {token}, repository)
    with pytest.raises(qualification.QualificationError, match="runtime_id_field"):
        qualification.encode_summary({"run_id": "b" * 32}, set(), repository)

    token_path.chmod(0o644)
    with pytest.raises(qualification.QualificationError, match="unsafe_token_mode"):
        qualification.read_project_token(repository)
    token_path.unlink()
    outside = tmp_path / "outside-token"
    outside.write_text("a" * 64 + "\n", encoding="ascii")
    outside.chmod(0o600)
    token_path.symlink_to(outside)
    with pytest.raises(qualification.QualificationError, match="symlink_in_managed_path"):
        qualification.read_project_token(repository)


def test_qualification_accepts_bounded_operator_token_and_rejects_bad_lines(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    token_path = _token(repository, "operator-token_2026")
    token, _identity = qualification.read_project_token(repository)
    assert token == "operator-token_2026"

    token_path.write_bytes(b"operator-token_2026\n\n")
    token_path.chmod(0o600)
    with pytest.raises(qualification.QualificationError, match="invalid_token"):
        qualification.read_project_token(repository)


def test_storage_metrics_count_logical_allocated_and_reject_links(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    agent = repository / "data" / "agents" / ("1" * 32)
    agent.mkdir(parents=True)
    database = agent / "state.sqlite"
    database.write_bytes(b"x" * 8193)
    usage = qualification.measure_storage(repository, "state")
    assert usage.logical_bytes == 8193
    assert usage.allocated_bytes >= 8193
    assert usage.files == 1

    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    (agent / "state.sqlite-wal").symlink_to(outside)
    with pytest.raises(qualification.QualificationError, match="symlink"):
        qualification.measure_storage(repository, "wal")
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cache_metrics_count_but_never_follow_symlinks(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    cache = repository / ".runtime" / "cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * 1_000_000)
    link = cache / "package-link"
    link.symlink_to(outside, target_is_directory=True)

    usage = qualification.measure_storage(repository, "cache")

    assert usage.files == 1
    assert usage.logical_bytes == os.lstat(link).st_size
    assert usage.logical_bytes < 1_000_000


def test_process_io_is_bounded_to_a_stable_process_identity() -> None:
    pid = os.getpid()
    marker = qualification._process_marker(pid)
    values = qualification.read_process_io(pid, marker)
    assert set(values) == set(qualification.IO_FIELDS)
    assert all(isinstance(value, int) and value >= 0 for value in values.values())
    with pytest.raises(qualification.QualificationError, match="process_identity_changed"):
        qualification.read_process_io(pid, marker + "0")


def test_qualification_limitations_are_architecture_and_observer_specific() -> None:
    x86 = qualification._qualification_limitations(
        "x86_64",
        observed_libc_sync_calls=False,
    )
    arm = qualification._qualification_limitations(
        "aarch64",
        observed_libc_sync_calls=True,
    )

    assert "aarch64_native_qualification_not_included" in x86
    assert "exact_libc_sync_call_count_not_observed" in x86
    assert "aarch64_native_qualification_not_included" not in arm
    assert "exact_libc_sync_call_count_not_observed" not in arm
    assert "kernel_physical_flush_not_observed" in arm
    assert "ssd_smart_not_observed" in arm
    assert qualification._expected_worker_process_images(
        {"completed_turns": 4, "cancelled_runs": 1}
    ) == 5


def test_gateway_pid_record_accepts_only_known_sync_counter_abi(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(parents=True, mode=0o700)
    record = runtime / "gateway.pid"
    record.write_text(
        "schema=1\n"
        "role=gateway\n"
        "pid=12345\n"
        "pgid=12345\n"
        "marker=linux:123\n"
        f"root={repository}\n"
        "sync_counter=libc-sync-calls-v1\n"
        "web_pid=12346\n"
        "web_marker=linux:124\n",
        encoding="utf-8",
    )
    record.chmod(0o600)

    assert qualification.read_gateway_pid_record(repository)["sync_counter"] == (
        "libc-sync-calls-v1"
    )
    record.write_text(record.read_text().replace("libc-sync-calls-v1", "unknown"))
    record.chmod(0o600)
    with pytest.raises(
        qualification.QualificationError, match="invalid_gateway_pid_record"
    ):
        qualification.read_gateway_pid_record(repository)


def test_managed_gateway_process_chain_binds_supervisor_and_web_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path / "repository")
    runtime = repository / ".runtime" / "control-plane"
    runtime.mkdir(parents=True, mode=0o700)
    record = runtime / "gateway.pid"
    record.write_text(
        "schema=1\n"
        "role=gateway\n"
        "pid=12345\n"
        "pgid=12345\n"
        "marker=linux:101\n"
        f"root={repository}\n"
        "web_pid=12346\n"
        "web_marker=linux:102\n",
        encoding="utf-8",
    )
    record.chmod(0o600)
    markers = {12345: "101", 12346: "102"}
    relationships = {12345: (1, 12345), 12346: (12345, 12345)}
    python = os.fsencode(str(repository / ".venv" / "bin" / "python"))
    commands = {
        12345: (
            python,
            os.fsencode(str(repository / "scripts" / "log_supervisor.py")),
            b"--",
            python,
            b"-m",
            b"agent_builder_v2.web",
        ),
        12346: (python, b"-m", b"agent_builder_v2.web"),
    }
    monkeypatch.setattr(
        qualification, "_process_marker", lambda pid: markers[pid]
    )
    monkeypatch.setattr(
        qualification,
        "_process_parent_and_group",
        lambda pid: relationships[pid],
    )
    monkeypatch.setattr(
        qualification, "_process_command_parts", lambda pid: commands[pid]
    )
    monkeypatch.setattr(qualification, "_process_cwd", lambda _pid: repository)

    assert qualification.managed_gateway_processes(repository) == {
        "supervisor": 12345,
        "gateway": 12346,
    }

    relationships[12346] = (99999, 12345)
    with pytest.raises(
        qualification.QualificationError, match="invalid_gateway_process_tree"
    ):
        qualification.managed_gateway_processes(repository)


class _ApiState:
    token = "operator-token_2026"
    csrf = "csrf-secret-value"
    next_id = 1
    conversations: dict[str, list[str]] = {}
    run_owner: dict[str, str] = {}
    cancelled: set[str] = set()
    deleted_runs: set[str] = set()

    @classmethod
    def identifier(cls) -> str:
        value = f"{cls.next_id:032x}"
        cls.next_id += 1
        return value


class _ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, status_code: int, value: object) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _empty(self, status_code: int) -> None:
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok", "agent_ready": True, "model": "test-model", "sandbox": "landlock+seccomp"})
            return
        session_match = __import__("re").fullmatch(r"/api/sessions/([a-f0-9]{32})", self.path)
        if session_match:
            self._json(404, {"detail": "not found"})
            return
        event_match = __import__("re").fullmatch(r"/api/runs/([a-f0-9]{32})/events", self.path)
        if event_match:
            run_id = event_match.group(1)
            if run_id in _ApiState.deleted_runs:
                self._json(404, {"detail": "not found"})
                return
            terminal = "run.cancelled" if run_id in _ApiState.cancelled else "run.completed"
            frames = []
            for seq, kind in ((1, "run.started"), (2, terminal)):
                envelope = json.dumps({"run_id": run_id, "seq": seq, "kind": kind, "payload": {}}, separators=(",", ":"))
                frames.append(f"id: {seq}\nevent: {kind}\ndata: {envelope}\n\n")
            encoded = "".join(frames).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/auth/login":
            body = self._body()
            assert body == {"token": _ApiState.token}
            encoded = json.dumps({"csrf_token": _ApiState.csrf}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "abv2_session=session-secret-value; Path=/; HttpOnly")
            self.send_header("Set-Cookie", f"abv2_csrf_seed={_ApiState.csrf}; Path=/; HttpOnly")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if self.path == "/api/sessions":
            self._body()
            conversation_id = _ApiState.identifier()
            _ApiState.conversations[conversation_id] = []
            self._json(201, {"session_id": conversation_id})
            return
        run_match = __import__("re").fullmatch(r"/api/sessions/([a-f0-9]{32})/runs", self.path)
        if run_match:
            body = self._body()
            if len(str(body.get("message", ""))) > 8192:
                self._json(400, {"detail": "rejected"})
                return
            conversation_id = run_match.group(1)
            run_id = _ApiState.identifier()
            _ApiState.conversations[conversation_id].append(run_id)
            _ApiState.run_owner[run_id] = conversation_id
            self._json(202, {"run_id": run_id, "session_id": conversation_id, "events_url": f"/api/runs/{run_id}/events"})
            return
        cancel_match = __import__("re").fullmatch(r"/api/runs/([a-f0-9]{32})/cancel", self.path)
        if cancel_match:
            _ApiState.cancelled.add(cancel_match.group(1))
            self._json(202, {"accepted": True})
            return
        if self.path == "/api/auth/logout":
            self._empty(204)
            return
        self._json(404, {"detail": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        match = __import__("re").fullmatch(r"/api/sessions/([a-f0-9]{32})", self.path)
        if not match:
            self._json(404, {"detail": "not found"})
            return
        conversation_id = match.group(1)
        _ApiState.deleted_runs.update(_ApiState.conversations.pop(conversation_id, []))
        self._empty(204)


def test_real_http_workload_contract_and_secret_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    _ApiState.next_id = 1
    _ApiState.conversations = {}
    _ApiState.run_owner = {}
    _ApiState.cancelled = set()
    _ApiState.deleted_runs = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(qualification, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(qualification, "ORIGIN", f"http://127.0.0.1:{server.server_port}")
    try:
        workload = qualification.Workload(2, time.monotonic() + 30)
        workload.run(_ApiState.token)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert workload.counts == {
        "health_checks": 2,
        "conversations_created": 2,
        "conversations_deleted_and_verified": 2,
        "completed_turns": 2,
        "cancelled_runs": 1,
        "rejected_requests": 1,
    }
    assert workload.conversations == {}
    assert workload.active_runs == set()
    assert _ApiState.token in workload.client.forbidden
    assert _ApiState.csrf in workload.client.forbidden
    assert "session-secret-value" in workload.client.forbidden


def test_api_client_rejects_non_relative_api_path_without_network() -> None:
    client = qualification.ApiClient()
    with pytest.raises(qualification.QualificationError, match="unsafe_api_path"):
        client._request("GET", "//attacker.invalid/", {200})

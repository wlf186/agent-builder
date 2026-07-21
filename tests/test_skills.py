"""Package, lifecycle and singleton sandbox tests for versioned Skills."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import time
import uuid
import zipfile

import pytest

from agent_builder_v2.capsule import CapsuleManager
from agent_builder_v2.command_exec import CommandExecutor
from agent_builder_v2.permissions import CapabilityRequest
from agent_builder_v2.skills import SkillError, SkillExecutor, SkillRegistry, inspect_skill_archive
from agent_builder_v2.tools import runtime_effective_toolset


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _archive(skill_id: str, version: str, *, dependencies=None, source: str | None = None) -> bytes:
    manifest = {
        "schema_version": 1,
        "skill_id": skill_id,
        "version": version,
        "display_name": "Qualification Skill",
        "entrypoint": "main.py",
        "capabilities": [],
        "dependencies": [] if dependencies is None else dependencies,
    }
    if source is None:
        source = """import json, os, socket
value = json.loads(os.environ['AGENT_BUILDER_SKILL_INPUT'])
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except OSError:
    network_denied = True
else:
    network_denied = False
try:
    os.fork()
except OSError:
    fork_denied = True
else:
    fork_denied = False
print(json.dumps({'echo': value.get('text'), 'network_denied': network_denied, 'fork_denied': fork_denied}, sort_keys=True))
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill.json", json.dumps(manifest, sort_keys=True))
        archive.writestr("main.py", source)
    return output.getvalue()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _request(prepared: dict[str, object], preview: str) -> CapabilityRequest:
    now = int(time.time() * 1000)
    return CapabilityRequest.create(
        agent_id="00000000-0000-4000-8000-000000000001",
        capsule_generation=1,
        conversation_id="2" * 32,
        run_id="3" * 32,
        call_id="skill-call",
        capability_id="skill/run",
        toolset_digest=runtime_effective_toolset().toolset_digest,
        policy_digest="4" * 64,
        arguments=prepared,
        preview=preview,
        expires_at_milliseconds=now + 30_000,
        now_milliseconds=now,
    )


def test_archive_integrity_manifest_dependencies_and_paths_fail_closed() -> None:
    skill_id = uuid.uuid4().hex
    raw = _archive(skill_id, "1.0.0")
    manifest, canonical, source, content_digest = inspect_skill_archive(raw, _digest(raw))
    assert manifest["skill_id"] == skill_id
    assert json.loads(canonical)["version"] == "1.0.0"
    assert source.startswith(b"import json")
    assert len(content_digest) == 64
    with pytest.raises(SkillError, match="digest"):
        inspect_skill_archive(raw, "0" * 64)
    dependency = _archive(skill_id, "1.0.0", dependencies=["requests"])
    with pytest.raises(SkillError, match="manifest"):
        inspect_skill_archive(dependency, _digest(dependency))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../skill.json", "{}")
        archive.writestr("main.py", "pass")
    traversal = output.getvalue()
    with pytest.raises(SkillError, match="file set"):
        inspect_skill_archive(traversal, _digest(traversal))


def test_install_upgrade_execute_tamper_and_delete_leave_no_residue(tmp_path: Path) -> None:
    capsules = CapsuleManager(REPOSITORY_ROOT)
    capsule = capsules.ensure_prototype_agent()
    registry = SkillRegistry(REPOSITORY_ROOT, capsule, tmp_path / "skills.sqlite")
    skill_id = uuid.uuid4().hex
    raw = _archive(skill_id, "1.0.0")
    record = registry.install(raw, _digest(raw))
    data_root = registry.data_root / skill_id
    environment_root = registry.runtime_root / skill_id
    assert record.version == "1.0.0"
    assert (data_root / "main.py").is_file()
    assert (environment_root / "bin" / "python").is_file()

    commands = CommandExecutor(REPOSITORY_ROOT, REPOSITORY_ROOT / "src", capsule)
    executor = SkillExecutor(registry, commands)
    run_id = uuid.uuid4().hex
    run_root = capsules.create_run_root(capsule, run_id)
    try:
        prepared, preview, dispatched = executor.prepare(
            {"skill_id": skill_id, "input_json": '{"text":"SKILL-OK"}'},
            run_root,
        )
        result = json.loads(dispatched.execute(_request(prepared, preview), lambda: False))
        payload = json.loads(result["stdout"])
        assert result["exit_code"] == 0
        assert payload == {
            "echo": "SKILL-OK",
            "fork_denied": True,
            "network_denied": True,
        }
        assert not list(run_root.glob("runner-*.pid"))
        assert not list((run_root / "output").iterdir())

        prepared, preview, dispatched = executor.prepare(
            {"skill_id": skill_id, "input_json": "{}"}, run_root
        )
        (data_root / "main.py").write_text("print('tampered')\n", encoding="utf-8")
        with pytest.raises(SkillError, match="changed after approval"):
            dispatched.execute(_request(prepared, preview), lambda: False)
    finally:
        capsules.remove_run_root(capsule, run_id)

    # Upgrade replaces both package and dedicated environment after the prior
    # version has no active execution.
    upgraded_raw = _archive(skill_id, "2.0.0", source="print('SKILL-V2')\n")
    upgraded = registry.install(upgraded_raw, _digest(upgraded_raw))
    assert upgraded.version == "2.0.0"
    assert (data_root / "main.py").read_text(encoding="utf-8") == "print('SKILL-V2')\n"
    registry.delete(skill_id)
    assert not data_root.exists()
    assert not environment_root.exists()
    assert registry.list() == ()
    registry.close()

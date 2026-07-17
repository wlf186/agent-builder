from __future__ import annotations

import copy
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pytest

from src import mcp_registry as mcp_registry_module
from src import model_service_registry as model_registry_module
from src.mcp_registry import MCPServiceRegistry
from src.model_service_registry import ModelServiceRegistry
from src.models import (
    MCPAuthType,
    MCPConnectionType,
    MCPServiceConfig,
    ModelProvider,
    ModelServiceConfig,
)
from src.secret_store import EncryptedSecretStore


class MemorySecretStore:
    """Thread-safe test double with controllable one-shot write failures."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.before_set: Callable[[dict[str, Any], "MemorySecretStore"], None] | None = None
        self.fail_next_before = False
        self.fail_next_after = False
        self._lock = threading.RLock()

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        with self._lock:
            return {
                key: copy.deepcopy(self.values[key])
                for key in keys
                if key in self.values
            }

    def set_many(self, updates: dict[str, Any]) -> None:
        with self._lock:
            updates = copy.deepcopy(dict(updates))
            self.calls.append(updates)
            if self.before_set is not None:
                self.before_set(updates, self)
            if self.fail_next_before:
                self.fail_next_before = False
                raise OSError("injected secret-store failure")

            candidate = copy.deepcopy(self.values)
            for key, value in updates.items():
                if value is None:
                    candidate.pop(key, None)
                else:
                    candidate[key] = copy.deepcopy(value)
            self.values = candidate

            if self.fail_next_after:
                self.fail_next_after = False
                raise OSError("injected post-write secret-store failure")


def model_config(
    name: str,
    secret: str | None = None,
    *,
    description: str = "",
) -> ModelServiceConfig:
    return ModelServiceConfig(
        name=name,
        description=description,
        provider=ModelProvider.ZHIPU,
        base_url="https://model.invalid/v1",
        api_key=secret,
        selected_model="glm-test",
        available_models=["glm-test", "glm-backup"],
    )


def mcp_config(
    name: str,
    secret: str | None = None,
    *,
    description: str = "",
) -> MCPServiceConfig:
    return MCPServiceConfig(
        name=name,
        description=description,
        connection_type=MCPConnectionType.STDIO,
        command="uvx",
        args=["--token", secret] if secret else [],
        env={"SERVICE_TOKEN": secret} if secret else {},
        auth_type=MCPAuthType.BEARER if secret else MCPAuthType.NONE,
        auth_value=secret,
        headers={"Authorization": f"Bearer {secret}"} if secret else {},
    )


REGISTRY_CASES = (
    pytest.param(
        model_registry_module,
        ModelServiceRegistry,
        model_config,
        "model_services.json",
        "model:{name}:api_key",
        id="model",
    ),
    pytest.param(
        mcp_registry_module,
        MCPServiceRegistry,
        mcp_config,
        "mcp_services.json",
        "mcp:{name}:connection",
        id="mcp",
    ),
)


def make_registry(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    registry_type: type,
    path: Path,
    store: Any,
) -> Any:
    monkeypatch.setattr(module, "get_registry_secret_store", lambda: store)
    return registry_type(path)


def assert_compact_private_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    decoded = json.loads(raw.decode("utf-8"))
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert raw == canonical
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return decoded


def test_metadata_is_compact_private_and_contains_no_plaintext_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySecretStore()
    model = make_registry(
        monkeypatch,
        model_registry_module,
        ModelServiceRegistry,
        tmp_path / "model",
        store,
    )
    mcp = make_registry(
        monkeypatch,
        mcp_registry_module,
        MCPServiceRegistry,
        tmp_path / "mcp",
        store,
    )

    assert model.create_service(model_config("primary", "model-secret-value"))
    assert mcp.create_service(mcp_config("tools", "mcp-secret-value"))

    model_path = tmp_path / "model" / "model_services.json"
    mcp_path = tmp_path / "mcp" / "mcp_services.json"
    model_data = assert_compact_private_json(model_path)
    mcp_data = assert_compact_private_json(mcp_path)

    assert "api_key" not in model_data["primary"]
    assert "model-secret-value" not in model_path.read_text(encoding="utf-8")
    for field in ("args", "env", "headers", "auth_value"):
        assert field not in mcp_data["tools"]
    assert "mcp-secret-value" not in mcp_path.read_text(encoding="utf-8")
    assert store.values["model:primary:api_key"] == "model-secret-value"
    assert store.values["mcp:tools:connection"]["auth_value"] == "mcp-secret-value"

    reloaded_model = make_registry(
        monkeypatch,
        model_registry_module,
        ModelServiceRegistry,
        tmp_path / "model",
        store,
    )
    reloaded_mcp = make_registry(
        monkeypatch,
        mcp_registry_module,
        MCPServiceRegistry,
        tmp_path / "mcp",
        store,
    )
    assert reloaded_model.get_service("primary").api_key == "model-secret-value"
    assert reloaded_mcp.get_service("tools").auth_value == "mcp-secret-value"


def test_real_secret_store_remains_encrypted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = tmp_path / "runtime" / "registry-secrets.enc"
    store = EncryptedSecretStore(secret_path, token="x" * 64)
    registry = make_registry(
        monkeypatch,
        model_registry_module,
        ModelServiceRegistry,
        tmp_path / "data",
        store,
    )
    assert registry.create_service(model_config("encrypted", "never-in-plaintext"))
    assert b"never-in-plaintext" not in secret_path.read_bytes()

    reloaded = make_registry(
        monkeypatch,
        model_registry_module,
        ModelServiceRegistry,
        tmp_path / "data",
        store,
    )
    assert reloaded.get_service("encrypted").api_key == "never-in-plaintext"


def test_getters_and_inputs_are_deep_copied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySecretStore()
    model = make_registry(
        monkeypatch,
        model_registry_module,
        ModelServiceRegistry,
        tmp_path / "model",
        store,
    )
    mcp = make_registry(
        monkeypatch,
        mcp_registry_module,
        MCPServiceRegistry,
        tmp_path / "mcp",
        store,
    )

    model_input = model_config("model", "model-key")
    mcp_input = mcp_config("mcp", "mcp-key")
    assert model.create_service(model_input)
    assert mcp.create_service(mcp_input)

    model_input.available_models.append("caller-mutation")
    mcp_input.args.append("caller-mutation")
    mcp_input.env["CALLER"] = "mutation"
    assert "caller-mutation" not in model.get_service("model").available_models
    assert "caller-mutation" not in mcp.get_service("mcp").args
    assert "CALLER" not in mcp.get_service("mcp").env

    model_result = model.get_service("model")
    model_result.available_models.append("result-mutation")
    model.list_services()[0].available_models.append("list-mutation")
    assert "result-mutation" not in model.get_service("model").available_models
    assert "list-mutation" not in model.get_service("model").available_models

    mcp_result = mcp.get_service("mcp")
    mcp_result.headers["Result"] = "mutation"
    mcp.list_services()[0].env["LIST"] = "mutation"
    mcp.get_services_by_names(["mcp"])[0].args.append("named-mutation")
    assert "Result" not in mcp.get_service("mcp").headers
    assert "LIST" not in mcp.get_service("mcp").env
    assert "named-mutation" not in mcp.get_service("mcp").args


@pytest.mark.parametrize(
    "module,registry_type,config_factory,filename,secret_key_template",
    REGISTRY_CASES,
)
@pytest.mark.parametrize("corruption", ("root", "name", "entry", "limit"))
def test_load_is_fail_closed_for_whole_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    registry_type: type,
    config_factory: Callable[..., Any],
    filename: str,
    secret_key_template: str,
    corruption: str,
) -> None:
    data_dir = tmp_path / corruption
    data_dir.mkdir()
    if corruption == "root":
        payload: Any = []
    elif corruption == "name":
        payload = {
            "valid": config_factory("valid").model_dump(mode="json"),
            "wrong-key": config_factory("different-name").model_dump(mode="json"),
        }
    elif corruption == "entry":
        payload = {
            "valid": config_factory("valid").model_dump(mode="json"),
            "invalid": "not-an-object",
        }
    else:
        payload = {
            f"service-{index}": {"name": f"service-{index}"}
            for index in range(101)
        }
    (data_dir / filename).write_text(json.dumps(payload), encoding="utf-8")

    registry = make_registry(
        monkeypatch,
        module,
        registry_type,
        data_dir,
        MemorySecretStore(),
    )
    assert registry.list_services() == []


@pytest.mark.parametrize(
    "module,registry_type,config_factory,filename,secret_key_template",
    REGISTRY_CASES,
)
def test_legacy_plaintext_is_migrated_and_metadata_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    registry_type: type,
    config_factory: Callable[..., Any],
    filename: str,
    secret_key_template: str,
) -> None:
    store = MemorySecretStore()
    config = config_factory("legacy", "legacy-plaintext-secret")
    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    metadata_path = data_dir / filename
    metadata_path.write_text(
        json.dumps({"legacy": config.model_dump(mode="json")}, indent=2),
        encoding="utf-8",
    )
    os.chmod(metadata_path, 0o644)

    registry = make_registry(monkeypatch, module, registry_type, data_dir, store)
    loaded = registry.get_service("legacy")
    assert loaded is not None
    if isinstance(loaded, ModelServiceConfig):
        assert loaded.api_key == "legacy-plaintext-secret"
    else:
        assert loaded.auth_value == "legacy-plaintext-secret"

    metadata = assert_compact_private_json(metadata_path)
    assert "legacy-plaintext-secret" not in metadata_path.read_text(encoding="utf-8")
    if registry_type is ModelServiceRegistry:
        assert "api_key" not in metadata["legacy"]
    else:
        for field in ("args", "env", "headers", "auth_value"):
            assert field not in metadata["legacy"]
    assert secret_key_template.format(name="legacy") in store.values


@pytest.mark.parametrize(
    "module,registry_type,config_factory,filename,secret_key_template",
    REGISTRY_CASES,
)
def test_disk_failures_roll_back_create_rename_delete_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    registry_type: type,
    config_factory: Callable[..., Any],
    filename: str,
    secret_key_template: str,
) -> None:
    store = MemorySecretStore()
    registry = make_registry(
        monkeypatch,
        module,
        registry_type,
        tmp_path,
        store,
    )
    assert registry.create_service(config_factory("old", "old-secret"))
    metadata_path = tmp_path / filename
    old_metadata = metadata_path.read_bytes()
    old_secrets = copy.deepcopy(store.values)

    def fail_metadata(_candidate: Any) -> None:
        raise OSError("injected metadata failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(registry, "_write_metadata", fail_metadata)
        with pytest.raises(OSError):
            registry.create_service(config_factory("new", "new-secret"))
    assert [item.name for item in registry.list_services()] == ["old"]
    assert metadata_path.read_bytes() == old_metadata
    assert store.values == old_secrets

    with monkeypatch.context() as scoped:
        scoped.setattr(registry, "_write_metadata", fail_metadata)
        with pytest.raises(OSError):
            registry.update_service("old", config_factory("renamed", "renamed-secret"))
    assert registry.service_exists("old")
    assert not registry.service_exists("renamed")
    assert metadata_path.read_bytes() == old_metadata
    assert store.values == old_secrets

    with monkeypatch.context() as scoped:
        scoped.setattr(registry, "_write_metadata", fail_metadata)
        with pytest.raises(OSError):
            registry.delete_service("old")
    assert registry.service_exists("old")
    assert metadata_path.read_bytes() == old_metadata
    assert store.values == old_secrets

    store.fail_next_after = True
    with pytest.raises(OSError):
        registry.delete_service("old")
    assert registry.service_exists("old")
    assert metadata_path.read_bytes() == old_metadata
    assert store.values == old_secrets


@pytest.mark.parametrize(
    "module,registry_type,config_factory,filename,secret_key_template",
    REGISTRY_CASES,
)
def test_rename_and_delete_clear_old_secret_only_after_metadata_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    registry_type: type,
    config_factory: Callable[..., Any],
    filename: str,
    secret_key_template: str,
) -> None:
    store = MemorySecretStore()
    registry = make_registry(
        monkeypatch,
        module,
        registry_type,
        tmp_path,
        store,
    )
    assert registry.create_service(config_factory("old", "old-secret"))
    metadata_path = tmp_path / filename
    old_key = secret_key_template.format(name="old")
    new_key = secret_key_template.format(name="new")
    observations: list[str] = []

    def observe(updates: dict[str, Any], secret_store: MemorySecretStore) -> None:
        if updates.get(old_key, object()) is None:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            assert "new" in metadata and "old" not in metadata
            assert old_key in secret_store.values
            assert new_key in secret_store.values
            observations.append("rename")
        if updates.get(new_key, object()) is None:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            assert "new" not in metadata
            assert new_key in secret_store.values
            observations.append("delete")

    store.before_set = observe
    assert registry.update_service("old", config_factory("new", "new-secret"))
    assert observations == ["rename"]
    assert old_key not in store.values
    assert new_key in store.values

    assert registry.delete_service("new")
    assert observations == ["rename", "delete"]
    assert new_key not in store.values


@pytest.mark.parametrize(
    "module,registry_type,config_factory,filename,secret_key_template",
    REGISTRY_CASES,
)
def test_multithreaded_mutations_have_no_lost_writes_and_json_stays_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    registry_type: type,
    config_factory: Callable[..., Any],
    filename: str,
    secret_key_template: str,
) -> None:
    store = MemorySecretStore()
    registry = make_registry(
        monkeypatch,
        module,
        registry_type,
        tmp_path,
        store,
    )
    metadata_path = tmp_path / filename
    stop_reader = threading.Event()
    parse_errors: list[Exception] = []

    def read_json_repeatedly() -> None:
        while not stop_reader.is_set():
            try:
                json.loads(metadata_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except Exception as exc:  # pragma: no cover - only populated on failure
                parse_errors.append(exc)
                stop_reader.set()

    reader = threading.Thread(target=read_json_repeatedly, daemon=True)
    reader.start()
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(
                pool.map(
                    lambda index: registry.create_service(
                        config_factory(f"service-{index:03d}", f"secret-{index:03d}")
                    ),
                    range(120),
                )
            )
        accepted = [
            f"service-{index:03d}"
            for index, outcome in enumerate(outcomes)
            if outcome
        ]
        assert len(accepted) == 100
        assert len(registry.list_services()) == 100

        to_update = accepted[:25]
        to_delete = accepted[25:50]
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [
                pool.submit(
                    registry.update_service,
                    name,
                    config_factory(name, f"updated-{name}", description="updated"),
                )
                for name in to_update
            ]
            futures.extend(
                pool.submit(registry.delete_service, name)
                for name in to_delete
            )
            assert all(future.result() for future in futures)
    finally:
        stop_reader.set()
        reader.join(timeout=5)

    assert parse_errors == []
    expected_names = set(accepted) - set(to_delete)
    actual = {service.name: service for service in registry.list_services()}
    assert set(actual) == expected_names
    assert all(actual[name].description == "updated" for name in to_update)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert set(metadata) == expected_names
    assert len(metadata) == 75

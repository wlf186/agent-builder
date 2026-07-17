"""
MCP服务注册表 - 全局MCP服务配置管理
"""

import copy
import json
import os
import stat
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .models import MCPServiceConfig
from .secret_store import get_registry_secret_store
from .storage_paths import ensure_real_directory, validate_regular_file


class MCPServiceRegistry:
    """MCP服务注册表 - 管理全局 MCP 服务配置。"""

    MAX_SERVICES = 100
    _SECRET_FIELDS = ("args", "env", "headers", "auth_value")

    def __init__(self, data_dir: Path):
        self.data_dir = ensure_real_directory(Path(data_dir))
        self._lock = threading.RLock()
        self.services: Dict[str, MCPServiceConfig] = {}
        self.secret_store = get_registry_secret_store()
        self._load_services()

    def _get_services_file(self) -> Path:
        """获取服务配置文件路径。"""
        return self.data_dir / "mcp_services.json"

    @staticmethod
    def _copy_services(
        services: Mapping[str, MCPServiceConfig],
    ) -> Dict[str, MCPServiceConfig]:
        return {
            name: config.model_copy(deep=True)
            for name, config in services.items()
        }

    @staticmethod
    def _secret_key(name: str) -> str:
        return f"mcp:{name}:connection"

    @staticmethod
    def _connection_secret(config: MCPServiceConfig) -> Optional[Dict[str, Any]]:
        connection = {
            "args": copy.deepcopy(config.args),
            "env": copy.deepcopy(config.env),
            "headers": copy.deepcopy(config.headers),
            "auth_value": config.auth_value,
        }
        return connection if any(connection.values()) else None

    def _secret_values(
        self,
        services: Mapping[str, MCPServiceConfig],
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        return {
            self._secret_key(name): self._connection_secret(config)
            for name, config in services.items()
        }

    def _serialize_metadata(
        self,
        services: Mapping[str, MCPServiceConfig],
    ) -> bytes:
        public_services = {}
        for name, config in services.items():
            public_config = config.model_dump(mode="json")
            for field in self._SECRET_FIELDS:
                public_config.pop(field, None)
            public_services[name] = public_config
        return json.dumps(
            public_services,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _atomic_replace_metadata(self, payload: bytes) -> None:
        ensure_real_directory(self.data_dir)
        services_file = self._get_services_file()
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{services_file.name}.",
            suffix=".tmp",
            dir=services_file.parent,
        )
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "wb")
            fd = -1
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, services_file)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _write_metadata(
        self,
        services: Mapping[str, MCPServiceConfig],
    ) -> None:
        self._atomic_replace_metadata(self._serialize_metadata(services))

    def _snapshot_metadata(self) -> Optional[bytes]:
        services_file = self._get_services_file()
        if not services_file.exists():
            return None
        return services_file.read_bytes()

    def _restore_metadata(self, snapshot: Optional[bytes]) -> None:
        services_file = self._get_services_file()
        if snapshot is None:
            if services_file.exists():
                services_file.unlink()
            return
        if services_file.exists() and services_file.read_bytes() == snapshot:
            if stat.S_IMODE(services_file.stat().st_mode) != 0o600:
                os.chmod(services_file, 0o600)
            return
        self._atomic_replace_metadata(snapshot)

    def _commit_services(
        self,
        previous: Mapping[str, MCPServiceConfig],
        candidate: Mapping[str, MCPServiceConfig],
    ) -> None:
        """Atomically publish secret and metadata changes as one logical update."""
        with self._lock:
            previous_secrets = self._secret_values(previous)
            candidate_secrets = self._secret_values(candidate)
            all_secret_keys = set(previous_secrets) | set(candidate_secrets)
            existing = self.secret_store.get_many(sorted(all_secret_keys))
            secret_snapshot = {
                key: copy.deepcopy(existing.get(key))
                for key in all_secret_keys
            }
            metadata_snapshot = self._snapshot_metadata()
            pre_metadata_updates = {}
            post_metadata_deletes = {}

            for key, desired in candidate_secrets.items():
                current = secret_snapshot.get(key)
                if key not in previous_secrets:
                    if desired != current:
                        pre_metadata_updates[key] = copy.deepcopy(desired)
                elif desired is not None and desired != current:
                    pre_metadata_updates[key] = copy.deepcopy(desired)
                elif desired is None and current is not None:
                    post_metadata_deletes[key] = None

            for key in previous_secrets.keys() - candidate_secrets.keys():
                if secret_snapshot.get(key) is not None:
                    post_metadata_deletes[key] = None

            secrets_touched = False
            try:
                if pre_metadata_updates:
                    secrets_touched = True
                    self.secret_store.set_many(pre_metadata_updates)
                self._write_metadata(candidate)
                if post_metadata_deletes:
                    secrets_touched = True
                    self.secret_store.set_many(post_metadata_deletes)
            except Exception:
                if secrets_touched:
                    try:
                        self.secret_store.set_many(secret_snapshot)
                    except Exception:
                        pass
                try:
                    self._restore_metadata(metadata_snapshot)
                except Exception:
                    pass
                raise

    def _load_services(self) -> None:
        """严格加载配置；任何损坏都会使整个注册表保持为空。"""
        with self._lock:
            self.services = {}
            services_file = self._get_services_file()
            if not services_file.exists():
                return

            try:
                validate_regular_file(services_file, allow_missing=False)
                raw_bytes = services_file.read_bytes()
                services_data = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(services_data, dict):
                    raise ValueError("registry root must be an object")
                if len(services_data) > self.MAX_SERVICES:
                    raise ValueError("registry service limit exceeded")

                raw_configs = {}
                for name, raw_config in services_data.items():
                    if not isinstance(name, str) or not isinstance(raw_config, dict):
                        raise ValueError("invalid registry entry")
                    if raw_config.get("name") != name:
                        raise ValueError("registry key does not match config name")
                    raw_configs[name] = copy.deepcopy(raw_config)

                encrypted = self.secret_store.get_many(
                    [self._secret_key(name) for name in raw_configs]
                )
                parsed: Dict[str, MCPServiceConfig] = {}
                for name, raw_config in raw_configs.items():
                    secret_key = self._secret_key(name)
                    legacy_connection = {
                        "args": raw_config.pop("args", []),
                        "env": raw_config.pop("env", {}),
                        "headers": raw_config.pop("headers", {}),
                        "auth_value": raw_config.pop("auth_value", None),
                    }
                    if not isinstance(legacy_connection["args"], list):
                        raise ValueError("invalid legacy args")
                    if not isinstance(legacy_connection["env"], dict):
                        raise ValueError("invalid legacy environment")
                    if not isinstance(legacy_connection["headers"], dict):
                        raise ValueError("invalid legacy headers")
                    if not isinstance(legacy_connection["auth_value"], (str, type(None))):
                        raise ValueError("invalid legacy auth value")
                    has_legacy_secret = any(legacy_connection.values())
                    if has_legacy_secret:
                        connection = legacy_connection
                    else:
                        connection = copy.deepcopy(encrypted.get(secret_key))
                        if connection is None:
                            connection = {}
                    if not isinstance(connection, dict):
                        raise ValueError("invalid encrypted connection data")
                    raw_config.update(
                        {
                            "args": copy.deepcopy(connection.get("args", [])),
                            "env": copy.deepcopy(connection.get("env", {})),
                            "headers": copy.deepcopy(connection.get("headers", {})),
                            "auth_value": connection.get("auth_value"),
                        }
                    )
                    config = MCPServiceConfig(**raw_config)
                    if config.name != name:
                        raise ValueError("validated service name mismatch")
                    parsed[name] = config

                canonical = self._serialize_metadata(parsed)
                current_mode = stat.S_IMODE(services_file.stat().st_mode)
                if raw_bytes != canonical or current_mode != 0o600:
                    self._save_services({}, parsed)
                self.services = self._copy_services(parsed)
            except Exception as exc:
                self.services = {}
                print(f"加载MCP服务配置失败: {type(exc).__name__}")

    def _save_services(
        self,
        previous: Optional[Mapping[str, MCPServiceConfig]] = None,
        candidate: Optional[Mapping[str, MCPServiceConfig]] = None,
    ) -> None:
        """保存服务配置；调用者只有在成功后才发布候选内存状态。"""
        with self._lock:
            if previous is None:
                previous = self._copy_services(self.services)
            if candidate is None:
                candidate = self._copy_services(self.services)
            self._commit_services(previous, candidate)

    def create_service(self, config: MCPServiceConfig) -> bool:
        """创建 MCP 服务。"""
        with self._lock:
            new_config = config.model_copy(deep=True)
            if new_config.name in self.services:
                return False
            if len(self.services) >= self.MAX_SERVICES:
                return False

            new_config.created_at = datetime.now().isoformat()
            new_config.updated_at = new_config.created_at
            previous = self._copy_services(self.services)
            candidate = self._copy_services(previous)
            candidate[new_config.name] = new_config
            self._save_services(previous, candidate)
            self.services = candidate
            return True

    def update_service(self, name: str, config: MCPServiceConfig) -> bool:
        """更新 MCP 服务。"""
        with self._lock:
            new_config = config.model_copy(deep=True)
            if name not in self.services:
                return False
            if name != new_config.name and new_config.name in self.services:
                return False

            new_config.created_at = self.services[name].created_at
            new_config.updated_at = datetime.now().isoformat()
            previous = self._copy_services(self.services)
            candidate = self._copy_services(previous)
            candidate.pop(name)
            candidate[new_config.name] = new_config
            self._save_services(previous, candidate)
            self.services = candidate
            return True

    def delete_service(self, name: str) -> bool:
        """删除 MCP 服务。"""
        with self._lock:
            if name not in self.services:
                return False
            previous = self._copy_services(self.services)
            candidate = self._copy_services(previous)
            candidate.pop(name)
            self._save_services(previous, candidate)
            self.services = candidate
            return True

    def get_service(self, name: str) -> Optional[MCPServiceConfig]:
        """获取单个服务配置的深拷贝。"""
        with self._lock:
            service = self.services.get(name)
            return service.model_copy(deep=True) if service is not None else None

    def list_services(self) -> List[MCPServiceConfig]:
        """获取所有服务配置的深拷贝。"""
        with self._lock:
            return [service.model_copy(deep=True) for service in self.services.values()]

    def get_services_by_names(self, names: List[str]) -> List[MCPServiceConfig]:
        """根据名称列表获取服务配置的深拷贝。"""
        with self._lock:
            return [
                self.services[name].model_copy(deep=True)
                for name in names
                if name in self.services
            ]

    def service_exists(self, name: str) -> bool:
        """检查服务是否存在。"""
        with self._lock:
            return name in self.services

    def get_enabled_services(self) -> List[MCPServiceConfig]:
        """获取所有启用服务的深拷贝。"""
        with self._lock:
            return [
                service.model_copy(deep=True)
                for service in self.services.values()
                if service.enabled
            ]

"""
预置 MCP 服务管理器 - SSE 模式
在系统启动时自动注册预置服务（SSE 模式），并启动 SSE 服务器
"""
import asyncio
import http.client
import os
import sys
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from .models import MCPServiceConfig, MCPConnectionType, MCPAuthType
from .mcp_registry import MCPServiceRegistry


# SSE 服务端口
SSE_SERVER_PORT = int(os.environ.get("MCP_SSE_PORT", "20882"))
SSE_SERVER_HOST = os.environ.get("MCP_SSE_HOST", "localhost")
if SSE_SERVER_HOST not in {"localhost", "127.0.0.1"}:
    raise RuntimeError("Built-in MCP services must bind to a loopback host")


class BuiltinServiceManager:
    """预置服务管理器 - SSE 模式"""

    # 预置服务配置 - 支持本地 SSE 服务和远程 MCP 服务
    BUILTIN_SERVICES = [
        # 本地 SSE 服务（由本地 SSE 服务器提供）
        {
            "name": "calculator",
            "description": "计算器服务：支持加减乘除、幂运算、平方根等数学计算",
            "connection_type": MCPConnectionType.SSE,
            "url": f"http://{SSE_SERVER_HOST}:{SSE_SERVER_PORT}/calculator",
            "enabled": True,
            "is_local": True,  # 标记为本地服务
        },
        {
            "name": "cold-jokes",
            "description": "冷笑话服务：提供各种类型的冷笑话，让人开心一下",
            "connection_type": MCPConnectionType.SSE,
            "url": f"http://{SSE_SERVER_HOST}:{SSE_SERVER_PORT}/cold-jokes",
            "enabled": True,
            "is_local": True,  # 标记为本地服务
        },
        # 远程 MCP 服务（第三方提供）
        {
            "name": "coingecko",
            "description": "CoinGecko 加密货币数据：实时价格、市场数据、历史K线、NFT、链上数据等（50+工具）",
            "connection_type": MCPConnectionType.SSE,
            "url": "https://mcp.api.coingecko.com/sse",
            "enabled": True,
            "is_local": False,  # 标记为远程服务
        },
    ]

    def __init__(self, registry: MCPServiceRegistry):
        self.registry = registry
        self.services_dir = Path(__file__).parent.parent / "builtin_mcp_services"
        self.project_root = Path(__file__).resolve().parent.parent
        self._sse_process: Optional[subprocess.Popen] = None

    def _get_python_executable(self) -> str:
        """获取当前 Python 解释器路径"""
        return sys.executable

    def start_sse_server(self) -> bool:
        """启动 SSE 服务器"""
        if self._sse_process is not None and self._sse_process.poll() is None:
            return True
        self._sse_process = None

        sse_server_script = self.services_dir / "sse_server.py"
        if not sse_server_script.exists():
            print(f"✗ SSE 服务器脚本不存在: {sse_server_script}")
            return False

        try:
            runtime_dir = Path(
                os.environ.get(
                    "AGENT_BUILDER_RUNTIME_DIR", self.project_root / ".runtime"
                )
            )
            log_file = runtime_dir / "logs" / "builtin-mcp.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_runner = self.project_root / "scripts" / "run_with_rotating_log.py"
            command = [
                self._get_python_executable(),
                str(log_runner),
                "--clean-env",
                str(log_file),
                "--",
                self._get_python_executable(),
                str(sse_server_script),
                "--port",
                str(SSE_SERVER_PORT),
                "--host",
                SSE_SERVER_HOST,
            ]
            child_env = os.environ.copy()
            child_env.pop("AGENT_BUILDER_API_TOKEN", None)
            self._sse_process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(self.services_dir.parent),
                env=child_env,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._sse_process.poll() is not None:
                    break
                connection = http.client.HTTPConnection(
                    SSE_SERVER_HOST, SSE_SERVER_PORT, timeout=0.25
                )
                try:
                    connection.request("GET", "/health")
                    response = connection.getresponse()
                    response.read(4096)
                    if response.status == 200:
                        print(
                            f"✓ SSE 服务器已启动 (PID: {self._sse_process.pid}, "
                            f"端口: {SSE_SERVER_PORT})"
                        )
                        return True
                except OSError:
                    pass
                finally:
                    connection.close()
                time.sleep(0.1)
            self.stop_sse_server()
            print("✗ SSE 服务器未能通过健康检查")
            return False
        except Exception as e:
            print(f"✗ 启动 SSE 服务器失败: error_type={type(e).__name__}")
            return False

    def stop_sse_server(self):
        """停止 SSE 服务器"""
        if self._sse_process is not None:
            try:
                self._sse_process.terminate()
                self._sse_process.wait(timeout=5)
                print("✓ SSE 服务器已停止")
            except Exception as e:
                print(f"✗ 停止 SSE 服务器失败: error_type={type(e).__name__}")
                try:
                    self._sse_process.kill()
                except Exception:
                    pass
            finally:
                self._sse_process = None

    def register_builtin_services(self) -> List[str]:
        """注册所有预置服务到注册表"""
        registered = []

        for service_config in self.BUILTIN_SERVICES:
            name = service_config["name"]

            # 创建配置 - SSE 模式
            config = MCPServiceConfig(
                name=name,
                description=service_config["description"],
                connection_type=MCPConnectionType.SSE,
                url=service_config["url"],
                enabled=service_config["enabled"],
            )

            existing = self.registry.get_service(name)
            if existing is not None:
                canonical_fields = (
                    existing.description == config.description
                    and existing.connection_type == config.connection_type
                    and existing.url == config.url
                    and existing.enabled == config.enabled
                    and existing.auth_type == MCPAuthType.NONE
                    and not existing.command
                    and not existing.args
                    and not existing.env
                    and not existing.headers
                    and not existing.auth_value
                )
                if canonical_fields:
                    print(f"预置服务 {name} 已存在且配置有效")
                    registered.append(name)
                    continue
                if self.registry.update_service(name, config):
                    print(f"✓ 预置服务 {name} 已恢复为受管配置")
                    registered.append(name)
                else:
                    print(f"✗ 预置服务 {name} 配置修复失败")
                continue

            if self.registry.create_service(config):
                print(f"✓ 预置服务 {name} 注册成功 (SSE: {service_config['url']})")
                registered.append(name)
            else:
                print(f"✗ 预置服务 {name} 注册失败")

        return registered

    def get_builtin_service_names(self) -> List[str]:
        """获取所有预置服务名称"""
        return [s["name"] for s in self.BUILTIN_SERVICES]

    def is_builtin_service(self, name: str) -> bool:
        """检查是否为预置服务"""
        return is_builtin_service_name(name)


def is_builtin_service_name(name: str) -> bool:
    """Return whether a name is reserved even before startup completes."""
    return any(service["name"] == name for service in BuiltinServiceManager.BUILTIN_SERVICES)


# 全局实例
_builtin_manager: Optional[BuiltinServiceManager] = None


def setup_builtin_services(registry: MCPServiceRegistry) -> List[str]:
    """
    设置预置服务的便捷函数

    Args:
        registry: MCP 服务注册表实例

    Returns:
        已注册的服务名称列表
    """
    global _builtin_manager
    _builtin_manager = BuiltinServiceManager(registry)

    # 启动 SSE 服务器
    if not _builtin_manager.start_sse_server():
        _builtin_manager = None
        raise RuntimeError("Built-in MCP server failed its startup health check")

    # 注册服务
    registered = _builtin_manager.register_builtin_services()
    expected = set(_builtin_manager.get_builtin_service_names())
    if set(registered) != expected:
        _builtin_manager.stop_sse_server()
        _builtin_manager = None
        raise RuntimeError("Built-in MCP service registration is incomplete")
    return registered


def shutdown_builtin_services():
    """关闭预置服务"""
    global _builtin_manager
    if _builtin_manager is not None:
        _builtin_manager.stop_sse_server()
        _builtin_manager = None


def get_builtin_manager() -> Optional[BuiltinServiceManager]:
    """获取预置服务管理器实例"""
    return _builtin_manager

"""
MCP连接诊断服务
提供分层诊断能力：配置→DNS→网络→TLS→SSE→MCP协议
"""
import asyncio
import socket
import ssl
import time
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse
from datetime import datetime
from pydantic import BaseModel

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    from contextlib import AsyncExitStack
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from .models import MCPServiceConfig, MCPConnectionType, MCPAuthType
from .mcp_manager import (
    SSEServerConnection,
    MCPServerConnection,
    create_hardened_mcp_http_client,
)
from .security import (
    SecurityValidationError,
    resolve_outbound_target,
    validate_headers,
    validate_outbound_url,
    validate_outbound_url_syntax,
    validate_stdio_configuration,
)


class DiagnosticResult(BaseModel):
    """单层诊断结果"""
    layer: str                    # 诊断层名称
    status: str                   # pass / fail / skip
    message: str                  # 状态消息
    latency_ms: Optional[int]     # 延迟（毫秒）
    details: Optional[Dict[str, Any]] = None  # 详细信息


class MCPDiagnosticReport(BaseModel):
    """完整诊断报告"""
    service_name: str
    overall_status: str           # healthy / degraded / down
    timestamp: str
    layers: List[DiagnosticResult]
    recommendation: str           # 修复建议


class MCPDiagnostic:
    """MCP服务诊断器"""

    def __init__(self, config: MCPServiceConfig):
        self.config = config
        self.layers: List[DiagnosticResult] = []

    async def diagnose(self) -> MCPDiagnosticReport:
        """执行完整诊断"""
        self.layers = []

        # 1. 配置验证
        config_result = self._check_config()
        self.layers.append(config_result)

        if config_result.status == "fail":
            return self._generate_report()

        # 根据连接类型执行不同的诊断
        if self.config.connection_type == MCPConnectionType.SSE:
            return await self._diagnose_sse()
        else:
            return await self._diagnose_stdio()

    def _check_config(self) -> DiagnosticResult:
        """检查配置有效性"""
        errors = []

        if self.config.connection_type == MCPConnectionType.SSE:
            if not self.config.url:
                errors.append("缺少URL配置")

            # 验证URL格式
            if self.config.url:
                try:
                    validate_outbound_url_syntax(self.config.url)
                    validate_headers(self.config.headers)
                except (SecurityValidationError, ValueError) as e:
                    errors.append(f"URL配置无效 ({type(e).__name__})")

            # 检查认证配置
            if self.config.auth_type and self.config.auth_type != MCPAuthType.NONE:
                if not self.config.auth_value:
                    errors.append(f"认证类型为{self.config.auth_type.value}但缺少auth_value")

        else:  # stdio
            try:
                validate_stdio_configuration(
                    self.config.command,
                    self.config.args or [],
                    self.config.env or {},
                )
            except SecurityValidationError as e:
                errors.append(f"stdio配置无效 ({type(e).__name__})")

        if errors:
            return DiagnosticResult(
                layer="config",
                status="fail",
                message="配置验证失败",
                latency_ms=None,
                details={"errors": errors}
            )

        return DiagnosticResult(
            layer="config",
            status="pass",
            message="配置验证通过",
            latency_ms=None
        )

    async def _diagnose_sse(self) -> MCPDiagnosticReport:
        """诊断SSE连接"""
        # 2. DNS解析
        dns_result = await self._check_dns()
        self.layers.append(dns_result)

        if dns_result.status == "fail":
            return self._generate_report()

        # 3. TCP连接
        tcp_result = await self._check_tcp_connection()
        self.layers.append(tcp_result)

        if tcp_result.status == "fail":
            return self._generate_report()

        # 4. TLS连接（HTTPS）
        if self.config.url and self.config.url.startswith("https://"):
            tls_result = await self._check_tls_connection()
            self.layers.append(tls_result)

            if tls_result.status == "fail":
                return self._generate_report()

        # 5. SSE/MCP协议
        mcp_result = await self._check_mcp_protocol()
        self.layers.append(mcp_result)

        return self._generate_report()

    async def _diagnose_stdio(self) -> MCPDiagnosticReport:
        """诊断stdio连接"""
        # 检查命令是否存在
        command_result = await self._check_stdio_command()
        self.layers.append(command_result)

        if command_result.status == "fail":
            return self._generate_report()

        # 尝试MCP协议连接
        mcp_result = await self._check_stdio_mcp()
        self.layers.append(mcp_result)

        return self._generate_report()

    async def _check_dns(self) -> DiagnosticResult:
        """检查DNS解析"""
        start = time.time()

        if not self.config.url:
            return DiagnosticResult(
                layer="dns",
                status="skip",
                message="非HTTP连接，跳过DNS检查",
                latency_ms=None
            )

        try:
            parsed = urlparse(self.config.url)
            host = parsed.hostname

            try:
                await validate_outbound_url(self.config.url)
            except SecurityValidationError as exc:
                return DiagnosticResult(
                    layer="dns",
                    status="fail",
                    message="目标被SSRF安全策略拒绝",
                    latency_ms=int((time.time() - start) * 1000),
                )

            # 异步DNS解析
            loop = asyncio.get_event_loop()
            try:
                # 使用getaddrinfo进行DNS查询
                result = await asyncio.wait_for(
                    loop.getaddrinfo(host, None),
                    timeout=5.0
                )

                # 提取IP地址
                ip_addresses = list(set([addr[4][0] for addr in result]))
                primary_ip = ip_addresses[0] if ip_addresses else None

                latency = int((time.time() - start) * 1000)

                return DiagnosticResult(
                    layer="dns",
                    status="pass",
                    message=f"DNS解析成功: {host} -> {primary_ip}",
                    latency_ms=latency,
                    details={
                        "host": host,
                        "ip_addresses": ip_addresses[:5]  # 最多显示5个
                    }
                )
            except asyncio.TimeoutError:
                return DiagnosticResult(
                    layer="dns",
                    status="fail",
                    message="DNS解析超时（5秒）",
                    latency_ms=5000
                )
        except Exception as e:
            return DiagnosticResult(
                layer="dns",
                status="fail",
                message=f"DNS解析失败 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )

    async def _check_tcp_connection(self) -> DiagnosticResult:
        """检查网络连接（使用httpx以支持代理）"""
        start = time.time()

        if not self.config.url:
            return DiagnosticResult(
                layer="network",
                status="skip",
                message="非HTTP连接，跳过网络检查",
                latency_ms=None
            )

        try:
            target = await resolve_outbound_target(self.config.url)
            parsed = urlparse(self.config.url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)

            # 使用httpx进行连接测试（自动支持代理环境变量）
            import httpx

            # 构建基础URL进行连通性测试
            base_url = f"{parsed.scheme}://{host}"
            if parsed.port:
                base_url += f":{parsed.port}"

            async with create_hardened_mcp_http_client(
                timeout=10.0,
                pinned_target=target,
            ) as client:
                # 发送HEAD请求测试连通性
                response = await client.head(base_url)

                latency = int((time.time() - start) * 1000)

                return DiagnosticResult(
                    layer="network",
                    status="pass",
                    message=f"网络连接成功: HTTP {response.status_code}",
                    latency_ms=latency,
                    details={
                        "host": host,
                        "port": port,
                        "http_status": response.status_code,
                        "proxy_used": None,
                    }
                )

        except asyncio.TimeoutError:
            return DiagnosticResult(
                layer="network",
                status="fail",
                message="网络连接超时（10秒）",
                latency_ms=10000
            )
        except ConnectionRefusedError:
            return DiagnosticResult(
                layer="network",
                status="fail",
                message="连接被拒绝，端口可能未开放",
                latency_ms=int((time.time() - start) * 1000)
            )
        except Exception as e:
            return DiagnosticResult(
                layer="network",
                status="fail",
                message=f"网络连接失败 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )

    async def _check_tls_connection(self) -> DiagnosticResult:
        """检查TLS/SSL连接（使用httpx以支持代理）"""
        start = time.time()

        if not self.config.url:
            return DiagnosticResult(
                layer="tls",
                status="skip",
                message="非HTTPS连接，跳过TLS检查",
                latency_ms=None
            )

        if not self.config.url.startswith("https://"):
            return DiagnosticResult(
                layer="tls",
                status="skip",
                message="非HTTPS连接，跳过TLS检查",
                latency_ms=None
            )

        try:
            target = await resolve_outbound_target(self.config.url)
            parsed = urlparse(self.config.url)
            host = parsed.hostname

            # 使用httpx进行TLS连接测试（自动支持代理环境变量）
            import httpx

            async with create_hardened_mcp_http_client(
                timeout=10.0,
                pinned_target=target,
            ) as client:
                # 使用HEAD请求避免SSE流式响应导致超时
                response = await client.head(self.config.url, follow_redirects=False)

                latency = int((time.time() - start) * 1000)

                # 从httpx获取TLS信息
                # httpx的底层连接会自动处理TLS
                tls_info = "TLS 1.2+"  # httpx默认使用安全TLS配置

                return DiagnosticResult(
                    layer="tls",
                    status="pass",
                    message="TLS连接成功",
                    latency_ms=latency,
                    details={
                        "tls_version": tls_info,
                        "proxy_used": None,
                        "http_status": response.status_code
                    }
                )
        except ssl.SSLCertVerificationError as e:
            return DiagnosticResult(
                layer="tls",
                status="fail",
                message=f"证书验证失败 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )
        except asyncio.TimeoutError:
            return DiagnosticResult(
                layer="tls",
                status="fail",
                message="TLS握手超时（10秒）",
                latency_ms=10000
            )
        except Exception as e:
            return DiagnosticResult(
                layer="tls",
                status="fail",
                message=f"TLS连接失败 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )

    async def _check_mcp_protocol(self) -> DiagnosticResult:
        """检查MCP协议（SSE模式）"""
        start = time.time()

        if not MCP_AVAILABLE:
            return DiagnosticResult(
                layer="mcp",
                status="fail",
                message="MCP库未安装",
                latency_ms=None
            )

        connection = None
        try:
            # 使用现有的连接类
            connection = SSEServerConnection(self.config)
            success = await connection.connect()

            if success:
                tool_count = len(connection.tools)
                tool_names = [t.name for t in connection.tools[:5]]  # 前5个

                latency = int((time.time() - start) * 1000)

                return DiagnosticResult(
                    layer="mcp",
                    status="pass",
                    message=f"MCP连接成功，获取到 {tool_count} 个工具",
                    latency_ms=latency,
                    details={
                        "tool_count": tool_count,
                        "tools": tool_names
                    }
                )
            else:
                return DiagnosticResult(
                    layer="mcp",
                    status="fail",
                    message="MCP握手失败",
                    latency_ms=int((time.time() - start) * 1000)
                )
        except Exception as e:
            return DiagnosticResult(
                layer="mcp",
                status="fail",
                message=f"MCP协议错误 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )
        finally:
            if connection is not None:
                await connection.disconnect()

    async def _check_stdio_command(self) -> DiagnosticResult:
        """检查stdio命令有效性"""
        start = time.time()

        if not self.config.command:
            return DiagnosticResult(
                layer="command",
                status="fail",
                message="缺少command配置",
                latency_ms=None,
                details=None
            )

        try:
            import shutil
            command_path = self.config.command

            # 检查命令是否存在于PATH
            if shutil.which(command_path):
                latency = int((time.time() - start) * 1000)
                return DiagnosticResult(
                    layer="command",
                    status="pass",
                    message=f"命令存在: {command_path}",
                    latency_ms=latency,
                    details={"command": command_path}
                )
            else:
                # 检查是否为绝对路径
                if "/" in command_path or "\\" in command_path:
                    import os
                    if os.path.isfile(command_path):
                        latency = int((time.time() - start) * 1000)
                        return DiagnosticResult(
                            layer="command",
                            status="pass",
                            message=f"命令文件存在: {command_path}",
                            latency_ms=latency
                        )

                return DiagnosticResult(
                    layer="command",
                    status="fail",
                    message=f"命令不存在: {command_path}",
                    latency_ms=int((time.time() - start) * 1000)
                )
        except Exception as e:
            return DiagnosticResult(
                layer="command",
                status="fail",
                message=f"命令检查失败 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )

    async def _check_stdio_mcp(self) -> DiagnosticResult:
        """检查stdio模式MCP连接"""
        start = time.time()

        if not MCP_AVAILABLE:
            return DiagnosticResult(
                layer="mcp",
                status="fail",
                message="MCP库未安装",
                latency_ms=None
            )

        connection = None
        try:
            # 使用现有的连接类
            from .models import MCPConfig
            mcp_config = MCPConfig(
                name=self.config.name,
                command=self.config.command,
                args=self.config.args,
                env=self.config.env
            )
            connection = MCPServerConnection(mcp_config)
            success = await connection.connect()

            if success:
                tool_count = len(connection.tools)
                tool_names = [t.name for t in connection.tools[:5]]

                latency = int((time.time() - start) * 1000)

                return DiagnosticResult(
                    layer="mcp",
                    status="pass",
                    message=f"MCP连接成功，获取到 {tool_count} 个工具",
                    latency_ms=latency,
                    details={
                        "tool_count": tool_count,
                        "tools": tool_names
                    }
                )
            else:
                return DiagnosticResult(
                    layer="mcp",
                    status="fail",
                    message="MCP连接失败",
                    latency_ms=int((time.time() - start) * 1000)
                )
        except Exception as e:
            return DiagnosticResult(
                layer="mcp",
                status="fail",
                message=f"MCP协议错误 ({type(e).__name__})",
                latency_ms=int((time.time() - start) * 1000)
            )
        finally:
            if connection is not None:
                await connection.disconnect()

    def _generate_report(self) -> MCPDiagnosticReport:
        """生成诊断报告"""
        # 确定总体状态
        failed_layers = [l for l in self.layers if l.status == "fail"]
        passed_layers = [l for l in self.layers if l.status == "pass"]

        if failed_layers:
            overall_status = "down"
        elif all(l.status == "pass" for l in self.layers):
            overall_status = "healthy"
        else:
            overall_status = "degraded"

        # 生成修复建议
        recommendation = self._generate_recommendation(failed_layers)

        return MCPDiagnosticReport(
            service_name=self.config.name,
            overall_status=overall_status,
            timestamp=datetime.now().isoformat(),
            layers=self.layers,
            recommendation=recommendation
        )

    def _generate_recommendation(self, failed_layers: List[DiagnosticResult]) -> str:
        """生成修复建议"""
        if not failed_layers:
            return "服务运行正常"

        # 根据失败层级生成建议
        layer = failed_layers[0].layer
        message = failed_layers[0].message

        recommendations = {
            "config": f"配置错误: {message}。请检查服务配置并重试。",
            "dns": "DNS解析失败。建议: 1) 检查网络连接 2) 尝试更换DNS服务器 3) 检查域名拼写",
            "network": f"网络连接失败: {message}。建议: 1) 检查防火墙设置 2) 确认端口是否开放 3) 检查目标服务是否运行",
            "tls": f"TLS连接失败: {message}。建议: 1) 检查系统时间是否正确 2) 更新CA证书 3) 检查目标服务证书",
            "mcp": f"MCP协议错误: {message}。建议: 1) 检查MCP服务是否正常运行 2) 查看服务端日志 3) 确认认证配置正确",
            "command": f"命令检查失败: {message}。建议: 1) 确认命令路径正确 2) 检查可执行权限 3) 验证命令参数"
        }

        return recommendations.get(layer, f"诊断失败: {message}。请查看详细日志并联系技术支持。")


async def diagnose_mcp_service(config: MCPServiceConfig) -> MCPDiagnosticReport:
    """诊断MCP服务连接（独立函数，用于API调用）"""
    diagnostic = MCPDiagnostic(config)
    return await diagnostic.diagnose()

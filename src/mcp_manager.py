"""
MCP工具管理器
"""
import asyncio
import hashlib
import json
import os
import re
import time
from functools import partial
from pathlib import Path
from typing import AsyncIterator, Dict, List, Any, Optional
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

try:
    import httpx
    import httpcore
    from httpcore._backends.auto import AutoBackend
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

from .models import MCPConfig, MCPServiceConfig, MCPConnectionType, MCPAuthType
from .security import (
    ResolvedOutboundTarget,
    SecurityValidationError,
    resolve_outbound_target,
    validate_headers,
    validate_stdio_configuration,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(
    os.environ.get("AGENT_BUILDER_RUNTIME_DIR", PROJECT_ROOT / ".runtime")
).resolve()
MAX_MCP_ARGUMENT_BYTES = 1024 * 1024
MAX_MCP_RESULT_CHARS = 1024 * 1024
MAX_MCP_HTTP_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_MCP_TOOLS = 128
MAX_MCP_TOOL_SCHEMA_BYTES = 64 * 1024
MAX_MCP_SSE_EVENT_BYTES = 1024 * 1024


class MCPResponseLimitError(ValueError):
    """Raised before MCP decoders can buffer an oversized wire payload."""


class _PinnedNetworkBackend:
    """Dial only pre-authorized IPs while httpcore retains Host and TLS SNI."""

    def __init__(self, target: ResolvedOutboundTarget):
        self._target = target
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ):
        normalized_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if normalized_host != self._target.hostname or port != self._target.port:
            raise httpcore.ConnectError("Pinned MCP transport rejected a different origin")

        started = time.monotonic()
        last_error: Optional[BaseException] = None
        for address in self._target.addresses:
            remaining = None
            if timeout is not None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    break
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectTimeout("Pinned MCP connection timed out")

    async def connect_unix_socket(self, *args: Any, **kwargs: Any):
        raise httpcore.ConnectError("Unix sockets are not allowed for HTTP MCP")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _LimitedMCPResponseStream(httpx.AsyncByteStream if HTTPX_AVAILABLE else object):
    def __init__(self, stream: Any, *, is_sse: bool, limit: int):
        self._stream = stream
        self._is_sse = is_sse
        self._limit = limit
        self._total = 0
        self._event_bytes = 0
        self._line_has_content = False
        self._after_cr = False

    def _consume_sse(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._after_cr:
                self._after_cr = False
                if byte == 0x0A:
                    continue
            self._event_bytes += 1
            if self._event_bytes > self._limit:
                raise MCPResponseLimitError("MCP SSE event exceeds the 1MB limit")
            if byte == 0x0D:
                if not self._line_has_content:
                    self._event_bytes = 0
                self._line_has_content = False
                self._after_cr = True
            elif byte == 0x0A:
                if not self._line_has_content:
                    self._event_bytes = 0
                self._line_has_content = False
            else:
                self._line_has_content = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                if self._is_sse:
                    self._consume_sse(chunk)
                else:
                    self._total += len(chunk)
                    if self._total > self._limit:
                        raise MCPResponseLimitError("MCP HTTP response exceeds the 5MB limit")
                yield chunk
        except BaseException:
            await self._stream.aclose()
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedLimitedMCPTransport(httpx.AsyncBaseTransport if HTTPX_AVAILABLE else object):
    def __init__(self, target: ResolvedOutboundTarget):
        self._target = target
        self._transport = httpx.AsyncHTTPTransport(
            trust_env=False,
            http1=True,
            http2=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            retries=0,
        )
        pool = getattr(self._transport, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            raise RuntimeError("Installed httpx/httpcore cannot provide DNS-pinned MCP transport")
        pool._network_backend = _PinnedNetworkBackend(target)

    def _validate_request_origin(self, request: Any) -> None:
        host = request.url.host.rstrip(".").encode("idna").decode("ascii").lower()
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if (
            request.url.scheme != self._target.scheme
            or host != self._target.hostname
            or port != self._target.port
        ):
            raise SecurityValidationError("MCP transport request origin changed")

    async def handle_async_request(self, request: Any):
        self._validate_request_origin(request)
        response = await self._transport.handle_async_request(request)
        content_type = response.headers.get("content-type", "").lower()
        is_sse = content_type.split(";", 1)[0].strip() == "text/event-stream"
        declared = response.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                await response.aclose()
                raise MCPResponseLimitError("MCP response has invalid Content-Length") from exc
            if declared_size < 0 or (
                not is_sse and declared_size > MAX_MCP_HTTP_RESPONSE_BYTES
            ):
                await response.aclose()
                raise MCPResponseLimitError("MCP HTTP response exceeds the 5MB limit")
        response.stream = _LimitedMCPResponseStream(
            response.stream,
            is_sse=is_sse,
            limit=(MAX_MCP_SSE_EVENT_BYTES if is_sse else MAX_MCP_HTTP_RESPONSE_BYTES),
        )
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def _ensure_mcp_runtime_directory(path: Path) -> None:
    """Create a runtime directory without following a pre-created symlink."""
    runtime_root = Path(RUNTIME_ROOT).absolute()
    candidate = Path(path).absolute()
    try:
        relative = candidate.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("MCP runtime path escapes the project runtime") from exc
    if runtime_root.is_symlink():
        raise ValueError("MCP runtime root cannot be a symlink")
    runtime_root.mkdir(parents=True, exist_ok=True)
    current = runtime_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("MCP runtime path cannot contain symlinks")
        current.mkdir(exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise ValueError("MCP runtime path is not a real directory")
    candidate.resolve(strict=True).relative_to(runtime_root.resolve(strict=True))
    os.chmod(candidate, 0o700)


async def _read_json_response_limited(response: Any) -> Any:
    """Read an httpx streaming response without buffering past the limit."""
    declared = response.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise ValueError("MCP response has an invalid Content-Length") from exc
        if declared_size < 0:
            raise ValueError("MCP response has an invalid Content-Length")
        if declared_size > MAX_MCP_HTTP_RESPONSE_BYTES:
            raise ValueError("MCP response exceeds the 5MB limit")
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > MAX_MCP_HTTP_RESPONSE_BYTES:
            raise ValueError("MCP response exceeds the 5MB limit")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP response is not valid UTF-8 JSON") from exc


def _validate_tool_definition(name: Any, description: Any, schema: Any) -> tuple[str, str, Dict]:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name):
        raise ValueError("MCP returned an invalid tool name")
    description_text = description if isinstance(description, str) else ""
    if len(description_text) > 4096 or not isinstance(schema, dict):
        raise ValueError("MCP returned an oversized tool definition")
    encoded_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded_schema) > MAX_MCP_TOOL_SCHEMA_BYTES:
        raise ValueError("MCP returned an oversized tool schema")
    return name, description_text, schema


def _validate_tool_arguments(arguments: Dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            arguments, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP tool arguments must be JSON serializable") from exc
    if len(encoded) > MAX_MCP_ARGUMENT_BYTES:
        raise ValueError("MCP tool arguments exceed the 1MB limit")


def _bounded_tool_content(content_items: Any) -> str:
    """Render MCP content without admitting an unbounded LLM prompt."""
    chunks: List[str] = []
    total = 0
    for item in content_items or []:
        value = item.text if hasattr(item, "text") else str(item)
        total += len(value) + (1 if chunks else 0)
        if total > MAX_MCP_RESULT_CHARS:
            raise ValueError("MCP tool result exceeds the 1MB limit")
        chunks.append(value)
    return "\n".join(chunks)


def create_hardened_mcp_http_client(
    headers: Optional[Dict[str, str]] = None,
    timeout: Any = None,
    auth: Any = None,
    *,
    pinned_target: Optional[ResolvedOutboundTarget] = None,
):
    """Create a bounded MCP client pinned to one validated DNS snapshot."""
    if not HTTPX_AVAILABLE:
        raise RuntimeError("httpx is required for remote MCP connections")
    if pinned_target is None:
        raise SecurityValidationError("MCP HTTP transport requires a resolved target")
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        transport=_PinnedLimitedMCPTransport(pinned_target),
    )


def build_stdio_environment(service_name: str, configured: Dict[str, str]) -> Dict[str, str]:
    """Build a project-contained environment without backend credentials."""
    digest = hashlib.sha256(service_name.encode("utf-8")).hexdigest()[:12]
    service_root = RUNTIME_ROOT / "mcp" / digest
    locations = {
        "HOME": service_root / "home",
        "TMPDIR": service_root / "tmp",
        "TEMP": service_root / "tmp",
        "TMP": service_root / "tmp",
        "XDG_CACHE_HOME": service_root / "cache",
        "XDG_CONFIG_HOME": service_root / "config",
        "XDG_DATA_HOME": service_root / "share",
        "XDG_STATE_HOME": service_root / "state",
        "XDG_RUNTIME_DIR": service_root / "xdg-runtime",
        "UV_CACHE_DIR": service_root / "cache" / "uv",
        "PIP_CACHE_DIR": service_root / "cache" / "pip",
        "HF_HOME": service_root / "cache" / "huggingface",
        "HUGGINGFACE_HUB_CACHE": service_root / "cache" / "huggingface" / "hub",
        "SENTENCE_TRANSFORMERS_HOME": service_root / "cache" / "huggingface" / "sentence-transformers",
        "TRANSFORMERS_CACHE": service_root / "cache" / "huggingface" / "transformers",
        "TORCH_HOME": service_root / "cache" / "torch",
        "TORCH_EXTENSIONS_DIR": service_root / "cache" / "torch-extensions",
        "TORCHINDUCTOR_CACHE_DIR": service_root / "cache" / "torchinductor",
        "TRITON_CACHE_DIR": service_root / "cache" / "triton",
        "NUMBA_CACHE_DIR": service_root / "cache" / "numba",
        "MPLCONFIGDIR": service_root / "config" / "matplotlib",
        "PYTHONPYCACHEPREFIX": service_root / "cache" / "pycache",
        "npm_config_cache": service_root / "cache" / "npm",
        "NPM_CONFIG_CACHE": service_root / "cache" / "npm",
    }
    for path in set(locations.values()):
        _ensure_mcp_runtime_directory(path)

    env = {
        key: value
        for key, value in os.environ.items()
        if key == "LANG" or key == "TZ" or key == "LC_ALL" or key.startswith("LC_")
    }
    env.update({str(key): str(value) for key, value in configured.items()})
    env.pop("AGENT_BUILDER_API_TOKEN", None)
    env.update({key: str(path) for key, path in locations.items()})
    env.update(
        {
            "PATH": os.pathsep.join(
                [
                    str(PROJECT_ROOT / ".venv" / "bin"),
                    str(PROJECT_ROOT / ".tools" / "node" / "bin"),
                    str(PROJECT_ROOT / ".tools"),
                    "/usr/bin",
                    "/bin",
                ]
            ),
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "AGENT_BUILDER_RUNTIME_DIR": str(RUNTIME_ROOT),
        }
    )
    return env


class MCPTool:
    """MCP工具封装"""
    def __init__(self, name: str, description: str, input_schema: Dict,
                 server_name: str, session: 'MCPServerConnection'):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.server_name = server_name
        self.session = session

    def to_langchain_tool(self) -> Dict[str, Any]:
        """转换为LangChain工具格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema
            }
        }


class MCPServerConnection:
    """MCP服务器连接（stdio模式）"""
    def __init__(self, config: MCPConfig):
        self.config = config
        self.session: Optional[ClientSession] = None
        self.tools: List[MCPTool] = []
        self._exit_stack = AsyncExitStack()
        self._connected = False

    async def connect(self) -> bool:
        """连接到MCP服务器"""
        if not MCP_AVAILABLE:
            print("MCP库未安装，跳过连接")
            return False

        async def establish() -> None:
            validate_stdio_configuration(
                self.config.command,
                self.config.args or [],
                self.config.env or {},
            )
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=build_stdio_environment(self.config.name, self.config.env or {}),
            )

            read, write = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )

            # 初始化会话
            await self.session.initialize()

            # 获取工具列表
            tools_result = await self.session.list_tools()
            raw_tools = list(tools_result.tools)
            if len(raw_tools) > MAX_MCP_TOOLS:
                raise ValueError("MCP returned too many tools")
            validated = [
                _validate_tool_definition(
                    tool.name,
                    tool.description or "",
                    tool.inputSchema or {},
                )
                for tool in raw_tools
            ]
            self.tools = [
                MCPTool(name, description, schema, self.config.name, self)
                for name, description, schema in validated
            ]

        try:
            await asyncio.wait_for(establish(), timeout=30)

            self._connected = True
            return True
        except asyncio.CancelledError:
            await asyncio.shield(self.disconnect())
            raise
        except Exception as e:
            await self.disconnect()
            print(
                f"连接MCP服务器 {self.config.name} 失败: "
                f"error_type={type(e).__name__}"
            )
            return False

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用工具"""
        if not self.session:
            return "错误: 未连接到MCP服务器"

        try:
            _validate_tool_arguments(arguments)
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments),
                timeout=60,
            )
            if result.content:
                return _bounded_tool_content(result.content)
            return "工具执行成功，无返回内容"
        except Exception as e:
            return f"工具调用失败 ({type(e).__name__})"

    async def disconnect(self):
        """断开连接"""
        try:
            await self._exit_stack.aclose()
        except Exception as exc:
            print(f"断开 stdio MCP 连接时出错: error_type={type(exc).__name__}")
        finally:
            self.session = None
            self.tools = []
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


class SSEServerConnection:
    """MCP服务器连接（SSE模式）- 支持标准 MCP SSE 协议和本地 REST API"""

    def __init__(self, config: MCPServiceConfig):
        self.config = config
        self.tools: List[MCPTool] = []
        self._connected = False
        # MCP SSE 客户端相关
        self._session: Optional[ClientSession] = None
        self._exit_stack = AsyncExitStack()
        self._read_stream = None
        self._write_stream = None
        # 本地 REST API 模式
        self._is_local_rest = False
        self._base_url = None
        self._resolved_target: Optional[ResolvedOutboundTarget] = None

    def _is_local_service(self) -> bool:
        """检测是否是本地 REST API 服务"""
        if not self.config.url:
            return False
        try:
            parsed = urlsplit(self.config.url)
            local_port = int(os.environ.get("MCP_SSE_PORT", "20882"))
            return (
                parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.port == local_port
                and parsed.path.rstrip("/") in {"/calculator", "/cold-jokes"}
            )
        except (TypeError, ValueError):
            return False

    def _get_headers(self) -> Dict[str, str]:
        """构建请求头"""
        validate_headers(self.config.headers)
        headers = {}
        headers.update(self.config.headers)

        # 添加认证
        if self.config.auth_type == MCPAuthType.BEARER and self.config.auth_value:
            headers["Authorization"] = f"Bearer {self.config.auth_value}"
        elif self.config.auth_type == MCPAuthType.APIKEY and self.config.auth_value:
            headers["X-API-Key"] = self.config.auth_value

        return headers

    async def connect(self) -> bool:
        """连接到MCP服务器（支持标准 MCP SSE 和本地 REST API）"""
        self.tools = []
        if not self.config.url:
            print("SSE模式需要配置URL")
            return False

        try:
            self._resolved_target = await resolve_outbound_target(self.config.url)
        except SecurityValidationError as exc:
            print(
                "拒绝不安全的MCP URL: "
                f"error_type={type(exc).__name__}"
            )
            return False

        # 检测是否是本地 REST API 服务
        if self._is_local_service():
            return await self._connect_local_rest()

        return await self._connect_mcp_sse()

    async def _connect_local_rest(self) -> bool:
        """连接到本地 REST API 服务"""
        import httpx

        self._is_local_rest = True
        # 从 URL 提取基础 URL 和服务路径
        # 例如: http://localhost:20882/calculator -> base_url=http://localhost:20882, service_path=/calculator
        url = self.config.url
        parsed = urlsplit(url)
        host_for_url = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        self._base_url = f"{parsed.scheme}://{host_for_url}:{parsed.port}"
        if parsed.path.rstrip("/") == "/calculator":
            service_path = "/calculator"
        elif parsed.path.rstrip("/") == "/cold-jokes":
            service_path = "/cold-jokes"
        else:
            print("不支持的本地REST服务路径")
            return False

        try:
            if self._resolved_target is None:
                raise SecurityValidationError("MCP target was not resolved")
            async with create_hardened_mcp_http_client(
                timeout=30,
                pinned_target=self._resolved_target,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}{service_path}/tools/list",
                    json={},
                    headers=self._get_headers(),
                ) as response:
                    response.raise_for_status()
                    data = await _read_json_response_limited(response)

                # 解析工具列表
                raw_tools = data.get("tools", []) if isinstance(data, dict) else []
                if not isinstance(raw_tools, list) or len(raw_tools) > MAX_MCP_TOOLS:
                    raise ValueError("MCP returned too many or invalid tools")
                for tool_data in raw_tools:
                    if not isinstance(tool_data, dict):
                        raise ValueError("MCP returned an invalid tool definition")
                    name, description, schema = _validate_tool_definition(
                        tool_data.get("name"),
                        tool_data.get("description", ""),
                        tool_data.get("inputSchema", {}),
                    )
                    self.tools.append(MCPTool(
                        name=name,
                        description=description,
                        input_schema=schema,
                        server_name=self.config.name,
                        session=self
                    ))

                self._connected = True
                print(f"✓ 本地 REST 服务连接成功: {self.config.name} ({len(self.tools)} 工具)")
                return True

        except Exception as e:
            print(f"连接本地 REST 服务失败: error_type={type(e).__name__}")
            return False

    async def _connect_mcp_sse(self) -> bool:
        """连接到标准 MCP SSE 服务"""
        if not MCP_AVAILABLE:
            print("MCP库未安装，无法使用SSE连接")
            return False

        async def establish() -> None:
            headers = self._get_headers()
            if self._resolved_target is None:
                raise SecurityValidationError("MCP target was not resolved")

            # 使用标准 MCP SSE 客户端
            sse_context = sse_client(
                self.config.url,
                headers=headers if headers else None,
                timeout=30,
                sse_read_timeout=300,  # 5分钟超时
                httpx_client_factory=partial(
                    create_hardened_mcp_http_client,
                    pinned_target=self._resolved_target,
                ),
            )

            # 进入 SSE 客户端上下文
            self._read_stream, self._write_stream = await self._exit_stack.enter_async_context(
                sse_context
            )

            # 创建 MCP 会话
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(self._read_stream, self._write_stream)
            )

            # 初始化会话
            await self._session.initialize()

            # 获取工具列表
            tools_result = await self._session.list_tools()
            raw_tools = list(tools_result.tools)
            if len(raw_tools) > MAX_MCP_TOOLS:
                raise ValueError("MCP returned too many tools")
            validated = [
                _validate_tool_definition(
                    tool.name,
                    tool.description or "",
                    tool.inputSchema or {},
                )
                for tool in raw_tools
            ]
            self.tools = [
                MCPTool(name, description, schema, self.config.name, self)
                for name, description, schema in validated
            ]

        try:
            await asyncio.wait_for(establish(), timeout=30)

            self._connected = True
            print(f"✓ SSE MCP 服务连接成功: {self.config.name} ({len(self.tools)} 工具)")
            return True
        except asyncio.CancelledError:
            await asyncio.shield(self.disconnect())
            raise
        except Exception as e:
            await self.disconnect()
            print(
                f"连接SSE MCP服务器 {self.config.name} 失败: "
                f"error_type={type(e).__name__}"
            )
            return False

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用工具（支持本地 REST API 和远程 MCP SSE）"""
        try:
            _validate_tool_arguments(arguments)
        except ValueError:
            return "工具调用失败: 参数无效或超过大小限制"
        # 本地 REST API 模式
        if self._is_local_rest:
            return await self._call_tool_local_rest(tool_name, arguments)

        # 远程 MCP SSE 模式
        return await self._call_tool_mcp_sse(tool_name, arguments)

    async def _call_tool_local_rest(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用本地 REST API 工具"""
        import httpx

        if not self._connected:
            if not await self.connect():
                return "错误: 无法建立连接"

        try:
            print(f"[LocalREST] 调用工具 {tool_name}")

            # 确定服务端点路径
            service_path = ""
            if "calculator" in self.config.url:
                service_path = "/calculator"
            elif "cold-jokes" in self.config.url:
                service_path = "/cold-jokes"

            if self._resolved_target is None:
                raise SecurityValidationError("MCP target was not resolved")
            async with create_hardened_mcp_http_client(
                timeout=60,
                pinned_target=self._resolved_target,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}{service_path}/tools/call",
                    json={"name": tool_name, "arguments": arguments},
                    headers=self._get_headers(),
                ) as response:
                    response.raise_for_status()
                    data = await _read_json_response_limited(response)

                # 解析返回内容 - 支持两种格式
                # 格式1: {"result": "..."}
                # 格式2: {"content": [{"type": "text", "text": "..."}]}
                if "result" in data:
                    result = data["result"]
                elif "content" in data:
                    # 提取所有文本内容
                    texts = []
                    for item in data["content"]:
                        if isinstance(item, dict) and item.get("type") == "text":
                            texts.append(item.get("text", ""))
                        elif isinstance(item, str):
                            texts.append(item)
                    result = "\n".join(texts)
                else:
                    result = str(data)

                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if len(result) > MAX_MCP_RESULT_CHARS:
                    raise ValueError("MCP tool result exceeds the 1MB limit")
                print(f"[LocalREST] 工具返回成功 ({len(result)} 字符)")
                return result

        except Exception as e:
            print(f"[LocalREST] 工具调用失败: error_type={type(e).__name__}")
            return f"工具调用失败 ({type(e).__name__})"

    async def _call_tool_mcp_sse(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用远程 MCP SSE 工具（支持自动重连、重试和超时）

        【AC130-202603141800 TC-002 修复】
        改进连接状态检查，处理 session 存在但已失效的情况
        """
        max_retries = 2

        for attempt in range(max_retries + 1):
            # ========================================
            # 【TC-002 修复】改进连接状态检查
            # ========================================
            # 检查 session 是否为 None 或连接标志是否为 False
            if not self._session or not self._connected:
                # 连接无效或未连接，先建立连接
                print(f"[SSE] 检测到连接断开（session={bool(self._session)}, connected={self._connected}），尝试重新连接...")
                # 清理可能的旧连接
                await self.disconnect()
                if not await self.connect():
                    if attempt < max_retries:
                        print(f"[SSE] 连接失败，重试 {attempt + 1}/{max_retries}...")
                        await asyncio.sleep(1)
                        continue
                    return "错误: 无法建立连接"

            async def _do_call():
                print(f"[SSE] 调用工具 {tool_name}")
                result = await self._session.call_tool(tool_name, arguments)
                print(f"[SSE] 工具返回 isError: {getattr(result, 'isError', None)}")

                if result.isError:
                    error_content = (
                        _bounded_tool_content(result.content)
                        if result.content
                        else "未知错误"
                    )
                    print("[SSE] 工具返回错误")
                    raise Exception(error_content)

                if result.content:
                    content = _bounded_tool_content(result.content)
                    print(f"[SSE] 工具返回成功 ({len(content)} 字符)")
                    return content
                return "工具执行成功，无返回内容"

            try:
                # 添加 60 秒超时
                return await asyncio.wait_for(_do_call(), timeout=60)
            except asyncio.TimeoutError:
                print(f"[SSE] 工具调用超时")
                await self.disconnect()
                if attempt < max_retries:
                    print(f"[SSE] 超时，重试 {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(1)
                    continue
                return "工具调用失败: 超时，请稍后重试"
            except Exception as e:
                error_type = type(e).__name__
                print(f"[SSE] 工具调用失败: error_type={error_type}")

                # 断开连接
                await self.disconnect()

                if attempt < max_retries:
                    print(f"[SSE] 失败，重试 {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(1)
                    continue
                return f"工具调用失败 ({error_type})"

        return "工具调用失败: 重试次数用尽"

    async def disconnect(self):
        """断开连接"""
        if self._is_local_rest:
            # 本地 REST API 不需要保持连接
            self._connected = False
            self.tools = []
            return

        try:
            await self._exit_stack.aclose()
        except Exception as e:
            print(f"断开 SSE 连接时出错: error_type={type(e).__name__}")
        finally:
            self._session = None
            self._read_stream = None
            self._write_stream = None
            self.tools = []
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


class MCPManager:
    """MCP管理器 - 支持stdio和SSE两种连接方式"""

    # 类型别名，用于存储不同类型的连接
    ConnectionType = 'MCPServerConnection | SSEServerConnection'

    def __init__(self):
        self.servers: Dict[str, MCPManager.ConnectionType] = {}
        self.all_tools: List[MCPTool] = []

    async def add_server(self, config: MCPConfig) -> bool:
        """添加MCP服务器（stdio模式，兼容旧API）"""
        if config.name in self.servers:
            print(f"MCP服务器 {config.name} 已存在")
            return False

        connection = MCPServerConnection(config)
        if await connection.connect():
            self.servers[config.name] = connection
            self.all_tools.extend(connection.tools)
            return True
        return False

    async def add_service(self, config: MCPServiceConfig) -> bool:
        """添加MCP服务（支持stdio和SSE两种模式）"""
        if config.name in self.servers:
            print(f"MCP服务 {config.name} 已存在")
            return False

        try:
            if config.connection_type == MCPConnectionType.SSE:
                connection = SSEServerConnection(config)
            else:
                # stdio 模式：将 MCPServiceConfig 转换为 MCPConfig
                if not config.command:
                    print(f"MCP服务 {config.name} 缺少command配置")
                    return False
                mcp_config = MCPConfig(
                    name=config.name,
                    command=config.command,
                    args=config.args,
                    env=config.env
                )
                connection = MCPServerConnection(mcp_config)

            if await connection.connect():
                self.servers[config.name] = connection
                self.all_tools.extend(connection.tools)
                return True
            return False
        except Exception as e:
            print(
                f"添加MCP服务 {config.name} 失败: "
                f"error_type={type(e).__name__}"
            )
            return False

    async def remove_server(self, name: str):
        """移除MCP服务器"""
        if name in self.servers:
            server = self.servers[name]
            await server.disconnect()
            self.all_tools = [t for t in self.all_tools if t.server_name != name]
            del self.servers[name]

    def get_tool(self, name: str) -> Optional[MCPTool]:
        """获取工具"""
        for tool in self.all_tools:
            if tool.name == name:
                return tool
        return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用工具"""
        tool = self.get_tool(tool_name)
        if tool:
            return await tool.session.call_tool(tool_name, arguments)
        return f"错误: 找不到工具 {tool_name}"

    def get_langchain_tools(self) -> List[Dict[str, Any]]:
        """获取所有工具的LangChain格式"""
        return [tool.to_langchain_tool() for tool in self.all_tools]

    def get_server_status(self, name: str) -> Dict[str, Any]:
        """获取服务器连接状态"""
        if name not in self.servers:
            return {"connected": False, "tools": []}

        server = self.servers[name]
        return {
            "connected": server.is_connected,
            "tools": [tool.name for tool in server.tools]
        }

    async def shutdown(self):
        """关闭所有连接"""
        for name in list(self.servers.keys()):
            await self.remove_server(name)


async def test_mcp_connection(config: MCPServiceConfig) -> Dict[str, Any]:
    """测试MCP服务连接（独立函数，用于API调用）"""
    result = {
        "success": False,
        "tools": [],
        "error": None,
        "mcp_available": MCP_AVAILABLE
    }

    # 检查MCP库是否可用（仅针对远程SSE服务）
    if config.connection_type == MCPConnectionType.SSE and not MCP_AVAILABLE:
        # 检查是否是本地服务（本地服务使用httpx，不需要MCP库）
        is_local = config.url and SSEServerConnection(config)._is_local_service()
        if not is_local:
            result["error"] = "MCP库未安装，无法连接远程SSE服务。请运行: pip install mcp"
            return result

    connection = None
    try:
        if config.connection_type == MCPConnectionType.SSE:
            connection = SSEServerConnection(config)
        else:
            if not config.command:
                result["error"] = "stdio模式需要配置command"
                return result
            mcp_config = MCPConfig(
                name=config.name,
                command=config.command,
                args=config.args,
                env=config.env
            )
            connection = MCPServerConnection(mcp_config)

        if await connection.connect():
            result["success"] = True
            result["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description
                }
                for tool in connection.tools
            ]
            result["tool_count"] = len(connection.tools)
        else:
            # 提供更详细的错误信息
            if config.connection_type == MCPConnectionType.SSE and not MCP_AVAILABLE:
                if not (config.url and SSEServerConnection(config)._is_local_service()):
                    result["error"] = "MCP库未安装，无法连接远程SSE服务。请运行: pip install mcp"
                else:
                    result["error"] = "连接失败，请检查服务是否运行"
            else:
                result["error"] = "连接失败，请检查配置或网络"
    except Exception as e:
        result["error"] = f"连接异常 ({type(e).__name__})"
    finally:
        if connection is not None:
            await connection.disconnect()

    return result

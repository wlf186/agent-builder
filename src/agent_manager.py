"""
Agent管理器 - 管理多个Agent实例
"""
import json
import asyncio
import os
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional, List, TYPE_CHECKING, Any, Set
from datetime import datetime

from .models import AgentConfig, MCPConfig
from .agent_engine import AgentEngine
from .mcp_manager import MCPManager
from .mcp_registry import MCPServiceRegistry
from .skill_registry import SkillRegistry
from .model_service_registry import ModelServiceRegistry
from .storage_paths import ensure_real_directory, validate_regular_file

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine


class AgentInstance:
    """Agent实例"""
    def __init__(
        self,
        config: AgentConfig,
        mcp_registry: MCPServiceRegistry = None,
        skill_registry: SkillRegistry = None,
        skills_dir: Path = None,
        model_service_registry: ModelServiceRegistry = None,
        execution_engine: Optional["ExecutionEngine"] = None,
        agent_manager: Optional["AgentManager"] = None,  # 【AC130-202603142210】
        kb_manager: Optional[Any] = None,  # 【AC130-202603161542】知识库管理器
        embedder: Optional[Any] = None      # 【AC130-202603161542】向量化器
    ):
        self.config = config
        self.mcp_registry = mcp_registry
        self.skill_registry = skill_registry
        self.skills_dir = skills_dir
        self.model_service_registry = model_service_registry
        self.execution_engine = execution_engine
        self.agent_manager = agent_manager  # 【AC130-202603142210】用于子Agent调用
        self.kb_manager = kb_manager        # 【AC130-202603161542】知识库管理器
        self.embedder = embedder            # 【AC130-202603161542】向量化器
        self.mcp_manager: Optional[MCPManager] = None
        self.engine: Optional[AgentEngine] = None
        self.created_at = datetime.now()
        self.conversation_history: Deque[Dict] = deque(maxlen=100)
        self._execution_lock = asyncio.Lock()
        self._shutdown_complete = False

    async def initialize(self) -> bool:
        """初始化Agent"""
        try:
            # 初始化MCP管理器
            if self.config.mcp_servers or self.config.mcp_services:
                self.mcp_manager = MCPManager()

                # 加载旧的 mcp_servers 配置
                if self.config.mcp_servers:
                    for server_config in self.config.mcp_servers:
                        mcp_config = MCPConfig(**server_config)
                        await self.mcp_manager.add_server(mcp_config)

                # 从 MCP 注册表加载 mcp_services
                if self.config.mcp_services and self.mcp_registry:
                    for service_name in self.config.mcp_services:
                        service_config = self.mcp_registry.get_service(service_name)
                        if service_config:
                            # 根据连接类型添加服务
                            success = await self.mcp_manager.add_service(service_config)
                            if success:
                                print(f"已加载 MCP 服务: {service_name}, 可用工具: {[t.name for t in self.mcp_manager.all_tools]}")
                            else:
                                print(f"警告: MCP 服务 {service_name} 连接失败")
                        else:
                            print(f"警告: MCP 服务 {service_name} 不存在")

            # 初始化引擎
            self.engine = AgentEngine(
                self.config,
                self.mcp_manager,
                self.skill_registry,
                self.skills_dir,
                self.model_service_registry,
                execution_engine=self.execution_engine,
                agent_manager=self.agent_manager,  # 【AC130-202603142210】
                kb_manager=self.kb_manager,       # 【AC130-202603161542】
                embedder=self.embedder            # 【AC130-202603161542】
            )
            self.engine.build_graph()
            self._shutdown_complete = False

            return True
        except Exception as e:
            print(f"初始化Agent失败: error_type={type(e).__name__}")
            return False

    async def _ensure_mcp_connections(self):
        """确保 MCP 连接有效，如果连接断开则重新连接

        【AC130-202603141800 TC-002 修复】
        添加实际的连接状态检查和自动重连机制
        """
        if not self.mcp_manager:
            return

        for name, server in self.mcp_manager.servers.items():
            # 检查 SSE 连接是否还有效
            if hasattr(server, '_session'):
                # ========================================
                # 【TC-002 修复】实际连接状态检查
                # ========================================
                # 本地 REST 服务按请求创建 HTTP 客户端，本来就没有持久
                # `_session`；连接状态由成功加载的工具清单标记。远程 SSE
                # 仍须同时具备活跃 session 和连接标志。
                is_local_rest = bool(getattr(server, "_is_local_rest", False))
                is_valid = bool(server.is_connected) and (
                    is_local_rest or server._session is not None
                )

                if not is_valid:
                    print(f"[MCP] 服务 {name} 连接已断开，尝试重新连接...")
                    try:
                        # 先清理旧连接
                        await server.disconnect()
                        # 尝试重新连接
                        success = await server.connect()
                        if success:
                            print(f"[MCP] 服务 {name} 重新连接成功 ({len(server.tools)} 工具)")
                        else:
                            print(f"[MCP] 服务 {name} 重新连接失败")
                    except Exception as e:
                        print(
                            f"[MCP] 服务 {name} 重新连接异常: "
                            f"error_type={type(e).__name__}"
                        )

    async def chat(self, message: str, history: List[Dict] = None) -> str:
        """Serialize access to the cached engine's mutable execution state."""
        async with self._execution_lock:
            if self._shutdown_complete:
                raise RuntimeError("Agent实例已关闭")
            return await self._chat_unlocked(message, history)

    async def _chat_unlocked(self, message: str, history: List[Dict] = None) -> str:
        """对话"""
        if not self.engine:
            if not await self.initialize():
                raise RuntimeError("Agent实例初始化失败")

        # 确保 MCP 连接有效
        await self._ensure_mcp_connections()

        # 使用传入的历史或本地历史，根据 short_term_memory 截取
        chat_history = history if history is not None else []
        memory_limit = self.config.short_term_memory

        # 截取最近 N 轮对话（每轮包含 user 和 assistant）
        if memory_limit > 0 and len(chat_history) > memory_limit * 2:
            chat_history = chat_history[-(memory_limit * 2):]

        response = await self.engine.run(message, chat_history)

        # 记录对话历史
        self.conversation_history.append({
            "role": "user",
            "content": message,
            "timestamp": datetime.now().isoformat()
        })
        self.conversation_history.append({
            "role": "assistant",
            "content": response,
            "timestamp": datetime.now().isoformat()
        })

        return response

    async def chat_stream(self, message: str, history: List[Dict] = None, file_context: str = "", trace_id: str = None, conversation_id: str = None):
        """Serialize a complete stream, including reconnect and final state."""
        async with self._execution_lock:
            if self._shutdown_complete:
                raise RuntimeError("Agent实例已关闭")
            async for event in self._chat_stream_unlocked(
                message,
                history,
                file_context,
                trace_id,
                conversation_id,
            ):
                yield event

    async def _chat_stream_unlocked(self, message: str, history: List[Dict] = None, file_context: str = "", trace_id: str = None, conversation_id: str = None):
        """流式对话 - 返回包含 thinking、tool_call、tool_result、content 的事件

        Args:
            message: 用户消息
            history: 对话历史
            file_context: 文件上下文信息（包含用户上传文件的元数据）
            trace_id: 追踪 ID（用于日志关联）
            conversation_id: 会话 ID（用于关联可观测链路）
        """
        if not self.engine:
            if not await self.initialize():
                raise RuntimeError("Agent实例初始化失败")

        # 确保 MCP 连接有效
        await self._ensure_mcp_connections()

        # 使用传入的历史或本地历史，根据 short_term_memory 截取
        chat_history = history if history is not None else []
        memory_limit = self.config.short_term_memory

        # 截取最近 N 轮对话（每轮包含 user 和 assistant）
        if memory_limit > 0 and len(chat_history) > memory_limit * 2:
            chat_history = chat_history[-(memory_limit * 2):]

        response_chunks: List[str] = []
        async for event in self.engine.stream(message, chat_history, file_context, trace_id, conversation_id=conversation_id):
            # event 是一个字典，包含 type 和其他字段
            if isinstance(event, dict):
                if event.get("type") == "content":
                    response_chunks.append(event.get("content", ""))
                yield event
            else:
                # 兼容旧格式（纯字符串）
                response_chunks.append(event)
                yield {"type": "content", "content": event}

        full_response = "".join(response_chunks)

        # 记录对话历史
        self.conversation_history.append({
            "role": "user",
            "content": message,
            "timestamp": datetime.now().isoformat()
        })
        self.conversation_history.append({
            "role": "assistant",
            "content": full_response,
            "timestamp": datetime.now().isoformat()
        })

    async def run_with_call_stack(self, message: str, call_stack: List[str]) -> str:
        """Run a sub-agent with request-local cycle state under the instance lock."""
        async with self._execution_lock:
            if self._shutdown_complete:
                raise RuntimeError("Agent实例已关闭")
            if not self.engine and not await self.initialize():
                raise RuntimeError("Agent实例初始化失败")
            assert self.engine is not None
            token = self.engine.set_request_call_stack(call_stack)
            try:
                return await self.engine.run(message, history=[])
            finally:
                self.engine.reset_request_call_stack(token)

    def get_token_usage(self) -> Dict[str, int]:
        """Return token usage for the caller's current async request."""
        if not self.engine:
            return {"input_tokens": 0, "output_tokens": 0}
        return self.engine.get_token_usage()

    async def shutdown(self):
        """关闭Agent"""
        async with self._execution_lock:
            if self._shutdown_complete:
                return
            errors = []
            try:
                if self.mcp_manager:
                    await self.mcp_manager.shutdown()
            except Exception as exc:
                errors.append(exc)
            try:
                if self.engine and hasattr(self.engine, "aclose"):
                    await self.engine.aclose()
            except Exception as exc:
                errors.append(exc)
            self._shutdown_complete = True
            if errors:
                raise RuntimeError("Agent resource cleanup failed") from errors[0]


class AgentManager:
    """Agent管理器"""
    MAX_AGENT_CONFIGS = 100
    def __init__(
        self,
        data_dir: Path,
        mcp_registry=None,
        skill_registry=None,
        skills_dir=None,
        model_service_registry=None,
        execution_engine=None,
        kb_manager=None,  # 【AC130-202603161542】知识库管理器
        embedder=None     # 【AC130-202603170949】向量化器
    ):
        self.data_dir = ensure_real_directory(Path(data_dir))
        self.mcp_registry = mcp_registry
        self.skill_registry = skill_registry
        self.skills_dir = skills_dir or (Path(__file__).parent.parent / "skills")
        self.model_service_registry = model_service_registry
        self.execution_engine = execution_engine
        self.kb_manager = kb_manager  # 【AC130-202603161542】
        self.embedder = embedder      # 【AC130-202603170949】
        self.agents: Dict[str, AgentInstance] = {}
        self.configs: Dict[str, AgentConfig] = {}
        self._agent_locks: Dict[str, asyncio.Lock] = {}
        self._config_generations: Dict[str, int] = {}
        self._background_tasks: Set[asyncio.Task] = set()
        self._shutting_down = False
        self._config_lock = threading.RLock()
        self._load_configs()
        for agent_name in self.configs:
            self._config_generations.setdefault(agent_name, 0)

    def _agent_lock(self, name: str) -> asyncio.Lock:
        lock = self._agent_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[name] = lock
        return lock

    def _advance_config_generation(self, name: str) -> None:
        self._config_generations[name] = self._config_generations.get(name, 0) + 1

    def _schedule_instance_shutdown(self, instance: Optional[AgentInstance]) -> None:
        """Close a detached instance without racing its in-flight execution."""
        if instance is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(instance.shutdown())
            return

        task = loop.create_task(instance.shutdown())
        self._background_tasks.add(task)

        def task_finished(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(
                    "[WARN] 关闭旧 Agent 实例失败: "
                    f"error_type={type(exc).__name__}"
                )

        task.add_done_callback(task_finished)

    def _build_instance(self, config: AgentConfig) -> AgentInstance:
        return AgentInstance(
            config,
            self.mcp_registry,
            self.skill_registry,
            self.skills_dir,
            self.model_service_registry,
            execution_engine=self.execution_engine,
            agent_manager=self,
            kb_manager=self.kb_manager,
            embedder=self.embedder,
        )

    def _load_configs(self):
        """加载已保存的配置"""
        config_file = self.data_dir / "agent_configs.json"
        if config_file.exists():
            try:
                validate_regular_file(config_file, allow_missing=False)
                with open(config_file, "r", encoding="utf-8") as f:
                    configs_data = json.load(f)
                if not isinstance(configs_data, dict):
                    raise ValueError("Agent 配置必须是对象")
                for name, config in list(configs_data.items())[: self.MAX_AGENT_CONFIGS]:
                    parsed = AgentConfig(**config)
                    if parsed.name != name:
                        raise ValueError("Agent 配置键与名称不一致")
                    self.configs[name] = parsed
            except Exception as e:
                print(f"加载配置失败: error_type={type(e).__name__}")
                self.configs.clear()

    def _save_configs(self):
        """Atomically persist a compact configuration snapshot."""
        ensure_real_directory(self.data_dir)
        config_file = self.data_dir / "agent_configs.json"
        configs_data = {
            name: config.model_dump()
            for name, config in self.configs.items()
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_file.name}.", suffix=".tmp", dir=config_file.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    configs_data,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, config_file)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def create_agent_config(self, config: AgentConfig) -> bool:
        """创建Agent配置"""
        with self._config_lock:
            if (
                config.name in self.configs
                or len(self.configs) >= self.MAX_AGENT_CONFIGS
            ):
                return False
            self.configs[config.name] = config
            try:
                self._save_configs()
            except Exception:
                self.configs.pop(config.name, None)
                return False
            self._advance_config_generation(config.name)
            return True

    def update_agent_config(self, name: str, config: AgentConfig) -> bool:
        """更新Agent配置"""
        with self._config_lock:
            previous = self.configs.get(name)
            if previous is None or config.name != name:
                return False
            if previous == config:
                return True
            self.configs[name] = config
            try:
                self._save_configs()
            except Exception:
                self.configs[name] = previous
                return False
            self._advance_config_generation(name)

        # Detach synchronously so a concurrent get cannot return stale config;
        # shutdown itself waits for the instance execution lock in the background.
        self._schedule_instance_shutdown(self.agents.pop(name, None))

        return True

    def delete_agent_config(self, name: str) -> bool:
        """删除Agent配置"""
        with self._config_lock:
            previous = self.configs.pop(name, None)
            if previous is None:
                return False
            try:
                self._save_configs()
            except Exception:
                self.configs[name] = previous
                return False
            self._advance_config_generation(name)
        self._schedule_instance_shutdown(self.agents.pop(name, None))
        return True

    def list_agents(self) -> List[str]:
        """列出所有Agent"""
        with self._config_lock:
            return list(self.configs.keys())

    def get_config(self, name: str) -> Optional[AgentConfig]:
        """获取配置"""
        with self._config_lock:
            return self.configs.get(name)

    async def ensure_agent_stopped(self, name: str) -> None:
        """Wait for initialization/in-flight work and fully detach one instance."""
        async with self._agent_lock(name):
            instance = self.agents.pop(name, None)
            if instance is not None:
                await instance.shutdown()
        pending = list(self._background_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def get_instance(self, name: str, force_new: bool = False) -> Optional[AgentInstance]:
        """获取或创建Agent实例

        Args:
            name: Agent 名称
            force_new: 是否强制创建新实例（用于 SSE 连接等需要重新连接的场景）
        """
        lock = self._agent_lock(name)
        async with lock:
            while not self._shutting_down:
                config = self.configs.get(name)
                if not config:
                    return None
                generation = self._config_generations.get(name, 0)

                cached = self.agents.get(name)
                if cached is not None and not force_new:
                    return cached

                if cached is not None:
                    self.agents.pop(name, None)
                    await cached.shutdown()
                    if generation != self._config_generations.get(name, 0):
                        continue

                instance = self._build_instance(config)
                initialized = await instance.initialize()

                # A synchronous update/delete may run while initialize awaits.
                # Never publish an instance built from the stale generation.
                if (
                    self._shutting_down
                    or generation != self._config_generations.get(name, 0)
                    or self.configs.get(name) is not config
                ):
                    await instance.shutdown()
                    if self.configs.get(name) is None or self._shutting_down:
                        return None
                    continue

                if not initialized:
                    await instance.shutdown()
                    return None
                self.agents[name] = instance
                return instance

        return None

    async def shutdown_all(self):
        """关闭所有Agent"""
        self._shutting_down = True
        names = sorted(set(self.agents) | set(self.configs))
        for name in names:
            async with self._agent_lock(name):
                instance = self.agents.pop(name, None)
                if instance is not None:
                    await instance.shutdown()

        pending = list(self._background_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.agents.clear()

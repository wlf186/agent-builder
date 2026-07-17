"""
Agent核心引擎 - 基于LangGraph，支持多种规划模式
"""
import os
import asyncio
import hashlib
import inspect
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, Token
from functools import partial
from pathlib import Path
from typing import (
    TypedDict,
    Annotated,
    Sequence,
    Optional,
    Dict,
    Any,
    List,
    Mapping,
    Tuple,
    TYPE_CHECKING,
)
from operator import add

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, END

from .models import AgentConfig, LLMProvider, PlanningMode, ModelProvider
from .mcp_manager import MCPManager
from .mcp_tool_adapter import (
    MCPToolAdapter,
    summarize_tool_arguments,
    tool_result_length,
)
from .skill_registry import SkillRegistry
from .skill_loader import SkillLoader
from .skill_tool import SkillTool
from .model_service_registry import ModelServiceRegistry
from .cycle_detector import CycleDetector
from .observability import get_tracer, is_observability_enabled
from .security import validate_outbound_url

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine
    from .agent_manager import AgentManager


# 工具调用策略枚举
class ToolCallStrategy:
    """工具调用策略"""
    AUTO = "auto"       # 模型自主决定（默认）
    ANY = "any"         # 强制至少调用一个工具
    REQUIRED = "required"  # 强制至少调用一个工具（OpenAI）


class AgentState(TypedDict):
    """Agent状态"""
    messages: Annotated[Sequence[BaseMessage], add]
    iterations: int
    # Plan & Solve / ReWOO 相关
    plan: Optional[List[str]]
    current_step: int
    tool_results: Optional[Dict[str, Any]]
    # Reflexion 相关
    reflection: Optional[str]
    is_satisfactory: bool
    # ToT 相关
    thoughts: Optional[List[str]]
    current_thought: Optional[str]
    evaluations: Optional[List[Dict[str, Any]]]


class AgentEngine:
    """Agent引擎 - 支持多种规划模式"""

    RAG_MAX_WORKERS = 2
    MAX_LLM_RESPONSE_CHARS = 2 * 1024 * 1024
    MAX_LLM_TOOL_ARGUMENT_BYTES = 1024 * 1024
    MAX_LLM_TOOL_CALLS = 64
    _OPAQUE_TRACE_ID = re.compile(r"^trace-[0-9a-f]{32}$")

    def __init__(
        self,
        config: AgentConfig,
        mcp_manager: Optional[MCPManager] = None,
        skill_registry: Optional[SkillRegistry] = None,
        skills_dir: Path = None,
        model_service_registry: Optional[ModelServiceRegistry] = None,
        execution_engine: Optional["ExecutionEngine"] = None,
        agent_manager: Optional["AgentManager"] = None,
        kb_manager: Optional[Any] = None,  # 知识库管理器
        embedder: Optional[Any] = None      # 向量化器
    ):
        self.config = config
        self.mcp_manager = mcp_manager
        self.skill_registry = skill_registry
        self.skills_dir = skills_dir or (Path(__file__).parent.parent / "skills")
        self.model_service_registry = model_service_registry
        self.execution_engine = execution_engine
        self.agent_manager = agent_manager  # 用于子Agent调用
        self.llm = None
        self.llm_with_tools = None  # 绑定工具后的 LLM
        self._http_client = None
        self._http_async_client = None
        self._client_close_tasks: set[asyncio.Task] = set()
        self._close_task: Optional[asyncio.Task] = None
        self._llm_fingerprint: Optional[Tuple[Any, ...]] = None
        self.graph = None
        self._tool_adapter: Optional[MCPToolAdapter] = None
        self._tool_call_strategy = ToolCallStrategy.AUTO  # 默认策略
        # 初始化 SkillTool 用于按需加载技能
        self.skill_tool: Optional[SkillTool] = None
        if skill_registry and config.skills:
            print(f"[DEBUG] AgentEngine.__init__: skill_count={len(config.skills)}")
            self.skill_tool = SkillTool(
                skill_registry=skill_registry,
                skills_dir=self.skills_dir,
                enabled_skills=config.skills,  # 初始值
                execution_engine=execution_engine,
                agent_name=config.name
            )
            print(f"[DEBUG] SkillTool.enabled_skill_count={len(self.skill_tool.enabled_skills)}")

        # 保存config引用，用于动态刷新skill_tool的enabled_skills
        self._config_ref = config

        # ====================================================================
        # 【AC130-202603142210】Agent-as-a-Tool: 子Agent支持
        # ====================================================================
        # 运行时调用栈追踪（用于循环检测）
        self._call_stack: List[str] = []
        self._call_stack_context: ContextVar[Tuple[str, ...]] = ContextVar(
            f"agent_call_stack_{id(self)}", default=()
        )
        # 子Agent并发控制（信号量）
        self._sub_agent_semaphore: Optional[asyncio.Semaphore] = None
        # 循环检测器（延迟初始化）
        self._cycle_detector: Optional[CycleDetector] = None

        # ====================================================================
        # 【AC130-202603161542】RAG 知识库支持
        # ====================================================================
        self.kb_manager = kb_manager
        self.embedder = embedder
        self._retrievers: Dict[str, Any] = {}  # kb_id -> Retriever
        self._retrievers_initialized = not bool(kb_manager and config.knowledge_bases)
        self._retriever_init_task: Optional[asyncio.Task] = None
        self._rag_executor: Optional[ThreadPoolExecutor] = None
        self._rag_worker_semaphore: Optional[asyncio.Semaphore] = None
        self._rag_futures: set[asyncio.Future] = set()
        self._rag_closed = False
        self._last_retrieval_sources: List[Dict[str, Any]] = []  # Track sources for citations
        self._retrieval_sources_context: ContextVar[Tuple[Dict[str, Any], ...]] = ContextVar(
            f"agent_retrieval_sources_{id(self)}", default=()
        )

        # ====================================================================
        # 【上下文窗口状态栏】Token 追踪
        # ====================================================================
        self._last_input_tokens: int = 0
        self._last_output_tokens: int = 0
        # 后备估算：当 API 不返回 usage_metadata 时，基于内容字符数估算
        self._last_input_chars: int = 0
        self._last_output_chars: int = 0
        self._request_token_usage: ContextVar[Tuple[int, int]] = ContextVar(
            f"agent_token_usage_{id(self)}", default=(0, 0)
        )

    def _normalize_trace_id(self, trace_id: Optional[str]) -> str:
        """Turn caller-controlled correlation text into an opaque identifier."""
        if isinstance(trace_id, str) and self._OPAQUE_TRACE_ID.fullmatch(trace_id):
            return trace_id
        if trace_id is None:
            seed = (
                f"{self.config.name}:{__import__('time').time_ns()}:"
                f"{id(asyncio.current_task())}"
            )
        else:
            seed = str(trace_id)
        digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:32]
        return f"trace-{digest}"

    def _refresh_skill_tool_if_needed(self):
        """检查并刷新工具配置，仅在指纹变化时重建 LLM。"""
        if self.skill_tool and hasattr(self._config_ref, 'skills'):
            current_skills = set(self._config_ref.skills or [])
            cached_skills = set(self.skill_tool.enabled_skills or [])
            if current_skills != cached_skills:
                # 配置已变化，更新skill_tool
                self.skill_tool.enabled_skills = list(current_skills)
                # 同时清除缓存，避免使用旧的skill内容
                self.skill_tool._loaded_skills.clear()

        fingerprint = self._current_llm_fingerprint()
        if self.llm is None or fingerprint != self._llm_fingerprint:
            self._setup_llm()
            self._llm_fingerprint = fingerprint

    @staticmethod
    def _fingerprint_secret(value: Optional[str]) -> str:
        """Track credential rotation without retaining plaintext in the fingerprint."""
        if not value:
            return ""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_fingerprint_value(value: Any) -> str:
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return repr(value)

    def _current_llm_fingerprint(self) -> Tuple[Any, ...]:
        """Return the model and tool configuration that affects the bound client."""
        service_fingerprint: Tuple[Any, ...] = ()
        if self.config.model_service and self.model_service_registry:
            service = self.model_service_registry.get_service(self.config.model_service)
            if service:
                service_fingerprint = (
                    service.name,
                    getattr(service.provider, "value", service.provider),
                    service.base_url,
                    service.selected_model,
                    self._fingerprint_secret(service.api_key),
                )

        mcp_tools: List[Tuple[str, str, str]] = []
        if self.mcp_manager:
            for tool in self.mcp_manager.all_tools or []:
                mcp_tools.append(
                    (
                        str(getattr(tool, "name", "")),
                        str(getattr(tool, "description", "")),
                        self._stable_fingerprint_value(
                            getattr(tool, "input_schema", getattr(tool, "args_schema", None))
                        ),
                    )
                )

        return (
            service_fingerprint,
            getattr(self.config.llm_provider, "value", self.config.llm_provider),
            self.config.llm_model,
            self.config.llm_base_url,
            self.config.temperature,
            tuple(sorted(self.skill_tool.enabled_skills or [])) if self.skill_tool else (),
            tuple(sorted(mcp_tools)),
            tuple(self.get_sub_agent_names()),
            tuple(self.config.knowledge_bases or []),
            self._tool_call_strategy,
        )

    def _setup_llm(self):
        """设置LLM - 从模型服务注册表获取配置"""
        import httpx
        from langchain_openai import ChatOpenAI

        self._retire_llm_clients()

        if self._http_client is None:
            timeout = httpx.Timeout(60.0, connect=10.0)
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
            self._http_client = httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
                limits=limits,
            )
            self._http_async_client = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
                limits=limits,
            )

        # 新版：从模型服务注册表获取配置
        if self.config.model_service and self.model_service_registry:
            service = self.model_service_registry.get_service(self.config.model_service)
            if service:
                # 所有供应商都使用OpenAI兼容接口
                self.llm = ChatOpenAI(
                    model=service.selected_model,
                    base_url=service.base_url,
                    api_key=service.api_key or "not-needed",  # Ollama不需要API key
                    temperature=self.config.temperature,
                    stream_usage=True,  # 启用流式响应中的 token 使用统计
                    max_retries=2,
                    http_client=self._http_client,
                    http_async_client=self._http_async_client,
                )
                # 绑定工具到 LLM
                self._bind_tools_to_llm()
                return
            else:
                raise ValueError(f"模型服务 '{self.config.model_service}' 不存在")

        # 兼容旧配置（已废弃）
        if self.config.llm_provider == LLMProvider.OLLAMA:
            from langchain_ollama import ChatOllama
            self.llm = ChatOllama(
                model=self.config.llm_model or "qwen2.5:7b",
                base_url=self.config.llm_base_url or "http://localhost:11434",
                temperature=self.config.temperature,
                client_kwargs={"trust_env": False, "follow_redirects": False},
                async_client_kwargs={"trust_env": False, "follow_redirects": False},
                sync_client_kwargs={"trust_env": False, "follow_redirects": False},
            )
        elif self.config.llm_provider == LLMProvider.ZHIPU:
            api_key = os.environ.get("ZHIPU_API_KEY")
            if not api_key:
                raise ValueError("ZHIPU_API_KEY 环境变量未设置")
            self.llm = ChatOpenAI(
                model=self.config.llm_model or "glm-4",
                base_url="https://open.bigmodel.cn/api/coding/paas/v4",
                api_key=api_key,
                temperature=self.config.temperature,
                stream_usage=True,  # 启用流式响应中的 token 使用统计
                max_retries=2,
                http_client=self._http_client,
                http_async_client=self._http_async_client,
            )
        else:
            # 默认使用Ollama
            raise ValueError("未配置模型服务，请在智能体配置中选择一个模型服务")

        # 绑定工具到 LLM（兼容旧配置路径）
        self._bind_tools_to_llm()

    def _retire_llm_clients(self) -> None:
        """Close transport pools owned by a replaced legacy model client."""
        llm = self.llm
        self.llm = None
        self.llm_with_tools = None
        if llm is None:
            return
        for attribute in ("_client", "_async_client"):
            client = getattr(llm, attribute, None)
            close = getattr(client, "close", None)
            if close is None:
                continue
            result = close()
            if not inspect.isawaitable(result):
                continue
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
                continue
            task = loop.create_task(result)
            self._client_close_tasks.add(task)

    async def aclose(self) -> None:
        """Drain background work and close all pools owned by this engine."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # A cancelled shutdown caller must not orphan embedding/vector-store
            # work or leave its executor threads alive.
            await asyncio.shield(self._close_task)
            raise

    async def _close_resources(self) -> None:
        self._rag_closed = True

        init_task = self._retriever_init_task
        if init_task is not None and init_task is not asyncio.current_task():
            await asyncio.gather(init_task, return_exceptions=True)

        # A request cancellation does not stop a running Python thread. Futures
        # therefore remain tracked until the underlying call actually exits.
        while self._rag_futures:
            await asyncio.gather(
                *tuple(self._rag_futures),
                return_exceptions=True,
            )

        rag_executor, self._rag_executor = self._rag_executor, None
        if rag_executor is not None:
            await asyncio.to_thread(
                rag_executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
        self._retrievers.clear()

        self._retire_llm_clients()
        if self._client_close_tasks:
            await asyncio.gather(*tuple(self._client_close_tasks), return_exceptions=True)
            self._client_close_tasks.clear()
        async_client, sync_client = self._http_async_client, self._http_client
        self._http_async_client = None
        self._http_client = None
        if async_client is not None:
            await async_client.aclose()
        if sync_client is not None:
            await asyncio.to_thread(sync_client.close)

    def _model_base_url(self) -> str:
        """Return the configured endpoint for just-in-time SSRF validation."""
        if self.config.model_service and self.model_service_registry:
            service = self.model_service_registry.get_service(self.config.model_service)
            if service:
                return service.base_url
        if self.config.llm_provider == LLMProvider.OLLAMA:
            return self.config.llm_base_url or "http://localhost:11434"
        if self.config.llm_provider == LLMProvider.ZHIPU:
            return "https://open.bigmodel.cn/api/coding/paas/v4"
        raise ValueError("模型服务端点不可用")

    async def _invoke_llm(self, llm: Any, messages: Any) -> Any:
        """Re-resolve and validate the model endpoint immediately before I/O."""
        await validate_outbound_url(self._model_base_url())
        response = await llm.ainvoke(messages)
        self._validate_llm_response(response)
        return response

    async def _stream_llm(self, llm: Any, messages: Any):
        """Validate the destination at call time, then proxy model chunks."""
        await validate_outbound_url(self._model_base_url())
        async for chunk in llm.astream(messages):
            yield chunk

    def _validate_llm_response(self, response: Any) -> None:
        """Reject oversized provider responses before they enter prompts/state."""
        content = getattr(response, "content", "")
        if content is not None and len(str(content)) > self.MAX_LLM_RESPONSE_CHARS:
            raise ValueError("模型响应超过 2MB 字符上限")
        tool_calls = getattr(response, "tool_calls", None) or []
        self._validate_complete_tool_calls(tool_calls)

    def _validate_complete_tool_calls(self, tool_calls: Sequence[Any]) -> None:
        if len(tool_calls) > self.MAX_LLM_TOOL_CALLS:
            raise ValueError("模型返回的工具调用数量过多")
        total = 0
        for tool_call in tool_calls:
            if isinstance(tool_call, Mapping):
                arguments = tool_call.get("args", {})
            else:
                arguments = getattr(tool_call, "args", {})
            try:
                total += len(
                    json.dumps(
                        arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=lambda item: f"<{type(item).__name__}>",
                    ).encode("utf-8")
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("模型返回了无法序列化的工具参数") from exc
            if total > self.MAX_LLM_TOOL_ARGUMENT_BYTES:
                raise ValueError("模型返回的工具参数超过 1MB 上限")

    def _bind_tools_to_llm(self):
        """
        将工具绑定到 LLM，启用原生工具调用

        使用 bind_tools() 方法让 LLM 原生支持工具调用，
        而不是依赖文本解析。
        """
        if not self.llm:
            return

        # 检查 LLM 是否支持 bind_tools
        if not hasattr(self.llm, 'bind_tools'):
            print(f"[WARNING] LLM {self.llm.__class__.__name__} 不支持 bind_tools，使用传统文本模式")
            self.llm_with_tools = None
            return

        # 收集所有需要绑定的工具
        tools_to_bind = []

        # 1. MCP 工具
        if self.mcp_manager and self.mcp_manager.all_tools:
            # 初始化适配器（如果还没有）
            if self._tool_adapter is None:
                self._tool_adapter = MCPToolAdapter(self.mcp_manager)

            # 转换 MCP 工具为 LangChain Tool
            mcp_tools = self._tool_adapter.convert_all_tools()
            tools_to_bind.extend(mcp_tools)
            print(f"[DEBUG] 绑定 {len(mcp_tools)} 个 MCP 工具")

        # 2. Skill 工具（load_skill, execute_skill）
        if self.skill_tool and self.skill_tool.enabled_skills:
            skill_tools = self._create_skill_tools()
            tools_to_bind.extend(skill_tools)
            print(f"[DEBUG] 绑定 {len(skill_tools)} 个 Skill 工具")

        # 3. 子Agent工具（Agent-as-a-Tool）
        sub_agent_tools = self._create_sub_agent_tools()
        if sub_agent_tools:
            tools_to_bind.extend(sub_agent_tools)
            print(f"[DEBUG] 绑定 {len(sub_agent_tools)} 个子Agent工具")

        # 4. 【AC130-202603161918】RAG 知识库检索工具
        self._rag_tools = self._create_rag_tools()
        if self._rag_tools:
            tools_to_bind.extend(self._rag_tools)
            print(f"[DEBUG] 绑定 {len(self._rag_tools)} 个 RAG 检索工具")

        if not tools_to_bind:
            print(f"[DEBUG] 没有工具需要绑定")
            self.llm_with_tools = None
            return

        # 绑定工具到 LLM
        try:
            # 根据策略选择 tool_choice 参数
            tool_choice = self._get_tool_choice_param()

            if tool_choice:
                self.llm_with_tools = self.llm.bind_tools(
                    tools_to_bind,
                    tool_choice=tool_choice
                )
                print(f"[DEBUG] LLM 已绑定 {len(tools_to_bind)} 个工具，tool_choice={tool_choice}")
            else:
                self.llm_with_tools = self.llm.bind_tools(tools_to_bind)
                print(f"[DEBUG] LLM 已绑定 {len(tools_to_bind)} 个工具，tool_choice=auto")
        except Exception as e:
            print(
                "[WARNING] bind_tools 失败，使用传统文本模式: "
                f"error_type={type(e).__name__}"
            )
            self.llm_with_tools = None

    def _get_tool_choice_param(self) -> Optional[str]:
        """
        获取 tool_choice 参数

        根据模型类型选择合适的 tool_choice 值
        OpenAI 使用 "required"，其他模型使用 "any"

        Returns:
            tool_choice 参数值或 None
        """
        if self._tool_call_strategy == ToolCallStrategy.AUTO:
            return None

        # 检查模型类型
        model_name = getattr(self.llm, 'model_name', '') or ''
        base_url = getattr(self.llm, 'base_url', '') or ''

        # OpenAI 使用 "required"
        if 'openai.com' in base_url or model_name.startswith('gpt-'):
            return ToolCallStrategy.REQUIRED

        # 其他模型使用 "any"
        return ToolCallStrategy.ANY

    def set_tool_call_strategy(self, strategy: str):
        """
        设置工具调用策略

        Args:
            strategy: 策略值 ("auto", "any", "required")
        """
        if strategy in (ToolCallStrategy.AUTO, ToolCallStrategy.ANY, ToolCallStrategy.REQUIRED):
            self._tool_call_strategy = strategy
            # 重新绑定工具
            self._bind_tools_to_llm()
            self._llm_fingerprint = self._current_llm_fingerprint()
        else:
            raise ValueError(f"无效的工具调用策略: {strategy}")

    def _create_skill_tools(self) -> List:
        """
        创建 Skill 相关的 LangChain 工具

        Returns:
            LangChain Tool 列表
        """
        from langchain_core.tools import tool

        tools = []

        @tool
        def load_skill(skill_name: str) -> str:
            """加载技能内容以获取详细指导。

            当用户询问与技能相关的问题时，使用此工具加载技能内容。

            Args:
                skill_name: 要加载的技能名称
            """
            # 这个方法会被 LLM 调用，实际执行在 _execute_tool 中处理
            return f"[SKILL_LOAD] {skill_name}"

        @tool
        def execute_skill(skill_name: str, arguments: List[str] = [], input_file_ids: List[str] = []) -> str:
            """执行技能脚本来处理上传的文件或执行特定任务。

            用于处理 PDF、DOCX 等文件时使用此工具。

            Args:
                skill_name: 要执行的技能名称
                arguments: 传递给脚本的参数列表
                input_file_ids: 要处理的文件 ID 列表
            """
            return f"[SKILL_EXECUTE] {skill_name}"

        tools.extend([load_skill, execute_skill])
        return tools

    # ========================================================================
    # 【AC130-202603142210】Agent-as-a-Tool: 子Agent工具创建
    # ========================================================================

    def _create_sub_agent_tools(self) -> List:
        """
        创建子Agent相关的LangChain工具

        Returns:
            LangChain Tool 列表
        """
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field

        tools = []
        sub_agents = self.config.sub_agents or []

        if not sub_agents:
            return tools

        # 为每个子Agent创建一个工具
        for sub_agent_name in sub_agents:
            # 获取子Agent配置以生成描述
            sub_config = self.agent_manager.get_config(sub_agent_name) if self.agent_manager else None
            sub_persona = sub_config.persona[:100] if sub_config else "专业助手"

            # 创建工具名称（有效标识符）
            safe_name = sub_agent_name.lower().replace('-', '_').replace(' ', '_')
            tool_name = f"call_agent_{safe_name}"

            # 创建工具描述
            tool_description = f"""调用子Agent '{sub_agent_name}'来处理特定任务。

子Agent人设: {sub_persona}

当用户请求需要'{sub_agent_name}'的专业能力时使用此工具。

Args:
    message: 发送给子Agent的消息内容

Returns:
    子Agent的响应结果
"""

            # 创建输入 schema（使用字典形式避免类定义问题）
            args_schema = {
                "title": f"{tool_name}_schema",
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "发送给子Agent的消息内容"
                    }
                },
                "required": ["message"]
            }

            # 使用工厂函数创建独立的闭包
            def make_sub_agent_tool(name: str, desc: str, schema: dict):
                def _run(message: str) -> str:
                    return f"[SUB_AGENT_CALL] {name}|{message}"

                async def _arun(message: str) -> str:
                    return f"[SUB_AGENT_CALL] {name}|{message}"

                # 使用 StructuredTool 创建工具
                return StructuredTool(
                    name=name,
                    description=desc,
                    func=_run,
                    coroutine=_arun,
                    args_schema=schema
                )

            sub_agent_tool = make_sub_agent_tool(tool_name, tool_description, args_schema)
            tools.append(sub_agent_tool)

        return tools

    # ========================================================================
    # 【AC130-202603161918】RAG 知识库检索工具创建
    # ========================================================================

    def _create_rag_tools(self) -> List:
        """
        创建RAG知识库检索相关的LangChain工具

        Returns:
            LangChain Tool 列表
        """
        from langchain_core.tools import StructuredTool

        tools = []

        # 只有配置了知识库时才创建工具
        if not self.config.knowledge_bases:
            return tools

        # 构建知识库描述信息
        kb_descriptions = []
        if self.kb_manager:
            for kb_id in self.config.knowledge_bases:
                kb = self.kb_manager.get_kb(kb_id)
                if kb:
                    kb_descriptions.append(f"- **{kb.name}**: {kb.description or '无描述'}")

        kb_list_text = "\n".join(kb_descriptions) if kb_descriptions else "无可用知识库"

        # 创建 rag_retrieve 工具
        tool_description = f"""从挂载的知识库中检索相关文档内容。

**可用知识库**：
{kb_list_text}

**使用指南**：
1. 仅在需要查询内部文档、公司制度、产品手册等信息时调用
2. 调用后会返回相关文档片段，请在回答中标注引用来源
3. 如果知识库中没有相关信息，请明确告知用户

Args:
    query: 检索查询语句（问题关键词）
    top_k: 返回结果数量，默认3个，最多5个

Returns:
    检索到的相关文档片段及来源信息
"""

        def _run_retrieve(query: str, top_k: int = 3) -> str:
            return f"[RAG_RETRIEVE] {query}|{top_k}"

        async def _arun_retrieve(query: str, top_k: int = 3) -> str:
            return f"[RAG_RETRIEVE] {query}|{top_k}"

        # 创建输入 schema
        args_schema = {
            "title": "rag_retrieve_schema",
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索查询语句（问题关键词或完整问题）"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量（1-5），默认3"
                }
            },
            "required": ["query"]
        }

        rag_tool = StructuredTool(
            name="rag_retrieve",
            description=tool_description,
            func=_run_retrieve,
            coroutine=_arun_retrieve,
            args_schema=args_schema
        )

        tools.append(rag_tool)
        return tools

    async def _execute_sub_agent(
        self,
        sub_agent_name: str,
        message: str,
        call_stack: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        执行子Agent调用

        Args:
            sub_agent_name: 子Agent名称
            message: 发送给子Agent的消息
            call_stack: 当前调用栈（用于循环检测）

        Returns:
            包含执行结果的字典 {success, result, error}
        """
        if not self.agent_manager:
            return {
                "success": False,
                "error": "AgentManager未初始化，无法调用子Agent"
            }

        # 初始化调用栈
        if call_stack is None:
            call_stack = list(self._call_stack_context.get())
        current_stack = call_stack.copy()

        # 运行时循环检测
        if sub_agent_name in current_stack:
            cycle_path = current_stack + [sub_agent_name]
            return {
                "success": False,
                "error": f"运行时循环检测: {' -> '.join(cycle_path)}"
            }

        # 检查子Agent是否存在
        sub_agent_instance = await self.agent_manager.get_instance(sub_agent_name)
        if not sub_agent_instance:
            return {
                "success": False,
                "error": f"子Agent '{sub_agent_name}' 不存在"
            }

        # 检查子Agent引擎是否已初始化
        if not sub_agent_instance.engine:
            try:
                await sub_agent_instance.initialize()
            except Exception as e:
                return {
                    "success": False,
                    "error": (
                        f"子Agent '{sub_agent_name}' 初始化失败 "
                        f"({type(e).__name__})"
                    )
                }

        # 初始化并发控制信号量
        if self._sub_agent_semaphore is None:
            max_concurrent = getattr(self.config, 'sub_agent_max_concurrent', 3)
            self._sub_agent_semaphore = asyncio.Semaphore(max_concurrent)

        # 执行子Agent（带超时和并发控制）
        timeout = getattr(self.config, 'sub_agent_timeout', 60)
        max_retries = getattr(self.config, 'sub_agent_max_retries', 1)

        for attempt in range(max_retries + 1):
            try:
                async with self._sub_agent_semaphore:
                    # 使用 asyncio.wait_for 实现超时控制
                    result = await asyncio.wait_for(
                        self._run_sub_agent_with_stack(
                            sub_agent_instance,
                            message,
                            current_stack + [self.config.name]
                        ),
                        timeout=timeout
                    )

                return {
                    "success": True,
                    "result": result
                }

            except asyncio.TimeoutError:
                if attempt < max_retries:
                    continue
                return {
                    "success": False,
                    "error": f"子Agent '{sub_agent_name}' 调用超时（{timeout}秒）"
                }
            except Exception as e:
                if attempt < max_retries:
                    continue
                return {
                    "success": False,
                    "error": (
                        f"子Agent '{sub_agent_name}' 调用失败 "
                        f"({type(e).__name__})"
                    )
                }

        return {
            "success": False,
            "error": f"子Agent '{sub_agent_name}' 调用失败（超过最大重试次数）"
        }

    async def _run_sub_agent_with_stack(
        self,
        sub_agent_instance: "AgentInstance",
        message: str,
        call_stack: List[str]
    ) -> str:
        """
        运行子Agent并设置调用栈

        Args:
            sub_agent_instance: 子Agent实例
            message: 发送给子Agent的消息
            call_stack: 调用栈

        Returns:
            子Agent的响应结果
        """
        # AgentInstance serializes access to its engine and installs the stack
        # in a request-local ContextVar, so concurrent parent requests cannot
        # overwrite one another's cycle-detection state.
        return await sub_agent_instance.run_with_call_stack(message, call_stack)

    def get_sub_agent_names(self) -> List[str]:
        """获取当前Agent配置的所有子Agent名称"""
        return getattr(self.config, 'sub_agents', []) or []

    def set_request_call_stack(self, call_stack: Sequence[str]) -> Token:
        """Install a cycle-detection stack for the current async request."""
        return self._call_stack_context.set(tuple(str(item) for item in call_stack))

    def reset_request_call_stack(self, token: Token) -> None:
        self._call_stack_context.reset(token)

    def _get_cycle_detector(self, all_agent_names: List[str]) -> CycleDetector:
        """获取或创建循环检测器"""
        if self._cycle_detector is None:
            self._cycle_detector = CycleDetector(all_agent_names)
        return self._cycle_detector

    # ========================================================================
    # 【AC130-202603161542】RAG 知识库检索方法
    # ========================================================================

    async def _run_rag_blocking(self, function, *args, **kwargs):
        """Run one embedding/vector-store operation in the bounded RAG pool."""
        if self._rag_closed:
            raise RuntimeError("AgentEngine 已关闭")

        semaphore = self._rag_worker_semaphore
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.RAG_MAX_WORKERS)
            self._rag_worker_semaphore = semaphore

        await semaphore.acquire()
        if self._rag_closed:
            semaphore.release()
            raise RuntimeError("AgentEngine 已关闭")

        executor = self._rag_executor
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=self.RAG_MAX_WORKERS,
                thread_name_prefix="agent-rag",
            )
            self._rag_executor = executor

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(executor, partial(function, *args, **kwargs))
        self._rag_futures.add(future)

        def release_worker(completed: asyncio.Future) -> None:
            self._rag_futures.discard(completed)
            semaphore.release()

        future.add_done_callback(release_worker)
        # Shielding is important: cancelling the request cannot cancel a Python
        # thread, so the future must stay alive and retain its semaphore slot.
        return await asyncio.shield(future)

    def _build_retrievers(self) -> Dict[str, Any]:
        """Open configured Chroma collections outside the event-loop thread."""
        from src.retriever import Retriever

        retrievers: Dict[str, Any] = {}
        configured = getattr(self.kb_manager, "_configs", {})
        for kb_id in dict.fromkeys(self.config.knowledge_bases or []):
            if kb_id in configured:
                collection = self.kb_manager._get_collection(kb_id)
                retrievers[kb_id] = Retriever(collection, self.embedder)
        return retrievers

    async def _initialize_retrievers(self) -> None:
        try:
            retrievers = await self._run_rag_blocking(self._build_retrievers)
            if not self._rag_closed:
                self._retrievers = retrievers
        except Exception as exc:
            print(
                "[ERROR] 初始化检索器失败: "
                f"error_type={type(exc).__name__}"
            )
        finally:
            self._retrievers_initialized = True

    async def _ensure_retrievers_initialized(self) -> None:
        """Initialize retrievers once without tying the work to one request."""
        if self._retrievers_initialized or self._rag_closed:
            return
        if self._retriever_init_task is None:
            self._retriever_init_task = asyncio.create_task(
                self._initialize_retrievers()
            )
        await asyncio.shield(self._retriever_init_task)

    async def _search_retrievers(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: Optional[float] = None,
    ) -> List[Any]:
        """Search configured knowledge bases concurrently within the worker cap."""
        await self._ensure_retrievers_initialized()
        searches: List[Tuple[str, asyncio.Task]] = []
        for kb_id in dict.fromkeys(self.config.knowledge_bases or []):
            retriever = self._retrievers.get(kb_id)
            if retriever is None:
                continue
            search_kwargs: Dict[str, Any] = {"top_k": top_k}
            if score_threshold is not None:
                search_kwargs["score_threshold"] = score_threshold
            searches.append(
                (
                    kb_id,
                    asyncio.create_task(
                        self._run_rag_blocking(
                            retriever.search,
                            query,
                            **search_kwargs,
                        )
                    ),
                )
            )

        if not searches:
            return []

        outcomes = await asyncio.gather(
            *(task for _, task in searches),
            return_exceptions=True,
        )
        all_results: List[Any] = []
        for (kb_id, _), outcome in zip(searches, outcomes):
            if isinstance(outcome, BaseException):
                print(
                    f"[ERROR] 检索失败 (kb_id={kb_id}): "
                    f"error_type={type(outcome).__name__}"
                )
                continue
            all_results.extend(outcome)
        return all_results

    async def _retrieve_for_query(self, query: str, trace_id: str = None) -> str:
        """为查询检索知识库内容

        Args:
            query: 用户查询
            trace_id: 可观测性追踪 ID（可选）

        Returns:
            str: 格式化的检索结果，如果无结果返回空字符串
        """
        observability_tracer = get_tracer()

        if not self.config.knowledge_bases:
            self._last_retrieval_sources = []
            self._retrieval_sources_context.set(())
            return ""

        await self._ensure_retrievers_initialized()
        if not self._retrievers:
            self._last_retrieval_sources = []
            self._retrieval_sources_context.set(())
            return ""

        # 使用配置或默认值
        config = self.config.retrieval_config
        if not config:
            # 【BUG FIX】当 retrieval_config 未配置时，使用默认值而不是跳过检索
            from src.models import RetrievalConfig
            config = RetrievalConfig()  # 使用默认值: top_k=3, score_threshold=0.6

        all_results = []
        rag_retrieve_span_id = None

        # ====================================================================
        # 【可观测性追踪】RAG 检索阶段 (Embedding + Vector Search)
        # ====================================================================
        if is_observability_enabled() and trace_id:
            rag_retrieve_result = observability_tracer.create_span(
                trace_id=trace_id,
                span_name="rag.retrieve",
                span_type="RETRIEVER",
                input=summarize_tool_arguments({
                    "query": query,
                    "knowledge_bases": self.config.knowledge_bases,
                    "top_k": config.top_k,
                    "score_threshold": config.score_threshold,
                })
            )
            rag_retrieve_span_id = rag_retrieve_result[0] if isinstance(rag_retrieve_result, tuple) else None

        all_results = await self._search_retrievers(
            query,
            top_k=config.top_k,
            score_threshold=config.score_threshold,
        )

        # 结束 RAG 检索阶段
        if rag_retrieve_span_id and is_observability_enabled():
            observability_tracer.end_span(
                trace_id=trace_id,
                span_id=rag_retrieve_span_id,
                output={
                    "raw_results_count": len(all_results),
                }
            )

        if not all_results:
            self._last_retrieval_sources = []
            self._retrieval_sources_context.set(())
            return ""

        # ====================================================================
        # 【可观测性追踪】RAG 重排阶段 (Sorting + Filtering)
        # ====================================================================
        rag_rerank_span_id = None
        if is_observability_enabled() and trace_id:
            rag_rerank_result = observability_tracer.create_span(
                trace_id=trace_id,
                span_name="rag.rerank",
                span_type="DEFAULT",
                input={
                    "results_count": len(all_results)
                }
            )
            rag_rerank_span_id = rag_rerank_result[0] if isinstance(rag_rerank_result, tuple) else None

        # 按相似度排序，取 Top-K
        all_results.sort(key=lambda x: x.score, reverse=True)
        top_results = all_results[:config.top_k]

        # 结束 RAG 重排阶段
        if rag_rerank_span_id and is_observability_enabled():
            observability_tracer.end_span(
                trace_id=trace_id,
                span_id=rag_rerank_span_id,
                output={
                    "top_results_count": len(top_results),
                }
            )

        # Track sources for citation
        self._last_retrieval_sources = [
            {
                "filename": r.filename,
                "chunk_index": r.chunk_index,
                "score": round(r.score, 2)
            }
            for r in top_results
        ]
        self._retrieval_sources_context.set(
            tuple(dict(source) for source in self._last_retrieval_sources)
        )

        # ====================================================================
        # 【可观测性追踪】RAG 格式化阶段 (Context Generation)
        # ====================================================================
        rag_format_span_id = None
        if is_observability_enabled() and trace_id:
            rag_format_result = observability_tracer.create_span(
                trace_id=trace_id,
                span_name="rag.format",
                span_type="DEFAULT",
                input={
                    "results_count": len(top_results)
                }
            )
            rag_format_span_id = rag_format_result[0] if isinstance(rag_format_result, tuple) else None

        # 格式化为上下文
        context = self._format_retrieved_context(top_results)

        # 结束 RAG 格式化阶段
        if rag_format_span_id and is_observability_enabled():
            observability_tracer.end_span(
                trace_id=trace_id,
                span_id=rag_format_span_id,
                output={
                    "context_length": len(context),
                }
            )

        return context

    def _format_retrieved_context(self, results: List[Any]) -> str:
        """格式化检索结果为上下文字符串

        Args:
            results: RetrievalResult 列表

        Returns:
            str: 格式化的上下文
        """
        if not results:
            return ""

        chunks = []
        for i, r in enumerate(results, 1):
            chunks.append(
                f"[{i}] {r.content}\n"
                f"    来源: {r.filename} (相关度: {r.score:.2f})"
            )

        return "\n".join(chunks)

    def get_last_retrieval_sources(self) -> List[Dict[str, Any]]:
        """Get sources from last retrieval for citation display"""
        return [dict(source) for source in self._retrieval_sources_context.get()]

    def _get_system_prompt(self) -> str:
        """获取系统提示词"""
        base_prompt = self.config.persona

        # ========================================
        # 【AC130-202603141800 P0 修复】
        # 添加负向约束和工具匹配规则
        # 目的：提升 MCP/Skill 工具调用优先级
        # ========================================

        # 构建 MCP 工具名称列表，用于匹配规则
        mcp_tool_names = []
        if self.mcp_manager and self.mcp_manager.all_tools:
            mcp_tool_names = [tool.name for tool in self.mcp_manager.all_tools]

        # 检测可用工具类型
        has_calculator = any(name in mcp_tool_names for name in ['evaluate', 'add', 'subtract', 'multiply', 'divide', 'power', 'sqrt'])
        has_jokes = any(name in mcp_tool_names for name in ['get_joke', 'list_categories'])
        has_coingecko = any(name in mcp_tool_names for name in ['get_coin_price', 'get_market_data'])
        has_skills = self.skill_tool and self.skill_tool.enabled_skills

        # 构建工具匹配规则表
        tool_matching_rules = []
        if has_calculator:
            tool_matching_rules.append("| 计算、加、减、乘、除、等于、数学 | evaluate |")
        if has_jokes:
            tool_matching_rules.append("| 笑话、幽默、搞笑、逗我 | get_joke |")
        if has_coingecko:
            tool_matching_rules.append("| 比特币、BTC、价格、市值、加密货币 | get_coin_price |")
        if has_skills:
            tool_matching_rules.append("| PDF、DOCX、文档、读取 | load_skill + execute_skill |")

        # 工具匹配规则区块
        tool_matching_block = ""
        if tool_matching_rules:
            tool_matching_block = f"""

## 工具匹配规则

| 用户问题关键词 | 必须调用的工具 |
|--------------|--------------|
{chr(10).join(tool_matching_rules)}

"""

        # 负向约束区块（精简版）
        negative_constraints = """

## 禁止行为

1. **禁止数学计算**：不要自己进行任何数学运算，必须使用计算工具
2. **禁止编造内容**：不要编造笑话、价格数据等需要工具验证的信息

"""

        # 组合增强后的 system prompt
        base_prompt += negative_constraints + tool_matching_block

        # 按需加载模式：不再静态注入所有skills内容
        # 改为提供简要提示，让LLM通过load_skill工具按需加载
        if has_skills:
            skills_hint = f"""

## 可用技能 (Skills)

你可以使用 `load_skill` 工具按需加载以下技能的详细指导：
{chr(10).join([f"- {name}" for name in self.skill_tool.enabled_skills])}

**重要**：当处理与这些技能相关的任务时，请先调用 `load_skill` 工具加载对应的技能内容，然后按照技能指导执行任务。
"""
            base_prompt += skills_hint

        return base_prompt

    # ==================== 通用工具调用 ====================

    async def _execute_tool(self, tool_name: str, tool_args: Dict, trace_id: Optional[str] = None) -> str:
        """执行工具调用"""
        # ========================================
        # 【AC130-202603142000 TC-001 修复】展开 kwargs 参数
        # ========================================
        # 当 LLM 使用动态 schema 时，参数可能被包装为 {'kwargs': {...}}
        # 需要展开 kwargs 的内容作为实际参数传递给 MCP
        actual_args = tool_args
        if len(tool_args) == 1 and 'kwargs' in tool_args:
            kwargs_value = tool_args['kwargs']
            if isinstance(kwargs_value, dict):
                actual_args = kwargs_value
                print(
                    "[DEBUG] [agent_engine] 展开动态 schema 参数: "
                    f"{summarize_tool_arguments(actual_args)}"
                )

        # 优先处理 skill 工具（按需加载）
        if tool_name == SkillTool.TOOL_NAME and self.skill_tool:
            # 支持两种参数名：skill_name（规范）和 skill（兼容）
            skill_name = tool_args.get("skill_name") or tool_args.get("skill", "")
            list_files = tool_args.get("list_files", False)
            return await self.skill_tool.execute(skill_name, list_files)

        # 处理 execute_skill 工具（脚本执行）
        if tool_name == SkillTool.EXECUTE_TOOL_NAME and self.skill_tool:
            skill_name = tool_args.get("skill_name", "")
            script_name = tool_args.get("script_name", "main.py")
            arguments = tool_args.get("arguments", [])
            input_file_ids = tool_args.get("input_file_ids", [])
            timeout = tool_args.get("timeout", 60)
            return await self.skill_tool.execute_script(
                skill_name=skill_name,
                script_name=script_name,
                arguments=arguments,
                input_file_ids=input_file_ids,
                timeout=timeout
            )

        # ====================================================================
        # 【AC130-202603161918】处理RAG知识库检索工具
        # ====================================================================
        if tool_name == "rag_retrieve":
            query = tool_args.get("query", "")
            top_k = tool_args.get("top_k", 3)
            # 限制 top_k 范围
            top_k = max(1, min(5, int(top_k))) if isinstance(top_k, (int, str)) else 3

            if not query:
                return "错误: 检索查询不能为空"

            await self._ensure_retrievers_initialized()
            if not self._retrievers:
                return "错误: 知识库检索器未初始化"

            # 【可观测性追踪】创建 RAG 工具调用 Span
            rag_tool_span_id = None
            if is_observability_enabled() and trace_id:
                observability_tracer = get_tracer()
                rag_tool_result = observability_tracer.create_span(
                    trace_id=trace_id,
                    span_name="tool.rag_retrieve",
                    span_type="TOOL",
                    input=summarize_tool_arguments({"query": query, "top_k": top_k})
                )
                rag_tool_span_id = rag_tool_result[0] if isinstance(rag_tool_result, tuple) else None

            try:
                all_results = await self._search_retrievers(query, top_k=top_k)

                if not all_results:
                    self._last_retrieval_sources = []
                    self._retrieval_sources_context.set(())
                    result = f"未找到与 '{query}' 相关的文档内容。请尝试其他关键词或告知用户该问题不在知识库范围内。"
                else:
                    # 按相似度排序，取 Top-K
                    all_results.sort(key=lambda x: x.score, reverse=True)
                    top_results = all_results[:top_k]
                    sources = [
                        {
                            "filename": result_item.filename,
                            "chunk_index": result_item.chunk_index,
                            "score": round(result_item.score, 2),
                        }
                        for result_item in top_results
                    ]
                    self._last_retrieval_sources = sources
                    self._retrieval_sources_context.set(
                        tuple(dict(source) for source in sources)
                    )

                    # 格式化结果
                    formatted_parts = []
                    for i, r in enumerate(top_results, 1):
                        formatted_parts.append(
                            f"[{i}] {r.content}\n"
                            f"    来源: {r.filename} (相关度: {r.score:.2%})"
                        )

                    result = "检索到以下相关内容：\n\n" + "\n".join(formatted_parts)

                # 【可观测性追踪】结束 Span
                if rag_tool_span_id and is_observability_enabled():
                    observability_tracer.end_span(
                        trace_id=trace_id,
                        span_id=rag_tool_span_id,
                        output={
                            "result_length": len(result),
                            "results_count": len(all_results),
                        }
                    )

                return result

            except Exception as e:
                # 【可观测性追踪】错误处理
                if rag_tool_span_id and is_observability_enabled():
                    observability_tracer.end_span(
                        trace_id=trace_id,
                        span_id=rag_tool_span_id,
                        output={"error_type": type(e).__name__},
                        status="error"
                    )
                return f"检索失败 ({type(e).__name__})"

        # ====================================================================
        # 【AC130-202603142210】处理子Agent调用
        # ====================================================================
        if tool_name.startswith("call_agent_"):
            # 提取子Agent名称（从 call_agent_xxx 中提取 xxx）
            # 需要反转之前创建工具时的名称转换
            sub_agent_name = None
            for candidate in self.get_sub_agent_names():
                expected_name = f"call_agent_{candidate.lower().replace('-', '_').replace(' ', '_')}"
                if tool_name == expected_name:
                    sub_agent_name = candidate
                    break

            if sub_agent_name:
                message = tool_args.get("message", "")
                result = await self._execute_sub_agent(
                    sub_agent_name,
                    message,
                    list(self._call_stack_context.get()),
                )
                if result["success"]:
                    return result["result"]
                else:
                    return f"子Agent调用失败: {result['error']}"
            else:
                return f"错误: 找不到子Agent '{tool_name}'"

        if not self.mcp_manager:
            return "错误: MCP管理器未初始化"

        # 检查工具是否存在
        tool = self.mcp_manager.get_tool(tool_name)
        if not tool:
            available_tools = [t.name for t in self.mcp_manager.all_tools]
            print(f"[TOOL] 工具调用失败: 找不到工具 '{tool_name}'，可用工具: {available_tools}")
            return f"错误: 找不到工具 '{tool_name}'。可用工具: {available_tools}"

        try:
            import time
            timestamp = time.strftime("%H:%M:%S", time.localtime())
            print(
                f"[TOOL] {timestamp} 调用工具: {tool_name}, "
                f"{summarize_tool_arguments(actual_args)}"
            )
            result = await self.mcp_manager.call_tool(tool_name, actual_args)
            print(
                f"[TOOL] {timestamp} 工具返回: "
                f"result_length={tool_result_length(result)}"
            )
            return result
        except Exception as e:
            print(f"[TOOL] 工具调用异常: error_type={type(e).__name__}")
            return f"工具调用异常 ({type(e).__name__})"

    def _parse_tool_calls(self, response) -> List[Dict]:
        """解析工具调用"""
        tool_calls = []
        if hasattr(response, 'tool_calls') and response.tool_calls:
            tool_calls = response.tool_calls
        elif self.mcp_manager and self.mcp_manager.all_tools:
            content = response.content
            for tool in self.mcp_manager.all_tools:
                if f"[CALL:{tool.name}]" in content or f"调用工具: {tool.name}" in content:
                    tool_calls.append({
                        "name": tool.name,
                        "args": {},
                        "id": f"call_{tool.name}"
                    })
        return tool_calls

    # ==================== ReAct 模式 ====================

    async def _react_agent_node(self, state: AgentState) -> Dict[str, Any]:
        """ReAct Agent节点 - 思考并决定下一步"""
        messages = list(state["messages"])
        iterations = state.get("iterations", 0)

        if iterations == 0:
            system_prompt = self._get_system_prompt()
            if self.mcp_manager and self.mcp_manager.all_tools:
                tools_desc = "\n".join([
                    f"- {tool.name}: {tool.description}"
                    for tool in self.mcp_manager.all_tools
                ])
                system_prompt += f"\n\n你可以使用以下工具:\n{tools_desc}"
            messages = [SystemMessage(content=system_prompt)] + messages

        response = await self._invoke_llm(self.llm, messages)
        return {"messages": [response], "iterations": iterations + 1}

    async def _react_tool_node(self, state: AgentState) -> Dict[str, Any]:
        """ReAct 工具节点"""
        messages = state["messages"]
        last_message = messages[-1]
        tool_messages = []

        tool_calls = self._parse_tool_calls(last_message)
        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call.get("args", {})
            result = await self._execute_tool(tool_name, tool_args, trace_id=None)
            tool_messages.append(
                ToolMessage(content=result, tool_call_id=tool_call.get("id", "unknown"))
            )

        return {"messages": tool_messages}

    def _react_should_continue(self, state: AgentState) -> str:
        """ReAct 判断是否继续"""
        iterations = state.get("iterations", 0)
        if iterations >= self.config.max_iterations:
            return "end"

        last_message = state["messages"][-1] if state["messages"] else None
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"
        if isinstance(last_message, ToolMessage):
            return "agent"
        return "end"

    def _build_react_graph(self) -> StateGraph:
        """构建 ReAct 图"""
        workflow = StateGraph(AgentState)
        workflow.add_node("agent", self._react_agent_node)
        workflow.add_node("tools", self._react_tool_node)
        workflow.set_entry_point("agent")
        workflow.add_conditional_edges("agent", self._react_should_continue,
            {"tools": "tools", "agent": "agent", "end": END})
        workflow.add_edge("tools", "agent")
        return workflow

    # ==================== Reflexion 模式 ====================

    async def _reflexion_agent_node(self, state: AgentState) -> Dict[str, Any]:
        """Reflexion Agent节点"""
        messages = list(state["messages"])
        iterations = state.get("iterations", 0)

        if iterations == 0:
            system_prompt = self._get_system_prompt()
            if self.mcp_manager and self.mcp_manager.all_tools:
                tools_desc = "\n".join([
                    f"- {tool.name}: {tool.description}"
                    for tool in self.mcp_manager.all_tools
                ])
                system_prompt += f"\n\n你可以使用以下工具:\n{tools_desc}"
            messages = [SystemMessage(content=system_prompt)] + messages

        # 如果有反思结果，添加到消息中
        reflection = state.get("reflection")
        if reflection and not state.get("is_satisfactory", True):
            messages.append(SystemMessage(content=f"之前的回答需要改进。反思: {reflection}\n请根据反思改进你的回答。"))

        response = await self._invoke_llm(self.llm, messages)
        return {"messages": [response], "iterations": iterations + 1}

    async def _reflexion_reflect_node(self, state: AgentState) -> Dict[str, Any]:
        """Reflexion 反思节点"""
        messages = state["messages"]
        last_ai_msg = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and not hasattr(msg, 'tool_calls'):
                last_ai_msg = msg
                break

        if not last_ai_msg:
            return {"is_satisfactory": True, "reflection": None}

        # 让 LLM 评估回答质量
        reflect_prompt = f"""请评估以下回答的质量，并判断是否需要改进。

用户问题: {messages[0].content if messages else '未知'}
回答: {last_ai_msg.content}

请回答:
1. 这个回答是否完整、准确、有帮助？(是/否)
2. 如果需要改进，请说明需要改进的地方。

格式:
SATISFACTORY: 是/否
REFLECTION: 改进建议（如果需要）"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=reflect_prompt)])
        reflection_text = response.content

        is_satisfactory = "SATISFACTORY: 是" in reflection_text or "SATISFACTORY:是" in reflection_text
        reflection = reflection_text.split("REFLECTION:")[-1].strip() if "REFLECTION:" in reflection_text else None

        return {"is_satisfactory": is_satisfactory, "reflection": reflection}

    def _reflexion_should_continue(self, state: AgentState) -> str:
        """Reflexion 判断是否继续"""
        iterations = state.get("iterations", 0)
        if iterations >= self.config.max_iterations:
            return "end"

        last_message = state["messages"][-1] if state["messages"] else None

        # 如果有工具调用，执行工具
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"

        # 如果是工具消息，继续思考
        if isinstance(last_message, ToolMessage):
            return "agent"

        # 如果是 AI 消息且没有工具调用，进入反思
        if isinstance(last_message, AIMessage):
            return "reflect"

        return "end"

    def _reflexion_after_reflect(self, state: AgentState) -> str:
        """反思后判断"""
        if state.get("is_satisfactory", True):
            return "end"
        iterations = state.get("iterations", 0)
        if iterations >= self.config.max_iterations:
            return "end"
        return "agent"

    def _build_reflexion_graph(self) -> StateGraph:
        """构建 Reflexion 图"""
        workflow = StateGraph(AgentState)
        workflow.add_node("agent", self._reflexion_agent_node)
        workflow.add_node("tools", self._react_tool_node)
        workflow.add_node("reflect", self._reflexion_reflect_node)
        workflow.set_entry_point("agent")
        workflow.add_conditional_edges("agent", self._reflexion_should_continue,
            {"tools": "tools", "reflect": "reflect", "end": END})
        workflow.add_edge("tools", "agent")
        workflow.add_conditional_edges("reflect", self._reflexion_after_reflect,
            {"agent": "agent", "end": END})
        return workflow

    # ==================== Plan & Solve 模式 ====================

    async def _plan_and_solve_planner_node(self, state: AgentState) -> Dict[str, Any]:
        """Plan & Solve 规划节点"""
        user_input = state["messages"][-1].content if state["messages"] else ""

        tools_desc = ""
        if self.mcp_manager and self.mcp_manager.all_tools:
            tools_desc = "\n可用工具:\n" + "\n".join([
                f"- {tool.name}: {tool.description}"
                for tool in self.mcp_manager.all_tools
            ])

        planner_prompt = f"""{self._get_system_prompt()}

你是一个任务规划专家。请为用户的请求制定一个详细的执行计划。

用户请求: {user_input}
{tools_desc}

请制定一个清晰的步骤计划，每步一行，格式如下:
1. [步骤描述]
2. [步骤描述]
...

只输出计划步骤，不要其他内容。"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=planner_prompt)])
        plan_text = response.content

        # 解析计划
        plan = []
        for line in plan_text.strip().split('\n'):
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith('-') or line.startswith('*')):
                # 移除序号
                step = line.lstrip('0123456789.-* ').strip()
                if step:
                    plan.append(step)

        return {"plan": plan, "current_step": 0, "messages": [AIMessage(content=f"执行计划:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(plan)))]}

    async def _plan_and_solve_executor_node(self, state: AgentState) -> Dict[str, Any]:
        """Plan & Solve 执行节点"""
        plan = state.get("plan", [])
        current_step = state.get("current_step", 0)
        messages = list(state["messages"])

        if current_step >= len(plan):
            return {"iterations": state.get("iterations", 0) + 1}

        step = plan[current_step]
        tools_desc = ""
        if self.mcp_manager and self.mcp_manager.all_tools:
            tools_desc = "\n可用工具:\n" + "\n".join([
                f"- {tool.name}: {tool.description}"
                for tool in self.mcp_manager.all_tools
            ])

        executor_prompt = f"""{self._get_system_prompt()}

当前执行计划:
{chr(10).join(f"{i+1}. {s}" for i, s in enumerate(plan))}

现在执行步骤 {current_step + 1}: {step}

{tools_desc}

请执行这个步骤。如果需要使用工具，请调用工具。完成后给出这个步骤的执行结果。"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=executor_prompt)])

        # 执行可能的工具调用
        tool_calls = self._parse_tool_calls(response)
        tool_results = []
        for tool_call in tool_calls:
            result = await self._execute_tool(tool_call["name"], tool_call.get("args", {}), trace_id=None)
            tool_results.append(f"工具 {tool_call['name']} 结果: {result}")

        if tool_results:
            # 将工具结果反馈给 LLM
            follow_up = await self._invoke_llm(self.llm, [
                SystemMessage(content=executor_prompt),
                response,
                SystemMessage(content=f"工具执行结果:\n{chr(10).join(tool_results)}\n\n请总结这个步骤的执行结果。")
            ])
            return {
                "messages": [response, follow_up],
                "current_step": current_step + 1,
                "iterations": state.get("iterations", 0) + 1
            }

        return {
            "messages": [response],
            "current_step": current_step + 1,
            "iterations": state.get("iterations", 0) + 1
        }

    def _plan_and_solve_should_continue(self, state: AgentState) -> str:
        """Plan & Solve 判断是否继续"""
        plan = state.get("plan", [])
        current_step = state.get("current_step", 0)
        iterations = state.get("iterations", 0)

        if iterations >= self.config.max_iterations:
            return "end"
        if current_step >= len(plan):
            return "end"
        return "execute"

    def _build_plan_and_solve_graph(self) -> StateGraph:
        """构建 Plan & Solve 图"""
        workflow = StateGraph(AgentState)
        workflow.add_node("planner", self._plan_and_solve_planner_node)
        workflow.add_node("execute", self._plan_and_solve_executor_node)
        workflow.set_entry_point("planner")
        workflow.add_edge("planner", "execute")
        workflow.add_conditional_edges("execute", self._plan_and_solve_should_continue,
            {"execute": "execute", "end": END})
        return workflow

    # ==================== ReWOO 模式 ====================

    async def _rewOO_planner_node(self, state: AgentState) -> Dict[str, Any]:
        """ReWOO 规划节点 - 一次性规划所有工具调用"""
        user_input = state["messages"][-1].content if state["messages"] else ""

        tools_desc = ""
        if self.mcp_manager and self.mcp_manager.all_tools:
            tools_desc = "\n可用工具:\n" + "\n".join([
                f"- {tool.name}: {tool.description}"
                for tool in self.mcp_manager.all_tools
            ])

        planner_prompt = f"""{self._get_system_prompt()}

你是一个任务规划专家。请为用户的请求制定一个工具调用计划。

用户请求: {user_input}
{tools_desc}

请列出所有需要调用的工具，格式如下:
TOOL: 工具名
ARGS: {{"参数名": "参数值"}}
---
TOOL: 工具名
ARGS: {{"参数名": "参数值"}}

如果没有工具可用或不需要工具，直接回答问题。"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=planner_prompt)])
        plan_text = response.content

        # 解析工具调用计划
        tool_calls = []
        current_tool = None
        for line in plan_text.split('\n'):
            line = line.strip()
            if line.startswith('TOOL:'):
                if current_tool:
                    tool_calls.append(current_tool)
                current_tool = {"name": line[5:].strip(), "args": {}}
            elif line.startswith('ARGS:') and current_tool:
                import json
                try:
                    current_tool["args"] = json.loads(line[5:].strip())
                except:
                    current_tool["args"] = {}

        if current_tool:
            tool_calls.append(current_tool)

        return {"tool_results": {}, "messages": [AIMessage(content=f"工具调用计划:\n{plan_text}")]}

    async def _rewOO_worker_node(self, state: AgentState) -> Dict[str, Any]:
        """ReWOO 工作节点 - 并行执行所有工具"""
        user_input = state["messages"][-1].content if state["messages"] else ""

        # 重新获取工具调用计划
        tools_desc = ""
        if self.mcp_manager and self.mcp_manager.all_tools:
            tools_desc = "\n可用工具:\n" + "\n".join([
                f"- {tool.name}: {tool.description}"
                for tool in self.mcp_manager.all_tools
            ])

        planner_prompt = f"""{self._get_system_prompt()}

用户请求: {user_input}
{tools_desc}

请列出所有需要调用的工具，格式如下:
TOOL: 工具名
ARGS: {{"参数名": "参数值"}}
---
TOOL: 工具名
ARGS: {{"参数名": "参数值"}}

如果没有工具可用或不需要工具，直接回答问题。"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=planner_prompt)])
        plan_text = response.content

        # 解析并执行工具调用
        tool_results = {}
        current_tool = None
        for line in plan_text.split('\n'):
            line = line.strip()
            if line.startswith('TOOL:'):
                if current_tool:
                    tool_name = current_tool["name"]
                    result = await self._execute_tool(tool_name, current_tool.get("args", {}), trace_id=None)
                    tool_results[tool_name] = result
                current_tool = {"name": line[5:].strip(), "args": {}}
            elif line.startswith('ARGS:') and current_tool:
                import json
                try:
                    current_tool["args"] = json.loads(line[5:].strip())
                except:
                    current_tool["args"] = {}

        if current_tool:
            tool_name = current_tool["name"]
            result = await self._execute_tool(tool_name, current_tool.get("args", {}), trace_id=None)
            tool_results[tool_name] = result

        return {"tool_results": tool_results, "iterations": state.get("iterations", 0) + 1}

    async def _rewOO_synthesizer_node(self, state: AgentState) -> Dict[str, Any]:
        """ReWOO 综合节点 - 整合所有结果"""
        user_input = state["messages"][-1].content if state["messages"] else ""
        tool_results = state.get("tool_results", {})

        results_text = "\n".join([f"{k}: {v}" for k, v in tool_results.items()]) if tool_results else "无工具调用结果"

        synthesizer_prompt = f"""{self._get_system_prompt()}

用户请求: {user_input}

工具执行结果:
{results_text}

请根据以上工具执行结果，给用户一个完整的回答。"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=synthesizer_prompt)])
        return {"messages": [response]}

    def _build_rewOO_graph(self) -> StateGraph:
        """构建 ReWOO 图"""
        workflow = StateGraph(AgentState)
        workflow.add_node("worker", self._rewOO_worker_node)
        workflow.add_node("synthesizer", self._rewOO_synthesizer_node)
        workflow.set_entry_point("worker")
        workflow.add_edge("worker", "synthesizer")
        workflow.add_edge("synthesizer", END)
        return workflow

    # ==================== ToT 模式 ====================

    async def _tot_generator_node(self, state: AgentState) -> Dict[str, Any]:
        """ToT 生成节点 - 生成多个思考分支"""
        user_input = state["messages"][-1].content if state["messages"] else ""
        current_thought = state.get("current_thought", "")
        iterations = state.get("iterations", 0)

        tools_desc = ""
        if self.mcp_manager and self.mcp_manager.all_tools:
            tools_desc = "\n可用工具:\n" + "\n".join([
                f"- {tool.name}: {tool.description}"
                for tool in self.mcp_manager.all_tools
            ])

        if current_thought:
            prompt = f"""{self._get_system_prompt()}

用户请求: {user_input}
{tools_desc}

当前思考路径: {current_thought}

请基于当前思考，生成2-3个可能的下一步思考方向。每个方向一行，格式:
THOUGHT: [思考内容]
"""
        else:
            prompt = f"""{self._get_system_prompt()}

用户请求: {user_input}
{tools_desc}

请生成2-3个不同的初始思考方向来解决这个问题。每个方向一行，格式:
THOUGHT: [思考内容]
"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=prompt)])

        # 解析思考
        thoughts = []
        for line in response.content.split('\n'):
            if 'THOUGHT:' in line:
                thought = line.split('THOUGHT:')[-1].strip()
                if thought:
                    thoughts.append(thought)

        if not thoughts:
            thoughts = [response.content]

        return {"thoughts": thoughts, "iterations": iterations + 1}

    async def _tot_evaluator_node(self, state: AgentState) -> Dict[str, Any]:
        """ToT 评估节点 - 评估并选择最佳思考"""
        user_input = state["messages"][-1].content if state["messages"] else ""
        thoughts = state.get("thoughts", [])
        evaluations = state.get("evaluations", [])

        # 评估每个思考
        thoughts_text = "\n".join([f"{i+1}. {t}" for i, t in enumerate(thoughts)])

        eval_prompt = f"""请评估以下思考方向，选择最有可能解决问题的一个。

用户请求: {user_input}

思考方向:
{thoughts_text}

请给出:
1. 每个思考的评分(1-10)
2. 最佳思考的编号

格式:
SCORES: [分数1, 分数2, ...]
BEST: 编号"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=eval_prompt)])

        # 解析评估结果
        best_idx = 0
        scores = []

        import re
        scores_match = re.search(r'SCORES:\s*\[([\d,\s]+)\]', response.content)
        if scores_match:
            scores = [int(s.strip()) for s in scores_match.group(1).split(',')]

        best_match = re.search(r'BEST:\s*(\d+)', response.content)
        if best_match:
            best_idx = int(best_match.group(1)) - 1
            best_idx = max(0, min(best_idx, len(thoughts) - 1))

        selected_thought = thoughts[best_idx] if thoughts else ""
        current_thought = state.get("current_thought", "")
        new_thought = f"{current_thought} → {selected_thought}" if current_thought else selected_thought

        new_evaluations = list(evaluations) + [{"thought": selected_thought, "score": scores[best_idx] if scores else 5}]

        return {
            "current_thought": new_thought,
            "evaluations": new_evaluations,
            "iterations": state.get("iterations", 0)
        }

    async def _tot_answer_node(self, state: AgentState) -> Dict[str, Any]:
        """ToT 回答节点 - 基于选定的思考路径给出最终答案"""
        user_input = state["messages"][-1].content if state["messages"] else ""
        thought_path = state.get("current_thought", "")
        evaluations = state.get("evaluations", [])

        tools_desc = ""
        if self.mcp_manager and self.mcp_manager.all_tools:
            tools_desc = "\n可用工具:\n" + "\n".join([
                f"- {tool.name}: {tool.description}"
                for tool in self.mcp_manager.all_tools
            ])

        answer_prompt = f"""{self._get_system_prompt()}

用户请求: {user_input}

思考路径: {thought_path}
{tools_desc}

请基于以上思考路径，给出最终答案。如果需要使用工具，请调用工具。"""

        response = await self._invoke_llm(self.llm, [SystemMessage(content=answer_prompt)])

        # 执行可能的工具调用
        tool_calls = self._parse_tool_calls(response)
        tool_results = []
        for tool_call in tool_calls:
            result = await self._execute_tool(tool_call["name"], tool_call.get("args", {}), trace_id=None)
            tool_results.append(f"工具 {tool_call['name']} 结果: {result}")

        if tool_results:
            follow_up = await self._invoke_llm(self.llm, [
                SystemMessage(content=answer_prompt),
                response,
                SystemMessage(content=f"工具执行结果:\n{chr(10).join(tool_results)}\n\n请给出最终答案。")
            ])
            return {"messages": [follow_up]}

        return {"messages": [response]}

    def _tot_should_continue(self, state: AgentState) -> str:
        """ToT 判断是否继续探索"""
        iterations = state.get("iterations", 0)
        evaluations = state.get("evaluations", [])

        if iterations >= self.config.max_iterations:
            return "answer"

        # 如果已经探索了足够多，给出答案
        if len(evaluations) >= 3:
            return "answer"

        return "generate"

    def _build_tot_graph(self) -> StateGraph:
        """构建 ToT 图"""
        workflow = StateGraph(AgentState)
        workflow.add_node("generate", self._tot_generator_node)
        workflow.add_node("evaluate", self._tot_evaluator_node)
        workflow.add_node("answer", self._tot_answer_node)
        workflow.set_entry_point("generate")
        workflow.add_edge("generate", "evaluate")
        workflow.add_conditional_edges("evaluate", self._tot_should_continue,
            {"generate": "generate", "answer": "answer"})
        workflow.add_edge("answer", END)
        return workflow

    # ==================== 构建图 ====================

    def build_graph(self):
        """构建工作流图"""
        if self.config.planning_mode == PlanningMode.REACT:
            workflow = self._build_react_graph()
        elif self.config.planning_mode == PlanningMode.REFLEXION:
            workflow = self._build_reflexion_graph()
        elif self.config.planning_mode == PlanningMode.PLAN_AND_SOLVE:
            workflow = self._build_plan_and_solve_graph()
        elif self.config.planning_mode == PlanningMode.REWOO:
            workflow = self._build_rewOO_graph()
        elif self.config.planning_mode == PlanningMode.TOT:
            workflow = self._build_tot_graph()
        else:
            workflow = self._build_react_graph()

        self.graph = workflow.compile()

    async def run(self, user_input: str, history: List[Dict] = None) -> str:
        """运行Agent"""
        # 确保LLM被初始化（与stream()方法一致）
        self._refresh_skill_tool_if_needed()

        if not self.graph:
            self.build_graph()

        messages = []

        if history:
            for msg in history:
                if msg.get("role") == "user":
                    messages.append(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "assistant":
                    messages.append(AIMessage(content=msg.get("content", "")))

        messages.append(HumanMessage(content=user_input))

        initial_state = {
            "messages": messages,
            "iterations": 0,
            "plan": None,
            "current_step": 0,
            "tool_results": None,
            "reflection": None,
            "is_satisfactory": True,
            "thoughts": None,
            "current_thought": None,
            "evaluations": None
        }

        final_state = await self.graph.ainvoke(initial_state)

        result_messages = final_state["messages"]
        for msg in reversed(result_messages):
            if isinstance(msg, AIMessage):
                return msg.content

        return "抱歉，我无法处理这个请求。"

    # ============================================================================
    # 【流式输出核心代码 - 谨慎修改】
    #
    # 此方法实现了 Agent 的流式输出功能，包括：
    # - 智能缓冲策略（50字符阈值）
    # - 工具调用检测与处理
    # - 多种事件类型（thinking/content/tool_call/tool_result/metrics）
    #
    # ⚠️ 修改此方法可能影响：
    # 1. 打字机效果的流畅性
    # 2. 工具调用的正确检测
    # 3. 思考过程的实时更新
    # 4. 前端渲染效果
    #
    # 相关文件：
    # - backend.py: chat_stream() - SSE 端点
    # - frontend/src/components/AgentChat.tsx - 前端渲染
    # - frontend/src/app/stream/agents/[name]/chat/route.ts - 流式代理
    # ============================================================================
    async def stream(
        self,
        user_input: str,
        history: List[Dict] = None,
        file_context: str = "",
        trace_id: str = None,
        conversation_id: str = None,
    ):
        """Stream events and guarantee that interrupted traces are closed."""

        active_trace_id = self._normalize_trace_id(trace_id)
        completed = False
        failure: Optional[BaseException] = None
        try:
            async for event in self._stream_events(
                user_input=user_input,
                history=history,
                file_context=file_context,
                trace_id=active_trace_id,
                conversation_id=conversation_id,
            ):
                yield event
            completed = True
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if not completed and is_observability_enabled():
                error = {
                    "type": type(failure).__name__ if failure else "StreamClosed",
                }
                get_tracer().end_trace(
                    trace_id=active_trace_id,
                    status="error",
                    error=error,
                )

    async def _stream_events(self, user_input: str, history: List[Dict] = None, file_context: str = "", trace_id: str = None, conversation_id: str = None):
        """流式运行Agent - 支持返回 thinking、多轮工具调用和最终回答

        【流式输出核心方法 - 谨慎修改】
        此方法通过 yield 返回事件字典，由 backend.py 的 chat_stream() 端点
        转换为 SSE 格式发送到前端。

        事件类型：
        - thinking: 思考过程（实时更新）
        - content: 最终回答内容（逐字符流式输出）
        - tool_call: 工具调用开始
        - tool_result: 工具执行结果
        - skill_loading/skill_loaded: 技能加载状态
        - metrics: 性能指标

        Args:
            user_input: 用户输入
            history: 对话历史
            file_context: 文件上下文信息（包含用户上传文件的元数据）
            trace_id: 可追溯ID，用于关联日志和调试
            conversation_id: 会话ID，用于可观测性追踪
        """
        # Request-scoped state must start empty even when the AgentEngine itself
        # is cached across conversations.
        self._request_token_usage.set((0, 0))
        self._retrieval_sources_context.set(())

        # 刷新skill_tool的enabled_skills（用于配置热更新）
        self._refresh_skill_tool_if_needed()

        # ====================================================================
        # 【可观测性追踪】创建 Trace
        # ====================================================================
        observability_tracer = get_tracer()
        active_trace_id = self._normalize_trace_id(trace_id)
        root_obs_id = None
        if is_observability_enabled():
            trace_info = observability_tracer.create_trace(
                trace_id=active_trace_id,
                name=f"agent:{self.config.name}",
                user_id=self.config.name,
                session_id=conversation_id,
                input={"query_length": len(user_input)},
                metadata={"agent": self.config.name}
            )
            root_obs_id = trace_info.get('observation_id') if trace_info else None

        # 构建系统提示
        system_prompt = self._get_system_prompt()

        # 如果有文件上下文，添加到系统提示词
        if file_context:
            system_prompt += file_context
            system_prompt += "\n\n请优先处理用户上传的文件内容。"

        # 构建非 MCP 工具的提示（MCP 工具已通过 bind_tools() 提供）
        tools_context_parts = []

        # 1. Skill 工具（保留提示，bind_tools Schema 不够直观）
        if self.skill_tool and self.skill_tool.enabled_skills:
            for skill_name in self.skill_tool.enabled_skills:
                skill_desc = self.skill_tool.get_skill_description(skill_name)
                tools_context_parts.append(
                    f"### load_skill ({skill_name})\n{skill_desc}\n"
                )

        # 2. RAG 工具（如果配置了知识库）
        if self.config.knowledge_bases and self.kb_manager:
            kb_descriptions = []
            for kb_id in self.config.knowledge_bases:
                kb = self.kb_manager.get_kb(kb_id)
                if kb:
                    kb_descriptions.append(f"  - {kb.name}: {kb.description or '无描述'}")

            if kb_descriptions:
                tools_context_parts.append(
                    f"### rag_retrieve\n"
                    f"从知识库检索相关文档内容。\n"
                    f"可用知识库：\n"
                    f"{chr(10).join(kb_descriptions)}\n"
                )

        # 3. 子 Agent 工具
        sub_agents = self.get_sub_agent_names()
        if sub_agents:
            for sub_agent in sub_agents:
                tool_name = f"call_agent_{sub_agent.lower().replace('-', '_').replace(' ', '_')}"
                tools_context_parts.append(
                    f"### {tool_name}\n调用子Agent '{sub_agent}'来处理特定任务\n"
                )

        # 动态注入非 MCP 工具提示
        if tools_context_parts:
            tools_context = "\n".join(tools_context_parts)
            system_prompt += f"""

## 辅助工具

{tools_context}

"""

        messages = [SystemMessage(content=system_prompt)]

        if history:
            for msg in history:
                if msg.get("role") == "user":
                    messages.append(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "assistant":
                    messages.append(AIMessage(content=msg.get("content", "")))

        messages.append(HumanMessage(content=user_input))

        full_response = ""
        all_tool_calls_info = []
        iteration = 0
        max_iterations = max(1, min(self.config.max_iterations, 50))
        total_input_tokens = 0
        total_output_tokens = 0
        total_input_chars = 0
        total_output_chars = 0

        # 【AC130-202603190000】发送 RAG 检索来源元数据
        # 前端可用于显示来源引用
        retrieval_sources = self.get_last_retrieval_sources()
        if retrieval_sources:
            yield {"type": "rag_sources", "sources": retrieval_sources}

        # 1. 输出思考过程的开始
        yield {"type": "thinking", "content": "正在分析您的问题..."}

        # 多轮工具调用循环
        while iteration < max_iterations:
            iteration += 1
            tool_calls_info = []
            llm_obs_id = None
            iteration_input_tokens = 0
            iteration_output_tokens = 0

            # ============================================================
            # 【流式输出核心 - 智能缓冲策略】
            #
            # 挑战：需要同时满足两个目标
            # 1. 流式输出：让用户尽快看到响应，实现打字机效果
            # 2. 工具检测：需要完整接收工具调用 JSON 才能正确解析
            #
            # 解决方案：智能缓冲策略
            # - 缓冲前 50 个字符（BUFFER_THRESHOLD）
            # - 如果检测到工具调用特征（{"tool": ...），继续缓冲直到完整 JSON
            # - 如果超过阈值且无工具调用特征，立即开始流式输出
            #
            # ⚠️ 修改 BUFFER_THRESHOLD 或缓冲逻辑可能影响：
            # - 首 token 时延（阈值太大会增加延迟）
            # - 工具调用检测准确性（阈值太小可能截断工具 JSON）
            # ============================================================
            response_content = ""
            response_parts: List[str] = []
            might_be_tool_call = False
            content_started = False
            buffer_content = ""  # 流结束后由 buffer_parts 合并
            buffer_parts: List[str] = []
            buffer_length = 0
            buffering = False    # 是否处于缓冲模式
            BUFFER_THRESHOLD = 50  # 【关键参数】缓冲阈值，平衡检测与响应性
            started_streaming = False  # 是否已开始流式输出
            native_tool_call_chunks: Dict[Tuple[str, Any], Dict[str, Any]] = {}
            streamed_native_tool_calls: List[Dict[str, Any]] = []

            # 更新 thinking 为等待状态
            if iteration == 1:
                yield {"type": "thinking", "content": "✓ 分析用户请求\n✓ 正在生成回答..."}
            else:
                yield {"type": "thinking", "content": f"✓ 第 {iteration} 轮处理\n✓ 分析是否需要更多工具调用..."}

            # 选择使用绑定工具的 LLM 还是原始 LLM
            llm_to_use = self.llm_with_tools if self.llm_with_tools else self.llm

            # ====================================================================
            # 【可观测性追踪】创建 LLM Span
            # ====================================================================
            llm_span_id = None
            llm_obs_id = None
            if is_observability_enabled():
                # 获取模型服务名称（新版 model_service 优先，兼容旧版 llm_provider）
                model_service_name = self.config.model_service or (
                    self.config.llm_provider.value if self.config.llm_provider else 'unknown'
                )
                llm_result = observability_tracer.create_span(
                    trace_id=active_trace_id,
                    span_name=f"llm.{model_service_name}",
                    span_type="LLM",
                    parent_observation_id=root_obs_id,
                    input={
                        "model": model_service_name,
                        "messages_count": len(messages),
                        "iteration": iteration,
                        "messages_length": sum(
                            len(str(getattr(message, "content", "")))
                            for message in messages
                        ),
                    }
                )
                llm_span_id = llm_result[0] if isinstance(llm_result, tuple) else None
                llm_obs_id = llm_result[1] if isinstance(llm_result, tuple) else None

            # ============================================================================
            # 【AC130-202603150000】LLM 流式输出异常处理
            # ============================================================================
            # 记录输入消息字符数，用于后备 token 估算
            iteration_input_chars = sum(
                len(str(getattr(m, 'content', '')))
                for m in messages
                if hasattr(m, 'content') and m.content
            )
            iteration_output_chars = 0
            try:
                async for chunk in self._stream_llm(llm_to_use, messages):
                    # 【AC130-202603222100】检测原生 tool_calls（bind_tools 模式）
                    # 当使用 bind_tools 时，工具调用信息在 chunk.tool_calls 中，而非 content
                    chunk_tool_call_chunks = getattr(chunk, 'tool_call_chunks', None) or []
                    if chunk_tool_call_chunks:
                        self._merge_native_tool_call_chunks(
                            native_tool_call_chunks,
                            chunk_tool_call_chunks,
                        )
                        might_be_tool_call = True
                        if not content_started:
                            content_started = True
                            buffering = True
                            yield {"type": "thinking", "content": "✓ 分析用户请求\n✓ 检测到原生工具调用..."}

                    complete_tool_calls = getattr(chunk, 'tool_calls', None) or []
                    if complete_tool_calls:
                        self._validate_complete_tool_calls(complete_tool_calls)
                        streamed_native_tool_calls = self._convert_native_tool_calls(
                            complete_tool_calls
                        )

                    if chunk.content:
                        chunk_text = str(chunk.content)
                        if (
                            iteration_output_chars + len(chunk_text)
                            > self.MAX_LLM_RESPONSE_CHARS
                        ):
                            raise ValueError("模型响应超过 2MB 字符上限")
                        response_parts.append(chunk_text)

                        # 检查是否可能是工具调用
                        if not content_started:
                            content_started = True
                            buffering = True
                            # The first chunk is part of the response regardless
                            # of whether it resembles a textual tool-call JSON.
                            buffer_parts.append(chunk_text)
                            buffer_length += len(chunk_text)
                            stripped = chunk_text.strip()
                            if stripped.startswith('{') or stripped.startswith('```json'):
                                might_be_tool_call = True
                                yield {"type": "thinking", "content": "✓ 分析用户请求\n✓ 检测到工具调用..."}
                        elif buffering and not started_streaming:
                            # 还在缓冲模式，累积内容
                            buffer_parts.append(chunk_text)
                            buffer_length += len(chunk_text)

                            # 检查缓冲内容是否包含工具调用 JSON
                            detection_buffer = "".join(buffer_parts) if not might_be_tool_call else ""
                            if '"tool"' in detection_buffer and '{' in detection_buffer:
                                might_be_tool_call = True

                            # 【流式输出核心】如果缓冲区超过阈值且没有检测到工具调用，开始流式输出
                            if buffer_length > BUFFER_THRESHOLD and not might_be_tool_call:
                                started_streaming = True
                                # Emit one bounded initial chunk. Subsequent model chunks
                                # remain incremental, without generating one event per char.
                                yield {"type": "content", "content": detection_buffer}
                                buffer_parts.clear()
                                buffer_length = 0
                        elif started_streaming:
                            # 【流式输出核心】已经开始流式输出，直接输出新内容
                            # 这里的 chunk.content 是 LLM 返回的增量内容，直接透传给前端
                            yield {"type": "content", "content": chunk_text}
                        elif buffering and might_be_tool_call:
                            # 检测到工具调用，继续缓冲
                            buffer_parts.append(chunk_text)
                            buffer_length += len(chunk_text)

                    # 【上下文窗口状态栏】提取 token 使用信息
                    # 许多 LLM API 在最后一个 chunk 中返回 usage_metadata（content 通常为空）
                    usage_metadata = getattr(chunk, 'usage_metadata', None)
                    if usage_metadata:
                        try:
                            reported_input = self._usage_metadata_value(
                                usage_metadata, "input_tokens"
                            )
                            reported_output = self._usage_metadata_value(
                                usage_metadata, "output_tokens"
                            )
                            if reported_input:
                                iteration_input_tokens = reported_input
                            if reported_output:
                                iteration_output_tokens = reported_output
                        except Exception:
                            pass

                    # 累计输出字符数（用于后备 token 估算）
                    chunk_content = getattr(chunk, 'content', None)
                    if chunk_content:
                        iteration_output_chars += len(chunk_content)

                response_content = "".join(response_parts)
                buffer_content = "".join(buffer_parts)

                # 流式结束后：仅对本轮 API 未返回的字段做估算。
                if iteration_input_tokens == 0 and iteration_input_chars > 0:
                    iteration_input_tokens = self._estimate_tokens_from_chars(
                        iteration_input_chars
                    )
                if iteration_output_tokens == 0 and iteration_output_chars > 0:
                    iteration_output_tokens = self._estimate_tokens_from_chars(
                        iteration_output_chars
                    )

                total_input_tokens += iteration_input_tokens
                total_output_tokens += iteration_output_tokens
                total_input_chars += iteration_input_chars
                total_output_chars += iteration_output_chars
                self._publish_token_usage(
                    total_input_tokens,
                    total_output_tokens,
                    total_input_chars,
                    total_output_chars,
                )

            # ============================================================================
            # 【AC130-202603150000】LLM 流式输出异常捕获
            # ============================================================================
            except Exception as e:
                # Publish any usage observed before the stream failed without
                # carrying data over from a previous request.
                self._publish_token_usage(
                    total_input_tokens + iteration_input_tokens,
                    total_output_tokens + iteration_output_tokens,
                    total_input_chars + iteration_input_chars,
                    total_output_chars + iteration_output_chars,
                )
                # LLM 调用失败，发送错误事件到前端
                error_msg = "LLM 调用失败"
                print(
                    "[ERROR] LLM stream error: "
                    f"error_type={type(e).__name__}"
                )
                # 【可观测性追踪】结束 LLM Span (错误)
                if llm_span_id and is_observability_enabled():
                    observability_tracer.end_span(
                        trace_id=active_trace_id,
                        span_id=llm_span_id,
                        output={"error_type": type(e).__name__},
                        status="error",
                        error={"type": type(e).__name__},
                    )
                if is_observability_enabled():
                    observability_tracer.end_trace(
                        trace_id=active_trace_id,
                        status="error",
                        error={"type": type(e).__name__},
                    )
                yield {
                    "type": "error",
                    "content": error_msg,
                    "error_type": type(e).__name__
                }
                # 重新抛出异常，让后端 logger 记录
                raise

            # 如果在缓冲模式，检查是否有工具调用
            if buffering and not started_streaming:
                # 检查缓冲内容是否包含工具调用
                if '"tool"' in buffer_content:
                    might_be_tool_call = True

            # 3. 检查是否有工具调用（支持原生 tool_calls 和文本解析）
            tool_calls = self._native_tool_calls_from_chunks(native_tool_call_chunks)

            # Some providers populate complete ``tool_calls`` on a streamed
            # chunk in addition to deltas. Prefer the merged deltas, and use the
            # complete streamed value only as a fallback. Never issue a second
            # model request to reconstruct an answer that has already streamed.
            if not tool_calls and streamed_native_tool_calls:
                tool_calls = streamed_native_tool_calls
            if tool_calls:
                print(
                    f"[DEBUG] 检测到原生 tool_calls: "
                    f"{[tc['name'] for tc in tool_calls]}"
                )

            # 如果没有原生 tool_calls，尝试文本解析（兼容模式）
            if not tool_calls:
                # 始终尝试解析工具调用，不依赖 might_be_tool_call 标志
                # 因为某些模型（如 GLM-4.5-Air）可能在 JSON 前输出换行符或思考文本
                class MockResponse:
                    def __init__(self, content):
                        self.content = content
                        self.tool_calls = []

                mock_response = MockResponse(response_content)
                tool_calls = self._parse_tool_calls_enhanced(mock_response)

            self._validate_complete_tool_calls(tool_calls)

            # 【可观测性追踪】结束 LLM Span (成功) — 在 tool_calls 解析后，包含工具调用信息
            if llm_span_id and is_observability_enabled():
                llm_output = {
                    "response_length": len(response_content),
                }
                if tool_calls:
                    llm_output["tool_calls"] = [
                        {"name": tc.get("name"), "call_id": tc.get("call_id")}
                        for tc in tool_calls
                    ]
                usage_data = None
                if iteration_input_tokens > 0 or iteration_output_tokens > 0:
                    usage_data = {
                        "input": iteration_input_tokens,
                        "output": iteration_output_tokens,
                        "total": iteration_input_tokens + iteration_output_tokens,
                    }
                observability_tracer.end_span(
                    trace_id=active_trace_id,
                    span_id=llm_span_id,
                    output=llm_output,
                    usage=usage_data,
                )

            # 4. 根据是否有工具调用，处理输出
            if tool_calls:
                # 有工具调用：显示决策过程
                thinking_steps = [
                    "✓ 分析用户请求",
                    f"✓ 匹配到可用工具: {', '.join([tc['name'] for tc in tool_calls])}",
                    "✓ 准备调用工具..."
                ]
                yield {"type": "thinking", "content": "\n".join(thinking_steps)}

                # 执行工具调用
                for tool_call in tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call.get("args", {})
                    service_name = ""
                    is_skill_tool = (
                        tool_name == SkillTool.TOOL_NAME
                        or tool_name == SkillTool.EXECUTE_TOOL_NAME
                    )
                    is_rag_tool = tool_name == "rag_retrieve"
                    is_sub_agent_tool = tool_name.startswith("call_agent_")
                    sub_agent_name = None
                    call_id = "unknown"
                    tool_span_id = None

                    # ============================================================================
                    # 【AC130-202603150000】工具执行异常处理
                    # ============================================================================
                    try:
                        # 获取工具的服务名
                        if self.mcp_manager:
                            tool = self.mcp_manager.get_tool(tool_name)
                            if tool:
                                service_name = tool.server_name

                        # 检查是否是 skill 工具
                        # 检测并解析子 Agent 工具
                        if is_sub_agent_tool:
                            # 从工具名中提取子Agent名称
                            for candidate in self.get_sub_agent_names():
                                expected_name = f"call_agent_{candidate.lower().replace('-', '_').replace(' ', '_')}"
                                if tool_name == expected_name:
                                    sub_agent_name = candidate
                                    break

                        # 生成唯一标识符（用于区分同名工具的多次调用）
                        import uuid
                        call_id = str(uuid.uuid4())[:8]
                        sub_agent_start_time = None  # 用于计算调用耗时

                        # 如果是 skill 工具，规范化 args 中的 skill_name
                        normalized_tool_args = tool_args
                        if is_skill_tool:
                            raw_skill_name = tool_args.get("skill_name") or tool_args.get("skill", "")
                            if raw_skill_name and self.skill_tool:
                                matched_name = self.skill_tool._match_skill_name(raw_skill_name)
                                if matched_name:
                                    normalized_tool_args = {**tool_args, "skill_name": matched_name}

                        # 输出工具调用信息（包含服务名和唯一ID）
                        # 确定服务名称：优先使用 service_name，skill 工具回退到 "skill-system"，子 Agent 工具使用 agent: 前缀
                        service_display = service_name
                        if not service_display:
                            if is_skill_tool:
                                service_display = "skill-system"
                            elif is_sub_agent_tool and sub_agent_name:
                                service_display = f"agent:{sub_agent_name}"
                            elif is_rag_tool:
                                service_display = "knowledge-base"

                        yield {
                            "type": "tool_call",
                            "name": tool_name,
                            "call_id": call_id,
                            "service": service_display,
                            "args": normalized_tool_args
                        }

                        # 如果是 skill 工具，发送 skill_loading 事件
                        if is_skill_tool:
                            # 使用已规范化的skill名称
                            skill_name = normalized_tool_args.get("skill_name") or normalized_tool_args.get("skill", "")
                            yield {
                                "type": "skill_loading",
                                "skill_name": skill_name
                            }

                        # ====================================================================
                        # 【AC130-202603142210】发送子Agent调用开始事件
                        # ====================================================================
                        if is_sub_agent_tool and sub_agent_name:
                            sub_agent_message = tool_args.get("message", "")
                            import time
                            sub_agent_start_time = time.time()
                            yield {
                                "type": "sub_agent_call",
                                "agent_name": sub_agent_name,
                                "message": sub_agent_message,
                                "call_id": call_id
                            }

                        # ====================================================================
                        # 【AC130-202603161918】发送RAG检索开始事件
                        # ====================================================================
                        if is_rag_tool:
                            query = tool_args.get("query", "")
                            yield {
                                "type": "rag_retrieve",
                                "query": query,
                                "call_id": call_id
                            }

                        # 【可观测性追踪】创建工具或子 Agent Span
                        if is_observability_enabled():
                            span_name = f"tool.{tool_name}"
                            if is_sub_agent_tool:
                                span_name = f"agent.{sub_agent_name or tool_name}"
                            elif is_rag_tool:
                                span_name = "rag.retrieve"
                            tool_result = observability_tracer.create_span(
                                trace_id=active_trace_id,
                                span_name=span_name,
                                span_type=(
                                    "AGENT" if is_sub_agent_tool
                                    else "RETRIEVER" if is_rag_tool
                                    else "TOOL"
                                ),
                                parent_observation_id=llm_obs_id,
                                input={
                                    "tool": tool_name,
                                    **summarize_tool_arguments(tool_args),
                                }
                            )
                            tool_span_id = tool_result[0] if isinstance(tool_result, tuple) else None

                        # 执行工具
                        result = await self._execute_tool(tool_name, tool_args, trace_id=active_trace_id)

                        # 【可观测性追踪】结束工具 Span
                        if tool_span_id and is_observability_enabled():
                            result_is_error = result.startswith(
                                ("Error:", "错误:", "检索失败:", "子Agent调用失败:")
                            )
                            observability_tracer.end_span(
                                trace_id=active_trace_id,
                                span_id=tool_span_id,
                                output={"result_length": tool_result_length(result)},
                                status="error" if result_is_error else "success",
                                error={"type": "tool_error"} if result_is_error else None,
                            )

                        # 如果是 skill 工具，发送 skill_loaded 事件
                        if is_skill_tool:
                            # 使用已规范化的skill名称
                            skill_name = normalized_tool_args.get("skill_name") or normalized_tool_args.get("skill", "")
                            yield {
                                "type": "skill_loaded",
                                "skill_name": skill_name,
                                "success": not result.startswith("Error:")
                            }

                        # ====================================================================
                        # 【AC130-202603142210】发送子Agent调用结果事件
                        # ====================================================================
                        if is_sub_agent_tool and sub_agent_name:
                            import time
                            duration_ms = int((time.time() - sub_agent_start_time) * 1000) if sub_agent_start_time else 0

                            # 检测是否为错误结果
                            if result.startswith("子Agent调用失败:"):
                                error_msg = result.replace("子Agent调用失败:", "").strip()
                                # 确定错误类型
                                error_type = "exception"
                                if "超时" in error_msg:
                                    error_type = "timeout"
                                elif "循环" in error_msg or "cycle" in error_msg.lower():
                                    error_type = "recursion"
                                elif "不存在" in error_msg:
                                    error_type = "not_found"

                                yield {
                                    "type": "sub_agent_error",
                                    "agent_name": sub_agent_name,
                                    "call_id": call_id,
                                    "error": error_msg,
                                    "error_type": error_type,
                                    "duration_ms": duration_ms
                                }
                            else:
                                # 成功结果
                                yield {
                                    "type": "sub_agent_result",
                                    "agent_name": sub_agent_name,
                                    "call_id": call_id,
                                    "result": result,
                                    "duration_ms": duration_ms
                                }

                        # 输出工具结果（包含唯一ID用于匹配）
                        # 确定服务名称：优先使用 service_name，skill 工具回退到 "skill-system"，子 Agent 工具使用 agent: 前缀
                        service_display = service_name
                        if not service_display:
                            if is_skill_tool:
                                service_display = "skill-system"
                            elif is_sub_agent_tool and sub_agent_name:
                                service_display = f"agent:{sub_agent_name}"
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "call_id": call_id,
                            "service": service_display,
                            "result": result
                        }

                        # Retrieval sources belong to this request and become
                        # available only after the RAG tool has completed.
                        # Emit even an empty list so the client can clear any
                        # citations left on screen from an earlier request.
                        if is_rag_tool:
                            yield {
                                "type": "rag_sources",
                                "sources": self.get_last_retrieval_sources(),
                            }

                        tool_calls_info.append({
                            "name": tool_name,
                            "call_id": call_id,
                            "args": tool_args,
                            "result": result
                        })

                    # ============================================================================
                    # 【AC130-202603150000】工具执行异常捕获
                    # ============================================================================
                    except Exception as e:
                        # 工具执行失败，只向浏览器暴露异常类型。
                        public_error = f"错误: 工具执行失败 ({type(e).__name__})"
                        print(
                            "[ERROR] Tool execution error: "
                            f"tool={tool_name}, error_type={type(e).__name__}"
                        )

                        if tool_span_id and is_observability_enabled():
                            observability_tracer.end_span(
                                trace_id=active_trace_id,
                                span_id=tool_span_id,
                                output={"result_length": 0},
                                status="error",
                                error={"type": type(e).__name__},
                            )

                        # 发送工具结果（错误信息）
                        # 确定服务名称：优先使用 service_name，skill 工具回退到 "skill-system"，子 Agent 工具使用 agent: 前缀
                        service_display = service_name
                        if not service_display:
                            if is_skill_tool:
                                service_display = "skill-system"
                            elif is_sub_agent_tool and sub_agent_name:
                                service_display = f"agent:{sub_agent_name}"
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "call_id": call_id,
                            "service": service_display,
                            "result": public_error,
                            "error": True
                        }

                        tool_calls_info.append({
                            "name": tool_name,
                            "call_id": call_id,
                            "args": tool_args,
                            "result": public_error
                        })

                all_tool_calls_info.extend(tool_calls_info)

                # 5. 将工具结果添加到消息中，继续下一轮（可能需要更多工具调用）
                tool_results_text = "\n".join([
                    f"工具 {tc['name']} 的结果: {tc['result']}"
                    for tc in tool_calls_info
                ])

                # 构建继续提示
                continue_prompt = f"""工具执行结果:
{tool_results_text}

请判断：
1. 如果用户的原始请求还有未完成的任务，请继续调用所需的工具
2. 如果所有任务都已完成，请使用 Markdown 格式汇总所有结果，分段展示给用户

原始用户请求: {user_input}"""

                messages.append(AIMessage(content=response_content))
                messages.append(HumanMessage(content=continue_prompt))

                # 继续下一轮循环，让 LLM 决定是否需要更多工具调用
            else:
                # 没有工具调用，这一轮是最终回答
                if started_streaming:
                    # 内容已经流式输出，不需要再处理
                    full_response = response_content
                elif buffering and buffer_content:
                    # 还在缓冲模式（内容较短，未触发阈值），现在输出缓冲的内容
                    yield {"type": "thinking", "content": "✓ 分析用户请求\n✓ 无需调用工具，直接回答"}
                    full_response = buffer_content
                    yield {"type": "content", "content": buffer_content}
                elif might_be_tool_call and response_content:
                    # 检测到可能的工具调用但解析失败，输出原始内容
                    yield {"type": "thinking", "content": "✓ 分析用户请求\n✓ 生成回答"}
                    full_response = response_content
                    yield {"type": "content", "content": response_content}
                elif response_content and not full_response:
                    # 其他情况，确保输出响应内容
                    full_response = response_content
                    yield {"type": "content", "content": response_content}

                # 退出循环
                break

        # 如果有多轮工具调用，确保最终有汇总输出
        if all_tool_calls_info and not full_response:
            # 【TC-004 修复】当 LLM 在多轮工具调用后没有生成最终回答时，
            # 需要确保输出内容，而不是只输出 thinking
            yield {"type": "thinking", "content": "✓ 所有工具调用完成\n✓ 生成汇总结果..."}

            # 构建工具调用结果的汇总内容
            summary_parts = []
            for tc in all_tool_calls_info:
                tool_name = tc.get('name', 'unknown')
                result = tc.get('result', '')
                # 截取结果的前 500 字符，避免过长
                if len(result) > 500:
                    result = result[:500] + '...(内容过长已截断)'
                summary_parts.append(f"**{tool_name}** 执行结果:\n```\n{result}\n```")

            if summary_parts:
                summary_content = "\n\n".join(summary_parts)
                full_response = summary_content
                for offset in range(0, len(summary_content), 64):
                    yield {"type": "content", "content": summary_content[offset:offset + 64]}
            else:
                # 如果没有工具调用结果，输出默认消息
                default_msg = "工具调用已完成，但未能生成最终回答。请尝试重新提问。"
                full_response = default_msg
                yield {"type": "content", "content": default_msg}

        # ====================================================================
        # 【可观测性追踪】结束 Trace
        # ====================================================================
        if is_observability_enabled():
            observability_tracer.end_trace(
                trace_id=active_trace_id,
                output={
                    "response_length": len(full_response),
                    "tool_call_count": len(all_tool_calls_info),
                }
            )

    @staticmethod
    def _tool_call_chunk_mapping(chunk: Any) -> Dict[str, Any]:
        if isinstance(chunk, Mapping):
            return dict(chunk)
        if hasattr(chunk, "model_dump"):
            dumped = chunk.model_dump()
            return dict(dumped) if isinstance(dumped, Mapping) else {}
        return {
            key: getattr(chunk, key, None)
            for key in ("index", "id", "name", "args")
        }

    @staticmethod
    def _merge_string_fragment(current: str, fragment: Any) -> str:
        if fragment is None:
            return current
        text = str(fragment)
        if not text:
            return current
        if not current or text.startswith(current):
            return text
        if text == current:
            return current
        return current + text

    def _merge_native_tool_call_chunks(
        self,
        accumulated: Dict[Tuple[str, Any], Dict[str, Any]],
        chunks: Sequence[Any],
    ) -> None:
        """Merge provider tool-call deltas by index without another LLM call."""
        for position, raw_chunk in enumerate(chunks):
            chunk = self._tool_call_chunk_mapping(raw_chunk)
            index = chunk.get("index")
            chunk_id = chunk.get("id")
            if index is not None:
                key: Tuple[str, Any] = ("index", index)
            else:
                # Provider IDs can themselves arrive as deltas (for example,
                # ``call_`` followed by ``123``), so they are not safe keys.
                # In the no-index fallback, list position is the only stable
                # identity shared by successive streamed chunks.
                key = ("position", position)

            state = accumulated.setdefault(
                key,
                {
                    "index": index,
                    "id": "",
                    "name": "",
                    "args_text": "",
                    "args_mapping": {},
                    "has_mapping_args": False,
                    "argument_bytes": 0,
                },
            )
            state["id"] = self._merge_string_fragment(state["id"], chunk_id)
            state["name"] = self._merge_string_fragment(
                state["name"], chunk.get("name")
            )
            if len(state["id"]) > 256 or len(state["name"]) > 128:
                raise ValueError("模型返回了过长的工具标识")
            args_fragment = chunk.get("args")
            if isinstance(args_fragment, Mapping):
                state["args_mapping"].update(dict(args_fragment))
                state["has_mapping_args"] = True
                encoded_fragment = json.dumps(
                    args_fragment,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=lambda item: f"<{type(item).__name__}>",
                ).encode("utf-8")
                state["argument_bytes"] += len(encoded_fragment)
            elif args_fragment is not None:
                fragment_text = str(args_fragment)
                state["args_text"] += fragment_text
                state["argument_bytes"] += len(fragment_text.encode("utf-8"))

            if len(accumulated) > self.MAX_LLM_TOOL_CALLS:
                raise ValueError("模型返回的工具调用数量过多")
            if sum(
                int(item.get("argument_bytes", 0))
                for item in accumulated.values()
            ) > self.MAX_LLM_TOOL_ARGUMENT_BYTES:
                raise ValueError("模型返回的工具参数超过 1MB 上限")

    def _native_tool_calls_from_chunks(
        self,
        accumulated: Mapping[Tuple[str, Any], Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        tool_calls: List[Dict[str, Any]] = []
        for state in accumulated.values():
            name = str(state.get("name") or "")
            if not name:
                continue
            if state.get("has_mapping_args"):
                args: Any = dict(state.get("args_mapping") or {})
            else:
                args_text = str(state.get("args_text") or "").strip()
                if not args_text:
                    args = {}
                else:
                    try:
                        args = json.loads(args_text)
                    except json.JSONDecodeError:
                        # A malformed provider delta is not safe to execute with
                        # guessed arguments; textual parsing remains available.
                        continue
            if not isinstance(args, Mapping):
                continue
            call_id = str(state.get("id") or f"call_{name}")
            tool_calls.append({"name": name, "args": dict(args), "id": call_id})
        return tool_calls

    @staticmethod
    def _usage_metadata_value(metadata: Any, field: str) -> int:
        value = metadata.get(field, 0) if isinstance(metadata, Mapping) else getattr(
            metadata, field, 0
        )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _publish_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        input_chars: int,
        output_chars: int,
    ) -> None:
        usage = (max(0, int(input_tokens)), max(0, int(output_tokens)))
        self._request_token_usage.set(usage)
        # Keep the legacy fields for diagnostic compatibility. Request-facing
        # reads use the ContextVar below and therefore cannot observe another
        # task's values.
        self._last_input_tokens, self._last_output_tokens = usage
        self._last_input_chars = max(0, int(input_chars))
        self._last_output_chars = max(0, int(output_chars))

    def _convert_native_tool_calls(self, native_tool_calls) -> List[Dict]:
        """
        将原生 tool_calls 转换为统一格式

        【AC130-202603141800 TC-001 修复】
        处理 Pydantic 模型参数的序列化问题

        Args:
            native_tool_calls: LangChain 原生 tool_calls

        Returns:
            统一格式的工具调用列表 [{"name": ..., "args": ...}, ...]
        """
        converted = []

        for tc in native_tool_calls:
            try:
                # LangChain tool_call 格式
                # tc.name: 工具名称
                # tc.args: 参数字典（可能是 Pydantic 模型）
                tool_name = tc.name if hasattr(tc, 'name') else tc.get('name', '')

                # 获取参数
                if hasattr(tc, 'args'):
                    tool_args = tc.args
                    # ========================================
                    # 【TC-001 修复】处理 Pydantic 模型参数
                    # ========================================
                    # 当 LLM 使用 bind_tools() 时，tc.args 可能是 Pydantic 模型实例
                    # Pydantic 模型无法直接 JSON 序列化，需要转换为 dict
                    if hasattr(tool_args, 'model_dump'):
                        # pydantic v2
                        tool_args = tool_args.model_dump()
                    elif hasattr(tool_args, 'dict'):
                        # pydantic v1
                        tool_args = tool_args.dict()
                elif isinstance(tc, dict):
                    tool_args = tc.get('args', {})
                    # 同样处理 dict 中的 Pydantic 模型
                    if hasattr(tool_args, 'model_dump'):
                        tool_args = tool_args.model_dump()
                    elif hasattr(tool_args, 'dict'):
                        tool_args = tool_args.dict()
                else:
                    tool_args = {}

                if tool_name:
                    converted.append({
                        "name": tool_name,
                        "args": tool_args,
                        "id": f"call_{tool_name}"
                    })
            except Exception as e:
                print(
                    "[WARNING] 转换 tool_call 失败: "
                    f"error_type={type(e).__name__}"
                )

        return converted

    def _parse_tool_calls_enhanced(self, response) -> List[Dict]:
        """增强版工具调用解析 - 支持多种格式"""
        tool_calls = []

        # 1. 检查 LLM 原生的 tool_calls
        if hasattr(response, 'tool_calls') and response.tool_calls:
            return response.tool_calls

        content = response.content or ""

        # 1.5 先移除 markdown 代码块标记和前后空白
        content_cleaned = content
        # 移除 ```json 和 ``` 标记
        content_cleaned = re.sub(r'```json\s*', '', content_cleaned)
        content_cleaned = re.sub(r'```\s*', '', content_cleaned)
        content_cleaned = content_cleaned.strip()

        # ====================================================================
        # 【AC130-202603141800】解析 GLM 格式: tool_call\n{name}\n{args}
        # ====================================================================
        if not tool_calls and "tool_call" in content_cleaned:
            lines = content_cleaned.split('\n')
            for i, line in enumerate(lines):
                if line.strip() == "tool_call" and i + 1 < len(lines):
                    tool_name = lines[i + 1].strip()
                    args = {}
                    if i + 2 < len(lines):
                        try:
                            args = json.loads(lines[i + 2].strip())
                        except:
                            args = {}
                    tool_calls.append({
                        "name": tool_name,
                        "args": args,
                        "id": f"call_{tool_name}"
                    })
                    break  # 只处理第一个工具调用

        # 2. 解析新 JSON 格式 {"tool": "name", "arguments": {...}}
        # 尝试直接解析清理后的内容
        try:
            # 检查是否包含工具调用 JSON（不要求必须在开头）
            if '"tool"' in content_cleaned and '{' in content_cleaned:
                # 找到第一个 JSON 对象
                start = content_cleaned.find('{')
                if start != -1:
                    # 尝试找到匹配的结束括号
                    brace_count = 0
                    end = start
                    for i, char in enumerate(content_cleaned[start:], start):
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                end = i + 1
                                break

                    if end > start:
                        json_str = content_cleaned[start:end]
                        parsed = json.loads(json_str)
                        if parsed.get("tool"):
                            tool_calls.append({
                                "name": parsed["tool"],
                                "args": parsed.get("arguments", {}),
                                "id": f"call_{parsed['tool']}"
                            })
                            return tool_calls
        except json.JSONDecodeError:
            pass

        # 3. 使用正则匹配
        json_pattern = r'\{[^{}]*"tool"\s*:\s*"([^"]+)"[^{}]*\}'
        json_matches = re.findall(json_pattern, content)
        for tool_name in json_matches:
            # 尝试解析完整的 JSON 获取 arguments
            try:
                # 找到完整的 JSON 对象
                start = content.find('{')
                end = content.rfind('}') + 1
                if start != -1 and end > start:
                    json_str = content[start:end]
                    # 清理可能的 markdown 标记
                    json_str = re.sub(r'```json\s*', '', json_str)
                    json_str = re.sub(r'```\s*', '', json_str)
                    parsed = json.loads(json_str)
                    if parsed.get("tool"):
                        tool_calls.append({
                            "name": parsed["tool"],
                            "args": parsed.get("arguments", {}),
                            "id": f"call_{parsed['tool']}"
                        })
            except:
                # 如果完整解析失败，只使用工具名
                tool_calls.append({"name": tool_name, "args": {}, "id": f"call_{tool_name}"})

        # 3. 解析 XML 格式的工具调用
        if not tool_calls:
            xml_pattern = r'<tool_call\s+name=["\']([^"\']+)["\']\s*>([^<]*)</tool_call\s*>'
            matches = re.findall(xml_pattern, content, re.IGNORECASE)
            for name, args_str in matches:
                try:
                    args = json.loads(args_str.strip()) if args_str.strip() else {}
                except:
                    args = {}
                tool_calls.append({"name": name, "args": args, "id": f"call_{name}"})

        # 4. 解析 [CALL:tool_name] 格式
        if not tool_calls and self.mcp_manager and self.mcp_manager.all_tools:
            call_pattern = r'\[CALL:(\w+)\]'
            call_matches = re.findall(call_pattern, content)
            for name in call_matches:
                tool_calls.append({"name": name, "args": {}, "id": f"call_{name}"})

            # 5. 检查 "调用工具: xxx" 格式
            for tool in self.mcp_manager.all_tools:
                if f"调用工具: {tool.name}" in content or f"调用 {tool.name}" in content:
                    if not any(tc["name"] == tool.name for tc in tool_calls):
                        tool_calls.append({"name": tool.name, "args": {}, "id": f"call_{tool.name}"})

        return tool_calls

    def get_token_usage(self) -> dict:
        """
        获取最后一次 LLM 调用的 token 使用信息。

        优先使用 API 返回的 usage_metadata，不可用时回退到字符数估算。

        Returns:
            dict: 包含 input_tokens 和 output_tokens 的字典
        """
        input_tokens, output_tokens = self._request_token_usage.get()
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    @staticmethod
    def _estimate_tokens_from_chars(char_count: int) -> int:
        """
        基于字符数估算 token 数量。
        中文约 1.5 字符/token，英文约 4 字符/token，混合内容取 2 字符/token。
        """
        if char_count <= 0:
            return 0
        return max(1, char_count // 2)

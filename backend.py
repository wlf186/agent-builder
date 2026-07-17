"""
通用Agent构建器 - FastAPI 后端
"""
import json
import asyncio
from collections.abc import Callable
import os
import re
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime

# 加载 .env 环境变量
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, HTTPException, UploadFile, Request, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn


# ============================================================================
# 辅助函数
# ============================================================================

def _calculate_mock_progress(elapsed_seconds: float) -> tuple[float, int]:
    """
    计算环境创建的模拟进度

    基于环境创建时间的保守估算：
    - 0-5秒: 初始化阶段 (0-30%)
    - 5-15秒: 下载依赖阶段 (30-70%)
    - 15-30秒: 安装配置阶段 (70-95%)
    - 超过30秒: 保持95%等待最终完成

    Args:
        elapsed_seconds: 已经过的时间(秒)

    Returns:
        (progress, estimated_remaining_ms) - 进度百分比(0-100)和预估剩余时间(毫秒)
    """
    if elapsed_seconds < 5:
        progress = min(30, (elapsed_seconds / 5) * 30)
        # 估算剩余时间: 假设总时间约30秒
        remaining = max(0, 30 - elapsed_seconds) * 1000
    elif elapsed_seconds < 15:
        progress = 30 + min(40, ((elapsed_seconds - 5) / 10) * 40)
        remaining = max(0, 30 - elapsed_seconds) * 1000
    elif elapsed_seconds < 30:
        progress = 70 + min(25, ((elapsed_seconds - 15) / 15) * 25)
        remaining = max(0, 35 - elapsed_seconds) * 1000
    else:
        progress = min(95, 70 + ((elapsed_seconds - 15) / 15) * 25)
        remaining = max(5000, (40 - elapsed_seconds) * 1000)  # 至少显示5秒

    return (round(progress, 1), int(remaining))

from src.models import (
    AgentConfig, LLMProvider, PlanningMode, MCPServiceConfig, MCPConnectionType,
    MCPAuthType, ModelServiceConfig, ModelProvider,
    # 新增：环境相关模型
    EnvironmentStatus,
    FileInfo, ExecutionRecord, ExecutionStatus,
    # 新增：RAG 知识库相关模型 (AC130-202603161542)
    KnowledgeBase, Document, DocumentStatus, RetrievalConfig, RetrievalResult
)
from src.agent_manager import AgentManager
from src.mcp_registry import MCPServiceRegistry
from src.mcp_manager import test_mcp_connection
from src.builtin_services import (
    is_builtin_service_name,
    setup_builtin_services,
    shutdown_builtin_services,
)
from src.skill_registry import SkillRegistry
from src.skill_loader import SkillLoader
from src.model_service_registry import ModelServiceRegistry
from src.model_provider_tester import test_model_service_connection
from src.conversation_manager import ConversationManager
from src.model_config import get_context_window_size
# 新增：环境、文件、执行管理器
from src.environment_manager import EnvironmentManager, EnvironmentError
from src.environment_creator import EnvironmentCreator
from src.file_storage_manager import FileStorageManager, FileStorageError
from src.execution_engine import ExecutionEngine, ExecutionError
# 【AC130-202603142210】循环检测器
from src.cycle_detector import CycleDetector
from src.observability import get_observability_status, get_tracer
from src.security import (
    APIAuthenticationError,
    RequestBodyLimitMiddleware,
    SecurityValidationError,
    authenticate_api_headers,
    parse_cors_origins,
    read_json_body_limited,
    redact_arguments,
    redact_mapping,
    resolve_contained_path,
    sanitise_filename,
    validate_execution_arguments,
    validate_headers,
    validate_outbound_url,
    validate_package_specs,
    validate_stdio_configuration,
)
from src.blocking_work import run_blocking_with_semaphore
from src.log_safety import content_length, serialized_length, summarize_arguments
from src.local_log_store import append_rotating_log, write_client_log
from src.storage_paths import UnsafeStoragePathError, ensure_real_directory


# 初始化
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
SKILLS_DIR = Path(__file__).parent / "skills"
RUNTIME_DIR = Path(
    os.environ.get(
        "AGENT_BUILDER_RUNTIME_DIR",
        str(Path(__file__).parent / ".runtime"),
    )
)
ENVIRONMENTS_DIR = RUNTIME_DIR / "environments"
TMP_DIR = RUNTIME_DIR / "tmp"
FILES_DIR = DATA_DIR / "files"
try:
    RUNTIME_DIR.absolute().relative_to(PROJECT_ROOT)
except ValueError as exc:
    raise RuntimeError("AGENT_BUILDER_RUNTIME_DIR must stay inside this checkout") from exc
BLOCKING_WORK_LIMIT = max(
    1, min(int(os.environ.get("AGENT_BUILDER_BLOCKING_WORKERS", "2")), 4)
)
blocking_work_semaphore = asyncio.Semaphore(BLOCKING_WORK_LIMIT)


async def run_blocking_work(function: Callable[..., Any], *args: Any) -> Any:
    """Run a synchronous mutation without releasing capacity before it exits.

    Cancelling ``asyncio.to_thread`` only cancels its Future, not the underlying
    thread. Shield and drain the real worker before propagating cancellation so
    callers cannot accumulate invisible writers after a timeout or disconnect.
    """

    return await run_blocking_with_semaphore(
        blocking_work_semaphore, function, *args
    )

# Recursive mkdir would follow an attacker-precreated data/runtime symlink.
try:
    DATA_DIR = ensure_real_directory(DATA_DIR)
    RUNTIME_DIR = ensure_real_directory(RUNTIME_DIR)
    ENVIRONMENTS_DIR = ensure_real_directory(ENVIRONMENTS_DIR)
    TMP_DIR = ensure_real_directory(TMP_DIR)
    FILES_DIR = ensure_real_directory(FILES_DIR)
except UnsafeStoragePathError as exc:
    raise RuntimeError("Project storage path is unsafe") from exc

mcp_registry = MCPServiceRegistry(DATA_DIR)
skill_registry = SkillRegistry(DATA_DIR, SKILLS_DIR)
model_service_registry = ModelServiceRegistry(DATA_DIR)
conversation_manager = ConversationManager(DATA_DIR)

# 新增：初始化环境、文件、执行管理器（需要在AgentManager之前初始化）
environment_manager = EnvironmentManager(DATA_DIR, ENVIRONMENTS_DIR)
environment_creator = EnvironmentCreator(environment_manager, max_concurrent=3)
file_storage_manager = FileStorageManager(FILES_DIR)
execution_engine = ExecutionEngine(environment_manager, file_storage_manager, DATA_DIR)
# 新增：RAG 知识库管理器 (AC130-202603161542)
from src.knowledge_base_manager import KnowledgeBaseManager
from src.embedder import Embedder
embedder = Embedder()  # 【AC130-202603170949】向量化器
kb_manager = KnowledgeBaseManager(DATA_DIR, embedder=embedder)

# 初始化 AgentManager，传入 execution_engine、kb_manager 和 embedder
manager = AgentManager(
    DATA_DIR,
    mcp_registry,
    skill_registry,
    model_service_registry=model_service_registry,
    execution_engine=execution_engine,
    kb_manager=kb_manager,  # 【AC130-202603161542】
    embedder=embedder       # 【AC130-202603170949】
)

# Built-in subprocesses are started by the FastAPI lifecycle, never at module
# import.  This keeps CLI tooling, test discovery and ASGI inspection free of
# hidden child processes.
registered_services: List[str] = []

app = FastAPI(title="Agent Builder API")

# CORS is deliberately limited to the local frontend by default. Additional
# origins must be explicitly configured with AGENT_BUILDER_CORS_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def authenticate_api_request(request: Request, call_next):
    """Fail closed for every management/execution endpoint under /api."""
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        # CORS preflight carries no application credentials; CORSMiddleware
        # still validates the requesting origin before answering it.
        if request.method != "OPTIONS":
            try:
                authenticate_api_headers(request.headers)
            except APIAuthenticationError as exc:
                headers = {"Cache-Control": "no-store"}
                if exc.status_code == 401:
                    headers["WWW-Authenticate"] = "Bearer"
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=headers,
                )
    return await call_next(request)


# Enforce limits at the ASGI receive boundary so multipart and chunked bodies
# cannot be fully spooled before an endpoint gets a chance to reject them.
app.add_middleware(RequestBodyLimitMiddleware)


def _security_error(_exc: SecurityValidationError) -> HTTPException:
    """Translate internal validation failures without leaking implementation details."""
    return HTTPException(status_code=400, detail="请求未通过安全校验")


@app.on_event("startup")
async def startup_event():
    """应用启动事件"""
    global registered_services
    if not registered_services:
        registered_services = setup_builtin_services(mcp_registry)
    print("\n" + "=" * 50)
    print("🚀 Agent Builder 启动完成")
    print(f"   预置 MCP 服务: {registered_services}")
    print("=" * 50)

    print("=" * 50 + "\n")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    print("\n" + "=" * 50)
    print("🛑 Agent Builder 正在关闭...")
    print("=" * 50)

    # Stop background environment creation before shutting down agents.
    await environment_creator.shutdown()
    print("✓ 环境创建任务已关闭")

    await environment_manager.shutdown()
    print("✓ 受管 uv/Skill 子进程已关闭")

    # 关闭所有 Agent 实例
    await manager.shutdown_all()
    print("✓ 所有 Agent 实例已关闭")

    # 关闭预置服务
    shutdown_builtin_services()
    registered_services.clear()
    print("✓ 预置 MCP 服务已关闭")

    # Flush and close the process-wide vendor-neutral tracer last so shutdown
    # spans from other components are still exportable.
    try:
        tracer = get_tracer()
        tracer.force_flush()
        await tracer.shutdown()
        print("✓ 可观测链路已刷新并关闭")
    except Exception as exc:
        print(f"✗ 可观测链路关闭失败: {type(exc).__name__}")

    print("=" * 50 + "\n")


# === 请求/响应模型 ===

class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=100_000)
    # 【AC130-202603141800】支持创建时指定子Agent
    model_service: Optional[str] = Field(default=None, max_length=100)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_iterations: int = Field(default=10, ge=1, le=50)
    short_term_memory: int = Field(default=5, ge=0, le=50)
    planning_mode: PlanningMode = PlanningMode.REACT
    mcp_services: List[str] = Field(default_factory=list, max_length=100)
    skills: List[str] = Field(default_factory=list, max_length=100)
    sub_agents: List[str] = Field(default_factory=list, max_length=50)
    sub_agent_timeout: int = Field(default=60, ge=10, le=300)
    sub_agent_max_retries: int = Field(default=1, ge=0, le=3)
    sub_agent_max_concurrent: int = Field(default=3, ge=1, le=10)
    # 【AC130-202603170949】RAG 知识库配置
    knowledge_bases: List[str] = Field(default_factory=list, max_length=100)
    retrieval_config: Optional[Dict[str, Any]] = None


class UpdateAgentRequest(BaseModel):
    persona: str = Field(max_length=100_000)
    model_service: Optional[str] = Field(default=None, max_length=100)  # 新版：引用模型服务
    # 旧字段已废弃，但保留用于向后兼容
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_iterations: int = Field(default=10, ge=1, le=50)
    short_term_memory: int = Field(default=5, ge=0, le=50)
    planning_mode: PlanningMode = PlanningMode.REACT
    mcp_services: List[str] = Field(default_factory=list, max_length=100)
    skills: List[str] = Field(default_factory=list, max_length=100)
    # ====================================================================
    # 【AC130-202603142210】Agent-as-a-Tool: 子Agent字段
    # ====================================================================
    sub_agents: List[str] = Field(default_factory=list, max_length=50)
    sub_agent_timeout: int = Field(default=60, ge=10, le=300)
    sub_agent_max_retries: int = Field(default=1, ge=0, le=3)
    sub_agent_max_concurrent: int = Field(default=3, ge=1, le=10)
    # ====================================================================
    # 【AC130-202603170949】RAG 知识库配置
    # ====================================================================
    knowledge_bases: List[str] = Field(default_factory=list, max_length=100)
    retrieval_config: Optional[Dict[str, Any]] = None


class CreateModelServiceRequest(BaseModel):
    """创建模型服务请求"""
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2_000)
    provider: str = Field(max_length=50)  # zhipu / alibaba_bailian / ollama
    base_url: str = Field(max_length=2_048)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    selected_model: str = Field(max_length=200)
    available_models: List[str] = Field(default_factory=list, max_length=500)
    enabled: bool = True


class UpdateModelServiceRequest(BaseModel):
    """更新模型服务请求"""
    description: str = Field(default="", max_length=2_000)
    provider: str = Field(max_length=50)
    base_url: str = Field(max_length=2_048)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    selected_model: str = Field(max_length=200)
    available_models: List[str] = Field(default_factory=list, max_length=500)
    enabled: bool = True


class TestModelServiceRequest(BaseModel):
    """测试模型服务连接请求"""
    provider: str = Field(max_length=50)
    base_url: str = Field(max_length=2_048)
    api_key: Optional[str] = Field(default=None, max_length=16_384)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=200_000)
    history: List[Dict[str, str]] = Field(default_factory=list, max_length=200)
    file_ids: List[str] = Field(default_factory=list, max_length=50)
    conversation_id: Optional[str] = Field(default=None, max_length=100)


class CreateMCPServiceRequest(BaseModel):
    """创建MCP服务请求"""
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2_000)
    connection_type: str = Field(default="stdio", max_length=16)  # stdio / sse
    # stdio 配置
    command: Optional[str] = Field(default=None, max_length=1_024)
    args: List[str] = Field(default_factory=list, max_length=128)
    env: Dict[str, str] = Field(default_factory=dict, max_length=128)
    # SSE 配置
    url: Optional[str] = Field(default=None, max_length=2_048)
    auth_type: str = Field(default="none", max_length=16)  # none / bearer / apikey
    auth_value: Optional[str] = Field(default=None, max_length=16_384)
    headers: Dict[str, str] = Field(default_factory=dict, max_length=64)
    enabled: bool = True


class UpdateMCPServiceRequest(BaseModel):
    """更新MCP服务请求"""
    description: str = Field(default="", max_length=2_000)
    connection_type: str = Field(default="stdio", max_length=16)
    command: Optional[str] = Field(default=None, max_length=1_024)
    args: List[str] = Field(default_factory=list, max_length=128)
    env: Dict[str, str] = Field(default_factory=dict, max_length=128)
    url: Optional[str] = Field(default=None, max_length=2_048)
    auth_type: str = Field(default="none", max_length=16)
    auth_value: Optional[str] = Field(default=None, max_length=16_384)
    headers: Dict[str, str] = Field(default_factory=dict, max_length=64)
    enabled: bool = True


class AgentResponse(BaseModel):
    name: str
    description: str
    llm_provider: str
    llm_model: str
    created_at: str


def _validate_resource_name(name: str, label: str) -> str:
    candidate = name.strip() if isinstance(name, str) else ""
    if (
        not candidate
        or len(candidate) > 100
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or any(ord(char) < 32 for char in candidate)
        or re.fullmatch(r"[\w .-]+", candidate, flags=re.UNICODE) is None
    ):
        raise HTTPException(status_code=400, detail=f"{label}无效")
    return candidate


async def _validate_mcp_payload(req) -> MCPConnectionType:
    try:
        connection_type = MCPConnectionType(req.connection_type)
        if req.auth_value is not None and len(req.auth_value) > 16_384:
            raise SecurityValidationError("MCP authentication value is too long")
        validate_headers(req.headers)
        if connection_type == MCPConnectionType.STDIO:
            validate_stdio_configuration(req.command, req.args, req.env)
        else:
            if not req.url:
                raise SecurityValidationError("SSE MCP URL is required")
            await validate_outbound_url(req.url)
    except (ValueError, SecurityValidationError) as exc:
        raise _security_error(
            exc if isinstance(exc, SecurityValidationError) else SecurityValidationError(str(exc))
        ) from exc
    return connection_type


async def _validate_model_service_url(base_url: str) -> str:
    try:
        return await validate_outbound_url(base_url)
    except SecurityValidationError as exc:
        raise _security_error(exc) from exc


def _restore_redacted_mapping(existing: Dict[str, str], submitted: Dict[str, str]) -> Dict[str, str]:
    return {
        key: existing[key] if value == "***" and key in existing else value
        for key, value in submitted.items()
    }


def _restore_redacted_arguments(existing: List[str], submitted: List[str]) -> List[str]:
    restored: List[str] = []
    for index, value in enumerate(submitted):
        if value == "***" and index < len(existing):
            restored.append(existing[index])
        elif value.endswith("=***") and index < len(existing):
            key = value[:-3]
            if existing[index].startswith(key):
                restored.append(existing[index])
            else:
                restored.append(value)
        else:
            restored.append(value)
    return restored


# === API 路由 ===

# === 系统级 API ===

@app.get("/api/system/check-runtime")
async def check_runtime():
    """
    检测项目内 uv/Python 运行时是否可用

    Returns:
        {
            "available": bool,           # uv 是否可用
            "path": str | None,          # uv 可执行文件路径
            "version": str | None,       # uv 版本
            "error": str | None,         # 错误代码
            "message": str               # 用户友好的消息
        }
    """
    result = await EnvironmentManager.check_runtime_available()
    return result


@app.get("/api/system/observability")
async def observability_status():
    """Return the active vendor-neutral tracing backend status."""
    return get_observability_status()


@app.get("/api/agents")
async def list_agents():
    """获取所有 Agent 列表"""
    agents = []
    for name in manager.list_agents():
        config = manager.get_config(name)
        if config:
            # 获取模型服务信息
            model_service_name = config.model_service
            model_info = ""
            if model_service_name:
                service = model_service_registry.get_service(model_service_name)
                if service:
                    model_info = f"{service.provider.value}: {service.selected_model}"

            agents.append({
                "name": name,
                "description": config.persona[:100] + "..." if len(config.persona) > 100 else config.persona,
                "model_service": model_service_name,
                "model_info": model_info,
                "llm_provider": config.llm_provider.value if config.llm_provider else None,
                "llm_model": config.llm_model,
                "created_at": "已保存"
            })
    return {"agents": agents}


@app.post("/api/agents")
async def create_agent(req: CreateAgentRequest):
    """创建新 Agent - 异步环境创建版本

    创建智能体后立即返回，环境在后台异步创建。
    前端应轮询 GET /api/agents/{name}/environment 获取环境状态。
    """
    agent_name = _validate_resource_name(req.name, "Agent名称")

    if agent_name in manager.list_agents():
        raise HTTPException(status_code=400, detail="Agent名称已存在")
    if len(manager.list_agents()) >= manager.MAX_AGENT_CONFIGS:
        raise HTTPException(status_code=409, detail="Agent 数量已达到 100 个上限")

    # 【AC130-202603141800】创建时进行循环检测
    if req.sub_agents:
        # 获取所有现有Agent（不包括正在创建的）
        all_agent_names = list(manager.list_agents())

        # 构建调用图
        configs = {}
        for existing_agent_name in all_agent_names:
            config = manager.get_config(existing_agent_name)
            if config:
                configs[existing_agent_name] = getattr(config, 'sub_agents', []) or []

        # 添加正在创建的Agent
        configs[agent_name] = req.sub_agents

        # 检测循环
        detector = CycleDetector(all_agent_names + [agent_name])
        detector.build_from_configs(configs)

        cycle_result = detector.validate_config(agent_name, req.sub_agents)
        if cycle_result.has_cycle:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "循环依赖检测失败",
                    "message": cycle_result.message,
                    "cycle_path": cycle_result.cycle_path
                }
            )

    config = AgentConfig(
        name=agent_name,
        persona=req.description or "你是一个有帮助的AI助手。",
        model_service=req.model_service,
        temperature=req.temperature,
        max_iterations=req.max_iterations,
        short_term_memory=req.short_term_memory,
        planning_mode=req.planning_mode,
        mcp_services=req.mcp_services,
        skills=req.skills,
        # 【AC130-202603141800】子Agent字段
        sub_agents=req.sub_agents,
        sub_agent_timeout=req.sub_agent_timeout,
        sub_agent_max_retries=req.sub_agent_max_retries,
        sub_agent_max_concurrent=req.sub_agent_max_concurrent,
        # 【AC130-202603170949】RAG知识库字段
        knowledge_bases=req.knowledge_bases,
        retrieval_config=req.retrieval_config
    )

    if manager.create_agent_config(config):
        # create() only registers the managed background task and returns
        # immediately. Awaiting registration closes the create/delete race;
        # EnvironmentManager writes CREATING metadata inside its writer gate.
        await environment_creator.create(agent_name)

        print(f"[AGENT] 已创建智能体 {agent_name}，后台环境创建任务已启动")

        # 立即返回
        return {
            "success": True,
            "name": agent_name,
            "environment_status": "creating"
        }
    else:
        raise HTTPException(status_code=500, detail="创建失败")


@app.get("/api/agents/call-graph")
async def get_global_call_graph():
    """获取系统中所有 Agent 之间的调用关系。"""
    all_agent_names = list(manager.list_agents())
    configs = {}
    for existing_agent_name in all_agent_names:
        config = manager.get_config(existing_agent_name)
        if config:
            configs[existing_agent_name] = getattr(config, "sub_agents", []) or []

    detector = CycleDetector(all_agent_names)
    detector.build_from_configs(configs)
    cycle_result = detector.detect_cycle()
    return {
        "call_graph": detector.get_call_graph().to_dict(),
        "has_cycle": cycle_result.has_cycle,
        "cycle_message": cycle_result.message if cycle_result.has_cycle else None,
    }


@app.get("/api/agents/{name}")
async def get_agent(name: str):
    """获取 Agent 详情"""
    config = manager.get_config(name)
    if not config:
        raise HTTPException(status_code=404, detail="Agent不存在")

    return {
        "name": name,
        "persona": config.persona,
        "model_service": config.model_service,
        "llm_provider": config.llm_provider.value if config.llm_provider else None,
        "llm_model": config.llm_model,
        "llm_base_url": config.llm_base_url,
        "temperature": config.temperature,
        "max_iterations": config.max_iterations,
        "short_term_memory": config.short_term_memory,
        "planning_mode": config.planning_mode.value,
        "mcp_services": config.mcp_services,
        "skills": config.skills,
        # 【AC130-202603141800】返回子Agent配置
        "sub_agents": getattr(config, 'sub_agents', []) or [],
        "sub_agent_timeout": getattr(config, 'sub_agent_timeout', 60),
        "sub_agent_max_retries": getattr(config, 'sub_agent_max_retries', 1),
        "sub_agent_max_concurrent": getattr(config, 'sub_agent_max_concurrent', 3),
        # 【AC130-202603170949】返回RAG知识库配置
        "knowledge_bases": getattr(config, 'knowledge_bases', []) or [],
        "retrieval_config": getattr(config, 'retrieval_config', None)
    }


@app.put("/api/agents/{name}")
async def update_agent(name: str, req: UpdateAgentRequest):
    """更新 Agent 配置"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # ====================================================================
    # 【AC130-202603142210】循环检测：保存前验证子Agent配置
    # ====================================================================
    if req.sub_agents:
        # 获取所有Agent名称
        all_agent_names = list(manager.list_agents())

        # 构建当前调用图（不包括正在更新的Agent）
        configs = {}
        for agent_name in all_agent_names:
            if agent_name != name:  # 排除当前正在更新的Agent
                config = manager.get_config(agent_name)
                if config:
                    configs[agent_name] = getattr(config, 'sub_agents', []) or []

        # 添加正在更新的Agent的新配置
        configs[name] = req.sub_agents

        # 检测循环
        detector = CycleDetector(all_agent_names)
        detector.build_from_configs(configs)

        cycle_result = detector.validate_config(name, req.sub_agents)
        if cycle_result.has_cycle:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "循环依赖检测失败",
                    "message": cycle_result.message,
                    "cycle_path": cycle_result.cycle_path
                }
            )

    # 获取现有配置以保留未提供的字段
    existing_config = manager.get_config(name)

    config = AgentConfig(
        name=name,
        persona=req.persona,
        model_service=req.model_service,
        # 旧字段不再使用，设为None
        llm_provider=None,
        llm_model=None,
        llm_base_url=None,
        temperature=req.temperature,
        max_iterations=req.max_iterations,
        short_term_memory=req.short_term_memory,
        planning_mode=req.planning_mode,
        mcp_services=req.mcp_services,
        skills=req.skills,
        # 【AC130-202603142210】子Agent字段
        sub_agents=req.sub_agents,
        sub_agent_timeout=req.sub_agent_timeout,
        sub_agent_max_retries=req.sub_agent_max_retries,
        sub_agent_max_concurrent=req.sub_agent_max_concurrent,
        # 【AC130-202603170949】RAG知识库字段
        knowledge_bases=req.knowledge_bases,
        retrieval_config=req.retrieval_config
    )

    if manager.update_agent_config(name, config):
        return {"success": True}
    else:
        raise HTTPException(status_code=500, detail="保存失败")


@app.delete("/api/agents/{name}")
async def delete_agent(name: str):
    """删除 Agent"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # Stop creation and in-flight execution before removing directories.  This
    # prevents a late background task from recreating an environment after the
    # Agent has been deleted.
    await environment_creator.cancel(name)
    if manager.delete_agent_config(name):
        await manager.ensure_agent_stopped(name)
        cleanup_complete = True
        # 清理关联资源（环境和文件）
        try:
            # 清理该 Agent 的项目内 Python 环境
            await environment_manager.delete_environment(name)
            print(f"[AGENT] 已删除 {name} 的执行环境")
        except Exception as e:
            print(f"[AGENT] 删除环境失败: error_type={type(e).__name__}")
            cleanup_complete = False

        try:
            # 清理上传的文件
            await file_storage_manager.cleanup_agent_files(name)
            print(f"[AGENT] 已清理 {name} 的上传文件")
        except Exception as e:
            print(f"[AGENT] 清理文件失败: error_type={type(e).__name__}")
            cleanup_complete = False

        try:
            await asyncio.to_thread(
                conversation_manager.delete_agent_conversations, name
            )
            print(f"[AGENT] 已清理 {name} 的会话")
        except Exception as e:
            print(f"[AGENT] 清理会话失败: error_type={type(e).__name__}")
            cleanup_complete = False

        if not await execution_engine.cleanup_agent_executions(name):
            print(f"[AGENT] 清理执行记录失败: {name}")
            cleanup_complete = False

        return {"success": True, "cleanup_complete": cleanup_complete}
    else:
        raise HTTPException(status_code=404, detail="Agent不存在")


# ========================================================================
# 【AC130-202603142210】Agent-as-a-Tool: 子Agent相关API端点
# ========================================================================

class ValidateSubAgentsRequest(BaseModel):
    """验证子Agent配置请求"""
    sub_agents: List[str] = []


class ValidateSubAgentsResponse(BaseModel):
    """验证子Agent配置响应"""
    valid: bool
    message: str = ""
    cycle_path: List[str] = []


@app.get("/api/agents/{name}/call-graph")
async def get_agent_call_graph(name: str):
    """获取Agent的调用关系图

    返回当前Agent调用其他Agent的关系，以及被哪些Agent调用。
    """
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # 获取所有Agent配置
    all_agent_names = list(manager.list_agents())

    # 构建调用图
    configs = {}
    for agent_name in all_agent_names:
        config = manager.get_config(agent_name)
        if config:
            configs[agent_name] = getattr(config, 'sub_agents', []) or []

    # 创建循环检测器并构建调用图
    detector = CycleDetector(all_agent_names)
    detector.build_from_configs(configs)

    # 获取调用图
    call_graph = detector.get_call_graph()

    # 获取特定Agent的摘要
    summary = detector.get_agent_summary(name)

    return {
        "call_graph": call_graph.to_dict(),
        "agent_summary": summary
    }


@app.post("/api/agents/{name}/sub-agents/validate", response_model=ValidateSubAgentsResponse)
async def validate_sub_agents(name: str, req: ValidateSubAgentsRequest):
    """验证子Agent配置（循环依赖检测）

    在保存Agent配置前调用此端点验证子Agent配置是否会导致循环依赖。
    """
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # 获取所有Agent名称
    all_agent_names = list(manager.list_agents())

    # 构建当前调用图（不包括正在验证的Agent）
    configs = {}
    for agent_name in all_agent_names:
        if agent_name != name:
            config = manager.get_config(agent_name)
            if config:
                configs[agent_name] = getattr(config, 'sub_agents', []) or []

    # 添加正在验证的Agent的新配置
    configs[name] = req.sub_agents

    # 检测循环
    detector = CycleDetector(all_agent_names)
    detector.build_from_configs(configs)

    cycle_result = detector.validate_config(name, req.sub_agents)

    return ValidateSubAgentsResponse(
        valid=not cycle_result.has_cycle,
        message=cycle_result.message or "配置有效，无循环依赖",
        cycle_path=cycle_result.cycle_path
    )


@app.post("/api/agents/{name}/chat")
async def chat_with_agent(name: str, req: ChatRequest):
    """与 Agent 对话"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    instance = await manager.get_instance(name)
    if not instance:
        raise HTTPException(status_code=500, detail="无法加载Agent")

    try:
        response = await instance.chat(req.message, req.history)
        return {"response": response}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Agent 请求处理失败") from exc


# ============================================================================
# 【流式输出 SSE 端点 - 谨慎修改】
#
# 此端点是流式对话的核心入口，将 AgentEngine.stream() 的 yield 事件
# 转换为 SSE (Server-Sent Events) 格式发送到前端。
#
# 关键实现：
# 1. 使用 StreamingResponse 返回 text/event-stream
# 2. 禁用所有缓冲（Cache-Control, X-Accel-Buffering）
# 3. 事件格式: data: {"type": "...", "content": "..."}\n\n
#
# ⚠️ 修改此端点可能影响：
# - 流式输出的实时性
# - 前端打字机效果
# - SSE 连接稳定性
#
# 相关文件：
# - src/agent_engine.py: stream() - 事件生成
# - frontend/src/app/stream/agents/[name]/chat/route.ts - 前端代理
# - frontend/src/components/AgentChat.tsx - 前端渲染
# ============================================================================
@app.post("/api/agents/{name}/chat/stream")
async def chat_stream(name: str, req: ChatRequest, request: Request):
    """流式对话 - 支持返回 thinking、工具调用和最终回答

    【AC130-202603150000】增强异常处理 - 添加结构化日志和错误事件

    【流式输出核心端点 - 谨慎修改】
    使用 SSE (Server-Sent Events) 协议实现流式传输。
    """
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # ============================================================================
    # 【AC130-202603150000】初始化日志记录器
    # 从请求头或自动生成 request_id
    # ============================================================================
    from src.stream_logger import get_logger, cleanup_old_logs
    supplied_request_id = request.headers.get("x-request-id", "")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", supplied_request_id):
        request_id = supplied_request_id
    else:
        request_id = f"stream-{uuid.uuid4().hex[:16]}"
    logger = get_logger(request_id)

    async def generate():
        import time
        start_time = time.time()
        first_token_time = None
        token_count = 0
        request_completed = False

        # ============================================================================
        # 【AC130-202603150000】记录请求开始
        # ============================================================================
        logger.log_event("request_start", {
            "agent_name": name,
            "message_length": len(req.message),
            "history_count": len(req.history) if req.history else 0,
            "file_count": len(req.file_ids) if req.file_ids else 0,
            "conversation_id": req.conversation_id,
            "request_id": request_id,
        })

        try:
            instance = await manager.get_instance(name)
            instance_ready_time = time.time()
            logger.log_event("agent_loaded", {
                "agent_name": name,
                "load_time_ms": round((instance_ready_time - start_time) * 1000, 2)
            })
            print(f"[METRICS] get_instance 耗时: {(instance_ready_time - start_time) * 1000:.0f}ms")

            if not instance:
                error_msg = "无法加载Agent"
                logger.log_error("AgentLoadError", error_msg)
                yield _error_event(error_msg)
                return

            # 构建文件上下文（增强版，包含 file_id 表格和调用示例）
            file_context = ""
            file_ids_list = []
            if req.file_ids:
                try:
                    files = await file_storage_manager.list_files(name)
                    matched_files = [f for f in files if f.file_id in req.file_ids]
                    if matched_files:
                        file_context = "\n\n=== 用户上传的文件 ===\n\n"
                        file_context += "| file_id | 文件名 | 类型 | 大小 |\n"
                        file_context += "|---------|--------|------|------|\n"
                        for f in matched_files:
                            file_ids_list.append(f.file_id)
                            size_kb = f.file_size / 1024
                            file_context += f"| {f.file_id} | {f.filename} | {f.mime_type} | {size_kb:.1f}KB |\n"

                        file_context += "\n**重要提示**:\n"
                        file_context += "1. 调用 execute_skill 工具时，请使用上述 file_id 作为 input_file_ids 参数\n"
                        file_context += "2. 文件会被自动放置在脚本的 ./input/ 目录下\n"
                        file_context += "3. 根据文件类型选择对应的 Skill：PDF 文件用 AB-pdf，Word 文档用 AB-docx\n"

                        # 生成调用示例
                        if matched_files:
                            first_file = matched_files[0]
                            file_id_str = '", "'.join(file_ids_list)
                            if first_file.mime_type == "application/pdf":
                                file_context += "\n**调用示例**:\n"
                                file_context += '```json\n'
                                file_context += f'{{"tool": "execute_skill", "arguments": {{"skill_name": "AB-pdf", "input_file_ids": ["{file_id_str}"], "arguments": ["./input/{first_file.filename}", "--action", "extract_text"]}}}}\n'
                                file_context += '```\n'
                            elif "word" in first_file.mime_type or first_file.filename.endswith('.docx'):
                                file_context += "\n**调用示例**:\n"
                                file_context += '```json\n'
                                file_context += f'{{"tool": "execute_skill", "arguments": {{"skill_name": "AB-docx", "input_file_ids": ["{file_id_str}"], "arguments": ["./input/{first_file.filename}", "--action", "extract_text"]}}}}\n'
                                file_context += '```\n'

                        logger.log_event("files_loaded", {
                            "file_count": len(matched_files),
                            "file_ids": file_ids_list
                        })
                except Exception as e:
                    logger.log_error("FileLoadError", "文件信息加载失败")
                    print(f"[WARN] 获取文件信息失败: error_type={type(e).__name__}")

            logger.log_event("llm_call_start", {
                "message_length": len(req.message)
            })

            # ============================================================================
            # 【AC130-202603150000】增强异常处理 - 捕获 LLM 调用异常
            # ============================================================================
            try:
                # Group trace spans by conversation when one is available.
                session_id = req.conversation_id or f"anon-{uuid.uuid4().hex[:8]}"

                async for event in instance.chat_stream(
                    req.message,
                    req.history,
                    file_context,
                    trace_id=request_id,
                    conversation_id=session_id,
                ):
                    # 记录 SSE 事件类型（用于调试）
                    event_type = event.get('type', 'unknown')
                    logger.log_sse_event(event_type)

                    # 记录第一个 token 时间
                    if first_token_time is None and event_type in ['content', 'thinking']:
                        first_token_time = time.time()
                        first_token_latency_ms = (first_token_time - start_time) * 1000
                        logger.log_event("first_token", {
                            "latency_ms": round(first_token_latency_ms, 2)
                        })
                        print(f"[METRICS] 首 Token 时延: {first_token_latency_ms:.0f}ms")

                    # 统计 token 数量（基于内容长度估算）
                    if event_type == 'content' and event.get('content'):
                        # 中文约 1.5 字符/token，英文约 4 字符/token，取中值估算
                        content = event.get('content', '')
                        # 检测是否主要是中文
                        chinese_chars = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
                        if chinese_chars > len(content) * 0.3:
                            token_count += int(len(content) / 1.5)
                        else:
                            token_count += len(content) // 4

                    if event_type == 'thinking' and event.get('content'):
                        content = event.get('content', '')
                        chinese_chars = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
                        if chinese_chars > len(content) * 0.3:
                            token_count += int(len(content) / 1.5)
                        else:
                            token_count += len(content) // 4

                    # 检测是否为错误事件（由 AgentEngine 生成）
                    if event_type == 'error':
                        logger.log_error("LLMStreamError", event.get('content', 'Unknown error'))

                    # event 是一个字典，包含 type 和其他字段
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                request_completed = True

            except asyncio.TimeoutError:
                # ============================================================================
                # 【AC130-202603150000】超时异常处理
                # ============================================================================
                logger.log_error("TimeoutError", "请求超时")
                yield _error_event("请求超时，请稍后重试")
                return

            except Exception as e:
                # ============================================================================
                # 【AC130-202603150000】LLM 流式输出异常处理
                # ============================================================================
                error_type = type(e).__name__
                logger.log_error(error_type, "流式处理失败")

                # 发送结构化错误事件
                yield _error_event("处理请求时发生错误，请查看服务端日志")
                return

            finally:
                # ============================================================================
                # 【AC130-202603150000】确保发送性能指标
                # ============================================================================
                end_time = time.time()
                total_duration = end_time - start_time
                first_token_latency = (first_token_time - start_time) if first_token_time else total_duration

                # ====================================================================
                # 【上下文窗口状态栏】获取 token 使用信息
                # ====================================================================
                input_tokens = 0
                output_tokens = 0
                context_window = 0

                try:
                    if instance and hasattr(instance, 'get_token_usage'):
                        token_usage = instance.get_token_usage()
                        input_tokens = token_usage.get('input_tokens', 0)
                        output_tokens = token_usage.get('output_tokens', 0)

                    # 获取模型名称和上下文窗口大小
                    if instance and hasattr(instance, 'config') and instance.config:
                        model_service_name = getattr(instance.config, 'model_service', None)
                        if model_service_name:
                            service = model_service_registry.get_service(model_service_name)
                            if service:
                                context_window = get_context_window_size(service.selected_model)
                except Exception as e:
                    print(f"[WARN] 获取 token 使用信息失败: error_type={type(e).__name__}")

                metrics = {
                    'type': 'metrics',
                    'first_token_latency': round(first_token_latency * 1000, 0),  # 毫秒
                    'total_tokens': token_count,
                    'total_duration': round(total_duration * 1000, 0),  # 毫秒
                    # 【上下文窗口状态栏】新增字段
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'context_window': context_window,
                }
                logger.log_event("metrics", metrics)
                yield f"data: {json.dumps(metrics, ensure_ascii=False)}\n\n"

                # 记录请求结束
                logger.log_event("request_end", {
                    "status": "completed" if request_completed else "interrupted",
                    "duration_ms": round(total_duration * 1000, 2),
                    "token_count": token_count
                })

        except Exception as e:
            # ============================================================================
            # 【AC130-202603150000】端点级异常处理
            # ============================================================================
            logger.log_error("EndpointError", "端点处理失败")
            yield _error_event("端点处理错误，请查看服务端日志")
        finally:
            # ============================================================================
            # 【AC130-202603150000】清理旧日志
            # ============================================================================
            cleanup_old_logs()

    # 添加防止缓冲的headers
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用nginx缓冲
            "X-Request-ID": request_id,  # 【AC130-202603150000】添加请求ID到响应头
        }
    )


def _error_event(message: str) -> str:
    """生成错误 SSE 事件

    【AC130-202603150000】辅助函数 - 生成标准错误事件

    Args:
        message: 错误消息

    Returns:
        SSE 格式的错误事件字符串
    """
    return f'data: {json.dumps({"type": "error", "content": message}, ensure_ascii=False)}\n\n'


# === MCP 服务 API ===

@app.get("/api/mcp-services")
async def list_mcp_services():
    """获取所有 MCP 服务列表"""
    services = mcp_registry.list_services()
    return {
        "services": [
            {
                "name": s.name,
                "description": s.description,
                "connection_type": s.connection_type.value,
                "enabled": s.enabled,
                "created_at": s.created_at,
                "updated_at": s.updated_at
            }
            for s in services
        ]
    }


@app.post("/api/mcp-services")
async def create_mcp_service(req: CreateMCPServiceRequest):
    """创建 MCP 服务"""
    name = _validate_resource_name(req.name, "服务名称")
    connection_type = await _validate_mcp_payload(req)

    if mcp_registry.service_exists(name):
        raise HTTPException(status_code=400, detail="服务名称已存在")

    try:
        config = MCPServiceConfig(
            name=name,
            description=req.description[:2_000],
            connection_type=connection_type,
            command=req.command,
            args=req.args,
            env=req.env,
            url=req.url,
            auth_type=MCPAuthType(req.auth_type),
            auth_value=req.auth_value,
            headers=req.headers,
            enabled=req.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="MCP认证类型无效") from exc

    if await run_blocking_work(mcp_registry.create_service, config):
        return {"success": True, "name": name}
    else:
        raise HTTPException(status_code=500, detail="创建失败")


@app.get("/api/mcp-services/{name}")
async def get_mcp_service(name: str):
    """获取 MCP 服务详情"""
    service = mcp_registry.get_service(name)
    if not service:
        raise HTTPException(status_code=404, detail="服务不存在")

    return {
        "name": service.name,
        "description": service.description,
        "connection_type": service.connection_type.value,
        "command": service.command,
        "args": redact_arguments(service.args),
        "env": redact_mapping(service.env),
        "url": service.url,
        "auth_type": service.auth_type.value if service.auth_type else "none",
        "auth_value": "***" if service.auth_value else None,  # 隐藏敏感信息
        "headers": redact_mapping(service.headers),
        "enabled": service.enabled,
        "created_at": service.created_at,
        "updated_at": service.updated_at
    }


@app.put("/api/mcp-services/{name}")
async def update_mcp_service(name: str, req: UpdateMCPServiceRequest):
    """更新 MCP 服务配置"""
    _validate_resource_name(name, "服务名称")
    if is_builtin_service_name(name):
        raise HTTPException(status_code=403, detail="预置服务不能修改")
    existing = mcp_registry.get_service(name)
    if not existing:
        raise HTTPException(status_code=404, detail="服务不存在")

    req.args = _restore_redacted_arguments(existing.args, req.args)
    req.env = _restore_redacted_mapping(existing.env, req.env)
    req.headers = _restore_redacted_mapping(existing.headers, req.headers)
    connection_type = await _validate_mcp_payload(req)
    try:
        config = MCPServiceConfig(
            name=name,
            description=req.description[:2_000],
            connection_type=connection_type,
            command=req.command,
            args=req.args,
            env=req.env,
            url=req.url,
            auth_type=MCPAuthType(req.auth_type),
            # 如果 auth_value 为空，保留原有的
            auth_value=(
                existing.auth_value
                if req.auth_value in {None, "", "***"}
                else req.auth_value
            ),
            headers=req.headers,
            enabled=req.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="MCP认证类型无效") from exc

    if await run_blocking_work(mcp_registry.update_service, name, config):
        return {"success": True}
    else:
        raise HTTPException(status_code=500, detail="保存失败")


@app.delete("/api/mcp-services/{name}")
async def delete_mcp_service(name: str):
    """删除 MCP 服务"""
    # 检查是否为预置服务
    if is_builtin_service_name(name):
        raise HTTPException(status_code=403, detail="预置服务不能删除")

    if await run_blocking_work(mcp_registry.delete_service, name):
        return {"success": True}
    else:
        raise HTTPException(status_code=404, detail="服务不存在")


@app.post("/api/mcp-services/{name}/test")
async def test_mcp_service_connection(name: str):
    """测试 MCP 服务连接"""
    service = mcp_registry.get_service(name)
    if not service:
        raise HTTPException(status_code=404, detail="服务不存在")

    result = await test_mcp_connection(service)
    return result


@app.get("/api/mcp-services/{name}/tools")
async def get_mcp_service_tools(name: str):
    """获取 MCP 服务的工具列表"""
    service = mcp_registry.get_service(name)
    if not service:
        raise HTTPException(status_code=404, detail="服务不存在")

    # 测试连接并获取工具列表
    result = await test_mcp_connection(service)
    if result["success"]:
        return {"tools": result["tools"]}
    else:
        return {"tools": [], "error": result["error"]}


@app.post("/api/mcp-services/{name}/diagnose")
async def diagnose_mcp_service_endpoint(name: str):
    """
    诊断MCP服务连接

    返回分层诊断报告，帮助定位连接问题。
    包括：配置验证 → DNS解析 → 网络连接 → TLS握手 → MCP协议
    """
    # Import at module level to avoid import-time issues
    import src.mcp_diagnostic as mcp_diag

    service = mcp_registry.get_service(name)
    if not service:
        raise HTTPException(status_code=404, detail="服务不存在")

    try:
        report = await mcp_diag.diagnose_mcp_service(service)
        return report.model_dump()
    except SecurityValidationError as e:
        raise _security_error(e) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail="诊断失败") from e


# === Skills API ===

@app.get("/api/skills")
async def list_skills():
    """获取所有 Skills 列表"""
    skills = skill_registry.list_skills()
    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "source": s.source.value,
                "version": s.version,
                "author": s.author,
                "tags": s.tags,
                "files": s.files,
                "enabled": s.enabled,
                "created_at": s.created_at,
                "updated_at": s.updated_at
            }
            for s in skills
        ]
    }


@app.get("/api/skills/{name}")
async def get_skill(name: str):
    """获取 Skill 详情"""
    skill = skill_registry.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill不存在")

    return {
        "name": skill.name,
        "description": skill.description,
        "source": skill.source.value,
        "skill_path": skill.skill_path,
        "version": skill.version,
        "author": skill.author,
        "tags": skill.tags,
        "files": skill.files,
        "enabled": skill.enabled,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at
    }


@app.delete("/api/skills/{name}")
async def delete_skill(name: str):
    """删除 Skill（仅用户 Skill）"""
    skill = skill_registry.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill不存在")

    if skill.source.value == "builtin":
        raise HTTPException(status_code=403, detail="预置Skill不能删除")

    if skill_registry.unregister_skill(name):
        return {"success": True}
    else:
        raise HTTPException(status_code=500, detail="删除失败")


@app.get("/api/skills/{name}/files")
async def get_skill_files(name: str):
    """获取 Skill 文件列表"""
    skill = skill_registry.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill不存在")

    return {"files": skill.files}


@app.get("/api/skills/{name}/files/{filepath:path}")
async def get_skill_file_content(name: str, filepath: str):
    """获取 Skill 文件内容预览"""
    skill = skill_registry.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill不存在")

    content = skill_registry.get_skill_file_content(name, filepath)
    if content is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 判断文件类型
    file_ext = Path(filepath).suffix.lower()
    file_type = "text"
    if file_ext in [".md", ".markdown"]:
        file_type = "markdown"
    elif file_ext in [".py"]:
        file_type = "python"
    elif file_ext in [".js", ".ts", ".jsx", ".tsx"]:
        file_type = "javascript"
    elif file_ext in [".json"]:
        file_type = "json"
    elif file_ext in [".yaml", ".yml"]:
        file_type = "yaml"

    return {
        "content": content,
        "file_type": file_type,
        "filepath": filepath
    }

@app.post("/api/skills/upload")
async def upload_skill(file: UploadFile = File(...)):
    """上传 Skill（zip包）"""
    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise HTTPException(status_code=400, detail="只支持zip文件")

    # Stream to a bounded temporary file; never materialise the archive in RAM.
    tmp_path = None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(
            delete=False, suffix='.zip', dir=TMP_DIR
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            uploaded = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                uploaded += len(chunk)
                if uploaded > skill_registry.MAX_ARCHIVE_SIZE:
                    raise HTTPException(status_code=413, detail="Zip包过大，最大支持25MB")
                tmp_file.write(chunk)

        success, message, skill_config = await run_blocking_work(
            skill_registry.extract_zip_and_register, tmp_path
        )
        if success and skill_config:
            return {
                "success": True,
                "message": message,
                "skill": {
                    "name": skill_config.name,
                    "description": skill_config.description,
                    "source": skill_config.source.value,
                    "version": skill_config.version,
                    "author": skill_config.author,
                    "tags": skill_config.tags
                }
            }
        else:
            raise HTTPException(status_code=400, detail=message)
    finally:
        # 清理临时文件
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


# === Model Services API ===

@app.get("/api/model-services")
async def list_model_services():
    """获取所有模型服务列表"""
    services = model_service_registry.list_services()
    return {
        "services": [
            {
                "name": s.name,
                "description": s.description,
                "provider": s.provider.value,
                "base_url": s.base_url,
                "selected_model": s.selected_model,
                "available_models": s.available_models,
                "enabled": s.enabled,
                "created_at": s.created_at,
                "updated_at": s.updated_at
            }
            for s in services
        ]
    }


@app.post("/api/model-services")
async def create_model_service(req: CreateModelServiceRequest):
    """创建模型服务"""
    name = _validate_resource_name(req.name, "服务名称")
    base_url = await _validate_model_service_url(req.base_url)

    if model_service_registry.service_exists(name):
        raise HTTPException(status_code=400, detail="服务名称已存在")

    try:
        config = ModelServiceConfig(
            name=name,
            description=req.description[:2_000],
            provider=ModelProvider(req.provider),
            base_url=base_url,
            api_key=req.api_key,
            selected_model=req.selected_model[:200],
            available_models=req.available_models[:500],
            enabled=req.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="模型供应商无效") from exc

    if await run_blocking_work(model_service_registry.create_service, config):
        return {"success": True, "name": name}
    else:
        raise HTTPException(status_code=500, detail="创建失败")


@app.get("/api/model-services/{name}")
async def get_model_service(name: str):
    """获取模型服务详情"""
    service = model_service_registry.get_service(name)
    if not service:
        raise HTTPException(status_code=404, detail="服务不存在")

    return {
        "name": service.name,
        "description": service.description,
        "provider": service.provider.value,
        "base_url": service.base_url,
        "api_key": "***" if service.api_key else None,  # 隐藏敏感信息
        "selected_model": service.selected_model,
        "available_models": service.available_models,
        "enabled": service.enabled,
        "created_at": service.created_at,
        "updated_at": service.updated_at
    }


@app.put("/api/model-services/{name}")
async def update_model_service(name: str, req: UpdateModelServiceRequest):
    """更新模型服务配置"""
    _validate_resource_name(name, "服务名称")
    existing = model_service_registry.get_service(name)
    if not existing:
        raise HTTPException(status_code=404, detail="服务不存在")

    base_url = await _validate_model_service_url(req.base_url)
    try:
        config = ModelServiceConfig(
            name=name,
            description=req.description[:2_000],
            provider=ModelProvider(req.provider),
            base_url=base_url,
            # 如果 api_key 为空，保留原有的
            api_key=(
                existing.api_key
                if req.api_key in {None, "", "***"}
                else req.api_key
            ),
            selected_model=req.selected_model[:200],
            available_models=req.available_models[:500],
            enabled=req.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="模型供应商无效") from exc

    if await run_blocking_work(model_service_registry.update_service, name, config):
        return {"success": True}
    else:
        raise HTTPException(status_code=500, detail="保存失败")


@app.delete("/api/model-services/{name}")
async def delete_model_service(name: str):
    """删除模型服务"""
    if await run_blocking_work(model_service_registry.delete_service, name):
        return {"success": True}
    else:
        raise HTTPException(status_code=404, detail="服务不存在")


@app.post("/api/model-services/test")
async def test_model_service(req: TestModelServiceRequest):
    """测试模型服务连接"""
    base_url = await _validate_model_service_url(req.base_url)
    try:
        provider = ModelProvider(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="模型供应商无效") from exc
    result = await test_model_service_connection(
        provider,
        base_url,
        req.api_key
    )
    return result


@app.get("/api/model-services/default-url/{provider}")
async def get_default_url(provider: str):
    """获取供应商默认URL"""
    try:
        default_url = model_service_registry.get_default_base_url(ModelProvider(provider))
        return {"default_url": default_url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"不支持的供应商: {provider}")


@app.get("/health")
async def health():
    return {"status": "ok"}


# === Conversations API ===

class CreateConversationRequest(BaseModel):
    """创建会话请求"""
    title: Optional[str] = Field(default=None, max_length=500)


class UpdateConversationRequest(BaseModel):
    """更新会话请求"""
    title: str = Field(min_length=1, max_length=500)


class AddMessageRequest(BaseModel):
    """添加消息请求"""
    role: str = Field(pattern=r"^(user|assistant|system)$")
    content: str = Field(max_length=500_000)
    thinking: Optional[str] = Field(default=None, max_length=500_000)
    tool_calls: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=100)
    metrics: Optional[Dict[str, Any]] = None


class ConversationMessageRequest(BaseModel):
    """Bounded message payload accepted by full and incremental sync APIs."""
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    role: str = Field(pattern=r"^(user|assistant|system)$")
    content: str = Field(max_length=500_000)
    thinking: Optional[str] = Field(default=None, max_length=500_000)
    tool_calls: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=100)
    metrics: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = Field(default=None, max_length=100)


class SaveMessagesRequest(BaseModel):
    """批量保存消息请求"""
    messages: List[ConversationMessageRequest] = Field(max_length=1_000)


class SyncMessagesRequest(BaseModel):
    """Incrementally synchronize one bounded conversation turn."""
    messages: List[ConversationMessageRequest] = Field(min_length=1, max_length=10)


@app.get("/api/agents/{name}/conversations")
async def list_conversations(name: str):
    """获取会话列表"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    conversations = await asyncio.to_thread(conversation_manager.list_conversations, name)
    return {
        "conversations": conversations,
        "total": len(conversations)
    }


@app.post("/api/agents/{name}/conversations")
async def create_conversation(name: str, req: CreateConversationRequest):
    """创建新会话"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    try:
        conversation = await asyncio.to_thread(
            conversation_manager.create_conversation, name, req.title
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="会话数量已达到存储上限") from exc
    return {
        "id": conversation.id,
        "title": conversation.title,
        "messages": conversation.messages,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at
    }


@app.get("/api/agents/{name}/conversations/{conversation_id}")
async def get_conversation(name: str, conversation_id: str):
    """获取会话详情"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    conversation = await asyncio.to_thread(
        conversation_manager.get_conversation, name, conversation_id
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "id": conversation.id,
        "title": conversation.title,
        "messages": conversation.messages,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at
    }


@app.put("/api/agents/{name}/conversations/{conversation_id}")
async def update_conversation(name: str, conversation_id: str, req: UpdateConversationRequest):
    """更新会话（重命名）"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    conversation = await asyncio.to_thread(
        conversation_manager.update_conversation, name, conversation_id, req.title
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "success": True,
        "conversation": {
            "id": conversation.id,
            "title": conversation.title,
            "updated_at": conversation.updated_at
        }
    }


@app.delete("/api/agents/{name}/conversations/{conversation_id}")
async def delete_conversation(name: str, conversation_id: str):
    """删除会话"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    if await asyncio.to_thread(
        conversation_manager.delete_conversation, name, conversation_id
    ):
        return {"success": True}
    else:
        raise HTTPException(status_code=404, detail="会话不存在")


@app.post("/api/agents/{name}/conversations/{conversation_id}/messages")
async def add_conversation_message(name: str, conversation_id: str, req: AddMessageRequest):
    """添加消息到会话"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    try:
        message = await asyncio.to_thread(
            conversation_manager.add_message,
            name,
            conversation_id,
            req.role,
            req.content,
            req.thinking,
            req.tool_calls,
            req.metrics,
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail="会话消息超过存储限制") from exc
    if not message:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "success": True,
        "message": message
    }


@app.post("/api/agents/{name}/conversations/{conversation_id}/save")
async def save_conversation_messages(name: str, conversation_id: str, req: SaveMessagesRequest):
    """批量保存会话消息"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    messages = [message.model_dump(exclude_none=True) for message in req.messages]
    try:
        conversation = await asyncio.to_thread(
            conversation_manager.save_messages, name, conversation_id, messages
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail="会话消息超过存储限制") from exc
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "success": True,
        "conversation": {
            "id": conversation.id,
            "title": conversation.title,
            "message_count": len(conversation.messages),
            "updated_at": conversation.updated_at
        }
    }


@app.post("/api/agents/{name}/conversations/{conversation_id}/messages/sync")
async def sync_conversation_messages(
    name: str,
    conversation_id: str,
    req: SyncMessagesRequest,
):
    """Upsert only the messages changed by the current completed turn."""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    messages = [message.model_dump(exclude_none=True) for message in req.messages]
    try:
        conversation = await asyncio.to_thread(
            conversation_manager.sync_messages, name, conversation_id, messages
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail="会话消息超过存储限制") from exc
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "success": True,
        "conversation": {
            "id": conversation.id,
            "title": conversation.title,
            "message_count": conversation.message_count,
            "updated_at": conversation.updated_at,
        },
    }


# === 日志收集 ===


def _append_rotating_log(
    path: Path,
    entry: str,
    max_bytes: int = 20 * 1024 * 1024,
    backups: int = 5,
) -> None:
    append_rotating_log(DATA_DIR, path, entry, max_bytes, backups)


def _write_client_log(log_file: Path, log_data: Any, logs_dir: Path) -> None:
    write_client_log(DATA_DIR, log_file, log_data, logs_dir)


@app.post("/api/log")
async def save_log(request: Request):
    """保存前端日志的无内容摘要。"""
    try:
        log_data = await read_json_body_limited(request, 64 * 1024)
    except SecurityValidationError as exc:
        raise HTTPException(status_code=413, detail="请求正文无效或超过大小限制") from exc

    log_file = DATA_DIR / "frontend_logs.txt"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    raw_type = log_data.get("type", "INFO")
    log_type = raw_type if isinstance(raw_type, str) and re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,32}", raw_type
    ) else "OTHER"
    log_entry = (
        f"[{timestamp}] {log_type}: "
        f"message_length={content_length(log_data.get('message'))} "
        f"details_length={content_length(log_data.get('details'))} "
        f"url_length={content_length(log_data.get('url'))} "
        f"error_length={content_length(log_data.get('error'))}\n"
    )

    await run_blocking_work(_append_rotating_log, log_file, log_entry)

    return {"success": True}


@app.post("/api/client-logs")
async def save_client_logs(request: Request):
    """保存客户端日志的无内容、有界摘要。"""
    try:
        log_data = await read_json_body_limited(request, 256 * 1024)
    except SecurityValidationError as exc:
        raise HTTPException(status_code=413, detail="请求正文无效或超过大小限制") from exc

    logs_dir = DATA_DIR / "logs"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"client_log_{timestamp}_{uuid.uuid4().hex[:8]}.json"
    log_file = logs_dir / filename

    try:
        summary = {
            "payload_type": type(log_data).__name__,
            "payload_length": serialized_length(log_data),
            "item_count": len(log_data) if isinstance(log_data, (dict, list)) else 0,
        }
        if isinstance(log_data, dict):
            summary.update(summarize_arguments(log_data))
        await run_blocking_work(_write_client_log, log_file, summary, logs_dir)

        return {"success": True, "filename": filename}
    except Exception:
        return {"success": False, "error": "日志保存失败"}


# ============================================================================
# 【AC130-202603150000】调试日志 API - 新增
# ============================================================================

@app.get("/api/debug/logs/{request_id}")
async def get_debug_logs(request_id: str):
    """获取指定请求的调试日志

    与前端 DebugLogger 配合，提供后端流式请求的结构化日志

    Args:
        request_id: 请求唯一标识符

    Returns:
        包含 meta 和 server 字段的结构化日志响应
    """
    from src.stream_logger import StreamLogger, cleanup_old_logs

    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", request_id):
        raise HTTPException(status_code=400, detail="请求 ID 无效")
    logger = StreamLogger.find_logger(request_id)
    if logger is None:
        raise HTTPException(status_code=404, detail="调试日志不存在")
    logs = logger.get_logs()

    # 定期清理旧日志
    cleanup_old_logs()

    return {
        "meta": {
            "version": "1.0",
            "exportedAt": datetime.now().isoformat(),
            "requestId": request_id
        },
        "server": {
            "logs": logs["events"],
            "start_time": logs["start_time"],
            "end_time": logs["end_time"],
            "event_count": logs["event_count"]
        }
    }


@app.get("/api/debug/logs")
async def list_debug_logs():
    """列出所有活跃的调试日志请求ID

    Returns:
        包含所有活跃请求 ID 的列表
    """
    from src.stream_logger import StreamLogger

    request_ids = StreamLogger.get_all_request_ids()

    return {
        "request_ids": request_ids,
        "count": len(request_ids)
    }


# ============================================================================
# 【环境管理 API - 新增】
# ============================================================================

class CreateEnvironmentRequest(BaseModel):
    """创建环境请求"""
    python_version: Literal["3.11"] = "3.11"


class InstallPackagesRequest(BaseModel):
    """安装包请求"""
    packages: List[str] = Field(min_length=1, max_length=64)


class ExecuteScriptRequest(BaseModel):
    """执行脚本请求"""
    skill_name: str = Field(min_length=1, max_length=200)
    script_path: str = Field(default="main.py", min_length=1, max_length=1_024)
    arguments: List[str] = Field(default_factory=list, max_length=128)
    input_file_ids: List[str] = Field(default_factory=list, max_length=128)
    timeout: int = Field(default=60, ge=1, le=300)


@app.post("/api/agents/{name}/environment")
async def create_environment(name: str, req: CreateEnvironmentRequest):
    """创建Agent运行环境"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    try:
        environment = await environment_manager.create_environment(
            agent_name=name,
            python_version=req.python_version
        )
        return {
            "success": True,
            "environment": {
                "environment_id": environment.environment_id,
                "agent_name": environment.agent_name,
                "status": environment.status.value,
                "python_version": environment.python_version,
                "created_at": environment.created_at
            }
        }
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail="创建运行环境失败") from exc


@app.get("/api/agents/{name}/environment")
async def get_environment(name: str):
    """获取Agent环境状态

    返回环境状态信息，支持后台任务状态查询和进度估算。
    前端应根据status字段判断：
    - creating: 环境创建中，禁用skill配置，显示进度条
    - ready: 环境就绪，可正常使用
    - error: 环境创建失败，需重试

    当状态为creating时，会返回：
    - progress: 当前进度(0-100)
    - estimated_remaining: 预估剩余时间(毫秒)
    """
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    environment = await environment_manager.get_environment_status(name)

    # 检查是否有进行中的创建任务
    task_status = await environment_creator.get_task_status(name)

    # 准备基础响应
    response = {
        "exists": False,
        "environment": None,
        "progress": None,
        "estimated_remaining": None
    }

    if not environment:
        # 如果没有环境记录，但有进行中的任务，返回creating状态
        if task_status == "running":
            response["exists"] = True
            response["environment"] = {
                "agent_name": name,
                "status": "creating",
                "environment_type": "uv",
                "python_version": "3.11",
                "packages": [],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "error_message": None
            }
            # 计算模拟进度（由于无法获取实际开始时间，使用默认值）
            progress, remaining = _calculate_mock_progress(5)  # 假设已进行5秒
            response["progress"] = progress
            response["estimated_remaining"] = remaining
        return response

    # 如果有运行中的任务，返回creating状态（即使元数据显示其他状态）
    is_creating = task_status == "running" or environment.status == EnvironmentStatus.CREATING

    env_dict = {
        "environment_id": environment.environment_id,
        "agent_name": environment.agent_name,
        "status": environment.status.value if not is_creating else "creating",
        "environment_type": environment.environment_type.value,
        "python_version": environment.python_version,
        "packages": environment.packages,
        "installed_dependencies": environment.installed_dependencies,
        "created_at": environment.created_at,
        "updated_at": environment.updated_at,
        "error_message": environment.error_message
    }

    response["exists"] = True
    response["environment"] = env_dict

    # 如果正在创建，计算进度
    if is_creating:
        try:
            from datetime import datetime as dt
            created_at = dt.fromisoformat(environment.created_at)
            elapsed = (dt.now() - created_at).total_seconds()
            progress, remaining = _calculate_mock_progress(elapsed)
            response["progress"] = progress
            response["estimated_remaining"] = remaining
        except Exception:
            # 如果时间解析失败，使用默认值
            progress, remaining = _calculate_mock_progress(5)
            response["progress"] = progress
            response["estimated_remaining"] = remaining
    else:
        # 已完成或失败状态
        response["progress"] = 100.0 if environment.status == EnvironmentStatus.READY else None
        response["estimated_remaining"] = 0 if environment.status == EnvironmentStatus.READY else None

    return response


@app.delete("/api/agents/{name}/environment")
async def delete_environment(name: str):
    """删除Agent运行环境"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # 取消可能进行中的创建任务
    await environment_creator.cancel(name)

    try:
        success = await environment_manager.delete_environment(name)
        return {"success": success}
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail="删除运行环境失败") from exc


@app.post("/api/agents/{name}/environment/retry")
async def retry_environment_creation(name: str):
    """重试环境创建

    当环境创建失败时，可以调用此接口重新创建环境。
    清理失败的环境并启动新的创建任务。
    """
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    # 检查环境状态
    existing = await environment_manager.get_environment_status(name)

    # 如果环境已经就绪，无需重试
    if existing and existing.status == EnvironmentStatus.READY:
        return {
            "status": "ready",
            "message": "环境已就绪，无需重试"
        }

    # 如果有进行中的任务，提示等待
    if environment_creator.has_running_task(name):
        return {
            "status": "creating",
            "message": "环境创建任务正在进行中，请等待完成"
        }

    # 清理失败的环境记录
    if existing and existing.status == EnvironmentStatus.ERROR:
        try:
            await environment_manager.delete_environment(name)
            print(f"[ENV] 已清理失败的环境: {name}")
        except Exception as e:
            print(
                "[ENV] 清理失败环境时出错: "
                f"error_type={type(e).__name__}"
            )

    # 重新启动环境创建任务
    await environment_creator.create(name)

    return {
        "status": "retrying",
        "message": "环境重新初始化中..."
    }


@app.post("/api/agents/{name}/environment/packages")
async def install_packages(name: str, req: InstallPackagesRequest):
    """安装Python包"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    try:
        packages = validate_package_specs(req.packages)
    except SecurityValidationError as exc:
        raise _security_error(exc) from exc

    success, message = await environment_manager.install_packages(name, packages)
    if success:
        return {"success": True, "message": message}
    else:
        raise HTTPException(status_code=400, detail=message)


@app.get("/api/agents/{name}/environment/packages")
async def list_packages(name: str):
    """列出已安装的包"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    packages = await environment_manager.list_packages(name)
    return {"packages": packages}


# ============================================================================
# 【文件管理 API - 新增】
# ============================================================================

@app.post("/api/agents/{name}/files")
async def upload_file(name: str, file: UploadFile = File(...)):
    """上传文件到Agent存储"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    try:
        file_info = await file_storage_manager.upload_stream(
            agent_name=name,
            upload=file,
            filename=file.filename or "unknown",
            mime_type=file.content_type
        )

        return {
            "success": True,
            "file": {
                "file_id": file_info.file_id,
                "filename": file_info.filename,
                "file_size": file_info.file_size,
                "mime_type": file_info.mime_type,
                "uploaded_at": file_info.uploaded_at
            }
        }
    except FileStorageError as exc:
        raise HTTPException(status_code=400, detail="文件无效或超过存储限制") from exc


@app.get("/api/agents/{name}/files")
async def list_files(name: str):
    """列出Agent的所有文件"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    files = await file_storage_manager.list_files(name)
    return {
        "files": [
            {
                "file_id": f.file_id,
                "filename": f.filename,
                "file_size": f.file_size,
                "mime_type": f.mime_type,
                "uploaded_at": f.uploaded_at
            }
            for f in files
        ]
    }


@app.get("/api/agents/{name}/files/{file_id}")
async def download_file(name: str, file_id: str):
    """下载文件"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    file_info = await file_storage_manager.get_file_info(name, file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="文件不存在")

    file_path = await file_storage_manager.get_file_path(name, file_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="文件不存在")

    from fastapi.responses import FileResponse
    return FileResponse(
        path=file_path,
        filename=file_info.filename,
        media_type=file_info.mime_type
    )


@app.delete("/api/agents/{name}/files/{file_id}")
async def delete_file(name: str, file_id: str):
    """删除文件"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    success = await file_storage_manager.delete_file(name, file_id)
    if success:
        return {"success": True}
    else:
        raise HTTPException(status_code=404, detail="文件不存在")


# ============================================================================
# 【脚本执行 API - 新增】
# ============================================================================

@app.post("/api/agents/{name}/execute")
async def execute_script(name: str, req: ExecuteScriptRequest):
    """执行Skill脚本"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    skill = skill_registry.get_skill(req.skill_name)
    if not skill or not skill.enabled or not skill.skill_path:
        raise HTTPException(status_code=404, detail="Skill不存在或已禁用")

    try:
        validate_execution_arguments(req.arguments, req.timeout)
        skill_root = resolve_contained_path(
            SKILLS_DIR,
            skill.skill_path,
            must_exist=True,
            require_file=False,
        )
        script_file = resolve_contained_path(skill_root, req.script_path)
        if script_file.suffix.lower() != ".py":
            raise SecurityValidationError("Only Python Skill scripts may be executed")
        script_path = script_file.relative_to(skill_root).as_posix()
    except SecurityValidationError as exc:
        raise _security_error(exc) from exc

    try:
        record = await execution_engine.execute_script(
            agent_name=name,
            skill_name=req.skill_name,
            script_path=script_path,
            args=req.arguments,
            input_file_ids=req.input_file_ids,
            timeout=req.timeout,
            skill_base_path=str(skill_root)
        )

        return {
            "success": record.status == ExecutionStatus.SUCCESS,
            "execution": {
                "execution_id": record.execution_id,
                "status": record.status.value,
                "exit_code": record.exit_code,
                "stdout": record.stdout,
                "stderr": record.stderr,
                "duration_ms": record.duration_ms,
                "started_at": record.started_at,
                "finished_at": record.finished_at
            }
        }
    except (ExecutionError, FileStorageError) as exc:
        raise HTTPException(status_code=400, detail="脚本执行请求无效") from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail="脚本执行失败") from e


@app.get("/api/agents/{name}/executions")
async def list_executions(name: str, limit: int = Query(default=50, ge=1, le=200)):
    """列出执行记录"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    records = await execution_engine.list_executions(name, limit)
    return {
        "executions": [
            {
                "execution_id": r.execution_id,
                "skill_name": r.skill_name,
                "script_path": r.script_path,
                "status": r.status.value,
                "exit_code": r.exit_code,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at,
                "finished_at": r.finished_at
            }
            for r in records
        ]
    }


@app.get("/api/agents/{name}/executions/{execution_id}")
async def get_execution(name: str, execution_id: str):
    """获取执行详情"""
    if name not in manager.list_agents():
        raise HTTPException(status_code=404, detail="Agent不存在")

    record = await execution_engine.get_execution_status(name, execution_id)
    if not record:
        raise HTTPException(status_code=404, detail="执行记录不存在")

    return {
        "execution": {
            "execution_id": record.execution_id,
            "skill_name": record.skill_name,
            "script_path": record.script_path,
            "arguments": record.arguments,
            "status": record.status.value,
            "exit_code": record.exit_code,
            "stdout": record.stdout,
            "stderr": record.stderr,
            "duration_ms": record.duration_ms,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at
        }
    }


# ============================================================================
# RAG 知识库 API (AC130-202603161542)
# ============================================================================

class CreateKnowledgeBaseRequest(BaseModel):
    """创建知识库请求"""
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5_000)
    embedding_model: str = Field(default="BAAI/bge-small-zh-v1.5", max_length=200)


class SearchRequest(BaseModel):
    """检索请求"""
    query: str = Field(min_length=1, max_length=20_000)
    top_k: int = Field(default=3, ge=1, le=50)
    score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)


@app.get("/api/knowledge-bases")
async def list_knowledge_bases():
    """列出所有知识库"""
    try:
        kbs = await asyncio.to_thread(kb_manager.list_kb)
        return {
            "knowledge_bases": [
                {
                    "kb_id": kb.kb_id,
                    "name": kb.name,
                    "description": kb.description,
                    "embedding_model": kb.embedding_model,
                    "created_at": kb.created_at,
                    "updated_at": kb.updated_at,
                    "doc_count": kb.doc_count,
                    "chunk_count": kb.chunk_count,
                    "total_size": kb.total_size
                }
                for kb in kbs
            ]
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail="获取知识库列表失败") from exc


@app.post("/api/knowledge-bases")
async def create_knowledge_base(req: CreateKnowledgeBaseRequest):
    """创建知识库"""
    try:
        kb = await run_blocking_work(
            kb_manager.create_kb,
            req.name,
            req.description,
            req.embedding_model,
        )
        return {
            "kb_id": kb.kb_id,
            "name": kb.name,
            "description": kb.description,
            "embedding_model": kb.embedding_model,
            "created_at": kb.created_at,
            "updated_at": kb.updated_at,
            "doc_count": kb.doc_count,
            "chunk_count": kb.chunk_count
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="知识库配置无效或已存在") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="创建知识库失败") from exc


@app.get("/api/knowledge-bases/{kb_id}")
async def get_knowledge_base(kb_id: str):
    """获取知识库详情"""
    kb = await asyncio.to_thread(kb_manager.get_kb, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    return {
        "kb_id": kb.kb_id,
        "name": kb.name,
        "description": kb.description,
        "embedding_model": kb.embedding_model,
        "created_at": kb.created_at,
        "updated_at": kb.updated_at,
        "doc_count": kb.doc_count,
        "chunk_count": kb.chunk_count,
        "total_size": kb.total_size
    }


@app.delete("/api/knowledge-bases/{kb_id}")
async def delete_knowledge_base(kb_id: str):
    """删除知识库"""
    success = await run_blocking_work(kb_manager.delete_kb, kb_id)
    if not success:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return {"message": "知识库已删除"}


@app.get("/api/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: str):
    """列出知识库中的所有文档"""
    if not await asyncio.to_thread(kb_manager.get_kb, kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")

    documents = await asyncio.to_thread(kb_manager.list_documents, kb_id)
    return {
        "documents": [
            {
                "doc_id": doc.doc_id,
                "filename": doc.filename,
                "file_size": doc.file_size,
                "mime_type": doc.mime_type,
                "chunk_count": doc.chunk_count,
                "char_count": doc.char_count,
                "status": doc.status.value,
                "uploaded_at": doc.uploaded_at,
                "processed_at": doc.processed_at
            }
            for doc in documents
        ]
    }


@app.post("/api/knowledge-bases/{kb_id}/documents")
async def upload_document(kb_id: str, file: UploadFile):
    """上传文档到知识库"""
    from src.document_processor import DocumentProcessor

    if not await asyncio.to_thread(kb_manager.get_kb, kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")

    try:
        filename = sanitise_filename(file.filename or "")
    except SecurityValidationError as exc:
        raise _security_error(exc) from exc

    # 验证文件类型
    suffix = Path(filename).suffix.lower()
    if suffix not in DocumentProcessor.SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {suffix}。支持的格式: {', '.join(DocumentProcessor.SUPPORTED_FORMATS)}"
        )

    # Stream to disk and stop reading as soon as the hard limit is exceeded.
    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, dir=TMP_DIR
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            uploaded = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                uploaded += len(chunk)
                if uploaded > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="文件过大，最大支持 10MB")
                tmp_file.write(chunk)

        document = await run_blocking_work(
            kb_manager.add_document, kb_id, tmp_path, filename
        )

        return {
            "doc_id": document.doc_id,
            "filename": document.filename,
            "file_size": document.file_size,
            "mime_type": document.mime_type,
            "chunk_count": document.chunk_count,
            "char_count": document.char_count,
            "status": document.status.value,
            "uploaded_at": document.uploaded_at,
            "processed_at": document.processed_at,
            "error_message": document.error_message
        }

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="文档无效或超过处理限制") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="文档处理失败") from exc
    finally:
        # 清理临时文件
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


@app.delete("/api/knowledge-bases/{kb_id}/documents/{doc_id}")
async def delete_document(kb_id: str, doc_id: str):
    """删除文档"""
    if not await asyncio.to_thread(kb_manager.get_kb, kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")

    success = await run_blocking_work(kb_manager.delete_document, kb_id, doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="文档不存在")

    return {"message": "文档已删除"}


@app.post("/api/knowledge-bases/{kb_id}/search")
async def search_knowledge_base(kb_id: str, req: SearchRequest):
    """检索知识库"""
    if not await asyncio.to_thread(kb_manager.get_kb, kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")

    try:
        def search_sync():
            retriever = kb_manager.get_retriever(kb_id)
            return retriever.search(
                req.query, req.top_k, req.score_threshold
            )

        results = await run_blocking_work(search_sync)

        return {
            "results": [
                {
                    "content": r.content,
                    "doc_id": r.doc_id,
                    "filename": r.filename,
                    "score": r.score,
                    "chunk_index": r.chunk_index
                }
                for r in results
            ]
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail="检索失败") from exc


@app.get("/api/knowledge-bases/{kb_id}/stats")
async def get_knowledge_base_stats(kb_id: str):
    """获取知识库统计信息"""
    kb = await asyncio.to_thread(kb_manager.get_kb, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    return {
        "kb_id": kb.kb_id,
        "doc_count": kb.doc_count,
        "chunk_count": kb.chunk_count,
        "total_size": kb.total_size
    }


if __name__ == "__main__":
    port = int(os.environ.get("BACKEND_PORT", os.environ.get("PORT", 20881)))
    host = os.environ.get(
        "AGENT_BUILDER_HOST",
        os.environ.get("BACKEND_HOST", "127.0.0.1"),
    )
    uvicorn.run(app, host=host, port=port)

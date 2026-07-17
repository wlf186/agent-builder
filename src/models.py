"""
Agent配置模型
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime
import uuid


class LLMProvider(str, Enum):
    """旧版LLM提供商（已废弃，保留用于数据迁移）"""
    OLLAMA = "ollama"
    ZHIPU = "zhipu"


class ModelProvider(str, Enum):
    """模型服务提供商"""
    ZHIPU = "zhipu"
    ALIBABA_BAILIAN = "alibaba_bailian"
    OLLAMA = "ollama"


class MCPConnectionType(str, Enum):
    """MCP连接类型"""
    STDIO = "stdio"      # 本地进程
    SSE = "sse"          # Server-Sent Events (远程)


class MCPAuthType(str, Enum):
    """MCP认证类型"""
    NONE = "none"        # 无认证
    BEARER = "bearer"    # Bearer Token
    APIKEY = "apikey"    # API Key


class SkillSource(str, Enum):
    """Skill来源类型"""
    BUILTIN = "builtin"    # 预置官方 Skills
    USER = "user"          # 用户上传


class PlanningMode(str, Enum):
    """规划模式"""
    REACT = "react"                    # Thought → Action → Observation 循环
    REFLEXION = "reflexion"            # 执行后反思，自我修正
    PLAN_AND_SOLVE = "plan_and_solve"  # 先规划再执行
    REWOO = "rewOO"                    # 无观察规划，并行执行工具
    TOT = "tot"                        # 树状思考，探索多条路径


# ============================================================================
# RAG 检索配置（需在 AgentConfig 之前定义）
# ============================================================================

DEFAULT_RAG_PROMPT_TEMPLATE = """请基于以下知识库内容回答用户问题。如果知识库中没有相关信息，请明确告知。

<knowledge_base>
{retrieved_chunks}
</knowledge_base>

用户问题：{user_query}"""


class RetrievalConfig(BaseModel):
    """检索配置"""
    top_k: int = Field(default=3, ge=1, le=10, description="返回结果数量")
    score_threshold: float = Field(default=0.6, ge=0.0, le=1.0, description="相似度阈值")
    prompt_template: str = Field(
        default=DEFAULT_RAG_PROMPT_TEMPLATE,
        description="注入到 System Prompt 的模板"
    )


class AgentConfig(BaseModel):
    """Agent配置"""
    name: str = Field(default="助手", description="Agent名称")
    persona: str = Field(
        default="你是一个有帮助的AI助手。",
        description="Agent人设/系统提示词"
    )
    # 新版：引用模型服务
    model_service: Optional[str] = Field(default=None, description="模型服务名称（引用全局注册的服务）")
    # 旧版字段（已废弃，保留用于数据迁移）
    llm_provider: Optional[LLMProvider] = Field(default=None, description="LLM提供商（已废弃）")
    llm_model: Optional[str] = Field(default=None, description="模型名称（已废弃）")
    llm_base_url: Optional[str] = Field(default=None, description="API基础URL（已废弃）")
    mcp_servers: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="MCP服务器配置列表（已废弃，请使用 mcp_services）"
    )
    mcp_services: List[str] = Field(
        default_factory=list,
        description="MCP服务名称列表（引用全局注册的服务）"
    )
    skills: List[str] = Field(
        default_factory=list,
        description="Skill名称列表（引用全局注册的技能）"
    )
    max_iterations: int = Field(default=10, description="最大迭代次数")
    planning_mode: PlanningMode = Field(default=PlanningMode.REACT, description="规划模式")
    temperature: float = Field(default=0.7, description="温度参数")
    short_term_memory: int = Field(default=5, description="短期记忆轮次(0-50)")
    # ========================================================================
    # Agent-as-a-Tool: 子 Agent 配置 (AC130-202603142210)
    # ========================================================================
    sub_agents: List[str] = Field(
        default_factory=list,
        description="可调用的子Agent名称列表"
    )
    sub_agent_timeout: int = Field(
        default=60,
        ge=10,
        le=300,
        description="子Agent调用超时时间（秒）"
    )
    sub_agent_max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description="子Agent调用失败重试次数"
    )
    sub_agent_max_concurrent: int = Field(
        default=3,
        ge=1,
        le=10,
        description="最大并发子Agent调用数"
    )
    # ========================================================================
    # RAG 知识库挂载配置 (AC130-202603161542)
    # ========================================================================
    knowledge_bases: List[str] = Field(
        default_factory=list,
        description="挂载的知识库 ID 列表"
    )
    retrieval_config: Optional[RetrievalConfig] = Field(
        default=None,
        description="检索配置"
    )


class MCPConfig(BaseModel):
    """MCP服务器配置"""
    name: str = Field(description="服务器名称")
    command: str = Field(description="启动命令")
    args: List[str] = Field(default_factory=list, description="命令参数")
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")


class MCPServiceConfig(BaseModel):
    """MCP服务配置（全局注册表）"""
    name: str = Field(description="服务名称（唯一标识）")
    description: str = Field(default="", description="服务描述")
    connection_type: MCPConnectionType = Field(
        default=MCPConnectionType.STDIO,
        description="连接类型: stdio(本地) / sse(远程)"
    )
    # stdio 配置
    command: Optional[str] = Field(default=None, description="启动命令（stdio模式）")
    args: List[str] = Field(default_factory=list, description="命令参数")
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    # SSE 配置
    url: Optional[str] = Field(default=None, description="SSE服务URL（sse模式）")
    auth_type: Optional[MCPAuthType] = Field(default=MCPAuthType.NONE, description="认证类型")
    auth_value: Optional[str] = Field(default=None, description="认证值（Token/API Key）")
    headers: Dict[str, str] = Field(default_factory=dict, description="自定义请求头")
    # 元数据
    enabled: bool = Field(default=True, description="是否启用")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")


class ModelServiceConfig(BaseModel):
    """模型服务配置（全局注册表）"""
    name: str = Field(description="服务名称（唯一标识）")
    description: str = Field(default="", description="服务描述")
    provider: ModelProvider = Field(description="模型提供商")
    base_url: str = Field(description="API基础URL")
    api_key: Optional[str] = Field(default=None, description="API密钥（Ollama无需）")
    selected_model: str = Field(description="选中的模型")
    available_models: List[str] = Field(default_factory=list, description="可用模型列表")
    enabled: bool = Field(default=True, description="是否启用")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")


class SkillConfig(BaseModel):
    """Skill配置（全局注册表）"""
    name: str = Field(description="Skill名称（唯一标识）")
    description: str = Field(default="", description="Skill描述（用于触发匹配）")
    source: SkillSource = Field(default=SkillSource.BUILTIN, description="来源类型")
    skill_path: str = Field(description="Skill文件夹路径（相对skills目录）")
    version: Optional[str] = Field(default="1.0.0", description="版本号")
    author: Optional[str] = Field(default=None, description="作者")
    tags: List[str] = Field(default_factory=list, description="标签列表")
    files: List[str] = Field(default_factory=list, description="包含的文件列表")
    enabled: bool = Field(default=True, description="是否启用")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")


class Message(BaseModel):
    """对话消息"""
    role: str = Field(description="角色: user/assistant/system/tool")
    content: str = Field(description="消息内容")
    tool_calls: Optional[List[Dict]] = Field(default=None)
    tool_call_id: Optional[str] = Field(default=None)


class ConversationConfig(BaseModel):
    """会话配置"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="会话唯一ID")
    agent_name: str = Field(default="", description="所属智能体名称")
    title: str = Field(default="新对话", description="会话标题")
    messages: List[Dict[str, Any]] = Field(default_factory=list, description="消息列表")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="创建时间")
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="更新时间")

    def get_preview(self, max_length: int = 50) -> str:
        """获取会话预览文本"""
        if not self.messages:
            return ""
        # 获取第一条用户消息作为预览
        for msg in self.messages:
            if msg.get("role") == "user" and msg.get("content"):
                content = msg.get("content", "")
                return content[:max_length] + "..." if len(content) > max_length else content
        return ""

    def get_message_count(self) -> int:
        """获取消息数量"""
        return len(self.messages)


# ============================================================================
# Skill 独立运行环境相关模型
# ============================================================================

class EnvironmentType(str, Enum):
    """环境类型"""
    UV = "uv"                    # uv 管理的项目内虚拟环境
    SYSTEM_PYTHON = "system"     # 系统 Python（降级模式）


class EnvironmentStatus(str, Enum):
    """环境状态"""
    CREATING = "creating"
    READY = "ready"
    ERROR = "error"
    DELETED = "deleted"


class ExecutionStatus(str, Enum):
    """执行状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class AgentEnvironment(BaseModel):
    """Agent运行环境"""
    environment_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8], description="环境唯一ID")
    agent_name: str = Field(description="所属Agent名称")
    environment_type: EnvironmentType = Field(default=EnvironmentType.UV, description="环境类型")
    status: EnvironmentStatus = Field(default=EnvironmentStatus.CREATING, description="环境状态")
    python_version: str = Field(default="3.11", description="Python版本")
    packages: List[str] = Field(default_factory=list, description="已安装的包列表")
    installed_dependencies: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="已安装的Skill依赖，格式: {skill_name: [依赖列表]}"
    )
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="创建时间")
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="更新时间")
    error_message: Optional[str] = Field(default=None, description="错误信息")


class FileInfo(BaseModel):
    """文件信息"""
    file_id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="文件唯一ID")
    agent_name: str = Field(description="所属Agent名称")
    filename: str = Field(description="原始文件名")
    file_size: int = Field(description="文件大小(字节)")
    mime_type: str = Field(default="application/octet-stream", description="MIME类型")
    checksum: str = Field(description="文件校验和(SHA-256)")
    file_path: str = Field(description="存储路径")
    uploaded_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="上传时间")


class ExecutionRecord(BaseModel):
    """执行记录"""
    execution_id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="执行唯一ID")
    agent_name: str = Field(description="所属Agent名称")
    skill_name: str = Field(description="执行的Skill名称")
    script_path: str = Field(description="脚本路径")
    arguments: List[str] = Field(default_factory=list, description="命令行参数")
    input_file_ids: List[str] = Field(default_factory=list, description="输入文件ID列表")
    status: ExecutionStatus = Field(default=ExecutionStatus.PENDING, description="执行状态")
    exit_code: Optional[int] = Field(default=None, description="退出码")
    stdout: str = Field(default="", description="标准输出")
    stderr: str = Field(default="", description="标准错误")
    duration_ms: Optional[int] = Field(default=None, description="执行时长(毫秒)")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="创建时间")
    started_at: Optional[str] = Field(default=None, description="开始时间")
    finished_at: Optional[str] = Field(default=None, description="结束时间")


# ============================================================================
# RAG 知识库系统相关模型 (AC130-202603161542)
# ============================================================================

class DocumentStatus(str, Enum):
    """文档处理状态"""
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeBase(BaseModel):
    """知识库配置"""
    kb_id: str = Field(default_factory=lambda: f"kb_{uuid.uuid4().hex}", description="知识库唯一ID")
    name: str = Field(description="知识库名称")
    description: str = Field(default="", description="知识库描述")
    embedding_model: str = Field(default="BAAI/bge-small-zh-v1.5", description="嵌入模型名称")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="创建时间")
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="更新时间")

    # 统计信息（运行时计算）
    doc_count: int = Field(default=0, description="文档数量")
    chunk_count: int = Field(default=0, description="文档块总数")
    total_size: int = Field(default=0, description="总文件大小(字节)")


class Document(BaseModel):
    """文档元数据"""
    doc_id: str = Field(default_factory=lambda: f"doc_{uuid.uuid4().hex}", description="文档唯一ID")
    kb_id: str = Field(description="所属知识库ID")
    filename: str = Field(description="文件名")
    file_size: int = Field(description="文件大小(字节)")
    file_path: str = Field(description="存储路径")
    mime_type: str = Field(description="MIME类型")
    chunk_count: int = Field(default=0, description="文档块数量")
    char_count: int = Field(default=0, description="字符数量")
    status: DocumentStatus = Field(default=DocumentStatus.PROCESSING, description="处理状态")
    uploaded_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="上传时间")
    processed_at: Optional[str] = Field(default=None, description="处理完成时间")
    error_message: Optional[str] = Field(default=None, description="错误信息")


class Chunk(BaseModel):
    """文档块"""
    chunk_id: str = Field(description="块唯一ID")
    doc_id: str = Field(description="所属文档ID")
    content: str = Field(description="块内容")
    chunk_index: int = Field(description="块索引")
    start_pos: int = Field(default=0, description="在原文档中的起始位置")
    end_pos: int = Field(default=0, description="在原文档中的结束位置")


class RetrievalResult(BaseModel):
    """检索结果"""
    content: str = Field(description="文档片段内容")
    doc_id: str = Field(description="文档ID")
    filename: str = Field(description="文件名")
    score: float = Field(description="相似度分数(0-1)")
    chunk_index: int = Field(description="块索引")

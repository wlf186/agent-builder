# API 参考文档

> Agent Builder 后端 API 端点汇总。端点实现以 `backend.py` 为准。

**Backend Base URL**: `http://127.0.0.1:20881`

除 `/health` 和 CORS 预检外，所有后端 `/api/**` 请求都必须携带启动时
生成的凭据：

```http
Authorization: Bearer <token>
```

也可使用 `X-API-Key`。凭据保存在 `.runtime/secrets/api-token`，浏览器代码
不得读取；浏览器应请求同源的 Next.js `/api` 代理，由服务端注入凭据。

## Agents

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/agents` | 获取所有智能体列表 |
| POST | `/api/agents` | 创建新智能体 |
| GET | `/api/agents/{name}` | 获取智能体详情 |
| PUT | `/api/agents/{name}` | 更新智能体配置 |
| DELETE | `/api/agents/{name}` | 删除智能体 |
| POST | `/api/agents/{name}/chat` | 同步聊天（非流式） |
| POST | `/api/agents/{name}/chat/stream` | 流式聊天（SSE） |
| GET | `/api/agents/{name}/call-graph` | 获取智能体调用图 |
| POST | `/api/agents/{name}/sub-agents/validate` | 验证子智能体配置 |
| GET | `/api/agents/call-graph` | 获取全局调用图 |

## Conversations

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/agents/{name}/conversations` | 获取会话列表 |
| POST | `/api/agents/{name}/conversations` | 创建新会话 |
| GET | `/api/agents/{name}/conversations/{conversation_id}` | 获取会话详情 |
| PUT | `/api/agents/{name}/conversations/{conversation_id}` | 更新会话 |
| DELETE | `/api/agents/{name}/conversations/{conversation_id}` | 删除会话 |
| POST | `/api/agents/{name}/conversations/{conversation_id}/messages` | 追加消息 |
| POST | `/api/agents/{name}/conversations/{conversation_id}/save` | 保存会话 |

## Knowledge Bases (RAG)

> 知识库管理 API，用于文档上传、向量化、检索等 RAG 功能。

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/knowledge-bases` | 获取所有知识库列表 |
| POST | `/api/knowledge-bases` | 创建新知识库 |
| GET | `/api/knowledge-bases/{kb_id}` | 获取知识库详情 |
| DELETE | `/api/knowledge-bases/{kb_id}` | 删除知识库 |
| GET | `/api/knowledge-bases/{kb_id}/documents` | 获取知识库文档列表 |
| POST | `/api/knowledge-bases/{kb_id}/documents` | 上传文档到知识库 |
| DELETE | `/api/knowledge-bases/{kb_id}/documents/{doc_id}` | 删除知识库文档 |
| POST | `/api/knowledge-bases/{kb_id}/search` | 在知识库中检索 |
| GET | `/api/knowledge-bases/{kb_id}/stats` | 获取知识库统计信息 |

**支持的文档格式**: PDF, DOCX, TXT, MD (自动分块 + 向量化)

## MCP Services

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/mcp-services` | 获取 MCP 服务列表 |
| POST | `/api/mcp-services` | 注册 MCP 服务 |
| GET | `/api/mcp-services/{name}` | 获取服务详情 |
| PUT | `/api/mcp-services/{name}` | 更新服务配置 |
| DELETE | `/api/mcp-services/{name}` | 删除服务 |
| POST | `/api/mcp-services/{name}/test` | 测试连接 |
| GET | `/api/mcp-services/{name}/tools` | 获取可用工具 |
| POST | `/api/mcp-services/{name}/diagnose` | 诊断连接问题 |

远程 MCP URL 会执行出站地址校验。可信私网服务必须加入
`AGENT_BUILDER_SSRF_ALLOWLIST`。`stdio` MCP 会启动本地命令，默认禁用；
只有运维人员显式设置 `AGENT_BUILDER_ALLOW_STDIO_MCP=1` 后才可配置或诊断。

## Skills

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/skills` | 获取技能列表 |
| GET | `/api/skills/{name}` | 获取技能详情 |
| DELETE | `/api/skills/{name}` | 删除技能 |
| GET | `/api/skills/{name}/files` | 获取技能文件列表 |
| GET | `/api/skills/{name}/files/{filepath:path}` | 获取技能文件内容 |
| POST | `/api/skills/upload` | 上传技能包 |

## Model Services

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/model-services` | 获取模型服务列表 |
| POST | `/api/model-services` | 注册模型服务 |
| GET | `/api/model-services/{name}` | 获取服务详情 |
| PUT | `/api/model-services/{name}` | 更新服务配置 |
| DELETE | `/api/model-services/{name}` | 删除服务 |
| POST | `/api/model-services/test` | 测试服务连接 |
| GET | `/api/model-services/default-url/{provider}` | 获取默认 URL |

## Environment

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/agents/{name}/environment` | 使用项目内 uv 创建环境 |
| GET | `/api/agents/{name}/environment` | 获取环境状态 |
| DELETE | `/api/agents/{name}/environment` | 删除环境 |
| POST | `/api/agents/{name}/environment/retry` | 重试环境创建 |
| POST | `/api/agents/{name}/environment/packages` | 安装依赖包 |
| GET | `/api/agents/{name}/environment/packages` | 获取已安装包 |

## Files

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/agents/{name}/files` | 上传文件 |
| GET | `/api/agents/{name}/files` | 获取文件列表 |
| GET | `/api/agents/{name}/files/{file_id}` | 下载文件 |
| DELETE | `/api/agents/{name}/files/{file_id}` | 删除文件 |

## Execution

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/agents/{name}/execute` | 执行脚本 |
| GET | `/api/agents/{name}/executions` | 获取执行记录 |
| GET | `/api/agents/{name}/executions/{execution_id}` | 获取执行详情 |

## System

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/api/system/check-runtime` | 检查项目内 uv/Python 运行时 |
| GET | `/api/system/observability` | 获取当前追踪后端状态 |

## Frontend Redirect (前端重定向)

> 这是 Next.js 服务端路由，不属于后端 Base URL。目标固定为本机追踪面板，
> 不使用请求 `Host` 头构造出站地址。

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/redirect/observability` | 重定向到 `http://127.0.0.1:${PHOENIX_PORT}` |

## Debug & Logging

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/log` | 提交日志 |
| POST | `/api/client-logs` | 提交客户端日志 |
| GET | `/api/debug/logs/{request_id}` | 获取请求日志 |
| GET | `/api/debug/logs` | 获取所有调试日志 |

---

## 数据模型

### AgentConfig
```typescript
interface AgentConfig {
  name: string;
  persona: string;
  model_service: string;           // 模型服务名称
  mcp_services?: string[];         // MCP 服务列表
  skills?: string[];               // 技能列表
  planning_mode?: PlanningMode;    // 规划模式
  short_term_memory?: number;      // 短期记忆轮数
  knowledge_bases?: string[];      // 知识库 ID 列表 (RAG)
}
```

### KnowledgeBase
```typescript
interface KnowledgeBase {
  kb_id: string;                   // 知识库唯一 ID
  name: string;                    // 知识库名称
  description: string;             // 描述
  embedding_model: string;         // 嵌入模型
  created_at: string;              // 创建时间
  updated_at: string;              // 更新时间
  doc_count: number;               // 文档数量
  chunk_count: number;             // 分块数量
  total_size: number;              // 总大小（字节）
}
```

### Document
```typescript
interface Document {
  doc_id: string;                  // 文档唯一 ID
  filename: string;                // 文件名
  file_size: number;               // 文件大小
  mime_type: string;               // MIME 类型
  chunk_count: number;             // 分块数量
  char_count: number;              // 字符数
  status: 'pending' | 'processing' | 'completed' | 'failed';
  uploaded_at: string;             // 上传时间
  processed_at?: string;           // 处理完成时间
  error_message?: string;          // 错误信息
}
```

### PlanningMode
- `react`: Thought → Action → Observation 循环
- `reflexion`: 执行后反思和自我纠正
- `plan_and_solve`: 先规划再执行
- `rewOO`: 无观察规划，并行工具执行
- `tot`: 思维树，探索多条路径

### ModelProvider
- `zhipu`: 智谱 AI
- `alibaba_bailian`: 阿里百炼
- `ollama`: Ollama 本地模型

### MCPConnectionType

- `stdio`: 标准 IO 连接（可信本地命令，需运维显式启用）
- `sse`: Server-Sent Events（远程服务）

---

## 流式事件类型 (SSE)

| 事件类型 | 说明 |
|---------|------|
| `thinking` | 智能体内部思考过程 |
| `content` | 最终回复内容（逐字符流式输出） |
| `tool_call` | 工具调用开始 |
| `tool_result` | 工具执行结果 |
| `skill_loading` | 技能加载中 |
| `skill_loaded` | 技能加载完成 |
| `rag_retrieve` | 知识库检索开始 |
| `rag_sources` | 当前请求的知识库引用来源 |
| `sub_agent_call` | 子智能体调用开始 |
| `sub_agent_result` | 子智能体调用成功 |
| `sub_agent_error` | 子智能体调用失败 |
| `metrics` | 性能指标（延迟、token 数等） |
| `error` | 错误信息 |

---

## 状态码

| 状态码 | 说明 |
|-------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 缺少或无效的 API 凭据 |
| 404 | 资源不存在 |
| 413 | 请求体或上传内容超过限制 |
| 422 | 请求模型校验失败 |
| 500 | 服务器内部错误 |
| 503 | 服务端 API 鉴权未正确配置 |

---

## 更新日志

- **2026-07-16**: 对齐项目内 uv、统一 API 鉴权和本地 OTLP 追踪端点
- **2026-03-17**: 添加 RAG 知识库 API 端点
- **2026-03-16**: 添加调试日志 API
- **2026-03-15**: 添加子智能体验证 API

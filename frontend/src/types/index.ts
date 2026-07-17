/**
 * 类型定义 - 上传文件功能
 *
 * 根据 UX 设计稿 (iteration-2603111255) 和产品需求规格说明书实现
 *
 * 包含文件上传相关的接口定义
 */

/**
 * 上传文件信息（已上传到服务器）
 *
 * 后端响应格式 (backend.py 第1293-1302行):
 * {
 *   "file_id": "uuid",
 *   "filename": "report.pdf",
 *   "file_size": 2457600,
 *   "mime_type": "application/pdf",
 *   "uploaded_at": "2026-03-11T12:00:00Z"
 * }
 */
export interface UploadedFile {
  id: string;           // 文件唯一标识 (UUID) - 对应 file_id
  filename: string;     // 文件名 - 对应 filename
  size: number;         // 文件大小 (bytes) - 对应 file_size
  mimeType: string;     // MIME 类型 - 对应 mime_type
  uploadedAt: string;   // 上传时间 (ISO 8601) - 对应 uploaded_at
}

/**
 * 待发送文件（本地选择，未上传）
 */
export interface PendingFile {
  id: string;           // 临时唯一标识
  file: File;           // 原始 File 对象
  name: string;         // 文件名
  size: number;         // 文件大小 (bytes)
  type: string;         // MIME 类型
}

/**
 * 文件附件（消息中的附件）
 */
export interface FileAttachment {
  id: string;           // 文件标识
  name: string;         // 文件名
  size: number;         // 文件大小 (bytes)
  type: string;         // MIME 类型
  url?: string;         // 上传后的URL（可选）
}

/**
 * 文件上传配置
 */
export interface FileUploadConfig {
  maxFileSize: number;      // 最大文件大小 (bytes)
  allowedTypes: string[];   // 允许的 MIME 类型
  maxFiles: number;         // 最大文件数量
}

/**
 * 默认文件上传配置
 *
 * 根据产品需求规格说明书 (iteration-2603111255):
 * - 单文件最大: 100MB
 * - 单次上传最大: 3个文件
 * - 支持类型: PDF, DOCX, XLSX, TXT, CSV, JSON, 图片
 */
export const DEFAULT_FILE_CONFIG: FileUploadConfig = {
  maxFileSize: 100 * 1024 * 1024,  // 100MB (根据产品需求规格说明书)
  allowedTypes: [
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document', // .docx
    'application/msword',                                                       // .doc
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',       // .xlsx
    'application/vnd.ms-excel',                                                 // .xls
    'text/plain',
    'text/csv',
    'application/json',
    'image/png',
    'image/jpeg',
  ],
  maxFiles: 3,  // 单次最多上传3个文件
};

/**
 * 文件类型图标配置
 */
export interface FileIconConfig {
  icon: string;         // 图标名称
  color: string;        // Tailwind 颜色类
}

/**
 * 获取文件图标配置
 */
export function getFileIconConfig(mimeType: string): FileIconConfig {
  if (mimeType === 'application/pdf') {
    return { icon: 'FileText', color: 'text-red-400' };
  }
  if (mimeType.includes('word') || mimeType.includes('document')) {
    return { icon: 'FileText', color: 'text-blue-400' };
  }
  if (mimeType.includes('spreadsheet') || mimeType.includes('excel')) {
    return { icon: 'FileSpreadsheet', color: 'text-green-400' };
  }
  if (mimeType.startsWith('image/')) {
    return { icon: 'Image', color: 'text-purple-400' };
  }
  return { icon: 'File', color: 'text-gray-400' };
}

/**
 * 格式化文件大小
 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/**
 * 获取文件扩展名
 */
export function getFileExtension(filename: string): string {
  const parts = filename.split('.');
  return parts.length > 1 ? parts.pop()?.toLowerCase() || '' : '';
}

/**
 * 文件上下文（发送消息时携带）
 *
 * 根据 PRD (iteration-2603121000) 需求：
 * - file_ids: 上传文件的 ID 列表
 * - file_infos: 文件的详细信息列表
 */
export interface FileContext {
  file_ids: string[];        // 文件 ID 列表
  file_infos: FileAttachment[]; // 文件信息列表
}

/**
 * Skill 执行状态
 */
export type SkillExecutionStatus =
  | 'idle'         // 空闲
  | 'loading'      // 加载中
  | 'executing'    // 执行中
  | 'completed'    // 完成
  | 'failed';      // 失败

/**
 * Skill 执行信息（用于展示状态）
 */
export interface SkillExecutionInfo {
  skillName: string;          // Skill 名称
  status: SkillExecutionStatus; // 执行状态
  message?: string;           // 状态消息
  error?: string;             // 错误信息
  startedAt?: number;         // 开始时间戳
  finishedAt?: number;        // 结束时间戳
}

// ============================================================================
// 【AC130】Agent-as-a-Tool 多 Agent 嵌套功能类型定义
// ============================================================================

/**
 * 子 Agent 调用状态
 */
export type SubAgentCallStatus =
  | 'pending'     // 等待中
  | 'running'     // 运行中
  | 'completed'   // 完成
  | 'failed'      // 失败
  | 'timeout';    // 超时

/**
 * 子 Agent 调用记录
 */
export interface SubAgentCallRecord {
  id: string;                    // 调用 ID (UUID)
  agentName: string;             // 子 Agent 名称
  message: string;               // 发送给子 Agent 的消息
  status: SubAgentCallStatus;    // 调用状态
  result?: string;               // 调用结果
  error?: string;                // 错误信息
  errorType?: 'timeout' | 'recursion' | 'not_found' | 'exception';  // 错误类型
  durationMs?: number;           // 耗时（毫秒）
  tokens?: {                     // Token 统计
    input: number;
    output: number;
    total: number;
  };
  startTime: number;             // 开始时间戳
  endTime?: number;              // 结束时间戳
}

/**
 * 子 Agent 信息（用于配置）
 */
export interface SubAgentInfo {
  name: string;                  // Agent 名称
  persona: string;               // Agent 人设
  model_service: string | null;  // 模型服务
  skills: string[];              // 启用的技能
  mcp_services: string[];        // 启用的 MCP 服务
  sub_agents?: string[];         // 该 Agent 的子 Agent（用于循环依赖检测）
}

/**
 * Agent 配置扩展（包含子 Agent）
 */
export interface AgentConfigWithSubAgents {
  name: string;
  persona: string;
  model_service: string | null;
  temperature: number;
  max_iterations: number;
  short_term_memory: number;
  planning_mode: string;
  mcp_services: string[];
  skills: string[];
  sub_agents?: string[];         // 【AC130 新增】子 Agent 名称列表
  sub_agent_timeout?: number;    // 【AC130 新增】子 Agent 调用超时（秒）
  sub_agent_max_retries?: number; // 【AC130 新增】子 Agent 调用最大重试次数
}

/**
 * 调用链路追踪摘要
 */
export interface CallTraceSummary {
  traceId: string;               // 链路 ID
  totalTokens: number;           // 总 Token 消耗
  totalDurationMs: number;       // 总耗时（毫秒）
  callCount: number;             // 调用次数
}

/**
 * 循环依赖错误信息
 */
export interface CycleDependencyError {
  error: string;                 // 错误类型 "circular_dependency"
  message: string;               // 用户友好的错误消息
  cycle_path: string[];          // 循环路径（Agent 名称列表）
}

/**
 * SSE 流式事件类型（扩展支持子 Agent 事件）
 */
export type StreamEventType =
  | 'thinking'           // 思考过程
  | 'content'            // 最终回答内容
  | 'tool_call'          // 工具调用开始
  | 'tool_result'        // 工具执行结果
  | 'skill_loading'      // 技能加载中
  | 'skill_loaded'       // 技能加载完成
  | 'metrics'            // 性能指标
  | 'trace_start'        // 【AC130 新增】链路追踪开始
  | 'sub_agent_call'     // 【AC130 新增】子 Agent 调用开始
  | 'sub_agent_result'   // 【AC130 新增】子 Agent 调用结果
  | 'sub_agent_error'    // 【AC130 新增】子 Agent 调用错误
  | 'trace_end';         // 【AC130 新增】链路追踪结束

/**
 * SSE 流式事件基础类型
 */
export interface StreamEvent {
  type: StreamEventType;
}

/**
 * 思考过程事件
 */
export interface ThinkingEvent extends StreamEvent {
  type: 'thinking';
  content: string;
}

/**
 * 内容事件（逐字符流式输出）
 */
export interface ContentEvent extends StreamEvent {
  type: 'content';
  content: string;
}

/**
 * 工具调用事件
 */
export interface ToolCallEvent extends StreamEvent {
  type: 'tool_call';
  name: string;
  call_id?: string;
  service?: string;
  args?: Record<string, unknown>;
}

/**
 * 工具结果事件
 */
export interface ToolResultEvent extends StreamEvent {
  type: 'tool_result';
  name: string;
  call_id?: string;
  result: string;
}

/**
 * 技能加载事件
 */
export interface SkillLoadingEvent extends StreamEvent {
  type: 'skill_loading';
  skill_name: string;
}

/**
 * 技能加载完成事件
 */
export interface SkillLoadedEvent extends StreamEvent {
  type: 'skill_loaded';
  skill_name: string;
  success: boolean;
  error?: string;
}

/**
 * 性能指标事件
 */
export interface MetricsEvent extends StreamEvent {
  type: 'metrics';
  first_token_latency: number;
  total_tokens: number;
  total_duration: number;
  // 上下文窗口状态栏字段
  input_tokens?: number;
  output_tokens?: number;
  context_window?: number;
}

/**
 * 【AC130 新增】链路追踪开始事件
 */
export interface TraceStartEvent extends StreamEvent {
  type: 'trace_start';
  trace_id: string;
}

/**
 * 【AC130 新增】子 Agent 调用事件
 */
export interface SubAgentCallEvent extends StreamEvent {
  type: 'sub_agent_call';
  agent_name: string;
  message: string;
  timeout?: number;
}

/**
 * 【AC130 新增】子 Agent 结果事件
 */
export interface SubAgentResultEvent extends StreamEvent {
  type: 'sub_agent_result';
  agent_name: string;
  result: string;
  duration_ms: number;
  tokens?: {
    input: number;
    output: number;
    total: number;
  };
}

/**
 * 【AC130 新增】子 Agent 错误事件
 */
export interface SubAgentErrorEvent extends StreamEvent {
  type: 'sub_agent_error';
  agent_name: string;
  error: string;
  error_type: 'timeout' | 'recursion' | 'not_found' | 'exception';
}

/**
 * 【AC130 新增】链路追踪结束事件
 */
export interface TraceEndEvent extends StreamEvent {
  type: 'trace_end';
  trace_id: string;
  total_tokens: number;
  total_duration_ms: number;
}

/**
 * 联合类型：所有可能的 SSE 事件
 */
export type ServerSentEvent =
  | ThinkingEvent
  | ContentEvent
  | ToolCallEvent
  | ToolResultEvent
  | SkillLoadingEvent
  | SkillLoadedEvent
  | MetricsEvent
  | TraceStartEvent
  | SubAgentCallEvent
  | SubAgentResultEvent
  | SubAgentErrorEvent
  | TraceEndEvent;

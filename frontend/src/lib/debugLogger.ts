/**
 * ============================================================================
 * 前端调试日志系统
 *
 * 功能：
 * 1. 生成并传递 X-Request-ID 到后端
 * 2. 收集请求摘要（不记录完整对话内容）
 * 3. 记录有界的 SSE 流式输出统计
 * 4. 记录渲染状态和环境指纹
 * 5. 日志导出功能（.log 或 .json 格式）
 * 6. 合并前后端日志
 *
 * 使用方式：
 * ```typescript
 * const logger = DebugLogger.create();
 * logger.logRequestStart(requestSummary);
 * logger.logChunk(chunkData);
 * logger.logRenderState(renderState);
 * const fullLog = logger.export();
 * ```
 * ============================================================================
 */

import { apiPath } from '@/lib/apiPath';

// ============================================================================
// 类型定义
// ============================================================================

export interface RequestPayload {
  agentName: string;
  messageLength: number;
  historyCount: number;
  historyCharacterCount?: number;
  fileCount: number;
  conversationId?: string | null;
}

export interface ChunkData {
  type: string;
  content?: string;
  name?: string;
  args?: Record<string, unknown>;
  result?: string;
  error?: string;
  timestamp: number;
}

export interface RenderState {
  messageCount: number;
  isRunning: boolean;
  hasThinking: boolean;
  toolCallCount: number;
  skillStateCount: number;
}

export interface EnvironmentFingerprint {
  userAgent: string;
  language: string;
  platform: string;
  screenResolution: string;
  viewportSize: string;
  timezone: string;
  locale: string;
  cookieEnabled: boolean;
  onlineStatus: boolean;
  connectionType?: string;
}

export interface BackendLogEntry {
  request_id: string;
  timestamp: string;
  level: string;
  category: string;
  data: unknown;
}

interface BackendModelCall {
  model_name?: string;
  provider?: string;
}

interface BackendToolCall {
  tool_name?: string;
  tool_type?: string;
}

export interface BackendLogPackage {
  meta: {
    version: string;
    exported_at: string;
    request_id: string;
  };
  server: {
    environment?: Record<string, unknown>;
    dependencies?: unknown;
    request?: unknown;
    logs?: BackendLogEntry[];
    model_calls?: BackendModelCall[];
    tool_calls?: BackendToolCall[];
    errors?: unknown[];
  };
}

interface ClientLogEntry {
  timestamp: number;
  category: string;
  data: unknown;
}

interface RenderStateEntry {
  timestamp: number;
  state: RenderState;
}

interface ErrorLogEntry {
  timestamp: number;
  error: string;
  stack?: string;
  context?: unknown;
}

interface DebugLogExport {
  meta: {
    version: string;
    exportedAt: string;
    requestId: string;
    duration: string;
  };
  client: {
    environment: EnvironmentFingerprint;
    request: Record<string, unknown> | null;
    logs: ClientLogEntry[];
    chunks: {
      total: number;
      typeSummary: Record<string, number>;
      samples: Array<ChunkData | { type: "..."; timestamp: number; truncated: true }>;
    };
    renderStates: RenderStateEntry[];
    errors: ErrorLogEntry[];
  };
  server: BackendLogPackage | null;
}

// ============================================================================
// X-Request-ID 生成
// ============================================================================

export function generateRequestId(): string {
  if (globalThis.crypto?.randomUUID) {
    return `req-${globalThis.crypto.randomUUID()}`;
  }
  // 仅供不支持 Web Crypto 的旧环境降级；Request ID 不作为授权凭据。
  return `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

// ============================================================================
// 敏感数据脱敏
// ============================================================================

const SENSITIVE_PATTERNS: Array<{ pattern: RegExp; replacement: string }> = [
  { pattern: /(["']?api[_-]?key["']?\s*[:=]\s*["']?)([^"'}\s]+)(["']?)/gi, replacement: '$1[已脱敏 ***]$3' },
  { pattern: /(["']?token["']?\s*[:=]\s*["']?)([^"'}\s]{10,})(["']?)/gi, replacement: '$1[已脱敏 ****]$3' },
  { pattern: /(["']?password["']?\s*[:=]\s*["']?)([^"'}\s]+)(["']?)/gi, replacement: '$1[已脱敏 ***]$3' },
  { pattern: /(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)/gi, replacement: '$1[已脱敏]' },
  { pattern: /(sk-[a-zA-Z0-9_-]{10,})/g, replacement: '[已脱敏 sk-****]' },
  { pattern: /([?&](?:access[_-]?token|api[_-]?key|token|key|secret)=)([^&#\s]+)/gi, replacement: '$1[已脱敏]' },
];

const MAX_SANITIZE_DEPTH = 5;
const MAX_ARRAY_ITEMS = 20;
const MAX_OBJECT_KEYS = 30;
const MAX_VISITED_NODES = 500;
const MAX_LOG_STRING_LENGTH = 2_000;
const SECRET_KEY_PATTERN = /(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|auth[_-]?value)/i;
const PRIVATE_CONTENT_KEY_PATTERN = /^(?:message|messages|content|history|prompt|input|output|result|chunk|chunkText)$/i;

function sanitizeString(value: string): string {
  let result = value;
  for (const { pattern, replacement } of SENSITIVE_PATTERNS) {
    result = result.replace(pattern, replacement);
  }
  if (result.length > MAX_LOG_STRING_LENGTH) {
    return `${result.slice(0, MAX_LOG_STRING_LENGTH)}...(truncated, original: ${result.length})`;
  }
  return result;
}

function privateValueSummary(value: unknown): string {
  if (typeof value === 'string') return `[omitted private text: ${value.length} chars]`;
  if (Array.isArray(value)) return `[omitted private list: ${value.length} items]`;
  if (value && typeof value === 'object') return '[omitted private object]';
  return '[omitted private value]';
}

/**
 * Produce a JSON-safe, privacy-preserving and strictly bounded log value.
 * Depth, collection size and cycles are bounded before JSON.stringify runs.
 */
export function sanitizeForLogging(data: unknown): unknown {
  const seen = new WeakSet<object>();
  let visitedNodes = 0;

  const visit = (value: unknown, depth: number): unknown => {
    visitedNodes += 1;
    if (visitedNodes > MAX_VISITED_NODES) return '[Log item budget reached]';
    if (typeof value === 'string') return sanitizeString(value);
    if (typeof value === 'bigint') return `${value}n`;
    if (typeof value === 'symbol') return String(value);
    if (typeof value === 'function') return `[Function ${value.name || 'anonymous'}]`;
    if (value === null || typeof value !== 'object') return value;
    if (seen.has(value)) return '[Circular]';
    if (depth >= MAX_SANITIZE_DEPTH) return '[Max depth reached]';
    seen.add(value);

    if (Array.isArray(value)) {
      const result = value.slice(0, MAX_ARRAY_ITEMS).map((item) => visit(item, depth + 1));
      if (value.length > MAX_ARRAY_ITEMS) {
        result.push(`[${value.length - MAX_ARRAY_ITEMS} more items omitted]`);
      }
      return result;
    }

    if (value instanceof Error) {
      return {
        name: value.name,
        message: sanitizeString(value.message),
        stack: value.stack ? sanitizeString(value.stack) : undefined,
      };
    }

    const sanitized: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
    let entryCount = 0;
    let truncated = false;
    try {
      for (const key in value) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
        if (entryCount >= MAX_OBJECT_KEYS) {
          truncated = true;
          break;
        }
        entryCount += 1;
        const descriptor = Object.getOwnPropertyDescriptor(value, key);
        const item = descriptor && 'value' in descriptor ? descriptor.value : '[Getter omitted]';
        if (SECRET_KEY_PATTERN.test(key)) {
          sanitized[key] = '[REDACTED]';
        } else if (PRIVATE_CONTENT_KEY_PATTERN.test(key)) {
          sanitized[key] = privateValueSummary(item);
        } else {
          sanitized[key] = visit(item, depth + 1);
        }
      }
    } catch {
      return '[Uninspectable object]';
    }
    if (truncated) {
      sanitized.__omittedKeys = 'additional keys omitted';
    }
    return sanitized;
  };

  return visit(data, 0);
}

// ============================================================================
// 环境指纹采集
// ============================================================================

export function collectEnvironmentFingerprint(): EnvironmentFingerprint {
  const fingerprint: EnvironmentFingerprint = {
    userAgent: navigator.userAgent,
    language: navigator.language,
    platform: navigator.platform,
    screenResolution: `${screen.width}x${screen.height}`,
    viewportSize: `${window.innerWidth}x${window.innerHeight}`,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: navigator.language,
    cookieEnabled: navigator.cookieEnabled,
    onlineStatus: navigator.onLine,
  };

  // 网络连接类型（如果支持）
  if ('connection' in navigator) {
    const conn = (navigator as Navigator & {
      connection?: { effectiveType?: string };
    }).connection;
    if (conn) {
      fingerprint.connectionType = conn.effectiveType || 'unknown';
    }
  }

  return fingerprint;
}

// ============================================================================
// 调试日志记录器
// ============================================================================

export class DebugLogger {
  private static readonly MAX_LOGS = 500;
  private static readonly MAX_CHUNKS = 500;
  private static readonly MAX_RENDER_STATES = 200;
  private static readonly MAX_ERRORS = 100;
  private static readonly MAX_CHUNK_TYPES = 50;
  private requestId: string;
  private startTime: number;
  private logs: ClientLogEntry[] = [];
  private chunks: ChunkData[] = [];
  private totalChunkCount = 0;
  private readonly chunkTypeCounts: Record<string, number> = Object.create(null) as Record<string, number>;
  private payload: Record<string, unknown> | null = null;
  private renderStates: RenderStateEntry[] = [];
  private errors: ErrorLogEntry[] = [];
  private backendLogPackage: BackendLogPackage | null = null;

  private constructor(requestId: string) {
    this.requestId = requestId;
    this.startTime = Date.now();
    this.log('logger_init', { requestId });
  }

  static create(requestId?: string): DebugLogger {
    return new DebugLogger(requestId || generateRequestId());
  }

  getRequestId(): string {
    return this.requestId;
  }

  // ========================================================================
  // 日志记录方法
  // ========================================================================

  log(category: string, data: unknown): void {
    const entry = {
      timestamp: Date.now(),
      category: sanitizeString(category),
      data: sanitizeForLogging(data),
    };
    this.logs.push(entry);
    if (this.logs.length > DebugLogger.MAX_LOGS) {
      this.logs.splice(0, this.logs.length - DebugLogger.MAX_LOGS);
    }
  }

  logRequestStart(payload: RequestPayload): void {
    this.payload = {
      agentName: sanitizeString(payload.agentName),
      messageLength: payload.messageLength,
      historyCount: payload.historyCount,
      historyCharacterCount: payload.historyCharacterCount || 0,
      fileCount: payload.fileCount,
      conversationId: payload.conversationId ? sanitizeString(payload.conversationId) : null,
    };
    // 只写入上面的明确允许字段，避免通过展开 payload 重新带入对话正文。
    this.log('request_start', this.payload);
  }

  logChunk(chunkText: string, parsedData?: { type?: string; name?: string; error?: string }): void {
    const requestedType = sanitizeString(parsedData?.type || 'unknown').slice(0, 100);
    const type = Object.hasOwn(this.chunkTypeCounts, requestedType)
      || Object.keys(this.chunkTypeCounts).length < DebugLogger.MAX_CHUNK_TYPES
      ? requestedType
      : 'other';
    const chunkData: ChunkData = {
      timestamp: Date.now(),
      type,
      name: parsedData?.name ? sanitizeString(parsedData.name) : undefined,
      error: parsedData?.error ? sanitizeString(parsedData.error) : undefined,
      // 只保留长度，不在浏览器中复制 SSE 正文。
      args: { characterCount: chunkText.length },
    };
    if (this.chunks.length < DebugLogger.MAX_CHUNKS) {
      this.chunks.push(chunkData);
    } else {
      this.chunks[this.totalChunkCount % DebugLogger.MAX_CHUNKS] = chunkData;
    }
    this.totalChunkCount += 1;
    this.chunkTypeCounts[type] = (this.chunkTypeCounts[type] || 0) + 1;

    // 每 50 个 chunk 记录一次摘要
    if (this.totalChunkCount % 50 === 0) {
      this.log('chunk_summary', {
        chunkCount: this.totalChunkCount,
        types: this.getChunkTypeSummary(),
      });
    }
  }

  logRenderState(state: RenderState): void {
    this.renderStates.push({
      timestamp: Date.now(),
      state: {
        messageCount: state.messageCount,
        isRunning: state.isRunning,
        hasThinking: state.hasThinking,
        toolCallCount: state.toolCallCount,
        skillStateCount: state.skillStateCount,
      },
    });
    if (this.renderStates.length > DebugLogger.MAX_RENDER_STATES) {
      this.renderStates.splice(0, this.renderStates.length - DebugLogger.MAX_RENDER_STATES);
    }
  }

  logError(error: Error, context?: unknown): void {
    this.errors.push({
      timestamp: Date.now(),
      error: sanitizeString(error.message),
      stack: error.stack ? sanitizeString(error.stack) : undefined,
      context: sanitizeForLogging(context),
    });
    if (this.errors.length > DebugLogger.MAX_ERRORS) {
      this.errors.splice(0, this.errors.length - DebugLogger.MAX_ERRORS);
    }
    this.log('error', {
      message: error.message,
      name: error.name,
      context,
    });
  }

  logSSEEvent(eventType: string, eventData: unknown): void {
    this.log(`sse_${eventType}`, {
      dataType: Array.isArray(eventData) ? 'array' : typeof eventData,
      characterCount: typeof eventData === 'string' ? eventData.length : undefined,
      itemCount: Array.isArray(eventData) ? eventData.length : undefined,
    });
  }

  // ========================================================================
  // 后端日志合并
  // ========================================================================

  async fetchBackendLogs(): Promise<void> {
    try {
      const response = await fetch(apiPath('debug', 'logs', this.requestId));
      if (response.ok) {
        this.backendLogPackage = sanitizeForLogging(await response.json()) as BackendLogPackage;
        this.log('backend_logs_fetched', {
          logCount: this.backendLogPackage?.server?.logs?.length || 0,
          toolCallCount: this.backendLogPackage?.server?.tool_calls?.length || 0,
          modelCallCount: this.backendLogPackage?.server?.model_calls?.length || 0,
        });
      }
    } catch (error) {
      this.log('backend_logs_fetch_failed', {
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  // ========================================================================
  // 导出功能
  // ========================================================================

  export(format: 'json' | 'log' = 'json'): string {
    const endTime = Date.now();
    const duration = endTime - this.startTime;

    const orderedChunks = this.getOrderedChunks();
    const fullLog: DebugLogExport = {
      meta: {
        version: '1.0',
        exportedAt: new Date().toISOString(),
        requestId: this.requestId,
        duration: `${duration}ms`,
      },
      client: {
        environment: collectEnvironmentFingerprint(),
        request: this.payload,
        logs: this.logs,
        chunks: {
          total: this.totalChunkCount,
          typeSummary: this.getChunkTypeSummary(),
          // 只保留前 100 和后 50 个 chunk，避免过大
          samples: [
            ...orderedChunks.slice(0, 100),
            ...orderedChunks.length > 150
              ? [{ type: '...' as const, timestamp: 0, truncated: true as const }]
              : [],
            ...orderedChunks.length > 150 ? orderedChunks.slice(-50) : [],
          ],
        },
        renderStates: this.renderStates,
        errors: this.errors,
      },
      server: this.backendLogPackage || null,
    };

    if (format === 'json') {
      return JSON.stringify(fullLog, null, 2);
    } else {
      return this.formatAsText(fullLog);
    }
  }

  private formatAsText(log: DebugLogExport): string {
    const lines: string[] = [];

    lines.push('='.repeat(60));
    lines.push(`调试日志报告 - ${log.meta.requestId}`);
    lines.push(`导出时间: ${log.meta.exportedAt}`);
    lines.push(`持续时间: ${log.meta.duration}`);
    lines.push('='.repeat(60));
    lines.push('');

    // 客户端环境
    lines.push('--- 客户端环境 ---');
    const env = log.client.environment;
    lines.push(`User Agent: ${env.userAgent}`);
    lines.push(`Platform: ${env.platform}`);
    lines.push(`屏幕: ${env.screenResolution}`);
    lines.push(`视口: ${env.viewportSize}`);
    lines.push(`时区: ${env.timezone}`);
    lines.push(`语言: ${env.locale}`);
    lines.push(`在线: ${env.onlineStatus}`);
    lines.push('');

    // 请求信息
    if (log.client.request) {
      lines.push('--- 请求信息 ---');
      lines.push(`Agent: ${log.client.request.agentName}`);
      lines.push(`消息长度: ${log.client.request.messageLength}`);
      lines.push(`历史消息数: ${log.client.request.historyCount}`);
      lines.push('');
    }

    // Chunk 统计
    lines.push('--- SSE Chunk 统计 ---');
    lines.push(`总计: ${log.client.chunks.total} 个`);
    if (log.client.chunks.typeSummary) {
      Object.entries(log.client.chunks.typeSummary).forEach(([type, count]) => {
        lines.push(`  ${type}: ${count}`);
      });
    }
    lines.push('');

    // 渲染状态
    if (log.client.renderStates.length > 0) {
      lines.push('--- 渲染状态记录 ---');
      log.client.renderStates.forEach((record, i) => {
        const time = new Date(record.timestamp).toISOString().substring(11, 23);
        lines.push(`[${time}] #${i + 1}: running=${record.state.isRunning}, messages=${record.state.messageCount}, tools=${record.state.toolCallCount}`);
      });
      lines.push('');
    }

    // 错误
    if (log.client.errors.length > 0) {
      lines.push('--- 错误记录 ---');
      log.client.errors.forEach((err) => {
        const time = new Date(err.timestamp).toISOString();
        lines.push(`[${time}] ${err.error}`);
        if (err.stack) {
          lines.push(`  Stack: ${err.stack.split('\n')[0]}`);
        }
      });
      lines.push('');
    }

    // 后端日志
    if (log.server) {
      const server = log.server.server;
      lines.push('--- 后端日志 ---');
      if (server.environment) {
        lines.push('环境信息:');
        Object.entries(server.environment).forEach(([key, value]) => {
          if (key !== 'error') {
            lines.push(`  ${key}: ${String(value)}`);
          }
        });
      }
      if (server.model_calls && server.model_calls.length > 0) {
        lines.push(`模型调用: ${server.model_calls.length} 次`);
        server.model_calls.forEach((call) => {
          lines.push(`  - ${call.model_name} (${call.provider})`);
        });
      }
      if (server.tool_calls && server.tool_calls.length > 0) {
        lines.push(`工具调用: ${server.tool_calls.length} 次`);
        server.tool_calls.forEach((call) => {
          lines.push(`  - ${call.tool_name} (${call.tool_type})`);
        });
      }
      if (server.errors && server.errors.length > 0) {
        lines.push(`错误: ${server.errors.length} 个`);
      }
      lines.push('');
    }

    lines.push('='.repeat(60));
    lines.push('--- 完整日志结束 ---');

    return lines.join('\n');
  }

  // ========================================================================
  // 辅助方法
  // ========================================================================

  private getChunkTypeSummary(): Record<string, number> {
    return { ...this.chunkTypeCounts };
  }

  private getOrderedChunks(): ChunkData[] {
    if (this.totalChunkCount <= DebugLogger.MAX_CHUNKS) return this.chunks.slice();
    const start = this.totalChunkCount % DebugLogger.MAX_CHUNKS;
    return [...this.chunks.slice(start), ...this.chunks.slice(0, start)];
  }

  getDuration(): number {
    return Date.now() - this.startTime;
  }
}

// ============================================================================
// 全局日志存储（用于调试）
// ============================================================================

const globalLogStore = new Map<string, DebugLogger>();
const MAX_GLOBAL_LOGGERS = 20;

export function getGlobalLogger(requestId: string): DebugLogger | undefined {
  return globalLogStore.get(requestId);
}

export function setGlobalLogger(logger: DebugLogger): void {
  globalLogStore.set(logger.getRequestId(), logger);
  while (globalLogStore.size > MAX_GLOBAL_LOGGERS) {
    const oldestRequestId = globalLogStore.keys().next().value as string | undefined;
    if (!oldestRequestId) break;
    globalLogStore.delete(oldestRequestId);
  }
}

export function removeGlobalLogger(requestId: string): void {
  globalLogStore.delete(requestId);
}

export function listGlobalLoggers(): Array<{ requestId: string; duration: number }> {
  return Array.from(globalLogStore.values()).map(logger => ({
    requestId: logger.getRequestId(),
    duration: logger.getDuration(),
  }));
}

// 清理超过 1 小时的日志
export function cleanupOldLoggers(): number {
  const cutoff = Date.now() - 3600000; // 1 hour
  let cleaned = 0;
  for (const [requestId, logger] of globalLogStore.entries()) {
    if (logger.getDuration() > cutoff) {
      globalLogStore.delete(requestId);
      cleaned++;
    }
  }
  return cleaned;
}

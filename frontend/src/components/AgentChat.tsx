/**
 * ============================================================================
 * 【流式输出前端渲染组件 - 谨慎修改】
 *
 * 此组件是调试对话的核心 UI，负责：
 * 1. 发送流式请求到 /stream/agents/{name}/chat
 * 2. 解析 SSE (Server-Sent Events) 数据
 * 3. 实时渲染思考过程、工具调用、最终回答
 * 4. 实现打字机效果
 *
 * 关键技术：
 * 1. ReadableStream API - 逐块读取 SSE 数据
 * 2. flushSync - 强制 React 同步渲染，确保打字机效果
 * 3. useRef 累积流式内容并按帧合并渲染 - 限制高频重渲染
 *
 * ⚠️ 修改此文件可能影响：
 * - 打字机效果的流畅性
 * - 思考过程的实时更新
 * - 工具调用的正确展示
 * - 性能指标的显示
 *
 * 相关文件：
 * - backend.py: chat_stream() - 后端 SSE 端点
 * - src/agent_engine.py: stream() - 事件生成
 * - frontend/src/app/stream/agents/[name]/chat/route.ts - 流式代理
 * ============================================================================
 */
/**
 * @userGuide
 * @title.en Chat Interface
 * @title.zh 聊天界面
 * @category core
 * @description.en The main conversation area where you interact with your AI agent.
 *   Type messages, attach files, and see real-time responses with typewriter effect.
 * @description.zh 与AI智能体对话的主要区域。输入消息、附加文件，并观看打字机效果的实时响应。
 *
 * @steps.en
 *   1. Type your message in the text input at the bottom
 *   2. Optionally attach files using the paperclip icon (max 3 files, 100MB each)
 *   3. Press Enter or click Send to submit your message
 *   4. Watch the AI respond with real-time streaming
 *   5. View thinking process and tool calls in expandable sections
 * @steps.zh
 *   1. 在底部的文本输入框中输入消息
 *   2. 可选：使用回形针图标附加文件（最多3个文件，每个100MB）
 *   3. 按回车键或点击发送按钮提交消息
 *   4. 观看AI的实时流式响应
 *   5. 在可展开区域查看思考过程和工具调用
 *
 * @tips.en
 *   - Use Shift+Enter for multi-line messages
 *   - Click on tool calls to see what the agent is doing
 *   - Performance metrics show response time and token usage
 * @tips.zh
 *   - 使用 Shift+Enter 输入多行消息
 *   - 点击工具调用查看智能体的操作
 *   - 性能指标显示响应时间和Token使用量
 *
 * @related KnowledgeBaseSelector, FileUploader
 */
'use client';

import { useState, useRef, useCallback, useEffect } from 'react';
import { flushSync } from 'react-dom';  // 【关键】flushSync 用于强制同步渲染
import { useLocale } from '@/lib/LocaleContext';
import { ChevronDown, ChevronRight, Wrench, Lightbulb, Loader2, Clock, Zap, Hash, Paperclip, FileText, FileSpreadsheet, Image as ImageIcon, File, X, Database, Square } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { PendingFile, FileAttachment, DEFAULT_FILE_CONFIG, formatFileSize, FileContext, SubAgentCallRecord } from '@/types';
import { SubAgentCallCard } from '@/components/SubAgentCallCard';
import { ContextStatusBar } from '@/components/ContextStatusBar';
import { uploadFile } from '@/lib/fileApi';
import { DebugLogger, generateRequestId, sanitizeForLogging } from '@/lib/debugLogger';
import { buildHistoryMessages, finalizeTurnMessages } from '@/lib/chatMessages';
import { streamPath } from '@/lib/apiPath';

// 本地日志只保留最近记录，避免长会话无限占用浏览器内存。
const localLogs: string[] = [];
const MAX_LOCAL_LOGS = 500;
const MAX_LOCAL_LOG_ENTRY_LENGTH = 8_000;

const addLog = (type: string, message: string, details?: unknown) => {
  const timestamp = new Date().toISOString();
  const sanitizedDetails = details === undefined ? '' : JSON.stringify(sanitizeForLogging(details), null, 2);
  const logEntry = `[${timestamp}] [${type}] ${message}${sanitizedDetails ? '\n  ' + sanitizedDetails.replace(/\n/g, '\n  ') : ''}`
    .slice(0, MAX_LOCAL_LOG_ENTRY_LENGTH);
  localLogs.push(logEntry);
  if (localLogs.length > MAX_LOCAL_LOGS) {
    localLogs.splice(0, localLogs.length - MAX_LOCAL_LOGS);
  }
  console.log(logEntry);
};

// 只在关键点记录日志，避免过多
const addChunkLog = (chunkNum: number, totalBytes: number) => {
  // 每 10 个 chunk 记录一次纯计数摘要。
  if (chunkNum % 10 === 0) {
    addLog('DEBUG', `已接收 ${chunkNum} 个数据块`, { totalBytes });
  }
};

interface ToolArguments extends Record<string, unknown> {
  skill_name?: string;
  query?: string;
}

interface ToolCall {
  name: string;
  call_id?: string;  // 唯一标识符，用于区分同名工具的多次调用
  service?: string;  // 来源服务名
  args: ToolArguments;
  result?: string;
}

/**
 * Skill 执行状态 (T017)
 */
interface SkillExecutionState {
  skillName: string;
  status: 'loading' | 'executing' | 'completed' | 'failed';
  message?: string;
  error?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  // 思考过程相关
  thinking?: string;
  toolCalls?: ToolCall[];
  isThinkingExpanded?: boolean;
  // 性能指标
  metrics?: PerformanceMetrics;
  // 已加载的技能
  loadedSkills?: string[];
  // 文件附件
  attachments?: FileAttachment[];
  // Skill 执行状态 (T017)
  skillStates?: SkillExecutionState[];
  // 当前正在加载的技能
  loadingSkill?: string;
  // 【AC130 新增】子 Agent 调用记录
  subAgentCalls?: SubAgentCallRecord[];
  // 【AC130-202603170949】RAG 知识库检索状态
  ragRetrievals?: RAGRetrieval[];
  // 【Phase3-Task3.3】RAG 来源引用
  ragSources?: RAGSource[];
}

/**
 * RAG 检索记录
 */
interface RAGRetrieval {
  query: string;
  status: 'retrieving' | 'completed' | 'failed';
  results?: string;
  call_id?: string;
}

/**
 * RAG 来源引用
 */
interface RAGSource {
  filename: string;
  chunk_index: number;
  score: number;
}

interface PerformanceMetrics {
  first_token_latency: number;  // 首 token 时延（毫秒）
  total_tokens: number;         // 总 token 数
  total_duration: number;       // 整体耗时（毫秒）
}

interface AgentChatProps {
  agentName: string;
  shortTermMemory?: number;
  conversationId?: string | null;
  initialMessages?: ChatMessage[];  // 新增：用于加载历史会话消息
  onConversationChange?: (
    id: string | null | undefined,
    messages: ChatMessage[],
    turnMessages?: ChatMessage[],
  ) => void;
  onCreateConversation?: () => Promise<string | null | undefined>;  // 新增：创建会话的回调
}

export function AgentChat({ agentName, shortTermMemory = 5, conversationId, initialMessages, onConversationChange, onCreateConversation }: AgentChatProps) {
  const { locale, t } = useLocale();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [, setHasError] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);  // 待发送的文件

  // 上下文窗口状态栏
  const [contextMetrics, setContextMetrics] = useState({
    inputTokens: 0,
    outputTokens: 0,
    contextWindow: 0,
  });

  // ========== file_context 管理 (T016) ==========
  // 文件上下文：维护已上传文件的 ID 和信息
  const [fileContext, setFileContext] = useState<FileContext>({
    file_ids: [],
    file_infos: []
  });
  // 上传状态
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadingFileName, setUploadingFileName] = useState<string>('');

  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesContainerRef = useRef<HTMLDivElement | null>(null);

  // ========== Debug Logger 集成 ==========
  // 调试日志记录器，用于采集请求、响应、SSE chunks 等数据
  const debugLoggerRef = useRef<DebugLogger | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);  // 文件输入引用

  // 使用 ref 存储流式内容，避免频繁触发重渲染
  const streamingContentRef = useRef<string>("");
  const streamingThinkingRef = useRef<string>("");
  const streamingToolCallsRef = useRef<ToolCall[]>([]);
  const streamingLoadedSkillsRef = useRef<string[]>([]);
  // T017: Skill 执行状态
  const streamingSkillStatesRef = useRef<SkillExecutionState[]>([]);
  // 【AC130 新增】子 Agent 调用记录
  const streamingSubAgentCallsRef = useRef<SubAgentCallRecord[]>([]);
  // 【AC130-202603170949】RAG 知识库检索记录
  const streamingRagRetrievalsRef = useRef<RAGRetrieval[]>([]);
  // 【Phase3-Task3.3】RAG 来源引用
  const streamingRagSourcesRef = useRef<RAGSource[]>([]);

  // 【修复】用于在渲染完成后安全地调用 onConversationChange
  // 避免 "Cannot update a component while rendering a different component" 错误
  const pendingConversationUpdateRef = useRef<{
    conversationId: string | null | undefined;
    messages: ChatMessage[];
    turnMessages: ChatMessage[];
  } | null>(null);

  // 【修复-2603131100】防止 onConversationChange 重复调用
  // onConversationChange 是内联函数，每次父组件渲染都会变化
  // 这导致 useEffect 被多次触发，需要跟踪上次处理的会话
  const lastProcessedConversationRef = useRef<string | null>(null);

  // 【修复-2603141800】使用 ref 跟踪运行状态，避免竞态条件
  // isRunning state 有异步延迟，使用 ref 可以同步检查和设置
  const isRunningRef = useRef(false);

  // 【修复-20260319】跟踪本地消息更新，防止 initialMessages useEffect 覆盖本地更新
  // 当本地消息更新时，设置此标志，防止 initialMessages useEffect 在短时间内覆盖
  const localMessageUpdateRef = useRef(false);

  // 【修复-2603141800】使用 ref 存储最新的 messages，避免 handleSend 闭包问题
  const messagesRef = useRef<ChatMessage[]>([]);
  const updateMessages = useCallback((updater: (current: ChatMessage[]) => ChatMessage[]) => {
    const nextMessages = updater(messagesRef.current);
    messagesRef.current = nextMessages;
    setMessages(nextMessages);
    return nextMessages;
  }, []);

  // 自动滚动到底部
  const scrollToBottom = useCallback(() => {
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
    }
  }, []);

  // 当消息变化时自动滚动
  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // ========== 技能名称规范化 (T017修复) ==========
  /**
   * 规范化技能名称为标准格式
   * - 转小写
   * - 移除特殊字符前缀（如 "AB-" 前缀）
   * - 提取核心关键词（如 "ab-pdf-processing-guide" -> "ab-pdf"）
   */
  const normalizeSkillName = useCallback((name: string): string => {
    if (!name) return name;
    // 转小写
    let normalized = name.toLowerCase();
    // 移除空格，替换为连字符
    normalized = normalized.replace(/\s+/g, '-');
    // 提取核心关键词（取第一个连字符分隔的部分作为基础）
    const parts = normalized.split('-');
    // 通常技能名称格式为 "ab-pdf" 或 "ab-pdf-processing-guide"
    // 我们取前两个有意义的部分作为规范化名称
    if (parts.length >= 2) {
      // 检查是否有常见的技能前缀（如 "ab"）
      const commonPrefixes = ['ab', 'an', 'example', 'skills'];
      if (commonPrefixes.includes(parts[0]) && parts[1]) {
        return `${parts[0]}-${parts[1]}`;
      }
    }
    return normalized;
  }, []);

  /**
   * 查找或创建技能状态
   * 使用规范化名称进行匹配，确保不同格式的名称能指向同一个状态
   */
  const findOrCreateSkillState = useCallback((
    currentStates: SkillExecutionState[],
    skillName: string,
    defaultState: Omit<SkillExecutionState, 'skillName'>
  ): { states: SkillExecutionState[], existingIndex?: number } => {
    const normalized = normalizeSkillName(skillName);

    // 先尝试精确匹配
    let existingIndex = currentStates.findIndex(s => s.skillName === skillName);

    // 如果没找到，尝试规范化匹配
    if (existingIndex === -1) {
      existingIndex = currentStates.findIndex(s => normalizeSkillName(s.skillName) === normalized);
    }

    if (existingIndex !== -1) {
      // 更新现有状态
      const updatedStates = [...currentStates];
      updatedStates[existingIndex] = {
        ...updatedStates[existingIndex],
        ...defaultState,
        skillName: updatedStates[existingIndex].skillName  // 保持原有名称
      };
      return { states: updatedStates, existingIndex };
    } else {
      // 创建新状态
      const newState: SkillExecutionState = {
        skillName,
        ...defaultState
      };
      return {
        states: [...currentStates, newState],
        existingIndex: undefined
      };
    }
  }, [normalizeSkillName]);

  // 【新增】监听 initialMessages 变化，用于加载历史会话
  useEffect(() => {
    // 只有当 initialMessages 不是 undefined 时才更新（允许空数组）
    if (initialMessages !== undefined) {
      // 【修复-20260319】如果本地刚更新了消息，跳过 initialMessages 的更新
      // 防止循环依赖：handleSend -> onConversationChange -> initialMessages -> 覆盖 messages
      if (localMessageUpdateRef.current) {
        localMessageUpdateRef.current = false; // 重置标志
        return;
      }
      setMessages(initialMessages);
      // 【修复-2603141800】同步更新 messagesRef
      messagesRef.current = initialMessages;
    }
  }, [initialMessages]);

  // 【修复】在渲染完成后安全地调用 onConversationChange
  // 这避免了在 setMessages 回调中直接调用导致的渲染期间 setState 错误
  // 【修复-20260317】使用 setTimeout 确保 onConversationChange 不阻塞 UI 更新
  useEffect(() => {
    const pendingUpdate = pendingConversationUpdateRef.current;
    // 【修复-2603131100】检查是否已处理过该会话，防止重复调用
    // onConversationChange 是内联函数，每次父组件渲染都会变化
    // 导致这个 useEffect 被多次触发，需要通过 conversationId 去重
    if (pendingUpdate && onConversationChange) {
      const convId = pendingUpdate.conversationId;
      const lastTurnMessage = pendingUpdate.turnMessages.at(-1);
      const updateKey = `${convId}-${lastTurnMessage?.id ?? 'none'}-${lastTurnMessage?.content.length ?? 0}`;
      if (lastProcessedConversationRef.current !== updateKey) {
        lastProcessedConversationRef.current = updateKey;
        pendingConversationUpdateRef.current = null; // 清除待处理的更新

        // 【修复-20260317】使用 setTimeout 延迟调用，确保不阻塞 UI 渲染
        // 这样 setIsRunning(false) 的效果可以立即反映到 UI
        setTimeout(() => {
          onConversationChange(
            pendingUpdate.conversationId,
            pendingUpdate.messages,
            pendingUpdate.turnMessages,
          );
        }, 0);
      } else {
        pendingConversationUpdateRef.current = null; // 清除待处理的更新
      }
    }
  }, [messages, onConversationChange]);

  // REQ-1.3: 监听 agentName 变化，重置内部状态
  // 使用 useRef 跟踪上一个 agentName 来检测变化
  const prevAgentNameRef = useRef(agentName);
  useEffect(() => {
    if (prevAgentNameRef.current !== agentName) {
      // agentName 发生变化，重置所有状态
      setMessages([]);
      // 【修复-2603141800】同步重置 messagesRef
      messagesRef.current = [];
      setFileContext({ file_ids: [], file_infos: [] });
      setInputValue('');
      setHasError(false);
      setPendingFiles([]);
      prevAgentNameRef.current = agentName;
    }
  }, [agentName]);

  const downloadLogs = async () => {
    if (!debugLoggerRef.current) {
      // Fallback to old behavior if logger not available
      const clientLog = locale === "zh"
        ? `=== 客户端日志 ===\n${localLogs.join('\n\n')}`
        : `=== Client Logs ===\n${localLogs.join('\n\n')}`;

      const allLogs = `${clientLog}\n\n=== 服务端日志 ===\n(无法获取)`;

      const blob = new Blob([allLogs], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `chat-debug-log-${Date.now()}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      return;
    }

    // 使用 DebugLogger 导出日志
    try {
      // 尝试获取后端日志
      await debugLoggerRef.current.fetchBackendLogs();

      // 导出为 JSON 格式
      const logData = debugLoggerRef.current.export('json');
      const blob = new Blob([logData], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `debug-log-${agentName}-${Date.now()}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Failed to export logs:', error);

      // Fallback to client-only logs
      const logData = debugLoggerRef.current.export('json');
      const blob = new Blob([logData], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `debug-log-client-only-${Date.now()}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }
  };

  // ========== 文件上传函数 (T016) ==========
  /**
   * 上传待发送的文件到后端
   * @returns 上传成功的文件信息列表
   */
  const uploadPendingFiles = useCallback(async (files: PendingFile[]): Promise<FileAttachment[]> => {
    if (files.length === 0) return [];

    const uploadedFiles: FileAttachment[] = [];
    const failedFiles: string[] = [];

    setIsUploading(true);
    setUploadProgress(0);

    addLog('INFO', locale === "zh" ? `开始上传 ${files.length} 个文件` : `Starting to upload ${files.length} files`);

    for (let i = 0; i < files.length; i++) {
      const pf = files[i];
      setUploadingFileName(pf.name);
      setUploadProgress(Math.round((i / files.length) * 100));

      try {
        const uploadedFile = await uploadFile(agentName, pf.file);
        uploadedFiles.push({
          id: uploadedFile.id,
          name: uploadedFile.filename,
          size: uploadedFile.size,
          type: uploadedFile.mimeType
        });
        addLog('INFO', locale === "zh" ? `文件上传成功: ${pf.name}` : `File uploaded: ${pf.name}`, { file_id: uploadedFile.id });
      } catch (e) {
        failedFiles.push(pf.name);
        console.error('文件上传失败:', pf.name, e);
        addLog('ERROR', locale === "zh" ? `文件上传失败: ${pf.name}` : `File upload failed: ${pf.name}`, { error: String(e) });
      }
    }

    setUploadProgress(100);
    setIsUploading(false);
    setUploadingFileName('');

    addLog('INFO', locale === "zh" ? `文件上传完成，成功 ${uploadedFiles.length} 个，失败 ${failedFiles.length} 个` : `File upload completed, ${uploadedFiles.length} success, ${failedFiles.length} failed`);

    return uploadedFiles;
  }, [agentName, locale]);

  /**
   * 清除文件上下文
   */
  const clearFileContext = useCallback(() => {
    setFileContext({
      file_ids: [],
      file_infos: []
    });
    setPendingFiles([]);
  }, []);

  /**
   * 移除单个已上传的文件
   */
  const removeUploadedFile = useCallback((fileId: string) => {
    setFileContext(prev => ({
      file_ids: prev.file_ids.filter(id => id !== fileId),
      file_infos: prev.file_infos.filter(info => info.id !== fileId)
    }));
  }, []);

  const handleSend = useCallback(async () => {
    // 【修复-2603141800】使用 ref 同步检查运行状态，避免竞态条件
    // isRunningRef 是同步的，而 isRunning state 有异步延迟
    if (isRunningRef.current) return;

    // 修改条件：允许只有文件没有文本，或者有文本
    if (!inputValue.trim() && pendingFiles.length === 0 && fileContext.file_ids.length === 0) return;

    // 【修复-2603141800】立即设置运行状态（使用 ref 和 state）
    isRunningRef.current = true;
    setIsRunning(true);

    const userContent = inputValue.trim();
    const currentPendingFiles = [...pendingFiles];  // 保存当前待发送文件
    setInputValue('');
    setPendingFiles([]);  // 清空待发送文件
    setHasError(false);

    // 如果没有 conversationId，先创建会话
    let activeConversationId = conversationId;
    if (!activeConversationId && onCreateConversation) {
      try {
        const newId = await onCreateConversation();
        if (newId) {
          activeConversationId = newId;
        }
      } catch (error) {
        console.error('Failed to create conversation:', error);
      }
    }

    const requestUrl = streamPath('agents', agentName, 'chat');

    // ========== T016: 使用 file_context 管理文件 ==========
    // 1. 先上传新的待发送文件
    let newUploadedFiles: FileAttachment[] = [];
    if (currentPendingFiles.length > 0) {
      newUploadedFiles = await uploadPendingFiles(currentPendingFiles);
    }

    // 2. 合并已有的文件上下文和新上传的文件
    const allFileIds = [...fileContext.file_ids, ...newUploadedFiles.map(f => f.id)];
    const allFileInfos = [...fileContext.file_infos, ...newUploadedFiles];

    // 3. 更新文件上下文（文件在对话中持续有效）
    if (newUploadedFiles.length > 0) {
      setFileContext({
        file_ids: allFileIds,
        file_infos: allFileInfos
      });
    }
    // ========== file_context 管理结束 ==========

    addLog('INFO', locale === "zh" ? '开始发送消息' : 'Start sending message', {
      agentName,
      messageLength: userContent.length,
      requestUrl,
      hostname: window.location.hostname,
      port: window.location.port,
      userAgent: navigator.userAgent,
      conversationId: activeConversationId,
      fileIds: allFileIds
    });

    // 只用追加本轮之前的消息构建 history，避免本轮 user 同时出现在
    // history 和 message 中，也不把占位用的空 assistant 发给后端。
    const messagesBeforeTurn = messagesRef.current;

    // 用户消息：使用所有文件信息
    const userMsg: ChatMessage = {
      id: `msg-${Date.now()}`,
      role: 'user',
      content: userContent,
      attachments: allFileInfos.length > 0 ? allFileInfos : undefined,
    };

    const assistantMsgId = `msg-${Date.now() + 1}`;

    // 【修复-2603141800】创建新的消息数组
    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      thinking: '',
      toolCalls: [],
      isThinkingExpanded: false,
      loadedSkills: [],
      skillStates: [],
      ragRetrievals: []  // 【AC130-202603170949】初始化 RAG 检索记录
    };
    const newMessages = [...messagesBeforeTurn, userMsg, assistantMsg];

    // 【修复-2603141800】同时更新 state 和 ref
    messagesRef.current = newMessages;
    // 【修复-20260319】标记本地消息已更新，防止 initialMessages useEffect 覆盖
    localMessageUpdateRef.current = true;
    // 【修复-20260319】使用 flushSync 强制立即渲染消息，避免在 fetch 等待期间 UI 不更新
    flushSync(() => {
      setMessages(newMessages);
    });
    setIsRunning(true);
    streamingContentRef.current = "";
    streamingThinkingRef.current = "";
    streamingToolCallsRef.current = [];
    streamingLoadedSkillsRef.current = [];
    streamingSkillStatesRef.current = [];  // T017: 重置 Skill 执行状态
    streamingSubAgentCallsRef.current = [];  // 【AC130 新增】重置子 Agent 调用记录
    streamingRagRetrievalsRef.current = [];  // 【AC130-202603170949】重置 RAG 检索记录

    // 将高频 SSE content 事件合并到每帧最多一次 React 更新，避免每个
    // chunk 都复制整份消息数组并强制同步布局。
    let contentRenderTimer: ReturnType<typeof setTimeout> | null = null;
    const renderStreamingContent = () => {
      contentRenderTimer = null;
      const content = streamingContentRef.current;
      updateMessages((prev) =>
        prev.map((msg) => msg.id === assistantMsgId ? { ...msg, content } : msg)
      );
    };
    const scheduleStreamingContentRender = () => {
      if (contentRenderTimer === null) {
        contentRenderTimer = setTimeout(renderStreamingContent, 16);
      }
    };

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    const historyMessages = buildHistoryMessages(messagesBeforeTurn, shortTermMemory);

    // ========== Debug Logger: 生成 Trace ID 并记录请求 ==========
    const traceId = generateRequestId();
    // 每次请求都使用与 X-Request-ID 完全相同的 logger ID。
    const requestLogger = DebugLogger.create(traceId);
    debugLoggerRef.current = requestLogger;
    requestLogger.logRequestStart({
      agentName,
      messageLength: userContent.length,
      historyCount: historyMessages.length,
      historyCharacterCount: historyMessages.reduce((total, item) => total + item.content.length, 0),
      fileCount: allFileIds.length,
      conversationId: activeConversationId,
    });

    try {
      addLog('INFO', locale === "zh" ? '准备发起 fetch 请求' : 'Preparing fetch request', { url: requestUrl, historyCount: historyMessages.length, fileIds: allFileIds, traceId });

      const res = await fetch(requestUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Request-ID': traceId  // 添加 Trace ID header
        },
        body: JSON.stringify({
          message: userContent,
          history: historyMessages,
          file_ids: allFileIds,
          conversation_id: activeConversationId || null
        }),
        signal: abortController.signal,
      });

      addLog('INFO', locale === "zh" ? 'fetch 请求完成' : 'Fetch request completed', { status: res.status, ok: res.ok, contentType: res.headers.get('content-type') });

      if (!res.ok) {
        throw new Error(`HTTP error! status: ${res.status}`);
      }

      // 【流式输出核心】使用 ReadableStream API 读取 SSE 数据
      // 这是实现打字机效果的关键：逐块读取，而不是等待完整响应
      const reader = res.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) {
        throw new Error('No reader available');
      }

      addLog('INFO', locale === "zh" ? '开始读取流' : 'Start reading stream', { hasReader: !!reader });

      let buffer = '';
      let chunkCount = 0;
      let totalBytes = 0;

      // 【流式输出核心】循环读取数据块，直到流结束
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          addLog('INFO', locale === "zh" ? '流读取完成' : 'Stream reading completed', { chunkCount, totalBytes });
          break;
        }

        chunkCount++;
        totalBytes += value?.length || 0;

        // 【关键】使用 stream: true 选项，确保跨 chunk 的 UTF-8 字符能正确解码
        const chunk = decoder.decode(value, { stream: true });
        addChunkLog(chunkCount, totalBytes);

        // ========== Debug Logger: 记录 SSE Chunks ==========
        if (debugLoggerRef.current) {
          debugLoggerRef.current.logChunk(chunk);
        }

        buffer += chunk;
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';  // 保留最后一个不完整的行

        // 【流式输出核心】解析 SSE 数据
        // SSE 格式: data: {"type": "...", "content": "..."}\n
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));

              if (data.type === 'thinking') {
                // 思考过程
                streamingThinkingRef.current = data.content;
                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? { ...msg, thinking: data.content }
                      : msg
                  )
                );
              } else if (data.type === 'tool_call') {
                // 工具调用开始
                const newToolCall: ToolCall = {
                  name: data.name,
                  call_id: data.call_id,  // 保存唯一标识符
                  service: data.service,
                  args: data.args || {}
                };
                streamingToolCallsRef.current = [...streamingToolCallsRef.current, newToolCall];

                // T017: 检测 execute_skill 工具调用，更新 Skill 执行状态
                if (data.name === 'execute_skill' && data.args?.skill_name) {
                  const skillName = data.args.skill_name;
                  // 【T017修复】使用规范化名称进行匹配
                  const result = findOrCreateSkillState(
                    streamingSkillStatesRef.current,
                    skillName,
                    { status: 'executing', message: locale === "zh" ? '正在执行...' : 'Executing...' }
                  );
                  streamingSkillStatesRef.current = result.states;
                }

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? {
                          ...msg,
                          toolCalls: [...(msg.toolCalls || []), newToolCall],
                          // T017: 更新 Skill 执行状态
                          skillStates: (() => {
                            if (data.name === 'execute_skill' && data.args?.skill_name) {
                              const skillName = data.args.skill_name;
                              // 【T017修复】使用规范化名称进行匹配
                              const msgResult = findOrCreateSkillState(
                                msg.skillStates || [],
                                skillName,
                                { status: 'executing', message: locale === "zh" ? '正在执行...' : 'Executing...' }
                              );
                              return msgResult.states;
                            }
                            return msg.skillStates;
                          })()
                        }
                      : msg
                  )
                );
              } else if (data.type === 'tool_result') {
                // 工具调用结果 - 使用 call_id 匹配（如果有），否则回退到 name
                const toolCallId = data.call_id;
                const toolName = data.name;
                const toolResult = data.result;

                // T017: 检测 execute_skill 工具结果，更新 Skill 执行状态
                if (toolName === 'execute_skill') {
                  // 从工具调用参数中提取 skill_name，用于精确匹配
                  const relatedToolCall = streamingToolCallsRef.current.find(tc =>
                    tc.name === 'execute_skill' &&
                    (toolCallId ? tc.call_id === toolCallId : !tc.result)
                  );
                  const skillName = relatedToolCall?.args?.skill_name;

                  if (skillName) {
                    // 【T017修复】使用规范化名称进行匹配
                    const normalized = normalizeSkillName(skillName);
                    // 【AC130-202603142200 修复】精确判断失败：检查头部状态标志，避免误判文档内容
                    const isFailed = toolResult &&
                      (toolResult.includes('Status: failed') ||
                       toolResult.includes('Exit Code: 1') ||
                       toolResult.includes('Exit Code: 2') ||
                       toolResult.includes('execution timeout') ||
                       toolResult.includes('Execution Error') ||
                       toolResult.startsWith('Error:') ||
                       toolResult.includes('--- Error Output ---'));
                    streamingSkillStatesRef.current = streamingSkillStatesRef.current.map(s =>
                      s.skillName === skillName || normalizeSkillName(s.skillName) === normalized
                        ? { ...s, status: isFailed ? 'failed' : 'completed', message: isFailed ? (locale === "zh" ? '执行失败' : 'Failed') : (locale === "zh" ? '执行完成' : 'Completed') }
                        : s
                    );
                  }
                }

                // 【AC130-202603170949】检测 rag_retrieve 工具结果，更新 RAG 检索状态
                if (toolName === 'rag_retrieve') {
                  // 从工具调用参数中提取查询内容
                  const relatedToolCall = streamingToolCallsRef.current.find(tc =>
                    tc.name === 'rag_retrieve' &&
                    (toolCallId ? tc.call_id === toolCallId : !tc.result)
                  );
                  const query = relatedToolCall?.args?.query || '';

                  // 更新 RAG 检索记录状态
                  streamingRagRetrievalsRef.current = streamingRagRetrievalsRef.current.map(r => {
                    if (r.query === query || (toolCallId && r.call_id === toolCallId)) {
                      return {
                        ...r,
                        status: toolResult?.includes('未找到') || toolResult?.includes('不在知识库') ? 'failed' : 'completed',
                        results: toolResult
                      };
                    }
                    return r;
                  });
                }

                streamingToolCallsRef.current = streamingToolCallsRef.current.map(tc => {
                  // 优先使用 call_id 匹配，确保同名工具的多次调用能正确匹配
                  if (toolCallId && tc.call_id === toolCallId) {
                    return { ...tc, result: toolResult };
                  }
                  // 回退：如果没有 call_id，则使用 name 匹配第一个没有结果的
                  if (!toolCallId && tc.name === toolName && !tc.result) {
                    return { ...tc, result: toolResult };
                  }
                  return tc;
                });
                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? {
                          ...msg,
                          toolCalls: msg.toolCalls?.map(tc => {
                            // 同样的匹配逻辑
                            if (toolCallId && tc.call_id === toolCallId) {
                              return { ...tc, result: toolResult };
                            }
                            if (!toolCallId && tc.name === toolName && !tc.result) {
                              return { ...tc, result: toolResult };
                            }
                            return tc;
                          }),
                          // T017: 更新 Skill 执行状态
                          skillStates: toolName === 'execute_skill'
                            ? (() => {
                                // 从工具调用参数中提取 skill_name，用于精确匹配
                                const relatedToolCall = msg.toolCalls?.find(tc =>
                                  tc.name === 'execute_skill' &&
                                  (toolCallId ? tc.call_id === toolCallId : (!tc.result || tc.result === toolResult))
                                );
                                const skillName = relatedToolCall?.args?.skill_name;

                                if (skillName) {
                                  // 【T017修复】使用规范化名称进行匹配
                                  const normalized = normalizeSkillName(skillName);
                                  // 【AC130-202603142200 修复】精确判断失败：检查头部状态标志，避免误判文档内容
                                  const isFailed = toolResult &&
                                    (toolResult.includes('Status: failed') ||
                                     toolResult.includes('Exit Code: 1') ||
                                     toolResult.includes('Exit Code: 2') ||
                                     toolResult.includes('execution timeout') ||
                                     toolResult.includes('Execution Error') ||
                                     toolResult.startsWith('Error:') ||
                                     toolResult.includes('--- Error Output ---'));
                                  return msg.skillStates?.map(s =>
                                    s.skillName === skillName || normalizeSkillName(s.skillName) === normalized
                                      ? { ...s, status: isFailed ? 'failed' : 'completed', message: isFailed ? (locale === "zh" ? '执行失败' : 'Failed') : (locale === "zh" ? '执行完成' : 'Completed') }
                                      : s
                                  );
                                }
                                return msg.skillStates;
                              })()
                            : msg.skillStates,
                          // 【AC130-202603170949 修复】同步 RAG 检索状态到消息对象
                          // 修复问题：rag_retrieve 工具结果返回时，streamingRagRetrievalsRef.current 被更新但消息对象未同步
                          ragRetrievals: [...streamingRagRetrievalsRef.current]
                        }
                      : msg
                  )
                );
              } else if (data.type === 'content') {
                // ============================================================
                // 【流式输出核心 - 打字机效果】
                //
                // 这里的实现非常关键：
                // 1. 使用 useRef 累积内容
                // 2. 将高频 chunk 合并为每帧一次渲染，保留实时效果并限制 CPU
                // ============================================================
                streamingContentRef.current += data.content;
                scheduleStreamingContentRender();
              } else if (data.type === 'metrics') {
                // 更新上下文窗口状态栏
                setContextMetrics({
                  inputTokens: data.input_tokens ?? 0,
                  outputTokens: data.output_tokens ?? 0,
                  contextWindow: data.context_window ?? 0,
                });
                // 性能指标
                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? { ...msg, metrics: {
                          first_token_latency: data.first_token_latency,
                          total_tokens: data.total_tokens,
                          total_duration: data.total_duration
                        }}
                      : msg
                  )
                );
              } else if (data.type === 'skill_loading') {
                // 技能加载中 (T017)
                const skillName = data.skill_name;
                streamingThinkingRef.current = locale === "zh" ? `正在加载技能: ${skillName}...` : `Loading skill: ${skillName}...`;

                // 【T017修复】使用规范化名称进行匹配，确保不同格式的名称能指向同一个状态
                const result = findOrCreateSkillState(
                  streamingSkillStatesRef.current,
                  skillName,
                  { status: 'loading', message: locale === "zh" ? '正在加载...' : 'Loading...' }
                );
                streamingSkillStatesRef.current = result.states;

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? {
                          ...msg,
                          thinking: streamingThinkingRef.current,
                          loadingSkill: skillName,
                          skillStates: (() => {
                            const msgResult = findOrCreateSkillState(
                              msg.skillStates || [],
                              skillName,
                              { status: 'loading', message: locale === "zh" ? '正在加载...' : 'Loading...' }
                            );
                            return msgResult.states;
                          })()
                        }
                      : msg
                  )
                );
              } else if (data.type === 'skill_loaded') {
                // 技能加载完成 (T017)
                const skillName = data.skill_name;
                const success = data.success;

                // 【T017修复】使用规范化名称进行匹配，更新 Skill 执行状态
                const normalized = normalizeSkillName(skillName);
                streamingSkillStatesRef.current = streamingSkillStatesRef.current.map(s =>
                  s.skillName === skillName || normalizeSkillName(s.skillName) === normalized
                    ? { ...s, status: success ? 'completed' : 'failed', message: success ? (locale === "zh" ? '加载完成' : 'Loaded') : (locale === "zh" ? '加载失败' : 'Failed') }
                    : s
                );

                if (success && !streamingLoadedSkillsRef.current.includes(skillName)) {
                  streamingLoadedSkillsRef.current = [...streamingLoadedSkillsRef.current, skillName];
                }

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? {
                          ...msg,
                          loadedSkills: [...(msg.loadedSkills || []), ...(success ? [skillName] : [])],
                          loadingSkill: msg.loadingSkill === skillName ? undefined : msg.loadingSkill,
                          skillStates: msg.skillStates?.map(s =>
                            s.skillName === skillName || normalizeSkillName(s.skillName) === normalized
                              ? { ...s, status: success ? 'completed' : 'failed', message: success ? (locale === "zh" ? '加载完成' : 'Loaded') : (locale === "zh" ? '加载失败' : 'Failed') }
                              : s
                          )
                          // 【修复】不再覆盖 thinking 字段，保留原始思考内容
                          // 技能加载状态通过 skillStates 展示，不需要修改 thinking
                        }
                      : msg
                  )
                );
              } else if (data.type === 'sub_agent_call') {
                // 【AC130 新增】子 Agent 调用开始
                const agentName = data.agent_name;
                const message = data.message;
                const callId = `sub-${Date.now()}-${agentName}`;

                const newCall: SubAgentCallRecord = {
                  id: callId,
                  agentName,
                  message,
                  status: 'running',
                  startTime: Date.now(),
                };

                streamingSubAgentCallsRef.current = [...streamingSubAgentCallsRef.current, newCall];

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? { ...msg, subAgentCalls: [...streamingSubAgentCallsRef.current] }
                      : msg
                  )
                );
              } else if (data.type === 'sub_agent_result') {
                // 【AC130 新增】子 Agent 调用结果
                const agentName = data.agent_name;
                const result = data.result;
                const durationMs = data.duration_ms;
                const tokens = data.tokens;

                streamingSubAgentCallsRef.current = streamingSubAgentCallsRef.current.map(call => {
                  if (call.agentName === agentName && call.status === 'running') {
                    return {
                      ...call,
                      status: 'completed',
                      result: result,
                      durationMs,
                      tokens,
                      endTime: Date.now(),
                    };
                  }
                  return call;
                });

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? { ...msg, subAgentCalls: [...streamingSubAgentCallsRef.current] }
                      : msg
                  )
                );
              } else if (data.type === 'sub_agent_error') {
                // 【AC130 新增】子 Agent 调用错误
                const agentName = data.agent_name;
                const error = data.error;
                const errorType = data.error_type;

                streamingSubAgentCallsRef.current = streamingSubAgentCallsRef.current.map(call => {
                  if (call.agentName === agentName && call.status === 'running') {
                    return {
                      ...call,
                      status: errorType === 'timeout' ? 'timeout' : 'failed',
                      error,
                      errorType,
                      endTime: Date.now(),
                      durationMs: Date.now() - call.startTime,
                    };
                  }
                  return call;
                });

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? { ...msg, subAgentCalls: [...streamingSubAgentCallsRef.current] }
                      : msg
                  )
                );
              } else if (data.type === 'rag_retrieve') {
                // 【AC130-202603170949】RAG 知识库检索开始
                const query = data.query;
                const callId = data.call_id || `rag-${Date.now()}`;

                const newRetrieval: RAGRetrieval = {
                  query,
                  status: 'retrieving',
                  call_id: callId
                };

                streamingRagRetrievalsRef.current = [...streamingRagRetrievalsRef.current, newRetrieval];

                // 更新 thinking 显示检索提示
                streamingThinkingRef.current = locale === "zh"
                  ? `正在检索知识库: "${query}"...`
                  : `Retrieving from knowledge base: "${query}"...`;

                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? {
                          ...msg,
                          thinking: streamingThinkingRef.current,
                          ragRetrievals: [...streamingRagRetrievalsRef.current]
                        }
                      : msg
                  )
                );
              } else if (data.type === 'rag_sources') {
                // 【Phase3-Task3.3】RAG 来源引用 - 存储来源用于引用显示
                const sources = data.sources as RAGSource[];
                if (sources && sources.length > 0) {
                  streamingRagSourcesRef.current = [...streamingRagSourcesRef.current, ...sources];
                  updateMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantMsgId
                        ? { ...msg, ragSources: [...streamingRagSourcesRef.current] }
                        : msg
                    )
                  );
                }
              } else if (data.content) {
                // 兼容旧格式
                streamingContentRef.current += data.content;
                scheduleStreamingContentRender();
              }

              const errorPrefix = locale === "zh" ? '错误: ' : 'Error: ';
              if (data.error) {
                streamingContentRef.current = `${errorPrefix}${data.error}`;
                updateMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantMsgId
                      ? { ...msg, content: streamingContentRef.current }
                      : msg
                  )
                );
              }
            } catch {
              // 忽略解析错误
            }
          }
        }
        // 滚动到底部
        scrollToBottom();
      }

      addLog('INFO', locale === "zh" ? '流式响应完成' : 'Stream response completed', { contentLength: streamingContentRef.current.length, chunkCount, totalBytes });

    } catch (error) {
      setHasError(true);

      if (contentRenderTimer !== null) {
        clearTimeout(contentRenderTimer);
        contentRenderTimer = null;
      }

      const errorDetails = error instanceof Error ? {
        name: error.name,
        message: error.message,
        stack: error.stack,
        requestUrl,
        cause: error.cause,
      } : { error: String(error), requestUrl };

      addLog('ERROR', locale === "zh" ? 'fetch 请求失败' : 'Fetch request failed', errorDetails);

      const networkError = locale === "zh" ? '网络错误，请重试' : 'Network error, please retry';
      if (error instanceof Error && error.name === 'AbortError') {
        addLog('INFO', locale === "zh" ? '请求被取消' : 'Request cancelled');
        // 移除空的 assistant 消息
        streamingContentRef.current = '';
        updateMessages((prev) => prev.filter(msg => msg.id !== assistantMsgId));
      } else {
        streamingContentRef.current = networkError;
        updateMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMsgId
              ? { ...msg, content: networkError }
              : msg
          )
        );
      }
    } finally {
      if (contentRenderTimer !== null) {
        clearTimeout(contentRenderTimer);
        renderStreamingContent();
      }

      // 最终消息必须先同步写入 ref，再触发 React 提交和父组件回调。
      // 这样不会依赖 useEffect 的执行时机，也不会把 ref 中的旧占位内容
      // 误当成最终结果持久化。
      const { messages: finalMessages, turnMessages } = finalizeTurnMessages(
        messagesRef.current,
        userMsg.id,
        assistantMsgId,
        streamingContentRef.current,
      );

      messagesRef.current = finalMessages;
      localMessageUpdateRef.current = true;
      if (turnMessages.length > 0) {
        pendingConversationUpdateRef.current = {
          conversationId: activeConversationId,
          messages: finalMessages,
          turnMessages,
        };
      }

      isRunningRef.current = false;
      flushSync(() => {
        setMessages(finalMessages);
        setIsRunning(false);
      });
      abortControllerRef.current = null;
    }
  // 【修复-2603141800】从依赖项中移除 messages，使用 ref 获取最新值
  // 这避免了每次 messages 变化时重建 handleSend 函数
  }, [inputValue, agentName, locale, shortTermMemory, scrollToBottom, conversationId, onCreateConversation, pendingFiles, fileContext, uploadPendingFiles, updateMessages, findOrCreateSkillState, normalizeSkillName]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleStop = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  const toggleThinkingExpand = (msgId: string) => {
    updateMessages((prev) =>
      prev.map((msg) =>
        msg.id === msgId
          ? { ...msg, isThinkingExpanded: !msg.isThinkingExpanded }
          : msg
      )
    );
  };

  // ============ 文件上传相关函数 ============

  /**
   * 生成文件唯一 ID
   */
  const generateFileId = () => `file-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

  /**
   * 获取文件图标组件
   */
  const getFileIcon = (type: string) => {
    if (type === 'application/pdf') return <FileText className="w-3 h-3 text-red-400" />;
    if (type.includes('word') || type.includes('document')) return <FileText className="w-3 h-3 text-blue-400" />;
    if (type.includes('spreadsheet') || type.includes('excel')) return <FileSpreadsheet className="w-3 h-3 text-green-400" />;
    if (type.startsWith('image/')) return <ImageIcon className="w-3 h-3 text-purple-400" />;
    return <File className="w-3 h-3 text-gray-400" />;
  };

  /**
   * 处理文件选择
   */
  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    const validFiles: PendingFile[] = [];

    for (const file of files) {
      // 检查文件类型
      if (!DEFAULT_FILE_CONFIG.allowedTypes.includes(file.type)) {
        console.warn(`Unsupported file type: ${file.name}`);
        continue;
      }
      // 检查文件大小
      if (file.size > DEFAULT_FILE_CONFIG.maxFileSize) {
        console.warn(`File too large: ${file.name}`);
        continue;
      }
      validFiles.push({
        id: generateFileId(),
        file,
        name: file.name,
        size: file.size,
        type: file.type,
      });
    }

    if (validFiles.length > 0) {
      setPendingFiles(prev => [...prev, ...validFiles]);
    }
    // 重置 input
    e.target.value = '';
  }, []);

  /**
   * 移除待发送文件
   */
  const handleRemoveFile = useCallback((fileId: string) => {
    setPendingFiles(prev => prev.filter(f => f.id !== fileId));
  }, []);

  /**
   * 打开文件选择器
   */
  const handleUploadClick = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  // 渲染思考过程区域
  const renderThinkingSection = (msg: ChatMessage) => {
    const hasThinking = msg.thinking || (msg.toolCalls && msg.toolCalls.length > 0) || (msg.loadedSkills && msg.loadedSkills.length > 0) || (msg.skillStates && msg.skillStates.length > 0) || (msg.ragRetrievals && msg.ragRetrievals.length > 0);
    if (!hasThinking) return null;

    // 如果正在运行且有思考内容或工具调用，自动展开
    const isExpanded = msg.isThinkingExpanded || (isRunning && (msg.thinking || (msg.toolCalls && msg.toolCalls.length > 0)));

    /**
     * 获取 Skill 状态的颜色和图标 (T017)
     */
    const getSkillStatusStyle = (status: SkillExecutionState['status']) => {
      switch (status) {
        case 'loading':
          return { color: 'text-yellow-400', bgColor: 'bg-yellow-500/20', borderColor: 'border-yellow-500/50' };
        case 'executing':
          return { color: 'text-blue-400', bgColor: 'bg-blue-500/20', borderColor: 'border-blue-500/50' };
        case 'completed':
          return { color: 'text-green-400', bgColor: 'bg-green-500/20', borderColor: 'border-green-500/50' };
        case 'failed':
          return { color: 'text-red-400', bgColor: 'bg-red-500/20', borderColor: 'border-red-500/50' };
        default:
          return { color: 'text-gray-400', bgColor: 'bg-gray-500/20', borderColor: 'border-gray-500/50' };
      }
    };

    return (
      <div className="border-b border-white/10">
        {/* 标题栏 - 可点击展开/收起 */}
        <button
          type="button"
          onClick={() => toggleThinkingExpand(msg.id)}
          className="w-full px-4 py-2 flex items-center gap-2 bg-white/5 hover:bg-white/10 transition-colors text-left"
        >
          {isExpanded ? (
            <ChevronDown className="w-4 h-4 text-gray-400" />
          ) : (
            <ChevronRight className="w-4 h-4 text-gray-400" />
          )}
          <Lightbulb className="w-4 h-4 text-yellow-500" />
          <span className="text-xs text-gray-400 font-medium">
            {t("thinkingProcess")}
          </span>
          {/* 流式输出指示器 */}
          {isRunning && msg.thinking && !msg.content && (
            <span className="inline-block w-2 h-2 bg-yellow-500 rounded-full animate-pulse ml-1"></span>
          )}
          {/* T017: Skill 状态指示器 */}
          {msg.loadingSkill && (
            <span className="ml-2 px-2 py-0.5 bg-yellow-500/20 text-yellow-400 rounded text-[10px] animate-pulse">
              {locale === "zh" ? `加载: ${msg.loadingSkill}` : `Loading: ${msg.loadingSkill}`}
            </span>
          )}
          {!isExpanded && msg.toolCalls && msg.toolCalls.length > 0 && (
            <span className="text-xs text-gray-500 ml-auto">
              {msg.toolCalls.length} {t("toolCallsCount")}
            </span>
          )}
        </button>

        {/* 展开内容 */}
        {isExpanded && (
          <div className="px-4 py-2 bg-white/5 space-y-2">
            {/* T017: Skill 执行状态区域 */}
            {msg.skillStates && msg.skillStates.length > 0 && (
              <div className="border-l-2 border-cyan-500/50 pl-2 py-1">
                <div className="flex items-center gap-1 text-xs mb-1">
                  <span className="text-cyan-400 font-medium">
                    {locale === "zh" ? "技能执行状态" : "Skill Execution"}
                  </span>
                </div>
                <div className="space-y-1">
                  {msg.skillStates.map((skill, idx) => {
                    const style = getSkillStatusStyle(skill.status);
                    return (
                      <div key={idx} className={`flex items-center gap-2 px-2 py-1 rounded ${style.bgColor} border ${style.borderColor}`}>
                        {skill.status === 'loading' && <Loader2 className="w-3 h-3 text-yellow-400 animate-spin" />}
                        {skill.status === 'executing' && <Loader2 className="w-3 h-3 text-blue-400 animate-spin" />}
                        {skill.status === 'completed' && <span className="text-green-400">✓</span>}
                        {skill.status === 'failed' && <span className="text-red-400">✗</span>}
                        <span className={`text-xs font-mono ${style.color}`}>{skill.skillName}</span>
                        <span className="text-[10px] text-gray-500">{skill.message}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 思考内容 - 流式输出效果 */}
            {msg.thinking && (
              <div className="text-xs text-gray-400 italic whitespace-pre-wrap">
                {msg.thinking}
                {/* 流式输出时的光标效果 */}
                {isRunning && !msg.content && (
                  <span className="inline-block w-1.5 h-3 bg-yellow-500 ml-0.5 animate-pulse"></span>
                )}
              </div>
            )}

            {/* 已加载技能列表 */}
            {msg.loadedSkills && msg.loadedSkills.length > 0 && (
              <div className="border-l-2 border-purple-500/50 pl-2 py-1">
                <div className="flex items-center gap-1 text-xs mb-1">
                  <span className="text-purple-400 font-medium">
                    {locale === "zh" ? "已加载技能" : "Loaded Skills"}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1">
                  {msg.loadedSkills.map((skill, idx) => (
                    <span key={idx} className="px-2 py-0.5 bg-purple-500/20 text-purple-300 rounded text-xs">
                      {skill}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* 工具调用列表 - 默认只显示名称，展开后显示详情 */}
            {msg.toolCalls && msg.toolCalls.map((tc, idx) => (
              <div key={idx} className="border-l-2 border-blue-500/50 pl-2 py-1">
                <div className="flex items-center gap-1 text-xs">
                  <Wrench className="w-3 h-3 text-blue-400" />
                  <span className="text-blue-400 font-mono">{tc.name}</span>
                  {tc.service && (
                    <span className="text-gray-500 text-[10px] ml-1">({tc.service})</span>
                  )}
                  {tc.result && (
                    <span className="text-green-400 text-[10px] ml-1">✓</span>
                  )}
                  {/* 工具执行中指示器 */}
                  {!tc.result && isRunning && (
                    <Loader2 className="w-3 h-3 text-blue-400 animate-spin ml-1" />
                  )}
                </div>
                {/* 展开后才显示输入输出详情 */}
                {Object.keys(tc.args).length > 0 && (
                  <div className="text-xs text-gray-500 mt-1 font-mono bg-black/20 px-2 py-1 rounded">
                    {t("input")}: {JSON.stringify(tc.args)}
                  </div>
                )}
                {tc.result && (
                  <div className="text-xs text-green-400 mt-1 font-mono bg-black/20 px-2 py-1 rounded max-h-32 overflow-auto whitespace-pre-wrap">
                    {tc.result}
                  </div>
                )}
              </div>
            ))}

            {/* 【AC130 新增】子 Agent 调用状态区域 - 使用独立组件 */}
            {msg.subAgentCalls && msg.subAgentCalls.length > 0 && (
              <SubAgentCallCard calls={msg.subAgentCalls} locale={locale} />
            )}

            {/* 【AC130-202603170949】RAG 知识库检索状态区域 */}
            {msg.ragRetrievals && msg.ragRetrievals.length > 0 && (
              <div className="border-l-2 border-emerald-500/50 pl-2 py-1">
                <div className="flex items-center gap-1 text-xs mb-1">
                  <Database className="w-3 h-3 text-emerald-400" />
                  <span className="text-emerald-400 font-medium">
                    {locale === "zh" ? "知识库检索" : "Knowledge Base"}
                  </span>
                </div>
                {msg.ragRetrievals.map((rag, idx) => (
                  <div key={idx} className="space-y-1">
                    <div className="flex items-center gap-2 text-xs">
                      {rag.status === 'retrieving' && <Loader2 className="w-3 h-3 text-emerald-400 animate-spin" />}
                      {rag.status === 'completed' && <span className="text-emerald-400">✓</span>}
                      {rag.status === 'failed' && <span className="text-yellow-400">⚠</span>}
                      <span className="text-gray-300">“{rag.query}”</span>
                    </div>
                    {rag.results && rag.status === 'completed' && (
                      <div className="text-xs text-gray-400 mt-1 bg-black/20 px-2 py-1 rounded max-h-24 overflow-auto">
                        {locale === "zh" ? "来源: " : "Source: "}{rag.results}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="h-full flex flex-col bg-black/20">
      {/* 上下文窗口状态栏 */}
      <ContextStatusBar
        inputTokens={contextMetrics.inputTokens}
        outputTokens={contextMetrics.outputTokens}
        contextWindow={contextMetrics.contextWindow}
      />
      {/* 消息列表 */}
      <div ref={messagesContainerRef} className="flex-1 overflow-auto p-4 space-y-3">
        {messages.length === 0 ? (
          <div className="text-center py-12">
            <p className="text-gray-500 text-sm">{t("sendTestMessage")}</p>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex flex-col ${msg.role === 'user' ? 'items-end' : 'items-start'}`}
            >
              {/* 用户消息 */}
              {msg.role === 'user' && (
                <div className="max-w-[85%] bg-blue-500 text-white rounded-2xl rounded-br-md px-4 py-2.5 text-sm whitespace-pre-wrap">
                  {msg.content}
                  {/* 文件附件显示 */}
                  {msg.attachments && msg.attachments.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-white/20 space-y-1">
                      {msg.attachments.map((att) => (
                        <div key={att.id} className="flex items-center gap-2 text-xs bg-white/10 rounded px-2 py-1">
                          {getFileIcon(att.type)}
                          <span className="truncate max-w-[120px]">{att.name}</span>
                          <span className="text-white/60">{formatFileSize(att.size)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* 助手消息 - 思考过程和回答分开显示 */}
              {msg.role === 'assistant' && (
                <div className="max-w-[85%] bg-white/10 text-gray-300 rounded-2xl rounded-bl-md overflow-hidden">
                  {/* 思考过程区域 - 独立的区块 */}
                  {renderThinkingSection(msg)}

                  {/* 最终回答区域 - 独立的区块，支持 Markdown */}
                  <div className="px-4 py-2.5 text-sm prose prose-invert prose-sm max-w-none">
                    {msg.content ? (
                      <span className="inline">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {msg.content}
                        </ReactMarkdown>
                        {/* 打字机光标效果 - 流式输出时显示 */}
                        {isRunning && (
                          <span className="inline-block w-1.5 h-4 bg-emerald-400 ml-0.5 animate-pulse align-middle"></span>
                        )}
                      </span>
                    ) : (isRunning && !msg.thinking && (!msg.toolCalls || msg.toolCalls.length === 0) ? (
                      <span className="flex items-center gap-1">
                        <Loader2 className="w-3 h-3 animate-spin" />
                        {t("generating")}
                      </span>
                    ) : '')}
                  </div>
                  {/* 【Phase3-Task3.3】RAG 来源引用 */}
                  {msg.ragSources && msg.ragSources.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-gray-700 px-4 pb-2.5">
                      <div className="flex items-center gap-1 text-xs text-gray-500 mb-2">
                        <Database className="w-3 h-3" />
                        <span>{locale === "zh" ? "来源:" : "Sources:"}</span>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {msg.ragSources.map((source, idx) => (
                          <span
                            key={idx}
                            className="inline-flex items-center gap-1 px-2 py-1 bg-emerald-900/30 text-emerald-400 text-xs rounded"
                          >
                            <FileText className="w-3 h-3" />
                            {source.filename}
                            <span className="text-emerald-600">({source.score.toFixed(2)})</span>
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          ))
        )}
        {isRunning && messages[messages.length - 1]?.role !== 'assistant' && (
          <div className="flex justify-start">
            <div className="bg-white/10 px-4 py-2.5 rounded-2xl rounded-bl-md">
              <span className="text-gray-400 text-sm animate-pulse">{t("thinking")}</span>
            </div>
          </div>
        )}
      </div>

      {/* 输入框 */}
      <div className="p-4 border-t border-white/10">
        {/* 上传进度指示器 (T016) */}
        {isUploading && (
          <div className="mb-2 p-3 bg-blue-500/10 border border-blue-500/30 rounded-lg">
            <div className="flex items-center gap-2 mb-2">
              <Loader2 className="w-4 h-4 text-blue-400 animate-spin" />
              <span className="text-xs text-blue-400">
                {locale === "zh" ? "正在上传文件..." : "Uploading files..."}
              </span>
            </div>
            <div className="w-full bg-white/10 rounded-full h-1.5">
              <div
                className="bg-blue-500 h-1.5 rounded-full transition-all duration-300"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
            {uploadingFileName && (
              <div className="mt-1 text-[10px] text-gray-500">
                {uploadingFileName}
              </div>
            )}
          </div>
        )}

        {/* 已上传文件上下文显示 (T016) */}
        {fileContext.file_infos.length > 0 && !isUploading && (
          <div className="mb-2 p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-lg">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <Paperclip className="w-4 h-4 text-emerald-400" />
                <span className="text-xs text-emerald-400">
                  {locale === "zh" ? "已上传文件" : "Uploaded Files"} ({fileContext.file_infos.length})
                </span>
              </div>
              <button
                type="button"
                onClick={clearFileContext}
                disabled={isRunning}
                className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {locale === "zh" ? "清除全部" : "Clear All"}
              </button>
            </div>
            <div role="list" aria-label={locale === "zh" ? "已上传文件" : "Uploaded Files"} className="flex gap-2 overflow-x-auto pb-1">
              {fileContext.file_infos.map((file) => (
                <div
                  key={file.id}
                  role="listitem"
                  aria-label={`${file.name}, ${formatFileSize(file.size)}`}
                  className="file-card flex-shrink-0 w-16 h-20 bg-white/5 border border-emerald-500/20 rounded-lg p-1.5 relative flex flex-col items-center justify-center group hover:bg-white/10 transition-colors"
                >
                  {/* 删除按钮 - 悬停时显示 */}
                  <button
                    type="button"
                    onClick={() => removeUploadedFile(file.id)}
                    aria-label={locale === "zh" ? "移除文件" : "Remove file"}
                    className="delete-btn absolute -top-1 -right-1 w-4 h-4 bg-red-500/80 hover:bg-red-500 rounded-full flex items-center justify-center text-white opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <X className="w-2.5 h-2.5" />
                  </button>
                  {/* 文件图标 */}
                  {getFileIcon(file.type)}
                  {/* 文件名 */}
                  <span className="text-[8px] text-gray-300 truncate max-w-full mt-1 text-center">
                    {file.name.length > 8 ? `${file.name.slice(0, 7)}...` : file.name}
                  </span>
                  {/* 文件大小 */}
                  <span className="text-[7px] text-gray-500">
                    {formatFileSize(file.size)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 待发送文件预览 - 符合 UX 设计稿规范 */}
        {pendingFiles.length > 0 && !isUploading && (
          <div className="mb-2 p-3 bg-white/5 border border-white/10 rounded-lg">
            {/* 标题栏 */}
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <Paperclip className="w-4 h-4 text-blue-400" />
                <span className="text-xs text-gray-400">
                  {t("pendingFiles")} ({pendingFiles.length})
                </span>
              </div>
              {pendingFiles.length < DEFAULT_FILE_CONFIG.maxFiles && (
                <button
                  type="button"
                  onClick={handleUploadClick}
                  disabled={isRunning}
                  className="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {t("addMoreFiles")}
                </button>
              )}
            </div>
            {/* 文件列表 - 可水平滚动，符合 UX 设计稿尺寸规范 (64px x 80px) */}
            <div role="list" aria-label={t("pendingFiles")} className="flex gap-2 overflow-x-auto pb-1">
              {pendingFiles.map((file) => (
                <div
                  key={file.id}
                  role="listitem"
                  aria-label={`${file.name}, ${formatFileSize(file.size)}`}
                  className="file-card flex-shrink-0 w-16 h-20 bg-white/5 border border-white/10 rounded-lg p-1.5 relative flex flex-col items-center justify-center group hover:bg-white/10 transition-colors"
                >
                  {/* 删除按钮 - 悬停时显示 */}
                  <button
                    type="button"
                    onClick={() => handleRemoveFile(file.id)}
                    aria-label={t("removeFile")}
                    className="delete-btn absolute -top-1 -right-1 w-4 h-4 bg-red-500/80 hover:bg-red-500 rounded-full flex items-center justify-center text-white opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <X className="w-2.5 h-2.5" />
                  </button>
                  {/* 文件图标 - 符合 UX 设计稿颜色规范 */}
                  {getFileIcon(file.type)}
                  {/* 文件名 - 最多8字符 */}
                  <span className="text-[8px] text-gray-300 truncate max-w-full mt-1 text-center">
                    {file.name.length > 8 ? `${file.name.slice(0, 7)}...` : file.name}
                  </span>
                  {/* 文件大小 */}
                  <span className="text-[7px] text-gray-500">
                    {formatFileSize(file.size)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 隐藏的文件输入 - 将 MIME 类型转换为文件扩展名 */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.doc,.xlsx,.xls,.txt,.csv,.json,.png,.jpg,.jpeg"
          onChange={handleFileSelect}
          className="hidden"
          disabled={isRunning || isUploading}
          aria-label={t("uploadFile")}
        />

        <div className="flex gap-2">
          {/* 上传按钮 - 符合 UX 设计稿 3.2.1 */}
          <button
            type="button"
            onClick={handleUploadClick}
            disabled={isRunning || isUploading}
            aria-label={t("uploadFile")}
            aria-describedby="upload-hint"
            className={`
              p-2 rounded-lg transition-colors
              ${pendingFiles.length > 0 || fileContext.file_ids.length > 0 ? 'text-blue-400' : 'text-gray-400 hover:text-blue-400'}
              ${isRunning || isUploading ? 'opacity-50 cursor-not-allowed' : ''}
            `}
            title={t("uploadFile")}
          >
            <Paperclip className="w-5 h-5" />
          </button>

          <input
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t("sendMessagePlaceholder")}
            disabled={isRunning}
            className="flex-1 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <button
            type="button"
            onClick={isRunning ? handleStop : handleSend}
            aria-label={isRunning ? (locale === "zh" ? "停止生成" : "Stop generating") : t("send")}
            disabled={!isRunning && (isUploading || (!inputValue.trim() && pendingFiles.length === 0 && fileContext.file_ids.length === 0))}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              isRunning
                ? 'bg-red-500 hover:bg-red-600 text-white'
                : 'bg-blue-500 hover:bg-blue-600 text-white disabled:opacity-50 disabled:cursor-not-allowed'
            }`}
          >
            {isRunning ? <Square className="w-4 h-4" /> : t("send")}
          </button>
        </div>

        {/* 日志下载按钮 */}
        {localLogs.length > 0 && (
          <button
            type="button"
            onClick={downloadLogs}
            className="mt-3 w-full px-4 py-2 bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded-lg text-sm font-medium hover:bg-blue-500/30 transition-colors"
          >
            📥 {t("downloadDebugLog")} ({localLogs.length} {t("logsCount")})
          </button>
        )}

        {/* 性能指标显示 */}
        {(() => {
          const lastAssistantMsg = [...messages].reverse().find(m => m.role === 'assistant' && m.metrics);
          if (lastAssistantMsg?.metrics) {
            const m = lastAssistantMsg.metrics;
            return (
              <div className="mt-3 p-3 bg-white/5 border border-white/10 rounded-lg">
                <div className="text-xs text-gray-500 mb-2 font-medium">
                  {locale === "zh" ? "最近响应性能" : "Last Response Metrics"}
                </div>
                <div className="flex items-center gap-4 text-xs">
                  <div className="flex items-center gap-1.5">
                    <Zap size={12} className="text-yellow-400" />
                    <span className="text-gray-400">{locale === "zh" ? "首Token" : "First Token"}:</span>
                    <span className="text-yellow-400 font-mono">{m.first_token_latency}ms</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Hash size={12} className="text-blue-400" />
                    <span className="text-gray-400">{locale === "zh" ? "Token数" : "Tokens"}:</span>
                    <span className="text-blue-400 font-mono">{m.total_tokens}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Clock size={12} className="text-emerald-400" />
                    <span className="text-gray-400">{locale === "zh" ? "总耗时" : "Duration"}:</span>
                    <span className="text-emerald-400 font-mono">{m.total_duration}ms</span>
                  </div>
                </div>
              </div>
            );
          }
          return null;
        })()}

        {isRunning && (
          <div className="text-xs text-gray-500 mt-2 flex items-center gap-1">
            <span className="inline-block w-2 h-2 bg-green-500 rounded-full animate-pulse"></span>
            {t("generating")}
          </div>
        )}
      </div>
    </div>
  );
}

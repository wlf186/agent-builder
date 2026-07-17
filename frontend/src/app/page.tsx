"use client";

import { useState, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Plus,
  ArrowLeft,
  Trash2,
  Save,
  RefreshCw,
  Bot,
  Settings,
  Sparkles,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  X,
  MessageSquare,
  Globe,
  Loader2,
  Server,
  Wrench,
  Download,
  BookOpen,
  Upload,
  History,
  AlertTriangle,
  ExternalLink,
  Database,
  Activity,
  Plug,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent } from "@/components/ui/card";
import { Tooltip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { AgentChat, type ChatMessage } from "@/components/AgentChat";
import { MCPServiceDialog } from "@/components/MCPServiceDialog";
import { ModelServiceDialog, ModelService } from "@/components/ModelServiceDialog";
import { SkillDetailDialog } from "@/components/SkillDetailDialog";
import { SkillUploadDialog } from "@/components/SkillUploadDialog";
import { ConversationDrawer } from "@/components/ConversationDrawer";
import { EnvironmentBanner } from "@/components/EnvironmentBanner";
import { InitializationGuideCard } from "@/components/InitializationGuideCard";
import { EnvironmentReadyNotification } from "@/components/EnvironmentReadyNotification";
import { SubAgentSelector } from "@/components/SubAgentSelector";
import { KnowledgeBaseSelector } from "@/components/KnowledgeBaseSelector";
import { KnowledgeBaseDialog } from "@/components/KnowledgeBaseDialog";
import { KbDetailPanel } from "@/components/KbDetailPanel";
import { KnowledgeBaseSidebar } from "@/components/KnowledgeBaseSidebar";
import { useLocale } from "@/lib/LocaleContext";
import { CycleDependencyError } from "@/types";
import { useEnvironmentStatus } from "@/hooks/useEnvironmentStatus";
import { systemApi, RuntimeCheckResult, type EnvironmentError } from "@/lib/systemApi";
import { EnvironmentErrorDialog } from "@/components/EnvironmentErrorDialog";
import { kbApi, KnowledgeBase } from "@/lib/kbApi";
import { selectConversationPersistence } from "@/lib/chatMessages";
import { apiPath } from "@/lib/apiPath";
import { sanitizeForLogging } from "@/lib/debugLogger";

const API_BASE = "/api";
const MAX_CONSOLE_LOGS = 500;
const MAX_CONSOLE_MESSAGE_LENGTH = 4_000;
type CapturedConsoleLog = { type: string; timestamp: string; message: string };
const capturedConsoleLogs: CapturedConsoleLog[] = [];

// 后端消息格式转换为前端 ChatMessage 格式
interface BackendMessage {
  id: string;
  role: string;
  content: string;
  thinking?: string;
  tool_calls?: Array<{
    name: string;
    args: Record<string, unknown>;
    result?: string;
  }>;
  metrics?: {
    first_token_latency: number;
    total_tokens: number;
    total_duration: number;
  };
}

function convertBackendMessages(messages: BackendMessage[]): ChatMessage[] {
  return messages.map(msg => ({
    id: msg.id || `msg-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
    role: msg.role as 'user' | 'assistant',
    content: msg.content,
    thinking: msg.thinking || '',
    toolCalls: msg.tool_calls?.map(tc => ({
      name: tc.name,
      args: tc.args || {},
      result: tc.result
    })) || [],
    isThinkingExpanded: false,
    metrics: msg.metrics,
    loadedSkills: []
  }));
}


interface Agent {
  name: string;
  description: string;
  model_service?: string;
  model_info?: string;
  llm_provider?: string;
  llm_model?: string;
  created_at: string;
}

interface MCPService {
  name: string;
  description: string;
  connection_type: string;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
  is_builtin?: boolean;  // 是否为预置服务
}

interface MCPServiceDetail extends MCPService {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  auth_type?: string;
  headers?: Record<string, string>;
}

interface Skill {
  name: string;
  description: string;
  source: string;
  version?: string;
  author?: string;
  tags: string[];
  files: string[];
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

// 预置服务列表（与后端保持一致）
const BUILTIN_SERVICES = ["calculator", "cold-jokes"];

export default function Home() {
  const { locale, setLocale, t } = useLocale();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [currentView, setCurrentView] = useState<"list" | "create" | "config">("list");
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  // Create form
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [createError, setCreateError] = useState("");

  // Config form
  const [persona, setPersona] = useState("");
  const [modelService, setModelService] = useState<string>("");
  const [temperature, setTemperature] = useState(0.7);
  const [maxIterations, setMaxIterations] = useState(10);
  const [shortTermMemory, setShortTermMemory] = useState(5);
  const [planningMode, setPlanningMode] = useState("react");
  const [selectedMcpServices, setSelectedMcpServices] = useState<string[]>([]);

  // MCP services
  const [mcpServices, setMcpServices] = useState<MCPService[]>([]);
  const [mcpDialogOpen, setMcpDialogOpen] = useState(false);
  const [editingMcpService, setEditingMcpService] = useState<MCPServiceDetail | null>(null);
  const [mcpTestResults, setMcpTestResults] = useState<Record<string, {
    testing: boolean;
    success?: boolean;
    tools?: { name: string; description: string }[];
    error?: string;
  }>>({});

  // Model services
  const [modelServices, setModelServices] = useState<ModelService[]>([]);
  const [modelServiceDialogOpen, setModelServiceDialogOpen] = useState(false);
  const [editingModelService, setEditingModelService] = useState<ModelService | null>(null);

  // Skills
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillDialogOpen, setSkillDialogOpen] = useState(false);
  const [selectedSkillName, setSelectedSkillName] = useState<string | null>(null);
  const [skillUploadDialogOpen, setSkillUploadDialogOpen] = useState(false);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);

  // 【AC130 新增】Sub-Agents
  const [selectedSubAgents, setSelectedSubAgents] = useState<string[]>([]);
  const [cycleError, setCycleError] = useState<CycleDependencyError | null>(null);

  // 【AC130-202603161542】知识库 RAG
  const [selectedKnowledgeBases, setSelectedKnowledgeBases] = useState<string[]>([]);

  // 展开/收起状态
  const [sidebarMcpExpanded, setSidebarMcpExpanded] = useState(true);
  const [sidebarModelServicesExpanded, setSidebarModelServicesExpanded] = useState(true);
  const [sidebarSkillsExpanded, setSidebarSkillsExpanded] = useState(true);
  const [configPersonaExpanded, setConfigPersonaExpanded] = useState(true);
  const [configModelExpanded, setConfigModelExpanded] = useState(true);
  const [configAdvancedExpanded, setConfigAdvancedExpanded] = useState(false);
  const [configToolsExpanded, setConfigToolsExpanded] = useState(true);
  // 子区块展开状态（嵌套在工具配置面板内）
  const [mcpToolsExpanded, setMcpToolsExpanded] = useState(false);
  const [skillToolsExpanded, setSkillToolsExpanded] = useState(false);
  const [agentToolsExpanded, setAgentToolsExpanded] = useState(false);
  const [kbToolsExpanded, setKbToolsExpanded] = useState(false);

  // Knowledge Base states - AC130
  const [sidebarKbExpanded, setSidebarKbExpanded] = useState(true);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKb, setSelectedKb] = useState<KnowledgeBase | null>(null);
  const [kbDetailOpen, setKbDetailOpen] = useState(false);
  const [kbDialogOpen, setKbDialogOpen] = useState(false);
  const [editingKb, setEditingKb] = useState<KnowledgeBase | null>(null);
  const [sidebarResourcesExpanded, setSidebarResourcesExpanded] = useState(true);


  // 巻加历史会话相关状态
  const [conversationDrawerOpen, setConversationDrawerOpen] = useState(false);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [currentConversationMessages, setCurrentConversationMessages] = useState<ChatMessage[]>([]);

  // 计算已选中的工具总数
  const totalSelectedTools = selectedMcpServices.length + selectedSkills.length + selectedSubAgents.length + selectedKnowledgeBases.length;

  // 环境状态管理 - 异步环境初始化功能
  const { isReady: isEnvironmentReady, isCreating: isEnvironmentCreating } =
    useEnvironmentStatus({
      agentName: selectedAgent || "",
      enabled: !!selectedAgent && currentView === "config",
    });

  // 环境就绪通知状态
  const [showReadyNotification, setShowReadyNotification] = useState(false);
  const previousWasCreatingRef = useRef(false);

  // 项目本地 uv 运行时检测状态
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeCheckResult | null>(null);
  const [runtimeChecking, setRuntimeChecking] = useState(false);
  const [showRuntimeError, setShowRuntimeError] = useState(false);
  const [environmentError, setEnvironmentError] = useState<EnvironmentError | null>(null);

  // 检测项目本地 uv 运行时（仅在创建视图时执行）
  useEffect(() => {
    if (currentView === "create" && !runtimeStatus && !runtimeChecking) {
      setRuntimeChecking(true);
      systemApi
        .checkRuntime()
        .then((result) => {
          setRuntimeStatus(result);
        })
        .catch((err) => {
          console.error("本地运行时检测失败:", err);
          setRuntimeStatus({
            available: false,
            path: null,
            version: null,
            error: "CHECK_FAILED",
            message: "检测失败",
          });
        })
        .finally(() => {
          setRuntimeChecking(false);
        });
    }
  }, [currentView, runtimeStatus, runtimeChecking]);

  // 监听环境状态变化，在从creating变为ready时显示通知
  useEffect(() => {
    const previousWasCreating = previousWasCreatingRef.current;
    previousWasCreatingRef.current = isEnvironmentCreating;

    if (previousWasCreating && isEnvironmentReady && selectedAgent) {
      setShowReadyNotification(true);
      // 10秒后自动消失
      const timer = setTimeout(() => setShowReadyNotification(false), 10000);
      return () => clearTimeout(timer);
    }
  }, [isEnvironmentReady, isEnvironmentCreating, selectedAgent]);

  const showToast = (message: string, type: "success" | "error" = "success") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 2500);
  };

  const loadAgents = async () => {
    try {
      const res = await fetch(`${API_BASE}/agents`);
      const data = await res.json();
      setAgents(data.agents || []);
    } catch (e) {
      console.error("Failed to load agents:", e);
    }
  };

  const loadMcpServices = async () => {
    try {
      const res = await fetch(`${API_BASE}/mcp-services`);
      const data = await res.json();
      setMcpServices(data.services || []);
    } catch (e) {
      console.error("Failed to load MCP services:", e);
    }
  };

  const loadModelServices = async () => {
    try {
      const res = await fetch(`${API_BASE}/model-services`);
      const data = await res.json();
      setModelServices(data.services || []);
    } catch (e) {
      console.error("Failed to load model services:", e);
    }
  };

  const loadSkills = async () => {
    try {
      const res = await fetch(`${API_BASE}/skills`);
      const data = await res.json();
      setSkills(data.skills || []);
    } catch (e) {
      console.error("Failed to load skills:", e);
    }
  };

  // Load knowledge bases - AC130
  const loadKnowledgeBases = async () => {
    try {
      const data = await kbApi.listKnowledgeBases();
      setKnowledgeBases(data);
    } catch (e) {
      console.error("Failed to load knowledge bases:", e);
    }
  };

  const testMcpConnection = async (name: string) => {
    // 设置测试中状态
    setMcpTestResults(prev => ({
      ...prev,
      [name]: { testing: true }
    }));

    try {
      const res = await fetch(apiPath('mcp-services', name, 'test'), {
        method: "POST",
      });
      const data = await res.json();

      setMcpTestResults(prev => ({
        ...prev,
        [name]: {
          testing: false,
          success: data.success,
          tools: data.tools || [],
          error: data.error,
        }
      }));
    } catch (e) {
      setMcpTestResults(prev => ({
        ...prev,
        [name]: {
          testing: false,
          success: false,
          error: e instanceof Error ? e.message : "Network error",
        }
      }));
    }
  };

  const handleDeleteMcpService = async (name: string) => {
    // 检查是否为预置服务
    if (BUILTIN_SERVICES.includes(name)) {
      showToast(locale === "zh" ? "预置服务不能删除" : "Cannot delete builtin service", "error");
      return;
    }

    const confirmMsg = locale === "zh" ? `确定删除 MCP 服务 "${name}"？` : `Delete MCP service "${name}"?`;
    if (!confirm(confirmMsg)) return;

    try {
      const res = await fetch(apiPath('mcp-services', name), { method: "DELETE" });
      const data = await res.json();
      if (res.ok) {
        await loadMcpServices();
        showToast(locale === "zh" ? "MCP 服务已删除" : "MCP service deleted");
      } else {
        showToast(data.detail || (locale === "zh" ? "删除失败" : "Delete failed"), "error");
      }
    } catch (e) {
      console.error("Delete MCP service failed:", e);
      showToast(locale === "zh" ? "删除失败" : "Delete failed", "error");
    }
  };

  const handleDeleteModelService = async (name: string) => {
    const confirmMsg = locale === "zh" ? `确定删除模型服务 "${name}"？` : `Delete model service "${name}"?`;
    if (!confirm(confirmMsg)) return;

    try {
      const res = await fetch(apiPath('model-services', name), { method: "DELETE" });
      const data = await res.json();
      if (res.ok) {
        await loadModelServices();
        showToast(locale === "zh" ? "模型服务已删除" : "Model service deleted");
      } else {
        showToast(data.detail || (locale === "zh" ? "删除失败" : "Delete failed"), "error");
      }
    } catch (e) {
      console.error("Delete model service failed:", e);
      showToast(locale === "zh" ? "删除失败" : "Delete failed", "error");
    }
  };

  const handleDeleteSkill = async (name: string) => {
    const confirmMsg = locale === "zh" ? `确定删除技能 "${name}"？` : `Delete skill "${name}"?`;
    if (!confirm(confirmMsg)) return;

    try {
      const res = await fetch(apiPath('skills', name), { method: "DELETE" });
      const data = await res.json();
      if (res.ok) {
        await loadSkills();
        showToast(locale === "zh" ? "技能已删除" : "Skill deleted");
      } else {
        showToast(data.detail || (locale === "zh" ? "删除失败" : "Delete failed"), "error");
      }
    } catch (e) {
      console.error("Delete skill failed:", e);
      showToast(locale === "zh" ? "删除失败" : "Delete failed", "error");
    }
  };

  // 控制台日志拦截器
  const setupConsoleInterceptor = () => {
    const originalConsole = {
      log: console.log,
      warn: console.warn,
      error: console.error,
      info: console.info,
    };

    const addLog = (type: string, ...args: unknown[]) => {
      const message = args.map(arg => {
        const sanitized = sanitizeForLogging(arg);
        if (typeof sanitized === 'string') return sanitized;
        try {
          return JSON.stringify(sanitized);
        } catch {
          return '[Unserializable log value]';
        }
      }).join(' ').slice(0, MAX_CONSOLE_MESSAGE_LENGTH);

      capturedConsoleLogs.push({
        type,
        timestamp: new Date().toISOString(),
        message,
      });

      if (capturedConsoleLogs.length > MAX_CONSOLE_LOGS) {
        capturedConsoleLogs.splice(0, capturedConsoleLogs.length - MAX_CONSOLE_LOGS);
      }
    };

    console.log = (...args: unknown[]) => {
      originalConsole.log.apply(console, args);
      addLog('log', ...args);
    };

    console.warn = (...args: unknown[]) => {
      originalConsole.warn.apply(console, args);
      addLog('warn', ...args);
    };

    console.error = (...args: unknown[]) => {
      originalConsole.error.apply(console, args);
      addLog('error', ...args);
    };

    console.info = (...args: unknown[]) => {
      originalConsole.info.apply(console, args);
      addLog('info', ...args);
    };

    return () => {
      console.log = originalConsole.log;
      console.warn = originalConsole.warn;
      console.error = originalConsole.error;
      console.info = originalConsole.info;
    };
  };

  const downloadClientLogs = () => {
    try {
      // 收集客户端信息
      const clientInfo = {
        timestamp: new Date().toISOString(),
        userAgent: navigator.userAgent,
        platform: navigator.platform,
        language: navigator.language,
        cookiesEnabled: navigator.cookieEnabled,
        doNotTrack: navigator.doNotTrack,
        onLine: navigator.onLine,
        screenWidth: window.screen.width,
        screenHeight: window.screen.height,
        devicePixelRatio: window.devicePixelRatio,
        locale: locale,
        currentView: currentView,
        agentsCount: agents.length,
        mcpServicesCount: mcpServices.length,
        selectedAgent: selectedAgent,
        // 不导出 query/hash 或 Web Storage 内容，它们可能包含凭据。
        url: `${window.location.origin}${window.location.pathname}`,
        storageEntryCounts: {
          localStorage: localStorage.length,
          sessionStorage: sessionStorage.length,
        },
        performance: {
          memory: (performance as Performance & { memory?: { usedJSHeapSize?: number } }).memory?.usedJSHeapSize || 'N/A',
          timing: performance.timing?.navigationStart || 'N/A',
        },
      };

      // 收集控制台日志
      const consoleLogs = capturedConsoleLogs.slice();

      // 组装完整日志
      const fullLog = {
        clientInfo,
        consoleLogs,
        exportedAt: new Date().toISOString(),
      };

      // 下载到本地
      const blob = new Blob([JSON.stringify(fullLog, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `client_log_${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      showToast(locale === "zh" ? "日志已下载" : "Logs downloaded");
    } catch (e) {
      console.error("Download client logs failed:", e);
      showToast(locale === "zh" ? "下载日志失败" : "Failed to download logs", "error");
    }
  };

  useEffect(() => {
    loadAgents();
    loadMcpServices();
    loadModelServices();
    loadSkills();
    loadKnowledgeBases();

    // 初始化控制台日志拦截器
    const cleanup = setupConsoleInterceptor();
    console.info("Console log interceptor initialized");

    return cleanup;
  }, []);

  const openAgentConfig = async (name: string) => {
    try {
      // REQ-1.1: 切换智能体时重置会话状态，避免会话串台
      setCurrentConversationId(null);
      setCurrentConversationMessages([]);

      const res = await fetch(apiPath('agents', name));
      const config = await res.json();
      setSelectedAgent(name);
      setPersona(config.persona || "");
      setModelService(config.model_service || "");
      setTemperature(config.temperature ?? 0.7);
      setMaxIterations(config.max_iterations ?? 10);
      setShortTermMemory(config.short_term_memory ?? 5);
      setPlanningMode(config.planning_mode ?? "react");
      setSelectedMcpServices(config.mcp_services || []);
      setSelectedSkills(config.skills || []);
      setSelectedSubAgents(config.sub_agents || []);  // 【AC130 新增】加载子 Agent
      setSelectedKnowledgeBases(config.knowledge_bases || []);  // 【AC130-202603161542】加载知识库
      setCycleError(null);  // 【AC130 新增】重置循环依赖错误
      setCurrentView("config");
    } catch (e) {
      console.error("Failed to load config:", e);
    }
  };

  const handleCreate = async () => {
    if (!newName.trim()) {
      setCreateError(t("agentNameRequired"));
      return;
    }

    setIsLoading(true);
    try {
      const res = await fetch(`${API_BASE}/agents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName, description: newDesc }),
      });
      const data = await res.json();
      if (res.ok) {
        const createdAgentName = data.name || newName.trim();
        setNewName("");
        setNewDesc("");
        setCreateError("");
        await loadAgents();
        // 直接进入配置页面，异步环境初始化将在后台进行
        setSelectedAgent(createdAgentName);
        setPersona(newDesc || (locale === "zh" ? "你是一个有帮助的AI助手。" : "You are a helpful AI assistant."));
        setModelService("");
        setTemperature(0.7);
        setMaxIterations(10);
        setShortTermMemory(5);
        setPlanningMode("react");
        setSelectedMcpServices([]);
        setSelectedSkills([]);
        setSelectedSubAgents([]);  // 【AC130 新增】重置子 Agent
        setCycleError(null);  // 【AC130 新增】重置循环依赖错误
        setCurrentView("config");
        // P0: 显示创建成功Toast，包含智能体名称和环境初始化提示
        const message = locale === "zh"
          ? `智能体「${createdAgentName}」创建成功，正在初始化运行环境...`
          : `Agent '${createdAgentName}' created successfully, initializing runtime environment...`;
        showToast(message);
      } else {
        const detail = data.detail || "";
        // 检测是否为项目本地运行时错误
        if (detail.includes("uv") || data.error_code === "UV_NOT_FOUND") {
          // 设置结构化错误信息并显示弹窗
          setEnvironmentError({
            error_code: data.error_code || "UV_NOT_FOUND",
            error_type: "环境依赖缺失",
            user_message: detail || "系统无法创建项目本地运行环境",
            solutions: [
              {
                title: "重新运行项目引导脚本",
                steps: [
                  "返回项目根目录",
                  "运行 ./bootstrap.sh --rebuild",
                  "重新启动整套服务",
                ],
                estimated_time: "2-10 分钟",
                commands: [
                  "./stop.sh",
                  "./bootstrap.sh --rebuild",
                  "./start.sh --skip-bootstrap",
                ],
              },
              {
                title: "离线恢复",
                steps: [
                  "确认项目本地缓存完整",
                  "运行 ./bootstrap.sh --offline",
                ],
                estimated_time: "1-5 分钟",
              },
            ],
            technical_details: {
              error_message: detail,
            },
          });
          setShowRuntimeError(true);
        } else {
          setCreateError(detail || (locale === "zh" ? "创建失败" : "Creation failed"));
        }
      }
    } catch {
      setCreateError(locale === "zh" ? "网络错误" : "Network error");
    } finally {
      setIsLoading(false);
    }
  };

  const handleSave = async () => {
    if (!selectedAgent) return;

    setIsSaving(true);
    setCycleError(null);  // 【AC130 新增】清除旧的循环依赖错误
    try {
      const res = await fetch(apiPath('agents', selectedAgent), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          persona,
          model_service: modelService || null,
          temperature,
          max_iterations: maxIterations,
          short_term_memory: shortTermMemory,
          planning_mode: planningMode,
          mcp_services: selectedMcpServices,
          skills: selectedSkills,
          sub_agents: selectedSubAgents,  // 【AC130 新增】子 Agent 列表
          knowledge_bases: selectedKnowledgeBases,  // 【AC130-202603161542】知识库列表
        }),
      });
      if (res.ok) {
        showToast(locale === "zh" ? "配置已保存" : "Configuration saved");
        // 刷新智能体列表，确保卡片显示最新数据
        await loadAgents();
      } else {
        // 【AC130 新增】处理错误响应，检测循环依赖
        const data = await res.json();
        if (data.detail?.error === "circular_dependency" || data.error === "circular_dependency") {
          setCycleError({
            error: data.detail?.error || data.error,
            message: data.detail?.message || data.message || (locale === "zh" ? "检测到循环依赖" : "Circular dependency detected"),
            cycle_path: data.detail?.cycle_path || data.cycle_path || [],
          });
          showToast(locale === "zh" ? "保存失败：存在循环依赖" : "Save failed: Circular dependency detected", "error");
        } else {
          showToast(data.detail || (locale === "zh" ? "保存失败" : "Save failed"), "error");
        }
      }
    } catch {
      showToast(locale === "zh" ? "保存失败" : "Save failed", "error");
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedAgent) return;
    const confirmMsg = locale === "zh" ? `确定删除 "${selectedAgent}"？` : `Delete "${selectedAgent}"?`;
    if (!confirm(confirmMsg)) return;

    try {
      await fetch(apiPath('agents', selectedAgent), { method: "DELETE" });
      await loadAgents();
      setCurrentView("list");
      setSelectedAgent(null);
      showToast(locale === "zh" ? "智能体已删除" : "Agent deleted");
    } catch (e) {
      console.error("Delete failed:", e);
    }
  };

  // 语言切换按钮
  const LanguageSwitcher = () => (
    <button
      onClick={() => setLocale(locale === "zh" ? "en" : "zh")}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-gray-400 hover:text-white transition-colors text-sm"
    >
      <Globe size={14} />
      {locale === "zh" ? "EN" : "中文"}
    </button>
  );

  // === List View ===
  if (currentView === "list") {
    return (
      <div className="min-h-screen bg-[#0a0a0f] flex">
        {/* Toast */}
        <AnimatePresence>
          {toast && (
            <motion.div
              initial={{ opacity: 0, y: -20, x: "-50%" }}
              animate={{ opacity: 1, y: 0, x: "-50%" }}
              exit={{ opacity: 0, y: -20, x: "-50%" }}
              className={cn(
                "fixed top-6 left-1/2 z-50 px-5 py-3 rounded-xl shadow-2xl flex items-center gap-2 font-medium",
                toast.type === "success" ? "bg-emerald-500 text-white" : "bg-red-500 text-white"
              )}
            >
              {toast.message}
            </motion.div>
          )}
        </AnimatePresence>

        {/* Sidebar */}
        <aside className="w-64 glass-sidebar flex flex-col flex-shrink-0">
          <div className="p-5">
            <div className="flex items-center gap-3 mb-8">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shadow-lg shadow-blue-500/30">
                <Sparkles size={18} className="text-white" />
              </div>
              <span className="font-semibold text-white text-lg">{t("appTitle")}</span>
            </div>

            <Button
              onClick={() => setCurrentView("create")}
              className="w-full h-11 text-sm font-medium"
            >
              <Plus size={18} />
              {t("createAgent")}
            </Button>
          </div>

          <div className="px-5 py-4 border-t border-white/[0.05]">
            <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">{t("workspace")}</div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-gray-400">{t("myAgents")}</span>
              <span className="bg-white/10 text-gray-300 px-2.5 py-1 rounded-lg text-xs font-medium">
                {agents.length}
              </span>
            </div>
          </div>

          {/* MCP Services Section */}
          <div className="px-5 py-4 border-t border-white/[0.05]">
            <div className="flex items-center justify-between mb-3">
              <button
                onClick={() => setSidebarMcpExpanded(!sidebarMcpExpanded)}
                className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wider hover:text-gray-400 transition-colors"
              >
                {sidebarMcpExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                {locale === "zh" ? "MCP 服务" : "MCP Services"}
                <span className="text-gray-600 normal-case">({mcpServices.length})</span>
              </button>
              <button
                onClick={() => { setEditingMcpService(null); setMcpDialogOpen(true); }}
                className="w-6 h-6 rounded-lg bg-white/5 hover:bg-white/10 flex items-center justify-center transition-colors"
              >
                <Plus size={12} className="text-gray-400" />
              </button>
            </div>
            {sidebarMcpExpanded && (
              <div className="space-y-1.5">
                {mcpServices.length === 0 ? (
                  <div className="text-xs text-gray-600 py-2">
                    {locale === "zh" ? "暂无服务" : "No services"}
                  </div>
                ) : (
                  mcpServices.map((service) => {
                    const isBuiltin = BUILTIN_SERVICES.includes(service.name);
                    const testResult = mcpTestResults[service.name];
                    const tools = testResult?.tools || [];
                    return (
                      <div key={service.name} className="rounded-lg hover:bg-white/5 group">
                        <div
                          className="flex items-center justify-between py-2 px-3 cursor-pointer"
                          onClick={async () => {
                            try {
                              const res = await fetch(apiPath('mcp-services', service.name));
                              const data = await res.json();
                              setEditingMcpService(data as MCPServiceDetail);
                              setMcpDialogOpen(true);
                            } catch (e) {
                              console.error("Failed to load MCP service:", e);
                            }
                          }}
                        >
                          <div className="flex items-center gap-2">
                            <span
                              className={cn(
                                "w-2 h-2 rounded-full",
                                service.enabled ? "bg-emerald-500 shadow-lg shadow-emerald-500/50" : "bg-gray-500"
                              )}
                            />
                            <span className="text-sm text-gray-300">{service.name}</span>
                            {isBuiltin && (
                              <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400">
                                {locale === "zh" ? "预置" : "builtin"}
                              </span>
                            )}
                            {/* 测试状态指示 */}
                            {testResult && !testResult.testing && (
                              <span
                                className={cn(
                                  "w-1.5 h-1.5 rounded-full",
                                  testResult.success ? "bg-emerald-400" : "bg-red-400"
                                )}
                              />
                            )}
                          </div>
                          <div className="flex items-center gap-1">
                            {/* 测试按钮 */}
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                testMcpConnection(service.name);
                              }}
                              disabled={testResult?.testing}
                              className="opacity-0 group-hover:opacity-100 p-1 hover:bg-blue-500/20 rounded transition-all disabled:opacity-50"
                              title={locale === "zh" ? "测试连接" : "Test Connection"}
                            >
                              {testResult?.testing ? (
                                <Loader2 size={12} className="text-blue-400 animate-spin" />
                              ) : (
                                <Wrench size={12} className="text-blue-400" />
                              )}
                            </button>
                            {/* 删除按钮 */}
                            {!isBuiltin && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleDeleteMcpService(service.name);
                                }}
                                className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-500/20 rounded transition-all"
                              >
                                <Trash2 size={12} className="text-red-400" />
                              </button>
                            )}
                          </div>
                        </div>
                        {/* 测试结果和工具列表 */}
                        {testResult && !testResult.testing && (
                          <div className="px-3 pb-2 ml-4 border-l border-white/10">
                            {testResult.success ? (
                              <div className="text-xs">
                                <div className="flex items-center gap-1.5 text-emerald-400 mb-1">
                                  <span className="w-1 h-1 rounded-full bg-emerald-400" />
                                  {locale === "zh" ? "连接成功" : "Connected"}
                                  {tools.length > 0 && (
                                    <span className="text-gray-500">({tools.length} {locale === "zh" ? "个工具" : "tools"})</span>
                                  )}
                                </div>
                                {tools.length > 0 && (
                                  <div className="flex flex-wrap gap-1 mt-1">
                                    {tools.map((tool, idx) => (
                                      <span
                                        key={`sidebar-${service.name}-tool-${idx}`}
                                        title={tool.description || tool.name}
                                        className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 font-mono cursor-help"
                                      >
                                        {tool.name}
                                      </span>
                                    ))}
                                  </div>
                                )}
                              </div>
                            ) : (
                              <div className="text-xs text-red-400 flex items-center gap-1.5">
                                <span className="w-1 h-1 rounded-full bg-red-400" />
                                {locale === "zh" ? "连接失败" : "Failed"}
                                {testResult.error && (
                                  <span className="text-gray-500 text-[10px] truncate max-w-[150px]">: {testResult.error}</span>
                                )}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </div>

          {/* Model Services Section */}
          <div className="px-5 py-4 border-t border-white/[0.05]">
            <div className="flex items-center justify-between mb-3">
              <button
                onClick={() => setSidebarModelServicesExpanded(!sidebarModelServicesExpanded)}
                className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wider hover:text-gray-400 transition-colors"
              >
                {sidebarModelServicesExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                {t("modelServices")}
                <span className="text-gray-600 normal-case">({modelServices.length})</span>
              </button>
              <button
                onClick={() => { setEditingModelService(null); setModelServiceDialogOpen(true); }}
                className="w-6 h-6 rounded-lg bg-white/5 hover:bg-white/10 flex items-center justify-center transition-colors"
              >
                <Plus size={12} className="text-gray-400" />
              </button>
            </div>
            {sidebarModelServicesExpanded && (
              <div className="space-y-1.5">
                {modelServices.length === 0 ? (
                  <div className="text-xs text-gray-600 py-2">
                    {t("noModelServices")}
                  </div>
                ) : (
                  modelServices.map((service) => (
                    <div
                      key={service.name}
                      className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-white/5 group cursor-pointer"
                      onClick={async () => {
                        try {
                          const res = await fetch(apiPath('model-services', service.name));
                          const data = await res.json();
                          setEditingModelService(data as ModelService);
                          setModelServiceDialogOpen(true);
                        } catch (e) {
                          console.error("Failed to load model service:", e);
                        }
                      }}
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "w-2 h-2 rounded-full",
                            service.enabled ? "bg-emerald-500 shadow-lg shadow-emerald-500/50" : "bg-gray-500"
                          )}
                        />
                        <span className="text-sm text-gray-300">{service.name}</span>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">
                          {service.provider === "zhipu" ? "智谱" :
                           service.provider === "alibaba_bailian" ? "百炼" :
                           service.provider === "ollama" ? "Ollama" : service.provider}
                        </span>
                      </div>
                      <div className="flex items-center gap-1">
                        <span className="text-[10px] text-gray-500 mr-1">{service.selected_model}</span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDeleteModelService(service.name);
                          }}
                          className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-500/20 rounded transition-all"
                        >
                          <Trash2 size={12} className="text-red-400" />
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Knowledge Bases Section */}
          <KnowledgeBaseSidebar
            knowledgeBases={knowledgeBases}
            selectedKbId={selectedKb?.kb_id || null}
            onSelectKb={(kb) => {
              setSelectedKb(kb);
              setKbDetailOpen(true);
            }}
            onCreateKb={() => { setEditingKb(null); setKbDialogOpen(true); }}
            expanded={sidebarKbExpanded}
            onToggleExpand={() => setSidebarKbExpanded(!sidebarKbExpanded)}
          />

          {/* Skills Section */}
          <div className="px-5 py-4 border-t border-white/[0.05]">
            <div className="flex items-center justify-between mb-3">
              <button
                onClick={() => setSidebarSkillsExpanded(!sidebarSkillsExpanded)}
                className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wider hover:text-gray-400 transition-colors"
              >
                {sidebarSkillsExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                {locale === "zh" ? "技能货架" : "Skills"}
                <span className="text-gray-600 normal-case">({skills.length})</span>
              </button>
              <button
                onClick={() => setSkillUploadDialogOpen(true)}
                className="w-6 h-6 rounded-lg bg-white/5 hover:bg-white/10 flex items-center justify-center transition-colors"
              >
                <Upload size={12} className="text-gray-400" />
              </button>
            </div>
            {sidebarSkillsExpanded && (
              <div className="space-y-1.5">
                {skills.length === 0 ? (
                  <div className="text-xs text-gray-600 py-2">
                    {locale === "zh" ? "暂无技能" : "No skills"}
                  </div>
                ) : (
                  skills.map((skill) => {
                    const isBuiltin = skill.source === "builtin";
                    return (
                      <div
                        key={skill.name}
                        className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-white/5 group cursor-pointer"
                        onClick={() => {
                          setSelectedSkillName(skill.name);
                          setSkillDialogOpen(true);
                        }}
                      >
                        <div className="flex items-center gap-2">
                          <BookOpen size={12} className="text-purple-400" />
                          <span className="text-sm text-gray-300">{skill.name}</span>
                          {isBuiltin ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400">
                              {locale === "zh" ? "官方" : "Official"}
                            </span>
                          ) : (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">
                              {locale === "zh" ? "自定义" : "Custom"}
                            </span>
                          )}
                        </div>
                        {!isBuiltin && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteSkill(skill.name);
                            }}
                            className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-500/20 rounded transition-all"
                          >
                            <Trash2 size={12} className="text-red-400" />
                          </button>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </div>

          {/* Resources Section - External Links */}
          <div className="p-5 border-t border-white/[0.05]">
            <div className="flex items-center justify-between mb-3">
              <button
                onClick={() => setSidebarResourcesExpanded(!sidebarResourcesExpanded)}
                className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wider hover:text-gray-400 transition-colors"
              >
                {sidebarResourcesExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                {locale === "zh" ? "资源" : "Resources"}
              </button>
            </div>
            {sidebarResourcesExpanded && (
              <div className="space-y-1.5">
                <a
                  href="/docs"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-white/5 group cursor-pointer"
                >
                  <div className="flex items-center gap-2">
                    <BookOpen size={12} className="text-blue-400" />
                    <span className="text-sm text-gray-300">
                      {locale === "zh" ? "用户手册" : "User Manual"}
                    </span>
                  </div>
                  <ExternalLink size={12} className="text-gray-500 group-hover:text-gray-400" />
                </a>
                <a
                  href="/api/redirect/observability"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-white/5 group cursor-pointer"
                >
                  <div className="flex items-center gap-2">
                    <Activity size={12} className="text-orange-400" />
                    <span className="text-sm text-gray-300">
                      {locale === "zh" ? "本地追踪" : "Local Traces"}
                    </span>
                  </div>
                  <ExternalLink size={12} className="text-gray-500 group-hover:text-gray-400" />
                </a>
              </div>
            )}
          </div>

          <div className="mt-auto p-5 border-t border-white/[0.05]">
            <div className="flex items-center justify-between mb-3">
              <div className="text-xs text-gray-600">{t("poweredBy")}</div>
              <LanguageSwitcher />
            </div>
            <button
              onClick={downloadClientLogs}
              className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-white/5 hover:bg-white/10 text-gray-400 hover:text-gray-300 transition-colors text-xs"
            >
              <Download size={14} />
              {locale === "zh" ? "下载调试日志" : "Download Debug Logs"}
            </button>
          </div>
        </aside>

        {/* Main Content */}
        <main className="flex-1 p-8 overflow-auto">
          <div className="max-w-5xl mx-auto">
            {/* Header */}
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex justify-between items-center mb-8"
            >
              <div>
                <h1 className="text-2xl font-bold text-white mb-1">{t("agentWorkspace")}</h1>
                <p className="text-gray-500 text-sm">{t("agentWorkspaceDesc")}</p>
              </div>
              <Button variant="outline" onClick={loadAgents} className="gap-2">
                <RefreshCw size={16} />
                {t("refresh")}
              </Button>
            </motion.div>

            {/* Agent List */}
            {agents.length === 0 ? (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="text-center py-24"
              >
                <div className="w-20 h-20 rounded-2xl bg-white/5 flex items-center justify-center mx-auto mb-5">
                  <Bot size={36} className="text-gray-600" />
                </div>
                <h3 className="text-lg font-medium text-gray-400 mb-2">{t("noAgents")}</h3>
                <p className="text-gray-600 text-sm">{t("noAgentsDesc")}</p>
              </motion.div>
            ) : (
              <div className="space-y-3">
                {agents.map((agent, index) => (
                  <motion.div
                    key={agent.name}
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: index * 0.05, duration: 0.3 }}
                  >
                    <Card
                      hoverable
                      onClick={() => openAgentConfig(agent.name)}
                      className="p-5 flex items-center gap-5"
                    >
                      <div className="w-14 h-14 rounded-xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shadow-lg shadow-blue-500/20">
                        <Bot size={26} className="text-white" />
                      </div>

                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <h3 className="font-semibold text-white">{agent.name}</h3>
                          <span className="w-2 h-2 bg-emerald-500 rounded-full shadow-lg shadow-emerald-500/50" />
                        </div>
                        <p className="text-sm text-gray-500 line-clamp-1">{agent.description}</p>
                        <div className="flex items-center gap-3 mt-2">
                          <span className="text-xs bg-blue-500/20 text-blue-400 px-2.5 py-1 rounded-lg font-medium">
                            {agent.model_info || (locale === "zh" ? "未配置模型" : "No model")}
                          </span>
                          <span className="text-xs text-gray-600">{agent.created_at}</span>
                        </div>
                      </div>

                      <ChevronRight size={20} className="text-gray-600" />
                    </Card>
                  </motion.div>
                ))}
              </div>
            )}
          </div>
        </main>

        {/* MCP Service Dialog */}
        <MCPServiceDialog
          key={`${mcpDialogOpen}:${editingMcpService?.name ?? "new"}`}
          isOpen={mcpDialogOpen}
          onClose={() => { setMcpDialogOpen(false); setEditingMcpService(null); }}
          onSave={loadMcpServices}
          service={editingMcpService}
        />

        {/* Model Service Dialog */}
        <ModelServiceDialog
          key={`${modelServiceDialogOpen}:${editingModelService?.name ?? "new"}`}
          open={modelServiceDialogOpen}
          onClose={() => { setModelServiceDialogOpen(false); setEditingModelService(null); }}
          onSave={loadModelServices}
          service={editingModelService}
        />

        {/* Skill Detail Dialog */}
        <SkillDetailDialog
          key={`${skillDialogOpen}:${selectedSkillName ?? "none"}`}
          isOpen={skillDialogOpen}
          onClose={() => { setSkillDialogOpen(false); setSelectedSkillName(null); }}
          skillName={selectedSkillName}
        />

        {/* Skill Upload Dialog */}
        <SkillUploadDialog
          isOpen={skillUploadDialogOpen}
          onClose={() => setSkillUploadDialogOpen(false)}
          onUploadSuccess={loadSkills}
        />

        {/* Knowledge Base Dialog - AC130 */}
        {kbDialogOpen && (
          <KnowledgeBaseDialog
            knowledgeBase={editingKb}
            onClose={() => {
              setKbDialogOpen(false);
              setEditingKb(null);
            }}
            onSave={() => {
              loadKnowledgeBases();
              setKbDialogOpen(false);
              setEditingKb(null);
            }}
          />
        )}

        {/* Knowledge Base Detail Panel - AC130 */}
        {kbDetailOpen && selectedKb && (
          <KbDetailPanel
            knowledgeBase={selectedKb}
            onClose={() => {
              setKbDetailOpen(false);
              setSelectedKb(null);
            }}
            onUpdate={() => {
              loadKnowledgeBases();
            }}
          />
        )}
      </div>
    );
  }

  // === Create View ===
  if (currentView === "create") {
    return (
      <div className="min-h-screen bg-[#0a0a0f] flex items-center justify-center p-4">
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.2 }}
          className="w-full max-w-md"
        >
          <Card className="overflow-hidden">
            {/* Header */}
            <div className="bg-gradient-to-r from-blue-500 to-purple-600 px-6 py-5">
              <div className="flex justify-between items-center">
                <div>
                  <h2 className="text-lg font-semibold text-white">{t("createNewAgent")}</h2>
                  <p className="text-sm text-white/70 mt-0.5">{t("createNewAgentDesc")}</p>
                </div>
                <button
                  onClick={() => setCurrentView("list")}
                  className="text-white/70 hover:text-white transition-colors p-1"
                >
                  <X size={20} />
                </button>
              </div>
            </div>

            {/* Form */}
            <div className="p-6 space-y-5">
              {/* 项目本地运行时检测警告 */}
              {!runtimeChecking && runtimeStatus && !runtimeStatus.available && (
                <div className="bg-orange-50 dark:bg-orange-950/20 border border-orange-200 dark:border-orange-800 rounded-lg p-4">
                  <div className="flex items-start gap-3">
                    <AlertTriangle className="h-5 w-5 text-orange-500 shrink-0 mt-0.5" />
                    <div className="flex-1 min-w-0">
                      <h4 className="font-medium text-orange-900 dark:text-orange-100 text-sm">
                        项目本地运行时未就绪
                      </h4>
                      <p className="text-sm text-orange-800 dark:text-orange-200 mt-1">
                        未检测到项目内 uv；请先修复引导环境再创建智能体。
                      </p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <button
                          onClick={() => {
                            setEnvironmentError({
                              error_code: "UV_NOT_FOUND",
                              error_type: "环境依赖缺失",
                              user_message: runtimeStatus.message || "项目本地 uv 未就绪",
                              solutions: [
                                {
                                  title: "重新运行项目引导脚本",
                                  steps: [
                                    "返回项目根目录",
                                    "重建项目本地依赖",
                                    "重新启动整套服务",
                                  ],
                                  estimated_time: "2-10 分钟",
                                  commands: [
                                    "./stop.sh",
                                    "./bootstrap.sh --rebuild",
                                    "./start.sh --skip-bootstrap",
                                  ],
                                },
                                {
                                  title: "了解更多",
                                  steps: [
                                    "查看官方文档了解详细安装步骤",
                                  ],
                                  estimated_time: "5 分钟",
                                },
                              ],
                            });
                            setShowRuntimeError(true);
                          }}
                          className="text-sm text-orange-700 dark:text-orange-300 hover:text-orange-900 dark:hover:text-orange-100 font-medium flex items-center gap-1"
                        >
                          查看解决方案
                          <ChevronRight size={14} />
                        </button>
                        <span className="text-xs text-orange-600 dark:text-orange-400 self-center">
                          修复后刷新页面
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {createError && (
                <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-xl text-sm">
                  {createError}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-300 mb-2">{t("agentName")}</label>
                <Input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder={t("agentNamePlaceholder")}
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-300 mb-2">{t("description")}</label>
                <Textarea
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder={t("descriptionPlaceholder")}
                  rows={3}
                />
              </div>

              <div className="flex gap-3 pt-2">
                <Button variant="outline" onClick={() => setCurrentView("list")} className="flex-1">
                  {t("cancel")}
                </Button>
                <Button onClick={handleCreate} disabled={isLoading} className="flex-1">
                  {isLoading ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />}
                  {t("create")}
                </Button>
              </div>
            </div>
          </Card>
        </motion.div>

        {/* 环境错误弹窗 */}
        <EnvironmentErrorDialog
          isOpen={showRuntimeError}
          onClose={() => setShowRuntimeError(false)}
          error={environmentError}
        />
      </div>
    );
  }

  // === Config View ===
  return (
    <div className="min-h-screen bg-[#0a0a0f]">
      {/* Toast */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: -20, x: "-50%" }}
            animate={{ opacity: 1, y: 0, x: "-50%" }}
            exit={{ opacity: 0, y: -20, x: "-50%" }}
            className={cn(
              "fixed top-6 left-1/2 z-50 px-5 py-3 rounded-xl shadow-2xl flex items-center gap-2 font-medium",
              toast.type === "success" ? "bg-emerald-500 text-white" : "bg-red-500 text-white"
            )}
          >
            {toast.message}
          </motion.div>
        )}
      </AnimatePresence>

      {/* 环境状态横幅 */}
      {selectedAgent && (
        <EnvironmentBanner agentName={selectedAgent} locale={locale as 'zh' | 'en'} />
      )}

      {/* 环境就绪通知 */}
      <AnimatePresence>
        {showReadyNotification && selectedAgent && (
          <EnvironmentReadyNotification
            agentName={selectedAgent}
            locale={locale as 'zh' | 'en'}
            onDismiss={() => setShowReadyNotification(false)}
          />
        )}
      </AnimatePresence>

      {/* Header */}
      <motion.header
        initial={{ y: -20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        className="glass-sidebar sticky top-0 z-10"
      >
        <div className="px-6 py-4 flex items-center justify-between max-w-7xl mx-auto">
          <div className="flex items-center gap-4">
            <Button variant="ghost" onClick={() => { setCurrentView("list"); loadAgents(); }} className="gap-2">
              <ArrowLeft size={18} />
              {t("back")}
            </Button>
            <div className="w-px h-6 bg-white/10" />
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shadow-lg shadow-blue-500/20">
                <Bot size={18} className="text-white" />
              </div>
              <div>
                <h1 className="font-semibold text-white">{selectedAgent}</h1>
                <p className="text-xs text-gray-500">{t("agentConfig")}</p>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <LanguageSwitcher />
            <Button variant="ghost" onClick={handleDelete} className="text-red-400 hover:text-red-300 hover:bg-red-500/10 gap-2">
              <Trash2 size={16} />
              {t("delete")}
            </Button>
            <Button onClick={handleSave} disabled={isSaving} className="gap-2">
              {isSaving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              {t("save")}
            </Button>
          </div>
        </div>
      </motion.header>

      {/* Main Content - 为环境横幅留出空间 */}
      <div className={cn(
        "flex p-6 gap-6 max-w-7xl mx-auto transition-all duration-300",
        // 当环境正在初始化时，顶部留出更多空间（横幅高度约48px）
        isEnvironmentCreating && "pt-20"
      )}>
        {/* 初始化引导卡片 - 环境创建时显示 */}
        {isEnvironmentCreating && selectedAgent && (
          <div className="w-full">
            <InitializationGuideCard
              agentName={selectedAgent}
              locale={locale as 'zh' | 'en'}
            />
          </div>
        )}

        {/* Left Panel - Config */}
        <motion.div
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          className="flex-1 space-y-5"
        >
          {/* Persona */}
          <Card className="overflow-hidden">
            <div
              className="px-5 py-4 border-b border-white/[0.05] flex items-center gap-3 bg-white/[0.02] cursor-pointer"
              onClick={() => setConfigPersonaExpanded(!configPersonaExpanded)}
            >
              <MessageSquare size={16} className="text-blue-400" />
              <span className="font-medium text-sm text-gray-300 flex-1">{t("personaAndPrompts")}</span>
              {configPersonaExpanded ? <ChevronUp size={16} className="text-gray-500" /> : <ChevronDown size={16} className="text-gray-500" />}
            </div>
            {configPersonaExpanded && (
              <CardContent className="p-5">
                <Textarea
                  value={persona}
                  onChange={(e) => setPersona(e.target.value)}
                  placeholder={t("personaPlaceholder")}
                  rows={5}
                  className="bg-transparent border-white/[0.05]"
                />
              </CardContent>
            )}
          </Card>

          {/* Model Config */}
          <Card className="overflow-hidden">
            <div
              className="px-5 py-4 border-b border-white/[0.05] flex items-center gap-3 bg-white/[0.02] cursor-pointer"
              onClick={() => setConfigModelExpanded(!configModelExpanded)}
            >
              <Sparkles size={16} className="text-purple-400" />
              <span className="font-medium text-sm text-gray-300 flex-1">{t("modelConfig")}</span>
              {configModelExpanded ? <ChevronUp size={16} className="text-gray-500" /> : <ChevronDown size={16} className="text-gray-500" />}
            </div>
            {configModelExpanded && (
              <CardContent className="p-5 space-y-4">
                {modelServices.length === 0 ? (
                  <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl px-4 py-3">
                    <div className="flex items-center justify-between">
                      <span className="text-sm text-amber-400">{t("noModelServices")}</span>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setEditingModelService(null);
                          setModelServiceDialogOpen(true);
                        }}
                      >
                        <Plus size={14} className="mr-1" />
                        {t("createModelServiceFirst")}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div>
                    <label className="block text-xs text-gray-500 mb-2">{t("modelServiceConfig")}</label>
                    <select
                      value={modelService}
                      onChange={(e) => setModelService(e.target.value)}
                      className="w-full h-10 rounded-xl border border-white/10 bg-white/5 px-4 text-sm text-white focus:outline-none focus:border-blue-500/50"
                    >
                      <option value="">{t("pleaseSelectModel")}</option>
                      {modelServices.map((service) => (
                        <option key={service.name} value={service.name}>
                          {service.name} ({service.provider === "zhipu" ? "智谱AI" :
                            service.provider === "alibaba_bailian" ? "阿里云百炼" :
                            service.provider === "ollama" ? "Ollama" : service.provider} - {service.selected_model})
                        </option>
                      ))}
                    </select>
                    {modelService && (
                      <div className="mt-3 bg-white/5 rounded-lg px-3 py-2">
                        <div className="text-xs text-gray-500 mb-1">{t("modelInfo")}</div>
                        {(() => {
                          const service = modelServices.find(s => s.name === modelService);
                          if (!service) return null;
                          return (
                            <div className="flex items-center gap-2">
                              <span className={cn(
                                "w-2 h-2 rounded-full",
                                service.enabled ? "bg-emerald-500" : "bg-gray-500"
                              )} />
                              <span className="text-sm text-gray-300">{service.selected_model}</span>
                              <span className="text-xs text-gray-500">
                                ({service.provider === "zhipu" ? "智谱AI" :
                                  service.provider === "alibaba_bailian" ? "阿里云百炼" :
                                  service.provider === "ollama" ? "Ollama" : service.provider})
                              </span>
                            </div>
                          );
                        })()}
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            )}
          </Card>

          {/* Advanced Settings */}
          <Card>
            <div
              className="px-5 py-4 border-b border-white/[0.05] flex items-center gap-3 bg-white/[0.02] cursor-pointer"
              onClick={() => setConfigAdvancedExpanded(!configAdvancedExpanded)}
            >
              <Settings size={16} className="text-amber-400" />
              <span className="font-medium text-sm text-gray-300 flex-1">{t("advancedSettings")}</span>
              {configAdvancedExpanded ? <ChevronUp size={16} className="text-gray-500" /> : <ChevronDown size={16} className="text-gray-500" />}
            </div>
            {configAdvancedExpanded && (
              <CardContent className="p-5">
              <div className="grid grid-cols-2 gap-8">
                {/* 温度参数 */}
                <div>
                  <div className="flex justify-between items-center mb-3">
                    <div className="flex items-center gap-1.5">
                      <label className="text-xs text-gray-500">{t("temperature")}</label>
                      <Tooltip content={t("temperatureTooltip")} />
                    </div>
                    <span className="text-xs bg-blue-500/20 text-blue-400 px-2.5 py-1 rounded-lg font-medium">
                      {temperature}
                    </span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.1"
                    value={temperature}
                    onChange={(e) => setTemperature(parseFloat(e.target.value))}
                    className="w-full accent-blue-500"
                  />
                </div>

                {/* 最大迭代次数 */}
                <div>
                  <div className="flex justify-between items-center mb-3">
                    <div className="flex items-center gap-1.5">
                      <label className="text-xs text-gray-500">{t("maxIterations")}</label>
                      <Tooltip content={t("maxIterationsTooltip")} />
                    </div>
                    <span className="text-xs bg-blue-500/20 text-blue-400 px-2.5 py-1 rounded-lg font-medium">
                      {maxIterations}
                    </span>
                  </div>
                  <input
                    type="range"
                    min="1"
                    max="20"
                    value={maxIterations}
                    onChange={(e) => setMaxIterations(parseInt(e.target.value))}
                    className="w-full accent-blue-500"
                  />
                </div>

                {/* 短期记忆轮次 */}
                <div>
                  <div className="flex justify-between items-center mb-3">
                    <div className="flex items-center gap-1.5">
                      <label className="text-xs text-gray-500">{t("shortTermMemory")}</label>
                      <Tooltip content={t("shortTermMemoryTooltip")} />
                    </div>
                    <span className="text-xs bg-purple-500/20 text-purple-400 px-2.5 py-1 rounded-lg font-medium">
                      {shortTermMemory}
                    </span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="50"
                    value={shortTermMemory}
                    onChange={(e) => setShortTermMemory(parseInt(e.target.value))}
                    className="w-full accent-purple-500"
                  />
                </div>

                {/* 规划模式 - 下拉框 */}
                <div>
                  <label className="text-xs text-gray-500 block mb-3">{t("planningMode")}</label>
                  <div className="relative">
                    <select
                      value={planningMode}
                      onChange={(e) => setPlanningMode(e.target.value)}
                      className="w-full bg-white/[0.05] border border-white/[0.1] rounded-lg px-4 py-2.5 text-sm text-gray-200 appearance-none cursor-pointer focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50"
                    >
                      <option value="react">{t("planningModeReact")}</option>
                      <option value="reflexion">{t("planningModeReflexion")}</option>
                      <option value="plan_and_solve">{t("planningModePlanAndSolve")}</option>
                      <option value="rewOO">{t("planningModeReWOO")}</option>
                      <option value="tot">{t("planningModeToT")}</option>
                    </select>
                    <ChevronDown size={16} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                  </div>
                  {/* 模式说明 */}
                  <p className="text-xs text-gray-500 mt-2">
                    {planningMode === "react" && t("planningModeReactDesc")}
                    {planningMode === "reflexion" && t("planningModeReflexionDesc")}
                    {planningMode === "plan_and_solve" && t("planningModePlanAndSolveDesc")}
                    {planningMode === "rewOO" && t("planningModeReWOODesc")}
                    {planningMode === "tot" && t("planningModeToTDesc")}
                  </p>
                </div>
              </div>
            </CardContent>
            )}
          </Card>

          {/* Tool Config - 统一的工具配置面板 */}
          <Card className="overflow-hidden">
            <div
              className="px-5 py-4 border-b border-white/[0.05] flex items-center gap-3 bg-white/[0.02] cursor-pointer"
              onClick={() => setConfigToolsExpanded(!configToolsExpanded)}
            >
              <Settings size={16} className="text-emerald-400" />
              <span className="font-medium text-sm text-gray-300 flex-1">
                {t("toolConfig")}
              </span>
              {totalSelectedTools > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400">
                  {totalSelectedTools}
                </span>
              )}
              {configToolsExpanded ? <ChevronUp size={16} className="text-gray-500" /> : <ChevronDown size={16} className="text-gray-500" />}
            </div>
            {configToolsExpanded && (
              <CardContent className="p-4 space-y-3">
                {/* MCP工具子区块 */}
                <div className="border border-white/[0.05] rounded-xl overflow-hidden">
                  <div
                    className="px-4 py-3 flex items-center gap-3 bg-white/[0.01] cursor-pointer hover:bg-white/[0.03] transition-colors"
                    onClick={() => setMcpToolsExpanded(!mcpToolsExpanded)}
                  >
                    <Plug size={14} className="text-emerald-400" />
                    <span className="font-medium text-sm text-gray-300 flex-1">
                      {t("mcpTools")}
                    </span>
                    {selectedMcpServices.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400">
                        {selectedMcpServices.length}
                      </span>
                    )}
                    {mcpToolsExpanded ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
                  </div>
                  {mcpToolsExpanded && (
                    <div className="p-4 border-t border-white/[0.05]">
                      <p className="text-xs text-gray-500 mb-3">{t("mcpToolsHint")}</p>
                      {mcpServices.length === 0 ? (
                        <div className="text-center py-4">
                          <div className="w-10 h-10 rounded-xl bg-white/5 flex items-center justify-center mx-auto mb-2">
                            <Server size={16} className="text-gray-600" />
                          </div>
                          <p className="text-xs text-gray-500 mb-2">
                            {locale === "zh" ? "暂无可用的 MCP 服务" : "No MCP services available"}
                          </p>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setCurrentView("list")}
                          >
                            {locale === "zh" ? "前往创建" : "Create One"}
                          </Button>
                        </div>
                      ) : (
                        <div className="space-y-2">
                          {mcpServices.map((service) => {
                            const isBuiltin = BUILTIN_SERVICES.includes(service.name);
                            const testResult = mcpTestResults[service.name];
                            const tools = testResult?.tools || [];
                            return (
                              <label
                                key={service.name}
                                className={cn(
                                  "flex items-start gap-3 p-3 rounded-lg cursor-pointer transition-all border",
                                  selectedMcpServices.includes(service.name)
                                    ? "bg-emerald-500/10 border-emerald-500/30"
                                    : "bg-white/[0.02] border-white/[0.05] hover:bg-white/[0.04]"
                                )}
                              >
                                <input
                                  type="checkbox"
                                  checked={selectedMcpServices.includes(service.name)}
                                  onChange={(e) => {
                                    if (e.target.checked) {
                                      setSelectedMcpServices([...selectedMcpServices, service.name]);
                                    } else {
                                      setSelectedMcpServices(selectedMcpServices.filter((s) => s !== service.name));
                                    }
                                  }}
                                  className="mt-0.5 accent-emerald-500 w-4 h-4 rounded"
                                />
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2 flex-wrap">
                                    <span className="font-medium text-sm text-gray-200">{service.name}</span>
                                    {isBuiltin && (
                                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400">
                                        {locale === "zh" ? "预置" : "builtin"}
                                      </span>
                                    )}
                                    <span
                                      className={cn(
                                        "w-2 h-2 rounded-full",
                                        service.enabled ? "bg-emerald-500" : "bg-gray-500"
                                      )}
                                    />
                                    <span className="text-xs px-2 py-0.5 rounded bg-white/10 text-gray-400">
                                      {service.connection_type}
                                    </span>
                                    {testResult?.success && tools.length > 0 && (
                                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">
                                        {tools.length} {locale === "zh" ? "个工具" : "tools"}
                                      </span>
                                    )}
                                  </div>
                                  {service.description && (
                                    <p className="text-xs text-gray-500 mt-1 line-clamp-2">{service.description}</p>
                                  )}
                                  {testResult?.success && tools.length > 0 && (
                                    <div className="flex flex-wrap gap-1 mt-2">
                                      {tools.slice(0, 5).map((tool, idx) => (
                                        <span
                                          key={`settings-${service.name}-tool-${idx}`}
                                          title={tool.description || tool.name}
                                          className="text-[10px] px-1.5 py-0.5 rounded bg-white/10 text-gray-400 font-mono cursor-help"
                                        >
                                          {tool.name}
                                        </span>
                                      ))}
                                      {tools.length > 5 && (
                                        <span className="text-[10px] px-1.5 py-0.5 text-gray-500">
                                          +{tools.length - 5}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                </div>
                              </label>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 技能工具子区块 */}
                <div className={cn(
                  "border border-white/[0.05] rounded-xl overflow-hidden transition-all duration-300",
                  isEnvironmentCreating && "opacity-50 pointer-events-none"
                )}>
                  <div
                    className="px-4 py-3 flex items-center gap-3 bg-white/[0.01] cursor-pointer hover:bg-white/[0.03] transition-colors"
                    onClick={() => !isEnvironmentCreating && setSkillToolsExpanded(!skillToolsExpanded)}
                  >
                    <BookOpen size={14} className="text-purple-400" />
                    <span className="font-medium text-sm text-gray-300 flex-1">
                      {t("skillTools")}
                    </span>
                    {selectedSkills.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">
                        {selectedSkills.length}
                      </span>
                    )}
                    {isEnvironmentCreating && (
                      <Loader2 size={12} className="text-blue-400 animate-spin" />
                    )}
                    {skillToolsExpanded ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
                  </div>
                  {skillToolsExpanded && (
                    <div className="p-4 border-t border-white/[0.05]">
                      <p className="text-xs text-gray-500 mb-3">{t("skillToolsHint")}</p>
                      {skills.length === 0 ? (
                        <div className="text-center py-4">
                          <div className="w-10 h-10 rounded-xl bg-white/5 flex items-center justify-center mx-auto mb-2">
                            <BookOpen size={16} className="text-gray-600" />
                          </div>
                          <p className="text-xs text-gray-500">
                            {locale === "zh" ? "暂无可用的技能" : "No skills available"}
                          </p>
                        </div>
                      ) : (
                        <div className="space-y-2">
                          {skills.map((skill) => {
                            const isBuiltin = skill.source === "builtin";
                            return (
                              <label
                                key={skill.name}
                                className={cn(
                                  "flex items-start gap-3 p-3 rounded-lg cursor-pointer transition-all border",
                                  selectedSkills.includes(skill.name)
                                    ? "bg-purple-500/10 border-purple-500/30"
                                    : "bg-white/[0.02] border-white/[0.05] hover:bg-white/[0.04]"
                                )}
                              >
                                <input
                                  type="checkbox"
                                  checked={selectedSkills.includes(skill.name)}
                                  onChange={(e) => {
                                    if (e.target.checked) {
                                      setSelectedSkills([...selectedSkills, skill.name]);
                                    } else {
                                      setSelectedSkills(selectedSkills.filter((s) => s !== skill.name));
                                    }
                                  }}
                                  className="mt-0.5 accent-purple-500 w-4 h-4 rounded"
                                />
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2 flex-wrap">
                                    <span className="font-medium text-sm text-gray-200">{skill.name}</span>
                                    {isBuiltin ? (
                                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400">
                                        {locale === "zh" ? "官方" : "Official"}
                                      </span>
                                    ) : (
                                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">
                                        {locale === "zh" ? "自定义" : "Custom"}
                                      </span>
                                    )}
                                    {skill.version && (
                                      <span className="text-xs px-2 py-0.5 rounded bg-white/10 text-gray-400">
                                        v{skill.version}
                                      </span>
                                    )}
                                  </div>
                                  {skill.description && (
                                    <p className="text-xs text-gray-500 mt-1 line-clamp-2">{skill.description}</p>
                                  )}
                                  {skill.tags && skill.tags.length > 0 && (
                                    <div className="flex flex-wrap gap-1 mt-2">
                                      {skill.tags.map((tag) => (
                                        <span key={tag} className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400">
                                          {tag}
                                        </span>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              </label>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 子智能体工具子区块 */}
                <div className={cn(
                  "border border-white/[0.05] rounded-xl overflow-hidden transition-all duration-300",
                  isEnvironmentCreating && "opacity-50 pointer-events-none"
                )}>
                  <div
                    className="px-4 py-3 flex items-center gap-3 bg-white/[0.01] cursor-pointer hover:bg-white/[0.03] transition-colors"
                    onClick={() => !isEnvironmentCreating && setAgentToolsExpanded(!agentToolsExpanded)}
                  >
                    <Bot size={14} className="text-indigo-400" />
                    <span className="font-medium text-sm text-gray-300 flex-1">
                      {t("agentTools")}
                    </span>
                    {selectedSubAgents.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400">
                        {selectedSubAgents.length}
                      </span>
                    )}
                    {isEnvironmentCreating && (
                      <Loader2 size={12} className="text-blue-400 animate-spin" />
                    )}
                    {agentToolsExpanded ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
                  </div>
                  {agentToolsExpanded && (
                    <div className="p-4 border-t border-white/[0.05]">
                      <p className="text-xs text-gray-500 mb-3">{t("agentToolsHint")}</p>
                      <SubAgentSelector
                        availableAgents={agents.map(agent => ({
                          name: agent.name,
                          persona: agent.description || "",
                          model_service: agent.model_service || null,
                          skills: [],
                          mcp_services: [],
                        }))}
                        currentAgentName={selectedAgent || undefined}
                        selectedAgents={selectedSubAgents}
                        onSelectionChange={setSelectedSubAgents}
                        cycleError={cycleError}
                        disabled={isEnvironmentCreating || isSaving}
                      />
                    </div>
                  )}
                </div>

                {/* 知识库工具子区块 */}
                <div className={cn(
                  "border border-white/[0.05] rounded-xl overflow-hidden transition-all duration-300",
                  isEnvironmentCreating && "opacity-50 pointer-events-none"
                )}>
                  <div
                    className="px-4 py-3 flex items-center gap-3 bg-white/[0.01] cursor-pointer hover:bg-white/[0.03] transition-colors"
                    onClick={() => !isEnvironmentCreating && setKbToolsExpanded(!kbToolsExpanded)}
                  >
                    <Database size={14} className="text-emerald-400" />
                    <span className="font-medium text-sm text-gray-300 flex-1">
                      {t("knowledgeTools")}
                    </span>
                    {selectedKnowledgeBases.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400">
                        {selectedKnowledgeBases.length}
                      </span>
                    )}
                    {isEnvironmentCreating && (
                      <Loader2 size={12} className="text-blue-400 animate-spin" />
                    )}
                    {kbToolsExpanded ? <ChevronUp size={14} className="text-gray-500" /> : <ChevronDown size={14} className="text-gray-500" />}
                  </div>
                  {kbToolsExpanded && (
                    <div className="p-4 border-t border-white/[0.05]">
                      <p className="text-xs text-gray-500 mb-3">{t("knowledgeToolsHint")}</p>
                      <KnowledgeBaseSelector
                        selectedIds={selectedKnowledgeBases}
                        onChange={setSelectedKnowledgeBases}
                        disabled={isEnvironmentCreating || isSaving}
                        onCreateNew={() => { setEditingKb(null); setKbDialogOpen(true); }}
                        onItemClick={(kb) => { setSelectedKb(kb); setKbDetailOpen(true); }}
                      />
                    </div>
                  )}
                </div>
              </CardContent>
            )}
          </Card>
        </motion.div>

        {/* Right Panel - Chat - 环境初始化期间禁用 */}
        <motion.div
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          className="flex-1 min-w-0 relative"
        >
          <Card
            data-chat-card="true"
            className={cn(
              "sticky top-24 overflow-hidden h-[calc(100vh-180px)] min-h-[500px] transition-all duration-300",
              isEnvironmentCreating && "opacity-50 pointer-events-none"
            )}
          >
            <div className="px-5 py-4 border-b border-white/[0.05] flex items-center justify-between bg-white/[0.02]">
              <div className="flex items-center gap-3">
                <Bot size={16} className={cn(
                  "transition-colors",
                  isEnvironmentCreating ? "text-gray-500" : "text-emerald-400"
                )} />
                <span className="font-medium text-sm text-gray-300">{t("debugChat")}</span>
                {isEnvironmentCreating && (
                  <Loader2 size={14} className="text-blue-400 animate-spin ml-2" />
                )}
              </div>
              <div className="flex items-center gap-2">
                {/* 巻加历史按钮 */}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setConversationDrawerOpen(true)}
                  disabled={isEnvironmentCreating}
                  className="text-xs text-gray-400 hover:text-white hover:bg-white/10 disabled:opacity-50"
                >
                  <History className="w-3.5 h-3.5 mr-1.5" />
                  {t("historyConversations")}
                </Button>
              </div>
            </div>
            <div className="h-[calc(100%-57px)] relative">
              {/* 环境初始化中的遮罩层 */}
              {isEnvironmentCreating && (
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-gray-900/80 rounded-lg">
                  <div className="text-center">
                    <Loader2 className="w-8 h-8 mx-auto mb-3 text-blue-400 animate-spin" />
                    <p className="text-sm text-gray-300">
                      {locale === "zh" ? "环境初始化中，调试功能暂不可用" : "Environment initializing, debug unavailable"}
                    </p>
                  </div>
                </div>
              )}
              <AgentChat
                agentName={selectedAgent || ""}
                shortTermMemory={shortTermMemory}
                conversationId={currentConversationId}
                initialMessages={currentConversationMessages}
                onCreateConversation={async () => {
                  try {
                    // 创建新会话记录
                    if (!selectedAgent) return null;
                    const res = await fetch(apiPath('agents', selectedAgent, 'conversations'), {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ title: locale === "zh" ? "新对话" : "New Chat" })
                    });
                    if (res.ok) {
                      const data = await res.json();
                      setCurrentConversationId(data.id);
                      return data.id;
                    }
                  } catch (error) {
                    console.error("Failed to create conversation:", error);
                  }
                  return null;
                }}
                onConversationChange={async (id, messages, turnMessages) => {
                  // 更新本地状态
                  if (id) {
                    setCurrentConversationId(id);
                  }
                  setCurrentConversationMessages(messages);

                  if (id && messages.length > 0) {
                    try {
                      // 正常对话只同步本轮增量，避免每轮重写整段会话。
                      // 未提供 turnMessages 的显式调用仍保留全量保存语义。
                      const persistence = selectConversationPersistence(messages, turnMessages);
                      const backendMessages = persistence.messages.map(msg => ({
                        id: msg.id,
                        role: msg.role,
                        content: msg.content,
                        thinking: msg.thinking || undefined,
                        tool_calls: msg.toolCalls || undefined,
                        metrics: msg.metrics || undefined
                      }));

                      if (!selectedAgent) return;
                      const endpoint = persistence.mode === 'sync'
                        ? apiPath('agents', selectedAgent, 'conversations', id, 'messages', 'sync')
                        : apiPath('agents', selectedAgent, 'conversations', id, 'save');
                      const response = await fetch(endpoint, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ messages: backendMessages })
                      });
                      if (!response.ok) {
                        throw new Error(`Conversation persistence failed: ${response.status}`);
                      }
                    } catch (error) {
                      console.error("Failed to persist conversation:", error);
                    }
                  }
                }}
              />
            </div>
          </Card>
        </motion.div>

        {/* 添加历史会话抽屉 */}
        <ConversationDrawer
          open={conversationDrawerOpen}
          onClose={() => setConversationDrawerOpen(false)}
          agentName={selectedAgent || ""}
          currentConversationId={currentConversationId}
          onSelectConversation={async (id) => {
            if (!selectedAgent) return;
            try {
              // 从后端加载会话详情（含消息）
              const res = await fetch(apiPath('agents', selectedAgent, 'conversations', id));
              if (res.ok) {
                const data = await res.json();
                // 转换消息格式并设置
                const convertedMessages = convertBackendMessages(data.messages || []);
                setCurrentConversationId(id);
                setCurrentConversationMessages(convertedMessages);
              }
            } catch (error) {
              console.error("Failed to load conversation:", error);
            }
            setConversationDrawerOpen(false);
          }}
          onNewConversation={async () => {
            if (!selectedAgent) return;
            try {
              // 创建新会话记录
              const res = await fetch(apiPath('agents', selectedAgent, 'conversations'), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: locale === "zh" ? "新对话" : "New Chat" })
              });
              if (res.ok) {
                const data = await res.json();
                setCurrentConversationId(data.id);
                setCurrentConversationMessages([]);
              } else {
                // 降级处理：保持原行为
                setCurrentConversationId(null);
                setCurrentConversationMessages([]);
              }
            } catch (error) {
              console.error("Failed to create conversation:", error);
              setCurrentConversationId(null);
              setCurrentConversationMessages([]);
            }
            setConversationDrawerOpen(false);
          }}
        />
      </div>
    </div>
  );
}

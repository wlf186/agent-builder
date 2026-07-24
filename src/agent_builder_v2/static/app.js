"use strict";

const TERMINAL_EVENTS = new Set(["run.completed", "run.failed", "run.cancelled"]);
const RUN_ID_PATTERN = /^[a-f0-9]{32}$/;
const AGENT_ID_PATTERN = /^[a-f0-9-]{32,36}$/;
const SYSTEM_AGENT_ID = "00000000-0000-4000-8000-000000000001";
const MODEL_ID_PATTERN = /^[A-Za-z0-9._:/+-]{1,128}$/;
const MAX_SSE_RECONNECTS = 3;
const SSE_RECONNECT_DELAY_MS = 250;
const PREPARATION_POLL_INITIAL_MS = 200;
const PREPARATION_POLL_MAX_MS = 1500;
const PREPARATION_MONITOR_MAX_MS = 300000;
const LOGOUT_TIMEOUT_MS = 5000;
const MESSAGE_MAX_BYTES = 8192;
const MESSAGE_MAX_CHARACTERS = 8192;
const STREAM_CONTROL_VERSION = "stream-control-v1";
const PREPARATION_STAGE_LABELS = Object.freeze({
  reading_history: "正在读取会话历史",
  building_context: "正在组装系统指令、历史和本轮能力",
  evaluating_compaction: "正在核对上下文容量和自动整理阈值",
  summarizing_history: "正在生成历史摘要；长会话可能需要稍候",
  verifying_continuation: "正在验证回答后仍可继续对话",
  admitting_run: "上下文已就绪，正在创建本轮运行",
});
const TURN_STATUS_LABELS = {
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const EVENT_PRESENTATIONS = Object.freeze({
  "run.started": {
    subject: "Harness",
    direction: "Harness 内部",
    action: "启动 Run",
    explanation: "受信控制面已启动本次 Run；这不是发往模型的请求边界。",
    tone: "internal",
  },
  "run.completed": {
    subject: "Harness",
    direction: "Harness 内部",
    action: "完成 Run",
    explanation: "受信控制面已将本次 Run 收敛为成功终态。",
    tone: "terminal",
  },
  "run.failed": {
    subject: "Harness",
    direction: "Harness 内部",
    action: "标记 Run 失败",
    explanation: "受信控制面已将本次 Run 收敛为失败终态。",
    tone: "failure",
  },
  "run.cancelled": {
    subject: "Harness",
    direction: "Harness 内部",
    action: "取消 Run",
    explanation: "受信控制面已将本次 Run 收敛为取消终态。",
    tone: "failure",
  },
  "model.request.started": {
    subject: "Harness",
    direction: "Harness → LLM",
    action: "提交模型请求",
    explanation: "Harness 已开始一次真实模型调用；规范事件只描述边界和受限元数据。",
    tone: "provider-outbound",
  },
  "model.transport.attempt": {
    subject: "模型传输",
    direction: "Harness ↔ 模型服务",
    action: "等待模型首帧",
    explanation: "每次受控 HTTP 尝试只记录开始、首帧或失败的有界计时，不保存请求正文或服务地址。",
    tone: "provider-outbound",
  },
  "model.response.finished": {
    subject: "LLM / Broker",
    direction: "LLM / Broker → Harness",
    action: "收敛模型响应",
    explanation: "正常时对应 provider 终帧；错误或取消时也可能由 Broker 收敛，并不代表模型正文。",
    tone: "model",
  },
  "model.recovery.started": {
    subject: "Harness",
    direction: "Harness 内部",
    action: "切换溢出恢复投影",
    explanation: "Provider 明确拒绝上下文后，Harness 切换到一次性、更小的受信上下文投影；不会重放工具副作用。",
    tone: "recovery",
  },
  "assistant.block.started": {
    subject: "LLM",
    direction: "LLM → Harness",
    action: "开始回答内容块",
    explanation: "Harness 收到经过规范化的模型回答内容块起始事件。",
    tone: "model",
  },
  "assistant.block.delta": {
    subject: "LLM",
    direction: "LLM → Harness",
    action: "流式生成回答增量",
    explanation: "Harness 收到经过规范化的模型回答文本增量。",
    tone: "model",
  },
  "assistant.block.finished": {
    subject: "LLM",
    direction: "LLM → Harness",
    action: "完成回答内容块",
    explanation: "Harness 收到经过规范化的模型回答内容块完成事件。",
    tone: "model",
  },
  "assistant.block.discarded": {
    subject: "Harness",
    direction: "Harness 恢复",
    action: "丢弃未完成回答块",
    explanation: "Harness 在失败、取消或恢复过程中移除未完成内容；这不是模型响应。",
    tone: "recovery",
  },
  "tool.call.requested": {
    subject: "LLM",
    direction: "LLM → Harness",
    action: "请求调用工具",
    explanation: "Harness 收到经过规范化的模型工具调用请求。",
    tone: "model",
  },
  "tool.call.started": {
    subject: "Harness",
    direction: "Harness → Tool",
    action: "启动受控工具调用",
    explanation: "Harness 已验证请求并开始执行受控工具。",
    tone: "tool",
  },
  "tool.call.finished": {
    subject: "Tool/恢复",
    direction: "Tool/恢复 → Harness",
    action: "返回工具结果",
    explanation: "工具结果或恢复结果已回到 Harness；此事件不证明结果已经发送给模型。",
    tone: "tool",
  },
  "stream.gap": {
    subject: "Replay",
    direction: "Replay 控制",
    action: "报告事件序列缺口",
    explanation: "浏览器回放控制消息说明部分事件不可用；它不是 Run 语义事件。",
    tone: "control",
  },
  "stream.snapshot": {
    subject: "Replay",
    direction: "Replay 控制",
    action: "提供回放状态快照",
    explanation: "浏览器回放控制消息提供已收敛状态快照；它不是 Run 语义事件。",
    tone: "control",
  },
});
const UNKNOWN_EVENT_PRESENTATION = Object.freeze({
  subject: "未知",
  direction: "方向未知",
  action: "未识别事件",
  explanation: "当前前端没有这个事件 kind 的语义映射，因此保持中性展示。",
  tone: "unknown",
});
const CONTEXT_RESPONSE_FIELDS = Object.freeze([
  "identity",
  "availability",
  "context_plan",
  "renderer",
  "provider_message_count",
  "sections",
  "content_exposure",
  "notice",
]);
const CONTEXT_PLAN_FIELDS = Object.freeze([
  "plan_id",
  "digest",
  "toolset_digest",
  "section_count",
  "history_message_count",
  "included_history_message_count",
  "omitted_history_message_count",
  "history_source_digest",
  "windowing_strategy",
  "estimated_input_tokens",
  "native_context_tokens",
  "operational_context_tokens",
  "input_budget_tokens",
  "compact_at_tokens",
  "compact_target_tokens",
  "output_reserve_tokens",
  "template_reserve_tokens",
  "estimator",
  "admission_count_version",
  "admission_basis",
  "soft_count_version",
  "soft_count_availability",
  "soft_estimated_tokens",
  "soft_error_margin_tokens",
  "included_turn_bundle_count",
]);
// Durable replay can legitimately contain a pre-count-domain run.started
// projection. Keep that decoder path read-only; all newly created plans use
// CONTEXT_PLAN_FIELDS above.
const LEGACY_CONTEXT_PLAN_FIELDS = Object.freeze(
  CONTEXT_PLAN_FIELDS.slice(0, 18),
);
const CONTEXT_SECTION_FIELDS = Object.freeze([
  "id",
  "role",
  "trust",
  "provenance",
  "cache",
  "truncation",
  "dependency_digest",
  "budget_tokens",
  "truncation_reason",
  "estimated_tokens",
  "content_bytes",
  "content_digest",
]);
const CHAT_ONLY_PROJECTION_FIELDS = Object.freeze([
  "version",
  "availability",
  "projection_mode",
  "fixed_context_tokens",
  "fixed_context_error_margin_tokens",
  "safe_user_tokens",
  "compact_before_user_tokens",
  "soft_trigger_tokens",
  "hard_input_tokens",
  "operational_context_tokens",
  "projection_strategy",
  "count_basis",
  "renderer_version",
  "toolset_digest",
]);
const MODEL_ERROR_LABELS = Object.freeze({
  model_first_frame_timeout: "等待模型首帧超时；零输出重试仍未恢复",
  model_stream_idle_timeout: "模型已开始响应，但流式传输长时间无新帧",
  model_turn_deadline: "模型持续响应，但单次调用超过总时限",
  model_transport_timeout: "模型网络传输阶段超时",
  model_busy: "本地有界模型队列等待超时",
  model_temporarily_unhealthy: "模型服务连续未返回首帧，已短暂退避；请稍后重试",
});
const TERMINAL_STAGE_LABELS = Object.freeze({
  model: "模型调用",
  capability: "能力执行",
  sandbox: "沙箱执行",
  persistence: "持久化",
  control: "控制面",
  worker: "运行进程",
  runtime: "运行时",
});
const AGENT_STATE_LABELS = Object.freeze({
  provisioning: "正在创建",
  active: "可用",
  renaming: "正在重命名",
  upgrading: "正在重建环境",
  deleting: "正在删除",
});
const TRANSPORT_OUTCOME_LABELS = Object.freeze({
  first_frame_received: "已收到首个响应",
  first_frame_timeout: "等待首个响应超时",
  transport_timeout: "连接模型服务超时",
  turn_deadline: "本轮模型调用达到时限",
  unavailable: "模型服务暂时不可用",
  rejected: "模型服务拒绝请求",
  cancelled: "连接已取消",
  failed_before_first_frame: "收到响应前连接失败",
});
const API_ERROR_LABELS = Object.freeze({
  invalid_session_cursor: "更早消息的位置已失效，请刷新会话后重试",
  invalid_session_page: "会话消息分页响应无效，请刷新后重试",
  invalid_session_relationship: "无法识别会话分支关系，请刷新后重试",
  invalid_session_title: "会话名称不符合要求，请修改后重试",
  session_not_found: "会话不存在或已被删除",
  session_relationship_conflict: "会话状态已变化，暂时不能创建分支",
  session_rename_conflict: "会话状态已变化，暂时不能重命名",
  session_state_unavailable: "会话当前不可用，请稍后重试",
  conversation_turn_capacity_exhausted: "当前会话已达到轮次上限，请继续为新会话",
  model_busy: "本地模型当前繁忙，请稍后重试",
});
const HTTP_ERROR_LABELS = Object.freeze({
  400: "请求内容不符合要求，请检查后重试",
  401: "认证失败，请检查访问令牌或重新登录",
  403: "当前操作未获授权，请刷新后重试",
  404: "请求的内容不存在或已被删除",
  409: "当前状态已变化，请刷新后重试",
  413: "提交内容过大，请缩短后重试",
  415: "提交格式不受支持",
  422: "提交内容无法处理，请检查后重试",
  429: "操作过于频繁，请稍后重试",
  500: "本地服务发生内部错误，请稍后重试",
  503: "本地服务暂时不可用，请稍后重试",
});

function modelErrorLabel(code) {
  return MODEL_ERROR_LABELS[code] ||
    "模型调用未能完成；可稍后重试，或在运行详情中查看技术信息";
}

const state = {
  csrfToken: null,
  authRequest: 0,
  agentId: null,
  agentEpoch: 0,
  agents: [],
  researchEnvironment: null,
  models: [],
  commands: [],
  defaultModelId: null,
  selectedModelId: null,
  sessions: [],
  sessionId: null,
  sessionRequest: 0,
  sessionController: null,
  sessionRollback: null,
  sessionListRequest: 0,
  permissionRequest: 0,
  timelineRequest: 0,
  timelineRuns: [],
  subagents: [],
  selectedTimelineRunId: null,
  contextRequest: 0,
  contextLoading: false,
  contextDialogTrigger: null,
  sessionLoading: false,
  activeRuns: new Map(),
  preparingRuns: new Map(),
  sessionDrafts: new Map(),
  backgroundCompletions: new Map(),
  get activeRun() {
    return this.sessionId ? this.activeRuns.get(this.sessionId) || null : null;
  },
  set activeRun(value) {
    if (value === null) {
      if (this.sessionId) this.activeRuns.delete(this.sessionId);
    } else {
      this.activeRuns.set(value.sessionId, value);
    }
  },
  get preparingRun() {
    return this.sessionId ? this.preparingRuns.get(this.sessionId) || null : null;
  },
  set preparingRun(value) {
    if (value === null) {
      if (this.sessionId) this.preparingRuns.delete(this.sessionId);
    } else {
      this.preparingRuns.set(value.sessionId, value);
    }
  },
  settling: false,
  mutationPending: false,
  conversationMessages: [],
  conversationPage: null,
  conversationPageLoading: false,
  conversationFollowLatest: true,
  conversationScrollTimer: null,
  sessionContextUsage: null,
  nextTurnPreview: null,
  previewRequest: 0,
  sessionTurnUsage: null,
  blocks: new Map(),
  liveAssistantContent: null,
  liveAssistantMessage: null,
  timelineEntries: [],
  timelineEntriesByRun: new Map(),
  timelineEntrySerial: 0,
  selectedTimelineEntryKey: null,
  selectedTimelineEntryKeyByRun: new Map(),
  timelineFilter: "all",
  followLatest: true,
  replayTimer: null,
  eventCount: 0,
  eventDetailTrigger: null,
};
const copyFeedbackTimers = new Map();

const elements = {
  statusDot: document.querySelector("#status-dot"),
  statusText: document.querySelector("#status-text"),
  composerStatus: document.querySelector("#composer-status"),
  interactionLiveStatus: document.querySelector("#interaction-live-status"),
  preparationLiveStatus: document.querySelector("#preparation-live-status"),
  runLiveStatus: document.querySelector("#run-live-status"),
  failureLiveStatus: document.querySelector("#failure-live-status"),
  loginPanel: document.querySelector("#login-panel"),
  loginForm: document.querySelector("#login-form"),
  loginError: document.querySelector("#login-error"),
  tokenInput: document.querySelector("#token-input"),
  logoutButton: document.querySelector("#logout-button"),
  workspace: document.querySelector("#workspace"),
  navigationRail: document.querySelector("#navigation-rail"),
  navigationToggle: document.querySelector("#navigation-toggle"),
  navigationClose: document.querySelector("#navigation-close"),
  workspaceBackdrop: document.querySelector("#workspace-backdrop"),
  runtimeInspectorButton: document.querySelector("#runtime-inspector-button"),
  runtimeInspectorClose: document.querySelector("#runtime-inspector-close"),
  runtimeEventBadge: document.querySelector("#runtime-event-badge"),
  replayWorkbench: document.querySelector("#replay-workbench"),
  agentId: document.querySelector("#agent-id"),
  activeAgentTitle: document.querySelector("#active-agent-title"),
  agentDrawer: document.querySelector("#agent-drawer"),
  agentList: document.querySelector("#agent-list"),
  agentListStatus: document.querySelector("#agent-list-status"),
  agentEmpty: document.querySelector("#agent-empty"),
  newAgentForm: document.querySelector("#new-agent-form"),
  newAgentName: document.querySelector("#new-agent-name"),
  newAgentButton: document.querySelector("#new-agent-button"),
  researchEnvironmentStatus: document.querySelector("#research-environment-status"),
  researchEnvironmentPackages: document.querySelector("#research-environment-packages"),
  researchEnvironmentInstall: document.querySelector("#research-environment-install"),
  researchEnvironmentDelete: document.querySelector("#research-environment-delete"),
  newSessionButton: document.querySelector("#new-session-button"),
  sessionList: document.querySelector("#session-list"),
  sessionListStatus: document.querySelector("#session-list-status"),
  sessionEmpty: document.querySelector("#session-empty"),
  activeSessionTitle: document.querySelector("#active-session-title"),
  sessionId: document.querySelector("#session-id"),
  runForm: document.querySelector("#run-form"),
  modelSelect: document.querySelector("#model-select"),
  compactInput: document.querySelector("#compact-input"),
  messageInput: document.querySelector("#message-input"),
  messageByteUsage: document.querySelector("#message-byte-usage"),
  commandHelpList: document.querySelector("#command-help-list"),
  commandResult: document.querySelector("#command-result"),
  commandResultTitle: document.querySelector("#command-result-title"),
  commandResultJson: document.querySelector("#command-result-json"),
  commandResultClose: document.querySelector("#command-result-close"),
  permissionPanel: document.querySelector("#permission-panel"),
  permissionList: document.querySelector("#permission-list"),
  runButton: document.querySelector("#run-button"),
  continueSessionButton: document.querySelector("#continue-session-button"),
  cancelButton: document.querySelector("#cancel-button"),
  runId: document.querySelector("#run-id"),
  conversationMessages: document.querySelector("#conversation-messages"),
  conversationEmpty: document.querySelector("#conversation-empty"),
  conversationLatestButton: document.querySelector("#conversation-latest-button"),
  contextUsage: document.querySelector("#context-usage"),
  contextUsageLabel: document.querySelector("#context-usage-label"),
  contextUsageValue: document.querySelector("#context-usage-value"),
  contextUsageMeter: document.querySelector("#context-usage-meter"),
  contextUsageFill: document.querySelector("#context-usage-fill"),
  contextUsageDetail: document.querySelector("#context-usage-detail"),
  contextTechnicalValue: document.querySelector("#context-technical-value"),
  turnTokenUsageLabel: document.querySelector("#turn-token-usage-label"),
  turnTokenUsageValue: document.querySelector("#turn-token-usage-value"),
  subagentPanel: document.querySelector("#subagent-panel"),
  subagentList: document.querySelector("#subagent-list"),
  eventList: document.querySelector("#event-list"),
  eventCount: document.querySelector("#event-count"),
  timelineRunSelect: document.querySelector("#timeline-run-select"),
  replayPrevButton: document.querySelector("#replay-prev-button"),
  replayPlayButton: document.querySelector("#replay-play-button"),
  replayNextButton: document.querySelector("#replay-next-button"),
  replayFollowButton: document.querySelector("#replay-follow-button"),
  timelineFilterForm: document.querySelector("#timeline-filter-form"),
  eventInspectorEmpty: document.querySelector("#event-inspector-empty"),
  eventInspectorSummary: document.querySelector("#event-inspector-summary"),
  eventInspectorBusiness: document.querySelector("#event-inspector-business"),
  eventInspectorPayload: document.querySelector("#event-inspector-payload"),
  eventInspectorEnvelope: document.querySelector("#event-inspector-envelope"),
  eventInspectorContextButton: document.querySelector("#event-inspector-context-button"),
  contextInspectButton: document.querySelector("#context-inspect-button"),
  contextInspectDialog: document.querySelector("#context-inspect-dialog"),
  contextInspectClose: document.querySelector("#context-inspect-close"),
  contextInspectAvailability: document.querySelector("#context-inspect-availability"),
  contextInspectMetrics: document.querySelector("#context-inspect-metrics"),
  contextSectionList: document.querySelector("#context-section-list"),
  contextInspectNotice: document.querySelector("#context-inspect-notice"),
  contextInspectJson: document.querySelector("#context-inspect-json"),
  eventDetailDialog: document.querySelector("#event-detail-dialog"),
  eventDetailClose: document.querySelector("#event-detail-close"),
  eventDetailSummary: document.querySelector("#event-detail-summary"),
  eventDetailJson: document.querySelector("#event-detail-json"),
};

function statusChannelFor(message) {
  if (/失败|无法|错误|超时|中断|拒绝|不可用|已过期/.test(message)) return "failure";
  if (/准备|整理|摘要|压缩|上下文|正在提交|正在创建/.test(message)) return "preparation";
  if (/运行|正在执行|模型|事件流|重连|等待.+(?:响应|结果|收敛)/.test(message)) return "running";
  return "interaction";
}

function setStatus(message, channel = "auto") {
  elements.composerStatus.textContent = message;
  const liveRegions = {
    interaction: elements.interactionLiveStatus,
    preparation: elements.preparationLiveStatus,
    running: elements.runLiveStatus,
    failure: elements.failureLiveStatus,
  };
  const selectedChannel = message
    ? (channel === "auto" ? statusChannelFor(message) : channel)
    : null;
  for (const [name, region] of Object.entries(liveRegions)) {
    if (region && (name !== selectedChannel || region.textContent !== message)) {
      region.textContent = name === selectedChannel ? message : "";
    }
  }
}

function setConnectionStatus(message) {
  elements.statusText.textContent = message;
}

function conversationStateKey(sessionId, agentId = state.agentId) {
  if (
    typeof agentId !== "string" || !AGENT_ID_PATTERN.test(agentId) ||
    typeof sessionId !== "string" || !RUN_ID_PATTERN.test(sessionId)
  ) return null;
  return `${agentId}:${sessionId}`;
}

function purgeAgentDrafts(agentId) {
  const prefix = `${agentId}:`;
  for (const key of state.sessionDrafts.keys()) {
    if (key.startsWith(prefix)) state.sessionDrafts.delete(key);
  }
}

function pruneSessionBrowserState(agentId, sessions) {
  const retained = new Set(sessions.map((session) => session.session_id));
  const prefix = `${agentId}:`;
  for (const key of state.sessionDrafts.keys()) {
    if (!key.startsWith(prefix)) continue;
    const sessionId = key.slice(prefix.length);
    if (
      !retained.has(sessionId) && !state.activeRuns.has(sessionId) &&
      !state.preparingRuns.has(sessionId)
    ) state.sessionDrafts.delete(key);
  }
  for (const sessionId of state.backgroundCompletions.keys()) {
    if (
      !retained.has(sessionId) && !state.activeRuns.has(sessionId) &&
      !state.preparingRuns.has(sessionId)
    ) state.backgroundCompletions.delete(sessionId);
  }
}

function runIsTracked(runContext) {
  return Boolean(
    runContext && state.activeRuns.get(runContext.sessionId) === runContext &&
    runContext.agentId === state.agentId &&
    runContext.agentEpoch === state.agentEpoch
  );
}

function preparationIsTracked(preparation) {
  return Boolean(
    preparation && state.preparingRuns.get(preparation.sessionId) === preparation &&
    preparation.agentId === state.agentId &&
    preparation.agentEpoch === state.agentEpoch
  );
}

function agentScopeIsCurrent(agentId, agentEpoch) {
  return state.agentId === agentId && state.agentEpoch === agentEpoch;
}

function authScopeIsCurrent(authRequest, csrfToken) {
  return state.authRequest === authRequest && state.csrfToken === csrfToken;
}

function beginMutation() {
  const scope = { authRequest: state.authRequest, csrfToken: state.csrfToken };
  state.mutationPending = true;
  setRunControls();
  return scope;
}

function mutationScopeIsCurrent(scope) {
  return authScopeIsCurrent(scope.authRequest, scope.csrfToken);
}

function finishMutation(scope) {
  if (!mutationScopeIsCurrent(scope)) return;
  state.mutationPending = false;
  setRunControls();
}

function normalizePreparationStatus(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value) ||
    value.version !== "run-preparation-v1" ||
    !["idle", "preparing", "cancelling"].includes(value.state) ||
    (value.state === "idle"
      ? value.operation_id !== null
      : typeof value.operation_id !== "string" || !RUN_ID_PATTERN.test(value.operation_id)) ||
    !Number.isSafeInteger(value.elapsed_ms) || value.elapsed_ms < 0 ||
    value.elapsed_ms > PREPARATION_MONITOR_MAX_MS ||
    (value.state === "idle" && (value.stage !== null || value.elapsed_ms !== 0)) ||
    (value.state !== "idle" && !Object.hasOwn(PREPARATION_STAGE_LABELS, value.stage))
  ) return null;
  return {
    state: value.state,
    operationId: value.operation_id,
    stage: value.stage,
    elapsedMs: value.elapsed_ms,
  };
}

function preparationStatusMessage(status) {
  if (status.state === "cancelling") return "正在安全取消上下文准备";
  const label = PREPARATION_STAGE_LABELS[status.stage] || "正在准备本轮";
  const elapsed = status.elapsedMs >= 1000
    ? ` · 已用 ${(status.elapsedMs / 1000).toFixed(1)} 秒`
    : "";
  return `${label}${elapsed}；消息草稿会保留`;
}

function waitForPreparationPoll(preparation, delayMs) {
  return new Promise((resolve) => {
    if (!preparationIsTracked(preparation) || preparation.monitorStopped) {
      resolve(false);
      return;
    }
    const finish = (ready) => {
      if (preparation.statusTimer !== null) {
        window.clearTimeout(preparation.statusTimer);
        preparation.statusTimer = null;
      }
      if (preparation.statusWake === finish) preparation.statusWake = null;
      resolve(ready);
    };
    preparation.statusWake = finish;
    preparation.statusTimer = window.setTimeout(() => finish(true), delayMs);
  });
}

function stopPreparationMonitor(preparation) {
  if (!preparation) return;
  preparation.monitorStopped = true;
  preparation.statusController?.abort();
  preparation.statusController = null;
  if (preparation.statusTimer !== null) {
    window.clearTimeout(preparation.statusTimer);
    preparation.statusTimer = null;
  }
  const wake = preparation.statusWake;
  preparation.statusWake = null;
  if (typeof wake === "function") wake(false);
}

async function monitorPreparation(preparation, { immediate = false } = {}) {
  let delayMs = immediate ? 0 : PREPARATION_POLL_INITIAL_MS;
  let previousStage = null;
  let failures = 0;
  while (preparationIsTracked(preparation) && !preparation.monitorStopped) {
    const elapsed = performance.now() - preparation.startedAt;
    if (elapsed >= PREPARATION_MONITOR_MAX_MS) {
      if (state.sessionId === preparation.sessionId) {
        setStatus("上下文准备已超过 5 分钟；可以取消后重试", "failure");
      }
      return;
    }
    if (!await waitForPreparationPoll(preparation, delayMs)) return;
    if (!preparationIsTracked(preparation) || preparation.monitorStopped) return;
    const controller = new AbortController();
    preparation.statusController = controller;
    try {
      const response = await api(
        `/api/agents/${encodeURIComponent(preparation.agentId)}/sessions/` +
        `${encodeURIComponent(preparation.sessionId)}/preparation`,
        { signal: controller.signal },
      );
      const status = normalizePreparationStatus(await response.json());
      if (!status) throw new Error("准备进度响应格式无效");
      if (!preparationIsTracked(preparation)) return;
      failures = 0;
      if (status.state === "idle") {
        if (preparation.cancelConfirmed) {
          preparation.converged = true;
          stopPreparationMonitor(preparation);
          if (preparationIsTracked(preparation)) {
            state.preparingRuns.delete(preparation.sessionId);
          }
          renderSessionList();
          if (
            state.agentId === preparation.agentId &&
            state.sessionId === preparation.sessionId
          ) {
            await selectSession(preparation.sessionId, {
              preserveTimeline: true,
            }).catch(() => null);
            setStatus("已安全取消上下文准备；消息草稿仍在输入框中", "interaction");
          }
          setRunControls();
        }
        return;
      }
      if (preparation.operationId === null) {
        preparation.operationId = status.operationId;
      } else if (preparation.operationId !== status.operationId) {
        preparation.converged = true;
        stopPreparationMonitor(preparation);
        if (preparationIsTracked(preparation)) {
          state.preparingRuns.delete(preparation.sessionId);
        }
        renderSessionList();
        if (
          state.agentId === preparation.agentId &&
          state.sessionId === preparation.sessionId
        ) {
          setStatus("准备操作已变化；没有取消新的任务，请刷新会话确认状态", "failure");
        }
        setRunControls();
        return;
      }
      if (
        status.state !== "idle" && state.agentId === preparation.agentId &&
        state.sessionId === preparation.sessionId
      ) {
        setStatus(
          preparation.cancelConfirmed
            ? "取消请求已确认，正在等待服务端安全收敛；消息草稿会保留"
            : preparationStatusMessage(status),
          "preparation",
        );
      }
      delayMs = status.stage !== previousStage
        ? 350
        : Math.min(PREPARATION_POLL_MAX_MS, delayMs + 250);
      previousStage = status.stage;
    } catch (error) {
      if (error.name === "AbortError" || preparation.monitorStopped) return;
      failures = Math.min(4, failures + 1);
      delayMs = Math.min(PREPARATION_POLL_MAX_MS, 500 * (2 ** failures));
      if (
        failures >= 3 && state.agentId === preparation.agentId &&
        state.sessionId === preparation.sessionId
      ) {
        setStatus("本轮仍在准备，但暂时无法读取细分进度；仍可取消", "preparation");
      }
    } finally {
      if (preparation.statusController === controller) {
        preparation.statusController = null;
      }
    }
  }
}

function hasAgentActivity() {
  return state.activeRuns.size > 0 || state.preparingRuns.size > 0;
}

function hasUnconfirmedPreparationCancellation() {
  return Array.from(state.preparingRuns.values()).some((preparation) => (
    preparation.cancelPending === true && preparation.cancelConfirmed !== true
  ));
}

function detachAgentBrowserActivity() {
  for (const runContext of state.activeRuns.values()) {
    stopTransportWait(runContext);
    stopPermissionPoll(runContext);
    runContext.controller?.abort();
  }
  for (const preparation of state.preparingRuns.values()) {
    stopPreparationMonitor(preparation);
    preparation.cancelController?.abort();
    preparation.controller.abort();
  }
  state.activeRuns.clear();
  state.preparingRuns.clear();
  state.backgroundCompletions.clear();
  state.permissionRequest += 1;
}

function saveSelectedDraft() {
  if (!state.sessionId) return;
  const key = conversationStateKey(state.sessionId);
  if (!key) return;
  const message = elements.messageInput.value;
  // The DOM maxlength protects user input; this second fail-closed boundary
  // also covers programmatic assignments and keeps the in-memory draft map
  // from retaining an unexpectedly large string.
  if (
    message.length > MESSAGE_MAX_CHARACTERS ||
    new TextEncoder().encode(message).length > MESSAGE_MAX_BYTES
  ) {
    state.sessionDrafts.delete(key);
    return;
  }
  state.sessionDrafts.set(key, {
    message,
    modelId: state.selectedModelId,
    compact: elements.compactInput.checked,
  });
}

function restoreSelectedDraft() {
  const key = state.sessionId ? conversationStateKey(state.sessionId) : null;
  const draft = key ? state.sessionDrafts.get(key) : null;
  elements.messageInput.value = draft?.message || "";
  const requestedModel = draft?.modelId || state.defaultModelId;
  if (requestedModel && state.models.some((item) => item.model_id === requestedModel)) {
    state.selectedModelId = requestedModel;
    elements.modelSelect.value = requestedModel;
  }
  elements.compactInput.checked = draft?.compact === true;
  resizeComposer();
  renderMessageByteUsage();
}

function narrowWorkspace() {
  return window.matchMedia("(max-width: 860px)").matches;
}

function setRuntimeInspector(open, { focus = false } = {}) {
  const next = Boolean(open);
  elements.workspace.classList.toggle("runtime-open", next);
  elements.runtimeInspectorButton.setAttribute("aria-expanded", String(next));
  elements.replayWorkbench.setAttribute("aria-hidden", String(!next));
  elements.replayWorkbench.inert = !next;
  if (next) {
    if (narrowWorkspace()) {
      elements.workspace.classList.remove("navigation-open");
      elements.navigationToggle.setAttribute("aria-expanded", "false");
      elements.navigationRail.setAttribute("aria-hidden", "true");
      elements.navigationRail.inert = true;
    }
    if (focus) {
      // Visibility changes are discrete. Force the opened panel through layout
      // before moving focus so Chromium does not discard focus as "hidden".
      elements.replayWorkbench.getBoundingClientRect();
      elements.runtimeInspectorClose.focus({ preventScroll: true });
      if (document.activeElement !== elements.runtimeInspectorClose) {
        window.requestAnimationFrame(() => {
          if (elements.workspace.classList.contains("runtime-open")) {
            elements.runtimeInspectorClose.focus({ preventScroll: true });
          }
        });
      }
    }
  } else {
    stopReplay();
    releaseHistoricalTimelineCaches();
    if (focus) elements.runtimeInspectorButton.focus();
  }
}

function setNavigation(open, { focus = false } = {}) {
  const next = Boolean(open);
  elements.navigationRail.setAttribute("aria-hidden", String(!next));
  elements.navigationRail.inert = !next;
  if (narrowWorkspace()) {
    elements.workspace.classList.toggle("navigation-open", next);
    elements.navigationToggle.setAttribute("aria-expanded", String(next));
    if (next) {
      setRuntimeInspector(false);
      if (focus) elements.navigationClose.focus();
    } else if (focus) {
      elements.navigationToggle.focus();
    }
    return;
  }
  elements.workspace.classList.toggle("sidebar-collapsed", !next);
  elements.navigationToggle.setAttribute("aria-expanded", String(next));
  if (!next && focus) elements.navigationToggle.focus();
}

function closeNavigationOnNarrowScreen() {
  if (narrowWorkspace()) setNavigation(false);
}

function resizeComposer() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 192)}px`;
}

function messageByteCount() {
  const value = elements.messageInput.value;
  if (value.length > MESSAGE_MAX_CHARACTERS) return MESSAGE_MAX_BYTES + 1;
  return new TextEncoder().encode(value).length;
}

function enforceMessageInputLimit({ announce = true } = {}) {
  const value = elements.messageInput.value;
  if (value.length > MESSAGE_MAX_CHARACTERS) {
    elements.messageInput.value = value.slice(0, MESSAGE_MAX_CHARACTERS);
  }
  const bytes = new TextEncoder().encode(elements.messageInput.value);
  if (bytes.length <= MESSAGE_MAX_BYTES) return false;
  let end = MESSAGE_MAX_BYTES;
  const decoder = new TextDecoder("utf-8", { fatal: true });
  while (end > 0) {
    try {
      elements.messageInput.value = decoder.decode(bytes.subarray(0, end));
      break;
    } catch (_error) {
      end -= 1;
    }
  }
  if (announce) {
    setStatus("输入已保留到 8,192 个 UTF-8 字节；超出部分未写入草稿", "interaction");
  }
  return true;
}

function renderMessageByteUsage() {
  const bytes = messageByteCount();
  elements.messageByteUsage.textContent = `${formatTokens(bytes)} / 8,192 字节`;
  elements.messageByteUsage.dataset.overLimit = String(bytes > 8192);
  return bytes;
}

function conversationIsNearLatest() {
  const remaining = (
    elements.conversationMessages.scrollHeight -
    elements.conversationMessages.scrollTop -
    elements.conversationMessages.clientHeight
  );
  return remaining <= 96;
}

function updateConversationLatestControl() {
  elements.conversationLatestButton.hidden = (
    state.conversationMessages.length === 0 || state.conversationFollowLatest
  );
}

function scheduleConversationLatest({ force = false } = {}) {
  if (force) state.conversationFollowLatest = true;
  if (!state.conversationFollowLatest) {
    updateConversationLatestControl();
    return;
  }
  const scroll = () => {
    state.conversationScrollTimer = null;
    elements.conversationMessages.scrollTop = elements.conversationMessages.scrollHeight;
    state.conversationFollowLatest = true;
    updateConversationLatestControl();
  };
  if (force) {
    if (state.conversationScrollTimer !== null) {
      window.clearTimeout(state.conversationScrollTimer);
      state.conversationScrollTimer = null;
    }
    scroll();
  } else if (state.conversationScrollTimer === null) {
    state.conversationScrollTimer = window.setTimeout(scroll, 0);
  }
}

function selectedModel() {
  return state.models.find((model) => model.model_id === state.selectedModelId) || null;
}

function latestConversationRunId() {
  return state.timelineRuns.filter((run) => run.kind !== "subagent").at(-1)?.runId || null;
}

function formatTokens(value) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function contextStrategyLabel(value) {
  return {
    full: "历史完整保留",
    "completed-turn-tail-v1": "保留最近的完整轮次",
    "completed-turn-collapse-v2": "较早轮次已做确定性整理",
    "semantic-summary-v1": "旧版摘要（只读回放）",
    "semantic-summary-v2": "较早轮次已使用摘要",
  }[value] || "策略暂不可用";
}

function formatContextPercent(value) {
  if (!Number.isFinite(value)) return "—";
  const bounded = Math.max(0, Math.min(100, value));
  return bounded < 10 && bounded > 0
    ? `${bounded.toFixed(1)}%`
    : `${Math.round(bounded)}%`;
}

function validChatOnlyProjection(value) {
  if (!hasExactFields(value, CHAT_ONLY_PROJECTION_FIELDS)) return false;
  const counts = [
    value.fixed_context_tokens,
    value.fixed_context_error_margin_tokens,
    value.safe_user_tokens,
    value.compact_before_user_tokens,
    value.soft_trigger_tokens,
    value.hard_input_tokens,
    value.operational_context_tokens,
  ];
  return Boolean(
    value.version === "next-turn-chat-only-projection-v1" &&
    value.availability === "available" && value.projection_mode === "chat_only" &&
    counts.every(isNonNegativeInteger) && value.operational_context_tokens >= 2048 &&
    value.fixed_context_tokens <= value.operational_context_tokens &&
    value.hard_input_tokens <= value.operational_context_tokens &&
    value.soft_trigger_tokens <= value.hard_input_tokens &&
    value.safe_user_tokens <= value.hard_input_tokens &&
    value.compact_before_user_tokens <= value.soft_trigger_tokens &&
    [
      "full",
      "completed-turn-tail-v1",
      "completed-turn-collapse-v2",
      "semantic-summary-v1",
      "semantic-summary-v2",
    ].includes(value.projection_strategy) &&
    [value.projection_strategy, value.count_basis, value.renderer_version].every(
      (item) => typeof item === "string" && item.length > 0 && item.length <= 128,
    ) &&
    typeof value.toolset_digest === "string" && /^[a-f0-9]{64}$/.test(value.toolset_digest)
  );
}

function contextProjectionForDisplay(preview) {
  if (preview?.availability === "available") {
    return { plan: preview, chatOnlyBaseline: false };
  }
  if (validChatOnlyProjection(preview?.chat_only_projection)) {
    return {
      plan: { ...preview, ...preview.chat_only_projection },
      chatOnlyBaseline: true,
    };
  }
  return { plan: preview, chatOnlyBaseline: false };
}

function renderSessionContextUsage() {
  const preview = state.nextTurnPreview;
  const { plan, chatOnlyBaseline } = contextProjectionForDisplay(preview);
  const fallbackModel = selectedModel();
  const total = plan?.operational_context_tokens || fallbackModel?.operational_context_tokens || null;
  const modelId = plan?.model_id || fallbackModel?.model_id || null;
  elements.contextUsageLabel.textContent = modelId
    ? `下一轮上下文 · ${modelId}${chatOnlyBaseline ? " · 纯对话基线" : ""}`
    : "下一轮上下文";
  if (!plan || plan.availability !== "available" || !total) {
    const reasons = {
      active_run: "本轮执行中，结束后刷新",
      no_provider_observation: "尚无可校准的模型服务实测",
      incomplete_provider_usage: "上一轮模型服务用量不完整",
      profile_or_projection_drift: "模型或上下文策略已变化，等待新实测",
      calibration_unavailable: "模型服务实测无法安全校准",
      toolset_calibration_unavailable: "已有纯对话实测；工具场景仍待校准",
      conversation_turn_capacity_exhausted: "会话轮次容量已满",
      projection_unavailable: "当前上下文无法生成可靠投影",
    };
    const reason = reasons[plan?.stale_reason] || null;
    elements.contextUsageValue.textContent = !state.sessionId
      ? "选择会话后显示占用与剩余"
      : plan
        ? "占用数与剩余比例暂不可用"
        : "正在读取占用数与剩余比例";
    elements.contextUsageDetail.textContent = (
      plan?.stale_reason === "toolset_calibration_unavailable"
        ? `纯对话实测不能安全推算工具场景；完成一次需要工具的对话后再显示。${
            total ? ` 模型窗口 ${formatTokens(total)} tokens。` : ""
          }`
        : total
          ? `${reason || "服务端投影尚未就绪"} · 模型窗口 ${formatTokens(total)} tokens · ` +
            "单条消息最多 8,192 个 UTF-8 字节"
          : reason || "容量由服务端按照当前模型、工具和已提交历史计算。"
    );
    elements.contextTechnicalValue.textContent = plan
      ? `状态 ${plan.stale_reason || "unavailable"} · 不使用浏览器估算 token`
      : "尚无可展示的模型与计数依据";
    elements.contextUsageMeter.setAttribute(
      "aria-valuemax",
      String(total || 100),
    );
    elements.contextUsageMeter.setAttribute("aria-valuenow", "0");
    elements.contextUsageMeter.setAttribute("aria-valuetext", elements.contextUsageValue.textContent);
    elements.contextUsageFill.style.width = "0%";
    elements.contextUsage.dataset.level = "empty";
    elements.contextUsage.title = (
      "不可用时不从事件或 UTF-8 字节数猜测 token。"
    );
    return;
  }
  const projectedTokens = plan.fixed_context_tokens;
  const compactRemaining = plan.compact_before_user_tokens;
  const hardRemaining = plan.safe_user_tokens;
  const compactAt = plan.soft_trigger_tokens;
  const windowPercent = Math.min(100, (projectedTokens / total) * 100);
  const remainingPercent = Math.max(0, 100 - windowPercent);
  const compactPercent = Math.min(
    100,
    compactAt > 0 ? (projectedTokens / compactAt) * 100 : 100,
  );
  const compactPercentText = formatContextPercent(compactPercent);
  elements.contextUsageValue.textContent = (
    `占用约 ${formatTokens(projectedTokens)} / ${formatTokens(total)} · ` +
    `剩余约 ${formatContextPercent(remainingPercent)}`
  );
  elements.contextUsageDetail.textContent = (
    `下一条消息安全可写约 ${formatTokens(hardRemaining)} tokens · ` +
    `占用误差 ± ${formatTokens(plan.fixed_context_error_margin_tokens)} tokens · ` +
    (compactRemaining > 0
      ? `自动整理前还可增加约 ${formatTokens(compactRemaining)} tokens · `
      : "下一条消息将先整理较早内容 · ") +
    `单条消息最多 8,192 个 UTF-8 字节 · ${contextStrategyLabel(plan.projection_strategy)}` +
    (plan.projection_mode === "conservative_tools" ? " · 按可能启用工具的场景计算" : "") +
    (chatOnlyBaseline ? " · 纯对话实测基线；若下一条启用工具，将在提交前重算" : "")
  );
  elements.contextTechnicalValue.textContent = (
    `模型 ${modelId} · 硬输入余量约 ${formatTokens(hardRemaining)} tokens · ` +
    `自动整理阈值占用 ${compactPercentText} · strategy=${plan.projection_strategy} · ` +
    `${plan.count_version}/${plan.count_basis}`
  );
  elements.contextUsageMeter.setAttribute("aria-valuemax", String(total));
  elements.contextUsageMeter.setAttribute(
    "aria-valuenow",
    String(Math.min(projectedTokens, total)),
  );
  elements.contextUsageMeter.setAttribute("aria-valuetext", elements.contextUsageValue.textContent);
  elements.contextUsageFill.style.width = `${windowPercent}%`;
  elements.contextUsage.dataset.level = compactPercent >= 90
    ? "critical"
    : compactPercent >= 75 ? "warning" : "normal";
  elements.contextUsage.title = (
    `服务端 ${plan.count_version} / ${plan.count_basis} 投影；` +
    "固定上下文不包含尚未提交的消息正文，误差边界已从可写余量扣除。"
  );
}

function renderSessionTurnUsage() {
  const usage = state.sessionTurnUsage;
  elements.turnTokenUsageLabel.textContent = usage?.terminalKind
    ? "上一轮模型实际计算量"
    : "本轮模型实际计算量";
  if (!usage || !usage.hasUsage) {
    elements.turnTokenUsageValue.textContent = usage?.terminalKind
      ? (usage.complete ? "未取得模型用量" : "Provider 用量不完整（模型流已提前关闭）")
      : "等待模型响应";
    elements.turnTokenUsageValue.title = (
      "Ollama 在每次模型响应结束时返回实际 input/output token，用量不会按流式文本逐 token 估算。"
    );
    return;
  }
  const total = usage.inputTokens + usage.outputTokens;
  const accounting = usage.terminalKind
    ? (usage.complete ? "已结算" : "部分统计")
    : "响应中";
  elements.turnTokenUsageValue.textContent = (
    `共 ${formatTokens(total)} · 输入 ${formatTokens(usage.inputTokens)} + ` +
    `输出 ${formatTokens(usage.outputTokens)} · ${accounting}`
  );
  elements.turnTokenUsageValue.title = (
    "本轮所有已完成模型调用的实际服务端用量；工具循环会重复计算共享上下文，" +
    "因此这是计算量/成本辅助信息，不是下一轮上下文占用。"
  );
}

function clearSessionContextUsage() {
  state.sessionContextUsage = null;
  state.nextTurnPreview = null;
  state.previewRequest += 1;
  state.sessionTurnUsage = null;
  renderSessionContextUsage();
  renderSessionTurnUsage();
}

function validNextTurnPreview(value, sessionId, modelId) {
  const nullableCount = (item) => item === null || isNonNegativeInteger(item);
  return Boolean(
    value && typeof value === "object" && !Array.isArray(value) &&
    value.version === "next-turn-projection-v1" &&
    value.agent_id === state.agentId && value.conversation_id === sessionId &&
    value.model_id === modelId &&
    ["available", "unavailable"].includes(value.availability) &&
    ["conservative_tools", "chat_only"].includes(value.projection_mode) &&
    typeof value.chat_calibration_available === "boolean" &&
    (value.chat_only_projection === null || validChatOnlyProjection(value.chat_only_projection)) &&
    isNonNegativeInteger(value.conversation_revision) &&
    isNonNegativeInteger(value.operational_context_tokens) &&
    isNonNegativeInteger(value.single_message_byte_limit) &&
    value.single_message_byte_limit === 8192 &&
    nullableCount(value.fixed_context_tokens) &&
    nullableCount(value.fixed_context_error_margin_tokens) &&
    nullableCount(value.safe_user_tokens) &&
    nullableCount(value.compact_before_user_tokens) &&
    nullableCount(value.soft_trigger_tokens) &&
    nullableCount(value.hard_input_tokens) &&
    (
      value.availability !== "available" || [
        value.fixed_context_tokens,
        value.fixed_context_error_margin_tokens,
        value.safe_user_tokens,
        value.compact_before_user_tokens,
        value.soft_trigger_tokens,
        value.hard_input_tokens,
      ].every(isNonNegativeInteger)
    )
  );
}

async function refreshNextTurnPreview(sessionId = state.sessionId) {
  if (!sessionId || !state.selectedModelId) return;
  const modelId = state.selectedModelId;
  const requestNumber = ++state.previewRequest;
  state.nextTurnPreview = null;
  renderSessionContextUsage();
  try {
    const path = agentApiPath(
      `/sessions/${encodeURIComponent(sessionId)}/context-preview`,
    ) + `?model_id=${encodeURIComponent(modelId)}`;
    const response = await api(path);
    const value = await response.json();
    if (
      requestNumber !== state.previewRequest || state.sessionId !== sessionId ||
      state.selectedModelId !== modelId
    ) return;
    if (!validNextTurnPreview(value, sessionId, modelId)) {
      throw new Error("下一轮预算响应格式无效");
    }
    state.nextTurnPreview = value;
  } catch (_error) {
    if (
      requestNumber === state.previewRequest && state.sessionId === sessionId &&
      state.selectedModelId === modelId
    ) {
      state.nextTurnPreview = {
        availability: "unavailable",
        stale_reason: "projection_unavailable",
        model_id: modelId,
        operational_context_tokens: selectedModel()?.operational_context_tokens || null,
      };
    }
  }
  renderSessionContextUsage();
  setRunControls();
}

function captureSessionContextUsage(envelope) {
  if (
    envelope?.kind !== "run.started" || envelope.agent_id !== state.agentId ||
    envelope.conversation_id !== state.sessionId ||
    envelope.run_id !== latestConversationRunId()
  ) {
    return;
  }
  const plan = envelope.payload?.context_plan;
  const modelId = envelope.payload?.model_id || envelope.payload?.model;
  if (
    !plan || typeof plan !== "object" || Array.isArray(plan) ||
    typeof modelId !== "string" || !MODEL_ID_PATTERN.test(modelId) ||
    !Number.isSafeInteger(plan.estimated_input_tokens) || plan.estimated_input_tokens < 0 ||
    !Number.isSafeInteger(plan.operational_context_tokens) ||
    plan.operational_context_tokens < 2048 ||
    !Number.isSafeInteger(plan.input_budget_tokens) ||
    plan.input_budget_tokens < 0 || plan.input_budget_tokens > plan.operational_context_tokens ||
    !Number.isSafeInteger(plan.compact_at_tokens) ||
    plan.compact_at_tokens < 0 || plan.compact_at_tokens > plan.input_budget_tokens ||
    plan.estimated_input_tokens > plan.input_budget_tokens
  ) {
    return;
  }
  state.sessionContextUsage = {
    runId: envelope.run_id,
    modelId,
    usedTokens: plan.estimated_input_tokens,
    totalTokens: plan.operational_context_tokens,
    inputBudgetTokens: plan.input_budget_tokens,
    compactAtTokens: plan.compact_at_tokens,
  };
  state.sessionTurnUsage = {
    runId: envelope.run_id,
    inputTokens: 0,
    outputTokens: 0,
    complete: true,
    hasUsage: false,
    terminalKind: null,
    firstInputTokens: null,
    finalOutputTokens: null,
    providerResponseCount: 0,
    seenEventIds: new Set(),
  };
  renderSessionContextUsage();
  renderSessionTurnUsage();
}

function captureSessionTurnUsage(envelope) {
  if (
    !envelope || envelope.agent_id !== state.agentId ||
    envelope.conversation_id !== state.sessionId ||
    envelope.run_id !== latestConversationRunId() ||
    !["model.response.finished", "run.completed", "run.failed", "run.cancelled"].includes(
      envelope.kind,
    )
  ) {
    return;
  }
  let usage = state.sessionTurnUsage;
  if (!usage || usage.runId !== envelope.run_id) {
    usage = {
      runId: envelope.run_id,
      inputTokens: 0,
      outputTokens: 0,
      complete: true,
      hasUsage: false,
      terminalKind: null,
      firstInputTokens: null,
      finalOutputTokens: null,
      providerResponseCount: 0,
      seenEventIds: new Set(),
    };
    state.sessionTurnUsage = usage;
  }
  if (envelope.kind === "model.response.finished") {
    const payload = envelope.payload;
    const eventIdentity = typeof envelope.event_id === "string"
      ? envelope.event_id
      : `${envelope.run_id}:${envelope.seq}`;
    if (
      usage.seenEventIds.has(eventIdentity) || !payload ||
      !Number.isSafeInteger(payload.input_tokens) || payload.input_tokens < 0 ||
      !Number.isSafeInteger(payload.output_tokens) || payload.output_tokens < 0 ||
      typeof payload.usage_complete !== "boolean"
    ) {
      return;
    }
    usage.seenEventIds.add(eventIdentity);
    usage.inputTokens += payload.input_tokens;
    usage.outputTokens += payload.output_tokens;
    if (!Number.isSafeInteger(usage.firstInputTokens)) {
      usage.firstInputTokens = payload.input_tokens;
    }
    usage.finalOutputTokens = payload.output_tokens;
    usage.providerResponseCount += 1;
    usage.complete = usage.complete && payload.usage_complete;
    usage.hasUsage ||= payload.input_tokens + payload.output_tokens > 0;
  } else {
    const aggregate = envelope.payload?.usage;
    if (
      !aggregate || typeof aggregate !== "object" || Array.isArray(aggregate) ||
      !Number.isSafeInteger(aggregate.input_tokens) || aggregate.input_tokens < 0 ||
      !Number.isSafeInteger(aggregate.output_tokens) || aggregate.output_tokens < 0 ||
      !Number.isSafeInteger(aggregate.last_input_tokens) || aggregate.last_input_tokens < 0 ||
      typeof aggregate.complete !== "boolean"
    ) {
      return;
    }
    usage.inputTokens = aggregate.input_tokens;
    usage.outputTokens = aggregate.output_tokens;
    if (
      usage.providerResponseCount === 0 &&
      aggregate.input_tokens === aggregate.last_input_tokens
    ) {
      // A snapshot-only single-call Run has no intermediate Tool loop, so the
      // aggregate also describes the first request and final answer.
      usage.firstInputTokens = aggregate.last_input_tokens;
      usage.finalOutputTokens = aggregate.output_tokens;
      usage.providerResponseCount = 1;
    }
    usage.complete = aggregate.complete;
    usage.hasUsage = aggregate.input_tokens + aggregate.output_tokens > 0;
    usage.terminalKind = envelope.kind;
  }
  renderSessionContextUsage();
  renderSessionTurnUsage();
}

function sessionIsActive(session) {
  return session && ["active", "running", "deleting"].includes(session.state);
}

function selectedSession() {
  return state.sessions.find((session) => session.session_id === state.sessionId) || null;
}

function selectedAgent() {
  return state.agents.find((agent) => agent.agent_id === state.agentId) || null;
}

function agentApiPath(suffix = "", agentId = state.agentId) {
  if (typeof agentId !== "string" || !AGENT_ID_PATTERN.test(agentId)) {
    throw new Error("当前智能体状态无效，请重新选择");
  }
  return `/api/agents/${encodeURIComponent(agentId)}${suffix}`;
}

function setContextInspectControl() {
  const disabled = (
    state.csrfToken === null || state.selectedTimelineRunId === null ||
    state.contextLoading || state.sessionLoading
  );
  elements.contextInspectButton.disabled = disabled;
  elements.contextInspectButton.textContent = state.contextLoading
    ? "正在读取上下文…"
    : "查看本轮上下文";
  if (elements.eventInspectorContextButton) {
    elements.eventInspectorContextButton.disabled = disabled || state.selectedTimelineEntryKey === null;
  }
}

function clearContextInspector() {
  state.contextRequest += 1;
  state.contextLoading = false;
  state.contextDialogTrigger = null;
  if (elements.contextInspectDialog.open) elements.contextInspectDialog.close();
  elements.contextInspectAvailability.textContent = "";
  elements.contextInspectMetrics.replaceChildren();
  elements.contextSectionList.replaceChildren();
  elements.contextInspectNotice.textContent = "";
  elements.contextInspectJson.textContent = "";
  setContextInspectControl();
}

function setSelectedTimelineRunId(runId, { scrollTurn = false } = {}) {
  const normalized = typeof runId === "string" && RUN_ID_PATTERN.test(runId) ? runId : null;
  if (state.selectedTimelineRunId !== normalized) {
    stopReplay();
    if (state.selectedTimelineRunId && state.selectedTimelineEntryKey) {
      state.selectedTimelineEntryKeyByRun.set(
        state.selectedTimelineRunId,
        state.selectedTimelineEntryKey,
      );
    }
    state.selectedTimelineRunId = normalized;
    state.timelineEntries = normalized
      ? state.timelineEntriesByRun.get(normalized) || []
      : [];
    state.selectedTimelineEntryKey = normalized
      ? state.selectedTimelineEntryKeyByRun.get(normalized) || null
      : null;
    clearContextInspector();
    renderTimelineEntries();
  } else {
    setContextInspectControl();
  }
  syncSelectedTurn(scrollTurn);
}

function setRunControls() {
  const runContext = state.activeRun;
  const preparation = state.preparingRun;
  const preparing = preparation !== null;
  const locked = (
    state.settling || state.sessionLoading || state.mutationPending ||
    preparing || runContext !== null
  );
  const hasSession = state.sessionId !== null;
  const selectedIsActive = sessionIsActive(selectedSession());
  elements.newSessionButton.disabled = state.mutationPending || state.sessionLoading;
  const globalLocked = state.settling || state.mutationPending || hasAgentActivity();
  elements.newAgentName.disabled = globalLocked;
  elements.newAgentButton.disabled = globalLocked;
  elements.researchEnvironmentInstall.disabled = globalLocked || state.researchEnvironment?.installed === true;
  elements.researchEnvironmentDelete.disabled = globalLocked || state.researchEnvironment?.installed !== true;
  // Keep the command line available during an active Run so /status,
  // /permissions and /cancel remain explicit control-plane actions.
  elements.messageInput.disabled = (
    state.sessionLoading || state.mutationPending || preparing || !hasSession
  );
  elements.modelSelect.disabled = locked || state.models.length === 0;
  elements.compactInput.disabled = locked || !hasSession || selectedIsActive;
  const messageOverLimit = renderMessageByteUsage() > 8192;
  const capacityBlocked = state.nextTurnPreview?.stale_reason === (
    "conversation_turn_capacity_exhausted"
  );
  const slashDraft = elements.messageInput.value.trimStart().startsWith("/");
  elements.runButton.disabled = (
    state.sessionLoading || state.mutationPending || preparing || !hasSession || messageOverLimit ||
    (!slashDraft && (capacityBlocked || state.settling)) ||
    (runContext !== null && !slashDraft)
  );
  elements.runButton.textContent = preparing ? "准备中…" : "发送";
  elements.continueSessionButton.hidden = !capacityBlocked;
  elements.continueSessionButton.disabled = locked || !capacityBlocked;
  elements.cancelButton.disabled = preparing
    ? preparation.cancelPending === true
    : runContext === null || runContext.terminalSeen || runContext.cancelPending;
  elements.cancelButton.textContent = preparing
    ? (preparation.cancelPending ? "正在取消…" : "取消准备")
    : "停止";
  elements.timelineRunSelect.disabled = (
    !hasSession || state.timelineRuns.length === 0 || state.sessionLoading || state.mutationPending
  );
  setReplayControls();
  setContextInspectControl();
  for (const button of elements.sessionList.querySelectorAll("button")) {
    const targetSession = button.dataset.sessionId;
    const switchesSession = button.classList.contains("session-select");
    const targetBusy = Boolean(
      targetSession && (
        state.activeRuns.has(targetSession) || state.preparingRuns.has(targetSession)
      )
    );
    const needsIdleTarget = (
      button.classList.contains("session-delete") ||
      button.classList.contains("session-branch")
    );
    const preparationBlocksRename = (
      button.classList.contains("session-rename") &&
      Boolean(targetSession && state.preparingRuns.has(targetSession))
    );
    button.disabled = (
      state.mutationPending || (state.sessionLoading && !switchesSession) ||
      (needsIdleTarget && targetBusy) || preparationBlocksRename
    );
  }
  for (const button of elements.agentList.querySelectorAll("button")) {
    const switching = button.classList.contains("agent-select");
    button.disabled = (
      state.settling || state.mutationPending || button.dataset.agentState !== "active" ||
      (switching && hasUnconfirmedPreparationCancellation()) ||
      (!switching && hasAgentActivity())
    );
  }
}

function clearConversation() {
  if (state.conversationScrollTimer !== null) {
    window.clearTimeout(state.conversationScrollTimer);
    state.conversationScrollTimer = null;
  }
  state.blocks.clear();
  state.liveAssistantContent = null;
  state.liveAssistantMessage = null;
  state.conversationMessages = [];
  state.conversationPage = null;
  state.conversationPageLoading = false;
  state.conversationFollowLatest = true;
  elements.conversationMessages.replaceChildren();
  elements.conversationEmpty.hidden = false;
  updateConversationLatestControl();
  state.subagents = [];
  elements.subagentList.replaceChildren();
  elements.subagentPanel.hidden = true;
}

function clearTimeline(label = "等待运行") {
  stopReplay();
  if (elements.eventDetailDialog.open) elements.eventDetailDialog.close();
  state.timelineEntries = [];
  if (state.selectedTimelineRunId) {
    state.timelineEntriesByRun.set(state.selectedTimelineRunId, state.timelineEntries);
    state.selectedTimelineEntryKeyByRun.delete(state.selectedTimelineRunId);
  }
  state.selectedTimelineEntryKey = null;
  state.eventCount = 0;
  renderTimelineEntries();
  clearEventInspector();
  elements.runId.textContent = label;
}

function clearSelectedSession({ preserveTimeline = false } = {}) {
  stopReplay();
  state.sessionController?.abort();
  state.sessionController = null;
  state.sessionRollback = null;
  state.sessionId = null;
  state.sessionLoading = false;
  state.sessionRequest += 1;
  state.timelineRequest += 1;
  state.timelineRuns = [];
  setSelectedTimelineRunId(null);
  state.timelineEntriesByRun.clear();
  state.selectedTimelineEntryKeyByRun.clear();
  state.timelineEntries = [];
  state.selectedTimelineEntryKey = null;
  state.timelineEntrySerial = 0;
  state.timelineFilter = "all";
  state.followLatest = true;
  renderTimelineRunSelect();
  elements.activeSessionTitle.textContent = "请选择一个会话";
  elements.sessionId.textContent = "未选择";
  clearConversation();
  clearSessionContextUsage();
  if (!preserveTimeline) clearTimeline();
  setRunControls();
}

function abortError() {
  const error = new Error("operation aborted");
  error.name = "AbortError";
  return error;
}

function abortableDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    const completed = () => {
      signal.removeEventListener("abort", aborted);
      resolve();
    };
    const timer = window.setTimeout(completed, milliseconds);
    const aborted = () => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", aborted);
      reject(abortError());
    };
    signal.addEventListener("abort", aborted, { once: true });
  });
}

function setAuthenticated(session) {
  state.csrfToken = session.csrf_token;
  state.agentId = session.agent_id;
  elements.agentId.textContent = session.agent_id;
  elements.activeAgentTitle.textContent = "正在读取智能体…";
  elements.statusDot.classList.add("online");
  setConnectionStatus("控制面已连接");
  setStatus("可以开始对话");
  elements.loginPanel.hidden = true;
  elements.workspace.hidden = false;
  elements.logoutButton.hidden = false;
  setNavigation(!narrowWorkspace());
  setRuntimeInspector(false);
}

function setUnauthenticated(message = "尚未认证") {
  detachAgentBrowserActivity();
  clearCopyFeedbackTimers();
  state.authRequest += 1;
  state.agentEpoch += 1;
  state.sessionListRequest += 1;
  state.permissionRequest += 1;
  state.csrfToken = null;
  state.agentId = null;
  state.agents = [];
  state.researchEnvironment = null;
  state.models = [];
  state.commands = [];
  state.defaultModelId = null;
  state.selectedModelId = null;
  state.sessions = [];
  state.sessionDrafts.clear();
  state.settling = false;
  state.mutationPending = false;
  state.sessionLoading = false;
  state.sessionRequest += 1;
  elements.statusDot.classList.remove("online");
  setConnectionStatus(message);
  setStatus("");
  elements.loginPanel.hidden = false;
  elements.workspace.hidden = true;
  setRuntimeInspector(false);
  elements.workspace.classList.remove("navigation-open");
  elements.logoutButton.hidden = true;
  elements.sessionList.replaceChildren();
  elements.agentList.replaceChildren();
  elements.agentListStatus.textContent = "";
  elements.agentEmpty.hidden = true;
  elements.activeAgentTitle.textContent = "未选择智能体";
  elements.agentId.textContent = "未选择";
  elements.researchEnvironmentStatus.textContent = "未连接";
  elements.researchEnvironmentPackages.textContent = "";
  elements.modelSelect.replaceChildren(new Option("需要先登录", ""));
  elements.commandHelpList.replaceChildren();
  elements.commandResultTitle.textContent = "";
  elements.commandResultJson.textContent = "";
  elements.commandResult.hidden = true;
  elements.permissionPanel.hidden = true;
  elements.permissionList.replaceChildren();
  elements.sessionListStatus.textContent = "";
  elements.messageInput.value = "";
  elements.compactInput.checked = false;
  resizeComposer();
  renderMessageByteUsage();
  clearSelectedSession();
}

async function api(path, options = {}) {
  let response;
  const requestAuth = state.authRequest;
  const requestCsrf = state.csrfToken;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(state.csrfToken ? { "X-CSRF-Token": state.csrfToken } : {}),
        ...(options.headers || {}),
      },
    });
  } catch (cause) {
    if (cause?.name === "AbortError") throw cause;
    const error = new Error("无法连接本地服务，请检查服务状态后重试");
    error.code = "network_unavailable";
    throw error;
  }
  // A response belongs to the browser authentication generation that sent it.
  // Discard both successful and failed late responses after logout/re-login so
  // an old mutation cannot alter the newly authenticated surface.
  if (!authScopeIsCurrent(requestAuth, requestCsrf)) throw abortError();
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (!authScopeIsCurrent(requestAuth, requestCsrf)) throw abortError();
    const detailObject = body && typeof body.detail === "object" ? body.detail : null;
    const errorCode = typeof detailObject?.code === "string" ? detailObject.code : null;
    const safeMessage = API_ERROR_LABELS[errorCode] || HTTP_ERROR_LABELS[response.status] ||
      "请求未能完成，请稍后重试";
    const error = new Error(safeMessage);
    error.status = response.status;
    error.code = errorCode;
    if (
      response.status === 401 && state.csrfToken !== null &&
      requestAuth === state.authRequest && requestCsrf === state.csrfToken
    ) {
      setUnauthenticated("登录已过期，请重新认证");
    }
    throw error;
  }
  // Fetch resolves after response headers. Wrap body readers with the same
  // generation fence so a delayed JSON body cannot cross logout/re-login.
  const guardedReaders = new Set(["json", "text", "arrayBuffer", "blob", "formData"]);
  return new Proxy(response, {
    get(target, property) {
      const value = Reflect.get(target, property, target);
      if (guardedReaders.has(property) && typeof value === "function") {
        return async (...arguments_) => {
          const body = await value.apply(target, arguments_);
          if (!authScopeIsCurrent(requestAuth, requestCsrf)) throw abortError();
          return body;
        };
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function normalizeAgent(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value) ||
    typeof value.agent_id !== "string" || !AGENT_ID_PATTERN.test(value.agent_id) ||
    typeof value.display_name !== "string" || !value.display_name.trim() ||
    new TextEncoder().encode(value.display_name).length > 128 ||
    !Number.isSafeInteger(value.generation) || value.generation < 1 ||
    !["provisioning", "active", "renaming", "upgrading", "deleting"].includes(value.state) ||
    typeof value.created_at !== "string" || typeof value.updated_at !== "string"
  ) {
    return null;
  }
  return value;
}

function setAgentIdentity(agent) {
  state.agentId = agent.agent_id;
  elements.agentId.textContent = agent.agent_id;
  elements.activeAgentTitle.textContent = agent.display_name;
}

function renderAgentList() {
  elements.agentList.replaceChildren();
  elements.agentEmpty.hidden = state.agents.length !== 0;
  for (const agent of state.agents) {
    const system = agent.agent_id === SYSTEM_AGENT_ID;
    const item = document.createElement("li");
    item.className = "agent-item";
    item.classList.toggle("active", agent.agent_id === state.agentId);

    const heading = document.createElement("div");
    heading.className = "agent-item-heading";
    const name = document.createElement("strong");
    name.textContent = agent.display_name;
    const badges = document.createElement("span");
    badges.className = "agent-badges";
    if (system) {
      const badge = document.createElement("span");
      badge.className = "agent-badge system";
      badge.textContent = "系统智能体";
      badges.append(badge);
    }
    const stateBadge = document.createElement("span");
    stateBadge.className = "agent-badge";
    stateBadge.textContent = AGENT_STATE_LABELS[agent.state] || "状态未知";
    badges.append(stateBadge);
    heading.append(name, badges);

    const metadata = document.createElement("div");
    metadata.className = "agent-meta";
    const identity = document.createElement("span");
    identity.textContent = agent.agent_id;
    const generation = document.createElement("span");
    generation.textContent = `运行环境 v${agent.generation}`;
    metadata.append(identity, generation);

    const actions = document.createElement("div");
    actions.className = "agent-item-actions";
    if (agent.agent_id !== state.agentId) {
      const select = document.createElement("button");
      select.type = "button";
      select.className = "quiet agent-select";
      select.textContent = "切换";
      select.dataset.agentState = agent.state;
      select.addEventListener("click", () => void switchAgent(agent.agent_id));
      actions.append(select);
    }
    if (!system) {
      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "quiet agent-rename";
      rename.textContent = "重命名";
      rename.dataset.agentState = agent.state;
      rename.addEventListener("click", () => void renameAgent(agent));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger agent-delete";
      remove.textContent = "删除";
      remove.dataset.agentState = agent.state;
      remove.addEventListener("click", () => void deleteAgent(agent));
      actions.append(rename, remove);

      const advanced = document.createElement("details");
      advanced.className = "agent-advanced";
      const advancedSummary = document.createElement("summary");
      advancedSummary.textContent = "高级操作";
      const advancedCopy = document.createElement("p");
      advancedCopy.textContent = (
        "仅在智能体指令、基础运行环境或安全策略发生实质变化时重建运行环境。"
      );
      const upgrade = document.createElement("button");
      upgrade.type = "button";
      upgrade.className = "quiet agent-upgrade";
      upgrade.textContent = "重建运行环境";
      upgrade.dataset.agentState = agent.state;
      upgrade.addEventListener("click", () => void upgradeAgent(agent));
      advanced.append(advancedSummary, advancedCopy, upgrade);
      item.append(heading, metadata, actions, advanced);
    } else {
      item.append(heading, metadata, actions);
    }
    elements.agentList.append(item);
  }
  setRunControls();
}

async function refreshAgents(
  preferredAgentId = state.agentId,
  expectedEpoch = state.agentEpoch,
) {
  elements.agentListStatus.textContent = "正在读取智能体…";
  const response = await api("/api/agents");
  const body = await response.json();
  if (!body || !Array.isArray(body.agents) || body.agents.length > 100) {
    throw new Error("智能体列表响应无效，请刷新后重试");
  }
  const agents = body.agents.map(normalizeAgent).filter((item) => item !== null);
  if (agents.length !== body.agents.length || new Set(
    agents.map((agent) => agent.agent_id),
  ).size !== agents.length) {
    throw new Error("智能体列表内容无效，请刷新后重试");
  }
  if (expectedEpoch !== state.agentEpoch) throw abortError();
  state.agents = agents;
  const target = agents.find((agent) => (
    agent.agent_id === preferredAgentId && agent.state === "active"
  )) || agents.find((agent) => (
    agent.agent_id === SYSTEM_AGENT_ID && agent.state === "active"
  )) || agents.find((agent) => agent.state === "active") || null;
  if (!target) throw new Error("当前没有可用的智能体");
  setAgentIdentity(target);
  elements.agentListStatus.textContent = `${agents.length} 个智能体`;
  renderAgentList();
  return target;
}

async function loadAgentSurface(preferredAgentId, successMessage) {
  const agentEpoch = state.agentEpoch + 1;
  state.agentEpoch = agentEpoch;
  state.sessionListRequest += 1;
  state.permissionRequest += 1;
  clearSelectedSession();
  state.sessions = [];
  state.researchEnvironment = null;
  state.commands = [];
  elements.messageInput.value = "";
  elements.compactInput.checked = false;
  state.selectedModelId = state.defaultModelId;
  if (state.selectedModelId) elements.modelSelect.value = state.selectedModelId;
  resizeComposer();
  renderMessageByteUsage();
  elements.activeAgentTitle.textContent = "正在切换智能体…";
  elements.agentId.textContent = "正在切换…";
  elements.commandHelpList.replaceChildren();
  elements.commandResultTitle.textContent = "";
  elements.commandResultJson.textContent = "";
  elements.commandResult.hidden = true;
  renderResearchEnvironment();
  renderSessionList();
  renderPermissions([]);
  await refreshAgents(preferredAgentId, agentEpoch);
  if (agentEpoch !== state.agentEpoch) throw abortError();
  const agentId = state.agentId;
  await refreshResearchEnvironment(agentId, agentEpoch);
  await refreshCommands(agentId, agentEpoch);
  await refreshSessions(null, agentId, agentEpoch);
  if (!agentScopeIsCurrent(agentId, agentEpoch)) throw abortError();
  if (successMessage) setStatus(successMessage);
}

function normalizeResearchEnvironment(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value) ||
    value.agent_id !== state.agentId || typeof value.installed !== "boolean" ||
    (value.installed && (!value.environment || typeof value.environment !== "object" ||
      !Array.isArray(value.environment.requirements) ||
      value.environment.requirements.length < 1 || value.environment.requirements.length > 16 ||
      value.environment.requirements.some((item) => typeof item !== "string" || item.length > 128))) ||
    (!value.installed && value.environment !== null)
  ) return null;
  return value;
}

function renderResearchEnvironment() {
  const value = state.researchEnvironment;
  if (value?.installed) {
    elements.researchEnvironmentStatus.textContent = "已安装 / 可复用";
    elements.researchEnvironmentPackages.textContent = value.environment.requirements.join(" · ");
  } else {
    elements.researchEnvironmentStatus.textContent = value ? "未安装" : "状态未知";
    elements.researchEnvironmentPackages.textContent = value
      ? "安装后可提取文档文字；运行时断网，且只能读取当前智能体的文档。"
      : "";
  }
  setRunControls();
}

async function refreshResearchEnvironment(
  agentId = state.agentId,
  agentEpoch = state.agentEpoch,
) {
  state.researchEnvironment = null;
  renderResearchEnvironment();
  const response = await api(agentApiPath("/research-environment", agentId));
  const payload = await response.json();
  if (!agentScopeIsCurrent(agentId, agentEpoch)) throw abortError();
  const value = normalizeResearchEnvironment(payload);
  if (!value || value.agent_id !== agentId) throw new Error("研究环境响应格式无效");
  state.researchEnvironment = value;
  renderResearchEnvironment();
}

async function installResearchEnvironment() {
  if (state.mutationPending || hasAgentActivity() || state.researchEnvironment?.installed) return;
  const mutationScope = beginMutation();
  setStatus("正在当前智能体内安装固定版本研究依赖…", "preparation");
  try {
    await api(agentApiPath("/research-environment"), {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refreshResearchEnvironment();
    await refreshCommands();
    setStatus("研究环境已安装；后续会话会直接复用，不会重复下载");
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
}

async function deleteResearchEnvironment() {
  if (state.mutationPending || hasAgentActivity() || !state.researchEnvironment?.installed) return;
  if (!window.confirm("删除当前智能体的研究依赖环境吗？工作区文档与历史会话不会删除。")) return;
  const mutationScope = beginMutation();
  try {
    await api(agentApiPath("/research-environment"), { method: "DELETE" });
    await refreshResearchEnvironment();
    await refreshCommands();
    setStatus("当前智能体的研究依赖环境已彻底清理");
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
}

async function switchAgent(agentId) {
  if (
    !AGENT_ID_PATTERN.test(agentId) || agentId === state.agentId ||
    state.settling || state.mutationPending || hasUnconfirmedPreparationCancellation()
  ) {
    return;
  }
  const previousAgentId = state.agentId;
  saveSelectedDraft();
  // Agent switching changes every conversation-scoped authority boundary.
  // Close old browser transports and discard their transient projections; the
  // server Run remains authoritative and is recovered from SQLite/SSE when the
  // operator returns to that Agent.
  detachAgentBrowserActivity();
  const mutationScope = beginMutation();
  try {
    await loadAgentSurface(agentId, null);
    setStatus(`已切换到 ${selectedAgent()?.display_name || "智能体"}`);
    elements.agentDrawer.open = false;
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope) && previousAgentId) {
      await loadAgentSurface(previousAgentId, null).catch(() => null);
      setStatus(`智能体切换失败：${error.message}`, "failure");
    }
  } finally {
    finishMutation(mutationScope);
  }
}

function validAgentDisplayName(value) {
  return typeof value === "string" && value.trim() &&
    new TextEncoder().encode(value).length <= 128;
}

async function createAgent(displayName) {
  if (!validAgentDisplayName(displayName)) {
    setStatus("智能体名称必须为 1–128 个 UTF-8 字节");
    return;
  }
  if (state.settling || state.mutationPending || hasAgentActivity()) return;
  const mutationScope = beginMutation();
  try {
    const response = await api("/api/agents", {
      method: "POST",
      body: JSON.stringify({ display_name: displayName.trim() }),
    });
    const agent = normalizeAgent(await response.json());
    if (!agent || agent.state !== "active") throw new Error("新建智能体响应无效，请重试");
    elements.newAgentName.value = "";
    await loadAgentSurface(agent.agent_id, `智能体“${agent.display_name}”已创建并切换`);
    elements.agentDrawer.open = false;
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
}

async function renameAgent(agent) {
  if (
    agent.agent_id === SYSTEM_AGENT_ID || agent.state !== "active" ||
    state.settling || state.mutationPending || hasAgentActivity()
  ) {
    return;
  }
  const displayName = window.prompt("输入新的智能体名称", agent.display_name);
  if (displayName === null) return;
  const normalizedName = displayName.trim();
  if (!validAgentDisplayName(normalizedName)) {
    setStatus("智能体名称必须为 1–128 个 UTF-8 字节");
    return;
  }
  if (normalizedName === agent.display_name) {
    setStatus("智能体名称没有变化");
    return;
  }
  const mutationScope = beginMutation();
  try {
    const response = await api(
      `/api/agents/${encodeURIComponent(agent.agent_id)}`,
      { method: "PATCH", body: JSON.stringify({ display_name: normalizedName }) },
    );
    const renamed = normalizeAgent(await response.json());
    if (!renamed || renamed.state !== "active") throw new Error("智能体重命名响应无效，请重试");
    if (renamed.generation !== agent.generation) {
      throw new Error("重命名意外改变了运行环境，请刷新后检查");
    }
    state.agents = state.agents.map((item) => (
      item.agent_id === renamed.agent_id ? renamed : item
    ));
    if (renamed.agent_id === state.agentId) setAgentIdentity(renamed);
    renderAgentList();
    setStatus(`智能体已重命名为“${renamed.display_name}”；运行环境未重建`);
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
}

async function upgradeAgent(agent) {
  if (
    agent.agent_id === SYSTEM_AGENT_ID || agent.state !== "active" ||
    state.settling || state.mutationPending || hasAgentActivity()
  ) {
    return;
  }
  const nextGeneration = agent.generation + 1;
  if (!window.confirm(
    `确定重建“${agent.display_name}”的运行环境吗？\n\n` +
    `运行代将从 ${agent.generation} 升至 ${nextGeneration}。该操作会排空当前运行时并重建 ` +
    "隔离运行环境；会话、工作区、已安装能力和持久依赖会保留。普通重命名不需要执行此操作。",
  )) return;
  const mutationScope = beginMutation();
  try {
    const response = await api(
      `/api/agents/${encodeURIComponent(agent.agent_id)}/upgrade`,
      { method: "POST", body: JSON.stringify({}) },
    );
    const upgraded = normalizeAgent(await response.json());
    if (!upgraded || upgraded.state !== "active") throw new Error("运行环境重建响应无效，请重试");
    if (agent.agent_id === state.agentId) {
      await loadAgentSurface(
        state.agentId,
        `智能体运行环境已重建为第 ${upgraded.generation} 版`,
      );
    } else {
      await refreshAgents(state.agentId);
      setStatus(`智能体运行环境已重建为第 ${upgraded.generation} 版`);
    }
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
}

async function deleteAgent(agent) {
  if (
    agent.agent_id === SYSTEM_AGENT_ID || agent.state !== "active" ||
    state.settling || state.mutationPending || hasAgentActivity()
  ) {
    return;
  }
  if (!window.confirm(
    `确定删除智能体“${agent.display_name}”吗？其会话、已安装能力、后台任务、环境和沙箱数据都会清除，且不可撤销。`,
  )) return;
  const deletingSelected = agent.agent_id === state.agentId;
  const mutationScope = beginMutation();
  try {
    await api(`/api/agents/${encodeURIComponent(agent.agent_id)}`, { method: "DELETE" });
    purgeAgentDrafts(agent.agent_id);
    if (deletingSelected) {
      await loadAgentSurface(
        SYSTEM_AGENT_ID,
        `智能体“${agent.display_name}”已删除且残留已清理`,
      );
    } else {
      await refreshAgents(state.agentId);
      setStatus(`智能体“${agent.display_name}”已删除且残留已清理`);
    }
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) {
      await refreshAgents(state.agentId).catch(() => null);
      setStatus(error.message);
    }
  } finally {
    finishMutation(mutationScope);
  }
}

async function refreshCommands(
  agentId = state.agentId,
  agentEpoch = state.agentEpoch,
) {
  const response = await api(agentApiPath("/commands", agentId));
  const body = await response.json();
  if (
    !body || body.schema_version !== 1 || !Array.isArray(body.commands) ||
    body.commands.length < 1 || body.commands.length > 32
  ) {
    throw new Error("快捷命令目录响应无效，请刷新后重试");
  }
  if (!agentScopeIsCurrent(agentId, agentEpoch)) throw abortError();
  state.commands = body.commands;
  elements.commandHelpList.replaceChildren();
  for (const command of body.commands) {
    if (
      !command || typeof command.command_id !== "string" ||
      typeof command.name !== "string" || !command.name.startsWith("/") ||
      typeof command.description !== "string" ||
      typeof command.argument_schema !== "string"
    ) {
      throw new Error("快捷命令内容无效，请刷新后重试");
    }
    const item = document.createElement("li");
    const name = document.createElement("code");
    name.textContent = `${command.name}${command.argument_schema ? ` ${command.argument_schema}` : ""}`;
    item.append(name, document.createTextNode(` — ${command.description}`));
    elements.commandHelpList.append(item);
  }
}

function showCommandResult(value) {
  elements.commandResultTitle.textContent = `/${value.command_id || "command"} 结果`;
  elements.commandResultJson.textContent = JSON.stringify(value, null, 2);
  elements.commandResult.hidden = false;
}

function renderPermissions(permissions) {
  const permissionAgentId = state.agentId;
  const permissionAgentEpoch = state.agentEpoch;
  const permissionSessionId = state.sessionId;
  const permissionGeneration = state.permissionRequest;
  elements.permissionList.replaceChildren();
  const safe = Array.isArray(permissions) ? permissions.filter((item) => (
    item && typeof item === "object" && RUN_ID_PATTERN.test(item.permission_id) &&
    RUN_ID_PATTERN.test(item.run_id) && typeof item.capability_id === "string" &&
    typeof item.preview === "string" && item.status === "pending"
  )).slice(0, 6) : [];
  elements.permissionPanel.hidden = safe.length === 0;
  for (const permission of safe) {
    const card = document.createElement("article");
    card.className = "permission-card";
    const heading = document.createElement("div");
    heading.className = "permission-heading";
    const capability = document.createElement("strong");
    capability.textContent = permission.capability_id;
    const run = document.createElement("code");
    run.textContent = permission.run_id.slice(0, 8);
    heading.append(capability, run);
    const preview = document.createElement("pre");
    preview.className = "permission-preview";
    preview.textContent = permission.preview;
    const actions = document.createElement("div");
    actions.className = "permission-actions";
    for (const decision of ["deny", "approve"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = decision === "deny" ? "danger" : "quiet";
      button.textContent = decision === "deny" ? "拒绝" : "批准";
      button.addEventListener("click", async () => {
        const permissionScopeIsCurrent = () => (
          agentScopeIsCurrent(permissionAgentId, permissionAgentEpoch) &&
          state.sessionId === permissionSessionId &&
          state.permissionRequest === permissionGeneration &&
          state.activeRuns.get(permissionSessionId)?.runId === permission.run_id
        );
        if (!permissionScopeIsCurrent()) return;
        for (const control of actions.querySelectorAll("button")) control.disabled = true;
        try {
          await api(
            `/api/agents/${encodeURIComponent(permissionAgentId)}/permissions/${encodeURIComponent(permission.permission_id)}`,
            { method: "POST", body: JSON.stringify({ decision }) },
          );
          if (!permissionScopeIsCurrent()) return;
          setStatus(decision === "approve" ? "能力已批准一次" : "能力已拒绝");
          await refreshPendingPermissions(permission.run_id);
        } catch (error) {
          if (!permissionScopeIsCurrent()) return;
          setStatus(error.message);
          for (const control of actions.querySelectorAll("button")) control.disabled = false;
        }
      });
      actions.append(button);
    }
    card.append(heading, preview, actions);
    elements.permissionList.append(card);
  }
}

function stopPermissionPoll(runContext) {
  if (!runContext) return;
  if (runContext.permissionPollTimer !== null && runContext.permissionPollTimer !== undefined) {
    window.clearTimeout(runContext.permissionPollTimer);
    runContext.permissionPollTimer = null;
  }
  const wake = runContext.permissionPollWake;
  runContext.permissionPollWake = null;
  if (typeof wake === "function") wake(false);
}

function waitForPermissionPoll(runContext, delayMs) {
  return new Promise((resolve) => {
    if (!runIsTracked(runContext) || runContext.terminalSeen) {
      resolve(false);
      return;
    }
    const finish = (ready) => {
      if (runContext.permissionPollTimer !== null) {
        window.clearTimeout(runContext.permissionPollTimer);
        runContext.permissionPollTimer = null;
      }
      if (runContext.permissionPollWake === finish) {
        runContext.permissionPollWake = null;
      }
      resolve(ready);
    };
    runContext.permissionPollWake = finish;
    runContext.permissionPollTimer = window.setTimeout(() => finish(true), delayMs);
  });
}

async function refreshPendingPermissions(runId = null) {
  const agentId = state.agentId;
  const agentEpoch = state.agentEpoch;
  if (!agentId) return false;
  const requestNumber = ++state.permissionRequest;
  const response = await api(`/api/agents/${encodeURIComponent(agentId)}/permissions`);
  const body = await response.json();
  if (
    !agentScopeIsCurrent(agentId, agentEpoch) ||
    requestNumber !== state.permissionRequest ||
    (runId !== null && state.activeRun?.runId !== runId)
  ) return false;
  const permissions = Array.isArray(body.permissions)
    ? body.permissions.filter((item) => runId === null || item.run_id === runId)
    : [];
  renderPermissions(permissions);
  return true;
}

async function pollPendingPermissions(runContext) {
  if (runContext.permissionPollPromise) {
    await runContext.permissionPollPromise;
    return;
  }
  const poller = (async () => {
    while (runIsTracked(runContext) && !runContext.terminalSeen && state.csrfToken !== null) {
      if (state.sessionId === runContext.sessionId) {
        await refreshPendingPermissions(runContext.runId).catch(() => null);
      }
      if (!await waitForPermissionPoll(runContext, 750)) break;
    }
    if (runIsTracked(runContext) && state.sessionId === runContext.sessionId) {
      state.permissionRequest += 1;
      renderPermissions([]);
    }
  })();
  runContext.permissionPollPromise = poller;
  try {
    await poller;
  } finally {
    stopPermissionPoll(runContext);
    if (runContext.permissionPollPromise === poller) {
      runContext.permissionPollPromise = null;
    }
  }
}

async function executeSlashCommand(sessionId, command) {
  const commandAuthRequest = state.authRequest;
  const commandCsrfToken = state.csrfToken;
  const commandAgentId = state.agentId;
  const commandAgentEpoch = state.agentEpoch;
  const commandSessionRequest = state.sessionRequest;
  const commandScopeIsCurrent = ({ allowOwnRefresh = false } = {}) => (
    authScopeIsCurrent(commandAuthRequest, commandCsrfToken) &&
    agentScopeIsCurrent(commandAgentId, commandAgentEpoch) &&
    (allowOwnRefresh || (
      state.sessionId === sessionId && state.sessionRequest === commandSessionRequest
    ))
  );
  const response = await api(agentApiPath(
    `/sessions/${encodeURIComponent(sessionId)}/commands`,
    commandAgentId,
  ), {
    method: "POST",
    body: JSON.stringify({ command }),
  });
  const value = await response.json();
  if (!commandScopeIsCurrent()) throw abortError();
  if (
    !value || value.schema_version !== 1 || value.kind !== "slash_command_result" ||
    value.model_invoked !== false || value.turn_created !== false ||
    typeof value.command_id !== "string" || typeof value.result !== "object" ||
    typeof value.ui_effect !== "object"
  ) {
    throw new Error("快捷命令响应无效，请重试");
  }
  showCommandResult(value);
  if (value.command_id === "permissions") renderPermissions(value.result.permissions);
  if (value.ui_effect.next_turn_model_id) {
    state.selectedModelId = value.ui_effect.next_turn_model_id;
    elements.modelSelect.value = state.selectedModelId;
  }
  if (value.ui_effect.compact_next_turn === true) elements.compactInput.checked = true;
  elements.messageInput.value = "";
  saveSelectedDraft();
  resizeComposer();
  if (value.ui_effect.conversation_deleted === true) {
    const deletedDraftKey = conversationStateKey(sessionId, commandAgentId);
    if (deletedDraftKey) state.sessionDrafts.delete(deletedDraftKey);
    clearSelectedSession();
    await refreshSessions(null, commandAgentId, commandAgentEpoch);
  } else {
    await refreshSessions(sessionId, commandAgentId, commandAgentEpoch);
  }
  if (!commandScopeIsCurrent({ allowOwnRefresh: true })) throw abortError();
  setStatus(`/${value.command_id} 已完成；未调用模型，也未创建对话轮次`);
}

async function refreshModels() {
  const authRequest = state.authRequest;
  const csrfToken = state.csrfToken;
  const response = await api("/api/models");
  const body = await response.json();
  if (
    !body || typeof body.default_model_id !== "string" ||
    !MODEL_ID_PATTERN.test(body.default_model_id) || !Array.isArray(body.models) ||
    body.models.length < 1 || body.models.length > 16
  ) {
    throw new Error("受信模型目录格式无效");
  }
  const models = body.models.map((item) => {
    if (
      !item || typeof item !== "object" || typeof item.model_id !== "string" ||
      !MODEL_ID_PATTERN.test(item.model_id) || typeof item.provider_model !== "string" ||
      !Number.isSafeInteger(item.operational_context_tokens) ||
      item.operational_context_tokens < 2048 ||
      typeof item.profile_digest !== "string" || !/^[a-f0-9]{64}$/.test(item.profile_digest)
    ) {
      throw new Error("受信模型目录条目无效");
    }
    return item;
  });
  if (!models.some((item) => item.model_id === body.default_model_id)) {
    throw new Error("受信默认模型不存在");
  }
  if (
    authRequest !== state.authRequest || csrfToken === null ||
    csrfToken !== state.csrfToken
  ) throw abortError();
  state.models = models;
  state.defaultModelId = body.default_model_id;
  if (!models.some((item) => item.model_id === state.selectedModelId)) {
    state.selectedModelId = body.default_model_id;
  }
  elements.modelSelect.replaceChildren();
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.model_id;
    option.textContent = `${model.model_id} · ${formatTokens(model.operational_context_tokens)} token 窗口${
      model.supports_tools ? " · 支持工具" : " · 纯文本"
    }`;
    elements.modelSelect.append(option);
  }
  elements.modelSelect.value = state.selectedModelId;
  renderSessionContextUsage();
  setRunControls();
}

function normalizeSession(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value) ||
    typeof value.session_id !== "string" || !RUN_ID_PATTERN.test(value.session_id) ||
    !Number.isSafeInteger(value.revision) || value.revision < 0
  ) {
    return null;
  }
  return {
    session_id: value.session_id,
    revision: value.revision,
    title: typeof value.title === "string" && value.title.trim() ? value.title : "未命名会话",
    created_at: typeof value.created_at === "string" ? value.created_at : "",
    updated_at: typeof value.updated_at === "string" ? value.updated_at : "",
    message_count: Number.isSafeInteger(value.message_count) && value.message_count >= 0
      ? value.message_count
      : 0,
    state: typeof value.state === "string" ? value.state : "idle",
  };
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function positionSessionMenuPopover(trigger, popover) {
  const triggerRect = trigger.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  const viewport = window.visualViewport;
  const viewportLeft = viewport?.offsetLeft || 0;
  const viewportTop = viewport?.offsetTop || 0;
  const viewportRight = viewportLeft + (viewport?.width || window.innerWidth);
  const viewportBottom = viewportTop + (viewport?.height || window.innerHeight);
  const edge = 8;
  const gap = 4;
  const width = popoverRect.width;
  const height = popoverRect.height;
  const roomBelow = viewportBottom - triggerRect.bottom - gap - edge;
  const roomAbove = triggerRect.top - viewportTop - gap - edge;
  const openAbove = roomBelow < height && roomAbove > roomBelow;
  const desiredTop = openAbove
    ? triggerRect.top - height - gap
    : triggerRect.bottom + gap;
  const top = Math.min(
    Math.max(viewportTop + edge, desiredTop),
    Math.max(viewportTop + edge, viewportBottom - height - edge),
  );
  const left = Math.min(
    Math.max(viewportLeft + edge, triggerRect.right - width),
    Math.max(viewportLeft + edge, viewportRight - width - edge),
  );
  popover.style.top = `${top}px`;
  popover.style.left = `${left}px`;
}

function closeSessionMenuPopover(popover) {
  if (typeof popover.hidePopover === "function" && popover.matches(":popover-open")) {
    popover.hidePopover();
  } else {
    popover.hidden = true;
  }
}

function renderSessionList() {
  elements.sessionList.replaceChildren();
  elements.sessionEmpty.hidden = state.sessions.length !== 0;
  for (const session of state.sessions) {
    const item = document.createElement("li");
    item.className = "session-item";
    if (session.session_id === state.sessionId) item.classList.add("active");

    const selectButton = document.createElement("button");
    selectButton.type = "button";
    selectButton.className = "session-select";
    selectButton.dataset.sessionId = session.session_id;
    selectButton.setAttribute("aria-pressed", String(session.session_id === state.sessionId));
    selectButton.setAttribute("aria-label", `恢复会话：${session.title}`);

    const title = document.createElement("strong");
    title.textContent = session.title;
    const meta = document.createElement("span");
    const updated = formatTime(session.updated_at);
    meta.textContent = `${session.message_count} 条消息${updated ? ` · ${updated}` : ""}`;
    const marker = document.createElement("span");
    marker.className = "session-state";
    const tracked = state.activeRuns.has(session.session_id);
    const preparing = state.preparingRuns.has(session.session_id);
    const completedInBackground = state.backgroundCompletions.get(session.session_id);
    if (preparing) marker.textContent = "正在准备";
    else if (tracked) {
      marker.textContent = session.session_id === state.sessionId ? "运行中" : "后台运行中";
    } else if (completedInBackground === "run.completed") marker.textContent = "后台已完成";
    else if (completedInBackground === "run.cancelled") marker.textContent = "后台已取消";
    else if (completedInBackground) marker.textContent = "后台运行失败";
    else marker.textContent = ({ idle: "空闲", running: "运行中" }[session.state] || session.state);
    selectButton.append(title, meta, marker);
    selectButton.addEventListener("click", () => {
      void selectSession(session.session_id);
      closeNavigationOnNarrowScreen();
    });

    const menu = document.createElement("div");
    menu.className = "session-menu";
    const menuTrigger = document.createElement("button");
    menuTrigger.type = "button";
    menuTrigger.className = "quiet session-menu-trigger";
    menuTrigger.textContent = "•••";
    menuTrigger.setAttribute("aria-label", `会话操作：${session.title}`);
    const menuBody = document.createElement("div");
    menuBody.className = "session-menu-popover";
    const popoverId = `session-menu-${session.session_id}`;
    menuBody.id = popoverId;
    if (typeof menuBody.showPopover === "function") {
      menuBody.setAttribute("popover", "auto");
      menuTrigger.setAttribute("popovertarget", popoverId);
      menuTrigger.setAttribute("popovertargetaction", "toggle");
      menuBody.addEventListener("toggle", (event) => {
        if (event.newState === "open") {
          positionSessionMenuPopover(menuTrigger, menuBody);
        }
      });
    } else {
      menuBody.hidden = true;
      menuTrigger.setAttribute("aria-expanded", "false");
      menuTrigger.addEventListener("click", () => {
        menuBody.hidden = !menuBody.hidden;
        menuTrigger.setAttribute("aria-expanded", String(!menuBody.hidden));
        if (!menuBody.hidden) positionSessionMenuPopover(menuTrigger, menuBody);
      });
    }

    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.className = "quiet session-rename";
    renameButton.textContent = "重命名";
    renameButton.dataset.sessionId = session.session_id;
    renameButton.dataset.sessionAction = "rename";
    renameButton.addEventListener("click", () => {
      closeSessionMenuPopover(menuBody);
      void renameSession(session);
    });

    const branchButton = document.createElement("button");
    branchButton.type = "button";
    branchButton.className = "quiet session-branch";
    branchButton.textContent = "从当前结尾分支";
    branchButton.dataset.sessionId = session.session_id;
    branchButton.dataset.sessionAction = "branch";
    branchButton.addEventListener("click", () => {
      closeSessionMenuPopover(menuBody);
      void branchSession(session.session_id);
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "session-delete danger";
    deleteButton.textContent = "删除";
    deleteButton.dataset.sessionId = session.session_id;
    deleteButton.dataset.sessionActive = String(sessionIsActive(session));
    deleteButton.setAttribute("aria-label", `删除会话：${session.title}`);
    deleteButton.addEventListener("click", () => {
      closeSessionMenuPopover(menuBody);
      void deleteSession(session);
    });

    menuBody.append(renameButton, branchButton, deleteButton);
    menu.append(menuTrigger, menuBody);
    item.append(selectButton, menu);
    elements.sessionList.append(item);
  }
  setRunControls();
}

function safeMarkdownUrl(raw) {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 2048) return null;
  try {
    const parsed = new URL(raw, window.location.origin);
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    if (parsed.username || parsed.password) return null;
    return parsed.href;
  } catch (_error) {
    return null;
  }
}

function appendRestrictedInline(parent, source) {
  const pattern = /(`[^`\n]{1,512}`|\*\*[^*\n]{1,512}\*\*|\*[^*\n]{1,512}\*|\[[^\]\n]{1,256}\]\([^\s)\n]{1,2048}\))/g;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    const index = match.index || 0;
    if (index > cursor) parent.append(document.createTextNode(source.slice(cursor, index)));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      parent.append(emphasis);
    } else {
      const split = token.lastIndexOf("](");
      const label = token.slice(1, split);
      const href = safeMarkdownUrl(token.slice(split + 2, -1));
      if (href === null) {
        parent.append(document.createTextNode(token));
      } else {
        const link = document.createElement("a");
        link.textContent = label;
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer nofollow";
        parent.append(link);
      }
    }
    cursor = index + token.length;
  }
  if (cursor < source.length) parent.append(document.createTextNode(source.slice(cursor)));
}

function appendMarkdownLines(parent, lines, tagName = "p") {
  const block = document.createElement(tagName);
  lines.forEach((line, index) => {
    if (index > 0) block.append(document.createElement("br"));
    appendRestrictedInline(block, line);
  });
  parent.append(block);
}

function renderRestrictedMarkdown(target, content) {
  target.replaceChildren();
  const lines = String(content || "").replaceAll("\r\n", "\n").split("\n");
  let paragraph = [];
  let list = null;
  let fence = null;
  const flushParagraph = () => {
    if (paragraph.length > 0) appendMarkdownLines(target, paragraph);
    paragraph = [];
  };
  const flushList = () => {
    if (list) target.append(list);
    list = null;
  };
  const flushFence = () => {
    if (!fence) return;
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    if (fence.language) code.dataset.language = fence.language;
    code.textContent = fence.lines.join("\n");
    pre.append(code);
    target.append(pre);
    fence = null;
  };
  for (const line of lines) {
    if (fence) {
      if (line === "```") flushFence();
      else fence.lines.push(line);
      continue;
    }
    const fenceStart = line.match(/^```([A-Za-z0-9_+.-]{0,32})\s*$/);
    if (fenceStart) {
      flushParagraph();
      flushList();
      fence = { language: fenceStart[1], lines: [] };
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      appendMarkdownLines(target, [heading[2]], `h${heading[1].length + 2}`);
      continue;
    }
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const expectedTag = unordered ? "UL" : "OL";
      if (list && list.tagName !== expectedTag) flushList();
      if (!list) list = document.createElement(expectedTag.toLowerCase());
      const item = document.createElement("li");
      appendRestrictedInline(item, (unordered || ordered)[1]);
      list.append(item);
      continue;
    }
    if (line.startsWith("> ")) {
      flushParagraph();
      flushList();
      appendMarkdownLines(target, [line.slice(2)], "blockquote");
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushParagraph();
  flushList();
  flushFence();
}

async function copyMessageContent(content, trigger) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(content);
    } else {
      const fallback = document.createElement("textarea");
      try {
        fallback.value = content;
        fallback.setAttribute("readonly", "");
        fallback.className = "clipboard-fallback";
        document.body.append(fallback);
        fallback.select();
        if (!document.execCommand("copy")) throw new Error("copy unavailable");
      } finally {
        fallback.value = "";
        fallback.remove();
      }
    }
    const previousFeedback = copyFeedbackTimers.get(trigger) || null;
    if (previousFeedback !== null) window.clearTimeout(previousFeedback.timer);
    const previous = previousFeedback?.label || trigger.textContent || "复制";
    trigger.textContent = "已复制";
    setStatus("消息已复制到剪贴板");
    const feedback = { label: previous, timer: null };
    feedback.timer = window.setTimeout(() => {
      if (trigger.isConnected) trigger.textContent = previous;
      if (copyFeedbackTimers.get(trigger) === feedback) {
        copyFeedbackTimers.delete(trigger);
      }
    }, 1400);
    copyFeedbackTimers.set(trigger, feedback);
  } catch (_error) {
    setStatus("浏览器未允许复制；可以选择消息文本后手动复制");
  }
}

function clearCopyFeedbackTimers() {
  for (const [trigger, feedback] of copyFeedbackTimers) {
    window.clearTimeout(feedback.timer);
    if (trigger.isConnected) trigger.textContent = feedback.label;
  }
  copyFeedbackTimers.clear();
}

function createMessageElement(role, content, metadata = {}) {
  const message = document.createElement("article");
  const safeRole = ["user", "assistant", "system"].includes(role) ? role : "system";
  message.className = `message message-${safeRole}`;
  if (typeof metadata.messageId === "string") message.dataset.messageId = metadata.messageId;
  if (typeof metadata.runId === "string") message.dataset.runId = metadata.runId;
  if (metadata.live === true) message.dataset.liveAssistant = "true";

  const heading = document.createElement("header");
  const roleLabel = document.createElement("span");
  roleLabel.textContent = safeRole === "user" ? "你" : safeRole === "assistant" ? "智能体" : "系统";
  const headingMeta = document.createElement("span");
  headingMeta.className = "message-meta";
  if (typeof metadata.turnStatus === "string" && TURN_STATUS_LABELS[metadata.turnStatus]) {
    const status = document.createElement("span");
    status.className = `turn-status turn-status-${metadata.turnStatus}`;
    status.textContent = TURN_STATUS_LABELS[metadata.turnStatus];
    headingMeta.append(status);
  }
  const time = document.createElement("time");
  time.textContent = formatTime(metadata.createdAt);
  headingMeta.append(time);
  heading.append(roleLabel, headingMeta);

  const body = document.createElement("div");
  body.className = "message-content";
  const normalizedContent = typeof content === "string" ? content : "";
  if (safeRole === "assistant" && metadata.live !== true) {
    renderRestrictedMarkdown(body, normalizedContent);
  } else {
    body.textContent = normalizedContent;
  }
  const actions = document.createElement("footer");
  actions.className = "message-actions";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "quiet message-action";
  copy.textContent = "复制";
  const copyRole = safeRole === "user" ? "用户" : safeRole === "assistant" ? "智能体" : "系统";
  copy.setAttribute("aria-label", `复制${copyRole}消息`);
  copy.addEventListener("click", () => void copyMessageContent(normalizedContent, copy));
  actions.append(copy);
  message.append(heading, body, actions);
  return { message, body };
}

function normalizeConversationMessage(message) {
  if (!message || typeof message !== "object" || typeof message.content !== "string") {
    return null;
  }
  const terminal = message.terminal;
  const normalizedTerminal = (
    terminal && terminal.version === "turn-terminal-v1" &&
    typeof terminal.code === "string" && /^[A-Za-z0-9._:-]{1,64}$/.test(terminal.code) &&
    Object.hasOwn(TERMINAL_STAGE_LABELS, terminal.stage) &&
    typeof terminal.retryable === "boolean" &&
    Number.isSafeInteger(terminal.duration_ms) &&
    terminal.duration_ms >= 0 && terminal.duration_ms <= 604_800_000
  ) ? {
    code: terminal.code,
    stage: terminal.stage,
    retryable: terminal.retryable,
    duration_ms: terminal.duration_ms,
  } : null;
  return {
    message_id: typeof message.message_id === "string" ? message.message_id : null,
    role: ["user", "assistant", "system"].includes(message.role) ? message.role : "system",
    content: message.content,
    created_at: typeof message.created_at === "string" ? message.created_at : "",
    turn_id: typeof message.turn_id === "string" && message.turn_id ? message.turn_id : null,
    run_id: typeof message.run_id === "string" && RUN_ID_PATTERN.test(message.run_id)
      ? message.run_id
      : null,
    turn_status: typeof message.turn_status === "string" ? message.turn_status : null,
    turn_position: Number.isSafeInteger(message.turn_position) && message.turn_position >= 1
      ? message.turn_position
      : null,
    terminal: normalizedTerminal,
    live: message.live === true,
  };
}

function normalizeConversationPage(page) {
  const validPosition = (value) => (
    value === null || (Number.isSafeInteger(value) && value >= 1 && value <= 128)
  );
  const validCursor = (value) => (
    value === null || (
      typeof value === "string" && value.length >= 1 && value.length <= 256 &&
      /^[A-Za-z0-9_-]+$/.test(value)
    )
  );
  if (
    !page || page.version !== "turn-page-v2" ||
    !Number.isSafeInteger(page.limit) || page.limit < 1 || page.limit > 64 ||
    !Number.isSafeInteger(page.returned_turns) || page.returned_turns < 0 ||
    page.returned_turns > page.limit ||
    !Number.isSafeInteger(page.total_turns) || page.total_turns < page.returned_turns ||
    page.total_turns > 128 || !validPosition(page.oldest_position) ||
    !validPosition(page.newest_position) ||
    typeof page.has_older !== "boolean" || typeof page.has_newer !== "boolean" ||
    !validCursor(page.before_cursor) || !validCursor(page.next_before_cursor) ||
    page.has_newer !== (page.before_cursor !== null) ||
    page.has_older !== (page.next_before_cursor !== null)
  ) {
    return null;
  }
  return {
    limit: page.limit,
    returned_turns: page.returned_turns,
    total_turns: page.total_turns,
    oldest_position: Number.isSafeInteger(page.oldest_position) ? page.oldest_position : null,
    newest_position: Number.isSafeInteger(page.newest_position) ? page.newest_position : null,
    before_cursor: page.before_cursor,
    has_older: page.has_older,
    has_newer: page.has_newer,
    next_before_cursor: page.next_before_cursor,
  };
}

function conversationTurnGroups() {
  const groups = [];
  const byKey = new Map();
  for (const message of state.conversationMessages) {
    const key = message.turn_id || message.run_id || message.message_id || `message-${groups.length}`;
    let group = byKey.get(key);
    if (!group) {
      group = {
        key,
        turnId: message.turn_id,
        runId: message.run_id,
        status: message.turn_status,
        position: message.turn_position,
        terminal: message.terminal,
        messages: [],
        turnNumber: groups.length + 1,
      };
      byKey.set(key, group);
      groups.push(group);
    }
    if (!group.turnId && message.turn_id) group.turnId = message.turn_id;
    if (!group.runId && message.run_id) group.runId = message.run_id;
    if (message.turn_status) group.status = message.turn_status;
    if (message.turn_position !== null) group.position = message.turn_position;
    if (message.terminal) group.terminal = message.terminal;
    group.messages.push(message);
  }
  return groups;
}

function prefillTurnMessage(content, reason) {
  if (typeof content !== "string" || !content.trim()) return;
  elements.messageInput.value = content;
  resizeComposer();
  saveSelectedDraft();
  setRunControls();
  setStatus(reason);
  elements.messageInput.focus();
}

function renderTurnTerminal(group) {
  if (!group.terminal) return null;
  const terminal = document.createElement("section");
  terminal.className = `turn-terminal turn-terminal-${group.status || "failed"}`;
  terminal.setAttribute("role", "group");
  terminal.setAttribute("aria-label", "本轮终态");
  const heading = document.createElement("strong");
  heading.textContent = group.status === "cancelled" ? "本轮已取消" :
    group.status === "interrupted" ? "本轮在重启恢复时中断" : "本轮没有完成";
  const detail = document.createElement("p");
  const codeLabel = modelErrorLabel(group.terminal.code);
  const elapsed = group.terminal.duration_ms < 1000
    ? `${group.terminal.duration_ms} 毫秒`
    : `${(group.terminal.duration_ms / 1000).toFixed(1)} 秒`;
  detail.textContent = `${TERMINAL_STAGE_LABELS[group.terminal.stage]} · ${codeLabel} · ${elapsed}`;
  const context = document.createElement("p");
  context.textContent = "这次失败的用户消息不会进入下一轮模型上下文。";
  terminal.append(heading, detail, context);
  if (group.terminal.retryable) {
    const userMessage = group.messages.find((message) => message.role === "user");
    if (userMessage) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "quiet turn-retry";
      retry.textContent = "填回输入框重试";
      retry.setAttribute("aria-label", `重试第 ${group.position || group.turnNumber} 轮`);
      retry.addEventListener("click", () => {
        prefillTurnMessage(userMessage.content, "原消息已填回；检查后发送会创建一个新轮次");
      });
      terminal.append(retry);
    }
  }
  return terminal;
}

function renderConversationTurns() {
  elements.conversationMessages.replaceChildren();
  state.liveAssistantContent = null;
  const groups = conversationTurnGroups();
  elements.conversationEmpty.hidden = groups.length !== 0;
  if (state.conversationPage?.has_older) {
    const older = document.createElement("button");
    older.type = "button";
    older.className = "quiet conversation-older";
    older.textContent = state.conversationPageLoading ? "正在加载更早消息…" : "加载更早消息";
    older.disabled = state.conversationPageLoading;
    older.addEventListener("click", () => void loadOlderConversationPage());
    elements.conversationMessages.append(older);
  }
  for (const group of groups) {
    const card = document.createElement("article");
    card.className = "turn-card";
    card.dataset.turnKey = group.key;
    if (group.turnId) card.dataset.turnId = group.turnId;
    if (group.runId) card.dataset.runId = group.runId;

    const selectButton = document.createElement("button");
    selectButton.type = "button";
    selectButton.className = "turn-select";
    selectButton.disabled = !group.runId;
    selectButton.setAttribute("aria-pressed", String(group.runId === state.selectedTimelineRunId));
    selectButton.setAttribute(
      "aria-label",
      group.runId
        ? `查看第 ${group.position || group.turnNumber} 轮的运行时间线`
        : `第 ${group.position || group.turnNumber} 轮`,
    );
    const title = document.createElement("strong");
    title.textContent = `第 ${group.position || group.turnNumber} 轮`;
    const status = document.createElement("span");
    status.className = `turn-status turn-status-${group.status || "unknown"}`;
    status.textContent = TURN_STATUS_LABELS[group.status] || group.status || "状态未知";
    const run = document.createElement("code");
    run.textContent = group.runId ? `运行 ${group.runId.slice(0, 8)}` : "尚无运行记录";
    selectButton.append(title, status, run);
    if (group.runId) {
      selectButton.addEventListener("click", () => {
        void selectRunFromTurn(group.runId);
      });
    }

    const messages = document.createElement("div");
    messages.className = "turn-messages";
    for (const message of group.messages) {
      const rendered = createMessageElement(message.role, message.content, {
        messageId: message.message_id,
        createdAt: message.created_at,
        runId: message.run_id,
        turnStatus: message.turn_status,
        live: message === state.liveAssistantMessage,
      });
      messages.append(rendered.message);
      if (message === state.liveAssistantMessage) state.liveAssistantContent = rendered.body;
    }
    card.append(selectButton, messages);
    const terminal = renderTurnTerminal(group);
    if (terminal) card.append(terminal);
    const completedHead = (
      group.status === "completed" && state.conversationPage &&
      state.conversationPage.has_newer === false &&
      group.position === state.conversationPage.newest_position &&
      state.activeRun === null && !sessionIsActive(selectedSession())
    );
    if (completedHead) {
      const actions = document.createElement("footer");
      actions.className = "turn-actions";
      const branch = document.createElement("button");
      branch.type = "button";
      branch.className = "quiet turn-branch";
      branch.textContent = "从这里分支";
      branch.addEventListener("click", () => void branchSession(state.sessionId));
      const regenerate = document.createElement("button");
      regenerate.type = "button";
      regenerate.className = "quiet turn-regenerate";
      regenerate.textContent = "在分支中重新生成";
      const userMessage = group.messages.find((message) => message.role === "user");
      regenerate.disabled = !userMessage;
      regenerate.addEventListener("click", () => {
        if (userMessage) void branchSession(state.sessionId, userMessage.content);
      });
      actions.append(branch, regenerate);
      card.append(actions);
    }
    elements.conversationMessages.append(card);
  }
  syncSelectedTurn(false);
  scheduleConversationLatest();
}

function appendMessage(role, content, metadata = {}) {
  const record = normalizeConversationMessage({
    message_id: metadata.messageId || `live-${role}-${metadata.runId || "pending"}`,
    role,
    content,
    created_at: metadata.createdAt || new Date().toISOString(),
    turn_id: metadata.turnId,
    run_id: metadata.runId,
    turn_status: metadata.turnStatus,
    live: metadata.live === true,
  });
  if (!record) return null;
  if (record.role === "user") state.conversationFollowLatest = true;
  state.conversationMessages.push(record);
  if (metadata.live === true && record.role === "assistant") {
    state.liveAssistantMessage = record;
  }
  renderConversationTurns();
  renderTimelineEntries();
  const body = Array.from(elements.conversationMessages.querySelectorAll(".message-content"))
    .find((candidate) => candidate.parentElement?.dataset.messageId === record.message_id);
  return body || null;
}

function renderMessages(messages) {
  state.blocks.clear();
  state.liveAssistantContent = null;
  state.liveAssistantMessage = null;
  state.conversationMessages = messages
    .map(normalizeConversationMessage)
    .filter((message) => message !== null);
  renderConversationTurns();
  renderTimelineEntries();
  scheduleConversationLatest();
}

async function loadOlderConversationPage() {
  const page = state.conversationPage;
  const sessionId = state.sessionId;
  const cursor = page?.next_before_cursor;
  if (
    !sessionId || !page?.has_older || state.conversationPageLoading ||
    typeof cursor !== "string"
  ) return;
  const requestNumber = state.sessionRequest;
  state.conversationPageLoading = true;
  const button = elements.conversationMessages.querySelector(".conversation-older");
  if (button) {
    button.disabled = true;
    button.textContent = "正在加载更早消息…";
  }
  const previousHeight = elements.conversationMessages.scrollHeight;
  const previousTop = elements.conversationMessages.scrollTop;
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(sessionId)}?limit=32&before=${encodeURIComponent(cursor)}`,
    ));
    const detail = await response.json();
    if (requestNumber !== state.sessionRequest || state.sessionId !== sessionId) return;
    const olderPage = normalizeConversationPage(detail.page);
    const session = normalizeSession(detail.session);
    if (!session || session.session_id !== sessionId || !olderPage || !Array.isArray(detail.messages)) {
      throw new Error("更早消息页格式无效");
    }
    const olderMessages = detail.messages.map(normalizeConversationMessage);
    if (olderMessages.some((message) => message === null)) {
      throw new Error("更早消息页包含无效消息");
    }
    const existingKeys = new Set(state.conversationMessages.map((message) => (
      message.message_id || `${message.turn_id}:${message.role}`
    )));
    const uniqueOlder = olderMessages.filter((message) => {
      const key = message.message_id || `${message.turn_id}:${message.role}`;
      if (existingKeys.has(key)) return false;
      existingKeys.add(key);
      return true;
    });
    if (
      olderPage.has_newer !== true ||
      (uniqueOlder.length > 0 && uniqueOlder.some((message) => (
        message.turn_position === null ||
        (page.oldest_position !== null && message.turn_position >= page.oldest_position)
      )))
    ) {
      throw new Error("更早消息页顺序无效");
    }
    state.conversationMessages = [...uniqueOlder, ...state.conversationMessages];
    state.conversationPage = {
      ...page,
      total_turns: olderPage.total_turns,
      oldest_position: olderPage.oldest_position,
      has_older: olderPage.has_older,
      next_before_cursor: olderPage.next_before_cursor,
    };
    state.conversationFollowLatest = false;
    renderConversationTurns();
    const nextHeight = elements.conversationMessages.scrollHeight;
    elements.conversationMessages.scrollTop = previousTop + (nextHeight - previousHeight);
    const preferredRunId = state.selectedTimelineRunId;
    state.timelineRuns = collectTimelineRuns(state.conversationMessages);
    if (preferredRunId && state.timelineRuns.some((run) => run.runId === preferredRunId)) {
      setSelectedTimelineRunId(preferredRunId);
    }
    renderTimelineRunSelect();
    setStatus(`已加载更早消息；当前显示 ${conversationTurnGroups().length} 个轮次`);
  } catch (error) {
    if (requestNumber === state.sessionRequest && state.csrfToken !== null) {
      if (error.code === "invalid_session_cursor") {
        saveSelectedDraft();
        setStatus("会话内容已更新，正在从最新消息重新载入…", "preparation");
        const restored = await selectSession(sessionId, { preserveTimeline: true });
        if (restored && state.sessionId === sessionId) {
          setStatus("会话已从最新消息恢复；可以继续加载更早内容");
        }
      } else {
        setStatus(`无法加载更早消息：${error.message}`, "failure");
      }
    }
  } finally {
    if (requestNumber === state.sessionRequest && state.sessionId === sessionId) {
      state.conversationPageLoading = false;
      const current = elements.conversationMessages.querySelector(".conversation-older");
      if (current) {
        current.disabled = false;
        current.textContent = "加载更早消息";
      }
    }
  }
}

function syncSelectedTurn(scrollTurn = false) {
  let selected = null;
  for (const card of elements.conversationMessages.querySelectorAll(".turn-card")) {
    const active = card.dataset.runId === state.selectedTimelineRunId;
    card.classList.toggle("selected", active);
    card.querySelector(".turn-select")?.setAttribute("aria-pressed", String(active));
    if (active) selected = card;
  }
  if (scrollTurn && selected) {
    selected.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

async function selectRunFromTurn(runId) {
  if (!RUN_ID_PATTERN.test(runId)) return;
  await selectTimelineRun(runId, { scrollTurn: false });
  setRuntimeInspector(true, { focus: true });
}

function runningRunId(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (
      message && message.role === "user" && message.turn_status === "running" &&
      typeof message.run_id === "string" && RUN_ID_PATTERN.test(message.run_id)
    ) {
      return message.run_id;
    }
  }
  return null;
}

function collectTimelineRuns(messages) {
  const runs = [];
  const byRunId = new Map();
  const turnNumbers = new Map();
  let nextTurnNumber = 1;
  for (const message of messages) {
    const runId = message?.run_id;
    if (typeof runId !== "string" || !RUN_ID_PATTERN.test(runId)) continue;
    const existing = byRunId.get(runId);
    if (existing) {
      if (typeof message.turn_status === "string") existing.status = message.turn_status;
      continue;
    }
    const turnId = typeof message.turn_id === "string" && message.turn_id
      ? message.turn_id
      : runId;
    if (!turnNumbers.has(turnId)) {
      turnNumbers.set(turnId, nextTurnNumber);
      nextTurnNumber += 1;
    }
    const run = {
      runId,
      turnId: turnId === runId ? null : turnId,
      turnNumber: turnNumbers.get(turnId),
      status: typeof message.turn_status === "string" ? message.turn_status : null,
    };
    byRunId.set(runId, run);
    runs.push(run);
  }
  return runs;
}

function renderTimelineRunSelect() {
  elements.timelineRunSelect.replaceChildren();
  if (state.timelineRuns.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无可回放的 Turn / Run";
    elements.timelineRunSelect.append(option);
  } else {
    for (const run of state.timelineRuns) {
      const option = document.createElement("option");
      option.value = run.runId;
      const status = TURN_STATUS_LABELS[run.status] || run.status || "状态未知";
      option.textContent = run.kind === "subagent"
        ? `子 Agent ${run.childAgentId.slice(0, 8)} · ${status} · Run ${run.runId.slice(0, 8)}`
        : `Turn ${run.turnNumber} · ${status} · Run ${run.runId.slice(0, 8)}`;
      elements.timelineRunSelect.append(option);
    }
    if (state.timelineRuns.some((run) => run.runId === state.selectedTimelineRunId)) {
      elements.timelineRunSelect.value = state.selectedTimelineRunId;
    }
  }
  syncSelectedTurn(false);
  setRunControls();
}

function setTimelineRuns(messages, preferredRunId = null) {
  state.timelineRuns = collectTimelineRuns(messages);
  const selectedRunId = state.timelineRuns.some((run) => run.runId === preferredRunId)
    ? preferredRunId
    : state.timelineRuns.at(-1)?.runId || null;
  setSelectedTimelineRunId(selectedRunId);
  renderTimelineRunSelect();
}

function registerTimelineRun(runId, status = "running") {
  if (!RUN_ID_PATTERN.test(runId)) return;
  const existing = state.timelineRuns.find((run) => run.runId === runId);
  if (existing) {
    existing.status = status;
  } else {
    state.timelineRuns.push({
      runId,
      turnId: null,
      turnNumber: state.timelineRuns.length + 1,
      status,
    });
  }
  state.followLatest = true;
  setSelectedTimelineRunId(runId);
  renderTimelineRunSelect();
}

function renderSubagents() {
  elements.subagentList.replaceChildren();
  elements.subagentPanel.hidden = state.subagents.length === 0;
  for (const delegation of state.subagents) {
    const card = document.createElement("article");
    card.className = "subagent-card";
    const heading = document.createElement("div");
    heading.className = "subagent-card-heading";
    const identity = document.createElement("strong");
    identity.textContent = `子智能体 ${delegation.child_agent_id.slice(0, 8)}`;
    const status = document.createElement("span");
    status.className = `subagent-state state-${delegation.state}`;
    status.textContent = delegation.state;
    heading.append(identity, status);
    card.append(heading);
    for (const message of delegation.mailbox) {
      const row = document.createElement("div");
      row.className = `subagent-message ${message.direction}`;
      const label = document.createElement("span");
      label.textContent = message.direction === "parent_to_child" ? "父 → 子" : "子 → 父";
      const content = document.createElement("pre");
      content.textContent = message.content;
      row.append(label, content);
      card.append(row);
    }
    if (typeof delegation.child_run_id === "string") {
      const replay = document.createElement("button");
      replay.type = "button";
      replay.className = "quiet subagent-replay";
      replay.textContent = `查看子运行 ${delegation.child_run_id.slice(0, 8)}`;
      replay.addEventListener("click", () => {
        void selectTimelineRun(delegation.child_run_id, { scrollTurn: false });
      });
      card.append(replay);
    }
    elements.subagentList.append(card);
  }
}

async function refreshSubagents(
  sessionId,
  requestNumber,
  agentId = state.agentId,
  agentEpoch = state.agentEpoch,
) {
  const response = await api(
    `/api/agents/${encodeURIComponent(agentId)}/sessions/` +
    `${encodeURIComponent(sessionId)}/subagents`,
  );
  const payload = await response.json();
  if (
    requestNumber !== state.sessionRequest || state.sessionId !== sessionId ||
    !agentScopeIsCurrent(agentId, agentEpoch)
  ) return;
  if (!Array.isArray(payload.delegations)) throw new Error("子智能体状态格式无效");
  state.subagents = payload.delegations.filter((item) => (
    item && typeof item.child_agent_id === "string" &&
    Array.isArray(item.mailbox)
  ));
  for (const item of state.subagents) {
    if (
      typeof item.child_run_id === "string" && RUN_ID_PATTERN.test(item.child_run_id) &&
      !state.timelineRuns.some((run) => run.runId === item.child_run_id)
    ) {
      state.timelineRuns.push({
        runId: item.child_run_id,
        turnId: item.child_turn_id || null,
        turnNumber: null,
        status: item.state,
        kind: "subagent",
        childAgentId: item.child_agent_id,
        childConversationId: item.child_conversation_id,
      });
    }
  }
  renderSubagents();
  renderTimelineRunSelect();
}

function updateTimelineRunTerminalStatus(runId, terminalKind) {
  const status = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
  }[terminalKind];
  if (!status) return;
  const run = state.timelineRuns.find((item) => item.runId === runId);
  if (!run) return;
  run.status = status;
  renderTimelineRunSelect();
}

function timelineLoadIsCurrent(runId, sessionRequest, timelineRequest, sessionId) {
  return (
    sessionRequest === state.sessionRequest &&
    timelineRequest === state.timelineRequest &&
    sessionId === state.sessionId &&
    runId === state.selectedTimelineRunId
  );
}

function addTimelineControl(kind, payload, cursor, conversationId = state.sessionId) {
  addTimelineEvent({
    schema_version: STREAM_CONTROL_VERSION,
    event_id: null,
    agent_id: state.agentId,
    conversation_id: conversationId,
    turn_id: payload?.snapshot?.turn_id || null,
    run_id: payload.run_id,
    parent_run_id: null,
    seq: cursor,
    occurred_at: null,
    kind,
    durability: "control",
    payload,
  });
}

async function loadDurableTimeline(runId, sessionRequest, timelineRequest, sessionId) {
  const runMetadata = state.timelineRuns.find((item) => item.runId === runId);
  const expectedConversationId = runMetadata?.kind === "subagent"
    ? runMetadata.childConversationId
    : sessionId;
  let cursor = 0;
  let pages = 0;
  let replayComplete = false;
  const seenGaps = new Set();
  const ordered = [];
  let insertionOrder = 0;
  while (pages < 5) {
    const replayAgentId = runMetadata?.kind === "subagent"
      ? runMetadata.childAgentId
      : state.agentId;
    if (typeof replayAgentId !== "string" || !AGENT_ID_PATTERN.test(replayAgentId)) {
      throw new Error("历史事件的智能体标识无效");
    }
    const replayPath = `/api/agents/${encodeURIComponent(replayAgentId)}/runs/` +
      `${encodeURIComponent(runId)}/replay?after=${cursor}&limit=128`;
    const response = await api(
      replayPath,
    );
    const page = await response.json();
    if (!timelineLoadIsCurrent(runId, sessionRequest, timelineRequest, sessionId)) {
      return false;
    }
    const identity = page?.identity;
    if (
      !identity || identity.run_id !== runId || identity.conversation_id !== expectedConversationId ||
      !Array.isArray(page.events) || !Array.isArray(page.gaps) ||
      !Number.isSafeInteger(page.next_cursor) || page.next_cursor < cursor ||
      typeof page.has_more !== "boolean"
    ) {
      throw new Error("历史事件回放格式无效");
    }
    for (const gap of page.gaps) {
      if (
        !gap || !Number.isSafeInteger(gap.from_seq) ||
        !Number.isSafeInteger(gap.to_seq) || gap.from_seq > gap.to_seq ||
        typeof gap.reason !== "string"
      ) {
        throw new Error("历史事件缺口格式无效");
      }
      const key = `${gap.from_seq}:${gap.to_seq}:${gap.reason}`;
      if (!seenGaps.has(key)) {
        seenGaps.add(key);
        ordered.push({
          sequence: gap.to_seq,
          type: "gap",
          value: gap,
          insertionOrder,
        });
        insertionOrder += 1;
      }
    }
    for (const envelope of page.events) {
      if (
        !envelope || envelope.run_id !== runId ||
        !Number.isSafeInteger(envelope.seq) || typeof envelope.kind !== "string"
      ) {
        throw new Error("历史事件消息格式无效");
      }
      ordered.push({
        sequence: envelope.seq,
        type: "event",
        value: envelope,
        insertionOrder,
      });
      insertionOrder += 1;
    }
    if (page.availability === "snapshot_only") {
      if (!page.snapshot || page.snapshot.run_id !== runId) {
        throw new Error("历史事件快照格式无效");
      }
      ordered.push({
        sequence: page.next_cursor,
        type: "snapshot",
        value: {
          cursor: page.next_cursor,
          availability: page.availability,
          snapshot: page.snapshot,
        },
        insertionOrder,
      });
      insertionOrder += 1;
    }
    pages += 1;
    if (!page.has_more) {
      replayComplete = true;
      break;
    }
    if (page.next_cursor <= cursor) throw new Error("历史事件回放未推进");
    cursor = page.next_cursor;
  }
  if (!replayComplete) throw new Error("历史事件回放页数超过上限");
  if (!timelineLoadIsCurrent(runId, sessionRequest, timelineRequest, sessionId)) return false;
  const priorities = { gap: 0, event: 1, snapshot: 2 };
  ordered.sort((left, right) => (
    left.sequence - right.sequence ||
    priorities[left.type] - priorities[right.type] ||
    left.insertionOrder - right.insertionOrder
  ));
  clearTimeline(runId);
  for (const item of ordered) {
    if (item.type === "event") {
      addTimelineEvent(item.value);
    } else if (item.type === "gap") {
      addTimelineControl("stream.gap", {
        control_version: STREAM_CONTROL_VERSION,
        run_id: runId,
        ...item.value,
        resume_cursor: item.value.to_seq,
      }, item.value.to_seq);
    } else {
      addTimelineControl("stream.snapshot", {
        control_version: STREAM_CONTROL_VERSION,
        run_id: runId,
        ...item.value,
      }, item.sequence);
    }
  }
  return true;
}

function turnStatusForRun(messages, runId) {
  const message = messages.find((item) => item && item.run_id === runId);
  return message && typeof message.turn_status === "string" ? message.turn_status : null;
}

function pruneInactiveTimelineCaches() {
  const activeRunIds = new Set(
    Array.from(state.activeRuns.values(), (runContext) => runContext.runId),
  );
  for (const runId of state.timelineEntriesByRun.keys()) {
    if (!activeRunIds.has(runId)) state.timelineEntriesByRun.delete(runId);
  }
  for (const runId of state.selectedTimelineEntryKeyByRun.keys()) {
    if (!activeRunIds.has(runId)) state.selectedTimelineEntryKeyByRun.delete(runId);
  }
}

function releaseHistoricalTimelineCaches(keepRunId = null) {
  const retainedRunIds = new Set(
    Array.from(state.activeRuns.values(), (runContext) => runContext.runId),
  );
  if (typeof keepRunId === "string" && RUN_ID_PATTERN.test(keepRunId)) {
    retainedRunIds.add(keepRunId);
  }
  for (const runId of state.timelineEntriesByRun.keys()) {
    if (!retainedRunIds.has(runId)) state.timelineEntriesByRun.delete(runId);
  }
  for (const runId of state.selectedTimelineEntryKeyByRun.keys()) {
    if (!retainedRunIds.has(runId)) state.selectedTimelineEntryKeyByRun.delete(runId);
  }
  if (
    state.selectedTimelineRunId !== null &&
    !retainedRunIds.has(state.selectedTimelineRunId)
  ) {
    stopReplay();
    state.timelineEntries = [];
    state.selectedTimelineEntryKey = null;
    renderTimelineEntries();
  }
}

async function selectSession(
  sessionId,
  {
    preserveTimeline = false,
    attachRunning = true,
    ownerRun = null,
    reconcileAttempt = false,
    deferTerminalCompletion = false,
  } = {},
) {
  if (typeof sessionId !== "string") return null;
  const agentId = state.agentId;
  const agentEpoch = state.agentEpoch;
  if (typeof agentId !== "string" || !AGENT_ID_PATTERN.test(agentId)) return null;
  state.sessionController?.abort();
  const rollback = state.sessionRollback || {
    sessionId: state.sessionId,
    conversationMessages: state.conversationMessages,
    conversationPage: state.conversationPage,
    subagents: state.subagents,
    liveAssistantMessage: state.liveAssistantMessage,
    timelineRunId: state.selectedTimelineRunId,
    timelineRuns: state.timelineRuns,
    sessionRevision: selectedSession()?.revision ?? null,
  };
  state.sessionRollback = rollback;
  const previousSessionId = rollback.sessionId;
  if (previousSessionId && previousSessionId !== sessionId) saveSelectedDraft();
  const previousConversationMessages = rollback.conversationMessages;
  const previousConversationPage = rollback.conversationPage;
  const previousSubagents = rollback.subagents;
  const previousLiveAssistantMessage = rollback.liveAssistantMessage;
  const previousTimelineRunId = rollback.timelineRunId;
  const previousTimelineRuns = rollback.timelineRuns;
  clearContextInspector();
  const requestNumber = ++state.sessionRequest;
  const controller = new AbortController();
  state.sessionController = controller;
  const timeout = window.setTimeout(() => controller.abort(), 15000);
  state.sessionLoading = true;
  state.timelineRequest += 1;
  state.sessionId = sessionId;
  if (previousSessionId !== sessionId) {
    // Identity changes synchronously: never display the previous session's
    // messages, command result, or approval controls under the new heading
    // while its detail request is in flight.
    clearConversation();
    state.permissionRequest += 1;
    renderPermissions([]);
    elements.commandResultTitle.textContent = "";
    elements.commandResultJson.textContent = "";
    elements.commandResult.hidden = true;
    restoreSelectedDraft();
    state.conversationFollowLatest = true;
    clearSessionContextUsage();
    state.timelineRuns = [];
    setSelectedTimelineRunId(null);
    pruneInactiveTimelineCaches();
    state.timelineEntries = [];
    state.selectedTimelineEntryKey = null;
    state.timelineEntrySerial = 0;
    state.timelineFilter = "all";
    state.followLatest = true;
    renderTimelineRunSelect();
  }
  renderSessionList();
  elements.activeSessionTitle.textContent = "正在恢复会话…";
  elements.sessionId.textContent = sessionId;
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(sessionId)}`,
      agentId,
    ), { signal: controller.signal });
    const detail = await response.json();
    if (
      requestNumber !== state.sessionRequest || state.sessionId !== sessionId ||
      !agentScopeIsCurrent(agentId, agentEpoch)
    ) return null;
    const session = normalizeSession(detail.session);
    const page = normalizeConversationPage(detail.page);
    if (
      !session || session.session_id !== sessionId || !Array.isArray(detail.messages) || !page
    ) {
      throw new Error("会话详情格式无效");
    }
    const index = state.sessions.findIndex((item) => item.session_id === sessionId);
    if (index >= 0) state.sessions[index] = session;
    if (!preserveTimeline) clearTimeline();
    elements.activeSessionTitle.textContent = session.title;
    elements.sessionId.textContent = session.session_id;
    state.conversationPage = page;
    state.conversationPageLoading = false;
    state.backgroundCompletions.delete(sessionId);
    renderMessages(detail.messages);
    restoreSelectedDraft();
    setTimelineRuns(
      detail.messages,
      preserveTimeline && previousSessionId === sessionId ? previousTimelineRunId : null,
    );
    await refreshSubagents(sessionId, requestNumber, agentId, agentEpoch);
    if (
      requestNumber !== state.sessionRequest || state.sessionId !== sessionId ||
      !agentScopeIsCurrent(agentId, agentEpoch)
    ) return null;
    if (ownerRun && !state.timelineRuns.some((run) => run.runId === ownerRun.runId)) {
      registerTimelineRun(ownerRun.runId, "running");
    }
    renderSessionList();
    void refreshNextTurnPreview(sessionId);

    const recoverableRunId = sessionIsActive(session) ? runningRunId(detail.messages) : null;
    const trackedRun = state.activeRuns.get(session.session_id) || null;
    if (attachRunning && recoverableRunId && trackedRun === null) {
      attachRecoveredRun(session.session_id, recoverableRunId);
    } else if (
      trackedRun !== null && trackedRun.terminalSeen &&
      trackedRun.awaitingCanonicalRefresh && !deferTerminalCompletion
    ) {
      trackedRun.awaitingCanonicalRefresh = false;
      completeRunContext(trackedRun, false);
    } else if (trackedRun !== null) {
      if (!trackedRun.terminalSeen) {
        registerTimelineRun(trackedRun.runId, "running");
        renderRunAssistant(trackedRun);
        void pollPendingPermissions(trackedRun);
        if (trackedRun.driverPromise === null) void driveRun(trackedRun);
      }
      setStatus(
        trackedRun.terminalSeen
          ? "本轮已结束，正在核对持久会话终态…"
          : "此会话正在后台运行，已切回实时视图",
        trackedRun.terminalSeen ? "preparation" : "auto",
      );
    } else if (sessionIsActive(session)) {
      setStatus(
        recoverableRunId ? "会话有正在执行的任务" : "会话正在运行，但当前无法恢复事件流",
      );
    } else {
      // Conversation restore is intentionally transcript-only. Canonical Run
      // envelopes may contain assistant bodies and are fetched only after the
      // operator opens/selects the advanced runtime inspector.
      setStatus("会话已恢复；运行详情将在需要时读取");
      elements.messageInput.focus();
    }
    state.sessionRollback = null;
    return { session, messages: detail.messages, page };
  } catch (error) {
    if (
      requestNumber !== state.sessionRequest || state.csrfToken === null ||
      !agentScopeIsCurrent(agentId, agentEpoch)
    ) return null;
    state.sessionId = state.sessions.some((item) => item.session_id === previousSessionId)
      ? previousSessionId
      : null;
    state.timelineRuns = state.sessionId === previousSessionId ? previousTimelineRuns : [];
    setSelectedTimelineRunId(
      state.sessionId === previousSessionId ? previousTimelineRunId : null,
    );
    renderTimelineRunSelect();
    const previous = selectedSession();
    if (previous && previous.session_id === previousSessionId) {
      state.conversationMessages = previousConversationMessages;
      state.conversationPage = previousConversationPage;
      state.subagents = previousSubagents;
      state.liveAssistantMessage = previousLiveAssistantMessage;
      renderConversationTurns();
      renderSubagents();
      restoreSelectedDraft();
      const previousRun = state.activeRuns.get(previousSessionId) || null;
      if (previousRun && !previousRun.terminalSeen) {
        renderRunAssistant(previousRun);
        void pollPendingPermissions(previousRun);
      }
    } else {
      clearConversation();
      elements.messageInput.value = "";
      resizeComposer();
      renderMessageByteUsage();
    }
    elements.activeSessionTitle.textContent = previous ? previous.title : "请选择一个会话";
    elements.sessionId.textContent = previous ? previous.session_id : "未选择";
    renderSessionList();
    const canonicalStateChanged = Boolean(
      previous && previous.session_id === previousSessionId && (
        state.backgroundCompletions.has(previousSessionId) ||
        (Number.isSafeInteger(rollback.sessionRevision) &&
          previous.revision > rollback.sessionRevision) ||
        (previousLiveAssistantMessage !== null &&
          !state.activeRuns.has(previousSessionId))
      )
    );
    if (canonicalStateChanged && !reconcileAttempt) {
      state.sessionRollback = null;
      setStatus(
        "上一会话的后台状态已变化，正在读取完整终态后再允许继续发送…",
        "preparation",
      );
      const reconciled = await selectSession(previousSessionId, {
        preserveTimeline: true,
        reconcileAttempt: true,
      });
      if (
        !reconciled && agentScopeIsCurrent(agentId, agentEpoch) &&
        state.sessionId === previousSessionId
      ) {
        state.sessionLoading = true;
        setRunControls();
        setStatus(
          "上一会话已在后台变化，但完整终态暂时无法读取；请重新选择该会话后再发送",
          "failure",
        );
      }
      return reconciled;
    }
    setStatus(
      error.name === "AbortError"
        ? "会话加载超时；已恢复上一会话，可以重试或选择其它会话"
        : error.message,
      error.name === "AbortError" ? "failure" : "auto",
    );
    state.sessionRollback = null;
    return null;
  } finally {
    window.clearTimeout(timeout);
    if (state.sessionController === controller) state.sessionController = null;
    if (
      requestNumber === state.sessionRequest &&
      agentScopeIsCurrent(agentId, agentEpoch)
    ) {
      state.sessionLoading = false;
      setRunControls();
    }
  }
}

async function selectTimelineRun(runId, { scrollTurn = true } = {}) {
  if (
    state.sessionId === null ||
    typeof runId !== "string" || !RUN_ID_PATTERN.test(runId) ||
    !state.timelineRuns.some((run) => run.runId === runId)
  ) {
    renderTimelineRunSelect();
    return;
  }
  const sessionId = state.sessionId;
  const sessionRequest = state.sessionRequest;
  const timelineRequest = ++state.timelineRequest;
  stopReplay();
  releaseHistoricalTimelineCaches(runId);
  setSelectedTimelineRunId(runId, { scrollTurn });
  renderTimelineRunSelect();
  elements.runId.textContent = runId;
  if (state.activeRun?.runId === runId) {
    state.timelineEntries = state.timelineEntriesByRun.get(runId) || [];
    renderTimelineEntries();
    setStatus("已切回正在执行的运行时间线");
    return;
  }
  clearTimeline(runId);
  setStatus("正在读取所选轮次的运行时间线…");
  try {
    const loaded = await loadDurableTimeline(
      runId,
      sessionRequest,
      timelineRequest,
      sessionId,
    );
    if (loaded) setStatus("所选轮次的运行时间线已恢复");
  } catch (error) {
    if (timelineLoadIsCurrent(runId, sessionRequest, timelineRequest, sessionId)) {
      setStatus(`无法读取所选运行时间线：${error.message}`, "failure");
    }
  }
}

function hasExactFields(value, fields) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === fields.length && fields.every((field) => keys.includes(field));
}

function isNonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validateContextInspection(value, runId, sessionId, agentId = state.agentId) {
  if (!hasExactFields(value, CONTEXT_RESPONSE_FIELDS)) {
    throw new Error("invalid context inspection");
  }
  const identity = value.identity;
  if (
    !hasExactFields(identity, ["agent_id", "conversation_id", "turn_id", "run_id"]) ||
    identity.agent_id !== agentId || identity.conversation_id !== sessionId ||
    identity.run_id !== runId || typeof identity.turn_id !== "string"
  ) {
    throw new Error("invalid context identity");
  }
  const planFields = hasExactFields(value.context_plan, CONTEXT_PLAN_FIELDS)
    ? CONTEXT_PLAN_FIELDS
    : (hasExactFields(value.context_plan, LEGACY_CONTEXT_PLAN_FIELDS)
      ? LEGACY_CONTEXT_PLAN_FIELDS
      : null);
  if (!planFields) {
    throw new Error("invalid context plan");
  }
  const plan = value.context_plan;
  const numericPlanFields = planFields.filter((field) => (
    field.endsWith("_count") || field.endsWith("_tokens")
  ));
  const stringPlanFields = planFields.filter(
    (field) => !numericPlanFields.includes(field),
  );
  if (
    numericPlanFields.some((field) => !isNonNegativeInteger(plan[field])) ||
    stringPlanFields.some((field) => (
      typeof plan[field] !== "string" || plan[field].length === 0 || plan[field].length > 512
    )) ||
    !/^[a-f0-9]{64}$/.test(plan.digest) ||
    !/^[a-f0-9]{64}$/.test(plan.toolset_digest) ||
    !/^[a-f0-9]{64}$/.test(plan.history_source_digest) ||
    plan.included_history_message_count + plan.omitted_history_message_count !==
      plan.history_message_count
  ) {
    throw new Error("invalid context plan metadata");
  }
  if (
    !hasExactFields(value.renderer, [
      "version",
      "section_registry_version",
      "leading_system_sections_merged",
      "leading_system_section_count",
      "description",
    ]) ||
    typeof value.renderer.description !== "string" ||
    value.renderer.description.length > 4096 ||
    !Array.isArray(value.sections) || value.sections.length > 512 ||
    typeof value.notice !== "string" || value.notice.length > 4096
  ) {
    throw new Error("invalid context renderer");
  }
  for (const section of value.sections) {
    if (
      !hasExactFields(section, CONTEXT_SECTION_FIELDS) ||
      !["system", "user", "assistant"].includes(section.role) ||
      ["id", "trust", "provenance", "cache", "truncation", "truncation_reason"].some((field) => (
        typeof section[field] !== "string" ||
        section[field].length === 0 || section[field].length > 512
      )) ||
      !isNonNegativeInteger(section.estimated_tokens) ||
      !isNonNegativeInteger(section.budget_tokens) ||
      !isNonNegativeInteger(section.content_bytes) ||
      typeof section.dependency_digest !== "string" ||
      !/^[a-f0-9]{64}$/.test(section.dependency_digest) ||
      typeof section.content_digest !== "string" ||
      !/^[a-f0-9]{64}$/.test(section.content_digest)
    ) {
      throw new Error("invalid context section");
    }
  }
  if (value.availability === "exact") {
    if (
      value.content_exposure !== "withheld" ||
      !isNonNegativeInteger(value.provider_message_count) ||
      plan.section_count !== value.sections.length ||
      typeof value.renderer.version !== "string" ||
      value.renderer.version.length === 0 ||
      typeof value.renderer.section_registry_version !== "string" ||
      value.renderer.section_registry_version.length === 0 ||
      typeof value.renderer.leading_system_sections_merged !== "boolean" ||
      !isNonNegativeInteger(value.renderer.leading_system_section_count)
    ) {
      throw new Error("invalid exact context inspection");
    }
  } else if (value.availability === "summary_only") {
    if (
      value.content_exposure !== "unavailable" || value.provider_message_count !== null ||
      value.sections.length !== 0 || value.renderer.version !== null ||
      value.renderer.section_registry_version !== null ||
      value.renderer.leading_system_sections_merged !== null ||
      value.renderer.leading_system_section_count !== null
    ) {
      throw new Error("invalid summary context inspection");
    }
  } else {
    throw new Error("invalid context availability");
  }
  return value;
}

function appendContextMetric(label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = String(value);
  elements.contextInspectMetrics.append(term, description);
}

function renderContextInspection(inspection) {
  const plan = inspection.context_plan;
  elements.contextInspectMetrics.replaceChildren();
  elements.contextSectionList.replaceChildren();
  elements.contextInspectAvailability.textContent = inspection.availability === "exact"
    ? "精确元数据 · 当前 Gateway 仍保留已验证的 ContextPlan"
    : "仅摘要 · 当前只能读取 durable run.started 中的上下文摘要";
  appendContextMetric(
    "纳入历史消息 included / history",
    `${plan.included_history_message_count} / ${plan.history_message_count}`,
  );
  appendContextMetric("省略历史消息", plan.omitted_history_message_count);
  appendContextMetric("Window 策略", plan.windowing_strategy);
  appendContextMetric(
    "输入估算 / 硬预算",
    `${plan.estimated_input_tokens} / ${plan.input_budget_tokens} tokens`,
  );
  appendContextMetric(
    "Provider 消息数",
    inspection.provider_message_count === null ? "摘要中不可用" : inspection.provider_message_count,
  );
  appendContextMetric(
    "Renderer",
    inspection.renderer.version === null ? "摘要中不可用" : inspection.renderer.version,
  );

  if (inspection.sections.length === 0) {
    const empty = document.createElement("li");
    empty.className = "context-section-empty";
    empty.textContent = "Section 顺序和逐项元数据已不在内存中。";
    elements.contextSectionList.append(empty);
  } else {
    inspection.sections.forEach((section, index) => {
      const item = document.createElement("li");
      item.className = "context-section";
      const heading = document.createElement("strong");
      heading.textContent = `#${index + 1} · ${section.role} · ${section.id}`;
      const trust = document.createElement("span");
      trust.textContent = `trust: ${section.trust} · provenance: ${section.provenance}`;
      const size = document.createElement("span");
      size.textContent = (
        `${section.estimated_tokens} / ${section.budget_tokens} tokens · ` +
        `${section.content_bytes} bytes`
      );
      const policy = document.createElement("span");
      policy.textContent = (
        `cache: ${section.cache} · truncation: ${section.truncation} · ` +
        `reason: ${section.truncation_reason}`
      );
      const digest = document.createElement("code");
      digest.textContent = (
        `content: ${section.content_digest.slice(0, 12)}… · ` +
        `dependency: ${section.dependency_digest.slice(0, 12)}…`
      );
      item.append(heading, trust, size, policy, digest);
      elements.contextSectionList.append(item);
    });
  }
  elements.contextInspectNotice.textContent = inspection.availability === "exact"
    ? "正文默认不返回：conversation 内容请查看右侧对话；system prompt 和隐藏 prompt 不暴露。"
    : "Gateway 重启或 ContextPlan retention 结束后，只保留经过验证的 run.started 摘要；无法还原逐项 Section 或隐藏正文。";
  elements.contextInspectJson.textContent = JSON.stringify(inspection, null, 2);
}

function contextLoadIsCurrent(runId, sessionId, sessionRequest, timelineRequest, contextRequest) {
  return (
    runId === state.selectedTimelineRunId && sessionId === state.sessionId &&
    sessionRequest === state.sessionRequest && timelineRequest === state.timelineRequest &&
    contextRequest === state.contextRequest
  );
}

async function inspectSelectedRunContext(trigger) {
  const runId = state.selectedTimelineRunId;
  const sessionId = state.sessionId;
  if (
    state.csrfToken === null || sessionId === null || runId === null ||
    !RUN_ID_PATTERN.test(runId) || state.contextLoading
  ) {
    return;
  }
  const sessionRequest = state.sessionRequest;
  const timelineRequest = state.timelineRequest;
  const contextRequest = ++state.contextRequest;
  state.contextLoading = true;
  state.contextDialogTrigger = trigger;
  elements.contextInspectAvailability.textContent = "正在读取本轮上下文元数据…";
  elements.contextInspectMetrics.replaceChildren();
  elements.contextSectionList.replaceChildren();
  elements.contextInspectNotice.textContent = "正文不会随此请求返回。";
  elements.contextInspectJson.textContent = "";
  setContextInspectControl();
  if (!elements.contextInspectDialog.open) elements.contextInspectDialog.showModal();
  try {
    const runMetadata = state.timelineRuns.find((item) => item.runId === runId);
    const expectedConversationId = runMetadata?.kind === "subagent"
      ? runMetadata.childConversationId
      : sessionId;
    const contextAgentId = runMetadata?.kind === "subagent"
      ? runMetadata.childAgentId
      : state.agentId;
    if (typeof contextAgentId !== "string" || !AGENT_ID_PATTERN.test(contextAgentId)) {
      throw new Error("上下文中的智能体标识无效");
    }
    const contextPath = `/api/agents/${encodeURIComponent(contextAgentId)}/runs/` +
      `${encodeURIComponent(runId)}/context`;
    const response = await api(contextPath);
    const body = await response.json();
    if (
      !contextLoadIsCurrent(
        runId,
        sessionId,
        sessionRequest,
        timelineRequest,
        contextRequest,
      )
    ) {
      return;
    }
    renderContextInspection(validateContextInspection(
      body,
      runId,
      expectedConversationId,
      contextAgentId,
    ));
  } catch (_error) {
    if (
      contextLoadIsCurrent(
        runId,
        sessionId,
        sessionRequest,
        timelineRequest,
        contextRequest,
      )
    ) {
      elements.contextInspectAvailability.textContent = "本轮上下文暂时不可用";
      elements.contextInspectMetrics.replaceChildren();
      elements.contextSectionList.replaceChildren();
      elements.contextInspectNotice.textContent = "未展示服务错误或任何未验证内容。";
      elements.contextInspectJson.textContent = "";
    }
  } finally {
    if (contextRequest === state.contextRequest) {
      if (
        contextLoadIsCurrent(
          runId,
          sessionId,
          sessionRequest,
          timelineRequest,
          contextRequest,
        )
      ) {
        state.contextLoading = false;
        setContextInspectControl();
      } else {
        clearContextInspector();
      }
    }
  }
}

async function refreshSessions(
  preferredSessionId = state.sessionId,
  agentId = state.agentId,
  agentEpoch = state.agentEpoch,
) {
  const listRequest = ++state.sessionListRequest;
  elements.sessionListStatus.textContent = "正在读取会话…";
  const response = await api(agentApiPath("/sessions", agentId));
  const body = await response.json();
  if (!body || !Array.isArray(body.sessions) || body.sessions.length > 100) {
    throw new Error("会话列表格式无效");
  }
  if (
    !agentScopeIsCurrent(agentId, agentEpoch) ||
    listRequest !== state.sessionListRequest
  ) throw abortError();
  const incoming = body.sessions.map(normalizeSession).filter((item) => item !== null);
  if (
    incoming.length !== body.sessions.length ||
    new Set(incoming.map((item) => item.session_id)).size !== incoming.length
  ) throw new Error("会话列表内容无效");
  const existingById = new Map(state.sessions.map((item) => [item.session_id, item]));
  state.sessions = incoming.map((item) => {
    const existing = existingById.get(item.session_id);
    return existing && existing.revision > item.revision ? existing : item;
  });
  pruneSessionBrowserState(agentId, state.sessions);
  elements.sessionListStatus.textContent = state.sessions.length
    ? `${state.sessions.length} 个会话`
    : "";
  const selected = state.sessions.find((item) => item.session_id === preferredSessionId);
  const target = selected || state.sessions[0] || null;
  if (!target) {
    clearSelectedSession();
    renderSessionList();
    return null;
  }
  renderSessionList();
  return selectSession(target.session_id);
}

async function refreshSessionSummaries(
  agentId = state.agentId,
  agentEpoch = state.agentEpoch,
) {
  const listRequest = ++state.sessionListRequest;
  const response = await api(agentApiPath("/sessions", agentId));
  const body = await response.json();
  if (!body || !Array.isArray(body.sessions) || body.sessions.length > 100) {
    throw new Error("会话列表格式无效");
  }
  if (
    !agentScopeIsCurrent(agentId, agentEpoch) ||
    listRequest !== state.sessionListRequest
  ) throw abortError();
  const incoming = body.sessions.map(normalizeSession).filter((item) => item !== null);
  if (
    incoming.length !== body.sessions.length ||
    new Set(incoming.map((item) => item.session_id)).size !== incoming.length
  ) throw new Error("会话列表内容无效");
  const existingById = new Map(state.sessions.map((item) => [item.session_id, item]));
  state.sessions = incoming.map((item) => {
    const existing = existingById.get(item.session_id);
    return existing && existing.revision > item.revision ? existing : item;
  });
  pruneSessionBrowserState(agentId, state.sessions);
  elements.sessionListStatus.textContent = state.sessions.length
    ? `${state.sessions.length} 个会话`
    : "";
  if (
    state.sessionId !== null &&
    !state.sessions.some((session) => session.session_id === state.sessionId)
  ) {
    clearSelectedSession({ preserveTimeline: true });
  }
  renderSessionList();
}

async function renameSession(session) {
  if (!session || state.mutationPending) return;
  const requested = window.prompt("输入新的会话名称（最多 256 个 UTF-8 字节）", session.title);
  if (requested === null) return;
  const title = requested.trim();
  const bytes = new TextEncoder().encode(title).length;
  if (!title || bytes > 256) {
    setStatus("会话名称必须为 1–256 个 UTF-8 字节");
    return;
  }
  if (title === session.title) {
    setStatus("会话名称没有变化");
    return;
  }
  const mutationScope = beginMutation();
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(session.session_id)}`,
    ), {
      method: "PATCH",
      body: JSON.stringify({ title, revision: session.revision }),
    });
    const renamed = normalizeSession(await response.json());
    if (!renamed || renamed.session_id !== session.session_id) {
      throw new Error("重命名响应格式无效");
    }
    const index = state.sessions.findIndex((item) => item.session_id === session.session_id);
    if (index >= 0) state.sessions[index] = renamed;
    if (state.sessionId === renamed.session_id) {
      // Rename advances the revision bound into every opaque page cursor.
      // Preserve the draft while obtaining a fresh latest-page cursor instead
      // of keeping a predictably stale "load older" action.
      saveSelectedDraft();
      const restored = await selectSession(renamed.session_id, { preserveTimeline: true });
      if (!restored) throw new Error("会话已重命名，但最新消息页刷新失败");
    } else {
      renderSessionList();
    }
    setStatus(`会话已重命名为“${renamed.title}”`);
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) {
      if (error.status === 409) {
        await refreshSessionSummaries().catch(() => null);
        if (state.sessionId === session.session_id) {
          saveSelectedDraft();
          await selectSession(session.session_id, { preserveTimeline: true }).catch(() => null);
        }
      }
      setStatus(`无法重命名会话：${error.message}`, "failure");
    }
  } finally {
    finishMutation(mutationScope);
  }
}

async function branchSession(sourceSessionId, regenerateMessage = null) {
  if (
    typeof sourceSessionId !== "string" || state.mutationPending ||
    state.activeRuns.has(sourceSessionId) || state.preparingRuns.has(sourceSessionId)
  ) return;
  const mutationScope = beginMutation();
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(sourceSessionId)}/continue`,
    ), {
      method: "POST",
      body: JSON.stringify({ mode: "branch", title: "分支会话" }),
    });
    const body = await response.json();
    const created = normalizeSession(body);
    if (
      !created || body?.relationship?.version !== "conversation-relationship-v1" ||
      body.relationship.type !== "branch" ||
      body.relationship.source_session_id !== sourceSessionId ||
      body.relationship.source_preserved !== true ||
      body.relationship.branch_point !== "completed_head"
    ) {
      throw new Error("分支会话响应格式无效");
    }
    await refreshSessions(created.session_id);
    if (typeof regenerateMessage === "string") {
      prefillTurnMessage(
        regenerateMessage,
        "已创建独立分支并填回原消息；检查后发送会创建新轮次，源会话保持不变",
      );
    } else {
      setStatus("已从最近完成位置创建独立分支；源会话保持不变");
    }
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) {
      setStatus(`无法创建分支：${error.message}`);
    }
  } finally {
    finishMutation(mutationScope);
  }
}

async function createSession() {
  if (state.mutationPending) return;
  const mutationScope = beginMutation();
  try {
    const response = await api(agentApiPath("/sessions"), {
      method: "POST",
      body: JSON.stringify({}),
    });
    const session = normalizeSession(await response.json());
    if (!session) throw new Error("新建会话响应格式无效");
    await refreshSessions(session.session_id);
    setStatus("新会话已创建");
  } catch (error) {
    if (error.status === 409 && mutationScopeIsCurrent(mutationScope)) {
      await refreshSessions(state.sessionId).catch(() => null);
    }
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
}

async function deleteSession(session) {
  if (
    state.mutationPending || state.activeRuns.has(session.session_id) ||
    state.preparingRuns.has(session.session_id)
  ) return;
  if (!window.confirm(`确定删除“${session.title}”及其全部消息吗？此操作不可撤销。`)) return;
  const mutationScope = beginMutation();
  let deleted = false;
  try {
    await api(agentApiPath(`/sessions/${encodeURIComponent(session.session_id)}`), {
      method: "DELETE",
    });
    deleted = true;
    const draftKey = conversationStateKey(session.session_id);
    if (draftKey) state.sessionDrafts.delete(draftKey);
    state.backgroundCompletions.delete(session.session_id);
    const deletedSelectedSession = state.sessionId === session.session_id;
    const preferredSessionId = deletedSelectedSession ? null : state.sessionId;
    state.sessions = state.sessions.filter((item) => item.session_id !== session.session_id);
    if (deletedSelectedSession) clearSelectedSession();
    renderSessionList();
    await refreshSessions(preferredSessionId);
    setStatus("会话已删除");
  } catch (error) {
    if (error.status === 409 && mutationScopeIsCurrent(mutationScope)) {
      await refreshSessions(state.sessionId).catch(() => null);
      setStatus("会话状态已更新；有正在运行的任务时不能删除");
    } else if (mutationScopeIsCurrent(mutationScope)) {
      setStatus(deleted ? `会话已删除；列表刷新失败：${error.message}` : error.message);
    }
  } finally {
    finishMutation(mutationScope);
  }
}

async function restoreLoginSession() {
  const authRequest = ++state.authRequest;
  try {
    const response = await api("/api/auth/status");
    const session = await response.json();
    if (authRequest !== state.authRequest) return;
    if (!session.authenticated) {
      setUnauthenticated();
      return;
    }
    setAuthenticated(session);
  } catch (error) {
    if (authRequest !== state.authRequest) return;
    setUnauthenticated();
    if (error.status !== 401) setStatus("无法读取控制面状态");
    return;
  }
  try {
    await refreshModels();
    if (authRequest !== state.authRequest || state.csrfToken === null) return;
    await loadAgentSurface(session.agent_id, null);
  } catch (error) {
    if (authRequest === state.authRequest && state.csrfToken !== null) {
      setStatus(error.message);
    }
  }
}

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.loginError.textContent = "";
  const token = elements.tokenInput.value;
  elements.tokenInput.value = "";
  const authRequest = ++state.authRequest;
  try {
    const response = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const session = await response.json();
    if (authRequest !== state.authRequest) return;
    setAuthenticated(session);
    try {
      await refreshModels();
      if (authRequest !== state.authRequest || state.csrfToken === null) return;
      await loadAgentSurface(session.agent_id, null);
    } catch (error) {
      if (authRequest === state.authRequest && state.csrfToken !== null) {
        setStatus(error.message);
      }
    }
  } catch (error) {
    if (authRequest === state.authRequest) {
      elements.loginError.textContent = error.message;
    }
  }
});

elements.logoutButton.addEventListener("click", async () => {
  state.authRequest += 1;
  detachAgentBrowserActivity();
  setStatus("正在退出并关闭浏览器连接…", "preparation");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), LOGOUT_TIMEOUT_MS);
  try {
    await api("/api/auth/logout", { method: "POST", signal: controller.signal });
  } catch (_error) {
    // Logout is best effort on the server; local credentials and all browser
    // transports are still removed within the fixed client deadline.
  } finally {
    window.clearTimeout(timeout);
    setUnauthenticated("会话已退出");
  }
});

elements.newSessionButton.addEventListener("click", () => {
  void createSession();
  closeNavigationOnNarrowScreen();
});

elements.navigationToggle.addEventListener("click", () => {
  const open = narrowWorkspace()
    ? !elements.workspace.classList.contains("navigation-open")
    : elements.workspace.classList.contains("sidebar-collapsed");
  setNavigation(open, { focus: true });
});

elements.navigationClose.addEventListener("click", () => setNavigation(false, { focus: true }));

elements.runtimeInspectorButton.addEventListener("click", () => {
  const opening = !elements.workspace.classList.contains("runtime-open");
  setRuntimeInspector(opening, { focus: true });
  const runId = state.selectedTimelineRunId;
  if (
    opening && runId && state.activeRun?.runId !== runId &&
    !state.timelineEntriesByRun.has(runId)
  ) {
    void selectTimelineRun(runId, { scrollTurn: false });
  }
});

elements.runtimeInspectorClose.addEventListener("click", () => {
  setRuntimeInspector(false, { focus: true });
});

elements.workspaceBackdrop.addEventListener("click", () => {
  if (elements.workspace.classList.contains("runtime-open")) {
    setRuntimeInspector(false, { focus: true });
  } else if (elements.workspace.classList.contains("navigation-open")) {
    setNavigation(false, { focus: true });
  }
});

for (const suggestion of document.querySelectorAll("[data-prompt-suggestion]")) {
  suggestion.addEventListener("click", () => {
    if (elements.messageInput.disabled) return;
    elements.messageInput.value = suggestion.dataset.promptSuggestion || "";
    resizeComposer();
    saveSelectedDraft();
    setRunControls();
    elements.messageInput.focus();
  });
}

elements.messageInput.addEventListener("input", () => {
  enforceMessageInputLimit();
  resizeComposer();
  saveSelectedDraft();
  setRunControls();
});
elements.compactInput.addEventListener("change", () => {
  saveSelectedDraft();
  setRunControls();
});
elements.messageInput.addEventListener("keydown", (event) => {
  if (
    event.key !== "Enter" || event.shiftKey || event.isComposing ||
    elements.runButton.disabled
  ) {
    return;
  }
  event.preventDefault();
  elements.runForm.requestSubmit();
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (elements.workspace.classList.contains("runtime-open")) {
    setRuntimeInspector(false, { focus: true });
  } else if (elements.workspace.classList.contains("navigation-open")) {
    setNavigation(false, { focus: true });
  }
});

window.addEventListener("resize", () => {
  if (narrowWorkspace()) {
    elements.workspace.classList.remove("sidebar-collapsed");
    elements.navigationToggle.setAttribute(
      "aria-expanded",
      String(elements.workspace.classList.contains("navigation-open")),
    );
    elements.navigationRail.setAttribute(
      "aria-hidden",
      String(!elements.workspace.classList.contains("navigation-open")),
    );
    elements.navigationRail.inert = !elements.workspace.classList.contains("navigation-open");
  } else {
    elements.workspace.classList.remove("navigation-open");
    elements.navigationToggle.setAttribute(
      "aria-expanded",
      String(!elements.workspace.classList.contains("sidebar-collapsed")),
    );
    elements.navigationRail.setAttribute(
      "aria-hidden",
      String(elements.workspace.classList.contains("sidebar-collapsed")),
    );
    elements.navigationRail.inert = elements.workspace.classList.contains("sidebar-collapsed");
  }
});

elements.newAgentForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void createAgent(elements.newAgentName.value);
});

elements.researchEnvironmentInstall.addEventListener("click", () => {
  void installResearchEnvironment();
});

elements.researchEnvironmentDelete.addEventListener("click", () => {
  void deleteResearchEnvironment();
});

elements.commandResultClose.addEventListener("click", () => {
  elements.commandResult.hidden = true;
  elements.commandResultJson.textContent = "";
});

elements.timelineRunSelect.addEventListener("change", () => {
  void selectTimelineRun(elements.timelineRunSelect.value);
});

elements.replayPrevButton?.addEventListener("click", () => {
  stepTimeline(-1);
});

elements.replayPlayButton?.addEventListener("click", () => {
  if (state.replayTimer === null) startReplay();
  else stopReplay();
});

elements.replayNextButton?.addEventListener("click", () => {
  stepTimeline(1);
});

elements.replayFollowButton?.addEventListener("click", () => {
  toggleFollowLatest();
});

elements.timelineFilterForm?.addEventListener("click", (event) => {
  const control = event.target instanceof Element
    ? event.target.closest("[data-timeline-filter]")
    : null;
  if (control && elements.timelineFilterForm.contains(control)) {
    setTimelineFilter(control.dataset.timelineFilter);
  }
});

function selectInspectorTab(tabName) {
  const tabs = Array.from(document.querySelectorAll("[data-inspector-tab]"));
  const panels = Array.from(document.querySelectorAll("[data-inspector-panel]"));
  if (!tabs.some((tab) => tab.dataset.inspectorTab === tabName)) return;
  for (const tab of tabs) {
    const active = tab.dataset.inspectorTab === tabName;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const panel of panels) panel.hidden = panel.dataset.inspectorPanel !== tabName;
}

for (const tab of document.querySelectorAll("[data-inspector-tab]")) {
  tab.addEventListener("click", () => selectInspectorTab(tab.dataset.inspectorTab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = Array.from(document.querySelectorAll("[data-inspector-tab]"));
    const current = tabs.indexOf(tab);
    let target = current;
    if (event.key === "Home") target = 0;
    else if (event.key === "End") target = tabs.length - 1;
    else if (event.key === "ArrowLeft") target = (current - 1 + tabs.length) % tabs.length;
    else target = (current + 1) % tabs.length;
    event.preventDefault();
    selectInspectorTab(tabs[target].dataset.inspectorTab);
    tabs[target].focus();
  });
}

elements.eventInspectorContextButton?.addEventListener("click", () => {
  void inspectSelectedRunContext(elements.eventInspectorContextButton);
});

elements.contextInspectButton.addEventListener("click", () => {
  void inspectSelectedRunContext(elements.contextInspectButton);
});

elements.contextInspectClose.addEventListener("click", () => {
  elements.contextInspectDialog.close();
});

elements.contextInspectDialog.addEventListener("click", (event) => {
  if (event.target === elements.contextInspectDialog) elements.contextInspectDialog.close();
});

elements.contextInspectDialog.addEventListener("close", () => {
  state.contextRequest += 1;
  state.contextLoading = false;
  state.contextDialogTrigger?.focus();
  state.contextDialogTrigger = null;
  setContextInspectControl();
});

function eventPresentation(kind) {
  return Object.prototype.hasOwnProperty.call(EVENT_PRESENTATIONS, kind)
    ? EVENT_PRESENTATIONS[kind]
    : UNKNOWN_EVENT_PRESENTATION;
}

function shortEventIdentifier(value) {
  if (typeof value !== "string" || value.length === 0) return null;
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}

function eventSummary(envelope) {
  const payload = envelope?.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return "事件元数据不可用";
  }
  if (envelope.kind === "model.request.started") {
    const iteration = payload.iteration;
    const messageCount = payload.message_count;
    const toolCount = payload.tool_count;
    const toolResults = payload.tool_result_call_ids;
    if (
      !Number.isSafeInteger(iteration) || iteration < 1 ||
      !Number.isSafeInteger(messageCount) || messageCount < 0 ||
      !Number.isSafeInteger(toolCount) || toolCount < 0 ||
      !Array.isArray(toolResults) || toolResults.length > 3 ||
      toolResults.some((callId) => (
        typeof callId !== "string" || callId.length === 0 || callId.length > 64
      ))
    ) {
      return "模型请求元数据不可用";
    }
    const attempt = Number.isSafeInteger(payload.attempt) ? payload.attempt : 0;
    const providerCall = Number.isSafeInteger(payload.provider_call_index)
      ? ` · provider #${payload.provider_call_index}` : "";
    return (
      `模型调用 #${iteration} / attempt ${attempt}${providerCall} · ` +
      `${messageCount} 条消息 · ${toolCount} tools · ` +
      `${toolResults.length} 个 tool result`
    );
  }
  if (envelope.kind === "model.transport.attempt") {
    const attempt = payload.attempt;
    const maximum = payload.max_attempts;
    const phase = payload.phase;
    if (
      !Number.isSafeInteger(attempt) || !Number.isSafeInteger(maximum) ||
      attempt < 1 || maximum < attempt || maximum > 4 ||
      !["attempt_started", "attempt_finished"].includes(phase)
    ) {
      return "模型传输元数据不可用";
    }
    if (phase === "attempt_started") {
      return `正在进行第 ${attempt}/${maximum} 次模型连接尝试`;
    }
    const elapsed = Number.isSafeInteger(payload.elapsed_ms)
      ? `${(payload.elapsed_ms / 1000).toFixed(1)} 秒`
      : "未知时长";
    return payload.outcome === "first_frame_received"
      ? `第 ${attempt}/${maximum} 次尝试已在 ${elapsed} 收到首帧`
      : `第 ${attempt}/${maximum} 次尝试结束 · ${payload.outcome || "未知原因"} · ${elapsed}`;
  }
  if (envelope.kind === "model.response.finished") {
    const iteration = payload.iteration;
    const outcome = payload.outcome;
    const inputTokens = payload.input_tokens;
    const outputTokens = payload.output_tokens;
    if (
      !Number.isSafeInteger(iteration) || iteration < 1 ||
      ![
        "tool_use", "end_turn", "repetition_truncated", "error", "cancelled",
      ].includes(outcome) ||
      !isNonNegativeInteger(inputTokens) || !isNonNegativeInteger(outputTokens)
    ) {
      return "模型响应元数据不可用";
    }
    if (outcome === "repetition_truncated") {
      if (
        inputTokens !== 0 || outputTokens !== 0 ||
        payload.usage_complete !== false || payload.error_code !== null
      ) {
        return "模型响应元数据不可用";
      }
      return "检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，Provider 用量不完整";
    }
    const attempt = Number.isSafeInteger(payload.attempt) ? payload.attempt : 0;
    const providerCall = Number.isSafeInteger(payload.provider_call_index)
      ? ` · provider #${payload.provider_call_index}` : "";
    return (
      `模型调用 #${iteration} / attempt ${attempt}${providerCall} · ${outcome} · ` +
      `${inputTokens} input / ${outputTokens} output tokens`
    );
  }
  if (envelope.kind === "model.recovery.started") {
    if (
      !Number.isSafeInteger(payload.iteration) || payload.iteration < 1 ||
      payload.attempt !== 1 ||
      !["model_context_overflow", "model_media_overflow"].includes(payload.overflow_code)
    ) {
      return "模型恢复元数据不可用";
    }
    return `模型调用 #${payload.iteration} · ${payload.overflow_code} · 单次恢复`;
  }
  if (envelope.kind === "run.started") {
    const context = payload.context_plan;
    if (
      !context || typeof context !== "object" || Array.isArray(context) ||
      !isNonNegativeInteger(context.included_history_message_count) ||
      !isNonNegativeInteger(context.history_message_count) ||
      ![
        "full", "completed-turn-tail-v1", "completed-turn-collapse-v2",
        "semantic-summary-v1", "semantic-summary-v2",
      ].includes(context.windowing_strategy)
    ) {
      return "上下文窗口元数据不可用";
    }
    return (
      `历史纳入 ${context.included_history_message_count} / ` +
      `${context.history_message_count} · ${context.windowing_strategy}`
    );
  }
  if (envelope.kind === "run.completed") {
    if (![
      "end_turn", "max_output", "repetition_truncated",
    ].includes(payload.reason)) {
      return "运行完成原因不可用";
    }
    if (payload.reason === "repetition_truncated") {
      return "检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，Provider 用量不完整";
    }
    return payload.reason === "max_output"
      ? "回答达到模型输出长度上限；此前正文已保留，可继续对话"
      : "模型正常结束回答；运行已提交";
  }
  if (envelope.kind.startsWith("tool.call.")) {
    const toolId = shortEventIdentifier(payload.tool_id);
    const callId = shortEventIdentifier(payload.call_id);
    if (!callId) return "工具调用标识不可用";
    return `tool: ${toolId || "恢复事件未提供"} · call: ${callId}`;
  }
  return eventPresentation(envelope.kind).explanation;
}

function showEventDetail(envelope, trigger) {
  const presentation = eventPresentation(envelope.kind);
  state.eventDetailTrigger = trigger;
  elements.eventDetailSummary.textContent = [
    `#${envelope.seq} · ${envelope.kind}`,
    `主体：${presentation.subject}`,
    `方向：${presentation.direction}`,
    `动作：${presentation.action}`,
    `映射说明：${presentation.explanation}`,
  ].join("\n");
  elements.eventDetailJson.textContent = JSON.stringify(envelope, null, 2);
}

elements.eventDetailClose.addEventListener("click", () => {
  elements.eventDetailDialog.close();
});

elements.eventDetailDialog.addEventListener("click", (event) => {
  if (event.target === elements.eventDetailDialog) elements.eventDetailDialog.close();
});

elements.eventDetailDialog.addEventListener("close", () => {
  state.eventDetailTrigger?.focus();
  state.eventDetailTrigger = null;
});

function selectedRunTurnFact() {
  const runId = state.selectedTimelineRunId;
  if (!runId) return null;
  const messages = state.conversationMessages.filter((message) => message.run_id === runId);
  const user = messages.find((message) => message.role === "user");
  if (!user) return null;
  const run = state.timelineRuns.find((candidate) => candidate.runId === runId);
  return {
    key: `turn-user:${runId}`,
    type: "derived-user",
    runId,
    turnId: user.turn_id || run?.turnId || null,
    turnNumber: run?.turnNumber || null,
    userMessage: user,
  };
}

function timelineEntriesForCurrentRun() {
  const fact = selectedRunTurnFact();
  return fact ? [fact, ...state.timelineEntries] : [...state.timelineEntries];
}

function timelineCategory(entry) {
  if (entry.type === "derived-user") return "user";
  const kind = entry.envelope.kind;
  const payload = entry.envelope.payload;
  if (
    ["run.failed", "run.cancelled", "assistant.block.discarded", "stream.gap"].includes(kind) ||
    (kind === "model.response.finished" && ["error", "cancelled"].includes(payload?.outcome)) ||
    (kind === "tool.call.finished" && payload?.outcome !== "succeeded")
  ) {
    return "error";
  }
  if (kind.startsWith("model.") || kind.startsWith("assistant.")) return "llm";
  if (kind.startsWith("tool.")) return "tool";
  return "harness";
}

function timelineEntryMatchesFilter(entry) {
  return state.timelineFilter === "all" || timelineCategory(entry) === state.timelineFilter;
}

function visibleTimelineEntries() {
  return timelineEntriesForCurrentRun().filter(timelineEntryMatchesFilter);
}

function eventFlow(entry) {
  if (entry.type === "derived-user") {
    return {
      source: "user",
      target: "harness",
      subject: "User（UI 投影）",
      direction: "User → Harness",
      action: "提交本轮用户消息",
      tone: "user",
    };
  }
  const kind = entry.envelope.kind;
  if (["stream.gap", "stream.snapshot"].includes(kind)) {
    return { source: "replay", target: "replay", ...eventPresentation(kind) };
  }
  if (kind === "model.request.started") {
    return { source: "harness", target: "llm", ...eventPresentation(kind) };
  }
  if (kind === "model.transport.attempt") {
    return entry.envelope.payload?.phase === "attempt_started"
      ? { source: "harness", target: "llm", ...eventPresentation(kind) }
      : { source: "llm", target: "harness", ...eventPresentation(kind) };
  }
  if (
    kind === "model.response.finished" || kind.startsWith("assistant.block.") ||
    kind === "tool.call.requested"
  ) {
    if (kind === "assistant.block.discarded") {
      return { source: "harness", target: "harness", ...eventPresentation(kind) };
    }
    return { source: "llm", target: "harness", ...eventPresentation(kind) };
  }
  if (kind === "tool.call.started") {
    return { source: "harness", target: "tool", ...eventPresentation(kind) };
  }
  if (kind === "tool.call.finished") {
    return { source: "tool", target: "harness", ...eventPresentation(kind) };
  }
  return { source: "harness", target: "harness", ...eventPresentation(kind) };
}

function createSequenceConnector(flow, markerId) {
  const namespace = "http://www.w3.org/2000/svg";
  const laneCenter = { user: 12.5, harness: 37.5, llm: 62.5, tool: 87.5 };
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("class", "sequence-connector");
  svg.setAttribute("viewBox", "0 0 100 36");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  svg.dataset.fromLane = flow.source;
  svg.dataset.toLane = flow.target;
  if (flow.source === "replay" || flow.target === "replay") {
    svg.classList.add("sequence-replay-rail");
    return svg;
  }

  const definitions = document.createElementNS(namespace, "defs");
  const marker = document.createElementNS(namespace, "marker");
  marker.setAttribute("id", markerId);
  marker.setAttribute("viewBox", "0 0 8 8");
  marker.setAttribute("refX", "7");
  marker.setAttribute("refY", "4");
  marker.setAttribute("markerWidth", "6");
  marker.setAttribute("markerHeight", "6");
  marker.setAttribute("orient", "auto");
  const arrowHead = document.createElementNS(namespace, "path");
  arrowHead.setAttribute("d", "M 0 0 L 8 4 L 0 8 z");
  arrowHead.setAttribute("class", "sequence-arrow-head");
  marker.append(arrowHead);
  definitions.append(marker);

  const path = document.createElementNS(namespace, "path");
  const sourceX = laneCenter[flow.source];
  const targetX = laneCenter[flow.target];
  path.setAttribute(
    "d",
    sourceX === targetX
      ? `M ${sourceX} 27 C ${sourceX + 8} 27 ${sourceX + 8} 9 ${sourceX} 9`
      : `M ${sourceX} 18 L ${targetX} 18`,
  );
  path.setAttribute("class", "sequence-arrow-line");
  path.setAttribute("marker-end", `url(#${markerId})`);
  svg.append(definitions, path);
  return svg;
}

function conversationBodyForRun(runId) {
  const messages = state.conversationMessages.filter((message) => (
    message.run_id === runId && ["user", "assistant"].includes(message.role)
  ));
  if (messages.length === 0) return "ConversationStore 中没有可展示的本轮消息正文。";
  return messages.map((message) => (
    `${message.role === "user" ? "用户消息" : "智能体消息"}：\n${message.content}`
  )).join("\n\n");
}

function eventBusinessBody(entry) {
  const conversationBody = conversationBodyForRun(entry.runId);
  if (entry.type === "derived-user") {
    return [
      "来源：ConversationStore 的 Turn 事实（UI 投影，不是 EventEnvelope）",
      conversationBody,
    ].join("\n\n");
  }
  const { kind, payload } = entry.envelope;
  let eventBody = "该事件没有独立的业务正文。";
  if (kind === "assistant.block.finished" && typeof payload?.content === "string") {
    eventBody = `事件中的完整 assistant 内容：\n${payload.content}`;
  } else if (kind === "tool.call.requested" && payload?.arguments) {
    eventBody = `事件中的 Tool arguments：\n${JSON.stringify(payload.arguments, null, 2)}`;
  } else if (kind === "tool.call.finished" && typeof payload?.result === "string") {
    eventBody = `事件中的 Tool result：\n${payload.result}`;
  } else if (kind === "model.request.started") {
    eventBody = "模型原始请求正文按协议未持久化；这里只存在边界元数据和摘要。";
  } else if (kind === "model.transport.attempt") {
    eventBody = "这里只记录首帧等待的尝试次数和耗时；不保存 endpoint、prompt 或模型原始报文。";
  } else if (kind === "model.response.finished") {
    eventBody = payload?.outcome === "repetition_truncated"
      ? "检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，Provider 用量不完整。"
      : payload?.error_code && MODEL_ERROR_LABELS[payload.error_code]
      ? `模型失败阶段：${MODEL_ERROR_LABELS[payload.error_code]}`
      : "模型供应商原始响应正文按协议未持久化；智能体正文来自规范 assistant 事件。";
  } else if (kind === "run.completed" && payload?.reason === "max_output") {
    eventBody = (
      "模型达到本轮输出长度上限；Harness 已保留有界正文并追加可信截断标记，" +
      "Provider 终帧用量仍完整结算。"
    );
  } else if (kind === "run.completed" && payload?.reason === "repetition_truncated") {
    eventBody = (
      "检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，" +
      "Provider 用量不完整。"
    );
  } else if (["stream.gap", "stream.snapshot"].includes(kind)) {
    eventBody = "这是 Replay control，不是 Run 业务消息，也不是模型报文。";
  }
  return [`ConversationStore 中本轮可展示的正文：\n${conversationBody}`, eventBody].join("\n\n");
}

function clearEventInspector() {
  if (elements.eventInspectorEmpty) elements.eventInspectorEmpty.hidden = false;
  if (elements.eventInspectorSummary) elements.eventInspectorSummary.textContent = "";
  if (elements.eventInspectorBusiness) elements.eventInspectorBusiness.textContent = "";
  if (elements.eventInspectorPayload) elements.eventInspectorPayload.textContent = "";
  if (elements.eventInspectorEnvelope) elements.eventInspectorEnvelope.textContent = "";
  if (elements.eventInspectorContextButton) elements.eventInspectorContextButton.disabled = true;
  elements.eventDetailSummary.textContent = "";
  elements.eventDetailJson.textContent = "";
}

function renderEventInspector(entry, trigger = null) {
  if (!entry) {
    clearEventInspector();
    return;
  }
  const flow = eventFlow(entry);
  const sequence = entry.type === "derived-user" ? "Turn fact（无 canonical seq）" : `#${entry.envelope.seq}`;
  const kind = entry.type === "derived-user" ? "turn.user.submitted（UI projection）" : entry.envelope.kind;
  const summary = [
    `${sequence} · ${kind}`,
    `主体：${flow.subject}`,
    `方向：${flow.direction}`,
    `动作：${flow.action}`,
    entry.type === "derived-user"
      ? "说明：来自 ConversationStore，帮助补齐用户进入 Harness 的时序；它不是规范事件信封。"
      : `说明：${flow.explanation}`,
  ].join("\n");
  if (elements.eventInspectorEmpty) elements.eventInspectorEmpty.hidden = true;
  if (elements.eventInspectorSummary) elements.eventInspectorSummary.textContent = summary;
  if (elements.eventInspectorBusiness) {
    elements.eventInspectorBusiness.textContent = eventBusinessBody(entry);
  }
  if (elements.eventInspectorPayload) {
    elements.eventInspectorPayload.textContent = entry.type === "derived-user"
      ? "无 payload：这是 ConversationStore 的 UI 投影。"
      : JSON.stringify(entry.envelope.payload, null, 2);
  }
  if (elements.eventInspectorEnvelope) {
    elements.eventInspectorEnvelope.textContent = entry.type === "derived-user"
      ? "无 canonical EventEnvelope：此节点是明确标注的 derived Turn fact。"
      : entry.type === "replay-control"
        ? `Replay control frame（不是 canonical EventEnvelope）：\n${JSON.stringify(entry.envelope, null, 2)}`
        : JSON.stringify(entry.envelope, null, 2);
  }
  if (elements.eventInspectorContextButton) {
    elements.eventInspectorContextButton.disabled = (
      !RUN_ID_PATTERN.test(entry.runId) || state.csrfToken === null ||
      state.contextLoading || state.sessionLoading
    );
  }
  if (entry.type === "event") {
    state.eventDetailTrigger = trigger;
    elements.eventDetailSummary.textContent = summary;
    elements.eventDetailJson.textContent = JSON.stringify(entry.envelope, null, 2);
  }
}

function selectTimelineEntry(key, { manual = true, scrollNode = true } = {}) {
  const entry = timelineEntriesForCurrentRun().find((candidate) => candidate.key === key);
  if (!entry) return;
  if (manual) {
    stopReplay();
    state.followLatest = false;
  }
  state.selectedTimelineEntryKey = key;
  if (state.selectedTimelineRunId) {
    state.selectedTimelineEntryKeyByRun.set(state.selectedTimelineRunId, key);
  }
  for (const item of elements.eventList.querySelectorAll(".sequence-step")) {
    const selected = item.dataset.entryKey === key;
    item.classList.toggle("selected", selected);
    item.querySelector(".sequence-node")?.setAttribute("aria-pressed", String(selected));
    if (selected && scrollNode) item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  syncSelectedTurn(true);
  renderEventInspector(entry, document.activeElement);
  setReplayControls();
}

function renderTimelineEntries() {
  const entries = timelineEntriesForCurrentRun();
  const visible = entries.filter(timelineEntryMatchesFilter);
  if (state.followLatest && visible.length > 0) {
    state.selectedTimelineEntryKey = visible.at(-1).key;
  } else if (
    state.selectedTimelineEntryKey !== null &&
    !entries.some((entry) => entry.key === state.selectedTimelineEntryKey)
  ) {
    state.selectedTimelineEntryKey = null;
  }
  if (state.selectedTimelineRunId) {
    if (state.selectedTimelineEntryKey) {
      state.selectedTimelineEntryKeyByRun.set(
        state.selectedTimelineRunId,
        state.selectedTimelineEntryKey,
      );
    } else {
      state.selectedTimelineEntryKeyByRun.delete(state.selectedTimelineRunId);
    }
  }
  elements.eventList.replaceChildren();
  entries.forEach((entry, index) => {
    const flow = eventFlow(entry);
    const envelope = entry.envelope;
    const item = document.createElement("li");
    item.className = `sequence-step lane-${flow.source}`;
    item.dataset.entryKey = entry.key;
    item.dataset.fromLane = flow.source;
    item.dataset.toLane = flow.target;
    item.dataset.kind = entry.type === "derived-user" ? "turn.user.submitted" : envelope.kind;
    item.dataset.seq = entry.type === "derived-user" ? "turn" : String(envelope.seq);
    item.hidden = !timelineEntryMatchesFilter(entry);
    if (entry.key === state.selectedTimelineEntryKey) item.classList.add("selected");

    const markerId = `sequence-arrow-${index}-${state.timelineEntrySerial}`;
    const connector = createSequenceConnector(flow, markerId);
    const detailButton = document.createElement("button");
    detailButton.type = "button";
    detailButton.className = `sequence-node event-entry event-tone-${flow.tone} lane-${flow.source}`;
    detailButton.setAttribute("aria-pressed", String(entry.key === state.selectedTimelineEntryKey));
    detailButton.setAttribute(
      "aria-label",
      entry.type === "derived-user"
        ? `${flow.direction}；${flow.action}；查看 ConversationStore 消息正文`
        : entry.type === "replay-control"
          ? `Replay control #${envelope.seq}；${flow.action}；查看控制帧消息体`
          : `事件 #${envelope.seq}；${flow.direction}；${flow.action}；查看规范事件消息体`,
    );
    const sequence = document.createElement("span");
    sequence.className = "seq";
    sequence.textContent = entry.type === "derived-user" ? "TURN" : `#${envelope.seq}`;
    const semantics = document.createElement("span");
    semantics.className = "event-semantics";
    const subject = document.createElement("span");
    subject.className = "event-subject";
    subject.textContent = `主体：${flow.subject}`;
    const direction = document.createElement("span");
    direction.className = "event-direction";
    direction.textContent = `方向：${flow.direction}`;
    const action = document.createElement("strong");
    action.className = "event-action";
    action.textContent = `动作：${flow.action}`;
    const kind = document.createElement("code");
    kind.className = "event-kind";
    if (entry.type === "derived-user") {
      kind.textContent = "turn.user.submitted · UI projection";
    } else {
      kind.textContent = envelope.kind;
    }
    const summary = document.createElement("span");
    summary.className = "event-summary";
    if (entry.type === "derived-user") {
      summary.textContent = "来源：ConversationStore；不是 EventEnvelope";
    } else {
      summary.textContent = eventSummary(envelope);
    }
    semantics.append(subject, direction, action, kind, summary);
    detailButton.append(sequence, semantics);
    detailButton.addEventListener("click", () => selectTimelineEntry(entry.key));
    item.append(connector, detailButton);
    elements.eventList.append(item);
  });
  state.eventCount = state.timelineEntries.length;
  elements.eventCount.textContent = `${state.eventCount} events${selectedRunTurnFact() ? " · 1 Turn fact" : ""}`;
  elements.runtimeEventBadge.textContent = String(state.eventCount);
  const selected = entries.find((entry) => entry.key === state.selectedTimelineEntryKey) || null;
  renderEventInspector(selected);
  setReplayControls();
}

function addTimelineEvent(envelope) {
  if (envelope.conversation_id === state.sessionId) {
    captureSessionContextUsage(envelope);
    captureSessionTurnUsage(envelope);
  }
  const serial = state.timelineEntrySerial;
  state.timelineEntrySerial += 1;
  const eventIdentity = typeof envelope.event_id === "string" && envelope.event_id
    ? envelope.event_id
    : `${envelope.kind}:${envelope.seq}:${serial}`;
  const entry = {
    key: `event:${eventIdentity}`,
    type: envelope.kind.startsWith("stream.") ? "replay-control" : "event",
    runId: envelope.run_id,
    turnId: envelope.turn_id,
    envelope,
  };
  const entries = state.timelineEntriesByRun.get(envelope.run_id) || [];
  entries.push(entry);
  state.timelineEntriesByRun.set(envelope.run_id, entries);
  if (envelope.run_id === state.selectedTimelineRunId) {
    state.timelineEntries = entries;
    renderTimelineEntries();
  }
}

function stopReplay() {
  if (state.replayTimer !== null) {
    window.clearInterval(state.replayTimer);
    state.replayTimer = null;
  }
  if (elements.replayPlayButton) elements.replayPlayButton.textContent = "重播";
  setReplayControls();
}

function setReplayControls() {
  const visible = visibleTimelineEntries();
  const selectedIndex = visible.findIndex((entry) => entry.key === state.selectedTimelineEntryKey);
  if (elements.replayPrevButton) {
    elements.replayPrevButton.disabled = visible.length === 0 || selectedIndex <= 0;
  }
  if (elements.replayNextButton) {
    elements.replayNextButton.disabled = (
      visible.length === 0 || selectedIndex < 0 || selectedIndex >= visible.length - 1
    );
  }
  if (elements.replayPlayButton) {
    elements.replayPlayButton.disabled = visible.length === 0;
    elements.replayPlayButton.textContent = state.replayTimer === null ? "重播" : "停止重播";
  }
  if (elements.replayFollowButton) {
    elements.replayFollowButton.disabled = visible.length === 0;
    elements.replayFollowButton.setAttribute("aria-pressed", String(state.followLatest));
    elements.replayFollowButton.textContent = state.followLatest ? "正在跟随最新" : "跟随最新";
  }
  if (elements.timelineFilterForm) {
    for (const control of elements.timelineFilterForm.querySelectorAll("[data-timeline-filter]")) {
      const active = control.dataset.timelineFilter === state.timelineFilter;
      control.classList.toggle("active", active);
      if (control.matches("button")) control.setAttribute("aria-pressed", String(active));
      if (control.matches('input[type="radio"], input[type="checkbox"]')) {
        control.checked = active;
      }
    }
  }
}

function stepTimeline(offset) {
  stopReplay();
  const visible = visibleTimelineEntries();
  if (visible.length === 0) return;
  const selectedIndex = visible.findIndex((entry) => entry.key === state.selectedTimelineEntryKey);
  const fallback = offset > 0 ? -1 : visible.length;
  const targetIndex = Math.max(0, Math.min(visible.length - 1, (selectedIndex < 0 ? fallback : selectedIndex) + offset));
  selectTimelineEntry(visible[targetIndex].key, { manual: true, scrollNode: true });
}

function startReplay() {
  stopReplay();
  const visible = visibleTimelineEntries();
  if (visible.length === 0) return;
  state.followLatest = false;
  let index = 0;
  selectTimelineEntry(visible[index].key, { manual: false, scrollNode: true });
  if (visible.length === 1) {
    setReplayControls();
    return;
  }
  state.replayTimer = window.setInterval(() => {
    index += 1;
    const current = visibleTimelineEntries();
    if (index >= current.length) {
      stopReplay();
      return;
    }
    selectTimelineEntry(current[index].key, { manual: false, scrollNode: true });
  }, 700);
  setReplayControls();
}

function toggleFollowLatest() {
  stopReplay();
  state.followLatest = !state.followLatest;
  if (state.followLatest) {
    const latest = visibleTimelineEntries().at(-1);
    if (latest) selectTimelineEntry(latest.key, { manual: false, scrollNode: true });
  }
  setReplayControls();
}

function setTimelineFilter(filter) {
  if (!["all", "llm", "tool", "error"].includes(filter)) return;
  stopReplay();
  state.timelineFilter = filter;
  const visible = visibleTimelineEntries();
  if (
    state.selectedTimelineEntryKey !== null &&
    !visible.some((entry) => entry.key === state.selectedTimelineEntryKey)
  ) {
    state.selectedTimelineEntryKey = visible[0]?.key || null;
  }
  renderTimelineEntries();
}

function ensureLiveAssistant() {
  if (!state.liveAssistantContent) {
    state.liveAssistantContent = appendMessage("assistant", "", {
      runId: state.activeRun?.runId,
      turnStatus: "running",
      live: true,
    });
    state.liveAssistantContent.textContent = "";
  }
  return state.liveAssistantContent;
}

function syncLiveAssistantMessage() {
  if (state.liveAssistantMessage) {
    state.liveAssistantMessage.content = Array.from(state.blocks.values())
      .map((block) => block.content)
      .join("");
  }
}

function stopTransportWait(runContext) {
  if (runContext?.transportTimer !== null && runContext?.transportTimer !== undefined) {
    window.clearInterval(runContext.transportTimer);
    runContext.transportTimer = null;
  }
  if (runContext) runContext.transportAttempt = null;
}

function startTransportWait(runContext, payload) {
  stopTransportWait(runContext);
  runContext.transportAttempt = {
    attempt: payload.attempt,
    maximum: payload.max_attempts,
    startedAt: performance.now(),
  };
  const update = () => {
    if (!runIsTracked(runContext) || !runContext.transportAttempt) {
      stopTransportWait(runContext);
      return;
    }
    const elapsedSeconds = Math.max(
      0,
      Math.floor((performance.now() - runContext.transportAttempt.startedAt) / 1000),
    );
    if (state.sessionId === runContext.sessionId) {
      setStatus(
        `正在等待模型首帧 · 第 ${payload.attempt}/${payload.max_attempts} 次尝试 · ` +
        `已等待 ${elapsedSeconds} 秒；可以停止本轮`,
      );
    }
  };
  update();
  runContext.transportTimer = window.setInterval(update, 1000);
}

function runAssistantContent(runContext) {
  return Array.from(runContext.assistantBlocks?.values() || [])
    .map((block) => block.content)
    .join("");
}

function renderRunAssistant(runContext) {
  if (state.sessionId !== runContext.sessionId) return;
  // A terminal refresh replaces the optimistic transcript with the canonical
  // ConversationStore page before the tracked Run is retired.  Do not append
  // the buffered live assistant beside that already-committed assistant: the
  // two records have different client identities (turn_id vs. run_id), so the
  // UI would otherwise render them as consecutive, identical Turn cards.
  const canonicalAssistant = state.conversationMessages.some((message) => (
    message.run_id === runContext.runId && message.role === "assistant" &&
    message.live !== true
  ));
  if (canonicalAssistant) return;
  const content = runAssistantContent(runContext);
  if (!content && (!runContext.assistantBlocks || runContext.assistantBlocks.size === 0)) {
    return;
  }
  const target = ensureLiveAssistant();
  if (state.liveAssistantMessage) state.liveAssistantMessage.content = content;
  const complete = Array.from(runContext.assistantBlocks.values())
    .every((block) => block.finished === true);
  if (complete) renderRestrictedMarkdown(target, content);
  else target.textContent = content;
  scheduleConversationLatest();
}

function captureRunAssistant(envelope, runContext) {
  if (!envelope.kind.startsWith("assistant.block.")) return;
  if (!runContext.assistantBlocks) runContext.assistantBlocks = new Map();
  const payload = envelope.payload || {};
  const blockId = payload.block_id;
  if (typeof blockId !== "string") return;
  if (envelope.kind === "assistant.block.started") {
    runContext.assistantBlocks.set(blockId, { content: "", finished: false });
  } else if (envelope.kind === "assistant.block.delta") {
    const block = runContext.assistantBlocks.get(blockId);
    if (block && typeof payload.text === "string") block.content += payload.text;
  } else if (envelope.kind === "assistant.block.finished") {
    const block = runContext.assistantBlocks.get(blockId);
    if (block) {
      if (typeof payload.content === "string") block.content = payload.content;
      block.finished = true;
    }
  } else if (envelope.kind === "assistant.block.discarded") {
    runContext.assistantBlocks.delete(blockId);
  }
  renderRunAssistant(runContext);
}

function renderEnvelope(envelope, runContext) {
  if (
    !envelope || envelope.run_id !== runContext.runId ||
    !Number.isSafeInteger(envelope.seq) || envelope.seq < 1 ||
    typeof envelope.kind !== "string"
  ) {
    throw new Error("事件流消息格式无效");
  }
  if (envelope.seq <= runContext.lastSeq) return;
  if (envelope.seq !== runContext.lastSeq + 1) throw new Error("事件流序号不连续");
  runContext.lastSeq = envelope.seq;
  addTimelineEvent(envelope);
  const payload = envelope.payload || {};
  const foreground = state.sessionId === runContext.sessionId;
  captureRunAssistant(envelope, runContext);
  if (foreground && envelope.kind === "model.request.started") {
    setStatus("模型请求已提交，正在等待首个响应帧…");
  } else if (envelope.kind === "model.transport.attempt") {
    if (payload.phase === "attempt_started") {
      startTransportWait(runContext, payload);
    } else {
      stopTransportWait(runContext);
      const elapsed = Number.isSafeInteger(payload.elapsed_ms)
        ? `${(payload.elapsed_ms / 1000).toFixed(1)} 秒`
        : "未知时长";
      if (!foreground) {
        // The attempt remains visible in its owning Run timeline. Do not
        // overwrite the selected Conversation's composer status.
      } else if (payload.outcome === "first_frame_received") {
        setStatus(`模型已在 ${elapsed} 返回首帧，正在生成回答…`);
      } else if (payload.attempt < payload.max_attempts) {
        setStatus(`第 ${payload.attempt}/${payload.max_attempts} 次尝试未返回首帧，正在重试…`);
      } else {
        setStatus(
          `模型连接尝试已结束：${TRANSPORT_OUTCOME_LABELS[payload.outcome] || "未收到首个响应"}`,
          "failure",
        );
      }
    }
  } else if (foreground && envelope.kind === "model.response.finished" && payload.error_code) {
    setStatus(modelErrorLabel(payload.error_code), "failure");
  } else if (foreground && envelope.kind === "assistant.block.started") {
    setStatus("模型正在流式生成回答…");
  }
  if (foreground && envelope.kind.startsWith("assistant.block.")) {
    const selected = timelineEntriesForCurrentRun()
      .find((entry) => entry.key === state.selectedTimelineEntryKey) || null;
    renderEventInspector(selected);
  }

  if (TERMINAL_EVENTS.has(envelope.kind)) {
    stopTransportWait(runContext);
    runContext.terminalSeen = true;
    runContext.terminalKind = envelope.kind;
    runContext.terminalPayload = payload;
    updateTimelineRunTerminalStatus(runContext.runId, envelope.kind);
    setRunControls();
  }
}

function decodeSseFrame(frame) {
  let data = "";
  let event = "message";
  let eventId = null;
  for (const line of frame.replaceAll("\r", "").split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trimStart();
    if (line.startsWith("id:")) eventId = line.slice(3).trimStart();
    if (line.startsWith("data:")) data += `${line.slice(5).trimStart()}\n`;
  }
  if (!data) return null;
  return { event, eventId, data: JSON.parse(data.slice(0, -1)) };
}

function renderSseFrame(frame, runContext) {
  const payload = frame?.data;
  if (frame?.event === "stream.gap") {
    const fromSeq = payload?.from_seq;
    const toSeq = payload?.to_seq;
    if (
      payload?.control_version !== STREAM_CONTROL_VERSION ||
      payload?.run_id !== runContext.runId ||
      !Number.isSafeInteger(fromSeq) || !Number.isSafeInteger(toSeq) ||
      fromSeq !== runContext.lastSeq + 1 || toSeq < fromSeq ||
      payload.resume_cursor !== toSeq ||
      (payload.reason === "retention"
        ? frame.eventId !== null
        : frame.eventId !== String(toSeq))
    ) {
      throw new Error("事件流缺口控制帧无效");
    }
    if (payload.reason !== "retention") runContext.lastSeq = toSeq;
    addTimelineControl(frame.event, payload, toSeq, runContext.sessionId);
    return;
  }
  if (frame?.event === "stream.snapshot") {
    const cursor = payload?.cursor;
    const terminalKind = payload?.snapshot?.document?.terminal?.kind;
    if (
      payload?.control_version !== STREAM_CONTROL_VERSION ||
      payload?.run_id !== runContext.runId ||
      payload?.availability !== "snapshot_only" ||
      !Number.isSafeInteger(cursor) || cursor < runContext.lastSeq ||
      frame.eventId !== String(cursor) ||
      !TERMINAL_EVENTS.has(terminalKind)
    ) {
      throw new Error("事件流快照控制帧无效");
    }
    runContext.lastSeq = cursor;
    addTimelineControl(frame.event, payload, cursor, runContext.sessionId);
    runContext.terminalSeen = true;
    runContext.terminalKind = terminalKind;
    stopTransportWait(runContext);
    updateTimelineRunTerminalStatus(runContext.runId, terminalKind);
    setRunControls();
    return;
  }
  if (
    !payload || frame?.event !== payload.kind ||
    frame.eventId !== String(payload.seq)
  ) {
    throw new Error("事件流消息类型无效");
  }
  renderEnvelope(payload, runContext);
}

async function consumeEventStream(url, runContext) {
  if (!runIsTracked(runContext)) throw abortError();
  const controller = new AbortController();
  runContext.controller = controller;
  const headers = { Accept: "text/event-stream" };
  if (runContext.lastSeq > 0) headers["Last-Event-ID"] = String(runContext.lastSeq);
  try {
    const response = await api(url, { headers, signal: controller.signal });
    if (!runIsTracked(runContext)) throw abortError();
    if (!response.body) throw new Error("浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (!runIsTracked(runContext)) {
        await reader.cancel().catch(() => null);
        throw abortError();
      }
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const normalized = buffer.replaceAll("\r\n", "\n");
      const frames = normalized.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        if (!runIsTracked(runContext)) {
          await reader.cancel().catch(() => null);
          throw abortError();
        }
        const decoded = decodeSseFrame(frame);
        if (decoded) renderSseFrame(decoded, runContext);
      }
      if (runContext.terminalSeen) {
        await reader.cancel();
        return;
      }
      if (done) break;
    }
  } finally {
    if (runContext.controller === controller) runContext.controller = null;
  }
}

async function reconnectDelay(runContext, attempt) {
  await new Promise((resolve) => window.setTimeout(resolve, SSE_RECONNECT_DELAY_MS * attempt));
  if (!runIsTracked(runContext)) throw abortError();
}

async function streamWithReconnect(runContext) {
  const eventsUrl = agentApiPath(
    `/runs/${encodeURIComponent(runContext.runId)}/events`,
    runContext.agentId,
  );
  let lastError = new Error("事件流在终态前结束");
  for (let attempt = 0; attempt <= MAX_SSE_RECONNECTS; attempt += 1) {
    try {
      await consumeEventStream(eventsUrl, runContext);
      if (runContext.terminalSeen) return;
      lastError = new Error("事件流在终态前结束");
    } catch (error) {
      if (error.name === "AbortError" || !runIsTracked(runContext)) throw abortError();
      if (runContext.terminalSeen) return;
      lastError = error;
    }
    if (attempt === MAX_SSE_RECONNECTS) break;
    if (state.sessionId === runContext.sessionId) {
      setStatus(`事件流中断，正在重连 (${attempt + 1}/${MAX_SSE_RECONNECTS})`);
    }
    await reconnectDelay(runContext, attempt + 1);
  }
  throw lastError;
}

function terminalKindForStatus(status) {
  if (status === "completed") return "run.completed";
  if (status === "cancelled") return "run.cancelled";
  if (["failed", "interrupted"].includes(status)) return "run.failed";
  return null;
}

function completeRunContext(runContext, refreshFailed) {
  if (!runIsTracked(runContext)) return;
  stopTransportWait(runContext);
  stopPermissionPoll(runContext);
  state.activeRuns.delete(runContext.sessionId);
  const inspectorOpen = elements.workspace.classList.contains("runtime-open");
  if (!inspectorOpen) {
    releaseHistoricalTimelineCaches();
  }
  const foreground = state.sessionId === runContext.sessionId;
  if (foreground) {
    state.permissionRequest += 1;
    renderPermissions([]);
  }
  if (!foreground) {
    state.backgroundCompletions.set(runContext.sessionId, runContext.terminalKind || "run.failed");
  }
  renderSessionList();
  const terminalStatus = {
    "run.completed": "本轮运行已完成",
    "run.failed": "本轮运行失败",
    "run.cancelled": "本轮运行已取消",
  };
  const suffix = refreshFailed
    ? (
      inspectorOpen
        ? "；会话刷新失败，当前运行详情已保留"
        : "；会话刷新失败，可稍后重新打开运行详情并刷新会话"
    )
    : "";
  const failureCode = runContext.terminalPayload?.code;
  const failureDetail = runContext.terminalKind === "run.failed" && failureCode
    ? `：${modelErrorLabel(failureCode)}`
    : "";
  if (foreground) {
    const outputLimited = (
      runContext.terminalKind === "run.completed" &&
      runContext.terminalPayload?.reason === "max_output"
    );
    const repetitionTruncated = (
      runContext.terminalKind === "run.completed" &&
      runContext.terminalPayload?.reason === "repetition_truncated"
    );
    setStatus(
      `${outputLimited
        ? "回答达到模型输出长度上限；已保留此前内容，可继续追问"
        : repetitionTruncated
        ? "检测到回答进入重复循环；重复尾部已截断，本轮已完成，可继续追问"
        : terminalStatus[runContext.terminalKind] || "本轮运行已结束"}${failureDetail}${suffix}`,
      runContext.terminalKind === "run.failed" ? "failure" : "interaction",
    );
  }
  setRunControls();
}

async function refreshRunConversation(runContext) {
  if (!runIsTracked(runContext)) throw abortError();
  if (state.sessionId === runContext.sessionId) {
    const selected = await selectSession(runContext.sessionId, {
      preserveTimeline: true,
      attachRunning: false,
      ownerRun: runContext,
      deferTerminalCompletion: true,
    });
    if (!runIsTracked(runContext)) throw abortError();
    return selected;
  }
  const response = await api(agentApiPath(
    `/sessions/${encodeURIComponent(runContext.sessionId)}`,
    runContext.agentId,
  ));
  const detail = await response.json();
  if (!runIsTracked(runContext)) throw abortError();
  const session = normalizeSession(detail.session);
  const page = normalizeConversationPage(detail.page);
  if (
    !session || session.session_id !== runContext.sessionId ||
    !page || !Array.isArray(detail.messages)
  ) {
    throw new Error("后台会话详情格式无效");
  }
  if (state.sessionId === runContext.sessionId) {
    const selected = await selectSession(runContext.sessionId, {
      preserveTimeline: true,
      attachRunning: false,
      ownerRun: runContext,
      deferTerminalCompletion: true,
    });
    if (!runIsTracked(runContext)) throw abortError();
    if (!selected) throw new Error("本轮终态尚未同步到当前会话");
    return selected;
  }
  const index = state.sessions.findIndex((item) => item.session_id === runContext.sessionId);
  if (index >= 0 && state.sessions[index].revision <= session.revision) {
    state.sessions[index] = session;
  }
  return { session, messages: detail.messages, page };
}

async function driveRun(runContext) {
  if (runContext.driverPromise) return runContext.driverPromise;
  const driver = (async () => {
    void pollPendingPermissions(runContext);
    let streamError = null;
    try {
      await streamWithReconnect(runContext);
    } catch (error) {
      if (error.name === "AbortError" || !runIsTracked(runContext)) return;
      streamError = error;
    }
    if (!runIsTracked(runContext)) return;

    let detail = null;
    let refreshFailed = false;
    try {
      detail = await refreshRunConversation(runContext);
    } catch (_error) {
      refreshFailed = true;
    }
    if (!runIsTracked(runContext)) return;
    if (!runContext.terminalSeen && detail) {
      const persistedStatus = turnStatusForRun(detail.messages, runContext.runId);
      const inferredTerminal = terminalKindForStatus(persistedStatus);
      if (inferredTerminal) {
        runContext.terminalSeen = true;
        runContext.terminalKind = inferredTerminal;
      }
    }

    if (!runContext.terminalSeen) {
      stopTransportWait(runContext);
      if (state.sessionId === runContext.sessionId) {
        setStatus(
          `${streamError?.message || "事件流连接中断"}；` +
          "切换回来可重新连接，或停止本轮",
        );
      }
      setRunControls();
      return;
    }

    if (state.sessionId === runContext.sessionId && detail === null) {
      stopTransportWait(runContext);
      runContext.awaitingCanonicalRefresh = true;
      setStatus(
        "本轮已结束，但完整会话终态暂时无法读取；请重新选择该会话后再继续发送",
        "failure",
      );
      setRunControls();
      return;
    }

    refreshFailed ||= detail === null;
    try {
      await refreshSessionSummaries(runContext.agentId, runContext.agentEpoch);
    } catch (_error) {
      refreshFailed = true;
    }
    if (!runIsTracked(runContext)) return;
    completeRunContext(runContext, refreshFailed);
  })();
  runContext.driverPromise = driver;
  try {
    await driver;
  } finally {
    if (runContext.driverPromise === driver) runContext.driverPromise = null;
  }
}

function attachRecoveredRun(sessionId, runId) {
  if (state.activeRuns.has(sessionId) || !RUN_ID_PATTERN.test(runId)) return;
  const runContext = {
    agentId: state.agentId,
    agentEpoch: state.agentEpoch,
    runId,
    sessionId,
    lastSeq: 0,
    terminalSeen: false,
    terminalKind: null,
    awaitingCanonicalRefresh: false,
    cancelPending: false,
    controller: null,
    driverPromise: null,
    transportTimer: null,
    transportAttempt: null,
    assistantBlocks: new Map(),
    permissionPollPromise: null,
    permissionPollTimer: null,
    permissionPollWake: null,
  };
  state.activeRuns.set(sessionId, runContext);
  state.liveAssistantContent = null;
  registerTimelineRun(runId, "running");
  clearTimeline(runId);
  setRunControls();
  setStatus("已恢复正在执行的任务，正在重连事件流", "running");
  void driveRun(runContext);
}

elements.modelSelect.addEventListener("change", () => {
  const candidate = elements.modelSelect.value;
  if (state.models.some((item) => item.model_id === candidate)) {
    state.selectedModelId = candidate;
    state.nextTurnPreview = null;
    state.previewRequest += 1;
    renderSessionContextUsage();
    saveSelectedDraft();
    void refreshNextTurnPreview();
  } else {
    elements.modelSelect.value = state.selectedModelId || "";
  }
});

elements.continueSessionButton.addEventListener("click", async () => {
  if (
    !state.sessionId || state.mutationPending ||
    state.activeRun !== null || state.preparingRun !== null
  ) return;
  const sourceSessionId = state.sessionId;
  const mutationScope = beginMutation();
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(sourceSessionId)}/continue`,
    ), { method: "POST", body: JSON.stringify({}) });
    const created = normalizeSession(await response.json());
    if (!created) throw new Error("续接会话响应格式无效");
    await refreshSessions(created.session_id);
    setStatus("已创建新会话；携带内容是有界、低权限的续接投影");
  } catch (error) {
    if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
  } finally {
    finishMutation(mutationScope);
  }
});

elements.conversationMessages.addEventListener("scroll", () => {
  state.conversationFollowLatest = conversationIsNearLatest();
  updateConversationLatestControl();
}, { passive: true });

elements.conversationLatestButton.addEventListener("click", () => {
  scheduleConversationLatest({ force: true });
  elements.conversationMessages.focus({ preventScroll: true });
});

elements.runForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = elements.messageInput.value;
  if (!message.trim()) {
    setStatus("消息不能为空");
    return;
  }
  const messageBytes = new TextEncoder().encode(message).length;
  if (messageBytes > 8192) {
    setStatus(`消息为 ${formatTokens(messageBytes)} bytes，不能超过 8,192 bytes`);
    renderMessageByteUsage();
    return;
  }
  const slashCommand = message.trimStart().startsWith("/");
  if (state.mutationPending) {
    setStatus("正在处理另一项设置，请稍候");
    return;
  }
  if (state.sessionId === null) {
    setStatus("请先选择或新建一个会话");
    return;
  }
  if (!slashCommand && state.preparingRun !== null) {
    setStatus("正在准备上一条消息；可先取消准备");
    return;
  }
  if (!slashCommand && state.activeRun !== null) {
    setStatus("当前会话仍在运行；可停止本轮，或输入 /cancel 等命令");
    return;
  }
  if (!slashCommand && state.settling) {
    setStatus("正在同步会话状态，请稍候");
    return;
  }
  const sessionId = state.sessionId;
  if (slashCommand) {
    const mutationScope = beginMutation();
    try {
      await executeSlashCommand(sessionId, message);
    } catch (error) {
      if (mutationScopeIsCurrent(mutationScope)) setStatus(error.message);
    } finally {
      finishMutation(mutationScope);
    }
    return;
  }
  const modelId = elements.modelSelect.value;
  if (!MODEL_ID_PATTERN.test(modelId) || !state.models.some(
    (item) => item.model_id === modelId
  )) {
    setStatus("请选择受信模型");
    return;
  }
  const preparation = {
    agentId: state.agentId,
    agentEpoch: state.agentEpoch,
    sessionId,
    controller: new AbortController(),
    startedAt: performance.now(),
    statusController: null,
    statusTimer: null,
    statusWake: null,
    monitorStopped: false,
    cancelPending: false,
    cancelConfirmed: false,
    cancelController: null,
    converged: false,
    operationId: null,
  };
  const compact = elements.compactInput.checked;
  state.preparingRuns.set(sessionId, preparation);
  setRunControls();
  setStatus("正在检查上下文并准备本轮；消息草稿会保留，必要时可取消", "preparation");
  preparation.statusPromise = monitorPreparation(preparation);
  void preparation.statusPromise.catch(() => null);
  let runContext = null;
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(sessionId)}/runs`,
    ), {
      method: "POST",
      signal: preparation.controller.signal,
      body: JSON.stringify({
        message,
        model_id: modelId,
        compact,
      }),
    });
    stopPreparationMonitor(preparation);
    const run = await response.json();
    if (
      typeof run.run_id !== "string" || !RUN_ID_PATTERN.test(run.run_id) ||
      typeof run.events_url !== "string" || run.session_id !== sessionId
    ) {
      throw new Error("运行响应格式无效");
    }
    const expectedEventsUrl = agentApiPath(
      `/runs/${encodeURIComponent(run.run_id)}/events`,
    );
    if (run.events_url !== expectedEventsUrl) throw new Error("运行事件地址无效");
    if (!preparationIsTracked(preparation)) throw abortError();
    state.preparingRuns.delete(sessionId);
    runContext = {
      agentId: preparation.agentId,
      agentEpoch: preparation.agentEpoch,
      runId: run.run_id,
      sessionId,
      lastSeq: 0,
      terminalSeen: false,
      terminalKind: null,
      awaitingCanonicalRefresh: false,
      cancelPending: false,
      controller: null,
      driverPromise: null,
      transportTimer: null,
      transportAttempt: null,
      assistantBlocks: new Map(),
      permissionPollPromise: null,
      permissionPollTimer: null,
      permissionPollWake: null,
    };
    state.activeRuns.set(sessionId, runContext);
    const draftKey = conversationStateKey(sessionId, preparation.agentId);
    if (draftKey) state.sessionDrafts.delete(draftKey);
    if (state.sessionId === sessionId) {
      elements.compactInput.checked = false;
      state.liveAssistantContent = null;
      elements.messageInput.value = "";
      resizeComposer();
      renderMessageByteUsage();
      appendMessage("user", message, { runId: run.run_id, turnStatus: "running" });
      registerTimelineRun(run.run_id, "running");
      clearTimeline(run.run_id);
      setStatus("正在执行本轮", "running");
    }
    renderSessionList();
    setRunControls();
    await driveRun(runContext);
  } catch (error) {
    const stillOwned = preparationIsTracked(preparation) && (
      state.agentId === preparation.agentId
    );
    if (error.name === "AbortError" && state.csrfToken !== null && stillOwned) {
      if (preparation.cancelConfirmed && !preparation.converged) {
        if (preparation.monitorStopped) {
          preparation.monitorStopped = false;
          preparation.statusPromise = monitorPreparation(preparation, { immediate: true });
          void preparation.statusPromise.catch(() => null);
        }
        setStatus("取消请求已确认，正在等待服务端安全收敛；消息草稿会保留", "preparation");
      } else {
        stopPreparationMonitor(preparation);
        setStatus("上下文准备连接已关闭；消息草稿仍在输入框中", "interaction");
      }
    } else if (state.csrfToken !== null && stillOwned) {
      stopPreparationMonitor(preparation);
      if (error.status === 409) {
        if (state.sessionId === sessionId) {
          await selectSession(sessionId).catch(() => null);
          setStatus(
            error.code === "conversation_turn_capacity_exhausted"
              ? "此会话已达到 128 个 Turn，请继续为新会话"
              : "会话状态已更新；同一会话只能有一个活跃 Run",
          );
        } else {
          await refreshSessionSummaries().catch(() => null);
          state.backgroundCompletions.set(sessionId, "preparation.failed");
          renderSessionList();
        }
      } else {
        if (state.sessionId === sessionId) setStatus(error.message, "failure");
        else {
          state.backgroundCompletions.set(sessionId, "preparation.failed");
          renderSessionList();
        }
      }
    }
  } finally {
    const awaitingCancellation = (
      preparation.cancelConfirmed && !preparation.converged &&
      preparationIsTracked(preparation)
    );
    if (!awaitingCancellation) {
      stopPreparationMonitor(preparation);
      if (preparationIsTracked(preparation)) state.preparingRuns.delete(sessionId);
    }
    setRunControls();
  }
});

elements.cancelButton.addEventListener("click", async () => {
  const preparation = state.preparingRun;
  if (preparation !== null) {
    if (preparation.cancelPending) return;
    preparation.cancelPending = true;
    stopPreparationMonitor(preparation);
    setStatus("正在安全取消上下文准备；消息草稿会保留", "preparation");
    setRunControls();
    const controller = new AbortController();
    preparation.cancelController = controller;
    const cancelAuthRequest = state.authRequest;
    const cancelCsrfToken = state.csrfToken;
    const cancelScopeIsCurrent = () => (
      authScopeIsCurrent(cancelAuthRequest, cancelCsrfToken) &&
      agentScopeIsCurrent(preparation.agentId, preparation.agentEpoch)
    );
    const timeout = window.setTimeout(() => controller.abort(), LOGOUT_TIMEOUT_MS);
    try {
      while (preparation.operationId === null) {
        const statusResponse = await api(
          `/api/agents/${encodeURIComponent(preparation.agentId)}/sessions/` +
          `${encodeURIComponent(preparation.sessionId)}/preparation`,
          { signal: controller.signal },
        );
        const status = normalizePreparationStatus(await statusResponse.json());
        // The create-Run request may have handed this exact operation to an
        // active Run while the status GET was in flight. Agent/auth scope, not
        // the transient browser map entry, owns the cancellation intent.
        if (!cancelScopeIsCurrent()) throw abortError();
        if (!status) throw new Error("准备进度响应格式无效");
        if (status.state === "idle") {
          // The UI can observe the click before the admission request has
          // registered its operation. Preserve that first cancellation intent
          // within the fixed client deadline instead of requiring a second
          // click after the Run has already started.
          await abortableDelay(50, controller.signal);
          continue;
        }
        if (status.operationId === null) throw new Error("准备操作标识缺失");
        preparation.operationId = status.operationId;
      }
      const response = await api(
        `/api/agents/${encodeURIComponent(preparation.agentId)}/sessions/` +
        `${encodeURIComponent(preparation.sessionId)}/preparation/cancel`,
        {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({ operation_id: preparation.operationId }),
        },
      );
      const result = await response.json();
      if (!cancelScopeIsCurrent()) throw abortError();
      if (
        !result || result.version !== "run-preparation-cancel-v1" ||
        !["cancellation_requested", "idle", "stale"].includes(result.state) ||
        !["preparation", "run", null].includes(result.target) ||
        (result.state === "cancellation_requested" && result.target === null) ||
        (["idle", "stale"].includes(result.state) && result.target !== null)
      ) {
        throw new Error("取消准备响应格式无效");
      }
      if (["idle", "stale"].includes(result.state)) {
        preparation.converged = true;
        preparation.controller.abort();
        if (preparationIsTracked(preparation)) {
          state.preparingRuns.delete(preparation.sessionId);
        }
        renderSessionList();
        if (state.sessionId === preparation.sessionId) {
          setStatus(
            result.state === "stale"
              ? "准备操作已变化；没有取消新的任务，已刷新会话状态"
              : "本次准备已经结束；已刷新会话状态",
            result.state === "stale" ? "failure" : "interaction",
          );
          await selectSession(preparation.sessionId, {
            preserveTimeline: true,
          }).catch(() => null);
        } else {
          await refreshSessionSummaries(
            preparation.agentId,
            preparation.agentEpoch,
          ).catch(() => null);
        }
        return;
      }
      preparation.cancelConfirmed = true;
      preparation.controller.abort();
      const handedOffRun = state.activeRuns.get(preparation.sessionId) || null;
      if (
        result.target === "run" && handedOffRun &&
        handedOffRun.agentId === preparation.agentId &&
        handedOffRun.agentEpoch === preparation.agentEpoch
      ) {
        handedOffRun.cancelPending = true;
        if (state.sessionId === preparation.sessionId) {
          setStatus("取消请求已确认，正在等待本轮安全收敛", "preparation");
        }
      } else if (preparationIsTracked(preparation)) {
        preparation.monitorStopped = false;
        preparation.statusPromise = monitorPreparation(preparation, { immediate: true });
        void preparation.statusPromise.catch(() => null);
        if (state.sessionId === preparation.sessionId) {
          setStatus("取消请求已确认，正在等待准备安全收敛", "preparation");
        }
      } else {
        // A narrow handoff can complete before the active Run is attached to
        // the browser. Durable session truth will expose the cancelling Run.
        if (state.sessionId === preparation.sessionId) {
          setStatus("取消请求已确认，正在同步本轮终态", "preparation");
          await selectSession(preparation.sessionId, {
            preserveTimeline: true,
          }).catch(() => null);
        } else {
          await refreshSessionSummaries(
            preparation.agentId,
            preparation.agentEpoch,
          ).catch(() => null);
        }
      }
    } catch (error) {
      if (cancelScopeIsCurrent() && state.sessionId === preparation.sessionId) {
        preparation.cancelPending = false;
        preparation.monitorStopped = false;
        preparation.statusPromise = monitorPreparation(preparation, { immediate: true });
        void preparation.statusPromise.catch(() => null);
        setStatus(
          error.name === "AbortError"
            ? "取消确认超时；准备仍在继续，可以再次取消"
            : `暂时无法取消准备：${error.message}`,
          "failure",
        );
      }
    } finally {
      window.clearTimeout(timeout);
      if (preparation.cancelController === controller) {
        preparation.cancelController = null;
      }
      setRunControls();
    }
    return;
  }
  const runContext = state.activeRun;
  if (!runContext || runContext.terminalSeen || runContext.cancelPending) return;
  runContext.cancelPending = true;
  setRunControls();
  try {
    await api(agentApiPath(`/runs/${encodeURIComponent(runContext.runId)}/cancel`), {
      method: "POST",
    });
    if (
      runIsTracked(runContext) && !runContext.terminalSeen &&
      state.sessionId === runContext.sessionId
    ) {
      setStatus("停止请求已发送，正在等待本轮运行结束", "running");
    }
    if (runContext.driverPromise) await runContext.driverPromise;
    if (runIsTracked(runContext) && !runContext.terminalSeen) {
      runContext.cancelPending = false;
      await driveRun(runContext);
    }
  } catch (error) {
    if (runIsTracked(runContext) && state.csrfToken !== null) {
      runContext.cancelPending = false;
      if (state.sessionId === runContext.sessionId) setStatus(error.message);
      void driveRun(runContext);
    }
  } finally {
    setRunControls();
  }
});

window.addEventListener("pagehide", () => {
  stopReplay();
  clearCopyFeedbackTimers();
  detachAgentBrowserActivity();
  releaseHistoricalTimelineCaches();
});

window.addEventListener("pageshow", (event) => {
  if (
    event.persisted && state.csrfToken !== null &&
    state.agentId !== null && state.sessionId !== null
  ) {
    // A page restored from the back-forward cache no longer owns the SSE and
    // admission requests that were aborted on pagehide. Reconcile from the
    // durable conversation/run state instead of displaying a phantom spinner.
    void selectSession(state.sessionId, { preserveTimeline: false });
  }
});

void restoreLoginSession();

"use strict";

const TERMINAL_EVENTS = new Set(["run.completed", "run.failed", "run.cancelled"]);
const RUN_ID_PATTERN = /^[a-f0-9]{32}$/;
const AGENT_ID_PATTERN = /^[a-f0-9-]{32,36}$/;
const SYSTEM_AGENT_ID = "00000000-0000-4000-8000-000000000001";
const MODEL_ID_PATTERN = /^[A-Za-z0-9._:/+-]{1,128}$/;
const MAX_SSE_RECONNECTS = 3;
const SSE_RECONNECT_DELAY_MS = 250;
const STREAM_CONTROL_VERSION = "stream-control-v1";
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
]);
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
const MODEL_ERROR_LABELS = Object.freeze({
  model_first_frame_timeout: "等待模型首帧超时；零输出重试仍未恢复",
  model_stream_idle_timeout: "模型已开始响应，但流式传输长时间无新帧",
  model_turn_deadline: "模型持续响应，但单次调用超过总时限",
  model_transport_timeout: "模型网络传输阶段超时",
  model_busy: "本地有界模型队列等待超时",
});

const state = {
  csrfToken: null,
  agentId: null,
  agents: [],
  researchEnvironment: null,
  models: [],
  commands: [],
  selectedModelId: null,
  sessions: [],
  sessionId: null,
  sessionRequest: 0,
  timelineRequest: 0,
  timelineRuns: [],
  subagents: [],
  selectedTimelineRunId: null,
  contextRequest: 0,
  contextLoading: false,
  contextDialogTrigger: null,
  sessionLoading: false,
  activeRun: null,
  settling: false,
  mutationPending: false,
  conversationMessages: [],
  conversationFollowLatest: true,
  conversationScrollTimer: null,
  sessionContextUsage: null,
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

const elements = {
  statusDot: document.querySelector("#status-dot"),
  statusText: document.querySelector("#status-text"),
  composerStatus: document.querySelector("#composer-status"),
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
  commandHelpList: document.querySelector("#command-help-list"),
  commandResult: document.querySelector("#command-result"),
  commandResultTitle: document.querySelector("#command-result-title"),
  commandResultJson: document.querySelector("#command-result-json"),
  commandResultClose: document.querySelector("#command-result-close"),
  permissionPanel: document.querySelector("#permission-panel"),
  permissionList: document.querySelector("#permission-list"),
  runButton: document.querySelector("#run-button"),
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

function setStatus(message) {
  elements.statusText.textContent = message;
  elements.composerStatus.textContent = message;
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
    elements.workspace.classList.remove("navigation-open");
    if (focus) elements.runtimeInspectorClose.focus();
  } else if (focus) {
    elements.runtimeInspectorButton.focus();
  }
}

function setNavigation(open) {
  if (narrowWorkspace()) {
    elements.workspace.classList.toggle("navigation-open", Boolean(open));
    elements.navigationToggle.setAttribute("aria-expanded", String(Boolean(open)));
    if (open) setRuntimeInspector(false);
    return;
  }
  elements.workspace.classList.toggle("sidebar-collapsed", !open);
  elements.navigationToggle.setAttribute("aria-expanded", String(Boolean(open)));
}

function closeNavigationOnNarrowScreen() {
  if (narrowWorkspace()) setNavigation(false);
}

function resizeComposer() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 192)}px`;
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

function renderSessionContextUsage() {
  const plan = state.sessionContextUsage;
  const usage = state.sessionTurnUsage;
  const fallbackModel = selectedModel();
  const total = plan?.totalTokens || fallbackModel?.operational_context_tokens || null;
  const modelId = plan?.modelId || fallbackModel?.model_id || null;
  elements.contextUsageLabel.textContent = modelId
    ? `下一条消息预算 · ${modelId}`
    : "下一条消息预算";
  const providerProjectionAvailable = (
    plan && usage && usage.runId === plan.runId &&
    usage.terminalKind === "run.completed" && usage.complete &&
    Number.isSafeInteger(usage.firstInputTokens) && usage.firstInputTokens >= 0 &&
    Number.isSafeInteger(usage.finalOutputTokens) && usage.finalOutputTokens >= 0
  );
  if (!providerProjectionAvailable || !total) {
    elements.contextUsageValue.textContent = "等待上一轮 Provider 结算";
    elements.contextUsageDetail.textContent = plan
      ? (
        `窗口 ${formatTokens(total)} · 自动压缩阈值 ${formatTokens(plan.compactAtTokens)} · ` +
        `硬输入上限 ${formatTokens(plan.inputBudgetTokens)}`
      )
      : "发送并完成一轮对话后，将使用 Provider 实际 token 推导。";
    elements.contextUsageMeter.setAttribute(
      "aria-valuemax",
      String(plan?.compactAtTokens || total || 100),
    );
    elements.contextUsageMeter.setAttribute("aria-valuenow", "0");
    elements.contextUsageMeter.setAttribute("aria-valuetext", elements.contextUsageValue.textContent);
    elements.contextUsageFill.style.width = "0%";
    elements.contextUsage.dataset.level = "empty";
    elements.contextUsage.title = (
      "下一轮尚未实际提交，Provider 无法精确计数；完成一轮后会用其实际 usage 推导。"
    );
    return;
  }
  const projectedTokens = usage.firstInputTokens + usage.finalOutputTokens;
  const compactRemaining = Math.max(0, plan.compactAtTokens - projectedTokens);
  const hardRemaining = Math.max(0, plan.inputBudgetTokens - projectedTokens);
  const percent = Math.min(100, (projectedTokens / plan.compactAtTokens) * 100);
  const percentText = percent < 10 ? percent.toFixed(1) : Math.round(percent).toString();
  elements.contextUsageValue.textContent = (
    compactRemaining > 0
      ? `触发压缩前约可写 ${formatTokens(compactRemaining)} tokens`
      : "下一条消息将触发压缩"
  );
  elements.contextUsageDetail.textContent = (
    `Provider 实测推导占用约 ${formatTokens(projectedTokens)} / ${formatTokens(total)} · ` +
    `首次完整请求 ${formatTokens(usage.firstInputTokens)} in + ` +
    `最终回答 ${formatTokens(usage.finalOutputTokens)} out · ` +
    `硬输入余量约 ${formatTokens(hardRemaining)} · 压缩阈值占用 ${percentText}%`
  );
  elements.contextUsageMeter.setAttribute("aria-valuemax", String(plan.compactAtTokens));
  elements.contextUsageMeter.setAttribute(
    "aria-valuenow",
    String(Math.min(projectedTokens, plan.compactAtTokens)),
  );
  elements.contextUsageMeter.setAttribute("aria-valuetext", elements.contextUsageValue.textContent);
  elements.contextUsageFill.style.width = `${percent}%`;
  elements.contextUsage.dataset.level = percent >= 90
    ? "critical"
    : percent >= 75 ? "warning" : "normal";
  elements.contextUsage.title = (
    "下轮基线取上一轮第一次完整 ToolSet 请求的实际 input，加最终回答调用的实际 output；" +
    "中间 Tool 调用及其重复读取只计入本轮累计用量，不叠加为持久会话上下文；" +
    "下轮消息和模板尚未送入 Provider，因此这是实际 usage 推导值，不是精确预先计数。"
  );
}

function renderSessionTurnUsage() {
  const usage = state.sessionTurnUsage;
  elements.turnTokenUsageLabel.textContent = usage?.terminalKind
    ? "上一轮 Provider 实际用量"
    : "本轮 Provider 实际用量";
  if (!usage || !usage.hasUsage) {
    elements.turnTokenUsageValue.textContent = "等待模型响应";
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
    `${formatTokens(total)} total · ${formatTokens(usage.inputTokens)} in + ` +
    `${formatTokens(usage.outputTokens)} out · ${accounting}`
  );
  elements.turnTokenUsageValue.title = (
    "本轮所有已完成模型调用的实际供应商用量；Tool 循环会重复计算共享上下文，" +
    "因此这是计算量/成本辅助信息，不是下一轮上下文占用。"
  );
}

function clearSessionContextUsage() {
  state.sessionContextUsage = null;
  state.sessionTurnUsage = null;
  renderSessionContextUsage();
  renderSessionTurnUsage();
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
    usage.hasUsage = true;
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
    usage.hasUsage = true;
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

function agentApiPath(suffix = "") {
  if (typeof state.agentId !== "string" || !AGENT_ID_PATTERN.test(state.agentId)) {
    throw new Error("当前 Agent 身份无效");
  }
  return `/api/agents/${encodeURIComponent(state.agentId)}${suffix}`;
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
  const locked = state.settling || state.mutationPending || runContext !== null;
  const hasSession = state.sessionId !== null;
  const selectedIsActive = sessionIsActive(selectedSession());
  elements.newSessionButton.disabled = locked;
  elements.newAgentName.disabled = locked;
  elements.newAgentButton.disabled = locked;
  elements.researchEnvironmentInstall.disabled = locked || state.researchEnvironment?.installed === true;
  elements.researchEnvironmentDelete.disabled = locked || state.researchEnvironment?.installed !== true;
  // Keep the command line available during an active Run so /status,
  // /permissions and /cancel remain explicit control-plane actions.
  elements.messageInput.disabled = state.mutationPending || !hasSession;
  elements.modelSelect.disabled = locked || state.models.length === 0;
  elements.compactInput.disabled = locked || !hasSession || selectedIsActive;
  elements.runButton.disabled = state.mutationPending || !hasSession;
  elements.cancelButton.disabled = (
    runContext === null || runContext.terminalSeen || runContext.cancelPending
  );
  elements.timelineRunSelect.disabled = (
    !hasSession || state.timelineRuns.length === 0 || state.sessionLoading || state.mutationPending
  );
  setReplayControls();
  setContextInspectControl();
  for (const button of elements.sessionList.querySelectorAll("button")) {
    button.disabled = locked || (
      button.classList.contains("session-delete") && button.dataset.sessionActive === "true"
    );
  }
  for (const button of elements.agentList.querySelectorAll("button")) {
    button.disabled = locked || button.dataset.agentState !== "active";
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

function setAuthenticated(session) {
  state.csrfToken = session.csrf_token;
  state.agentId = session.agent_id;
  elements.agentId.textContent = session.agent_id;
  elements.activeAgentTitle.textContent = "正在读取 Agent…";
  elements.statusDot.classList.add("online");
  setStatus("控制面已连接");
  elements.loginPanel.hidden = true;
  elements.workspace.hidden = false;
  elements.logoutButton.hidden = false;
  setNavigation(!narrowWorkspace());
  setRuntimeInspector(false);
}

function setUnauthenticated(message = "尚未认证") {
  state.activeRun?.controller?.abort();
  state.csrfToken = null;
  state.agentId = null;
  state.agents = [];
  state.researchEnvironment = null;
  state.models = [];
  state.commands = [];
  state.selectedModelId = null;
  state.sessions = [];
  state.activeRun = null;
  state.settling = false;
  state.mutationPending = false;
  state.sessionLoading = false;
  state.sessionRequest += 1;
  elements.statusDot.classList.remove("online");
  setStatus(message);
  elements.loginPanel.hidden = false;
  elements.workspace.hidden = true;
  setRuntimeInspector(false);
  elements.workspace.classList.remove("navigation-open");
  elements.logoutButton.hidden = true;
  elements.sessionList.replaceChildren();
  elements.agentList.replaceChildren();
  elements.agentListStatus.textContent = "";
  elements.agentEmpty.hidden = true;
  elements.activeAgentTitle.textContent = "未选择 Agent";
  elements.agentId.textContent = "未选择";
  elements.researchEnvironmentStatus.textContent = "未连接";
  elements.researchEnvironmentPackages.textContent = "";
  elements.modelSelect.replaceChildren(new Option("需要先登录", ""));
  elements.commandHelpList.replaceChildren();
  elements.commandResult.hidden = true;
  elements.permissionPanel.hidden = true;
  elements.permissionList.replaceChildren();
  elements.sessionListStatus.textContent = "";
  clearSelectedSession();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(state.csrfToken ? { "X-CSRF-Token": state.csrfToken } : {}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : `请求失败 (${response.status})`;
    const error = new Error(detail);
    error.status = response.status;
    if (response.status === 401 && state.csrfToken !== null) {
      setUnauthenticated("登录已过期，请重新认证");
    }
    throw error;
  }
  return response;
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
      badge.textContent = "系统 Agent";
      badges.append(badge);
    }
    const stateBadge = document.createElement("span");
    stateBadge.className = "agent-badge";
    stateBadge.textContent = agent.state;
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
        "仅在 Agent 指令、基础运行环境或安全策略发生实质变化时重建运行代。"
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

async function refreshAgents(preferredAgentId = state.agentId) {
  elements.agentListStatus.textContent = "正在读取 Agent…";
  const response = await api("/api/agents");
  const body = await response.json();
  if (!body || !Array.isArray(body.agents) || body.agents.length > 100) {
    throw new Error("Agent 列表格式无效");
  }
  const agents = body.agents.map(normalizeAgent).filter((item) => item !== null);
  if (agents.length !== body.agents.length || new Set(
    agents.map((agent) => agent.agent_id),
  ).size !== agents.length) {
    throw new Error("Agent 列表条目无效");
  }
  state.agents = agents;
  const target = agents.find((agent) => (
    agent.agent_id === preferredAgentId && agent.state === "active"
  )) || agents.find((agent) => (
    agent.agent_id === SYSTEM_AGENT_ID && agent.state === "active"
  )) || agents.find((agent) => agent.state === "active") || null;
  if (!target) throw new Error("没有可用的 active Agent");
  setAgentIdentity(target);
  elements.agentListStatus.textContent = `${agents.length} 个 Agent`;
  renderAgentList();
  return target;
}

async function loadAgentSurface(preferredAgentId, successMessage) {
  clearSelectedSession();
  state.sessions = [];
  renderSessionList();
  renderPermissions([]);
  await refreshAgents(preferredAgentId);
  await refreshResearchEnvironment();
  await refreshCommands();
  await refreshSessions(null);
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
      ? "安装后将启用 document/extract_text，运行期断网且仅只读当前 Agent 文档。"
      : "";
  }
  setRunControls();
}

async function refreshResearchEnvironment() {
  state.researchEnvironment = null;
  renderResearchEnvironment();
  const response = await api(agentApiPath("/research-environment"));
  const value = normalizeResearchEnvironment(await response.json());
  if (!value) throw new Error("研究环境响应格式无效");
  state.researchEnvironment = value;
  renderResearchEnvironment();
}

async function installResearchEnvironment() {
  if (state.mutationPending || state.activeRun !== null || state.researchEnvironment?.installed) return;
  state.mutationPending = true;
  setRunControls();
  setStatus("正在当前 Agent 内安装固定版本研究依赖…");
  try {
    await api(agentApiPath("/research-environment"), {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refreshResearchEnvironment();
    await refreshCommands();
    setStatus("研究环境已安装；后续会话会直接复用，不会重复下载");
  } catch (error) {
    if (state.csrfToken !== null) setStatus(error.message);
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function deleteResearchEnvironment() {
  if (state.mutationPending || state.activeRun !== null || !state.researchEnvironment?.installed) return;
  if (!window.confirm("删除当前 Agent 的研究依赖环境吗？工作区文档与历史会话不会删除。")) return;
  state.mutationPending = true;
  setRunControls();
  try {
    await api(agentApiPath("/research-environment"), { method: "DELETE" });
    await refreshResearchEnvironment();
    await refreshCommands();
    setStatus("当前 Agent 的研究依赖环境已彻底清理");
  } catch (error) {
    if (state.csrfToken !== null) setStatus(error.message);
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function switchAgent(agentId) {
  if (
    !AGENT_ID_PATTERN.test(agentId) || agentId === state.agentId ||
    state.settling || state.mutationPending || state.activeRun !== null
  ) {
    return;
  }
  const previousAgentId = state.agentId;
  state.mutationPending = true;
  setRunControls();
  try {
    await loadAgentSurface(agentId, `已切换到 ${selectedAgent()?.display_name || "Agent"}`);
    elements.agentDrawer.open = false;
  } catch (error) {
    if (state.csrfToken !== null && previousAgentId) {
      await loadAgentSurface(previousAgentId, null).catch(() => null);
      setStatus(`Agent 切换失败：${error.message}`);
    }
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

function validAgentDisplayName(value) {
  return typeof value === "string" && value.trim() &&
    new TextEncoder().encode(value).length <= 128;
}

async function createAgent(displayName) {
  if (!validAgentDisplayName(displayName)) {
    setStatus("Agent 名称必须为 1–128 UTF-8 bytes");
    return;
  }
  if (state.settling || state.mutationPending || state.activeRun !== null) return;
  state.mutationPending = true;
  setRunControls();
  try {
    const response = await api("/api/agents", {
      method: "POST",
      body: JSON.stringify({ display_name: displayName.trim() }),
    });
    const agent = normalizeAgent(await response.json());
    if (!agent || agent.state !== "active") throw new Error("新建 Agent 响应格式无效");
    elements.newAgentName.value = "";
    await loadAgentSurface(agent.agent_id, `Agent“${agent.display_name}”已创建并切换`);
    elements.agentDrawer.open = false;
  } catch (error) {
    if (state.csrfToken !== null) setStatus(error.message);
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function renameAgent(agent) {
  if (
    agent.agent_id === SYSTEM_AGENT_ID || agent.state !== "active" ||
    state.settling || state.mutationPending || state.activeRun !== null
  ) {
    return;
  }
  const displayName = window.prompt("输入新的 Agent 名称", agent.display_name);
  if (displayName === null) return;
  const normalizedName = displayName.trim();
  if (!validAgentDisplayName(normalizedName)) {
    setStatus("Agent 名称必须为 1–128 UTF-8 bytes");
    return;
  }
  if (normalizedName === agent.display_name) {
    setStatus("Agent 名称没有变化");
    return;
  }
  state.mutationPending = true;
  setRunControls();
  try {
    const response = await api(
      `/api/agents/${encodeURIComponent(agent.agent_id)}`,
      { method: "PATCH", body: JSON.stringify({ display_name: normalizedName }) },
    );
    const renamed = normalizeAgent(await response.json());
    if (!renamed || renamed.state !== "active") throw new Error("Agent 重命名响应格式无效");
    if (renamed.generation !== agent.generation) {
      throw new Error("Agent 重命名意外改变了运行代");
    }
    state.agents = state.agents.map((item) => (
      item.agent_id === renamed.agent_id ? renamed : item
    ));
    if (renamed.agent_id === state.agentId) setAgentIdentity(renamed);
    renderAgentList();
    setStatus(`Agent 已重命名为“${renamed.display_name}”；运行环境未重建`);
  } catch (error) {
    if (state.csrfToken !== null) setStatus(error.message);
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function upgradeAgent(agent) {
  if (
    agent.agent_id === SYSTEM_AGENT_ID || agent.state !== "active" ||
    state.settling || state.mutationPending || state.activeRun !== null
  ) {
    return;
  }
  const nextGeneration = agent.generation + 1;
  if (!window.confirm(
    `确定重建“${agent.display_name}”的运行环境吗？\n\n` +
    `运行代将从 ${agent.generation} 升至 ${nextGeneration}。该操作会排空当前运行时并重建 ` +
    "Worker 虚拟环境；会话、工作区、Skills 和持久依赖会保留。普通重命名不需要执行此操作。",
  )) return;
  state.mutationPending = true;
  setRunControls();
  try {
    const response = await api(
      `/api/agents/${encodeURIComponent(agent.agent_id)}/upgrade`,
      { method: "POST", body: JSON.stringify({}) },
    );
    const upgraded = normalizeAgent(await response.json());
    if (!upgraded || upgraded.state !== "active") throw new Error("Agent 升级响应格式无效");
    if (agent.agent_id === state.agentId) {
      await loadAgentSurface(
        state.agentId,
        `Agent 运行环境已重建为 generation ${upgraded.generation}`,
      );
    } else {
      await refreshAgents(state.agentId);
      setStatus(`Agent 运行环境已重建为 generation ${upgraded.generation}`);
    }
  } catch (error) {
    if (state.csrfToken !== null) setStatus(error.message);
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function deleteAgent(agent) {
  if (
    agent.agent_id === SYSTEM_AGENT_ID || agent.state !== "active" ||
    state.settling || state.mutationPending || state.activeRun !== null
  ) {
    return;
  }
  if (!window.confirm(
    `确定删除 Agent“${agent.display_name}”吗？其会话、Skill、Task、环境和沙箱数据都会清除，且不可撤销。`,
  )) return;
  const deletingSelected = agent.agent_id === state.agentId;
  state.mutationPending = true;
  setRunControls();
  try {
    await api(`/api/agents/${encodeURIComponent(agent.agent_id)}`, { method: "DELETE" });
    if (deletingSelected) {
      await loadAgentSurface(
        SYSTEM_AGENT_ID,
        `Agent“${agent.display_name}”已删除且残留已清理`,
      );
    } else {
      await refreshAgents(state.agentId);
      setStatus(`Agent“${agent.display_name}”已删除且残留已清理`);
    }
  } catch (error) {
    if (state.csrfToken !== null) {
      await refreshAgents(state.agentId).catch(() => null);
      setStatus(error.message);
    }
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function refreshCommands() {
  const response = await api(agentApiPath("/commands"));
  const body = await response.json();
  if (
    !body || body.schema_version !== 1 || !Array.isArray(body.commands) ||
    body.commands.length < 1 || body.commands.length > 32
  ) {
    throw new Error("Slash Command 目录格式无效");
  }
  state.commands = body.commands;
  elements.commandHelpList.replaceChildren();
  for (const command of body.commands) {
    if (
      !command || typeof command.command_id !== "string" ||
      typeof command.name !== "string" || !command.name.startsWith("/") ||
      typeof command.description !== "string" ||
      typeof command.argument_schema !== "string"
    ) {
      throw new Error("Slash Command 条目格式无效");
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
        for (const control of actions.querySelectorAll("button")) control.disabled = true;
        try {
          await api(
            `/api/agents/${encodeURIComponent(state.agentId)}/permissions/${encodeURIComponent(permission.permission_id)}`,
            { method: "POST", body: JSON.stringify({ decision }) },
          );
          setStatus(decision === "approve" ? "能力已批准一次" : "能力已拒绝");
          await refreshPendingPermissions(state.activeRun?.runId || null);
        } catch (error) {
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

async function refreshPendingPermissions(runId = null) {
  if (!state.agentId) return;
  const response = await api(`/api/agents/${encodeURIComponent(state.agentId)}/permissions`);
  const body = await response.json();
  const permissions = Array.isArray(body.permissions)
    ? body.permissions.filter((item) => runId === null || item.run_id === runId)
    : [];
  renderPermissions(permissions);
}

async function pollPendingPermissions(runContext) {
  while (state.activeRun === runContext && !runContext.terminalSeen && state.csrfToken !== null) {
    await refreshPendingPermissions(runContext.runId).catch(() => null);
    await new Promise((resolve) => window.setTimeout(resolve, 750));
  }
  if (state.activeRun === runContext || state.activeRun === null) renderPermissions([]);
}

async function executeSlashCommand(sessionId, command) {
  const response = await api(agentApiPath(
    `/sessions/${encodeURIComponent(sessionId)}/commands`,
  ), {
    method: "POST",
    body: JSON.stringify({ command }),
  });
  const value = await response.json();
  if (
    !value || value.schema_version !== 1 || value.kind !== "slash_command_result" ||
    value.model_invoked !== false || value.turn_created !== false ||
    typeof value.command_id !== "string" || typeof value.result !== "object" ||
    typeof value.ui_effect !== "object"
  ) {
    throw new Error("Slash Command 响应格式无效");
  }
  showCommandResult(value);
  if (value.command_id === "permissions") renderPermissions(value.result.permissions);
  if (value.ui_effect.next_turn_model_id) {
    state.selectedModelId = value.ui_effect.next_turn_model_id;
    elements.modelSelect.value = state.selectedModelId;
  }
  if (value.ui_effect.compact_next_turn === true) elements.compactInput.checked = true;
  elements.messageInput.value = "";
  resizeComposer();
  if (value.ui_effect.conversation_deleted === true) {
    clearSelectedSession();
    await refreshSessions(null);
  } else {
    await refreshSessions(sessionId);
  }
  setStatus(`/${value.command_id} 已完成；未调用模型、未创建 Turn`);
}

async function refreshModels() {
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
  state.models = models;
  if (!models.some((item) => item.model_id === state.selectedModelId)) {
    state.selectedModelId = body.default_model_id;
  }
  elements.modelSelect.replaceChildren();
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.model_id;
    option.textContent = `${model.model_id} · ${model.operational_context_tokens} ctx${
      model.supports_tools ? " · tools" : " · text"
    }`;
    elements.modelSelect.append(option);
  }
  elements.modelSelect.value = state.selectedModelId;
  renderSessionContextUsage();
  setRunControls();
}

function normalizeSession(value) {
  if (!value || typeof value !== "object" || typeof value.session_id !== "string") {
    return null;
  }
  return {
    session_id: value.session_id,
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
    selectButton.setAttribute("aria-pressed", String(session.session_id === state.sessionId));
    selectButton.setAttribute("aria-label", `恢复会话：${session.title}`);

    const title = document.createElement("strong");
    title.textContent = session.title;
    const meta = document.createElement("span");
    const updated = formatTime(session.updated_at);
    meta.textContent = `${session.message_count} 条消息${updated ? ` · ${updated}` : ""}`;
    const marker = document.createElement("span");
    marker.className = "session-state";
    marker.textContent = session.state;
    selectButton.append(title, meta, marker);
    selectButton.addEventListener("click", () => {
      void selectSession(session.session_id);
      closeNavigationOnNarrowScreen();
    });

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "session-delete danger";
    deleteButton.textContent = "删除";
    deleteButton.dataset.sessionActive = String(sessionIsActive(session));
    deleteButton.setAttribute("aria-label", `删除会话：${session.title}`);
    deleteButton.addEventListener("click", () => {
      void deleteSession(session);
    });

    item.append(selectButton, deleteButton);
    elements.sessionList.append(item);
  }
  setRunControls();
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
  body.textContent = typeof content === "string" ? content : "";
  message.append(heading, body);
  return { message, body };
}

function normalizeConversationMessage(message) {
  if (!message || typeof message !== "object" || typeof message.content !== "string") {
    return null;
  }
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
    live: message.live === true,
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
        messages: [],
        turnNumber: groups.length + 1,
      };
      byKey.set(key, group);
      groups.push(group);
    }
    if (!group.turnId && message.turn_id) group.turnId = message.turn_id;
    if (!group.runId && message.run_id) group.runId = message.run_id;
    if (message.turn_status) group.status = message.turn_status;
    group.messages.push(message);
  }
  return groups;
}

function renderConversationTurns() {
  elements.conversationMessages.replaceChildren();
  state.liveAssistantContent = null;
  const groups = conversationTurnGroups();
  elements.conversationEmpty.hidden = groups.length !== 0;
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
      group.runId ? `查看 Turn ${group.turnNumber} 的 Run 时间线` : `Turn ${group.turnNumber}`,
    );
    const title = document.createElement("strong");
    title.textContent = `Turn ${group.turnNumber}`;
    const status = document.createElement("span");
    status.className = `turn-status turn-status-${group.status || "unknown"}`;
    status.textContent = TURN_STATUS_LABELS[group.status] || group.status || "状态未知";
    const run = document.createElement("code");
    run.textContent = group.runId ? `Run ${group.runId.slice(0, 8)}` : "尚无 Run";
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
    identity.textContent = `Agent ${delegation.child_agent_id.slice(0, 8)}`;
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
      replay.textContent = `查看子 Run ${delegation.child_run_id.slice(0, 8)}`;
      replay.addEventListener("click", () => {
        void selectTimelineRun(delegation.child_run_id, { scrollTurn: false });
      });
      card.append(replay);
    }
    elements.subagentList.append(card);
  }
}

async function refreshSubagents(sessionId, requestNumber) {
  const response = await api(
    `/api/agents/${encodeURIComponent(state.agentId)}/sessions/` +
    `${encodeURIComponent(sessionId)}/subagents`,
  );
  const payload = await response.json();
  if (requestNumber !== state.sessionRequest || state.sessionId !== sessionId) return;
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

function addTimelineControl(kind, payload, cursor) {
  addTimelineEvent({
    schema_version: STREAM_CONTROL_VERSION,
    event_id: null,
    agent_id: state.agentId,
    conversation_id: state.sessionId,
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
      throw new Error("历史事件 Agent 身份无效");
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

async function selectSession(
  sessionId,
  { preserveTimeline = false, attachRunning = true, ownerRun = null } = {},
) {
  if (
    typeof sessionId !== "string" ||
    (state.activeRun !== null && state.activeRun !== ownerRun)
  ) {
    return null;
  }
  const previousSessionId = state.sessionId;
  const previousTimelineRunId = state.selectedTimelineRunId;
  const previousTimelineRuns = state.timelineRuns;
  clearContextInspector();
  const requestNumber = ++state.sessionRequest;
  state.sessionLoading = true;
  state.timelineRequest += 1;
  state.sessionId = sessionId;
  if (previousSessionId !== sessionId) {
    state.conversationFollowLatest = true;
    clearSessionContextUsage();
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
  }
  renderSessionList();
  elements.activeSessionTitle.textContent = "正在恢复会话…";
  elements.sessionId.textContent = sessionId;
  try {
    const response = await api(agentApiPath(`/sessions/${encodeURIComponent(sessionId)}`));
    const detail = await response.json();
    if (requestNumber !== state.sessionRequest || state.sessionId !== sessionId) return null;
    const session = normalizeSession(detail.session);
    if (!session || session.session_id !== sessionId || !Array.isArray(detail.messages)) {
      throw new Error("会话详情格式无效");
    }
    const index = state.sessions.findIndex((item) => item.session_id === sessionId);
    if (index >= 0) state.sessions[index] = session;
    if (!preserveTimeline) clearTimeline();
    elements.activeSessionTitle.textContent = session.title;
    elements.sessionId.textContent = session.session_id;
    renderMessages(detail.messages);
    setTimelineRuns(
      detail.messages,
      preserveTimeline && previousSessionId === sessionId ? previousTimelineRunId : null,
    );
    await refreshSubagents(sessionId, requestNumber);
    if (ownerRun && !state.timelineRuns.some((run) => run.runId === ownerRun.runId)) {
      registerTimelineRun(ownerRun.runId, "running");
    }
    renderSessionList();

    const recoverableRunId = sessionIsActive(session) ? runningRunId(detail.messages) : null;
    if (attachRunning && recoverableRunId && state.activeRun === null) {
      attachRecoveredRun(session.session_id, recoverableRunId);
    } else if (sessionIsActive(session)) {
      setStatus(
        recoverableRunId ? "会话有正在执行的 Run" : "会话正在运行，但当前无法恢复事件流",
      );
    } else {
      const historicalRunId = preserveTimeline ? null : state.selectedTimelineRunId;
      if (historicalRunId) {
        const timelineRequest = ++state.timelineRequest;
        try {
          const loaded = await loadDurableTimeline(
            historicalRunId,
            requestNumber,
            timelineRequest,
            session.session_id,
          );
          if (requestNumber !== state.sessionRequest || state.sessionId !== sessionId) {
            return null;
          }
          if (loaded) setStatus("会话与所选 Run 时间线已恢复");
        } catch (error) {
          if (requestNumber !== state.sessionRequest || state.sessionId !== sessionId) {
            return null;
          }
          if (
            timelineLoadIsCurrent(
              historicalRunId,
              requestNumber,
              timelineRequest,
              session.session_id,
            )
          ) {
            setStatus(`会话已恢复；${error.message}`);
          }
        }
      } else {
        setStatus("会话已恢复");
      }
      elements.messageInput.focus();
    }
    return { session, messages: detail.messages };
  } catch (error) {
    if (requestNumber !== state.sessionRequest || state.csrfToken === null) return null;
    state.sessionId = state.sessions.some((item) => item.session_id === previousSessionId)
      ? previousSessionId
      : null;
    state.timelineRuns = state.sessionId === previousSessionId ? previousTimelineRuns : [];
    setSelectedTimelineRunId(
      state.sessionId === previousSessionId ? previousTimelineRunId : null,
    );
    renderTimelineRunSelect();
    const previous = selectedSession();
    elements.activeSessionTitle.textContent = previous ? previous.title : "请选择一个会话";
    elements.sessionId.textContent = previous ? previous.session_id : "未选择";
    renderSessionList();
    setStatus(error.message);
    return null;
  } finally {
    if (requestNumber === state.sessionRequest) {
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
  setSelectedTimelineRunId(runId, { scrollTurn });
  renderTimelineRunSelect();
  elements.runId.textContent = runId;
  if (state.activeRun?.runId === runId) {
    state.timelineEntries = state.timelineEntriesByRun.get(runId) || [];
    renderTimelineEntries();
    setStatus("已切回正在执行的 Run 时间线");
    return;
  }
  clearTimeline(runId);
  setStatus("正在读取所选 Turn / Run 的时间线…");
  try {
    const loaded = await loadDurableTimeline(
      runId,
      sessionRequest,
      timelineRequest,
      sessionId,
    );
    if (loaded) setStatus("所选 Turn / Run 时间线已恢复");
  } catch (error) {
    if (timelineLoadIsCurrent(runId, sessionRequest, timelineRequest, sessionId)) {
      setStatus(`无法读取所选 Run 时间线：${error.message}`);
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
  if (!hasExactFields(value.context_plan, CONTEXT_PLAN_FIELDS)) {
    throw new Error("invalid context plan");
  }
  const plan = value.context_plan;
  const numericPlanFields = CONTEXT_PLAN_FIELDS.filter((field) => (
    field.endsWith("_count") || field.endsWith("_tokens")
  ));
  const stringPlanFields = CONTEXT_PLAN_FIELDS.filter(
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
      throw new Error("上下文 Agent 身份无效");
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

async function refreshSessions(preferredSessionId = state.sessionId) {
  elements.sessionListStatus.textContent = "正在读取会话…";
  const response = await api(agentApiPath("/sessions"));
  const body = await response.json();
  if (!body || !Array.isArray(body.sessions)) throw new Error("会话列表格式无效");
  state.sessions = body.sessions.map(normalizeSession).filter((item) => item !== null);
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

async function refreshSessionSummaries() {
  const response = await api(agentApiPath("/sessions"));
  const body = await response.json();
  if (!body || !Array.isArray(body.sessions)) throw new Error("会话列表格式无效");
  state.sessions = body.sessions.map(normalizeSession).filter((item) => item !== null);
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

async function createSession() {
  if (state.settling || state.mutationPending || state.activeRun !== null) return;
  state.mutationPending = true;
  setRunControls();
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
    if (error.status === 409 && state.csrfToken !== null) {
      await refreshSessions(state.sessionId).catch(() => null);
    }
    if (state.csrfToken !== null) setStatus(error.message);
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function deleteSession(session) {
  if (state.settling || state.mutationPending || state.activeRun !== null) return;
  if (!window.confirm(`确定删除“${session.title}”及其全部消息吗？此操作不可撤销。`)) return;
  state.mutationPending = true;
  setRunControls();
  let deleted = false;
  try {
    await api(agentApiPath(`/sessions/${encodeURIComponent(session.session_id)}`), {
      method: "DELETE",
    });
    deleted = true;
    const deletedSelectedSession = state.sessionId === session.session_id;
    const preferredSessionId = deletedSelectedSession ? null : state.sessionId;
    state.sessions = state.sessions.filter((item) => item.session_id !== session.session_id);
    if (deletedSelectedSession) clearSelectedSession();
    renderSessionList();
    await refreshSessions(preferredSessionId);
    setStatus("会话已删除");
  } catch (error) {
    if (error.status === 409 && state.csrfToken !== null) {
      await refreshSessions(state.sessionId).catch(() => null);
      setStatus("会话状态已更新；有活跃 Run 时不能删除");
    } else if (state.csrfToken !== null) {
      setStatus(deleted ? `会话已删除；列表刷新失败：${error.message}` : error.message);
    }
  } finally {
    state.mutationPending = false;
    setRunControls();
  }
}

async function restoreLoginSession() {
  try {
    const response = await api("/api/auth/status");
    const session = await response.json();
    if (!session.authenticated) {
      setUnauthenticated();
      return;
    }
    setAuthenticated(session);
  } catch (error) {
    setUnauthenticated();
    if (error.status !== 401) setStatus("无法读取控制面状态");
    return;
  }
  try {
    await refreshModels();
    await refreshAgents(session.agent_id);
    await refreshResearchEnvironment();
    await refreshCommands();
    await refreshSessions(null);
  } catch (error) {
    if (state.csrfToken !== null) setStatus(error.message);
  }
}

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.loginError.textContent = "";
  const token = elements.tokenInput.value;
  elements.tokenInput.value = "";
  try {
    const response = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const session = await response.json();
    setAuthenticated(session);
    try {
      await refreshModels();
      await refreshAgents(session.agent_id);
      await refreshResearchEnvironment();
      await refreshCommands();
      await refreshSessions(null);
    } catch (error) {
      if (state.csrfToken !== null) setStatus(error.message);
    }
  } catch (error) {
    elements.loginError.textContent = error.message;
  }
});

elements.logoutButton.addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
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
  setNavigation(open);
});

elements.navigationClose.addEventListener("click", () => setNavigation(false));

elements.runtimeInspectorButton.addEventListener("click", () => {
  setRuntimeInspector(!elements.workspace.classList.contains("runtime-open"), { focus: true });
});

elements.runtimeInspectorClose.addEventListener("click", () => {
  setRuntimeInspector(false, { focus: true });
});

elements.workspaceBackdrop.addEventListener("click", () => {
  setRuntimeInspector(false);
  setNavigation(false);
  elements.navigationToggle.focus();
});

for (const suggestion of document.querySelectorAll("[data-prompt-suggestion]")) {
  suggestion.addEventListener("click", () => {
    if (elements.messageInput.disabled) return;
    elements.messageInput.value = suggestion.dataset.promptSuggestion || "";
    resizeComposer();
    elements.messageInput.focus();
  });
}

elements.messageInput.addEventListener("input", resizeComposer);
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
    setNavigation(false);
    elements.navigationToggle.focus();
  }
});

window.addEventListener("resize", () => {
  if (narrowWorkspace()) {
    elements.workspace.classList.remove("sidebar-collapsed");
    elements.navigationToggle.setAttribute(
      "aria-expanded",
      String(elements.workspace.classList.contains("navigation-open")),
    );
  } else {
    elements.workspace.classList.remove("navigation-open");
    elements.navigationToggle.setAttribute(
      "aria-expanded",
      String(!elements.workspace.classList.contains("sidebar-collapsed")),
    );
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
  if (envelope.kind === "model.response.finished") {
    const iteration = payload.iteration;
    const outcome = payload.outcome;
    const inputTokens = payload.input_tokens;
    const outputTokens = payload.output_tokens;
    if (
      !Number.isSafeInteger(iteration) || iteration < 1 ||
      !["tool_use", "end_turn", "error", "cancelled"].includes(outcome) ||
      !isNonNegativeInteger(inputTokens) || !isNonNegativeInteger(outputTokens)
    ) {
      return "模型响应元数据不可用";
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
        "semantic-summary-v1",
      ].includes(context.windowing_strategy)
    ) {
      return "上下文窗口元数据不可用";
    }
    return (
      `历史纳入 ${context.included_history_message_count} / ` +
      `${context.history_message_count} · ${context.windowing_strategy}`
    );
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
  } else if (kind === "model.response.finished") {
    eventBody = payload?.error_code && MODEL_ERROR_LABELS[payload.error_code]
      ? `模型失败阶段：${MODEL_ERROR_LABELS[payload.error_code]}`
      : "模型供应商原始响应正文按协议未持久化；智能体正文来自规范 assistant 事件。";
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
  captureSessionContextUsage(envelope);
  captureSessionTurnUsage(envelope);
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
  if (state.liveAssistantMessage && state.liveAssistantContent) {
    state.liveAssistantMessage.content = state.liveAssistantContent.textContent || "";
  }
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
  if (envelope.kind === "model.request.started") {
    setStatus("模型请求已提交，正在等待首个响应帧…");
  } else if (envelope.kind === "model.response.finished" && payload.error_code) {
    setStatus(MODEL_ERROR_LABELS[payload.error_code] || `模型调用失败：${payload.error_code}`);
  } else if (envelope.kind === "assistant.block.started") {
    setStatus("模型正在流式生成回答…");
    const block = document.createElement("div");
    block.className = "assistant-block";
    block.dataset.blockId = payload.block_id;
    ensureLiveAssistant().append(block);
    state.blocks.set(payload.block_id, { element: block, content: "" });
    syncLiveAssistantMessage();
  } else if (envelope.kind === "assistant.block.delta") {
    const block = state.blocks.get(payload.block_id);
    if (block) {
      block.content += payload.text || "";
      block.element.textContent = block.content;
      syncLiveAssistantMessage();
    }
  } else if (envelope.kind === "assistant.block.finished") {
    const block = state.blocks.get(payload.block_id);
    if (block) {
      block.content = typeof payload.content === "string" ? payload.content : block.content;
      block.element.textContent = block.content;
      syncLiveAssistantMessage();
    }
  } else if (envelope.kind === "assistant.block.discarded") {
    const block = state.blocks.get(payload.block_id);
    if (block) block.element.remove();
    state.blocks.delete(payload.block_id);
    syncLiveAssistantMessage();
  }
  if (envelope.kind.startsWith("assistant.block.")) {
    scheduleConversationLatest();
    const selected = timelineEntriesForCurrentRun()
      .find((entry) => entry.key === state.selectedTimelineEntryKey) || null;
    renderEventInspector(selected);
  }

  if (TERMINAL_EVENTS.has(envelope.kind)) {
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
    addTimelineControl(frame.event, payload, toSeq);
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
    addTimelineControl(frame.event, payload, cursor);
    runContext.terminalSeen = true;
    runContext.terminalKind = terminalKind;
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
  if (state.activeRun !== runContext) throw abortError();
  const controller = new AbortController();
  runContext.controller = controller;
  const headers = { Accept: "text/event-stream" };
  if (runContext.lastSeq > 0) headers["Last-Event-ID"] = String(runContext.lastSeq);
  try {
    const response = await api(url, { headers, signal: controller.signal });
    if (!response.body) throw new Error("浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const normalized = buffer.replaceAll("\r\n", "\n");
      const frames = normalized.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
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
  if (state.activeRun !== runContext) throw abortError();
}

async function streamWithReconnect(runContext) {
  const eventsUrl = agentApiPath(`/runs/${encodeURIComponent(runContext.runId)}/events`);
  let lastError = new Error("事件流在终态前结束");
  for (let attempt = 0; attempt <= MAX_SSE_RECONNECTS; attempt += 1) {
    try {
      await consumeEventStream(eventsUrl, runContext);
      if (runContext.terminalSeen) return;
      lastError = new Error("事件流在终态前结束");
    } catch (error) {
      if (error.name === "AbortError" || state.activeRun !== runContext) throw abortError();
      if (runContext.terminalSeen) return;
      lastError = error;
    }
    if (attempt === MAX_SSE_RECONNECTS) break;
    setStatus(`事件流中断，正在重连 (${attempt + 1}/${MAX_SSE_RECONNECTS})`);
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

function completeRunContext(runContext, refreshFailed, summariesRefreshed) {
  if (state.activeRun !== runContext) return;
  state.activeRun = null;
  state.settling = false;
  renderSessionList();
  const terminalStatus = {
    "run.completed": "Run 已完成",
    "run.failed": "Run 执行失败",
    "run.cancelled": "Run 已取消",
  };
  const suffix = refreshFailed ? "；会话刷新失败，当前事件时间线已保留" : "";
  const failureCode = runContext.terminalPayload?.code;
  const failureDetail = runContext.terminalKind === "run.failed" && failureCode
    ? `：${MODEL_ERROR_LABELS[failureCode] || failureCode}`
    : "";
  setStatus(
    `${terminalStatus[runContext.terminalKind] || "Run 已结束"}${failureDetail}${suffix}`,
  );
  if (
    summariesRefreshed && state.sessionId === runContext.sessionId &&
    sessionIsActive(selectedSession())
  ) {
    state.settling = true;
    setRunControls();
    void selectSession(runContext.sessionId, { preserveTimeline: true }).finally(() => {
      if (state.activeRun === null) {
        state.settling = false;
        setRunControls();
      }
    });
  }
}

async function driveRun(runContext) {
  if (runContext.driverPromise) return runContext.driverPromise;
  const driver = (async () => {
    void pollPendingPermissions(runContext);
    let streamError = null;
    try {
      await streamWithReconnect(runContext);
    } catch (error) {
      if (error.name === "AbortError" || state.activeRun !== runContext) return;
      streamError = error;
    }
    if (state.activeRun !== runContext) return;

    const detail = await selectSession(runContext.sessionId, {
      preserveTimeline: true,
      attachRunning: false,
      ownerRun: runContext,
    });
    if (state.activeRun !== runContext) return;
    if (!runContext.terminalSeen && detail) {
      const persistedStatus = turnStatusForRun(detail.messages, runContext.runId);
      const inferredTerminal = terminalKindForStatus(persistedStatus);
      if (inferredTerminal) {
        runContext.terminalSeen = true;
        runContext.terminalKind = inferredTerminal;
      }
    }

    if (!runContext.terminalSeen) {
      setStatus(
        `${streamError?.message || "事件流连接中断"}；已刷新会话，可取消后再次重连`,
      );
      setRunControls();
      return;
    }

    let refreshFailed = detail === null;
    let summariesRefreshed = false;
    try {
      await refreshSessionSummaries();
      summariesRefreshed = true;
    } catch (_error) {
      refreshFailed = true;
    }
    completeRunContext(runContext, refreshFailed, summariesRefreshed);
  })();
  runContext.driverPromise = driver;
  try {
    await driver;
  } finally {
    if (runContext.driverPromise === driver) runContext.driverPromise = null;
  }
}

function attachRecoveredRun(sessionId, runId) {
  if (state.activeRun !== null || !RUN_ID_PATTERN.test(runId)) return;
  const runContext = {
    runId,
    sessionId,
    lastSeq: 0,
    terminalSeen: false,
    terminalKind: null,
    cancelPending: false,
    controller: null,
    driverPromise: null,
  };
  state.activeRun = runContext;
  state.settling = true;
  state.liveAssistantContent = null;
  registerTimelineRun(runId, "running");
  clearTimeline(runId);
  setRunControls();
  setStatus("已恢复正在执行的 Run，正在重连事件流");
  void driveRun(runContext);
}

elements.modelSelect.addEventListener("change", () => {
  const candidate = elements.modelSelect.value;
  if (state.models.some((item) => item.model_id === candidate)) {
    state.selectedModelId = candidate;
    renderSessionContextUsage();
  } else {
    elements.modelSelect.value = state.selectedModelId || "";
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
  const slashCommand = message.trimStart().startsWith("/");
  if (
    state.mutationPending || state.sessionId === null ||
    (!slashCommand && (state.activeRun !== null || state.settling))
  ) return;
  const sessionId = state.sessionId;
  if (slashCommand) {
    state.mutationPending = true;
    setRunControls();
    try {
      await executeSlashCommand(sessionId, message);
    } catch (error) {
      if (state.csrfToken !== null) setStatus(error.message);
    } finally {
      state.mutationPending = false;
      setRunControls();
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
  state.settling = true;
  setRunControls();
  let runContext = null;
  try {
    const response = await api(agentApiPath(
      `/sessions/${encodeURIComponent(sessionId)}/runs`,
    ), {
      method: "POST",
      body: JSON.stringify({
        message,
        model_id: modelId,
        compact: elements.compactInput.checked,
      }),
    });
    const run = await response.json();
    if (
      typeof run.run_id !== "string" || !RUN_ID_PATTERN.test(run.run_id) ||
      typeof run.events_url !== "string" || run.session_id !== sessionId
    ) {
      throw new Error("Run 响应格式无效");
    }
    const expectedEventsUrl = agentApiPath(
      `/runs/${encodeURIComponent(run.run_id)}/events`,
    );
    if (run.events_url !== expectedEventsUrl) throw new Error("Run 事件地址无效");
    runContext = {
      runId: run.run_id,
      sessionId,
      lastSeq: 0,
      terminalSeen: false,
      terminalKind: null,
      cancelPending: false,
      controller: null,
      driverPromise: null,
    };
    state.activeRun = runContext;
    elements.compactInput.checked = false;
    state.liveAssistantContent = null;
    elements.messageInput.value = "";
    resizeComposer();
    appendMessage("user", message, { runId: run.run_id, turnStatus: "running" });
    registerTimelineRun(run.run_id, "running");
    clearTimeline(run.run_id);
    setRunControls();
    setStatus("Run 正在执行");
    await driveRun(runContext);
  } catch (error) {
    if (error.name !== "AbortError" && state.csrfToken !== null) {
      if (error.status === 409) {
        await refreshSessions(sessionId).catch(() => null);
        setStatus("会话状态已更新；同一会话只能有一个活跃 Run");
      } else {
        setStatus(error.message);
      }
    }
  } finally {
    if (runContext === null && state.activeRun === null) state.settling = false;
    setRunControls();
  }
});

elements.cancelButton.addEventListener("click", async () => {
  const runContext = state.activeRun;
  if (!runContext || runContext.terminalSeen || runContext.cancelPending) return;
  runContext.cancelPending = true;
  setRunControls();
  try {
    await api(agentApiPath(`/runs/${encodeURIComponent(runContext.runId)}/cancel`), {
      method: "POST",
    });
    setStatus("取消请求已发送，正在等待 Run 收敛");
    if (runContext.driverPromise) await runContext.driverPromise;
    if (state.activeRun === runContext && !runContext.terminalSeen) {
      runContext.cancelPending = false;
      await driveRun(runContext);
    }
  } catch (error) {
    if (state.activeRun === runContext && state.csrfToken !== null) {
      runContext.cancelPending = false;
      setStatus(error.message);
      void driveRun(runContext);
    }
  } finally {
    setRunControls();
  }
});

window.addEventListener("pagehide", () => {
  stopReplay();
});

void restoreLoginSession();

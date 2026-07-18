"use strict";

const TERMINAL_EVENTS = new Set(["run.completed", "run.failed", "run.cancelled"]);
const RUN_ID_PATTERN = /^[a-f0-9]{32}$/;
const MAX_SSE_RECONNECTS = 3;
const SSE_RECONNECT_DELAY_MS = 250;
const TURN_STATUS_LABELS = {
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

const state = {
  csrfToken: null,
  agentId: null,
  sessions: [],
  sessionId: null,
  sessionRequest: 0,
  activeRun: null,
  settling: false,
  mutationPending: false,
  blocks: new Map(),
  liveAssistantContent: null,
  eventCount: 0,
  eventDetailTrigger: null,
};

const elements = {
  statusDot: document.querySelector("#status-dot"),
  statusText: document.querySelector("#status-text"),
  loginPanel: document.querySelector("#login-panel"),
  loginForm: document.querySelector("#login-form"),
  loginError: document.querySelector("#login-error"),
  tokenInput: document.querySelector("#token-input"),
  logoutButton: document.querySelector("#logout-button"),
  workspace: document.querySelector("#workspace"),
  agentId: document.querySelector("#agent-id"),
  newSessionButton: document.querySelector("#new-session-button"),
  sessionList: document.querySelector("#session-list"),
  sessionListStatus: document.querySelector("#session-list-status"),
  sessionEmpty: document.querySelector("#session-empty"),
  activeSessionTitle: document.querySelector("#active-session-title"),
  sessionId: document.querySelector("#session-id"),
  runForm: document.querySelector("#run-form"),
  messageInput: document.querySelector("#message-input"),
  runButton: document.querySelector("#run-button"),
  cancelButton: document.querySelector("#cancel-button"),
  runId: document.querySelector("#run-id"),
  conversationMessages: document.querySelector("#conversation-messages"),
  conversationEmpty: document.querySelector("#conversation-empty"),
  eventList: document.querySelector("#event-list"),
  eventCount: document.querySelector("#event-count"),
  eventDetailDialog: document.querySelector("#event-detail-dialog"),
  eventDetailClose: document.querySelector("#event-detail-close"),
  eventDetailSummary: document.querySelector("#event-detail-summary"),
  eventDetailJson: document.querySelector("#event-detail-json"),
};

function setStatus(message) {
  elements.statusText.textContent = message;
}

function sessionIsActive(session) {
  return session && ["active", "running", "deleting"].includes(session.state);
}

function selectedSession() {
  return state.sessions.find((session) => session.session_id === state.sessionId) || null;
}

function setRunControls() {
  const runContext = state.activeRun;
  const locked = state.settling || state.mutationPending || runContext !== null;
  const hasSession = state.sessionId !== null;
  const selectedIsActive = sessionIsActive(selectedSession());
  elements.newSessionButton.disabled = locked;
  elements.messageInput.disabled = locked || !hasSession || selectedIsActive;
  elements.runButton.disabled = locked || !hasSession || selectedIsActive;
  elements.cancelButton.disabled = (
    runContext === null || runContext.terminalSeen || runContext.cancelPending
  );
  for (const button of elements.sessionList.querySelectorAll("button")) {
    button.disabled = locked || (
      button.classList.contains("session-delete") && button.dataset.sessionActive === "true"
    );
  }
}

function clearConversation() {
  state.blocks.clear();
  state.liveAssistantContent = null;
  elements.conversationMessages.replaceChildren();
  elements.conversationEmpty.hidden = false;
}

function clearTimeline(label = "等待运行") {
  if (elements.eventDetailDialog.open) elements.eventDetailDialog.close();
  state.blocks.clear();
  state.eventCount = 0;
  elements.eventList.replaceChildren();
  elements.eventCount.textContent = "0 events";
  elements.runId.textContent = label;
}

function clearSelectedSession({ preserveTimeline = false } = {}) {
  state.sessionId = null;
  state.sessionRequest += 1;
  elements.activeSessionTitle.textContent = "请选择一个会话";
  elements.sessionId.textContent = "未选择";
  clearConversation();
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
  elements.statusDot.classList.add("online");
  setStatus("控制面已连接");
  elements.loginPanel.hidden = true;
  elements.workspace.hidden = false;
  elements.logoutButton.hidden = false;
}

function setUnauthenticated(message = "尚未认证") {
  state.activeRun?.controller?.abort();
  state.csrfToken = null;
  state.agentId = null;
  state.sessions = [];
  state.activeRun = null;
  state.settling = false;
  state.mutationPending = false;
  state.sessionRequest += 1;
  elements.statusDot.classList.remove("online");
  setStatus(message);
  elements.loginPanel.hidden = false;
  elements.workspace.hidden = true;
  elements.logoutButton.hidden = true;
  elements.sessionList.replaceChildren();
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

function appendMessage(role, content, metadata = {}) {
  const message = document.createElement("article");
  const safeRole = ["user", "assistant", "system"].includes(role) ? role : "system";
  message.className = `message message-${safeRole}`;
  if (typeof metadata.messageId === "string") message.dataset.messageId = metadata.messageId;
  if (typeof metadata.runId === "string") message.dataset.runId = metadata.runId;

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
  elements.conversationMessages.append(message);
  elements.conversationEmpty.hidden = true;
  return body;
}

function renderMessages(messages) {
  clearConversation();
  for (const message of messages) {
    if (!message || typeof message !== "object" || typeof message.content !== "string") continue;
    appendMessage(message.role, message.content, {
      messageId: message.message_id,
      createdAt: message.created_at,
      runId: message.run_id,
      turnStatus: message.turn_status,
    });
  }
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
  const requestNumber = ++state.sessionRequest;
  state.sessionId = sessionId;
  renderSessionList();
  elements.activeSessionTitle.textContent = "正在恢复会话…";
  elements.sessionId.textContent = sessionId;
  try {
    const response = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
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
    renderSessionList();

    const recoverableRunId = sessionIsActive(session) ? runningRunId(detail.messages) : null;
    if (attachRunning && recoverableRunId && state.activeRun === null) {
      attachRecoveredRun(session.session_id, recoverableRunId);
    } else if (sessionIsActive(session)) {
      setStatus(
        recoverableRunId ? "会话有正在执行的 Run" : "会话正在运行，但当前无法恢复事件流",
      );
    } else {
      setStatus("会话已恢复");
      elements.messageInput.focus();
    }
    return { session, messages: detail.messages };
  } catch (error) {
    if (requestNumber !== state.sessionRequest || state.csrfToken === null) return null;
    state.sessionId = state.sessions.some((item) => item.session_id === previousSessionId)
      ? previousSessionId
      : null;
    const previous = selectedSession();
    elements.activeSessionTitle.textContent = previous ? previous.title : "请选择一个会话";
    elements.sessionId.textContent = previous ? previous.session_id : "未选择";
    renderSessionList();
    setStatus(error.message);
    return null;
  }
}

async function refreshSessions(preferredSessionId = state.sessionId) {
  elements.sessionListStatus.textContent = "正在读取会话…";
  const response = await api("/api/sessions");
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
  const response = await api("/api/sessions");
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
    const response = await api("/api/sessions", {
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
    await api(`/api/sessions/${encodeURIComponent(session.session_id)}`, {
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
    setAuthenticated(await response.json());
    try {
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
});

function showEventDetail(envelope, trigger) {
  state.eventDetailTrigger = trigger;
  elements.eventDetailSummary.textContent = `#${envelope.seq} · ${envelope.kind}`;
  elements.eventDetailJson.textContent = JSON.stringify(envelope, null, 2);
  elements.eventDetailDialog.showModal();
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

function addTimelineEvent(envelope) {
  state.eventCount += 1;
  elements.eventCount.textContent = `${state.eventCount} events`;
  const item = document.createElement("li");
  const detailButton = document.createElement("button");
  detailButton.type = "button";
  detailButton.className = "event-entry";
  detailButton.setAttribute("aria-label", `查看事件 #${envelope.seq} ${envelope.kind} 的完整消息体`);
  const sequence = document.createElement("span");
  sequence.className = "seq";
  sequence.textContent = `#${envelope.seq}`;
  const kind = document.createElement("span");
  if (envelope.kind.startsWith("tool.")) kind.className = "tool";
  if (envelope.kind.startsWith("run.") && envelope.kind !== "run.started") {
    kind.className = "terminal";
  }
  kind.textContent = envelope.kind;
  detailButton.append(sequence, kind);
  detailButton.addEventListener("click", () => showEventDetail(envelope, detailButton));
  item.append(detailButton);
  elements.eventList.append(item);
}

function ensureLiveAssistant() {
  if (!state.liveAssistantContent) {
    state.liveAssistantContent = appendMessage("assistant", "", { turnStatus: "running" });
    state.liveAssistantContent.textContent = "";
  }
  return state.liveAssistantContent;
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
  if (envelope.kind === "assistant.block.started") {
    const block = document.createElement("div");
    block.className = "assistant-block";
    block.dataset.blockId = payload.block_id;
    ensureLiveAssistant().append(block);
    state.blocks.set(payload.block_id, block);
  } else if (envelope.kind === "assistant.block.delta") {
    const block = state.blocks.get(payload.block_id);
    if (block) block.textContent += payload.text || "";
  } else if (envelope.kind === "assistant.block.finished") {
    const block = state.blocks.get(payload.block_id);
    if (block) block.textContent = payload.content || block.textContent;
  } else if (envelope.kind === "assistant.block.discarded") {
    const block = state.blocks.get(payload.block_id);
    if (block) block.remove();
    state.blocks.delete(payload.block_id);
  }

  if (TERMINAL_EVENTS.has(envelope.kind)) {
    runContext.terminalSeen = true;
    runContext.terminalKind = envelope.kind;
    setRunControls();
  }
}

function decodeSseFrame(frame) {
  let data = "";
  for (const line of frame.replaceAll("\r", "").split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("data:")) data += `${line.slice(5).trimStart()}\n`;
  }
  if (!data) return null;
  return JSON.parse(data.slice(0, -1));
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
        const envelope = decodeSseFrame(frame);
        if (envelope) renderEnvelope(envelope, runContext);
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
  const eventsUrl = `/api/runs/${encodeURIComponent(runContext.runId)}/events`;
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
  setStatus(`${terminalStatus[runContext.terminalKind] || "Run 已结束"}${suffix}`);
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
  clearTimeline(runId);
  setRunControls();
  setStatus("已恢复正在执行的 Run，正在重连事件流");
  void driveRun(runContext);
}

elements.runForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (
    state.activeRun !== null || state.settling || state.mutationPending ||
    state.sessionId === null
  ) {
    return;
  }
  const message = elements.messageInput.value;
  if (!message.trim()) {
    setStatus("消息不能为空");
    return;
  }
  const sessionId = state.sessionId;
  state.settling = true;
  setRunControls();
  let runContext = null;
  try {
    const response = await api(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    const run = await response.json();
    if (
      typeof run.run_id !== "string" || !RUN_ID_PATTERN.test(run.run_id) ||
      typeof run.events_url !== "string" || run.session_id !== sessionId
    ) {
      throw new Error("Run 响应格式无效");
    }
    const expectedEventsUrl = `/api/runs/${encodeURIComponent(run.run_id)}/events`;
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
    state.liveAssistantContent = null;
    elements.messageInput.value = "";
    appendMessage("user", message, { runId: run.run_id, turnStatus: "running" });
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
    await api(`/api/runs/${encodeURIComponent(runContext.runId)}/cancel`, {
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

void restoreLoginSession();

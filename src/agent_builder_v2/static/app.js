"use strict";

const state = {
  csrfToken: null,
  agentId: null,
  runId: null,
  streamController: null,
  blocks: new Map(),
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
  runForm: document.querySelector("#run-form"),
  messageInput: document.querySelector("#message-input"),
  runButton: document.querySelector("#run-button"),
  cancelButton: document.querySelector("#cancel-button"),
  runId: document.querySelector("#run-id"),
  assistantOutput: document.querySelector("#assistant-output"),
  eventList: document.querySelector("#event-list"),
  eventCount: document.querySelector("#event-count"),
  eventDetailDialog: document.querySelector("#event-detail-dialog"),
  eventDetailClose: document.querySelector("#event-detail-close"),
  eventDetailSummary: document.querySelector("#event-detail-summary"),
  eventDetailJson: document.querySelector("#event-detail-json"),
};

function setAuthenticated(session) {
  state.csrfToken = session.csrf_token;
  state.agentId = session.agent_id;
  elements.agentId.textContent = session.agent_id;
  elements.statusDot.classList.add("online");
  elements.statusText.textContent = "控制面已连接";
  elements.loginPanel.hidden = true;
  elements.workspace.hidden = false;
  elements.logoutButton.hidden = false;
}

function setUnauthenticated(message = "尚未认证") {
  state.csrfToken = null;
  state.agentId = null;
  state.runId = null;
  state.streamController?.abort();
  state.streamController = null;
  elements.statusDot.classList.remove("online");
  elements.statusText.textContent = message;
  elements.loginPanel.hidden = false;
  elements.workspace.hidden = true;
  elements.logoutButton.hidden = true;
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
    const error = new Error(body.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response;
}

async function restoreSession() {
  try {
    const response = await api("/api/auth/status");
    const session = await response.json();
    if (session.authenticated) setAuthenticated(session);
    else setUnauthenticated();
  } catch (_error) {
    setUnauthenticated();
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

function resetRunView(runId) {
  if (elements.eventDetailDialog.open) elements.eventDetailDialog.close();
  state.blocks.clear();
  state.eventCount = 0;
  elements.assistantOutput.replaceChildren();
  elements.eventList.replaceChildren();
  elements.eventCount.textContent = "0 events";
  elements.runId.textContent = runId;
}

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
  if (event.target === elements.eventDetailDialog) {
    elements.eventDetailDialog.close();
  }
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
  detailButton.setAttribute(
    "aria-label",
    `查看事件 #${envelope.seq} ${envelope.kind} 的完整消息体`,
  );
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
  detailButton.addEventListener("click", () => {
    showEventDetail(envelope, detailButton);
  });
  item.append(detailButton);
  elements.eventList.append(item);
}

function renderEnvelope(envelope) {
  addTimelineEvent(envelope);
  const payload = envelope.payload || {};
  if (envelope.kind === "assistant.block.started") {
    const block = document.createElement("div");
    block.className = "assistant-block";
    block.dataset.blockId = payload.block_id;
    elements.assistantOutput.append(block);
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

  if (["run.completed", "run.failed", "run.cancelled"].includes(envelope.kind)) {
    elements.runButton.disabled = false;
    elements.cancelButton.disabled = true;
    state.runId = null;
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

async function consumeEventStream(url, controller) {
  const response = await api(url, {
    headers: { Accept: "text/event-stream" },
    signal: controller.signal,
  });
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
      if (envelope) renderEnvelope(envelope);
    }
    if (done) break;
  }
}

elements.runForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  state.streamController?.abort();
  try {
    const response = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ message: elements.messageInput.value }),
    });
    const run = await response.json();
    state.runId = run.run_id;
    resetRunView(run.run_id);
    elements.runButton.disabled = true;
    elements.cancelButton.disabled = false;
    const controller = new AbortController();
    state.streamController = controller;
    try {
      await consumeEventStream(run.events_url, controller);
    } finally {
      if (state.streamController === controller) state.streamController = null;
    }
    if (state.runId === run.run_id) {
      throw new Error("事件流在终态前结束");
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      elements.statusText.textContent = error.message;
      elements.runButton.disabled = false;
      elements.cancelButton.disabled = true;
    }
  }
});

elements.cancelButton.addEventListener("click", async () => {
  if (!state.runId) return;
  elements.cancelButton.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(state.runId)}/cancel`, {
      method: "POST",
    });
  } catch (error) {
    elements.statusText.textContent = error.message;
  }
});

restoreSession();

"""Executable Chromium regressions for the dependency-free Web client."""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "agent_builder_v2" / "static"
CHROMIUM = shutil.which("chromium")


_BROWSER_TEST = r"""
window.setTimeout(async () => {
  const input = document.querySelector("#message-input");
  const usage = document.querySelector("#message-byte-usage");
  const run = document.querySelector("#run-button");
  const form = document.querySelector("#run-form");
  const messages = document.querySelector("#conversation-messages");
  document.querySelector("#workspace").hidden = false;
  const outcomes = {};

  const enter = (value) => {
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return {
      count: messageByteCount(),
      over: usage.dataset.overLimit,
      retained: input.value === value,
    };
  };
  outcomes.ascii8192 = enter("a".repeat(8192));
  outcomes.ascii8193 = enter("a".repeat(8193));
  outcomes.cjk8192 = enter("你".repeat(2730) + "ab");
  outcomes.emoji8192 = enter("😀".repeat(2048));
  outcomes.escaped = enter("quote=\" slash=\\ newline=\n");
  outcomes.escaped.expected = new TextEncoder().encode(input.value).length;

  let submits = 0;
  form.requestSubmit = () => { submits += 1; };
  input.value = "draft";
  run.disabled = false;
  input.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Enter", bubbles: true, cancelable: true, isComposing: true,
  }));
  outcomes.imeSubmits = submits;
  input.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Enter", bubbles: true, cancelable: true, isComposing: false,
  }));
  outcomes.enterSubmits = submits;
  outcomes.enterDraftRetained = input.value === "draft";

  messages.style.height = "80px";
  messages.style.maxHeight = "80px";
  messages.style.overflow = "auto";
  messages.style.display = "block";
  const spacer = document.createElement("div");
  spacer.style.height = "2000px";
  spacer.style.minHeight = "2000px";
  spacer.textContent = "browser behavior spacer";
  messages.append(spacer);
  messages.scrollTop = 0;
  scheduleConversationLatest({ force: true });
  outcomes.forcedLatest = messages.scrollTop > 0;
  outcomes.scrollMetrics = {
    top: messages.scrollTop,
    height: messages.scrollHeight,
    client: messages.clientHeight,
  };

  const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const originalExecCommand = document.execCommand;
  const copyTrigger = document.createElement("button");
  copyTrigger.textContent = "复制";
  document.body.append(copyTrigger);
  try {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    document.execCommand = () => false;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await copyMessageContent("sensitive clipboard fallback body", copyTrigger);
    }
    outcomes.copyFallback = {
      residualNodes: document.querySelectorAll(".clipboard-fallback").length,
      bodyRetained: document.body.textContent.includes("sensitive clipboard fallback body"),
    };
  } finally {
    document.execCommand = originalExecCommand;
    if (clipboardDescriptor) {
      Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
    } else {
      delete navigator.clipboard;
    }
    copyTrigger.remove();
  }

  const result = document.createElement("output");
  result.id = "browser-test-result";
  result.dataset.payload = JSON.stringify(outcomes);
  result.textContent = "complete";
  document.body.append(result);
}, 250);
"""


_RESPONSIVE_TEST = r"""
window.setTimeout(async () => {
  const workspace = document.querySelector("#workspace");
  const login = document.querySelector("#login-panel");
  const input = document.querySelector("#message-input");
  const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  login.hidden = true;
  workspace.hidden = false;
  workspace.classList.remove("navigation-open", "runtime-open");
  setConnectionStatus("控制面已连接");
  input.disabled = false;
  input.value = "一条用于验证窄屏输入区布局的消息。\n第二行内容。";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus({ preventScroll: true });

  state.agentId = "00000000-0000-4000-8000-000000000001";
  state.sessionId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const chatOnlyProjection = {
    version: "next-turn-chat-only-projection-v1",
    availability: "available",
    projection_mode: "chat_only",
    operational_context_tokens: 32768,
    fixed_context_tokens: 24576,
    fixed_context_error_margin_tokens: 256,
    safe_user_tokens: 3840,
    compact_before_user_tokens: 1792,
    soft_trigger_tokens: 26624,
    hard_input_tokens: 28672,
    projection_strategy: "full",
    count_basis: "provider-calibrated-context-v1",
    renderer_version: "provider-renderer-v1",
    toolset_digest: "0".repeat(64),
  };
  const availablePreview = {
    availability: "unavailable",
    stale_reason: "toolset_calibration_unavailable",
    model_id: "qwen3.5:2b",
    operational_context_tokens: 32768,
    count_version: "soft-context-estimate-v1",
    chat_only_projection: chatOnlyProjection,
  };
  const validatedPreview = {
    version: "next-turn-projection-v1",
    agent_id: state.agentId,
    conversation_id: state.sessionId,
    conversation_revision: 2,
    model_id: "qwen3.5:2b",
    availability: "unavailable",
    projection_mode: "conservative_tools",
    chat_calibration_available: true,
    operational_context_tokens: 32768,
    single_message_byte_limit: 8192,
    fixed_context_tokens: null,
    fixed_context_error_margin_tokens: null,
    safe_user_tokens: null,
    compact_before_user_tokens: null,
    soft_trigger_tokens: 26624,
    hard_input_tokens: 28672,
    chat_only_projection: chatOnlyProjection,
  };
  const previewValidation = {
    acceptsExact: validNextTurnPreview(
      validatedPreview, state.sessionId, "qwen3.5:2b"
    ),
    rejectsExtraNestedField: !validNextTurnPreview({
      ...validatedPreview,
      chat_only_projection: { ...chatOnlyProjection, unexpected: true },
    }, state.sessionId, "qwen3.5:2b"),
  };
  state.nextTurnPreview = availablePreview;
  renderSessionContextUsage();
  const availableContext = {
    label: elements.contextUsageLabel.textContent,
    value: elements.contextUsageValue.textContent,
    detail: elements.contextUsageDetail.textContent,
    meterMax: elements.contextUsageMeter.getAttribute("aria-valuemax"),
    meterNow: elements.contextUsageMeter.getAttribute("aria-valuenow"),
    meterText: elements.contextUsageMeter.getAttribute("aria-valuetext"),
  };
  state.nextTurnPreview = {
    ...availablePreview,
    availability: "available",
    stale_reason: null,
    projection_mode: "conservative_tools",
    fixed_context_tokens: 1024,
    fixed_context_error_margin_tokens: 128,
    safe_user_tokens: 27520,
    compact_before_user_tokens: 25472,
    soft_trigger_tokens: 26624,
    hard_input_tokens: 28672,
    projection_strategy: "full",
    count_basis: "provider-calibrated-context-v1",
  };
  renderSessionContextUsage();
  const conservativeContextValue = elements.contextUsageValue.textContent;
  state.nextTurnPreview = {
    availability: "unavailable",
    stale_reason: "toolset_calibration_unavailable",
    model_id: "qwen3.5:2b",
    operational_context_tokens: 32768,
    chat_only_projection: null,
  };
  renderSessionContextUsage();
  const unavailableContext = {
    value: elements.contextUsageValue.textContent,
    detail: elements.contextUsageDetail.textContent,
  };
  state.nextTurnPreview = availablePreview;
  renderSessionContextUsage();

  const rect = (selector) => {
    const value = document.querySelector(selector).getBoundingClientRect();
    return {
      top: value.top,
      right: value.right,
      bottom: value.bottom,
      left: value.left,
      width: value.width,
      height: value.height,
    };
  };
  const viewport = window.visualViewport || {
    width: window.innerWidth,
    height: window.innerHeight,
  };
  const visibleInteractive = [
    ...document.querySelectorAll(
      "#workspace button:not(.workspace-backdrop), #workspace select, " +
      "#composer-more > summary, .session-menu-trigger"
    ),
  ].filter((element) => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      box.width > 0 && box.height > 0 && box.right > 0 && box.left < viewport.width &&
      box.bottom > 0 && box.top < viewport.height;
  });
  const undersizedTargets = visibleInteractive
    .filter((element) => {
      const box = element.getBoundingClientRect();
      return box.width < 43.5 || box.height < 43.5;
    })
    .map((element) => ({
      id: element.id || element.getAttribute("aria-label") || element.tagName,
      width: element.getBoundingClientRect().width,
      height: element.getBoundingClientRect().height,
    }));

  const focusRestoration = {};
  if (narrowWorkspace()) {
    setNavigation(true, { focus: true });
    focusRestoration.navigationOpen = document.activeElement === elements.navigationClose;
    elements.navigationClose.click();
    focusRestoration.navigationClose = document.activeElement === elements.navigationToggle;
  }
  setRuntimeInspector(true, { focus: true });
  focusRestoration.runtimeOpen = document.activeElement === elements.runtimeInspectorClose;
  elements.workspaceBackdrop.click();
  focusRestoration.runtimeBackdrop = document.activeElement === elements.runtimeInspectorButton;
  if (narrowWorkspace()) {
    setNavigation(true, { focus: true });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    focusRestoration.navigationEscape = document.activeElement === elements.navigationToggle;
  }

  const messageProbe = document.createElement("div");
  messageProbe.className = "message-content";
  messageProbe.textContent = "正文大小探针";
  document.querySelector("#conversation-messages").append(messageProbe);
  const messageFontPx = parseFloat(getComputedStyle(messageProbe).fontSize);
  messageProbe.remove();

  const durationMs = (value) => value.split(",").reduce((maximum, item) => {
    const normalized = item.trim();
    const milliseconds = normalized.endsWith("ms")
      ? parseFloat(normalized)
      : parseFloat(normalized) * 1000;
    return Math.max(maximum, Number.isFinite(milliseconds) ? milliseconds : 0);
  }, 0);
  const motionStyle = getComputedStyle(document.querySelector(".navigation-rail"));
  const reducedMotion = {
    requested: matchMedia("(prefers-reduced-motion: reduce)").matches,
    transitionMs: durationMs(motionStyle.transitionDuration),
    animationMs: durationMs(motionStyle.animationDuration),
  };

  const liveRegions = {
    connection: {
      role: elements.statusText.getAttribute("role"),
      live: elements.statusText.getAttribute("aria-live"),
    },
    interaction: {
      role: elements.interactionLiveStatus.getAttribute("role"),
      live: elements.interactionLiveStatus.getAttribute("aria-live"),
    },
    preparation: {
      role: elements.preparationLiveStatus.getAttribute("role"),
      live: elements.preparationLiveStatus.getAttribute("aria-live"),
    },
    running: {
      role: elements.runLiveStatus.getAttribute("role"),
      live: elements.runLiveStatus.getAttribute("aria-live"),
    },
    failure: {
      role: elements.failureLiveStatus.getAttribute("role"),
      live: elements.failureLiveStatus.getAttribute("aria-live"),
    },
    conversationLive: elements.conversationMessages.getAttribute("aria-live"),
  };

  const naturalWorkspaceHeight = workspace.getBoundingClientRect().height;
  const simulatedHeight = Math.max(320, naturalWorkspaceHeight - 180);
  workspace.style.height = `${simulatedHeight}px`;
  await wait(30);
  const keyboardWorkspace = rect("#workspace");
  const keyboardComposer = rect(".composer-dock");
  const keyboardLayout = {
    workspace: keyboardWorkspace,
    composer: keyboardComposer,
    staysVisible: keyboardComposer.bottom <= keyboardWorkspace.bottom + 0.5 &&
      keyboardComposer.top >= keyboardWorkspace.top - 0.5,
  };
  workspace.style.height = "";
  await wait(20);

  const statusStyle = getComputedStyle(elements.statusText);
  const criticalAidFonts = [
    "#context-usage-detail",
    "#context-usage-label",
    "#session-list-status",
    "#composer-status",
  ].map((selector) => parseFloat(getComputedStyle(document.querySelector(selector)).fontSize));
  const outcome = {
    viewport: {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      visualWidth: viewport.width,
      visualHeight: viewport.height,
    },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      bodyClientWidth: document.body.clientWidth,
      bodyScrollWidth: document.body.scrollWidth,
    },
    masthead: rect(".masthead"),
    workspace: rect("#workspace"),
    primary: rect(".primary-workspace"),
    composer: rect(".composer-dock"),
    toolbar: rect(".composer-toolbar"),
    input: rect("#message-input"),
    contextFontPx: parseFloat(getComputedStyle(
      document.querySelector("#context-usage-detail")
    ).fontSize),
    contextValueFontPx: parseFloat(getComputedStyle(
      document.querySelector("#context-usage-value")
    ).fontSize),
    messageFontPx,
    minimumCriticalAidFontPx: Math.min(...criticalAidFonts),
    connectionWording: {
      text: elements.statusText.textContent,
      visible: statusStyle.display !== "none" && statusStyle.visibility !== "hidden",
    },
    meterLabel: document.querySelector("#context-usage-meter").getAttribute("aria-label"),
    meterDescription: document.querySelector("#context-usage-meter").getAttribute(
      "aria-describedby"
    ),
    availableContext,
    conservativeContextValue,
    unavailableContext,
    previewValidation,
    focusRestoration,
    liveRegions,
    reducedMotion,
    keyboardLayout,
    undersizedTargets,
  };
  const result = document.createElement("output");
  result.id = "responsive-test-result";
  result.dataset.payload = JSON.stringify(outcome);
  result.textContent = "complete";
  document.body.append(result);
}, 300);
"""


_MULTISESSION_TEST = r"""
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

window.setTimeout(async () => {
  const outcomes = {};
  const agentId = "00000000-0000-4000-8000-000000000001";
  const secondAgentId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const sessionA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const sessionB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  const sessionC = "cccccccccccccccccccccccccccccccc";
  const runA = "11111111111111111111111111111111";
  const modelId = "qwen3.5:2b";
  const agentRecords = [
    {
      agent_id: agentId,
      display_name: "系统智能体",
      generation: 1,
      state: "active",
      created_at: "2026-07-22T00:00:00.000Z",
      updated_at: "2026-07-22T00:00:00.000Z",
    },
    {
      agent_id: secondAgentId,
      display_name: "研究智能体",
      generation: 1,
      state: "active",
      created_at: "2026-07-22T00:00:00.000Z",
      updated_at: "2026-07-22T00:00:00.000Z",
    },
  ];

  const summary = (sessionId, title, messageCount = 0, sessionState = "idle") => ({
    session_id: sessionId,
    title,
    revision: 0,
    created_at: "2026-07-22T00:00:00.000Z",
    updated_at: "2026-07-22T00:01:00.000Z",
    message_count: messageCount,
    state: sessionState,
  });
  const page = ({
    limit = 32,
    returned = 0,
    total = returned,
    oldest = returned ? 1 : null,
    newest = returned ? returned : null,
    older = false,
    newer = false,
    cursor = null,
    before = null,
  } = {}) => ({
    version: "turn-page-v2",
    limit,
    before_cursor: before,
    returned_turns: returned,
    total_turns: total,
    oldest_position: oldest,
    newest_position: newest,
    has_older: older,
    has_newer: newer,
    next_before_cursor: cursor,
  });
  const turnMessages = (position, prefix, status = "completed", contentSize = 0) => {
    const turnId = position.toString(16).padStart(32, "0");
    const runId = (position + 100).toString(16).padStart(32, "0");
    const padding = contentSize ? ` ${"x".repeat(contentSize)}` : "";
    return [
      {
        message_id: `${prefix}-${position}-user`,
        role: "user",
        content: `${prefix} user ${position}${padding}`,
        created_at: `2026-07-22T00:00:0${position}.000Z`,
        turn_id: turnId,
        run_id: runId,
        turn_position: position,
        turn_status: status,
      },
      {
        message_id: `${prefix}-${position}-assistant`,
        role: "assistant",
        content: `${prefix} assistant ${position}${padding}`,
        created_at: `2026-07-22T00:00:0${position}.500Z`,
        turn_id: turnId,
        run_id: runId,
        turn_position: position,
        turn_status: status,
      },
    ];
  };

  const sessionRecords = new Map([
    [sessionA, summary(sessionA, "会话 A", 2, "idle")],
    [sessionB, summary(sessionB, "会话 B", 2, "idle")],
    [sessionC, summary(sessionC, "会话 C", 0, "idle")],
  ]);
  const details = new Map([
    [sessionA, {
      session: sessionRecords.get(sessionA),
      messages: turnMessages(1, "A persisted"),
      page: page({ returned: 1 }),
    }],
    [sessionB, {
      session: sessionRecords.get(sessionB),
      messages: turnMessages(1, "B visible"),
      page: page({ returned: 1 }),
    }],
    [sessionC, {
      session: sessionRecords.get(sessionC),
      messages: [],
      page: page(),
    }],
  ]);
  const requests = [];
  const preparationStages = ["reading_history", "summarizing_history", "admitting_run"];
  let preparationReads = 0;
  let preparationCancelCalls = 0;
  const jsonResponse = (value, status = 200) => new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), window.location.origin);
    const method = String(init.method || "GET").toUpperCase();
    requests.push({ method, path: `${url.pathname}${url.search}`, body: init.body || null });

    if (method === "GET" && url.pathname === "/api/agents") {
      return jsonResponse({ agents: agentRecords });
    }
    const scopedAgent = url.pathname.match(/^\/api\/agents\/([a-f0-9-]{36})/i)?.[1];
    if (method === "GET" && url.pathname.endsWith("/research-environment")) {
      return jsonResponse({ agent_id: scopedAgent, installed: false, environment: null });
    }
    if (method === "GET" && url.pathname.endsWith("/commands")) {
      return jsonResponse({
        schema_version: 1,
        commands: [{
          command_id: "status",
          name: "/status",
          description: "查看状态",
          argument_schema: "",
        }],
      });
    }
    if (method === "POST" && url.pathname === "/api/auth/logout") {
      return new Response(null, { status: 204 });
    }
    if (method === "GET" && url.pathname.endsWith("/preparation")) {
      const stage = preparationStages[Math.min(preparationReads, preparationStages.length - 1)];
      preparationReads += 1;
      return jsonResponse({
        version: "run-preparation-v1",
        state: "preparing",
        operation_id: "34343434343434343434343434343434",
        stage,
        elapsed_ms: preparationReads * 400,
      });
    }
    if (method === "POST" && url.pathname.endsWith("/preparation/cancel")) {
      preparationCancelCalls += 1;
      return jsonResponse({
        version: "run-preparation-cancel-v1",
        state: "cancellation_requested",
        target: "preparation",
      });
    }
    if (url.pathname.endsWith("/subagents")) {
      return jsonResponse({ delegations: [] });
    }
    if (url.pathname.endsWith("/context-preview")) {
      return jsonResponse({ detail: "preview intentionally omitted" }, 404);
    }
    if (
      method === "GET" && url.searchParams.get("before") === "opaqueBefore3" &&
      url.pathname.endsWith(`/sessions/${sessionA}`)
    ) {
      const duplicate = {
        ...turnMessages(3, "current", "completed", 260)[0],
        message_id: "current-3-user",
      };
      return jsonResponse({
        session: sessionRecords.get(sessionA),
        messages: [
          ...turnMessages(1, "older", "completed", 260),
          ...turnMessages(2, "older", "completed", 260),
          duplicate,
        ],
        page: page({
          limit: 32,
          returned: 2,
          total: 4,
          oldest: 1,
          newest: 2,
          older: false,
          newer: true,
          cursor: null,
          before: "opaqueBefore3",
        }),
      });
    }
    if (method === "PATCH" && url.pathname.endsWith(`/sessions/${sessionA}`)) {
      const body = JSON.parse(init.body || "{}");
      if (body.revision !== sessionRecords.get(sessionA).revision) {
        return jsonResponse({ detail: { code: "session_rename_conflict" } }, 409);
      }
      const renamed = {
        ...sessionRecords.get(sessionA),
        title: body.title,
        revision: body.revision + 1,
      };
      sessionRecords.set(sessionA, renamed);
      details.set(sessionA, { ...details.get(sessionA), session: renamed });
      return jsonResponse(renamed);
    }
    if (
      method === "POST" &&
      url.pathname.endsWith(`/sessions/${sessionA}/continue`)
    ) {
      return jsonResponse({
        ...sessionRecords.get(sessionC),
        relationship: {
          version: "conversation-relationship-v1",
          type: "branch",
          source_session_id: sessionA,
          source_preserved: true,
          branch_point: "completed_head",
        },
      });
    }
    if (method === "GET" && /\/sessions$/.test(url.pathname)) {
      if (url.pathname.includes(`/agents/${secondAgentId}/`)) {
        return jsonResponse({ sessions: [] });
      }
      return jsonResponse({ sessions: Array.from(sessionRecords.values()) });
    }
    const detailMatch = url.pathname.match(/\/sessions\/([a-f0-9]{32})$/);
    if (method === "GET" && detailMatch && details.has(detailMatch[1])) {
      return jsonResponse(details.get(detailMatch[1]));
    }
    return jsonResponse({ detail: `unhandled test route ${method} ${url.pathname}` }, 404);
  };

  document.querySelector("#login-panel").hidden = true;
  document.querySelector("#workspace").hidden = false;
  document.querySelector("#workspace").classList.add("navigation-open");
  state.csrfToken = "test-csrf";
  state.agentId = agentId;
  state.agents = agentRecords;
  state.models = [{
    model_id: modelId,
    provider: "ollama",
    model: modelId,
    operational_context_tokens: 32768,
    max_output_tokens: 4096,
    supports_tools: true,
  }];
  state.defaultModelId = modelId;
  state.selectedModelId = modelId;
  elements.modelSelect.replaceChildren(new Option(modelId, modelId));
  elements.modelSelect.value = modelId;
  state.sessions = [sessionRecords.get(sessionA), sessionRecords.get(sessionB)];
  state.sessionId = sessionA;
  state.activeRuns.clear();
  state.preparingRuns.clear();
  state.sessionDrafts.clear();
  state.backgroundCompletions.clear();
  state.conversationPage = page({ returned: 1 });
  renderMessages(turnMessages(1, "A running", "running"));
  elements.messageInput.value = "A draft survives switching";

  const runContextA = {
    agentId,
    agentEpoch: state.agentEpoch,
    runId: runA,
    sessionId: sessionA,
    lastSeq: 0,
    terminalSeen: false,
    terminalKind: null,
    terminalPayload: null,
    cancelPending: false,
    controller: null,
    driverPromise: null,
    transportTimer: null,
    transportAttempt: null,
    assistantBlocks: new Map(),
  };
  state.activeRuns.set(sessionA, runContextA);
  state.sessionDrafts.set(conversationStateKey(sessionB, agentId), {
    message: "B independent draft",
    modelId,
    compact: false,
  });
  renderSessionList();

  await selectSession(sessionB, { preserveTimeline: true });
  setStatus("B status must remain stable");
  const beforeBackground = {
    text: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    status: elements.composerStatus.textContent,
  };
  const envelope = (seq, kind, payload) => ({
    schema_version: "2.2-prototype",
    event_id: seq.toString(16).padStart(32, "0"),
    agent_id: agentId,
    conversation_id: sessionA,
    turn_id: "99999999999999999999999999999999",
    run_id: runA,
    parent_run_id: null,
    seq,
    occurred_at: `2026-07-22T00:02:0${seq}.000Z`,
    kind,
    durability: kind === "assistant.block.delta" ? "ephemeral" : "durable",
    payload,
  });
  renderEnvelope(envelope(1, "assistant.block.started", {
    block_id: "answer",
    block_type: "content",
  }), runContextA);
  renderEnvelope(envelope(2, "assistant.block.delta", {
    block_id: "answer",
    text: "A secret background stream",
  }), runContextA);
  renderEnvelope(envelope(3, "assistant.block.finished", {
    block_id: "answer",
    content: "A final background stream",
  }), runContextA);
  renderEnvelope(envelope(4, "run.completed", {
    reason: "end_turn",
    model_iterations: 1,
    usage: {
      input_tokens: 100,
      output_tokens: 20,
      last_input_tokens: 100,
      complete: true,
    },
  }), runContextA);
  const afterBackground = {
    text: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    status: elements.composerStatus.textContent,
    aBuffered: runAssistantContent(runContextA),
    aTimelineEvents: state.timelineEntriesByRun.get(runA)?.length || 0,
    trackedBeforeCompletion: runIsTracked(runContextA),
    scope: {
      stateAgent: state.agentId,
      stateEpoch: state.agentEpoch,
      runAgent: runContextA.agentId,
      runEpoch: runContextA.agentEpoch,
    },
  };
  completeRunContext(runContextA, false);
  const markerAfterCompletion = Array.from(elements.sessionList.querySelectorAll(".session-item"))
    .find((item) => item.querySelector(".session-select")?.dataset.sessionId === sessionA)
    ?.querySelector(".session-state")?.textContent || "";

  await selectSession(sessionA, { preserveTimeline: true });
  const markerAfterReturn = Array.from(elements.sessionList.querySelectorAll(".session-item"))
    .find((item) => item.querySelector(".session-select")?.dataset.sessionId === sessionA)
    ?.querySelector(".session-state")?.textContent || "";
  outcomes.backgroundRun = {
    beforeBackground,
    afterBackground,
    markerAfterCompletion,
    returnedText: elements.conversationMessages.textContent,
    returnedDraft: elements.messageInput.value,
    markerAfterReturn,
    activeAfterCompletion: state.activeRuns.has(sessionA),
    completionMarkerRetained: state.backgroundCompletions.has(sessionA),
  };

  const repetitionRun = "12121212121212121212121212121212";
  const repetitionTurn = "34343434343434343434343434343434";
  const repetitionMarker = "\n\n[回答因重复死循环被截断；后续忽略重复尾部。]";
  const repetitionContent = `bounded answer${repetitionMarker}`;
  const repetitionContext = {
    agentId,
    agentEpoch: state.agentEpoch,
    runId: repetitionRun,
    sessionId: sessionA,
    lastSeq: 0,
    terminalSeen: false,
    terminalKind: null,
    terminalPayload: null,
    cancelPending: false,
    controller: null,
    driverPromise: null,
    transportTimer: null,
    transportAttempt: null,
    assistantBlocks: new Map(),
  };
  const repetitionEnvelope = (seq, kind, payload) => ({
    ...envelope(seq, kind, payload),
    event_id: (100 + seq).toString(16).padStart(32, "0"),
    turn_id: repetitionTurn,
    run_id: repetitionRun,
  });
  state.activeRuns.set(sessionA, repetitionContext);
  state.timelineRuns.push({
    runId: repetitionRun,
    turnId: repetitionTurn,
    turnNumber: 2,
    status: "running",
    kind: "conversation",
  });
  state.selectedTimelineRunId = repetitionRun;
  state.timelineEntries = [];
  state.timelineEntriesByRun.set(repetitionRun, state.timelineEntries);
  renderMessages([{
    message_id: "repetition-user",
    role: "user",
    content: "write a joke",
    created_at: "2026-07-22T00:03:00.000Z",
    turn_id: repetitionTurn,
    run_id: repetitionRun,
    turn_position: 2,
    turn_status: "running",
  }]);
  renderEnvelope(repetitionEnvelope(1, "assistant.block.started", {
    block_id: "answer",
    block_type: "content",
  }), repetitionContext);
  renderEnvelope(repetitionEnvelope(2, "assistant.block.delta", {
    block_id: "answer",
    text: repetitionContent,
  }), repetitionContext);
  const repetitionResponse = repetitionEnvelope(3, "model.response.finished", {
    request_id: "model-1",
    iteration: 1,
    attempt: 0,
    recovery_id: null,
    provider_call_index: 1,
    outcome: "repetition_truncated",
    input_tokens: 0,
    output_tokens: 0,
    usage_complete: false,
    error_code: null,
  });
  renderEnvelope(repetitionResponse, repetitionContext);
  renderEnvelope(repetitionEnvelope(4, "assistant.block.finished", {
    block_id: "answer",
    content: repetitionContent,
  }), repetitionContext);
  renderEnvelope(repetitionEnvelope(5, "run.completed", {
    reason: "repetition_truncated",
    model_iterations: 1,
    usage: {
      input_tokens: 0,
      output_tokens: 0,
      last_input_tokens: 0,
      complete: false,
    },
  }), repetitionContext);
  renderMessages([
    {
      message_id: "repetition-user",
      role: "user",
      content: "write a joke",
      created_at: "2026-07-22T00:03:00.000Z",
      turn_id: repetitionTurn,
      run_id: repetitionRun,
      turn_position: 2,
      turn_status: "completed",
    },
    {
      message_id: "repetition-assistant",
      role: "assistant",
      content: repetitionContent,
      created_at: "2026-07-22T00:03:01.000Z",
      turn_id: repetitionTurn,
      run_id: repetitionRun,
      turn_position: 2,
      turn_status: "completed",
    },
  ]);
  completeRunContext(repetitionContext, false);
  const repetitionText = elements.conversationMessages.textContent;
  outcomes.repetitionCompletion = {
    turnCards: elements.conversationMessages.querySelectorAll(".turn-card").length,
    assistantMessages: elements.conversationMessages.querySelectorAll(
      ".message-assistant",
    ).length,
    markerCount: repetitionText.split("回答因重复死循环被截断").length - 1,
    completedLabel: elements.conversationMessages.querySelector(".turn-status")?.textContent,
    usageComplete: state.sessionTurnUsage?.complete,
    usageText: elements.turnTokenUsageValue.textContent,
    eventSummary: eventSummary(repetitionResponse),
    terminalKind: repetitionContext.terminalKind,
    terminalReason: repetitionContext.terminalPayload?.reason,
    status: elements.composerStatus.textContent,
  };

  const currentMessages = [
    ...turnMessages(3, "current", "completed", 260),
    ...turnMessages(4, "current", "completed", 260),
  ];
  state.conversationPage = page({
    limit: 32,
    returned: 2,
    total: 4,
    oldest: 3,
    newest: 4,
    older: true,
    newer: false,
    cursor: "opaqueBefore3",
  });
  state.conversationFollowLatest = false;
  renderMessages(currentMessages);
  state.conversationFollowLatest = false;
  elements.conversationMessages.style.height = "180px";
  elements.conversationMessages.style.maxHeight = "180px";
  elements.conversationMessages.style.overflow = "auto";
  await wait(20);
  elements.conversationMessages.scrollTop = Math.min(
    80,
    Math.max(1, elements.conversationMessages.scrollHeight - 180),
  );
  const anchorSelector = `[data-turn-id="${(3).toString(16).padStart(32, "0")}"]`;
  const anchorBefore = elements.conversationMessages.querySelector(anchorSelector)
    .getBoundingClientRect().top;
  const heightBefore = elements.conversationMessages.scrollHeight;
  const topBefore = elements.conversationMessages.scrollTop;
  await loadOlderConversationPage();
  await wait(20);
  const anchorAfter = elements.conversationMessages.querySelector(anchorSelector)
    .getBoundingClientRect().top;
  const messageIds = state.conversationMessages.map((message) => message.message_id);
  outcomes.pagination = {
    turnPositions: conversationTurnGroups().map((group) => group.position),
    messageCount: state.conversationMessages.length,
    uniqueMessageCount: new Set(messageIds).size,
    duplicateCurrentUserCount: messageIds.filter((id) => id === "current-3-user").length,
    anchorDelta: anchorAfter - anchorBefore,
    scrollDelta: elements.conversationMessages.scrollTop - topBefore,
    heightDelta: elements.conversationMessages.scrollHeight - heightBefore,
    hasOlder: state.conversationPage.has_older,
    status: elements.composerStatus.textContent,
  };

  state.sessions = [sessionRecords.get(sessionA), sessionRecords.get(sessionB)];
  renderSessionList();
  document.querySelector("#workspace").classList.add("navigation-open");
  await wait(20);
  let sessionItemA = Array.from(elements.sessionList.querySelectorAll(".session-item"))
    .find((item) => item.querySelector(".session-select")?.dataset.sessionId === sessionA);
  let menuTrigger = sessionItemA.querySelector(".session-menu-trigger");
  menuTrigger.focus();
  const menuRect = menuTrigger.getBoundingClientRect();
  const keyboardSummary = document.activeElement === menuTrigger && menuTrigger.tabIndex >= 0;
  const listScrollBeforeMenu = elements.sessionList.scrollTop;
  menuTrigger.click();
  await wait(20);
  const menuPopover = sessionItemA.querySelector(".session-menu-popover");
  let renameButton = sessionItemA.querySelector(".session-rename");
  let branchButton = sessionItemA.querySelector(".session-branch");
  const deleteButton = sessionItemA.querySelector(".session-delete");
  renameButton.focus();
  const keyboardRename = document.activeElement === renameButton && renameButton.tabIndex >= 0;
  branchButton.focus();
  const keyboardBranch = document.activeElement === branchButton && branchButton.tabIndex >= 0;
  const renameRect = renameButton.getBoundingClientRect();
  const branchRect = branchButton.getBoundingClientRect();
  const deleteRect = deleteButton.getBoundingClientRect();
  const popoverRect = menuPopover.getBoundingClientRect();
  const actionRects = [renameRect, branchRect, deleteRect];
  const allActionsInViewport = actionRects.every((rect) => (
    rect.top >= 0 && rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight
  ));
  const listScrollAfterMenu = elements.sessionList.scrollTop;
  window.prompt = () => "会话 A 已重命名";
  renameButton.click();
  await wait(50);

  sessionItemA = Array.from(elements.sessionList.querySelectorAll(".session-item"))
    .find((item) => item.querySelector(".session-select")?.dataset.sessionId === sessionA);
  menuTrigger = sessionItemA.querySelector(".session-menu-trigger");
  menuTrigger.click();
  await wait(10);
  branchButton = sessionItemA.querySelector(".session-branch");
  branchButton.click();
  await wait(120);
  outcomes.sessionMenu = {
    summaryVisible: menuRect.width > 0 && menuRect.height > 0 && menuRect.right <= innerWidth,
    summarySize: { width: menuRect.width, height: menuRect.height },
    keyboardSummary,
    keyboardRename,
    keyboardBranch,
    actionsVisible: actionRects.every((rect) => rect.width > 0 && rect.height > 0),
    actionSizes: actionRects.map((rect) => ({ width: rect.width, height: rect.height })),
    allActionsInViewport,
    popoverInViewport: popoverRect.top >= 0 && popoverRect.left >= 0 &&
      popoverRect.right <= innerWidth && popoverRect.bottom <= innerHeight,
    listDidNotScroll: listScrollAfterMenu === listScrollBeforeMenu,
    summaryLabel: menuTrigger.getAttribute("aria-label"),
    renamedTitle: sessionRecords.get(sessionA).title,
    renameRequest: requests.some((item) => (
      item.method === "PATCH" && item.path.endsWith(`/sessions/${sessionA}`)
    )),
    branchRequest: requests.some((item) => (
      item.method === "POST" && item.path.endsWith(`/sessions/${sessionA}/continue`)
    )),
    selectedBranch: state.sessionId,
  };

  state.sessionId = sessionB;
  const preparationProbeController = new AbortController();
  const preparationProbe = {
    agentId,
    agentEpoch: state.agentEpoch,
    sessionId: sessionB,
    controller: preparationProbeController,
    startedAt: performance.now(),
    statusController: null,
    statusTimer: null,
    statusWake: null,
    monitorStopped: false,
    cancelPending: false,
    cancelConfirmed: false,
    converged: false,
    operationId: null,
  };
  state.preparingRuns.set(sessionB, preparationProbe);
  setRunControls();
  preparationProbe.statusPromise = monitorPreparation(preparationProbe);
  await wait(650);
  const preparationSummaryStatus = elements.composerStatus.textContent;
  await wait(400);
  const preparationStatusBeforeCancel = elements.composerStatus.textContent;
  elements.cancelButton.click();
  await wait(100);
  outcomes.preparationProgress = {
    reads: preparationReads,
    summaryStatus: preparationSummaryStatus,
    statusBeforeCancel: preparationStatusBeforeCancel,
    cancelCalls: preparationCancelCalls,
    cancelConfirmed: preparationProbe.cancelConfirmed,
    requestAborted: preparationProbeController.signal.aborted,
    liveStatus: elements.preparationLiveStatus.textContent,
  };
  stopPreparationMonitor(preparationProbe);
  state.preparingRuns.delete(sessionB);

  const runControllers = [new AbortController(), new AbortController()];
  const preparationControllers = [new AbortController(), new AbortController()];
  const pageRuns = [sessionA, sessionB].map((sessionId, index) => ({
    agentId,
    agentEpoch: state.agentEpoch,
    runId: `${index + 7}`.repeat(32),
    sessionId,
    controller: runControllers[index],
    transportTimer: window.setInterval(() => {}, 1000),
    transportAttempt: { attempt: 1 },
  }));
  state.activeRuns.clear();
  pageRuns.forEach((run) => state.activeRuns.set(run.sessionId, run));
  state.preparingRuns.clear();
  state.preparingRuns.set(sessionA, {
    agentId,
    agentEpoch: state.agentEpoch,
    sessionId: sessionA,
    controller: preparationControllers[0],
  });
  state.preparingRuns.set(sessionC, {
    agentId,
    agentEpoch: state.agentEpoch,
    sessionId: sessionC,
    controller: preparationControllers[1],
  });
  window.dispatchEvent(new Event("pagehide"));
  outcomes.pagehide = {
    runAborted: runControllers.map((controller) => controller.signal.aborted),
    preparationAborted: preparationControllers.map((controller) => controller.signal.aborted),
    timersCleared: pageRuns.map((run) => run.transportTimer === null),
    attemptsCleared: pageRuns.map((run) => run.transportAttempt === null),
    trackedRuns: state.activeRuns.size,
    trackedPreparations: state.preparingRuns.size,
  };

  state.activeRuns.clear();
  state.preparingRuns.clear();
  state.agents = agentRecords;
  state.agentId = agentId;
  setAgentIdentity(agentRecords[0]);
  renderAgentList();
  const switchButton = Array.from(elements.agentList.querySelectorAll(".agent-select"))
    .find((button) => button.textContent === "切换");
  const switchFenceController = new AbortController();
  state.activeRuns.set(sessionA, {
    agentId,
    agentEpoch: state.agentEpoch,
    runId: "abababababababababababababababab",
    sessionId: sessionA,
    controller: switchFenceController,
    transportTimer: null,
    transportAttempt: null,
  });
  setRunControls();
  const disabledDuringBackgroundRun = switchButton.disabled;
  const enabledAfterBackgroundRun = !switchButton.disabled;
  switchButton.click();
  await wait(180);
  outcomes.agentSwitch = {
    disabledDuringBackgroundRun,
    oldRunAborted: switchFenceController.signal.aborted,
    enabledAfterBackgroundRun,
    selectedAgentId: state.agentId,
    selectedAgentTitle: elements.activeAgentTitle.textContent,
    status: elements.composerStatus.textContent,
    activeRuns: state.activeRuns.size,
    preparations: state.preparingRuns.size,
  };

  const logoutRunController = new AbortController();
  const logoutPreparationController = new AbortController();
  state.activeRuns.set(sessionA, {
    agentId: state.agentId,
    agentEpoch: state.agentEpoch,
    runId: "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
    sessionId: sessionA,
    controller: logoutRunController,
    transportTimer: null,
    transportAttempt: null,
  });
  state.preparingRuns.set(sessionC, {
    agentId: state.agentId,
    agentEpoch: state.agentEpoch,
    sessionId: sessionC,
    controller: logoutPreparationController,
  });
  elements.logoutButton.hidden = false;
  elements.logoutButton.click();
  await wait(80);
  outcomes.logout = {
    runAborted: logoutRunController.signal.aborted,
    preparationAborted: logoutPreparationController.signal.aborted,
    activeRuns: state.activeRuns.size,
    preparations: state.preparingRuns.size,
    csrfCleared: state.csrfToken === null,
    loginVisible: !elements.loginPanel.hidden,
    workspaceHidden: elements.workspace.hidden,
    connectionStatus: elements.statusText.textContent,
  };

  const result = document.createElement("output");
  result.id = "multisession-test-result";
  result.dataset.payload = JSON.stringify(outcomes);
  result.textContent = "complete";
  document.body.append(result);
}, 350);
"""


_RACE_FENCE_TEST = r"""
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
};

window.setTimeout(async () => {
  const outcomes = {};
  const agentA = "00000000-0000-4000-8000-000000000001";
  const agentB = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const sessionA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const sessionB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  const runA = "11111111111111111111111111111111";
  const permissionId = "22222222222222222222222222222222";
  const operationId = "33333333333333333333333333333333";
  const csrfA = "race-csrf-a";
  const agents = [
    {
      agent_id: agentA,
      display_name: "系统智能体",
      generation: 1,
      state: "active",
      created_at: "2026-07-22T00:00:00.000Z",
      updated_at: "2026-07-22T00:00:00.000Z",
    },
    {
      agent_id: agentB,
      display_name: "研究智能体",
      generation: 1,
      state: "active",
      created_at: "2026-07-22T00:00:00.000Z",
      updated_at: "2026-07-22T00:00:00.000Z",
    },
  ];
  const summary = (sessionId, title) => ({
    session_id: sessionId,
    title,
    revision: 0,
    created_at: "2026-07-22T00:00:00.000Z",
    updated_at: "2026-07-22T00:01:00.000Z",
    message_count: 2,
    state: "idle",
  });
  const sessionRecords = [summary(sessionA, "会话 A"), summary(sessionB, "会话 B")];
  const page = () => ({
    version: "turn-page-v2",
    limit: 32,
    before_cursor: null,
    returned_turns: 1,
    total_turns: 1,
    oldest_position: 1,
    newest_position: 1,
    has_older: false,
    has_newer: false,
    next_before_cursor: null,
  });
  const messages = (prefix, runId = null, status = "completed") => [
    {
      message_id: `${prefix}-user`,
      role: "user",
      content: `${prefix} user body`,
      created_at: "2026-07-22T00:00:00.000Z",
      turn_id: "44444444444444444444444444444444",
      run_id: runId,
      turn_position: 1,
      turn_status: status,
    },
    {
      message_id: `${prefix}-assistant`,
      role: "assistant",
      content: `${prefix} assistant body`,
      created_at: "2026-07-22T00:00:01.000Z",
      turn_id: "44444444444444444444444444444444",
      run_id: runId,
      turn_position: 1,
      turn_status: status,
    },
  ];
  const jsonResponse = (value, status = 200) => new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
  const preparationStatus = (stateValue, operation = operationId, stage = "admitting_run") => ({
    version: "run-preparation-v1",
    state: stateValue,
    operation_id: stateValue === "idle" ? null : operation,
    stage: stateValue === "idle" ? null : stage,
    elapsed_ms: stateValue === "idle" ? 0 : 100,
  });
  const runContext = (sessionId = sessionA, runId = runA) => ({
    agentId: state.agentId,
    agentEpoch: state.agentEpoch,
    runId,
    sessionId,
    lastSeq: 0,
    terminalSeen: false,
    terminalKind: null,
    terminalPayload: null,
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
  });
  const preparationContext = () => ({
    agentId: state.agentId,
    agentEpoch: state.agentEpoch,
    sessionId: sessionA,
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
  });

  let route = async (url, method) => jsonResponse({
    detail: `unhandled race route ${method} ${url.pathname}`,
  }, 404);
  const requests = [];
  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), window.location.origin);
    const method = String(init.method || "GET").toUpperCase();
    requests.push({ method, path: `${url.pathname}${url.search}`, body: init.body || null });
    return route(url, method, init);
  };

  const resetSurface = (selectedAgent = agentA, selectedSession = sessionA) => {
    state.sessionController?.abort();
    state.sessionController = null;
    state.sessionRequest += 1;
    state.sessionRollback = null;
    state.sessionLoading = false;
    state.mutationPending = false;
    state.settling = false;
    state.agentId = selectedAgent;
    state.agentEpoch += 1;
    state.agents = agents;
    state.sessions = sessionRecords;
    state.sessionId = selectedSession;
    state.activeRuns.clear();
    for (const preparation of state.preparingRuns.values()) stopPreparationMonitor(preparation);
    state.preparingRuns.clear();
    state.backgroundCompletions.clear();
    state.permissionRequest += 1;
    state.models = [{
      model_id: "qwen3.5:2b",
      provider: "ollama",
      model: "qwen3.5:2b",
      operational_context_tokens: 32768,
      max_output_tokens: 4096,
      supports_tools: true,
    }];
    state.defaultModelId = "qwen3.5:2b";
    state.selectedModelId = "qwen3.5:2b";
    elements.modelSelect.replaceChildren(new Option("qwen3.5:2b", "qwen3.5:2b"));
    elements.modelSelect.value = "qwen3.5:2b";
    state.nextTurnPreview = null;
    state.conversationPage = page();
    state.sessionDrafts.clear();
    setAgentIdentity(agents.find((agent) => agent.agent_id === selectedAgent));
    elements.activeSessionTitle.textContent = selectedSession === sessionA ? "会话 A" : "会话 B";
    elements.sessionId.textContent = selectedSession;
    renderMessages(messages(selectedSession === sessionA ? "A committed" : "B committed"));
    elements.messageInput.value = "";
    renderPermissions([]);
    renderAgentList();
    renderSessionList();
    setRunControls();
  };

  document.querySelector("#login-panel").hidden = true;
  document.querySelector("#workspace").hidden = false;
  state.authRequest += 1;
  state.csrfToken = csrfA;
  resetSurface();

  // A pending navigation must synchronously hide every A-owned surface. A
  // failed B load restores the one committed A snapshot and its draft.
  const bDetail = deferred();
  route = async (url, method) => {
    if (method === "GET" && url.pathname.endsWith(`/sessions/${sessionB}`)) {
      return bDetail.promise;
    }
    return jsonResponse({ permissions: [] });
  };
  const permissionRun = runContext();
  state.activeRuns.set(sessionA, permissionRun);
  elements.messageInput.value = "A draft survives failed navigation";
  showCommandResult({ command_id: "status", marker: "A command secret" });
  renderPermissions([{
    permission_id: permissionId,
    run_id: runA,
    capability_id: "file/read_text",
    preview: "A approval secret",
    status: "pending",
  }]);
  const staleApprove = elements.permissionList.querySelector(".permission-actions button:last-child");
  // The rollback does not need an active Run; retaining only the stale DOM
  // reference proves its captured scope cannot authorize a POST.
  state.activeRuns.clear();
  const bSelection = selectSession(sessionB);
  await wait(20);
  const pendingSurface = {
    selected: state.sessionId,
    messageText: elements.conversationMessages.textContent,
    commandHidden: elements.commandResult.hidden,
    permissionHidden: elements.permissionPanel.hidden,
    staleButtonConnected: staleApprove.isConnected,
  };
  staleApprove.click();
  await wait(10);
  const permissionPostsWhilePending = requests.filter((item) => (
    item.method === "POST" && item.path.includes("/permissions/")
  )).length;
  bDetail.resolve(jsonResponse({ detail: "B intentionally unavailable" }, 503));
  await bSelection;
  outcomes.sessionRollback = {
    pendingSurface,
    permissionPostsWhilePending,
    selected: state.sessionId,
    messageText: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    commandHidden: elements.commandResult.hidden,
    permissionHidden: elements.permissionPanel.hidden,
  };

  // Exact terminal interleave: A's refresh chooses the background path while
  // B is pending; B then fails and rolls the UI back to A before A's durable
  // detail arrives. The background response must notice that A is foreground
  // again and re-select its canonical transcript before completion unlocks it.
  resetSurface();
  const terminalInterleaveRun = runContext();
  terminalInterleaveRun.terminalSeen = true;
  terminalInterleaveRun.terminalKind = "run.completed";
  terminalInterleaveRun.terminalPayload = { reason: "end_turn" };
  terminalInterleaveRun.assistantBlocks.set("final", {
    content: "A canonical terminal assistant body",
    finished: true,
  });
  state.activeRuns.set(sessionA, terminalInterleaveRun);
  renderMessages(messages("A stale partial", runA, "running"));
  elements.messageInput.value = "A draft after canonical sync";
  setRunControls();
  const interleaveB = deferred();
  const firstARefresh = deferred();
  let aDetailReads = 0;
  const canonicalSession = { ...sessionRecords[0], state: "idle", revision: 0 };
  const canonicalDetail = {
    session: canonicalSession,
    messages: messages("A canonical terminal", runA, "completed"),
    page: page(),
  };
  route = async (url, method) => {
    if (method === "GET" && url.pathname.endsWith(`/sessions/${sessionB}`)) {
      return interleaveB.promise;
    }
    if (method === "GET" && url.pathname.endsWith(`/sessions/${sessionA}`)) {
      aDetailReads += 1;
      if (aDetailReads === 1) return firstARefresh.promise;
      return jsonResponse(canonicalDetail);
    }
    if (method === "GET" && url.pathname.endsWith("/subagents")) {
      return jsonResponse({ delegations: [] });
    }
    if (method === "GET" && url.pathname.endsWith("/context-preview")) {
      return jsonResponse({ detail: "preview omitted" }, 404);
    }
    return jsonResponse({ detail: `unexpected interleave route ${method} ${url.pathname}` }, 404);
  };
  const interleaveBSelection = selectSession(sessionB);
  await wait(20);
  const terminalRefresh = refreshRunConversation(terminalInterleaveRun);
  for (let attempt = 0; attempt < 10 && aDetailReads === 0; attempt += 1) await wait(10);
  interleaveB.resolve(jsonResponse({ detail: "B failed during terminal refresh" }, 503));
  await interleaveBSelection;
  setRunControls();
  const afterRollbackBeforeCanonical = {
    selected: state.sessionId,
    messageText: elements.conversationMessages.textContent,
    tracked: state.activeRuns.get(sessionA) === terminalInterleaveRun,
    sendDisabled: elements.runButton.disabled,
  };
  firstARefresh.resolve(jsonResponse(canonicalDetail));
  const refreshedDetail = await terminalRefresh;
  const beforeCompletion = {
    selected: state.sessionId,
    messageText: elements.conversationMessages.textContent,
    detailMessageText: refreshedDetail.messages.map((message) => message.content).join(" "),
    assistantCount: elements.conversationMessages.querySelectorAll(
      `.message-assistant[data-run-id="${runA}"]`,
    ).length,
    turnCount: conversationTurnGroups().length,
    tracked: state.activeRuns.get(sessionA) === terminalInterleaveRun,
    sendDisabled: elements.runButton.disabled,
  };
  completeRunContext(terminalInterleaveRun, false);
  outcomes.terminalRollbackInterleave = {
    aDetailReads,
    afterRollbackBeforeCanonical,
    beforeCompletion,
    afterCompletion: {
      selected: state.sessionId,
      messageText: elements.conversationMessages.textContent,
      assistantCount: elements.conversationMessages.querySelectorAll(
        `.message-assistant[data-run-id="${runA}"]`,
      ).length,
      turnCount: conversationTurnGroups().length,
      draft: elements.messageInput.value,
      tracked: state.activeRuns.has(sessionA),
      sendDisabled: elements.runButton.disabled,
      status: elements.composerStatus.textContent,
    },
  };

  // If the foreground canonical read itself fails after a terminal event, the
  // Run remains tracked and normal submission stays fenced. A later explicit
  // re-select renders canonical state first and only then completes the Run.
  resetSurface();
  const retryRunId = "66666666666666666666666666666666";
  const canonicalRetryRun = runContext(sessionA, retryRunId);
  state.activeRuns.set(sessionA, canonicalRetryRun);
  renderMessages(messages("A terminal partial", retryRunId, "running"));
  state.sessionDrafts.set(conversationStateKey(sessionA), {
    message: "draft retained while canonical retry is required",
    modelId: "qwen3.5:2b",
    compact: false,
  });
  elements.messageInput.value = "draft retained while canonical retry is required";
  let canonicalReadFails = true;
  route = async (url, method) => {
    if (method === "GET" && url.pathname.endsWith("/permissions")) {
      return jsonResponse({ permissions: [] });
    }
    if (method === "GET" && url.pathname.endsWith(`/sessions/${sessionA}`)) {
      return canonicalReadFails
        ? jsonResponse({ detail: "canonical detail temporarily unavailable" }, 503)
        : jsonResponse({
            session: { ...sessionRecords[0], state: "idle" },
            messages: messages("A canonical retry", retryRunId, "completed"),
            page: page(),
          });
    }
    if (method === "GET" && url.pathname.endsWith("/subagents")) {
      return jsonResponse({ delegations: [] });
    }
    if (method === "GET" && url.pathname.endsWith("/context-preview")) {
      return jsonResponse({ detail: "preview omitted" }, 404);
    }
    return jsonResponse({ detail: `unexpected canonical retry route ${method} ${url.pathname}` }, 404);
  };
  const originalStreamWithReconnect = streamWithReconnect;
  streamWithReconnect = async (context) => {
    context.terminalSeen = true;
    context.terminalKind = "run.completed";
    context.terminalPayload = { reason: "end_turn" };
  };
  try {
    await driveRun(canonicalRetryRun);
  } finally {
    streamWithReconnect = originalStreamWithReconnect;
  }
  setRunControls();
  const waitingForCanonical = {
    tracked: state.activeRuns.get(sessionA) === canonicalRetryRun,
    awaiting: canonicalRetryRun.awaitingCanonicalRefresh,
    messageText: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    sendDisabled: elements.runButton.disabled,
    status: elements.composerStatus.textContent,
  };
  canonicalReadFails = false;
  await selectSession(sessionA, { preserveTimeline: true });
  outcomes.canonicalRetryFence = {
    waitingForCanonical,
    afterRetry: {
      tracked: state.activeRuns.has(sessionA),
      awaiting: canonicalRetryRun.awaitingCanonicalRefresh,
      messageText: elements.conversationMessages.textContent,
      draft: elements.messageInput.value,
      sendDisabled: elements.runButton.disabled,
      status: elements.composerStatus.textContent,
    },
  };

  // One early cancellation click observes idle, keeps polling, then follows
  // the same operation across preparation -> active Run handoff. Its late ACK
  // must not replace the newly selected B conversation surface.
  resetSurface();
  const handoffPreparation = preparationContext();
  const handoffStatus = deferred();
  let handoffReads = 0;
  let handoffCancelBody = null;
  let handoffCancelCalls = 0;
  route = async (url, method, init) => {
    if (method === "GET" && url.pathname.endsWith("/preparation")) {
      handoffReads += 1;
      if (handoffReads === 1) return jsonResponse(preparationStatus("idle"));
      if (handoffReads === 2) return handoffStatus.promise;
      return jsonResponse(preparationStatus("preparing"));
    }
    if (method === "POST" && url.pathname.endsWith("/preparation/cancel")) {
      handoffCancelCalls += 1;
      handoffCancelBody = JSON.parse(init.body || "{}");
      return jsonResponse({
        version: "run-preparation-cancel-v1",
        state: "cancellation_requested",
        target: "run",
      }, 202);
    }
    return jsonResponse({ sessions: sessionRecords });
  };
  state.preparingRuns.set(sessionA, handoffPreparation);
  setRunControls();
  elements.cancelButton.click();
  for (let attempt = 0; attempt < 10 && handoffReads < 2; attempt += 1) await wait(20);
  const handedOffRun = runContext();
  state.preparingRuns.delete(sessionA);
  state.activeRuns.set(sessionA, handedOffRun);
  state.sessionId = sessionB;
  elements.activeSessionTitle.textContent = "会话 B";
  elements.sessionId.textContent = sessionB;
  renderMessages(messages("B foreground"));
  elements.messageInput.value = "B foreground draft";
  setStatus("B foreground remains stable");
  setRunControls();
  handoffStatus.resolve(jsonResponse(preparationStatus("preparing")));
  await wait(100);
  outcomes.preparationHandoff = {
    reads: handoffReads,
    cancelCalls: handoffCancelCalls,
    cancelBody: handoffCancelBody,
    requestAborted: handoffPreparation.controller.signal.aborted,
    handedOffRunCancelPending: handedOffRun.cancelPending,
    selected: state.sessionId,
    messageText: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    status: elements.composerStatus.textContent,
  };

  // Fetch resolves at headers. The guarded JSON reader must still reject with
  // AbortError if logout/re-login changes the authentication generation while
  // its body remains delayed.
  resetSurface();
  const bodyStream = deferred();
  const encoder = new TextEncoder();
  let bodyController = null;
  route = async (url, method) => {
    if (method === "GET" && url.pathname === "/api/auth/status") {
      bodyStream.resolve(true);
      return new Response(new ReadableStream({
        start(controller) {
          bodyController = controller;
        },
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return jsonResponse({ detail: "unused" }, 404);
  };
  const guardedAuthResponse = await api("/api/auth/status");
  await bodyStream.promise;
  const oldBody = guardedAuthResponse.json().then(
    () => ({ resolved: true, errorName: null }),
    (error) => ({ resolved: false, errorName: error?.name || null }),
  );
  setUnauthenticated("race logout complete");
  state.authRequest += 1;
  setAuthenticated({ csrf_token: "race-csrf-b", agent_id: agentB });
  state.agentEpoch += 1;
  state.agents = agents;
  setAgentIdentity(agents[1]);
  state.sessions = sessionRecords;
  state.sessionId = sessionB;
  elements.activeSessionTitle.textContent = "新认证会话 B";
  elements.sessionId.textContent = sessionB;
  renderMessages(messages("new auth B"));
  elements.messageInput.value = "new auth draft";
  setStatus("new auth surface stable");
  bodyController.enqueue(encoder.encode(JSON.stringify({
    authenticated: true,
    csrf_token: "stale-csrf",
    agent_id: agentA,
  })));
  bodyController.close();
  outcomes.authBodyFence = {
    body: await oldBody,
    agentId: state.agentId,
    agentTitle: elements.activeAgentTitle.textContent,
    sessionId: state.sessionId,
    sessionTitle: elements.activeSessionTitle.textContent,
    messageText: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    status: elements.composerStatus.textContent,
    csrf: state.csrfToken,
  };

  // Terminal convergence clears an outstanding approval card. A separate
  // active-Run cancel ACK arriving after terminal + navigation cannot overwrite
  // the new foreground surface.
  resetSurface();
  const terminalRun = runContext();
  state.activeRuns.set(sessionA, terminalRun);
  renderPermissions([{
    permission_id: permissionId,
    run_id: runA,
    capability_id: "file/edit",
    preview: "pending edit",
    status: "pending",
  }]);
  const permissionBeforeTerminal = {
    hidden: elements.permissionPanel.hidden,
    cards: elements.permissionList.children.length,
  };
  terminalRun.terminalSeen = true;
  terminalRun.terminalKind = "run.completed";
  terminalRun.terminalPayload = { reason: "end_turn" };
  completeRunContext(terminalRun, false);
  const permissionAfterTerminal = {
    hidden: elements.permissionPanel.hidden,
    cards: elements.permissionList.children.length,
  };

  const cancelAck = deferred();
  const lateRunId = "55555555555555555555555555555555";
  const lateRun = runContext(sessionA, lateRunId);
  state.activeRuns.set(sessionA, lateRun);
  state.sessionId = sessionA;
  renderMessages(messages("A cancelling", lateRunId, "running"));
  route = async (url, method) => {
    if (method === "POST" && url.pathname.endsWith(`/runs/${lateRunId}/cancel`)) {
      return cancelAck.promise;
    }
    return jsonResponse({ detail: "unused" }, 404);
  };
  setRunControls();
  elements.cancelButton.click();
  await wait(20);
  const cancelPendingBeforeTerminal = lateRun.cancelPending;
  lateRun.terminalSeen = true;
  lateRun.terminalKind = "run.completed";
  lateRun.terminalPayload = { reason: "end_turn" };
  completeRunContext(lateRun, false);
  state.sessionId = sessionB;
  elements.activeSessionTitle.textContent = "会话 B terminal foreground";
  elements.sessionId.textContent = sessionB;
  renderMessages(messages("B after terminal"));
  elements.messageInput.value = "B after terminal draft";
  setStatus("B terminal status remains stable");
  cancelAck.resolve(new Response(null, { status: 202 }));
  await wait(50);
  outcomes.permissionAndLateCancel = {
    permissionBeforeTerminal,
    permissionAfterTerminal,
    cancelPendingBeforeTerminal,
    selected: state.sessionId,
    messageText: elements.conversationMessages.textContent,
    draft: elements.messageInput.value,
    status: elements.composerStatus.textContent,
    activeRuns: state.activeRuns.size,
  };

  // Agent switching is fenced only while a preparation cancellation has not
  // been acknowledged. The same control is restored after a valid ACK even as
  // the server continues converging to idle.
  resetSurface();
  const gatePreparation = preparationContext();
  const gateAck = deferred();
  let gateCancelCalls = 0;
  route = async (url, method) => {
    if (method === "GET" && url.pathname.endsWith("/preparation")) {
      return jsonResponse(preparationStatus("cancelling"));
    }
    if (method === "POST" && url.pathname.endsWith("/preparation/cancel")) {
      gateCancelCalls += 1;
      return gateAck.promise;
    }
    return jsonResponse({ detail: "unused" }, 404);
  };
  state.preparingRuns.set(sessionA, gatePreparation);
  renderAgentList();
  const agentSwitch = Array.from(elements.agentList.querySelectorAll(".agent-select"))
    .find((button) => button.textContent === "切换");
  setRunControls();
  elements.cancelButton.click();
  for (let attempt = 0; attempt < 10 && gateCancelCalls === 0; attempt += 1) await wait(10);
  const disabledBeforeAck = agentSwitch.disabled;
  gateAck.resolve(jsonResponse({
    version: "run-preparation-cancel-v1",
    state: "cancellation_requested",
    target: "preparation",
  }, 202));
  await wait(50);
  outcomes.agentSwitchCancelFence = {
    cancelCalls: gateCancelCalls,
    cancelPending: gatePreparation.cancelPending,
    cancelConfirmed: gatePreparation.cancelConfirmed,
    disabledBeforeAck,
    disabledAfterAck: agentSwitch.disabled,
  };
  stopPreparationMonitor(gatePreparation);
  state.preparingRuns.delete(sessionA);
  setRunControls();

  const result = document.createElement("output");
  result.id = "race-fence-test-result";
  result.dataset.payload = JSON.stringify(outcomes);
  result.textContent = "complete";
  document.body.append(result);
}, 350);
"""


_SSE_RECONNECT_TEST = r"""
window.setTimeout(async () => {
  const agentId = "00000000-0000-4000-8000-000000000001";
  const sessionId = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
  const turnId = "ffffffffffffffffffffffffffffffff";
  const runId = "12121212121212121212121212121212";
  const encoder = new TextEncoder();
  const requestHeaders = [];
  const appliedSeq = [];
  let terminalApplications = 0;
  let secondStreamCancelled = 0;
  let timerObservedBeforeResume = false;
  let attemptObservedBeforeResume = false;

  const envelope = (seq, kind, payload) => ({
    schema_version: "2.2-prototype",
    event_id: seq.toString(16).padStart(32, "0"),
    agent_id: agentId,
    conversation_id: sessionId,
    turn_id: turnId,
    run_id: runId,
    parent_run_id: null,
    seq,
    occurred_at: `2026-07-22T00:00:0${seq}.000Z`,
    kind,
    durability: "durable",
    payload,
  });
  const frame = (value) => encoder.encode(
    `id: ${value.seq}\nevent: ${value.kind}\ndata: ${JSON.stringify(value)}\n\n`,
  );
  const firstEvent = envelope(1, "model.transport.attempt", {
    attempt: 1,
    max_attempts: 2,
    phase: "attempt_started",
    outcome: null,
    elapsed_ms: 0,
    first_frame_ms: null,
  });
  const terminalEvent = envelope(2, "run.completed", {
    reason: "end_turn",
    model_iterations: 1,
    usage: {
      input_tokens: 10,
      output_tokens: 4,
      last_input_tokens: 10,
      complete: true,
    },
  });

  document.querySelector("#login-panel").hidden = true;
  document.querySelector("#workspace").hidden = false;
  state.csrfToken = "test-csrf";
  state.agentId = agentId;
  state.sessionId = sessionId;
  state.timelineRuns = [{
    runId,
    turnId,
    turnNumber: 1,
    status: "running",
    kind: "conversation",
  }];
  state.selectedTimelineRunId = runId;
  state.timelineEntries = [];
  state.timelineEntriesByRun.clear();
  state.timelineEntriesByRun.set(runId, state.timelineEntries);
  const runContext = {
    agentId,
    agentEpoch: state.agentEpoch,
    runId,
    sessionId,
    lastSeq: 0,
    terminalSeen: false,
    terminalKind: null,
    terminalPayload: null,
    cancelPending: false,
    controller: null,
    driverPromise: null,
    transportTimer: null,
    transportAttempt: null,
    assistantBlocks: new Map(),
  };
  state.activeRuns.clear();
  state.activeRuns.set(sessionId, runContext);

  const originalAddTimelineEvent = addTimelineEvent;
  const originalTerminalUpdate = updateTimelineRunTerminalStatus;
  addTimelineEvent = (value) => {
    appliedSeq.push(value.seq);
    return originalAddTimelineEvent(value);
  };
  updateTimelineRunTerminalStatus = (...args) => {
    terminalApplications += 1;
    return originalTerminalUpdate(...args);
  };

  let connection = 0;
  window.fetch = async (_input, init = {}) => {
    connection += 1;
    const headers = init.headers || {};
    requestHeaders.push({
      accept: headers.Accept || null,
      lastEventId: headers["Last-Event-ID"] || null,
    });
    if (connection === 1) {
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(frame(firstEvent));
          controller.close();
        },
      }), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    timerObservedBeforeResume = runContext.transportTimer !== null;
    attemptObservedBeforeResume = runContext.transportAttempt?.attempt === 1;
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(frame(terminalEvent));
      },
      cancel() {
        secondStreamCancelled += 1;
      },
    }), {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };

  let error = null;
  try {
    await streamWithReconnect(runContext);
  } catch (caught) {
    error = caught?.message || String(caught);
  } finally {
    addTimelineEvent = originalAddTimelineEvent;
    updateTimelineRunTerminalStatus = originalTerminalUpdate;
  }

  const entries = state.timelineEntriesByRun.get(runId) || [];
  const result = document.createElement("output");
  result.id = "sse-reconnect-test-result";
  result.dataset.payload = JSON.stringify({
    error,
    tracked: runIsTracked(runContext),
    scope: {
      stateAgent: state.agentId,
      stateEpoch: state.agentEpoch,
      runAgent: runContext.agentId,
      runEpoch: runContext.agentEpoch,
    },
    connections: connection,
    requestHeaders,
    appliedSeq,
    storedSeq: entries.map((entry) => entry.envelope.seq),
    terminalApplications,
    terminalEntryCount: entries.filter((entry) => (
      entry.envelope.kind === "run.completed"
    )).length,
    lastSeq: runContext.lastSeq,
    terminalSeen: runContext.terminalSeen,
    terminalKind: runContext.terminalKind,
    controllerReleased: runContext.controller === null,
    timerCleared: runContext.transportTimer === null,
    attemptCleared: runContext.transportAttempt === null,
    timerObservedBeforeResume,
    attemptObservedBeforeResume,
    secondStreamCancelled,
  });
  result.textContent = "complete";
  document.body.append(result);
}, 300);
"""


class _StaticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path in {
            "/", "/responsive", "/multisession", "/race-fences", "/sse-reconnect",
        }:
            source = (STATIC / "index.html").read_text(encoding="utf-8")
            test_script = (
                "/responsive-test.js" if self.path == "/responsive" else
                "/multisession-test.js" if self.path == "/multisession" else
                "/race-fence-test.js" if self.path == "/race-fences" else
                "/sse-reconnect-test.js" if self.path == "/sse-reconnect" else
                "/browser-test.js"
            )
            source = source.replace(
                '<script src="/assets/app.js" defer></script>',
                '<script src="/assets/app.js" defer></script>'
                f'<script src="{test_script}" defer></script>',
            )
            self._send(200, "text/html; charset=utf-8", source.encode())
            return
        if self.path == "/assets/app.js":
            self._send(
                200, "text/javascript; charset=utf-8",
                (STATIC / "app.js").read_bytes(),
            )
            return
        if self.path == "/assets/styles.css":
            self._send(
                200, "text/css; charset=utf-8",
                (STATIC / "styles.css").read_bytes(),
            )
            return
        if self.path == "/browser-test.js":
            self._send(200, "text/javascript; charset=utf-8", _BROWSER_TEST.encode())
            return
        if self.path == "/responsive-test.js":
            self._send(
                200,
                "text/javascript; charset=utf-8",
                _RESPONSIVE_TEST.encode(),
            )
            return
        if self.path == "/multisession-test.js":
            self._send(
                200,
                "text/javascript; charset=utf-8",
                _MULTISESSION_TEST.encode(),
            )
            return
        if self.path == "/race-fence-test.js":
            self._send(
                200,
                "text/javascript; charset=utf-8",
                _RACE_FENCE_TEST.encode(),
            )
            return
        if self.path == "/sse-reconnect-test.js":
            self._send(
                200,
                "text/javascript; charset=utf-8",
                _SSE_RECONNECT_TEST.encode(),
            )
            return
        self._send(404, "application/json", b'{"error":"test fixture"}')

    def _send(self, status: int, media_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.mark.skipif(CHROMIUM is None, reason="qualified Chromium is unavailable")
def test_composer_boundaries_ime_and_latest_scroll_in_a_real_browser() -> None:
    """Exercise real DOM events and TextEncoder in the supported headless browser."""

    results_root = ROOT / ".runtime" / "test-results"
    results_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix="chromium-context-", dir=results_root
        ) as profile:
            environment = os.environ.copy()
            environment.update({
                "HOME": str(ROOT / ".runtime" / "home"),
                "TMPDIR": str(ROOT / ".runtime" / "tmp"),
            })
            result = subprocess.run(
                [
                    CHROMIUM or "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--force-prefers-reduced-motion",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=1500",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=environment,
            )
        assert result.returncode == 0, result.stderr[-2000:]
        match = re.search(
            r'id="browser-test-result"[^>]*data-payload="([^"]+)"',
            result.stdout,
        )
        assert match is not None, result.stdout[-2000:]
        outcome = json.loads(html.unescape(match.group(1)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert outcome["ascii8192"] == {
        "count": 8192, "over": "false", "retained": True,
    }
    assert outcome["ascii8193"] == {
        "count": 8192, "over": "false", "retained": False,
    }
    assert outcome["cjk8192"]["count"] == 8192
    assert outcome["cjk8192"]["over"] == "false"
    assert outcome["emoji8192"]["count"] == 8192
    assert outcome["emoji8192"]["over"] == "false"
    assert outcome["escaped"]["count"] == outcome["escaped"]["expected"]
    assert outcome["imeSubmits"] == 0
    assert outcome["enterSubmits"] == 1
    assert outcome["enterDraftRetained"] is True
    assert outcome["forcedLatest"] is True, outcome["scrollMetrics"]
    assert outcome["copyFallback"] == {
        "residualNodes": 0,
        "bodyRetained": False,
    }


@pytest.mark.skipif(CHROMIUM is None, reason="qualified Chromium is unavailable")
def test_sse_disconnect_resumes_from_last_event_without_duplicate_terminal() -> None:
    """Resume a real browser stream after disconnect without replaying applied events."""

    results_root = ROOT / ".runtime" / "test-results"
    results_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix="chromium-sse-reconnect-", dir=results_root
        ) as profile:
            environment = os.environ.copy()
            environment.update({
                "HOME": str(ROOT / ".runtime" / "home"),
                "TMPDIR": str(ROOT / ".runtime" / "tmp"),
            })
            result = subprocess.run(
                [
                    CHROMIUM or "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=2200",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/sse-reconnect",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=environment,
            )
        assert result.returncode == 0, result.stderr[-2_000:]
        match = re.search(
            r'id="sse-reconnect-test-result"[^>]*data-payload="([^"]+)"',
            result.stdout,
        )
        assert match is not None, result.stdout[-4_000:]
        outcome = json.loads(html.unescape(match.group(1)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert outcome["error"] is None, outcome
    assert outcome["connections"] == 2
    assert outcome["requestHeaders"] == [
        {"accept": "text/event-stream", "lastEventId": None},
        {"accept": "text/event-stream", "lastEventId": "1"},
    ]
    assert outcome["appliedSeq"] == [1, 2]
    assert outcome["storedSeq"] == [1, 2]
    assert outcome["terminalApplications"] == 1
    assert outcome["terminalEntryCount"] == 1
    assert outcome["lastSeq"] == 2
    assert outcome["terminalSeen"] is True
    assert outcome["terminalKind"] == "run.completed"
    assert outcome["timerObservedBeforeResume"] is True
    assert outcome["attemptObservedBeforeResume"] is True
    assert outcome["controllerReleased"] is True
    assert outcome["timerCleared"] is True
    assert outcome["attemptCleared"] is True
    assert outcome["secondStreamCancelled"] == 1


@pytest.mark.skipif(CHROMIUM is None, reason="qualified Chromium is unavailable")
def test_identity_cancellation_and_auth_races_are_fenced_in_a_real_browser() -> None:
    """Keep delayed responses inside their original Agent/session/auth scope."""

    results_root = ROOT / ".runtime" / "test-results"
    results_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix="chromium-race-fences-", dir=results_root
        ) as profile:
            environment = os.environ.copy()
            environment.update({
                "HOME": str(ROOT / ".runtime" / "home"),
                "TMPDIR": str(ROOT / ".runtime" / "tmp"),
            })
            result = subprocess.run(
                [
                    CHROMIUM or "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=4200",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/race-fences",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=25,
                env=environment,
            )
        assert result.returncode == 0, result.stderr[-2_000:]
        match = re.search(
            r'id="race-fence-test-result"[^>]*data-payload="([^"]+)"',
            result.stdout,
        )
        assert match is not None, result.stdout[-5_000:]
        outcome = json.loads(html.unescape(match.group(1)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    rollback = outcome["sessionRollback"]
    assert rollback["pendingSurface"] == {
        "selected": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "messageText": "",
        "commandHidden": True,
        "permissionHidden": True,
        "staleButtonConnected": False,
    }
    assert rollback["permissionPostsWhilePending"] == 0
    assert rollback["selected"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert "A committed" in rollback["messageText"]
    assert rollback["draft"] == "A draft survives failed navigation"
    assert rollback["commandHidden"] is True
    assert rollback["permissionHidden"] is True

    interleave = outcome["terminalRollbackInterleave"]
    assert interleave["aDetailReads"] == 2
    assert interleave["afterRollbackBeforeCanonical"]["selected"] == (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert "A stale partial" in interleave["afterRollbackBeforeCanonical"]["messageText"]
    assert "A canonical terminal" not in (
        interleave["afterRollbackBeforeCanonical"]["messageText"]
    )
    assert interleave["afterRollbackBeforeCanonical"]["tracked"] is True
    assert interleave["afterRollbackBeforeCanonical"]["sendDisabled"] is True
    assert "A canonical terminal" in interleave["beforeCompletion"]["messageText"]
    assert "A stale partial" not in interleave["beforeCompletion"]["messageText"]
    assert "A canonical terminal" in interleave["beforeCompletion"]["detailMessageText"]
    assert interleave["beforeCompletion"]["assistantCount"] == 1
    assert interleave["beforeCompletion"]["turnCount"] == 1
    assert interleave["beforeCompletion"]["tracked"] is True
    assert interleave["beforeCompletion"]["sendDisabled"] is True
    completed = interleave["afterCompletion"]
    assert completed["selected"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert "A canonical terminal" in completed["messageText"]
    assert "A stale partial" not in completed["messageText"]
    assert completed["assistantCount"] == 1
    assert completed["turnCount"] == 1
    assert completed["draft"] == "A draft after canonical sync"
    assert completed["tracked"] is False
    assert completed["sendDisabled"] is False
    assert completed["status"] == "本轮运行已完成"

    canonical_retry = outcome["canonicalRetryFence"]
    waiting = canonical_retry["waitingForCanonical"]
    assert waiting["tracked"] is True
    assert waiting["awaiting"] is True
    assert "A terminal partial" in waiting["messageText"]
    assert waiting["draft"] == "draft retained while canonical retry is required"
    assert waiting["sendDisabled"] is True
    assert waiting["status"] == (
        "本轮已结束，但完整会话终态暂时无法读取；请重新选择该会话后再继续发送"
    )
    after_retry = canonical_retry["afterRetry"]
    assert after_retry["tracked"] is False
    assert after_retry["awaiting"] is False
    assert "A canonical retry" in after_retry["messageText"]
    assert "A terminal partial" not in after_retry["messageText"]
    assert after_retry["draft"] == "draft retained while canonical retry is required"
    assert after_retry["sendDisabled"] is False
    assert after_retry["status"] == "本轮运行已完成"

    handoff = outcome["preparationHandoff"]
    assert handoff["reads"] >= 2
    assert handoff["cancelCalls"] == 1
    assert handoff["cancelBody"] == {
        "operation_id": "33333333333333333333333333333333",
    }
    assert handoff["requestAborted"] is True
    assert handoff["handedOffRunCancelPending"] is True
    assert handoff["selected"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert "B foreground" in handoff["messageText"]
    assert handoff["draft"] == "B foreground draft"
    assert handoff["status"] == "B foreground remains stable"

    auth = outcome["authBodyFence"]
    assert auth["body"] == {"resolved": False, "errorName": "AbortError"}
    assert auth["agentId"] == "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    assert auth["agentTitle"] == "研究智能体"
    assert auth["sessionId"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert auth["sessionTitle"] == "新认证会话 B"
    assert "new auth B" in auth["messageText"]
    assert auth["draft"] == "new auth draft"
    assert auth["status"] == "new auth surface stable"
    assert auth["csrf"] == "race-csrf-b"

    terminal = outcome["permissionAndLateCancel"]
    assert terminal["permissionBeforeTerminal"] == {"hidden": False, "cards": 1}
    assert terminal["permissionAfterTerminal"] == {"hidden": True, "cards": 0}
    assert terminal["cancelPendingBeforeTerminal"] is True
    assert terminal["selected"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert "B after terminal" in terminal["messageText"]
    assert terminal["draft"] == "B after terminal draft"
    assert terminal["status"] == "B terminal status remains stable"
    assert terminal["activeRuns"] == 0

    switch = outcome["agentSwitchCancelFence"]
    assert switch["cancelCalls"] == 1
    assert switch["cancelPending"] is True
    assert switch["cancelConfirmed"] is True
    assert switch["disabledBeforeAck"] is True
    assert switch["disabledAfterAck"] is False


@pytest.mark.skipif(CHROMIUM is None, reason="qualified Chromium is unavailable")
@pytest.mark.parametrize(
    ("viewport_width", "viewport_height"),
    ((360, 640), (390, 844), (768, 1024), (1440, 900)),
)
def test_workspace_is_responsive_and_composer_remains_visible(
    viewport_width: int,
    viewport_height: int,
) -> None:
    """Keep the conversation shell usable across qualified narrow and wide layouts."""

    results_root = ROOT / ".runtime" / "test-results"
    results_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"chromium-responsive-{viewport_width}-",
            dir=results_root,
        ) as profile:
            environment = os.environ.copy()
            environment.update({
                "HOME": str(ROOT / ".runtime" / "home"),
                "TMPDIR": str(ROOT / ".runtime" / "tmp"),
            })
            result = subprocess.run(
                [
                    CHROMIUM or "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    f"--window-size={viewport_width},{viewport_height}",
                    "--force-prefers-reduced-motion",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=1600",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/responsive",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=environment,
            )
        assert result.returncode == 0, result.stderr[-2000:]
        match = re.search(
            r'id="responsive-test-result"[^>]*data-payload="([^"]+)"',
            result.stdout,
        )
        assert match is not None, result.stdout[-2000:]
        outcome = json.loads(html.unescape(match.group(1)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    viewport = outcome["viewport"]
    document = outcome["document"]
    assert document["scrollWidth"] <= document["clientWidth"], outcome
    assert document["bodyScrollWidth"] <= document["bodyClientWidth"], outcome
    for name in ("masthead", "workspace", "primary", "composer", "toolbar", "input"):
        box = outcome[name]
        assert box["left"] >= -0.5, (name, outcome)
        assert box["right"] <= viewport["visualWidth"] + 0.5, (name, outcome)
    assert outcome["workspace"]["top"] >= outcome["masthead"]["bottom"] - 0.5
    assert outcome["composer"]["bottom"] <= viewport["visualHeight"] + 0.5, outcome
    assert outcome["composer"]["top"] >= outcome["masthead"]["bottom"] - 0.5
    assert outcome["contextFontPx"] >= 12
    assert 14 <= outcome["contextValueFontPx"] <= 16
    assert outcome["messageFontPx"] >= 16
    assert outcome["minimumCriticalAidFontPx"] >= 12
    assert outcome["connectionWording"] == {
        "text": "控制面已连接", "visible": True,
    }
    assert outcome["meterLabel"] == "上下文窗口占用"
    assert outcome["meterDescription"] == "context-usage-value context-usage-detail"
    assert outcome["availableContext"] == {
        "label": "下一轮上下文 · qwen3.5:2b · 纯对话基线",
        "value": "占用约 24,576 / 32,768 · 剩余约 25%",
        "detail": (
            "下一条消息安全可写约 3,840 tokens · 占用误差 ± 256 tokens · "
            "自动整理前还可增加约 1,792 tokens · 单条消息最多 8,192 个 UTF-8 字节 · "
            "历史完整保留 · 纯对话实测基线；若下一条启用工具，将在提交前重算"
        ),
        "meterMax": "32768",
        "meterNow": "24576",
        "meterText": "占用约 24,576 / 32,768 · 剩余约 25%",
    }
    assert outcome["conservativeContextValue"] == (
        "占用约 1,024 / 32,768 · 剩余约 97%"
    )
    assert outcome["unavailableContext"] == {
        "value": "占用数与剩余比例暂不可用",
        "detail": (
            "纯对话实测不能安全推算工具场景；完成一次需要工具的对话后再显示。 "
            "模型窗口 32,768 tokens。"
        ),
    }
    assert outcome["previewValidation"] == {
        "acceptsExact": True,
        "rejectsExtraNestedField": True,
    }
    assert outcome["liveRegions"] == {
        "connection": {"role": "status", "live": "polite"},
        "interaction": {"role": "status", "live": "polite"},
        "preparation": {"role": "status", "live": "polite"},
        "running": {"role": "status", "live": "polite"},
        "failure": {"role": "alert", "live": "assertive"},
        "conversationLive": "off",
    }
    assert outcome["reducedMotion"]["requested"] is True
    assert outcome["reducedMotion"]["transitionMs"] <= 0.02
    assert outcome["reducedMotion"]["animationMs"] <= 0.02
    assert outcome["keyboardLayout"]["staysVisible"] is True, outcome["keyboardLayout"]
    assert outcome["focusRestoration"]["runtimeOpen"] is True, outcome["focusRestoration"]
    assert outcome["focusRestoration"]["runtimeBackdrop"] is True
    if viewport_width <= 768:
        assert outcome["undersizedTargets"] == [], outcome["undersizedTargets"]
        assert outcome["focusRestoration"] == {
            "navigationOpen": True,
            "navigationClose": True,
            "runtimeOpen": True,
            "runtimeBackdrop": True,
            "navigationEscape": True,
        }


@pytest.mark.skipif(CHROMIUM is None, reason="qualified Chromium is unavailable")
def test_multisession_pagination_menu_and_pagehide_in_a_real_browser() -> None:
    """Exercise cross-session isolation and touch workflows in real Chromium."""

    results_root = ROOT / ".runtime" / "test-results"
    results_root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(
            prefix="chromium-multisession-", dir=results_root
        ) as profile:
            environment = os.environ.copy()
            environment.update({
                "HOME": str(ROOT / ".runtime" / "home"),
                "TMPDIR": str(ROOT / ".runtime" / "tmp"),
            })
            result = subprocess.run(
                [
                    CHROMIUM or "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--touch-events=enabled",
                    "--window-size=390,844",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=5200",
                    "--dump-dom",
                    f"http://127.0.0.1:{server.server_port}/multisession",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=25,
                env=environment,
            )
        assert result.returncode == 0, result.stderr[-2_000:]
        match = re.search(
            r'id="multisession-test-result"[^>]*data-payload="([^"]+)"',
            result.stdout,
        )
        assert match is not None, result.stdout[-4_000:]
        outcome = json.loads(html.unescape(match.group(1)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    background = outcome["backgroundRun"]
    assert "B visible" in background["beforeBackground"]["text"]
    assert background["afterBackground"]["text"] == background["beforeBackground"]["text"]
    assert background["afterBackground"]["draft"] == "B independent draft"
    assert background["afterBackground"]["status"] == "B status must remain stable"
    assert background["afterBackground"]["aBuffered"] == "A final background stream"
    assert background["afterBackground"]["aTimelineEvents"] == 4
    assert background["markerAfterCompletion"] == "后台已完成", background
    assert "A persisted" in background["returnedText"]
    assert "A final background stream" not in background["returnedText"]
    assert background["returnedDraft"] == "A draft survives switching"
    assert background["markerAfterReturn"] == "空闲"
    assert background["activeAfterCompletion"] is False
    assert background["completionMarkerRetained"] is False

    repetition = outcome["repetitionCompletion"]
    assert repetition["turnCards"] == 1
    assert repetition["assistantMessages"] == 1
    assert repetition["markerCount"] == 1
    assert repetition["completedLabel"] == "已完成"
    assert repetition["usageComplete"] is False
    assert "Provider 用量不完整" in repetition["usageText"]
    assert repetition["eventSummary"] == (
        "检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，Provider 用量不完整"
    )
    assert repetition["terminalKind"] == "run.completed"
    assert repetition["terminalReason"] == "repetition_truncated"
    assert "重复尾部已截断" in repetition["status"]

    pagination = outcome["pagination"]
    assert pagination["turnPositions"] == [1, 2, 3, 4]
    assert pagination["messageCount"] == 8
    assert pagination["uniqueMessageCount"] == 8
    assert pagination["duplicateCurrentUserCount"] == 1
    assert abs(pagination["anchorDelta"]) <= 1.5, pagination
    assert abs(pagination["scrollDelta"] - pagination["heightDelta"]) <= 1.5
    assert pagination["hasOlder"] is False
    assert "已加载更早消息" in pagination["status"]

    menu = outcome["sessionMenu"]
    assert menu["summaryVisible"] is True
    assert menu["summarySize"]["width"] >= 43.5
    assert menu["summarySize"]["height"] >= 43.5
    assert menu["keyboardSummary"] is True
    assert menu["keyboardRename"] is True
    assert menu["keyboardBranch"] is True
    assert menu["actionsVisible"] is True
    assert all(size["width"] >= 43.5 and size["height"] >= 43.5 for size in menu["actionSizes"])
    assert menu["allActionsInViewport"] is True, menu
    assert menu["popoverInViewport"] is True, menu
    assert menu["listDidNotScroll"] is True, menu
    assert menu["summaryLabel"] == "会话操作：会话 A 已重命名"
    assert menu["renamedTitle"] == "会话 A 已重命名"
    assert menu["renameRequest"] is True
    assert menu["branchRequest"] is True
    assert menu["selectedBranch"] == "cccccccccccccccccccccccccccccccc"

    preparation = outcome["preparationProgress"]
    assert preparation["reads"] >= 2
    assert "历史摘要" in preparation["summaryStatus"]
    assert "上下文已就绪" in preparation["statusBeforeCancel"]
    assert preparation["cancelCalls"] == 1
    assert preparation["cancelConfirmed"] is True
    assert preparation["requestAborted"] is True
    assert "取消请求已确认" in preparation["liveStatus"]

    pagehide = outcome["pagehide"]
    assert pagehide["runAborted"] == [True, True]
    assert pagehide["preparationAborted"] == [True, True]
    assert pagehide["timersCleared"] == [True, True]
    assert pagehide["attemptsCleared"] == [True, True]
    # pagehide closes browser transports and drops ephemeral browser tracking;
    # a persisted pageshow reconstructs truth from durable service state.
    assert pagehide["trackedRuns"] == 0
    assert pagehide["trackedPreparations"] == 0

    agent_switch = outcome["agentSwitch"]
    assert agent_switch["disabledDuringBackgroundRun"] is False
    assert agent_switch["enabledAfterBackgroundRun"] is True
    assert agent_switch["oldRunAborted"] is True
    assert agent_switch["selectedAgentId"] == "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    assert agent_switch["selectedAgentTitle"] == "研究智能体"
    assert agent_switch["status"] == "已切换到 研究智能体"
    assert agent_switch["activeRuns"] == 0
    assert agent_switch["preparations"] == 0

    logout = outcome["logout"]
    assert logout == {
        "runAborted": True,
        "preparationAborted": True,
        "activeRuns": 0,
        "preparations": 0,
        "csrfCleared": True,
        "loginVisible": True,
        "workspaceHidden": True,
        "connectionStatus": "会话已退出",
    }

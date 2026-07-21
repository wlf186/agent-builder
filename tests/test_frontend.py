"""Static contract checks for the dependency-free browser client."""

from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPOSITORY_ROOT / "src" / "agent_builder_v2" / "static"
INDEX = STATIC_ROOT.joinpath("index.html").read_text(encoding="utf-8")
SCRIPT = STATIC_ROOT.joinpath("app.js").read_text(encoding="utf-8")
STYLES = STATIC_ROOT.joinpath("styles.css").read_text(encoding="utf-8")


def test_session_ui_exposes_create_restore_delete_and_multiturn_surfaces() -> None:
    required_ids = {
        "new-session-button",
        "session-list",
        "active-session-title",
        "session-id",
        "conversation-messages",
        "message-input",
        "run-form",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))

    assert required_ids <= html_ids
    assert 'api(agentApiPath("/sessions")' in SCRIPT
    assert "agentApiPath(`/sessions/${encodeURIComponent(sessionId)}`)" in SCRIPT
    assert "`/sessions/${encodeURIComponent(sessionId)}/runs`" in SCRIPT
    assert 'method: "DELETE"' in SCRIPT
    assert "preserveTimeline: true" in SCRIPT


def test_agent_drawer_exposes_safe_scoped_lifecycle_management() -> None:
    required_ids = {
        "agent-drawer",
        "active-agent-title",
        "agent-id",
        "new-agent-form",
        "new-agent-name",
        "new-agent-button",
        "agent-list-status",
        "agent-list",
        "research-environment-status",
        "research-environment-packages",
        "research-environment-install",
        "research-environment-delete",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))
    renderer = _function_body("renderAgentList")
    creator = _function_body("createAgent")
    upgrader = _function_body("upgradeAgent")
    deletion = _function_body("deleteAgent")

    assert required_ids <= html_ids
    assert "系统 Agent 是正式默认运行时" in INDEX
    assert 'const SYSTEM_AGENT_ID = "00000000-0000-4000-8000-000000000001"' in SCRIPT
    assert 'api("/api/agents")' in SCRIPT
    assert "function agentApiPath" in SCRIPT
    assert "textContent = agent.display_name" in renderer
    assert "switchAgent(agent.agent_id)" in renderer
    assert "clearSelectedSession()" in _function_body("loadAgentSurface")
    assert "await refreshCommands()" in _function_body("loadAgentSurface")
    assert "await refreshSessions(null)" in _function_body("loadAgentSurface")
    assert 'method: "POST"' in creator
    assert 'method: "POST"' in upgrader
    assert 'method: "DELETE"' in deletion
    assert "agent.agent_id === SYSTEM_AGENT_ID" in upgrader
    assert "agent.agent_id === SYSTEM_AGENT_ID" in deletion
    assert "其会话、Skill、Task、环境和沙箱数据都会清除" in deletion
    assert "PDF / DOCX 依赖可跨会话复用" in INDEX
    assert 'agentApiPath("/research-environment")' in SCRIPT
    assert "await refreshResearchEnvironment()" in _function_body("loadAgentSurface")
    assert '.innerHTML' not in renderer


def test_every_javascript_element_reference_exists_in_the_page() -> None:
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))
    referenced_ids = set(re.findall(r'querySelector\("#([a-z0-9-]+)"\)', SCRIPT))

    assert referenced_ids
    assert referenced_ids - html_ids == set()


def test_browser_api_keeps_cookie_csrf_and_inert_rendering_boundaries() -> None:
    assert 'credentials: "same-origin"' in SCRIPT
    assert 'cache: "no-store"' in SCRIPT
    assert '"X-CSRF-Token": state.csrfToken' in SCRIPT
    assert "localStorage" not in SCRIPT
    assert "sessionStorage" not in SCRIPT
    assert ".innerHTML" not in SCRIPT
    assert ".outerHTML" not in SCRIPT
    assert "insertAdjacentHTML" not in SCRIPT
    assert "document.write" not in SCRIPT
    assert "body.textContent =" in SCRIPT


def test_complete_timeline_event_body_remains_available() -> None:
    assert 'id="event-detail-dialog"' in INDEX
    assert '<h2 id="event-detail-title">规范事件消息体</h2>' in INDEX
    assert "不是 Ollama 或模型供应商的原始请求、响应报文" in INDEX
    assert "JSON.stringify(envelope, null, 2)" in SCRIPT
    assert "elements.eventDetailJson.textContent =" in SCRIPT
    assert "addTimelineEvent(envelope)" in SCRIPT
    assert "loadDurableTimeline" in SCRIPT
    assert 'addTimelineControl("stream.gap"' in SCRIPT
    assert 'addTimelineControl("stream.snapshot"' in SCRIPT


def test_timeline_has_explicit_truthful_actor_direction_and_action_mappings() -> None:
    expected_mappings = {
        '"run.started"': ("Harness", "Harness 内部", "启动 Run"),
        '"model.request.started"': ("Harness", "Harness → LLM", "提交模型请求"),
        '"model.response.finished"': (
            "LLM / Broker",
            "LLM / Broker → Harness",
            "收敛模型响应",
        ),
        '"model.recovery.started"': (
            "Harness",
            "Harness 内部",
            "切换溢出恢复投影",
        ),
        '"assistant.block.started"': ("LLM", "LLM → Harness", "开始回答内容块"),
        '"assistant.block.delta"': ("LLM", "LLM → Harness", "流式生成回答增量"),
        '"assistant.block.finished"': ("LLM", "LLM → Harness", "完成回答内容块"),
        '"assistant.block.discarded"': ("Harness", "Harness 恢复", "丢弃未完成回答块"),
        '"tool.call.requested"': ("LLM", "LLM → Harness", "请求调用工具"),
        '"tool.call.started"': ("Harness", "Harness → Tool", "启动受控工具调用"),
        '"tool.call.finished"': ("Tool/恢复", "Tool/恢复 → Harness", "返回工具结果"),
        '"stream.gap"': ("Replay", "Replay 控制", "报告事件序列缺口"),
        '"stream.snapshot"': ("Replay", "Replay 控制", "提供回放状态快照"),
    }
    for kind, labels in expected_mappings.items():
        mapping_start = SCRIPT.index(kind)
        mapping_end = SCRIPT.index("tone:", mapping_start)
        mapping = SCRIPT[mapping_start:mapping_end]
        assert all(label in mapping for label in labels)

    assert 'subject: "未知"' in SCRIPT
    assert 'direction: "方向未知"' in SCRIPT
    renderer = _function_body("renderTimelineEntries")
    assert "kind.textContent = envelope.kind" in renderer
    assert "`主体：${flow.subject}`" in renderer
    assert "`方向：${flow.direction}`" in renderer
    assert "`动作：${flow.action}`" in renderer


def test_timeline_exposes_all_runs_in_a_four_lane_sequence_workbench() -> None:
    assert 'id="timeline-run-select"' in INDEX
    assert 'id="sequence-stage"' in INDEX
    assert 'id="sequence-lane-header"' in INDEX
    for lane, label in {
        "user": "User",
        "harness": "Harness",
        "llm": "LLM",
        "tool": "Tool",
    }.items():
        assert f'data-sequence-lane="{lane}">{label}</span>' in INDEX

    assert "function collectTimelineRuns(messages)" in SCRIPT
    assert "const byRunId = new Map()" in SCRIPT
    assert "state.timelineRuns.at(-1)?.runId" in SCRIPT
    assert "option.value = run.runId" in SCRIPT
    assert "void selectTimelineRun(elements.timelineRunSelect.value)" in SCRIPT

    lane_header_rule = _css_rule(".sequence-lane-header")
    sequence_step_rule = _css_rule(".sequence-step")
    assert "grid-template-columns: repeat(4" in lane_header_rule
    assert "grid-template-columns: repeat(4" in sequence_step_rule
    assert ".sequence-step.lane-user" in STYLES
    assert ".sequence-step.lane-harness" in STYLES
    assert ".sequence-step.lane-llm" in STYLES
    assert ".sequence-step.lane-tool" in STYLES
    assert ".sequence-step.lane-replay" in STYLES
    assert 'document.createElementNS(namespace, "svg")' in SCRIPT
    assert "<canvas" not in INDEX.lower()
    assert 'createElement("canvas")' not in SCRIPT


def test_replay_workspace_is_two_column_responsive_and_scrolls_narrow_lanes() -> None:
    desktop = _css_rule(".replay-grid")
    sequence_stage = _css_rule(".sequence-stage")
    responsive = STYLES[STYLES.index("@media (max-width: 1080px)") :]

    assert "grid-template-columns: minmax(36rem, 1.45fr) minmax(24rem, 0.8fr)" in desktop
    assert "overflow: auto" in sequence_stage
    assert "min-width: 36rem" in STYLES
    assert ".replay-grid {\n    grid-template-columns: 1fr;" in responsive
    assert "@media (max-width: 680px)" in responsive
    assert "@media (prefers-reduced-motion: reduce)" in responsive


def test_conversation_is_grouped_by_turn_and_live_content_stays_in_its_run() -> None:
    grouping = _function_body("conversationTurnGroups")
    renderer = _function_body("renderConversationTurns")
    appender = _function_body("appendMessage")
    live_assistant = _function_body("ensureLiveAssistant")

    assert "message.turn_id || message.run_id || message.message_id" in grouping
    assert "const byKey = new Map()" in grouping
    assert "group.messages.push(message)" in grouping
    assert "group.status = message.turn_status" in grouping
    assert 'card.className = "turn-card"' in renderer
    assert "card.dataset.turnId = group.turnId" in renderer
    assert "card.dataset.runId = group.runId" in renderer
    assert "TURN_STATUS_LABELS[group.status]" in renderer
    assert "messages.append(rendered.message)" in renderer
    assert "void selectRunFromTurn(group.runId)" in renderer
    assert "state.conversationMessages.push(record)" in appender
    assert "state.liveAssistantMessage = record" in appender
    assert "renderConversationTurns()" in appender
    assert "runId: state.activeRun?.runId" in live_assistant
    assert "live: true" in live_assistant


def test_turn_run_and_sequence_node_selection_are_bidirectionally_linked() -> None:
    turn_selector = _function_body("selectRunFromTurn")
    run_selector = _function_body("setSelectedTimelineRunId")
    node_selector = _function_body("selectTimelineEntry")
    turn_sync = _function_body("syncSelectedTurn")
    timeline_selector = _function_body("selectTimelineRun")

    assert "await selectTimelineRun(runId" in turn_selector
    assert "syncSelectedTurn(scrollTurn)" in run_selector
    assert "syncSelectedTurn(true)" in node_selector
    assert 'card.classList.toggle("selected", active)' in turn_sync
    assert 'setAttribute("aria-pressed", String(active))' in turn_sync
    assert "scrollIntoView" in turn_sync
    assert "state.activeRun !== null" not in timeline_selector


def test_each_run_keeps_an_independent_timeline_view_during_live_switching() -> None:
    run_selector = _function_body("setSelectedTimelineRunId")
    event_adder = _function_body("addTimelineEvent")
    session_selector = _function_body("selectSession")

    assert "timelineEntriesByRun: new Map()" in SCRIPT
    assert "selectedTimelineEntryKeyByRun: new Map()" in SCRIPT
    assert "state.timelineEntriesByRun.get(normalized)" in run_selector
    assert "state.selectedTimelineEntryKeyByRun.get(normalized)" in run_selector
    assert "state.timelineEntriesByRun.get(envelope.run_id)" in event_adder
    assert "state.timelineEntriesByRun.set(envelope.run_id, entries)" in event_adder
    assert "envelope.run_id === state.selectedTimelineRunId" in event_adder
    assert "state.timelineEntriesByRun.clear()" in session_selector


def test_timeline_run_switch_ignores_stale_replay_responses() -> None:
    loader = _function_body("loadDurableTimeline")
    selector = _function_body("selectTimelineRun")

    assert "timelineLoadIsCurrent" in loader
    assert "return false" in loader
    assert "const timelineRequest = ++state.timelineRequest" in selector
    assert "timelineLoadIsCurrent" in selector
    assert "clearTimeline(runId)" in selector


def test_context_inspector_is_explicit_lazy_and_content_withholding() -> None:
    required_ids = {
        "context-inspect-button",
        "event-inspector-context-button",
        "context-inspect-dialog",
        "context-inspect-close",
        "context-inspect-availability",
        "context-inspect-metrics",
        "context-section-list",
        "context-inspect-notice",
        "context-inspect-json",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))
    inspector = _function_body("inspectSelectedRunContext")
    renderer = _function_body("renderContextInspection")

    assert required_ids <= html_ids
    assert "查看本轮上下文" in INDEX
    assert "Section 正文默认不返回" in INDEX
    assert "prompt 和其它隐藏 prompt 不暴露" in INDEX
    assert "void inspectSelectedRunContext(elements.contextInspectButton)" in SCRIPT
    assert "void inspectSelectedRunContext(elements.eventInspectorContextButton)" in SCRIPT
    assert "`/api/agents/${encodeURIComponent(contextAgentId)}/runs/`" in inspector
    assert "`${encodeURIComponent(runId)}/context`" in inspector
    assert "contextAgentId" in inspector
    assert "include_content" not in SCRIPT
    assert "JSON.stringify(inspection, null, 2)" in renderer
    assert "elements.contextInspectJson.textContent =" in SCRIPT
    assert "section.content_digest.slice(0, 12)" in renderer
    assert "section.content_bytes" in renderer
    assert "section.content_digest" in renderer


def test_context_inspector_explains_history_window_and_summary_only() -> None:
    renderer = _function_body("renderContextInspection")

    assert "included_history_message_count" in renderer
    assert "history_message_count" in renderer
    assert "omitted_history_message_count" in renderer
    assert "windowing_strategy" in renderer
    assert "estimated_input_tokens" in renderer
    assert "input_budget_tokens" in renderer
    assert "provider_message_count" in renderer
    assert "Gateway 重启或 ContextPlan retention 结束后" in renderer
    assert "run.started 摘要" in renderer
    assert "conversation 内容请查看右侧对话" in renderer
    assert "查看左侧对话" not in renderer


def test_context_inspector_rejects_stale_or_unexpected_responses() -> None:
    inspector = _function_body("inspectSelectedRunContext")
    validator = _function_body("validateContextInspection")

    assert "const sessionRequest = state.sessionRequest" in inspector
    assert "const timelineRequest = state.timelineRequest" in inspector
    assert "const contextRequest = ++state.contextRequest" in inspector
    assert "contextLoadIsCurrent" in inspector
    assert "error.message" not in inspector
    assert "CONTEXT_RESPONSE_FIELDS" in validator
    assert "CONTEXT_SECTION_FIELDS" in validator
    assert 'value.availability === "exact"' in validator
    assert 'value.availability === "summary_only"' in validator
    assert 'value.content_exposure !== "withheld"' in validator


def test_context_loading_is_released_across_session_and_timeline_generation_changes() -> None:
    controls = _function_body("setContextInspectControl")
    session_selector = _function_body("selectSession")
    run_selector = _function_body("setSelectedTimelineRunId")
    inspector = _function_body("inspectSelectedRunContext")

    assert "state.sessionLoading" in controls
    assert "state.sessionLoading = true" in session_selector
    assert "requestNumber === state.sessionRequest" in session_selector
    assert "state.sessionLoading = false" in session_selector
    assert "contextRequest === state.contextRequest" in inspector
    assert "clearContextInspector()" in inspector
    assert "state.contextLoading = false" in inspector
    assert "clearContextInspector()" in run_selector


def test_terminal_events_update_the_run_selector_without_waiting_for_session_refresh() -> None:
    updater = _function_body("updateTimelineRunTerminalStatus")
    renderer = _function_body("renderEnvelope")
    sse_renderer = _function_body("renderSseFrame")

    assert '"run.completed": "completed"' in updater
    assert '"run.failed": "failed"' in updater
    assert '"run.cancelled": "cancelled"' in updater
    assert "run.status = status" in updater
    assert "renderTimelineRunSelect()" in updater
    assert "updateTimelineRunTerminalStatus(runContext.runId, envelope.kind)" in renderer
    assert "updateTimelineRunTerminalStatus(runContext.runId, terminalKind)" in sse_renderer


def test_sequence_flow_maps_user_model_tool_and_replay_without_faking_events() -> None:
    turn_fact = _function_body("selectedRunTurnFact")
    flow = _function_body("eventFlow")
    connector = _function_body("createSequenceConnector")
    renderer = _function_body("renderTimelineEntries")

    assert 'messages.find((message) => message.role === "user")' in turn_fact
    assert 'type: "derived-user"' in turn_fact
    assert 'source: "user"' in flow
    assert 'target: "harness"' in flow
    assert 'subject: "User（UI 投影）"' in flow
    assert 'direction: "User → Harness"' in flow
    assert 'source: "harness", target: "llm"' in flow
    assert 'source: "llm", target: "harness"' in flow
    assert 'source: "harness", target: "tool"' in flow
    assert 'source: "tool", target: "harness"' in flow
    assert 'source: "replay", target: "replay"' in flow
    assert 'flow.source === "replay" || flow.target === "replay"' in connector
    assert 'svg.classList.add("sequence-replay-rail")' in connector
    assert "item.dataset.fromLane = flow.source" in renderer
    assert "item.dataset.toLane = flow.target" in renderer
    assert '"turn.user.submitted · UI projection"' in renderer
    assert '"来源：ConversationStore；不是 EventEnvelope"' in renderer


def test_node_inspector_has_safe_complete_views_and_truthful_content_boundaries() -> None:
    required_ids = {
        "event-inspector",
        "event-inspector-empty",
        "event-inspector-summary",
        "event-inspector-business",
        "event-inspector-payload",
        "event-inspector-envelope",
        "event-inspector-context-button",
        "inspector-tab-summary",
        "inspector-tab-business",
        "inspector-tab-payload",
        "inspector-tab-envelope",
        "inspector-tab-context",
        "inspector-panel-summary",
        "inspector-panel-business",
        "inspector-panel-payload",
        "inspector-panel-envelope",
        "inspector-panel-context",
    }
    html_ids = re.findall(r'\bid="([a-z0-9-]+)"', INDEX)
    business_body = _function_body("eventBusinessBody")
    conversation_body = _function_body("conversationBodyForRun")
    renderer = _function_body("renderEventInspector")
    tab_selector = _function_body("selectInspectorTab")

    assert required_ids <= set(html_ids)
    assert len(html_ids) == len(set(html_ids))
    assert "逻辑消息" in INDEX
    assert "Payload" in INDEX
    assert "Envelope" in INDEX
    assert "Context" in INDEX
    assert "Replay 节点显示 control frame" in INDEX
    assert "不是 Ollama 或模型供应商的原始报文" in INDEX
    assert '["user", "assistant"].includes(message.role)' in conversation_body
    assert 'kind === "assistant.block.finished"' in business_body
    assert "payload.content" in business_body
    assert 'kind === "tool.call.requested"' in business_body
    assert "JSON.stringify(payload.arguments, null, 2)" in business_body
    assert 'kind === "tool.call.finished"' in business_body
    assert "payload.result" in business_body
    assert "模型原始请求正文按协议未持久化" in business_body
    assert "模型供应商原始响应正文按协议未持久化" in business_body
    assert "Replay control，不是 Run 业务消息" in business_body
    assert "UI 投影，不是 EventEnvelope" in business_body
    assert "elements.eventInspectorBusiness.textContent" in renderer
    assert "elements.eventInspectorPayload.textContent" in renderer
    assert "elements.eventInspectorEnvelope.textContent" in renderer
    assert "JSON.stringify(entry.envelope.payload, null, 2)" in renderer
    assert "JSON.stringify(entry.envelope, null, 2)" in renderer
    assert "无 canonical EventEnvelope" in renderer
    assert "Replay control frame（不是 canonical EventEnvelope）" in renderer
    assert 'entry.type === "event"' in renderer
    assert 'tab.setAttribute("aria-selected", String(active))' in tab_selector
    assert "panel.hidden = panel.dataset.inspectorPanel !== tabName" in tab_selector
    assert 'tab.addEventListener("keydown"' in SCRIPT
    assert '["ArrowLeft", "ArrowRight", "Home", "End"]' in SCRIPT
    assert "event.preventDefault()" in SCRIPT
    assert "tabs[target].focus()" in SCRIPT


def test_replay_controls_filter_without_mutating_the_canonical_sequence() -> None:
    required_ids = {
        "replay-toolbar",
        "replay-prev-button",
        "replay-play-button",
        "replay-next-button",
        "replay-follow-button",
        "timeline-filter-form",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))
    stopper = _function_body("stopReplay")
    player = _function_body("startReplay")
    stepper = _function_body("stepTimeline")
    follower = _function_body("toggleFollowLatest")
    filter_setter = _function_body("setTimelineFilter")
    category = _function_body("timelineCategory")
    run_switcher = _function_body("setSelectedTimelineRunId")
    timeline_clearer = _function_body("clearTimeline")
    clearer = _function_body("clearSelectedSession")

    assert required_ids <= html_ids
    assert "elements.replayPrevButton?.addEventListener" in SCRIPT
    assert "elements.replayPlayButton?.addEventListener" in SCRIPT
    assert "elements.replayNextButton?.addEventListener" in SCRIPT
    assert "elements.replayFollowButton?.addEventListener" in SCRIPT
    assert "elements.timelineFilterForm?.addEventListener" in SCRIPT
    assert "window.clearInterval(state.replayTimer)" in stopper
    assert "state.replayTimer = null" in stopper
    assert "stopReplay()" in player
    assert "window.setInterval" in player
    assert "state.followLatest = false" in player
    assert "api(" not in player
    assert "stopReplay()" in stepper
    assert "stopReplay()" in follower
    assert "stopReplay()" in filter_setter
    assert '["all", "llm", "tool", "error"].includes(filter)' in filter_setter
    assert "state.timelineFilter = filter" in filter_setter
    assert "renderTimelineEntries()" in filter_setter
    error_test = category.index('["run.failed", "run.cancelled"')
    assert error_test < category.index('kind.startsWith("model.")')
    assert error_test < category.index('kind.startsWith("tool.")')
    assert "stopReplay()" in run_switcher
    assert "stopReplay()" in timeline_clearer
    assert "stopReplay()" in clearer
    assert 'state.timelineFilter = "all"' in clearer
    assert "state.followLatest = true" in clearer


def test_event_cards_summarize_model_iterations_without_body_or_full_digest() -> None:
    summary = _function_body("eventSummary")
    renderer = _function_body("renderTimelineEntries")

    assert 'envelope.kind === "model.request.started"' in summary
    assert "payload.message_count" in summary
    assert "payload.tool_count" in summary
    assert "payload.tool_result_call_ids" in summary
    assert 'envelope.kind === "model.response.finished"' in summary
    assert 'envelope.kind === "model.recovery.started"' in summary
    assert "payload.provider_call_index" in summary
    assert "payload.attempt" in summary
    assert "payload.input_tokens" in summary
    assert "payload.output_tokens" in summary
    assert 'envelope.kind === "run.started"' in summary
    assert "context.included_history_message_count" in summary
    assert "context.history_message_count" in summary
    assert 'envelope.kind.startsWith("tool.call.")' in summary
    assert "shortEventIdentifier(payload.tool_id)" in summary
    assert "shortEventIdentifier(payload.call_id)" in summary
    assert "request_digest" not in summary
    assert "payload.arguments" not in summary
    assert "payload.result" not in summary
    assert "summary.textContent = eventSummary(envelope)" in renderer


def _function_body(name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{", SCRIPT)
    assert match is not None, f"missing JavaScript function: {name}"
    start = match.end()
    depth = 1
    for index in range(start, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start:index]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _css_rule(selector: str) -> str:
    start = STYLES.index(f"{selector} {{")
    end = STYLES.index("}", start)
    return STYLES[start : end + 1]


def test_terminal_event_keeps_the_owning_run_locked_until_reconciliation() -> None:
    renderer = _function_body("renderEnvelope")
    completer = _function_body("completeRunContext")

    assert "runContext.terminalSeen = true" in renderer
    assert "state.activeRun = null" not in renderer
    assert "state.settling = false" not in renderer
    assert "state.activeRun = null" in completer
    assert "state.settling = false" in completer


def test_sse_reconnect_is_bounded_and_resumes_from_the_last_sequence() -> None:
    consumer = _function_body("consumeEventStream")
    reconnect = _function_body("streamWithReconnect")

    assert 'headers["Last-Event-ID"] = String(runContext.lastSeq)' in consumer
    assert "attempt <= MAX_SSE_RECONNECTS" in reconnect
    assert "runContext.terminalSeen" in reconnect
    assert "MAX_SSE_RECONNECTS = 3" in SCRIPT
    renderer = _function_body("renderSseFrame")
    assert 'frame?.event === "stream.gap"' in renderer
    assert 'frame?.event === "stream.snapshot"' in renderer
    assert 'payload.reason !== "retention"' in renderer
    assert "runContext.lastSeq = cursor" in renderer
    assert "runContext.terminalSeen = true" in renderer


def test_preserved_timeline_uses_fetch_then_commit_and_survives_refresh_error() -> None:
    selector = _function_body("selectSession")

    fetch_position = selector.index("await api(")
    render_position = selector.index("renderMessages(detail.messages)")
    assert fetch_position < render_position
    assert "if (!preserveTimeline) clearTimeline()" in selector
    assert "clearSelectedSession" not in selector


def test_turn_status_running_recovery_and_auth_expiry_are_explicit() -> None:
    normalizer = _function_body("normalizeConversationMessage")
    renderer = _function_body("renderConversationTurns")

    assert "turn_status:" in normalizer
    assert "message.turn_status" in normalizer
    assert "turnStatus: message.turn_status" in renderer
    assert 'message.turn_status === "running"' in SCRIPT
    assert "attachRecoveredRun(session.session_id, recoverableRunId)" in SCRIPT
    assert 'response.status === 401' in SCRIPT
    assert 'setUnauthenticated("登录已过期，请重新认证")' in SCRIPT


def test_mutations_lock_controls_and_conflicts_refresh_server_state() -> None:
    controls = _function_body("setRunControls")
    deletion = _function_body("deleteSession")

    assert "state.mutationPending" in controls
    assert "state.mutationPending = true" in deletion
    assert "state.mutationPending = false" in deletion
    assert "error.status === 409" in deletion
    assert "await refreshSessions(state.sessionId)" in deletion


def test_frontend_exposes_bounded_subagent_mailbox_and_child_replay() -> None:
    assert 'id="subagent-panel"' in INDEX
    assert 'id="subagent-list"' in INDEX
    assert "async function refreshSubagents" in SCRIPT
    assert "/subagents`" in SCRIPT
    assert "content.textContent = message.content" in SCRIPT
    assert 'kind: "subagent"' in SCRIPT
    assert "expectedConversationId" in SCRIPT

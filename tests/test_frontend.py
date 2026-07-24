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
    assert "`/sessions/${encodeURIComponent(sessionId)}`" in SCRIPT
    assert "`/sessions/${encodeURIComponent(sessionId)}/runs`" in SCRIPT
    assert 'method: "DELETE"' in SCRIPT
    assert "preserveTimeline: true" in SCRIPT


def test_conversation_first_workspace_keeps_operations_available_on_demand() -> None:
    required_ids = {
        "navigation-rail",
        "navigation-toggle",
        "navigation-close",
        "runtime-inspector-button",
        "runtime-inspector-close",
        "runtime-event-badge",
        "workspace-backdrop",
        "replay-workbench",
        "composer-status",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))

    assert required_ids <= html_ids
    assert 'class="primary-workspace"' in INDEX
    assert 'data-prompt-suggestion=' in INDEX
    assert 'aria-keyshortcuts="Enter"' in INDEX
    assert "function setRuntimeInspector" in SCRIPT
    assert 'elements.replayWorkbench.inert = !next' in SCRIPT
    assert 'setRuntimeInspector(true, { focus: true })' in _function_body(
        "selectRunFromTurn"
    )
    assert 'event.key !== "Enter" || event.shiftKey || event.isComposing' in SCRIPT
    assert "elements.runForm.requestSubmit()" in SCRIPT
    assert "elements.composerStatus.textContent = message" in SCRIPT
    assert ".workspace.runtime-open .replay-workbench" in STYLES
    assert "@media (max-width: 860px)" in STYLES


def test_responsive_shell_has_safe_viewport_and_readable_status_colors() -> None:
    assert "viewport-fit=cover" in INDEX
    assert "interactive-widget=resizes-content" in INDEX
    assert "100dvh" in STYLES
    assert "env(safe-area-inset-top, 0px)" in STYLES
    assert "env(safe-area-inset-bottom)" in STYLES
    assert 'aria-label="上下文窗口占用"' in INDEX
    assert (
        'aria-describedby="context-usage-value context-usage-detail"' in INDEX
    )
    assert 'id="status-text" role="status" aria-live="polite"' in INDEX
    assert 'id="conversation-messages"' in INDEX
    assert 'role="log"' in INDEX
    assert 'aria-live="off"' in INDEX
    for live_id in (
        "interaction-live-status",
        "preparation-live-status",
        "run-live-status",
        "failure-live-status",
    ):
        assert f'id="{live_id}"' in INDEX
    assert "transition: none !important" in STYLES
    assert "animation: none !important" in STYLES
    assert re.search(
        r"\.session-menu-trigger\s*\{[^}]*min-width:\s*2\.75rem;"
        r"[^}]*min-height:\s*2\.75rem;",
        STYLES,
        re.DOTALL,
    )
    assert "font-size: 0.9rem" in STYLES

    def css_color(variable: str) -> str:
        match = re.search(rf"--{re.escape(variable)}:\s*(#[0-9a-fA-F]{{6}})", STYLES)
        assert match is not None
        return match.group(1)

    def luminance(color: str) -> float:
        channels = [int(color[offset : offset + 2], 16) / 255 for offset in (1, 3, 5)]
        linear = [
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(foreground: str, background: str) -> float:
        values = sorted((luminance(foreground), luminance(background)), reverse=True)
        return (values[0] + 0.05) / (values[1] + 0.05)

    muted = css_color("muted")
    accent = css_color("accent")
    accent_soft = css_color("accent-soft")
    panel_raised = css_color("panel-raised")
    lane_background = "#f6f8f4"
    lane_header_rule = _css_rule(".sequence-lane-header")
    sequence_node_rule = _css_rule(".sequence-node,\n.event-entry")
    assert "background: #f6f8f4" in lane_header_rule
    assert "background: #ffffff" in sequence_node_rule
    assert contrast(muted, "#ffffff") >= 4.5
    assert contrast(muted, accent_soft) >= 4.5
    assert contrast(accent, "#ffffff") >= 4.5
    assert contrast(accent, accent_soft) >= 4.5
    for lane in ("user", "harness", "llm", "tool"):
        assert contrast(css_color(lane), lane_background) >= 4.5
    assert contrast(css_color("tool"), accent_soft) >= 4.5
    assert contrast(css_color("tool"), panel_raised) >= 4.5
    assert contrast(css_color("text"), "#ffffff") >= 4.5
    assert contrast(muted, "#ffffff") >= 4.5


def test_conversation_follows_latest_and_projects_trusted_context_usage() -> None:
    required_ids = {
        "conversation-latest-button",
        "context-usage",
        "context-usage-label",
        "context-usage-value",
        "context-usage-meter",
        "context-usage-fill",
        "context-usage-detail",
        "turn-token-usage-label",
        "turn-token-usage-value",
        "message-byte-usage",
        "continue-session-button",
    }
    html_ids = set(re.findall(r'\bid="([a-z0-9-]+)"', INDEX))

    assert required_ids <= html_ids
    assert 'id="conversation-messages"' in INDEX
    assert 'tabindex="0"' in INDEX
    assert "function conversationIsNearLatest" in SCRIPT
    assert "function scheduleConversationLatest" in SCRIPT
    assert "elements.conversationMessages.scrollTop =" in SCRIPT
    assert "elements.conversationMessages.scrollHeight" in SCRIPT
    assert "scroll-behavior: auto" in STYLES
    assert "function refreshNextTurnPreview" in SCRIPT
    assert "/context-preview" in SCRIPT
    assert "function validNextTurnPreview" in SCRIPT
    assert "function validChatOnlyProjection" in SCRIPT
    assert "CHAT_ONLY_PROJECTION_FIELDS" in SCRIPT
    assert 'value.version === "next-turn-chat-only-projection-v1"' in SCRIPT
    assert "contextProjectionForDisplay" in SCRIPT
    assert "function captureSessionTurnUsage" in SCRIPT
    assert "plan.fixed_context_tokens" in SCRIPT
    assert "plan.safe_user_tokens" in SCRIPT
    assert "plan.compact_before_user_tokens" in SCRIPT
    assert "plan.fixed_context_error_margin_tokens" in SCRIPT
    assert "toolset_calibration_unavailable" in SCRIPT
    assert "纯对话实测不能安全推算工具场景" in SCRIPT
    assert 'plan.projection_mode === "conservative_tools"' in SCRIPT
    assert 'typeof value.chat_calibration_available === "boolean"' in SCRIPT
    assert "不使用浏览器估算 token" in SCRIPT
    assert 'envelope.kind === "model.response.finished"' in SCRIPT
    assert "payload.input_tokens" in SCRIPT
    assert "payload.output_tokens" in SCRIPT
    assert "aggregate.last_input_tokens" in SCRIPT
    assert "usage.firstInputTokens + usage.finalOutputTokens" not in SCRIPT
    assert "占用约" in SCRIPT
    assert "剩余约" in SCRIPT
    assert "下一条消息安全可写约" in SCRIPT
    assert "占用数与剩余比例暂不可用" in SCRIPT
    assert "纯对话实测基线；若下一条启用工具，将在提交前重算" in SCRIPT
    assert "usage.providerResponseCount === 0" in SCRIPT
    assert "aggregate.input_tokens === aggregate.last_input_tokens" in SCRIPT
    assert "lastInputTokens" not in SCRIPT
    assert "lastOutputTokens" not in SCRIPT
    assert "自动整理前还可增加约" in SCRIPT
    assert "Provider 实测推导占用约" not in SCRIPT
    assert "aggregate.complete" in SCRIPT
    assert ".context-usage-meter" in STYLES
    assert ".context-usage-detail" in STYLES
    assert ".turn-token-usage" in STYLES
    assert "function enforceMessageInputLimit" in SCRIPT
    assert 'maxlength="8192"' in INDEX
    assert "new TextEncoder().encode(message).length > MESSAGE_MAX_BYTES" in SCRIPT
    assert "messageBytes > 8192" in SCRIPT
    assert "/continue" in SCRIPT


def test_session_action_menu_escapes_scroll_clipping() -> None:
    assert 'menuBody.setAttribute("popover", "auto")' in SCRIPT
    assert 'menuTrigger.setAttribute("popovertarget", popoverId)' in SCRIPT
    assert "positionSessionMenuPopover(menuTrigger, menuBody)" in SCRIPT
    assert ".session-menu-popover[popover]:popover-open" in STYLES
    popover_rule = _css_rule(".session-menu-popover")
    assert "position: fixed" in popover_rule
    assert "max-height: calc(100dvh - 1rem)" in popover_rule


def test_model_timeout_stages_have_operator_visible_status() -> None:
    api_client = _function_body("api")
    assert "const MODEL_ERROR_LABELS" in SCRIPT
    assert "const API_ERROR_LABELS" in SCRIPT
    assert "const HTTP_ERROR_LABELS" in SCRIPT
    assert "function modelErrorLabel" in SCRIPT
    assert "无法连接本地服务，请检查服务状态后重试" in api_client
    assert "detailObject?.message" not in api_client
    assert 'typeof body.detail === "string"' not in api_client
    assert "model_first_frame_timeout" in SCRIPT
    assert "model_stream_idle_timeout" in SCRIPT
    assert "model_turn_deadline" in SCRIPT
    assert "model_transport_timeout" in SCRIPT
    assert "正在等待首个响应帧" in SCRIPT
    assert "模型正在流式生成回答" in SCRIPT
    assert "runContext.terminalPayload = payload" in SCRIPT


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
    renamer = _function_body("renameAgent")
    upgrader = _function_body("upgradeAgent")
    deletion = _function_body("deleteAgent")

    assert required_ids <= html_ids
    assert "系统智能体是正式默认运行时" in INDEX
    assert 'const SYSTEM_AGENT_ID = "00000000-0000-4000-8000-000000000001"' in SCRIPT
    assert 'api("/api/agents")' in SCRIPT
    assert "function agentApiPath" in SCRIPT
    assert "textContent = agent.display_name" in renderer
    assert "运行环境 v${agent.generation}" in renderer
    assert "switchAgent(agent.agent_id)" in renderer
    assert "clearSelectedSession()" in _function_body("loadAgentSurface")
    assert "await refreshCommands(agentId, agentEpoch)" in _function_body("loadAgentSurface")
    assert "await refreshSessions(null, agentId, agentEpoch)" in _function_body(
        "loadAgentSurface"
    )
    assert 'method: "POST"' in creator
    assert 'method: "PATCH"' in renamer
    assert "renamed.generation !== agent.generation" in renamer
    assert "运行环境未重建" in renamer
    assert 'method: "POST"' in upgrader
    assert "重建运行环境" in renderer
    assert "普通重命名不需要执行此操作" in upgrader
    assert 'body: JSON.stringify({})' in upgrader
    assert "重命名 / 升级" not in SCRIPT
    assert 'method: "DELETE"' in deletion
    assert "agent.agent_id === SYSTEM_AGENT_ID" in renamer
    assert "agent.agent_id === SYSTEM_AGENT_ID" in upgrader
    assert "agent.agent_id === SYSTEM_AGENT_ID" in deletion
    assert "其会话、已安装能力、后台任务、环境和沙箱数据都会清除" in deletion
    assert "PDF / DOCX 依赖可跨会话复用" in INDEX
    assert "重命名不会重建环境" in INDEX
    assert ".agent-advanced" in STYLES
    assert 'agentApiPath("/research-environment")' in SCRIPT
    assert "await refreshResearchEnvironment(agentId, agentEpoch)" in _function_body(
        "loadAgentSurface"
    )
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
    assert "pruneInactiveTimelineCaches()" in session_selector
    assert "state.timelineEntriesByRun.clear()" not in session_selector


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
    assert "LEGACY_CONTEXT_PLAN_FIELDS" in validator
    assert "planFields" in validator
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
    assert 'envelope.kind === "run.completed"' in summary
    assert '"max_output"' in summary
    assert '"repetition_truncated"' in summary
    assert "已保留" in summary
    assert (
        "检测到回答进入重复循环；重复尾部已截断，本轮正文已提交，Provider 用量不完整"
        in summary
    )
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
    assert "state.activeRuns.delete(runContext.sessionId)" in completer
    assert "state.backgroundCompletions.set(runContext.sessionId" in completer
    assert "const foreground = state.sessionId === runContext.sessionId" in completer
    assert 'runContext.terminalPayload?.reason === "max_output"' in completer
    assert 'runContext.terminalPayload?.reason === "repetition_truncated"' in completer
    assert "已保留此前内容" in completer
    assert "重复尾部已截断" in completer


def test_repetition_completion_is_presented_as_success_with_incomplete_usage() -> None:
    summary = _function_body("eventSummary")
    usage = _function_body("renderSessionTurnUsage")
    business = _function_body("eventBusinessBody")
    category = _function_body("timelineCategory")

    assert '"repetition_truncated"' in summary
    assert "payload.usage_complete" in summary
    assert "payload.error_code" in summary
    assert "Provider 用量不完整" in summary
    assert "Provider 用量不完整" in usage
    assert 'payload?.reason === "repetition_truncated"' in business
    assert "重复尾部已截断" in business
    assert '"repetition_truncated"' not in category.split('return "error"')[0]


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
    assert "const mutationScope = beginMutation()" in deletion
    assert "finishMutation(mutationScope)" in deletion
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

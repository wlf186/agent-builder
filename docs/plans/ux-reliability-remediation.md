---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-22
review_cycle: quarterly
---

# Web UX and runtime resilience remediation plan

## Objective

本计划是用户体验整改的唯一细粒度状态账本，覆盖 7 个 P0、5 个 P1 和 2 个 P2，合计
14 个工作项。目标不是只调整视觉样式，而是让一次提交从输入、上下文准备、Provider
传输、流式回答到失败恢复都有清晰、持久、可操作的状态；同时保证长会话、窄屏、重启和
Provider 故障下的体验一致。

本计划复用 [context reliability remediation](context-reliability-remediation.md) 已完成的
持久 transcript、类型化计数域、next-turn projection、自动压缩和 continuation 合同，不建立
第二套上下文或消息事实源。

## Product invariants

- 用户触发的任何操作都必须立即产生可见反馈，不能以静默 `return` 表示忙碌或拒绝；
- Provider 重试、等待和失败必须可观察，但事件、日志和 UI 不得包含 endpoint、prompt、
  token、cookie 或其它秘密；
- failed/cancelled/interrupted Turn 不进入后续模型上下文，但其终态原因必须可跨刷新恢复；
- “下一条消息余量”、单条 UTF-8 byte 上限和上一轮 Provider 计算量是三个独立概念；
- Conversation、Run 和草稿状态按会话隔离；后台 Run 不得阻止用户查看其它会话；
- Markdown 只生成受限 DOM，不执行 HTML、脚本、事件属性、危险 URL 或外部资源；
- UI 历史分页不能改变 canonical SQLite transcript，也不能造成消息重复、遗漏或顺序漂移；
- 响应式改动必须覆盖触屏、软键盘、安全区、reduced motion 和键盘/屏幕阅读器操作；
- 不增加逐 token 磁盘写、无界健康记录、无界 DOM、无界 timer 或无界重试。

## Dependency graph

```text
UX-R02-01 transport telemetry ── UX-R02-02 main-model circuit
              └─────────────── UX-R02-04 durable failure/retry
UX-R02-03 preparing state ───── UX-R02-10 background runs
UX-R02-05 responsive shell ──── UX-R02-09 typography/a11y
UX-R02-06 durable calibration ─ UX-R02-08 context language
UX-R02-01..06 ───────────────── UX-R02-07 qualification matrix
UX-R02-04 + 11 + 12 ─────────── UX-R02-13 message actions/branch
UX-R02-01 + 08 ──────────────── UX-R02-14 advanced details
```

## Work-item ledger

状态只使用 `not_started`、`in_progress`、`blocked`、`done`、`deferred` 或 `superseded`。

| 顺序 | ID | Priority | 目标 | 状态 |
| ---: | --- | --- | --- | --- |
| 1 | UX-R02-01 | P0 | Provider transport attempt、elapsed 和 TTFB 的有界可观测性 | done |
| 2 | UX-R02-02 | P0 | 主模型按 profile 隔离的短期健康退避与熔断 | done |
| 3 | UX-R02-03 | P0 | 消除发送静默无响应，显式且可取消的上下文准备态 | done |
| 4 | UX-R02-04 | P0 | 持久失败摘要、明确恢复动作和安全重试 | done |
| 5 | UX-R02-05 | P0 | 360px 起的移动端、软键盘、安全区与触控修复 | done |
| 6 | UX-R02-06 | P0 | preview/admission/compaction 共享同一持久 token 校准 | done |
| 7 | UX-R02-07 | P0 | 8K/16K/32K、重启、压缩与 Provider 故障回归矩阵 | done |
| 8 | UX-R02-08 | P1 | 重写上下文卡片、压缩策略和 Provider 用量文案 | done |
| 9 | UX-R02-09 | P1 | 字号、对比度、焦点、live region 与触控无障碍 | done |
| 10 | UX-R02-10 | P1 | Run 后台继续、按会话隔离状态和草稿 | done |
| 11 | UX-R02-11 | P1 | 长会话向前分页、增量 DOM 和稳定滚动锚点 | done |
| 12 | UX-R02-12 | P1 | 确定性自动标题、显式重命名和触屏会话菜单 | done |
| 13 | UX-R02-13 | P2 | 安全受限 Markdown、复制、重试/重新生成与分支 | done |
| 14 | UX-R02-14 | P2 | 普通中文界面与高级 Provider/ToolSet 技术检查器分层 | done |

## Acceptance checklist

### UX-R02-01 — Provider transport telemetry

- [x] 每次 HTTP 尝试在发起前产生 `attempt_started`，首帧、失败或取消时产生唯一结束状态；
- [x] 字段只含有界 attempt/max、outcome、elapsed_ms、first_frame_ms 和稳定错误码；
- [x] 用户能在等待期间看到第几次尝试和已等待时间，历史时间线可跨重启回放；
- [x] request observer 继续 fail closed；transport 诊断 observer 异常不能造成重复 HTTP、
  改变模型流、错序 request/response boundary 或提示词泄露；
- [x] event/SQLite 写入按 HTTP attempt，而不是 token、frame 或 timer tick。

### UX-R02-02 — Main-model health circuit

- [x] 只把实际 HTTP attempt 的零首帧瞬态失败计入健康退避；partial、idle、cancel 不计；
- [x] key 绑定受信 model/profile，不含 endpoint 明文；成功首帧立即复位；
- [x] map 大小、failure threshold 和 TTL 固定有界；Gateway 重启安全清空；
- [x] circuit open 时不占 model slot、不发 HTTP，以 `model_temporarily_unhealthy` 快速返回；
- [x] 一个模型的失败不影响目录中的其它模型。

### UX-R02-03 — Preparing and submission feedback

- [x] 普通消息在 settling/active/mutation 状态提交时显示明确原因，不静默返回；
- [x] POST 发出前立即展示保留草稿的“正在准备上下文”状态；
- [x] 摘要/压缩准备变成可观察阶段，用户可取消尚未取得 Run ID 的请求；
- [x] slash command 与普通消息的可用状态、按钮标签和 Enter 行为一致；
- [x] 网络/API admission 失败后草稿、模型选择和压缩选择保持不变。

### UX-R02-04 — Durable failure and retry

- [x] Session detail 为 failed/cancelled/interrupted Turn 返回有界 terminal summary；
- [x] 失败卡片跨刷新保留 code 的中文说明、阶段、耗时和 retryability；
- [x] 可重试失败提供动作，重复提交必须显式创建新 Turn 且不把失败内容加入历史；
- [x] retry 不绕过 active Run、Turn capacity、模型目录、CSRF 或 byte/context admission；
- [x] `0 total` 不再伪装成 Provider 实际用量，明确显示“未取得用量”。

### UX-R02-05 — Responsive shell

- [x] 360x640、390x844、768x1024、1440x900 均无横向页面溢出；
- [x] composer 在虚拟键盘和动态 viewport 下完整可见，支持 safe-area inset；
- [x] masthead、上下文说明和 composer toolbar 在窄屏不裁切；
- [x] 触屏主要目标至少 44x44 CSS px；会话操作不依赖 hover；
- [x] inspector/navigation 在窄屏互斥且焦点可恢复。

### UX-R02-06 — Durable calibration authority

- [x] preview、Run admission 和自动压缩调用同一持久 calibration builder；
- [x] Gateway 重启后不会出现 UI 有实测而 admission 无实测的分裂状态；
- [x] calibration 绑定 model/profile/count basis/renderer/ToolSet，漂移即失效；
- [x] incomplete/failed 用量不能产生虚假校准；无校准时明确 unavailable；
- [x] 持久化按完整 provider response 语义更新，不逐帧写入。

### UX-R02-07 — Qualification matrix

- [x] 确定性测试覆盖 8K/16K/32K、tools on/off 和 model/profile 切换；
- [x] 覆盖 trigger 前/上/后、summary 成功/复用/失败、Gateway 重启和 overflow recovery；
- [x] 覆盖 20–50 Turn、500/1500 词输出、failed/cancelled 后继续；
- [x] Provider fault 覆盖 first-frame、idle、turn deadline、429/503、SSE reconnect；
- [x] 普通长回答达到 12 KiB commit ceiling 时保留有界前缀、单个受信 marker 和最终
  Provider usage，以 `run.completed.reason=max_output` 收敛；Tool/空正文/协议错误仍失败；
- [x] 真实 qwen3.5:2b 有界 warm second/third-turn soak 记录 TTFB 和终态，不以外部随机性污染 CI；
- [x] 测试确认无静默交互、无逐 token 写、无 Run/Worker/temp 残留。

### UX-R02-08 — Context language

- [x] 主信息展示当前 committed 固定上下文“占用约 X / 模型窗口”和剩余百分比；
- [x] 次信息分开展示下一条消息安全可写量、自动整理前余量、误差、单条 byte 上限和
  上一轮计算量；
- [x] `full/collapse/summary` 映射为普通中文，原始值只在高级详情中出现；
- [x] meter 与 ARIA 同时表达实际分母；压缩阈值与完整窗口不再混称；
- [x] `conservative_tools` 投影可用时始终优先；只有独立空 ToolSet scope 已校准时才把
  `chat_only_projection` 标成纯对话基线展示，不跨 ToolSet 迁移 ratio 或误差；
- [x] 手动压缩说明完整记录不会删除，并显示 summary 可能发生的额外等待。

### UX-R02-09 — Typography and accessibility

- [x] 关键辅助文本不低于 12px，消息正文维持至少 16px；
- [x] 小字号正常文本在其实际背景上达到 WCAG AA 4.5:1；
- [x] 连接、准备、运行和失败状态使用独立且合适的 live region；
- [x] 焦点、键盘、IME、reduced-motion 和屏幕阅读器命名有浏览器回归；
- [x] 颜色不是区分成功、运行和失败的唯一方式。

### UX-R02-10 — Background runs

- [x] active Run 以 Conversation 为 key 管理，不再用单个全局指针锁住导航；
- [x] 用户可在 Run 期间查看其它会话，后台 SSE 只更新所属会话；
- [x] 每个会话有独立 active badge、草稿、模型/compact 选择和未读状态；
- [x] 回到会话能恢复 live/historical timeline，不重复应用 SSE；
- [x] terminal refresh 已读到同 Run 的 canonical assistant 后不再附加 live buffer，避免
  `turn_id`/`run_id` 的客户端分组差异产生重复 Turn；
- [x] logout、Agent switch/delete 和 pagehide 会有界取消浏览器连接，但不伪造服务端 terminal。

### UX-R02-11 — Long-conversation pagination

- [x] Session API 默认返回最新有界页，支持稳定、不可伪造的向前 cursor；
- [x] 首屏始终包含最新 Turn 和 active/terminal 状态；旧页按 canonical position 向前加载；
- [x] 前端 prepend 后保持视觉滚动锚点，不重复 Turn、消息或 Run；
- [x] 新事件只增量更新所属 Turn，避免每个 delta 全量重建 128 Turn DOM；
- [x] 删除、重启、容量边界和 cursor 漂移有负面测试。

### UX-R02-12 — Session naming and menu

- [x] 默认标题在第一条成功准入的用户消息中确定性生成，不调用模型；
- [x] 认证+CSRF rename API 有长度、Unicode、revision/active 状态与竞态检查；
- [x] 用户可在会话菜单重命名、分支/续接和删除，触屏无需 hover；
- [x] 会话菜单使用 top-layer Popover，并按 visual viewport 自动上下展开和夹紧；三个操作
  不依赖滚动会话列表才能完整显示；
- [x] 标题更新跨重启持久，Agent/Conversation 身份不变；
- [x] 自动标题失败不能阻止 Turn admission。

### UX-R02-13 — Message rendering and actions

- [x] assistant Markdown 只支持白名单结构，原始 HTML 始终作为文本；
- [x] URL 只允许安全 scheme，外链使用安全 `rel`，不加载远程图片或资源；
- [x] user/assistant 消息有可访问的复制反馈；
- [x] failed Turn 可重试；completed head 可创建明确分支后重新生成，旧位置不伪装成任意点分支；
- [x] branch 不修改源 transcript，不携带 failed/partial 内容，不越过 context/security admission。

### UX-R02-14 — Progressive technical disclosure

- [x] 默认界面不再直接暴露 `Provider/ToolSet/ctx/full` 等未解释术语；
- [x] 普通状态、错误和模型选择使用中文、动作导向文案；
- [x] 高级详情保留 model/profile、计数 basis、strategy、ToolSet digest 和 canonical event；
- [x] 高级详情按需读取、不缓存正文、不扩大 context reveal 权限；
- [x] 文档与 UI 明确“事件 envelope 不是原始 Provider 报文”。

## Execution evidence

2026-07-22 完成 `UX-R02` 资格收口：

- 资格收口后的四项定向修正（会话菜单 top layer、canonical/live terminal fence、
  conservative/chat-only 投影分层和长回答有界完成）相关回归合计 `226 passed`。真实
  `iollama:11434/qwen3.5:2b` 长回答得到 `12,286 / 12,288` UTF-8 bytes，截断 marker
  恰好一次，终态为 `run.completed.reason=max_output`，Provider 最终 usage 为
  `252 input / 2,357 output`。该证据只证明当前固定 `/api/chat` profile 与上述边界，
  不扩大成任意模型长度或吞吐保证。

- 全量确定性门禁为 `741 passed in 37.84s`；其中真实 Chromium 专项覆盖 360/390/768/
  1440 viewport、动态可视高度、IME/键盘、reduced motion、焦点/触控、SSE 断线续接，以及
  Agent/auth/Conversation/preparation/cancel/permission 的延迟响应和终态交错。前端、浏览器与
  UX 专项为 `72 passed`。物理手机软键盘不属于当前 GNU/Linux 发布基线，未据此扩张平台
  支持声明；浏览器回归直接执行生产 `app.js`，不是复制状态机的单元替身。
- `./governance.sh` 扫描 17 个 Markdown、11 个 shell、146 个文本文件并通过；JavaScript
  syntax、`git diff --check` 和 `AGENTS.md -> CLAUDE.md` 精确软链接同时通过。
- 当前代码在 `0.0.0.0:20815` 上使用真实 `iollama:11434/qwen3.5:2b` 和
  `landlock+seccomp` 完成同一 Conversation 四轮：终态全部为 `run.completed`，首帧分别为
  4614/273/292/458 ms。前两轮验证 warm history；“约 500/1500 词”请求分别在 9.002/
  29.744 秒结束，实际可见输出为 278/1063 个英文词，均为一次 Provider 调用、无 Tool loop。
  该外部模型输出长度不是产品保证；确定性 suite 另覆盖 500/1500 词上限、20–50 Turn、
  8K/16K/32K、自动压缩、summary/recovery 和 Provider 故障矩阵。
- `RR-QUA-20260722-02` 在显式 libc sync 计数模式下 PASS：4 completed、1 cancelled、1
  oversized reject、2 Conversation 创建后删除验证；Run entry 与 Worker PID residual 均为 0。
  Gateway 写入 1,568,768 bytes，12 次 `fsync` 全部成功；cache/temp 增长为 0，log 仅增长
  78 bytes，SQLite/WAL 语义增长约 1.50 MiB，全部低于门槛且没有逐 token/frame/timer 写入。
  资格仍明确不声称 SMART、物理介质 flush 或长期 SSD 寿命；这些是发布文档中的硬件限制。
- `RR-QUA-20260722-01` 在 workload 前因全量 pytest 保留的 symlink 测试树而 fail closed；随后
  只清理 checkout 内不可恢复的 disposable pytest/Chromium temp，再运行 `02`。最终 temp
  workload logical/allocated growth 为 0，peak 仅 781/4096 bytes，API 资源全部删除。
- normal stop、force stop、regular start 和 qualification-instrumented start 均通过 PID/
  process-group/20815 检查；一次真实 Conversation 在 Gateway normal restart 前后各完成一轮，
  第二轮复用持久历史，随后 Conversation/Run 全部删除。最终服务以普通模式重新发布在
  `0.0.0.0:20815`。

## Validation gates

完成声明至少需要：

```bash
source ./env.sh
./.venv/bin/python -m pytest
./governance.sh
./stop.sh
./start.sh
```

此外还要执行真实浏览器 viewport/IME/键盘检查、真实 `qwen3.5:2b` 多轮与失败恢复 smoke、
Gateway restart、normal/force stop、Worker/Run/temp 零残留检查。活动计划只在 14 项逐条获得
代码、行为测试和最终运行证据后才可删除。

---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-20
review_cycle: quarterly
---

# Runtime rebuild plan

## Objective

在不继承旧系统依赖和隐式状态的前提下，逐步把当前 Claude Code 风格 walking skeleton
发展为可维护、可恢复、可扩展的本地智能体运行时。每个阶段必须保持 P1-P8、明确的
单一 Agent loop、规范事件流、checkout containment 和 fail-closed sandbox。

本计划描述方向与验收，不承诺日期。逐项完成状态只使用下方 Execution ledger 定义的
状态机；上面的 Implemented baseline 只是带日期的现状说明，不是第二份状态账本。

## Implemented baseline

- 根级 bootstrap/start/stop/purge/governance 生命周期与 checkout-local 环境边界；
- `0.0.0.0:20815` 认证 Web UI、CSRF、Conversation create/list/read/delete、同会话
  多轮 Run/cancel、SSE 和完整事件详情；
- 有界 `QueryEngineRegistry` 与每 Conversation 一个逻辑 `QueryEngine`；Engine 固定
  Agent/Conversation 身份，提供 restore/submit/interrupt/delete，返回不可变 Run handle，
  删除后 retire/驱逐，重启后从 SQLite 懒重建且不复制 durable 或 per-Run 状态；
- 固定 demo Agent Capsule、Agent 专属 `worker-env`、每 Run 独立目录和进程组；
- 单一 `HarnessKernel` context → model → tool → model loop，无 LangGraph/LangChain；
- 受信 Ollama Broker，固定 `iollama:11434/qwen3.5:2b` 和 per-Run transcript；启动时从
  `/api/show` 资格检查原生上下文窗口，并应用受信 operational cap/output reserve；
- Agent-scoped `state.sqlite` 中的 Conversation/Turn repository；一个 Conversation 同时
  最多一个 active Run，Turn 接受/终态与 boundary event 事务提交，Gateway 重启将遗留
  `running` Turn 收敛为 `interrupted`；
- Control Plane 从仅含 completed user/assistant pairs 的 durable history 编译不可变、
  带 provenance/digest/budget 的 ContextPlan；完整 plan 不进入 Worker，Model Broker
  IPC v2 只传摘要引用；
- 共享不可变 `ToolSpec` 同时生成 provider schema、Worker/Control Plane 校验和
  toolset digest；
- 当前模型画像为 native `262144`、operational `32768`、output reserve `2048`、硬输入
  `30720`，并已生成 80% trigger / 60% target 的动态压缩策略；
- admission estimator 是 `utf8-bytes-upper-bound-v1`，没有模型 tokenizer 时每个 UTF-8
  byte 按一个 token 做保守上界，并计入实际 renderer、Tool manifest 与 `256` tokens
  provider template reserve；初始 plan 和每个后续完整 transcript 都执行相同 admission；
  超过动态 80% trigger 时从最旧端按完整 completed Turn pair 做 tail-window，直到不高于
  60% target 或没有可移出的 pair，所有可省略 pair 移出后仍超硬预算则 fail closed；
- 所有 terminal 都附带 `{input_tokens,output_tokens,last_input_tokens,complete}`；provider
  报告 usage，Control Plane 按模型 profile 校验并累计，Worker 不可伪造；
- Model Broker 最多同时执行 2 路 provider stream，其它 active Run 在总容量内有界排队，
  30 秒无 slot 时以可重试 `model_busy` 失败；
- 每轮最多 4096 个 provider 原始帧，合并为最多 128 个 content IPC frame；Broker error
  code/retryability 由 Control Plane 绑定 canonical terminal；
- Worker 无网络，Landlock + seccomp + rlimits + attestation，无 unconfined fallback；
- 仅有界、只读、输入/结果各 `8192` UTF-8 bytes 的 one-shot `builtin/echo`；结果回流后
  provider ToolSet 收窄为空；
- canonical event sequencing、durable semantic SQLite/WAL、ephemeral delta、取消和唯一终态；
- 新 Run 以 feature marker 声明每次真实 provider 调用严格配对的
  `model.request.started`/`model.response.finished`；runtime admission、usage ledger 与边界
  原子对账，不保存 prompt 或逐 token/provider frame；
- Web 右侧按 durable Turn 展示多轮 user/assistant transcript，左侧把所选 Run 按
  User/Harness/LLM/Tool 四泳道与 canonical `seq` 回放；Turn/Run/node 双向定位，支持过滤、
  步进/播放和常驻节点检查。推导的 User → Harness 提交节点不冒充 EventEnvelope；详情
  分开显示逻辑消息、payload 和完整 canonical envelope。按需 context inspector 只返回
  `no-store`、正文隐藏的 ContextPlan/section 元数据或经验证历史摘要；
- bounded Run/event/log/journal/tree 资源与安全的 PID/process-group 清理；
- 旧系统隔离到 `_legacy-reference/`，当前 runtime 不依赖它；
- P1-P8 权威文档和自动文档/边界治理。

“implemented baseline”只表示当前固定路径已跑通，不能解读为整个产品生产就绪。

## Current validation evidence

2026-07-18 在当前 `x86_64` checkout 完成了受控环境全量测试、文档治理、diff/脚本语法
门禁和真实 lifecycle 复验：Gateway 在 `0.0.0.0:20815` 以真实
`iollama:11434/qwen3.5:2b`、Landlock + seccomp 资格启动；同一 Conversation 的前两轮
用依赖历史的暗号问答验证 completed history，重启 Gateway 后第三轮仍从恢复的 transcript
得到相同答案。另一个已完成 Run 的会话删除后，session detail 与 event endpoint 都返回
404；运行结束无 Worker PID 文件或 Run 根残留，Gateway 保持健康。
同日引入逻辑 QueryEngine 后再次完成受管 stop/start、全量 `200 passed` 和真实两轮 API
验收：同一 Engine/Conversation 产生两个独立 Run，第二个 `run.started` 报告纳入前一轮
2 条 completed history messages，两轮均以 `run.completed` 收敛；删除后 Conversation 和
两个 event endpoint 均为 404。自动集成测试还覆盖 Registry/Service 关闭后从同一 SQLite
懒创建新 Engine，并把重启前 completed pair 投影进下一轮。

以上 2026-07-18 证据不是 cold-checkout、多架构或 production qualification：当时依赖
缓存已存在，也没有长期 load/soak、SSD SMART 对照、故障注入矩阵或 TLS/多用户边界。

2026-07-19 用最终候选源码新建
`.runtime/qualification/cold/RR-COLD-20260719-02/checkout`；复制前确认其中不存在
`.tools/`、`.venv/`、`.runtime/` 或 `data/`，随后从网络下载固定 `uv 0.11.7`、受管
CPython 3.11.15 和 frozen 依赖，所有产物均落在该 cold stage。该 stage 的
`RR-QUA-20260719-17` 以 `qwen3.5:2b` 和 Landlock + seccomp 完成 16 个真实 Turn、1 个
cancelled Run、1 个 admission reject 和 2 个 Conversation 删除；总耗时 13.479 秒，
终态 Run root、Worker PID record 和 API 资源残留均为 0。workload 中实际 Gateway
`write_bytes/syscw` 增量为 `2,842,624/1,593`，state logical/allocated growth 为
`2,743,920/2,744,320` bytes，WAL logical peak 为 `2,859,312` bytes，log growth 为
78 bytes，temp peak 为 `1,038/4,096` logical/allocated bytes，cache growth 为 0；19 个
插桩进程镜像全部登记，workload 内只观察到 34 次成功的 preloaded-libc `fsync` symbol
call。正常 `stop.sh` 后完整插桩生命周期共 51 次成功 `fsync`，其它五类 symbol 为 0，
没有 slot overflow 或 registration failure。

`RR-QUA-20260719-11..16` 另保留为 final-candidate 之前的三组 instrumented/plain 4-Turn
A/B 基线：每轮均为 4 completed、1 cancelled、1 rejected、2 deleted、零残留；插桩组/
普通组 duration median 分别为 3.379/3.398 秒，Gateway `write_bytes` median 均为
1,036,288 bytes，state logical growth median 均为 1,009,432 bytes。三轮插桩 workload
各观察到 12 次成功的 preloaded-libc `fsync` symbol call，普通轮不观察精确 sync-call。
这些应用层数据不观察 direct syscall、kernel/block flush 或 SSD/NAND 写入，也没有
SMART 对照，不能解释成物理磨损测量。

同日 recovery final candidate 完成严格 replay/snapshot/outer-metadata 复审、真实
SIGKILL 与尾部 SQL fault、不可写 journal、SSE gap/delete race/restart 唯一 terminal
负面矩阵；该 recovery 候选当时全量为 `317 passed`，`./governance.sh` 扫描 12 个
Markdown、8 个 shell script 和 81 个文本文件后通过。这关闭 REC-01/GATE-02，但不表示
operation ledger 能为
未来任意副作用提供 exactly-once。

最终发布 checkout 的普通模式 `RR-QUA-20260719-20` 再次 PASS：4 completed、1 cancelled、
1 rejected、2 deleted、零残留，真实模型与沙箱仍为 `qwen3.5:2b` / Landlock + seccomp，
Gateway 维持 `0.0.0.0:20815` 健康。此前 `RR-QUA-20260719-18/19` 在 workload 前分别因
pytest 和浏览器遗留在 `.runtime/tmp` 的 symlink fail closed；没有 Run 或 API side effect，
相关可再生临时树被可恢复地移到 `.runtime/test-results/qualification-quarantine/` 后，
RR20 才执行成功。失败记录保留且不从证据链中隐藏。当前仍没有原生 `aarch64`、长期
failure/restart soak、SSD SMART、TLS 或多用户隔离证据。

同日完成 single-operator 本地 token rotation 纵向验证：隐藏 TTY 双输入不回显，受管
Gateway stop 后以 private temp + file fsync + atomic replace + parent fsync 轮换，再次
stop 收敛并重启；secret 保持 owner `0600`/single-link，Web login/logout 为 `200/204`，
token 未出现在受管进程 argv/environ 或 Gateway log。默认创建仍为 256-bit 随机值；该
证据不关闭 production 无中断轮换、多用户凭据治理或弱运维口令风险。

同日完成 model-call boundary、全 Turn/Run 时间线和 authenticated context inspection 的
开发门禁：合并定向测试 `278 passed`，完整测试 `348 passed`，`./governance.sh` 扫描
12 个 Markdown、8 个 shell script 和 80 个文本文件后通过，Python/JavaScript 语法与
`git diff --check` 通过。事务内 SIGKILL 会让 usage/response boundary 整体回滚；事务已
提交但 terminal 尚未提交时重启会保留 exact usage，并以唯一失败终态收敛；open request
则保持 incomplete。受管 Gateway 随后在 `0.0.0.0:20815` 以真实
`qwen3.5:2b` 和 `landlock+seccomp` 启动。认证开发 smoke 留下题为
“模型边界与多轮时间线验收”的两轮 Conversation：每个 Run 均有两组严格配对的
request/response，第二个 Run 的 `run.started` 显示历史 `2/2/0`（完整/纳入/省略），
ContextPlan section role 为 system/system/user/assistant/user，replay 为 `run-ui-v2`；context
inspection 为 `exact`、`no-store`、正文隐藏且正文提升 query 返回 `400`。终态无 Worker PID
record 或 Run root 残留，Gateway 保持健康。该开发 smoke 不是新的 platform/SSD 资格 RR，
也不关闭 LOOP-01 或 CTX-02 的剩余验收。

2026-07-20 完成双栏事件回放 UI 的发布态复验：右侧 Conversation 按 Turn 分组，左侧
使用 User/Harness/LLM/Tool 四泳道、独立 Replay control、常驻 Inspector、过滤和有界
播放游标；active Run 与历史 Run 使用分仓内存投影，后台 SSE 不会污染正在查看的历史。
定向 frontend/web 为 `65 passed`，全量为 `355 passed`，JavaScript/diff 门禁与治理检查
通过。受管 Gateway 重启后仍监听 `0.0.0.0:20815`，健康检查报告真实
`qwen3.5:2b` 与 `landlock+seccomp`。认证 smoke 新建“双栏时序回放验收”两轮
Conversation，两轮均以 `run.completed` 收敛，durable replay 分别为 11/6 个有序事件；
第二轮 ContextPlan 纳入首轮 `2/2/0`（完整/纳入/省略）历史消息并正确回答依赖首轮的
暗号问题，context inspection 为 `exact`、`no-store`。终态无生产 Worker PID record 或
Run root 残留；该 smoke 不是新的 platform/SSD qualification RR。

## Phase 1 — Clean-root qualification

目标：让清洁后的根目录成为唯一可运行系统，并用可重复证据封住旧/新边界。

- 冷 checkout bootstrap/start/health/real Run/stop smoke；
- lifecycle 并发、启动回滚、stale/伪造 PID、orphan Run 负面测试；
- 验证所有 cache/temp/home/data/process 路径留在 checkout；
- 验证 `_legacy-reference/` 与 Claude Code materials 不进入 import/build/test/runtime 图；
- 在受支持 `x86_64` 和 `aarch64` 主机分别完成资格记录；
- 建立基础 load/soak 与磁盘写入基线，记录而非推测 SSD 影响。

完成标准：全门禁通过，真实模型 Run 无重大运行错误，停止后无受管进程/Run 残留；
多架构未验证前仍保留平台限制声明。

## Phase 2 — Durable conversations and replay

目标：从“有 durable events”升级为“可验证恢复的 conversation runtime”。

范围包括 durable cursor availability、gap/replay/live 交界、幂等 snapshot/projector、
journal prune/retention、WAL 不可写或损坏、bounded usage ledger，以及未来副作用的
intent/outcome 恢复协议。canonical transcript 和 durable semantic events 保持权威；
ephemeral delta 丢失必须明确呈现，不能伪装成完整重放。

逐项状态只在 REC-01、CMP-02 和 GATE-02 维护；现有能力与验证证据只在本计划顶部
Implemented baseline / Current validation evidence 记录。

## Phase 3 — General Agent Capsule lifecycle

目标：安全提供 create/list/upgrade/delete，而不破坏 Agent 隔离。

- 引入持久 Agent registry 和 generation/version 状态机；
- provisioning 只使用 project-local、allowlisted、binary-only 依赖；
- Agent instructions/context policy 作为版本化 Capsule 内容；
- upgrade 有 staging、资格检查、原子切换和 rollback；
- delete 实现 draining → process proof → data/runtime/env cleanup → residual audit；
- 中断/崩溃后 create/upgrade/delete 均能幂等恢复；
- 多 Agent 并发与交叉污染负面测试。

完成标准：[Agent Capsule delete contract](../design/agent-capsule.md#通用-agent-删除尚未实现的契约)
全部满足，删除一个 Agent 不改变其它 Agent 的进程、文件或状态。

## Phase 4 — Capability and permission brokers

目标：逐项增加 Claude Code 风格实用能力，同时不扩大 Worker 的直接权限。

推荐顺序：只读文件元数据/读取 → bounded search → atomic edit/write → controlled command
execution → MCP/Skill。每项都需要：

- 稳定 Tool ID 和严格 request/result schema；
- 用户/Agent/Run capability policy 与必要的交互授权；
- 路径 containment、输入/输出/时间/并发/进程/磁盘限制；
- cancellation、terminal convergence、审计和 secret redaction；
- 独立 broker 或更细子沙箱；不向主 Worker 开放 socket/fork/exec/任意写；
- archive、URL、subprocess、MCP transport 的专项负面测试。

Skill 与 stdio MCP 是代码执行，默认禁用。kernel 能力不足时 fail closed，不提供开发
环境 unconfined fallback。

## Phase 5 — Context and instruction system

目标：让 `ContextCompiler` 真正拥有 provider-agnostic model view。

范围包括分层 Prompt section、可审计 context inspection、Capsule workspace instructions、
有界 Git/date/env、Tool-result projection、durable compaction boundary、semantic summary
和单次 overflow recovery。模型视图与 canonical transcript 分离，所有预算和压缩策略
从受信 ModelProfile/ContextPolicy 推导。

逐项状态只在 CTX-*、CMP-* 和 GATE-05 维护；最终 section、event、storage 和安全契约
分别写入 architecture、event protocol 和 SECURITY。

## Phase 6 — Tasks and sub-agents

目标：在已有 Agent/Run identity 上增加显式 parent-child orchestration，而不是嵌套隐式
graph。

- `parent_run_id`、Task 状态、mailbox 和结果回收协议；
- 每个子 Agent 使用自己的 Capsule/环境/沙箱，父 Run 只持有有界 capability；
- 全局/每 Agent/每父 Run 并发、深度、预算和 wall deadline；
- 取消传播、孤儿处理、循环检测和唯一终态；
- Web 展示 parent/child timeline 和独立事件详情。

完成标准：父/子故障、取消和重启测试无 orphan process/state；一个 Agent 无法读取或
修改另一个 Capsule，除非有显式 brokered message capability。

## Phase 7 — Production qualification

目标：在功能边界稳定后再决定受支持部署形态。

- TLS/可信 reverse proxy、多用户/租户边界和 production token lifecycle；当前仅有
  single-operator 的本地 stop/atomic-rotate/restart，不等同于无中断轮换或多用户凭据治理；
- Control Plane 与 Web BFF 是否拆进程，以故障与权限分析决定，不为“微服务化”而拆；
- bounded/redacted tracing、metrics、audit 和 retention；
- load、soak、chaos、upgrade/rollback、backup/restore 与磁盘磨损测试；
- dependency/SBOM/vulnerability、release artifact、cold-checkout 和运维 runbook；
- 明确支持矩阵和 capacity envelope。

只有所有 release gates 都有可重复证据，才能修改“walking skeleton / not production
ready”的声明。

## Execution ledger — Claude Code 核心机制演进

本节是本计划中唯一的逐项状态账本。上面的 Phase 说明目标和边界；这里用稳定 ID
记录依赖、退出门槛和验收证据。后续实现不得另建一份长期总路线复制这些状态。只有当
某一项跨多个变更、包含独立迁移或故障矩阵时，才临时建立窄执行子计划；子计划完成后
把最终契约写回对应 design/security 文档，并在这里记录证据和状态。

### 研究输入映射

下列文章只提供设计心智模型，不是本项目的需求规范或安全证明。实现判断以 P1-P8、
本计划、[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和本地
[Claude Code 2.1.88 来源说明](../../references/claude-code/PROVENANCE.md)为准。
外部文章链接便于回看研究上下文，但 governance 的本地链接检查不保证其长期可用；
可复现判断必须落到上述 checkout-local 权威资料、设计契约和测试证据。

| 研究主题 | 主要启发 | 对应 work item |
| --- | --- | --- |
| [QueryEngine](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc5) | Conversation-scoped 编排、跨 Turn 边界、模型/Tool 闭环 | QE-01、REC-01、LOOP-01 |
| [提示词工程](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc18) | 分层 Prompt、角色和 Tool prompt、运行时装配 | CTX-01、CTX-02、CTX-03 |
| [Tool 系统](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc6) | 统一契约、动态有效工具集、执行上下文和权限 | TOOL-01、PERM-01 |
| [Slash Commands](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc7) | 人类显式控制面与模型 Tool 分离 | CMD-01 |
| [项目上下文](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc8) | Git、CLAUDE.md 和高信号工程上下文 | CTX-03、CTX-04 |
| [上下文压缩](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc8b) | 结果裁剪、projection、summary、reactive recovery | CMP-01 至 CMP-05 |
| [文件链路](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc13) | Read/Search/Edit/Write 风险分层和 diff | READ-01、SEARCH-01、WRITE-01 |
| [Bash 子系统](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc14) | 命令解析、权限、sandbox、Task、取消和回流 | EXEC-01、TASK-01、EXEC-02 |

不得照搬参考实现中的固定压缩阈值、可关闭 sandbox、symlink 跟随、atomic 失败后原地
写入等取舍。当前 Worker 的无网络、无子进程和无任意写边界继续保持。

### 账本维护规则

- 状态只有：`not_started`、`in_progress`、`blocked`、`done`、`deferred`、`superseded`。
- 开始实现时先把该项改为 `in_progress`；方向、依赖或范围变化必须同步更新本计划。
- 只有该项所有 checkbox、全局 DoD 和要求的真实 lifecycle 证据都满足，才能标记
  `done`。代码已合入但故障、文档或清理证据缺失时仍不是完成。
- `blocked` 必须在证据栏记录具体阻塞条件和解除条件；不能用它代替未开始。
- `deferred` 表示明确移出当前 release scope，并记录重审条件；`superseded` 只用于已由
  新稳定 ID 完整取代的旧项，证据栏必须列出 replacement IDs，二者都不是 `done`。
- 证据记录测试文件、命令结果摘要、真实模型纵向验收、故障注入和权威文档链接；
  不提交 runtime 日志、token、会话 cookie、绝对秘密或不稳定 Run ID。
- 每项的数值资源上限应在实现时写入对应权威设计/安全文档；本账本只追踪是否完成，
  不复制会漂移的常量。
- ID 永不复用；拆分时把原 ID 标为 `superseded` 并记录 replacement。后续治理脚本应检查 ID 唯一、
  dependency 存在且无环、状态枚举合法、done 有完整证据、blocked 有解除条件；自动
  检查落地前由每次 plan review 人工执行。
- 本账本建立前已经完成的 baseline 项，可用顶部带日期的 Current validation evidence、
  实现和自动测试链接作为初始 `done` 证据；下一次实质修改该项时补齐 RR 记录和发布 SHA。

真实 lifecycle、故障或资源验收使用稳定记录 ID，格式为：

    RR-VER-YYYYMMDD-NN
    implementation_ref: <full commit sha; pre-publication validation may say worktree>
    work_items: <stable IDs>
    platform: <arch/kernel/sandbox/model qualification>
    commands: <reproducible entrypoints>
    result: <pass/fail and bounded metrics>
    residual_audit: <none or explicit findings>

### 总状态

| 顺序 | ID | Priority | 目标 | depends_on | 状态 |
| ---: | --- | --- | --- | --- | --- |
| 1 | QE-01 | baseline | Conversation-scoped 逻辑 QueryEngine | — | done |
| 2 | QUA-01 | P0/parallel | clean-root、多架构、SSD 与 lifecycle 资格 | — | in_progress |
| 3 | REC-01 | P0 | durable replay、gap、snapshot 与副作用恢复协议 | — | done |
| 4 | AGT-01 | P0 | 通用 Agent Capsule create/upgrade/delete | REC-01 | not_started |
| 5 | LOOP-01 | P0 | 有界多步 model → Tool → model loop substrate | REC-01, AGT-01 | in_progress |
| 6 | CTX-01 | P0/parallel | Prompt section registry | — | not_started |
| 7 | CTX-02 | P0/parallel | authenticated context inspection | CTX-01 | in_progress |
| 8 | CTX-03 | P0 | workspace CLAUDE.md | CTX-01, AGT-01 | not_started |
| 9 | CTX-04 | P0 | bounded Git/date/environment context | CTX-01, AGT-01 | not_started |
| 10 | TOOL-01 | P0 | Tool contract v2 与 EffectiveToolSet | LOOP-01, AGT-01 | not_started |
| 11 | PERM-01 | P0 | Capability Broker 与 durable permission 状态机 | REC-01, AGT-01, TOOL-01 | not_started |
| 12 | CMP-01 | P0 | Tool-result budget 与 micro-compaction | CTX-01, TOOL-01 | not_started |
| 13 | CMP-02 | P0 | durable projection boundary 与 snapshot | REC-01, CTX-01, CMP-01 | not_started |
| 14 | READ-01 | P1 | descriptor-anchored file metadata/read | AGT-01, PERM-01, CMP-01 | not_started |
| 15 | SEARCH-01 | P1 | bounded Glob/Grep | READ-01 | not_started |
| 16 | CMP-03 | P1 | deterministic context-collapse projection | CMP-02 | not_started |
| 17 | CMP-04 | P1 | semantic summary 与 autocompact | CMP-03, MODEL-01, READ-01, SEARCH-01 | not_started |
| 18 | CMP-05 | P1 | one-shot reactive overflow recovery | CMP-04 | not_started |
| 19 | WRITE-01 | P1 | diff-bound atomic Edit/Write | READ-01, PERM-01 | not_started |
| 20 | EXEC-01 | P1 | allowlisted `shell=False` argv runner | AGT-01, PERM-01, CMP-01 | not_started |
| 21 | MODEL-01 | P1 | trusted ModelCatalog 与动态模型切换 | LOOP-01, CTX-01 | not_started |
| 22 | CMD-01 | P1 | Slash Command/Web 显式控制面 | CTX-02, PERM-01, CMP-04, MODEL-01 | not_started |
| 23 | TASK-01 | P2 | durable background Task substrate | REC-01, AGT-01, EXEC-01 | not_started |
| 24 | EXEC-02 | P2 | bounded foreground Bash 与可选后台执行 | EXEC-01, TASK-01 | not_started |
| 25 | EXT-01 | P2 | MCP/LSP 通过同一 capability 边界 | TOOL-01, PERM-01, EXEC-01 | not_started |
| 26 | SKILL-01 | P2 | versioned Skill registry 与隔离执行 | AGT-01, TOOL-01, PERM-01, EXEC-01 | not_started |
| 27 | SUB-01 | P2 | Task/mailbox 驱动的子智能体 | REC-01, AGT-01, PERM-01, TASK-01 | not_started |
| 28 | REL-01 | release | 首个受支持版本资格与运维契约 | QUA-01 | not_started |

### Phase exit gates

Gate 只在依赖 work item 均为 done 且验收证据可重复时关闭。Gate 状态不替代上面的
work item 状态。

| Gate | 当前状态 | 退出条件 |
| --- | --- | --- |
| GATE-01 clean-root | in_progress | QUA-01 全部完成；双架构 cold checkout、真实 Run、完整停止、containment、soak 和磁盘基线有证据 |
| GATE-02 recovery | done | REC-01 完成；所有支持的崩溃点恢复到最后 durable boundary，客户端能区分 replay/gap/live |
| GATE-03 Agent lifecycle | not_started | AGT-01 完成；create/upgrade/delete 可恢复、可回滚、无跨 Agent 影响或残留 |
| GATE-04 capability/read | not_started | LOOP-01、TOOL-01、PERM-01、CMP-01、READ-01、SEARCH-01 完成，Worker 权限未扩大 |
| GATE-05 context | not_started | CTX-01 至 CTX-04、CMP-02 至 CMP-05、MODEL-01 完成；projection 可复现、summary 可恢复且 canonical transcript 不变 |
| GATE-06 tasks/sub-agents | not_started | TASK-01、SUB-01 和纳入范围的执行能力完成；取消/崩溃/重启/删除无 orphan |
| GATE-07 release | not_started | GATE-01 至 GATE-06 和 REL-01 完成；首发 scope 的平台、容量、安全、备份恢复和运维证据通过 |

依赖只决定“可以完成”的顺序，不禁止安全设计、评测和测试脚手架并行；唯一推进顺序
由总状态表中的顺序与 `depends_on` 共同确定。

### 所有 work item 的统一完成门槛

每一项在标记 `done` 前都必须满足：

1. 权威 contract/API/event schema、状态所有者、禁止拥有项、迁移/回滚或恢复行为已定义；
2. 输入、输出、时间、并发、内存、进程、文件、磁盘和 retention 上限有明确数值与
   fail-closed 语义；
3. cancel、deadline、Gateway/Worker/executor 崩溃和 partial side effect 的收敛行为已测；
4. 相关负面矩阵通过，normal/force stop 和 Agent delete residual audit 无残留；
5. 磁盘写放大有实测，增长只与新增语义数据近似线性，不逐 token/chunk/line flush；
6. 最小测试、完整 `pytest`、`./governance.sh` 和受影响时当前受支持真实模型的
   纵向流程通过；
7. architecture/security/event/Capsule/README/CLAUDE 中受影响的权威文档与本计划同改；
8. 未验证平台和 production 声明没有被扩大。

所有有外部副作用的 capability 还必须遵守统一恢复原则：

```text
durable intent → at-most-once automatic dispatch → durable outcome | outcome_unknown
```

本地 journal 不能证明 exactly-once。dispatch 前必须持久化 executor/runner/cgroup 等
可核验身份，并在适用时通过受信 start handshake 释放执行；intent 与 outcome 之间崩溃
标记 `outcome_unknown`，它既可能“未执行”也可能“已执行但未记录”，重启后不得自动
重放。只有能用幂等键和外部事实证明安全的专用操作，才可设计显式 reconcile；模型
重试、SSE 重连或普通 Run 恢复本身永远不是重复执行授权。

### QE-01 — 逻辑 QueryEngine 基线

Authority：[架构](../design/architecture.md)和本计划 Implemented baseline。

- [x] [架构中的 QueryEngine contract](../design/architecture.md)已实现：Conversation-scoped
  canonicalization、窄 ownership、生命周期和禁止拥有项保持单一权威。
- [x] 身份串线、取消/删除竞态、关闭、删除后 handle 失效和重启懒恢复有自动测试。
- [x] 当前真实 lifecycle 复验覆盖同 Engine 多 Turn、重启恢复和删除收敛。

证据：[实现](../../src/agent_builder_v2/query_engine.py)、
[测试](../../tests/test_query_engine.py)和 Current validation evidence。

### QUA-01 — Clean-root、平台和 SSD 资格

Authority：[P1-P8](../PRINCIPLES.md)、[README](../../README.md)和
[安全边界](../../SECURITY.md)。

- [x] 当前 `x86_64` checkout 完成受管 start/health/真实 Run/stop 基础复验。
- [x] 空依赖缓存 cold-checkout 完成 bootstrap/start/Run/stop，并证明受管路径全部留在
  cold checkout；运行时没有项目级 checkout 外写入。
- [x] lifecycle 并发、启动回滚、stale/伪造 PID、orphan 和强制停止矩阵通过。
- [ ] `x86_64` 与 `aarch64` 分别留下 host/kernel/model qualification 证据。
- [ ] 建立代表性 N-turn、取消、失败、重启、删除 workload 的 load/soak 和磁盘增长基线。
- [x] 记录 logical/allocated bytes、WAL/log/cache/temp 峰值、fsync 次数和终态残留上限。

证据：Current validation evidence、[qualification contract](../design/qualification.md)、
[lifecycle identity tests](../../tests/test_lifecycle_identity.py)和
[qualification tests](../../tests/test_qualification.py)。QUA-01 因原生 `aarch64` 与完整
failure/restart soak/SMART 基线仍保持 `in_progress`；不得用应用层计数替代物理设备证据。

### REC-01 — Durable replay、snapshot 与恢复语义

Authority：[event protocol](../design/event-protocol.md)和[架构](../design/architecture.md)。

- [x] 定义旧 Run durable replay API、oldest/latest cursor、availability 和明确 gap marker。
- [x] 定义有版本的 snapshot 与幂等 UI projection；ephemeral delta 丢失不伪装成完整回放。
- [x] 对 journal prune、不可写、损坏和每个事务/发布崩溃点建立可验证失败行为。
- [x] 建立通用 operation intent/outcome ledger、幂等 identity 和 `outcome_unknown` 语义。
- [x] context/token/cost usage 的记录、redaction、retention 和查询均有硬上限。
- [x] 重启、SSE 重连和重复请求不会重复 terminal、权限决定或未来文件/进程副作用。

证据：[event protocol](../design/event-protocol.md)、[strict replay/projector](../../src/agent_builder_v2/replay.py)、
[journal](../../src/agent_builder_v2/state.py)、[conversation recovery](../../src/agent_builder_v2/sessions.py)、
[replay tests](../../tests/test_replay.py)、[journal tests](../../tests/test_event_journal.py)、
[session crash tests](../../tests/test_sessions.py)、[Control Plane failure tests](../../tests/test_control.py)
和 Current validation evidence。operation ledger 是未来副作用 capability 的恢复 substrate；
当前只读 echo 不产生 privileged side effect，本项不宣称 exactly-once。

### AGT-01 — 通用 Agent Capsule 生命周期

Authority：[Agent Capsule design](../design/agent-capsule.md)。

- [ ] 持久 Agent registry 和 generation/version 状态机支持 create/list/get/upgrade/delete。
- [ ] provisioning 仅使用 checkout-local、allowlisted、binary-only 依赖和 Agent 专属环境。
- [ ] upgrade 使用 staging、资格检查、原子 promotion 和 rollback；旧 generation capability 失效。
- [ ] delete 实现 draining → process proof → data/runtime/env cleanup → residual audit。
- [ ] create/upgrade/delete 在每个 staging、rename、registry commit 崩溃点可幂等收敛。
- [ ] 删除清理 Engine、Conversation、approval、Task、queue、env/cache/log/WAL/SHM/lock
  等完整资产清单，且其它 Agent 的目录、进程和 journal 不变。

证据：_待补。_

### LOOP-01 — 有界多步 Agent loop substrate

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [ ] 每次 submit 固化不可变 TurnRuntimeSnapshot：Agent generation、model profile、当前
  canonical tool manifest/digest、ContextPlan、max turns/tool calls、usage budget 和 deadline。
- [ ] 将当前 one-shot 路径提炼为通用、有界、顺序执行的多步状态机；动态 EffectiveToolSet
  与 permission wait 由 TOOL-01/PERM-01 在此 substrate 上启用，不反向成为本项依赖。
- [ ] 多 Tool call 保持 call/result identity；基础 loop 一律顺序执行，未来只有经
  TOOL-01 明确声明 concurrency-safe 的工具才可并行。
- [ ] 每次 provider call 都重新做完整 transcript admission，并校验 usage 和 terminal。
- [ ] model、现有 bounded Tool、cancel、deadline 和 provider failure 均收敛到唯一终态。
- [ ] 真实模型完成至少两次 Tool 决策再回答；没有第二套 graph、loop 或隐藏持久状态。

阶段证据：当前固定 Echo loop 已为每次真实 provider call 增加 exact-request observer、连续
iteration、runtime admission 估算及严格配对的 request/response durable boundary；错误、
取消、Gateway recovery 和 replay tamper 负面测试已覆盖。通用 TurnRuntimeSnapshot、动态
EffectiveToolSet、多 Tool 顺序执行和真实两次 Tool 决策仍未完成，因此本项保持
`in_progress`。

### CTX-01 — Prompt section registry

Authority：[架构](../design/architecture.md)和[安全边界](../../SECURITY.md)。

- [ ] 建立稳定有序的 Prompt section provider registry，记录 trust、provenance、依赖
  digest、独立 budget、cache scope、truncation 和 renderer version。
- [ ] 平台安全 contract 永不可被 browser、Agent、workspace 或 Worker replace。
- [ ] 受信平台/Agent/Tool contract 位于动态 workspace/session/Git/history section 之前；
  受信 instruction 保持独立 role/section，绝不伪装成 synthetic user content。
- [ ] 相同 source/history/profile/generation/policy 产生字节一致的 plan 和 digest；
  单一依赖变化只失效相关 section。
- [ ] section cache 仅为有界内存优化；当前 Ollama 不支持的 cache API 不用伪 marker 模拟。

证据：_待补。_

### CTX-02 — Authenticated context inspection

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[event protocol](../design/event-protocol.md)。

- [ ] 提供 typed、authenticated context inspect，展示 section identity/source/digest、
  bytes/token estimate、history projection、budget 和 truncation reason。
- [ ] 默认不回显正文、secret、hidden prompt 或可用于重建它们的高精度片段；诊断提升
  必须走独立受信 operator policy、审计和有界 redaction。
- [ ] inspect projection 与 `run.started` metadata 和实际 provider request digest 可对账，
  但浏览器不能提交或覆盖任何 provider-bound section。
- [ ] active Run、历史 Run、restart、deleted Agent 和 foreign Conversation 的授权、404/
  redaction 语义有负面测试，查询与响应大小有硬上限。

阶段证据：已提供认证、`no-store`、默认正文隐藏的 `/api/runs/<run-id>/context` 和按需 Web
dialog；驻留 Run 返回现场重验的精确 section 元数据，重启/retention 后只返回严格 replay
验证的 summary，正文提升 query 被拒绝。独立 operator 提升策略/审计、通用 Agent 授权和
CTX-01 registry 收敛仍未完成，因此本项保持 `in_progress`。

### CTX-03 — Workspace CLAUDE.md

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] 首版只从当前 Agent Capsule 内精确的 `workspace/CLAUDE.md` 加载，不向上遍历、
  不读取 HOME、不支持 checkout 外 include。
- [ ] regular-file、no-follow、containment、owner/mode、UTF-8、硬字节上限和 stable-read
  digest 全部 fail closed；缺文件是确定性 no-op。
- [ ] workspace instruction 只能形成独立 trust section，不能伪装 platform/system contract。
- [ ] oversize、非 UTF-8、symlink/hardlink、rename race、跨 Agent 和删除中 Capsule
  都不会接受 Run 或泄露其它路径。

证据：_待补。_

### CTX-04 — Bounded Git/date/environment context

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] Git collector 使用固定 executable identity、固定 Capsule workspace cwd、clean env、
  禁用 optional lock/pager/hooks/user config，并限制子命令、时间、输出和提交数。
- [ ] date/timezone 与环境 section 只来自受信 Control Plane allowlist；不复制继承环境、
  secret、host path 或高基数机器信息。
- [ ] 每个 section 记录 source digest/provenance；Git、branch、commit message 和文件名
  一律按 untrusted project data 渲染，不得提升为 instruction。
- [ ] non-repository、detached/unborn、恶意 config/ref/message、超时、输出洪泛、rename/
  delete race 和跨 Agent 路径均确定性收敛且不扩大 Run 权限。

证据：_待补。_

### TOOL-01 — Tool contract v2 与 EffectiveToolSet

Authority：[架构](../design/architecture.md)、[event protocol](../design/event-protocol.md)
和[安全边界](../../SECURITY.md)。

- [ ] 扩展受控 schema vocabulary、结构化 result/progress、风险、只读性、并发、超时、
  cancellation、结果 trust/source 和版本语义。
- [ ] 明确 ToolCatalog → Policy Resolver → EffectiveToolSet → Broker/Executor 边界。
- [ ] provider 暴露、ContextPlan、Worker 校验和 executor 使用同一 canonical manifest/digest。
- [ ] 工具在模型暴露前按 policy 过滤，但每次调用仍重新校验 identity、schema、语义和权限。
- [ ] unknown/duplicate tool/provider、contract drift、foreign/replayed/out-of-order result、
  oversize frame 和调用次数超限全部拒绝。
- [ ] ToolUseContext 不成为跨信任边界的 ambient authority；只传窄 capability/reference。

证据：_待补。_

### PERM-01 — Capability Broker 与 permission 状态机

Authority：[安全边界](../../SECURITY.md)和[event protocol](../design/event-protocol.md)。

- [ ] 文件、runner、MCP 和 Skill 等 privileged work 由 Control Plane/独立 executor
  执行，主 Worker 仍无 socket/fork/exec/任意写。
- [ ] capability 绑定 agent_id、generation、conversation/run/call、toolset/policy/args/
  preview digest 和 expiry；upgrade/delete 后旧 capability 永久失效。
- [ ] policy 支持 deny precedence 和 `allow|ask|deny`；pending queue、TTL 和容量有硬上限。
- [ ] one-shot approval 绑定规范化参数与准确 preview，由认证/CSRF Web action 决定；
  模型、Worker 和浏览器请求体不能伪造内部 approval/result/identity。
- [ ] cancel、delete、restart、expiry、policy revision、approve/deny race 和无交互
  sub-agent/background 场景默认拒绝且零 executor side effect。
- [ ] permission requested/resolved 与 operation intent/outcome 可 durable replay，但批准本身
  不会因重放再次执行副作用。

证据：_待补。_

### CMP-01 — Tool-result budget 与 micro-compaction

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [ ] 每种 Tool 定义 request/result/provider-projection 硬上限和明确 truncation policy。
- [ ] 超长结果替换为有界 placeholder/reference，保留 call ID、原始 bytes、digest 和
  truncated reason，不破坏 tool_use/tool_result 配对。
- [ ] micro-compaction 优先清理低价值 Tool payload；canonical transcript/event 不重写。
- [ ] artifact/output retention、单 Run/Agent 总量和清理策略明确，无逐 chunk 落盘。
- [ ] 每次 provider call 在替换后重新 admission；仍超 hard budget 时 fail closed。
- [ ] 同一 canonical source 和 policy 重建出相同 projection 与 digest。

证据：_待补。_

### CMP-02 — Durable projection boundary 与 snapshot

Authority：[event protocol](../design/event-protocol.md)和[架构](../design/architecture.md)。

- [ ] projection boundary/snapshot 绑定 conversation revision、history/model/profile/
  instruction/toolset/policy digest 和 renderer version。
- [ ] canonical completed transcript 保持 append-only；snapshot 只描述模型视图。
- [ ] snapshot 不匹配任何绑定字段时拒绝复用并确定性重算，不“尽量恢复”陈旧视图。
- [ ] crash/restart 在上一个 durable boundary 恢复；preserved recent segment identity 可验证。
- [ ] replay、manual compact 和后续 semantic summary 使用同一 boundary protocol。
- [ ] snapshot/prune/retention 的磁盘上限和增长斜率有测试。

证据：_待补。_

### READ-01 — 安全文件元数据与文本读取

Authority：[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] 首版提供 workspace-relative `file/stat` 和 `file/read_text`，限定 offset/line/
  byte/time，结果带 path identity、content digest 和 truncated 标志。
- [ ] 使用 descriptor-anchored containment：可用时采用 openat2-style BENEATH、
  NO_SYMLINKS/NO_MAGICLINKS/NO_XDEV；否则使用逐组件 dirfd + no-follow 的等价验证，
  若 host 无法提供等价原语就禁用该 capability，绝不退化为 path-based 普通 open。
- [ ] 只接受符合 owner/mode 策略的普通 UTF-8 文件；hardlink、FIFO、socket、device、
  sparse/growing/oversize/binary 和特殊 proc 路径 fail closed。
- [ ] read receipt 可供后续 mutation CAS 使用；文件内容始终是 untrusted Tool data。
- [ ] 真实模型完成 read → reasoning → answer，且 checkout/其它 Agent 零读取、零写入。
- [ ] read-only workload 除规定的 durable semantic event 外不产生文件内容日志或临时索引。

证据：_待补。_

### SEARCH-01 — 有界 Glob/Grep

Authority：[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] Glob/Grep 不通过 shell，固定 workspace root，并限制 depth、entries、files、bytes、
  matches、result bytes、regex complexity 和 wall time。
- [ ] 遍历与打开复用 READ-01 的 descriptor-anchored primitive，不先收集未经验证的路径
  再二次打开；不具备等价 containment 的 host 上 fail closed。
- [ ] 遍历拒绝 symlink/magiclink/xdev/cycle、特殊文件和其它 Agent/runtime/data root。
- [ ] 结果稳定排序，包含 provenance/truncated reason，恶意换行文件名不能破坏协议/UI。
- [ ] deep/wide tree、pathological glob/regex、output flood、cancel 和 Capsule delete race
  均有负面测试，不创建无界磁盘索引。
- [ ] 真实模型完成 search → bounded read → answer 的同一 Run 闭环。

证据：_待补。_

### CMP-03 — Deterministic context-collapse projection

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [ ] 定义无模型调用的有序 collapse layers：低价值 Tool payload projection、已完成旧
  Turn group placeholder/collapse、保留最近完整 Turn 与 Tool call/result identity。
- [ ] trigger、target、hard input、output reserve 和各层 budget 全部从受信
  ModelProfile/ContextPolicy 推导，不按模型名或某个固定窗口/比例建立分支。
- [ ] 相同 canonical source、projection boundary、profile 和 policy 产生字节一致的模型视图；
  canonical transcript/events 保持 append-only。
- [ ] 从当前 tail-window 迁移时显式版本化 renderer/snapshot；旧 snapshot 不静默套用新规则。
- [ ] 每层后重新 admission，所有可省略层耗尽仍超 hard budget 时 fail closed，不截断
  instruction、user input 或 tool_use/tool_result 配对。

证据：_待补。_

### CMP-04 — Semantic summary 与 autocompact

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [ ] 只有 deterministic collapse 的质量/容量基线完成且 summary 评测门槛通过后，才启用
  semantic summary；summary Run 使用空 ToolSet 且不能产生 privileged side effect。
- [ ] summary 绑定 source Turn IDs/digest、model/profile/prompt/policy/renderer version，结构、
  输入、输出、时间、重试和持久化大小均有硬上限，并保留最近完整 pairs/Tool identities。
- [ ] manual compact 与 autocompact 使用 CMP-02 的同一 durable boundary；重启可复用匹配
  snapshot，不匹配时确定性重建，不修改 canonical transcript。
- [ ] summary failure/timeout/cancel 沿用上一个有效 projection；有限重试和熔断不会形成
  immediate compact loop，也不会阻塞显式 cancel/delete。
- [ ] 以关键事实、决定、未完成任务、文件状态、引用准确性和 prompt-injection resistance
  评测至少两个不同窗口 profile，退化时可关闭并回滚到 CMP-03。

证据：_待补。_

### CMP-05 — One-shot reactive overflow recovery

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [ ] 对 provider 的真实 context-length/media overflow 分类；认证、网络、格式和其它错误
  不冒充 overflow，也不触发压缩重试。
- [ ] 每个 provider call 最多一次 recovery：先重做受信 profile 下的 cheap projection，
  必要时复用/生成一个符合 CMP-04 的 summary，然后重新 admission 并发起一次 provider call。
- [ ] recovery identity 和 attempt 进入 durable boundary/terminal；Gateway/Worker 重启、SSE
  重连或模型再次请求 Tool 都不能重复既有 Tool/文件/进程副作用。
- [ ] recovery 仍超限或再次 overflow 时收敛为 canonical failure，并记录 estimator/profile
  偏差供重新 qualification；不循环缩短、不静默丢失当前 user input。
- [ ] cancel、deadline、provider partial stream、summary failure 和换模型竞态有故障矩阵。

证据：_待补。_

### WRITE-01 — Diff-bound atomic Edit/Write

Authority：[安全边界](../../SECURITY.md)、[event protocol](../design/event-protocol.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] Edit 与 Write 是不同 Tool：Edit 要求精确唯一匹配；Write 明确 create/full-replace。
- [ ] Edit/full-replace 需要现存 target 的完整 read receipt；create 使用独立的“target absent
  + parent directory identity”receipt，并以 no-clobber commit 保证不会覆盖竞态新文件。
- [ ] receipt 绑定 agent generation、canonical target identity/content digest，以及审批时的
  准确 diff/preview digest；所有路径操作复用 READ-01 的 descriptor-anchored primitive。
- [ ] Broker 按 canonical target 串行化 mutation，并在同一 conditional commit boundary 内
  重验 receipt/CAS；使用同目录 private temp 和 fail-closed atomic/no-clobber rename，atomic
  失败不跟随 symlink、不原地 fallback。
- [ ] intent/approval/temp/write/sync/rename/outcome 各崩溃点遵守统一 at-most-once dispatch；
  rename 已发生但 outcome 未 durable 时标记 `outcome_unknown`，重启不自动重放，残留 temp
  有界回收。
- [ ] 单文件、单调用、单 Run mutation/diff/output bytes 和 fsync 次数有量化上限。
- [ ] 用户批准的 diff digest 与最终文件 digest 对账；所有失败点都只能留下旧内容或完整的
  已批准新内容，绝不留下 torn/out-of-scope write；commit 后确认不明可呈现
  `outcome_unknown`，但不能自动重做。

证据：_待补。_

### EXEC-01 — Allowlisted argv runner

Authority：[安全边界](../../SECURITY.md)、[event protocol](../design/event-protocol.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] 首版只执行固定 project-local executable identity + argv，`shell=False`、固定 cwd、
  clean env/PATH，无 shell rc、用户配置、继承 secret 或请求级 executable。
- [ ] 使用独立更细 runner sandbox；主 Worker 权限不变，默认无网络、无 checkout/
  workspace 外读写和无未授权 package install；整个 workspace 默认只读，每个 command
  仅获得预声明、精确、带 quota 的输出目录，源文件 mutation 只能走 WRITE-01。
- [ ] CPU、memory、process、FD、file/output/disk/entry/time 限制明确；执行前必须建立可验证
  descendant container（cgroup/PID namespace 或等价机制），仅有 process group 时 fail closed。
- [ ] durable intent 在 dispatch 前记录 executor/runner/descendant-container identity，并通过
  受信 start handshake 释放执行；spawn/exit/outcome 不明时不自动重跑。
- [ ] cancel/timeout/stop/delete 清理整个 descendant container、pipe/FD、PID record、
  output/temp；setsid/double-fork 不能逃逸，process-group cancel 只作为辅助而非安全边界。
- [ ] v1 只支持 foreground；fork bomb、setsid/double-fork、signal ignore、output flood、
  network、PATH/executable swap 和环境注入负面测试通过。
- [ ] 真实模型可运行 allowlisted test/build 命令并把有界结果回流同一 Run。

证据：_待补。_

### MODEL-01 — Trusted ModelCatalog

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[README](../../README.md)。

- [ ] 建立受信 provider/model catalog；endpoint 只能来自 operator allowlist，不能由
  browser、Worker、Tool 参数或普通会话消息覆盖。
- [ ] 每个模型记录并资格检查 native/operational window、output reserve、Tool/streaming
  能力、tokenizer/estimator version 和 generation options 边界。
- [ ] 每个 Turn 固化模型快照；只允许在 Turn 之间切换，并从完整 durable transcript
  按新 profile 重编 ContextPlan。
- [ ] provider usage 经 Control Plane 校验后进入 bounded conversation/run ledger；
  incomplete usage 不作为完整费用或预算事实。
- [ ] 默认模型只由受信 operator 配置和 README 记录；至少用两个不同窗口/能力 profile
  验证动态 projection、切换、回滚和不按 model name 分支。

证据：_待补。_

### CMD-01 — Slash Command 与 Web 控制面

Authority：[架构](../design/architecture.md)、[README](../../README.md)和
[event protocol](../design/event-protocol.md)。

- [ ] 建立 command registry、稳定 ID/schema、alias、availability、help 和 feature gate。
- [ ] Slash Command 在认证 Web 输入边界解析，不作为 user Turn 发送给模型，也不依赖
  模型猜测管理意图。
- [ ] 首批提供 status、context、model、compact、permissions、cancel 和 clear；
  修改态保持 CSRF、active Run 冲突和审计语义。
- [ ] 命令只调用已有受信服务，不创建第二套 context、permission、model 或 persistence 状态。
- [ ] Web 展示 Context budget、compaction boundary、permission、diff、Tool/Task progress
  和完整事件详情；正文/secret/hidden prompt 保持不可见。

证据：_待补。_

### TASK-01 — Durable background Task substrate

Authority：[架构](../design/architecture.md)、[event protocol](../design/event-protocol.md)
和[Agent Capsule design](../design/agent-capsule.md)。

- [ ] 定义 Task identity/state、owner generation、parent Run、intent/outcome、result 和
  bounded output/notification protocol。
- [ ] 前后台切换、stop、deadline、restart-interrupted 和 Agent delete 可收敛，无隐式 daemon。
- [ ] descendant ownership 使用可验证 process tree/cgroup 或等价机制；PID/PGID 单独不足
  时不开放 background execution。
- [ ] output 批量持久化/轮转并有总量和 retention 上限，不逐 stdout/stderr chunk fsync。
- [ ] runner crash、Gateway crash、double-fork、zombie、FD/socket/cwd 引用和 orphan
  cleanup 负面测试通过。

证据：_待补。_

### EXEC-02 — Bounded Bash

Authority：[安全边界](../../SECURITY.md)和[event protocol](../design/event-protocol.md)。

- [ ] 在 EXEC-01 之上增加明确 shell grammar/parser；parse error、unknown expansion 和
  无法证明的结构默认 ask/deny。
- [ ] read/search 分类只服务 UI/策略提示，不能作为 sandbox 或只读安全证明。
- [ ] command/normalized AST、cwd、env、redirection 和 preview digest 与 one-shot approval
  绑定；无 `dangerouslyDisableSandbox` 或 unconfined fallback。
- [ ] substitution、backtick、env assignment、redirection/heredoc/subshell/pipe/glob、
  shell rc、git hook/pager/config 和 package-manager network 有专项策略和负面测试。
- [ ] background Bash 只通过 TASK-01；cancel/delete/stop 清除整个 descendant tree 和输出资产。

证据：_待补。_

### EXT-01 — MCP/LSP

Authority：[安全边界](../../SECURITY.md)和[架构](../design/architecture.md)。

- [ ] MCP/LSP capability 适配同一 Tool contract、policy、approval、event 和 result budget。
- [ ] stdio/local process 按代码执行默认禁用；remote endpoint 固定 allowlist，实施 SSRF、
  DNS rebinding、credential isolation、transport frame/time/concurrency 限制。
- [ ] 不把 control-plane token、浏览器 session 或不相关环境变量传给 child/remote。
- [ ] connect/disconnect、schema drift、恶意 payload、timeout/cancel/restart 和 Agent delete
  均无旁路、无 orphan、无跨 Agent capability。

证据：_待补。_

### SKILL-01 — Skills 与插件化能力

Authority：[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [ ] versioned registry、package integrity、safe archive extraction、声明式 capability 和
  Agent 专属环境均有明确 contract。
- [ ] 依赖只允许 binary-only allowlist；source distribution/build hook 和运行时任意安装禁用。
- [ ] Skill 代码执行使用独立 sandbox、资源/输出/网络 policy，无 unconfined fallback。
- [ ] 文件触碰或 prompt 声明不能动态建立 Skill 信任；启用必须来自受信配置/审批。
- [ ] install/upgrade/delete/crash 后 environment/cache/process/file residual audit 通过。

证据：_待补。_

### SUB-01 — Tasks、mailbox 与子智能体

Authority：[架构](../design/architecture.md)、[event protocol](../design/event-protocol.md)
和[Agent Capsule design](../design/agent-capsule.md)。

- [ ] parent_run_id、Task 状态、mailbox、result collection 和 brokered message capability
  是显式协议，不引入嵌套 hidden graph。
- [ ] 每个子 Agent 使用自己的 Capsule/environment/sandbox；父 Run 只持有有界 capability。
- [ ] 全局、每 Agent、每父 Run concurrency/depth/token/cost/wall budget 和循环检测明确。
- [ ] cancel/deadline/delete/restart 传播、orphan cleanup 和唯一 terminal 有故障矩阵。
- [ ] Web 展示 parent/child timeline 和独立事件详情；普通文本不能冒充跨 Agent message。
- [ ] 一个 Agent 无法读取或修改另一个 Capsule，除非持有精确 brokered capability。

证据：_待补。_

### REL-01 — 首个受支持版本资格

Authority：[README](../../README.md)、[安全边界](../../SECURITY.md)、
[P1-P8](../PRINCIPLES.md)和本计划。

- [ ] 冻结首个 release scope；未纳入的 P2 能力明确 deferred，不以空实现冒充支持。
- [ ] cold-checkout、支持架构、真实模型、load/soak/chaos、upgrade/rollback、backup/restore、
  stop/delete residual 和 SSD 写放大 gate 全部可重复。
- [ ] 明确受信网络/TLS 或 reverse-proxy contract、single-operator 边界和 production
  token lifecycle；当前本地 stop/atomic-rotate/restart 不能替代无中断轮换、审计或
  多用户凭据治理，未实现多用户隔离时保持明确声明。
- [ ] bounded/redacted logs、metrics、audit、retention、SBOM/dependency/vulnerability 和
  release artifact/runbook 完成。
- [ ] capacity envelope、平台支持矩阵和已知限制有证据；所有权威文档与实际 release 一致。
- [ ] 只有 GATE-01 至 GATE-06、冻结 scope 中的所有 work item 和本项其余 checkbox 均通过，
  才关闭 GATE-07 并移除 walking skeleton / not production ready 声明。

证据：_待补。_

### 强制负面测试矩阵

以下矩阵是对应 work item 的退出门槛，不是可选加固：

- **NEG-CAPSULE（AGT-01）**：非法/碰撞 ID、预置 symlink/hardlink/特殊文件、错误
  owner/mode、mount/device 变化、恶意 registry/manifest；create/create、upgrade/run、
  delete/run/approval 并发；PID reuse、伪造 record、foreign live process、leader 已死但
  descendants 存活；每个 staging/rename/commit 点崩溃；删除后完整资产为零且其它 Agent
  hash/mtime/process/journal 不变。
- **NEG-BROKER（TOOL-01、PERM-01）**：unknown/duplicate/schema drift、extra/missing/NaN/
  oversize/foreign/replay/out-of-order；重复、过期、取消、删除、重启、foreign approval；
  args/preview/policy revision 偷换和 approve/deny race；任何失败均零 privileged side effect。
- **NEG-FILE（READ-01、SEARCH-01）**：traversal、absolute/NUL/超深/Unicode 路径、
  symlink/magiclink/hardlink/rename/xdev race、special file、wrong owner/mode、huge/sparse/
  growing/binary/invalid UTF-8；深宽树、cycle、pathological glob/regex、恶意文件名和
  output flood；跨 Agent、取消、超时和 Capsule 删除竞态。
- **NEG-WRITE（WRITE-01）**：stale/partial receipt、零/多匹配、审批后修改/rename/
  symlink/mode race；ENOSPC/EDQUOT/EIO 在 temp/write/sync/rename/dir-sync 各点；每个
  intent/approval/commit/outcome 点崩溃；重复调用、超 mutation/diff 限制、UI XSS/ANSI/
  secret payload；无原地 fallback、无双写、无跨 Agent 变化。
- **NEG-EXEC（EXEC-01、TASK-01、EXEC-02）**：command substitution、backtick、env/
  PATH/loader 注入、redirection/heredoc/subshell/pipe/glob/newline、shell rc、git hook/
  pager/config、package-manager network；socket/ptrace/proc/mount/device/outside read/write；
  fork bomb/setsid/double-fork/zombie/FD leak；CPU/memory/process/disk/output flood、
  timeout/cancel/restart；终态 descendant/output/temp/PID/socket/lock 全清。
- **NEG-SSD-LIFECYCLE（所有新增 capability）**：read-only、mutation、command、取消、
  失败、重启、删除 workload 记录 logical/allocated bytes、process I/O、fsync、WAL/log/
  cache/temp 峰值；连续 N Turn 的增长只依赖新增语义数据；无 delta/provider frame/
  command chunk 逐条落盘，无 delete VACUUM、整树每 Turn 扫描或无法证明的 secure erase。

## Architecture guardrails

- 不重新引入旧系统、LangGraph 或 LangChain；若未来要采用编排框架，必须先证明其
  解决了具体缺口且不会产生第二套状态机、事件序列或不可控持久化。
- 保持 Control Plane 为 identity/sequence/persistence authority，Worker 永不自报身份。
- 保持 QueryEngine 为轻量 Conversation orchestration；不得让它缓存 transcript/event、
  另开 SQLite、拥有 Worker/model session 或形成第二套 agent loop。
- 保持 token-level streaming，但只在语义边界持久化。
- 模型窗口和压缩阈值来自受信 qualification/profile，不按 provider/model 名称硬编码；
  原生容量、运行上限、输出预留和估算器版本都必须进入可审计 plan metadata。
- Provider usage 必须先由 Control Plane 对照 profile 校验后才进入 canonical terminal；
  `complete=false` 必须阻止调用方把部分累计误当成本 Run 完整用量。
- 新能力走显式 broker/capability，不扩大主 Worker 权限。
- 所有资源先定义硬上限和失败语义，再实现 happy path。
- 活动计划、设计、安全和用户文档与实现同改。

推进顺序、当前状态和下一可执行项只以总状态表、`depends_on` 和 Phase exit gates 为准；
不要另建 Now/Next/Later 状态副本。当前 tail-window 不能宣称为语义摘要，也不能绕过
依赖直接加入文件 mutation、Bash、大量 Tool 或多 Agent。

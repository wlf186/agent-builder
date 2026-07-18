---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-18
review_cycle: quarterly
---

# Runtime rebuild plan

## Objective

在不继承旧系统依赖和隐式状态的前提下，逐步把当前 Claude Code 风格 walking skeleton
发展为可维护、可恢复、可扩展的本地智能体运行时。每个阶段必须保持 P1-P8、明确的
单一 Agent loop、规范事件流、checkout containment 和 fail-closed sandbox。

本计划描述方向与验收，不承诺日期。只有已通过代码、测试和真实 lifecycle 验收的
内容才能从“planned”移到“implemented”。

## Implemented baseline

- 根级 bootstrap/start/stop/purge/governance 生命周期与 checkout-local 环境边界；
- `0.0.0.0:20815` 认证 Web UI、CSRF、Conversation create/list/read/delete、同会话
  多轮 Run/cancel、SSE 和完整事件详情；
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

这份证据不是 cold-checkout、多架构或 production qualification：依赖缓存已存在，尚未在
`aarch64` 重复，也没有长期 load/soak、SSD SMART 对照、故障注入矩阵或 TLS/多用户边界。

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

- 已实现：Conversation/Turn/Run 生命周期分离和 Agent-scoped repository；公共路径为
  `GET|POST /api/sessions`、`GET|DELETE /api/sessions/<id>`、
  `POST /api/sessions/<id>/runs`；
- 已实现：一个 Conversation 最多一个 active Run；Turn 接受与 `run.started`、Turn 完成
  与 terminal 分别在同一 SQLite/WAL 事务提交；completed-history snapshot revision 与
  Turn 接受使用 CAS，拒绝编译期间的历史漂移；
- 已实现：durable Conversation transcript 跨 Gateway 重启恢复，启动时把遗留
  `running` Turn 标为 `interrupted`；旧 Worker/模型流不复活；
- 已实现：删除空闲 Conversation 时原子移除 Turn 和关联 events；active Run 存在时拒绝，
  先取消或等待终态；
- 待实现：从 durable event journal 重建旧 Run、cursor availability、gap marker、
  snapshot/replay 和幂等事件投影；当前活跃 SSE/ephemeral delta 不跨 Gateway 进程恢复；
- SSE 增加明确的 cursor availability、gap marker、snapshot/replay 和幂等投影；
- 对 journal prune、崩溃点、WAL 损坏/不可写设计可验证失败行为；
- 继续批量语义写入，禁止 token delta 落盘和完整历史反复重写；
- 添加 context/token/cost usage 的有界记录，但先定义 retention/redaction。
- 以 ContextPlan 中的模型画像动态决定 projection/compaction，不按模型名称或固定
  `4096` 阈值分支；保留输出预留，达到硬输入预算 80% 时触发，压缩到 60% 目标；
- 压缩决策优先使用实际模型 profile 和 provider 报告、Control Plane 校验累计的 usage；
  `utf8-bytes-upper-bound-v1` 只作为没有精确 tokenizer/终帧数据时的 admission fallback；
- 已实现：仅 completed Turn 的完整 user/assistant pair 可进入模型历史；失败、取消、
  interrupted 和 partial output 留在产品状态但不污染模型上下文；
- 已实现：根据当前模型 profile 的 80%/60% 策略做完整 Turn pair 的确定性 tail-window；
  到达目标或没有可移出的 pair 后停止；这不是语义 summary，durable transcript 不被
  截断或重写；
- 待实现：Tool-result clipping/micro-compaction、可恢复 snapshot 和模型 summary；完整
  canonical events 保持 append-only，不反复重写历史。

完成标准：当前 conversation transcript/interrupt recovery 子切片已落地；Phase 只有在
任意受支持崩溃点恢复到最后 durable boundary、客户端能区分 replay/gap/live，且写放大
与磁盘上界有测试后才整体完成。

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

- 已完成基础：分层 platform contract、固定 demo Agent instructions 和当前 user turn，
  保留 provenance/cache scope；编译结果已穿过 Broker 并替代 Broker 内固定 prompt；
- 已完成基础：ToolSpec/model profile/budget/plan digest 进入同一受信 ContextPlan；模型
  原生窗口变化会动态重算 operational window、输出预留、硬输入预算和 80%/60% 阈值；
- 已实现：只从 durable completed Turn pairs 构建 conversation history；每个新 Turn 根据
  当前模型 profile 重新计算 projection，并把 history source digest、完整/纳入/省略计数
  和窗口策略绑定到 ContextPlan；
- 已实现：超过 80% trigger 后按完整 Turn pair 从最旧端做 deterministic tail-window，
  直到不高于 60% target 或没有可移出的 pair；这是有损窗口选择，不是语义 summary；
- 待实现：workspace instructions（包括受控发现 `CLAUDE.md`）、Tool-result micro-
  compaction、summary/snapshot 与 compaction events；
- 防止 prompt/Tool/result 混淆、跨 Agent context 泄漏和 hidden instruction 输出；
- 同一 Run 的 model/tool transcript 可恢复并与 canonical events 对账。

完成标准：相同输入、completed history、模型画像和 Capsule generation 产生可复现
context plan；超预算和未来语义压缩有明确事件；不同 Agent 指令/历史在进程、存储和
模型请求层均隔离。当前满足 plan/tail-window 可复现和硬预算，summary/snapshot、
workspace instructions 与 compaction event 尚未完成。

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

- TLS/可信 reverse proxy、多用户/租户边界和 token rotation；
- Control Plane 与 Web BFF 是否拆进程，以故障与权限分析决定，不为“微服务化”而拆；
- bounded/redacted tracing、metrics、audit 和 retention；
- load、soak、chaos、upgrade/rollback、backup/restore 与磁盘磨损测试；
- dependency/SBOM/vulnerability、release artifact、cold-checkout 和运维 runbook；
- 明确支持矩阵和 capacity envelope。

只有所有 release gates 都有可重复证据，才能修改“walking skeleton / not production
ready”的声明。

## Architecture guardrails

- 不重新引入旧系统、LangGraph 或 LangChain；若未来要采用编排框架，必须先证明其
  解决了具体缺口且不会产生第二套状态机、事件序列或不可控持久化。
- 保持 Control Plane 为 identity/sequence/persistence authority，Worker 永不自报身份。
- 保持 token-level streaming，但只在语义边界持久化。
- 模型窗口和压缩阈值来自受信 qualification/profile，不按 provider/model 名称硬编码；
  原生容量、运行上限、输出预留和估算器版本都必须进入可审计 plan metadata。
- Provider usage 必须先由 Control Plane 对照 profile 校验后才进入 canonical terminal；
  `complete=false` 必须阻止调用方把部分累计误当成本 Run 完整用量。
- 新能力走显式 broker/capability，不扩大主 Worker 权限。
- 所有资源先定义硬上限和失败语义，再实现 happy path。
- 活动计划、设计、安全和用户文档与实现同改。

## Next decision

Phase 1 在当前 `x86_64` checkout 的真实模型 lifecycle 子切片已经复验；仍需完成空缓存
cold-checkout、`aarch64`、load/soak、故障注入和磁盘写入基线，才能关闭该 Phase。
Phase 2 已落地 durable Conversation/Turn、重启 interrupted recovery 和确定性
completed-pair windowing 子切片；下一步应补 durable event replay/gap/snapshot，再推进
Phase 3（通用 Capsule 生命周期）。不要把当前 tail-window 宣称为语义摘要，也不要直接
跳到文件、Bash、大量 Tool 或多 Agent 功能，否则会把临时恢复语义固化为新历史包袱。

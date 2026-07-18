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
- `0.0.0.0:20815` 认证 Web UI、CSRF、Run/cancel、SSE 和完整事件详情；
- 固定 demo Agent Capsule、Agent 专属 `worker-env`、每 Run 独立目录和进程组；
- 单一 `HarnessKernel` context → model → tool → model loop，无 LangGraph/LangChain；
- 受信 Ollama Broker，固定 `iollama:11434/qwen3.5:2b` 和 per-Run transcript；
- Worker 无网络，Landlock + seccomp + rlimits + attestation，无 unconfined fallback；
- 仅有界、只读 `builtin/echo`；
- canonical event sequencing、durable semantic SQLite/WAL、ephemeral delta、取消和唯一终态；
- bounded Run/event/log/journal/tree 资源与安全的 PID/process-group 清理；
- 旧系统隔离到 `_legacy-reference/`，当前 runtime 不依赖它；
- P1-P8 权威文档和自动文档/边界治理。

“implemented baseline”只表示当前固定路径已跑通，不能解读为整个产品生产就绪。

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

- 定义 Conversation/Turn/Run repository 与事务边界；
- 服务重启时从 durable journal + snapshot 重建状态；
- SSE 增加明确的 cursor availability、gap marker、snapshot/replay 和幂等投影；
- 对 journal prune、崩溃点、WAL 损坏/不可写设计可验证失败行为；
- 继续批量语义写入，禁止 token delta 落盘和完整历史反复重写；
- 添加 context/token/cost usage 的有界记录，但先定义 retention/redaction。

完成标准：任意受支持崩溃点恢复到最后 durable boundary；客户端能区分 replay、gap 和
live；写放大与磁盘上界有测试。

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

- 分层 platform contract、Agent instructions、workspace instructions、history、Tool schema
  和当前 user turn，并保留 provenance/cache scope；
- 将编译后的有界 context 穿过 Broker，替代 Broker 内固定 prototype prompt；
- 实现 token budget、deterministic truncation、summary/snapshot 与 compaction events；
- 防止 prompt/Tool/result 混淆、跨 Agent context 泄漏和 hidden instruction 输出；
- 同一 Run 的 model/tool transcript 可恢复并与 canonical events 对账。

完成标准：相同输入和 Capsule generation 产生可复现 context plan；超预算和压缩有明确
事件；不同 Agent 指令/历史在进程、存储和模型请求层均隔离。

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
- 新能力走显式 broker/capability，不扩大主 Worker 权限。
- 所有资源先定义硬上限和失败语义，再实现 happy path。
- 活动计划、设计、安全和用户文档与实现同改。

## Next decision

Phase 1 是当前阻断项。完成 clean-root 全门禁和真实 lifecycle 复验后，再优先推进
Phase 2（恢复语义）与 Phase 3（通用 Capsule 生命周期）；不要直接跳到大量 Tool 或
多 Agent 功能，否则会把原型中的临时状态模型固化为新历史包袱。

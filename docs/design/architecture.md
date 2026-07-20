---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-20
review_cycle: quarterly
---

# Runtime architecture

## Scope and status

本设计只描述当前根目录 greenfield runtime。它采用 Claude Code “入口/UI → command →
每 Conversation 一个逻辑 QueryEngine → 单一 per-Run agent loop →
context/model/tool/state → 增量输出”的骨架，同时把 Web 部署需要的控制面、事件 journal
和强沙箱边界显式化。

当前是 walking skeleton，不是兼容层，也不是生产就绪系统。Gateway、Control Plane、
Model Broker 和 SQLite ownership 仍在同一个受信 Python 进程；每个 Run 的 Agent loop
位于独立 Worker 进程。

## 整体图

```text
┌──────────────────────────── Browser ─────────────────────────────┐
│ login · submit/cancel · Turn transcript · four-lane Run replay   │
└───────────────────────────────┬───────────────────────────────────┘
                                │ HTTP + SSE, 0.0.0.0:20815
                                ▼
┌──────────── trusted Gateway / Control Plane (one process) ────────┐
│ web.py       auth, CSRF, session CRUD, limits, static UI, SSE       │
│ agents.py    persistent Agent registry + generation lifecycle       │
│ agent_runtime.py lazy per-Agent services + shared model admission   │
│ commands.py  typed CommandBus                                      │
│ query_engine.py  bounded identity map + one Engine / Conversation   │
│ control.py   RunService, Worker supervisor, validator, sequencer   │
│ sessions.py  Conversation/Turn + recovery/ledger/snapshot txns      │
│ context.py   trusted history projection + model-capacity policy    │
│ tools.py     immutable ToolSpec + effective prototype Tool set      │
│ state.py     durable EventJournal + bounded replay/retention        │
│ ollama.py    fixed-target, bounded model broker                     │
└──────────────┬────────────────────────────────────┬────────────────┘
               │ bounded versioned stdio NDJSON     │ trusted TCP only
               ▼                                    ▼
┌──────── one process per Run ───────┐       iollama:11434
│ worker.py                          │       qwen3.5:2b
│ close FDs → rlimits → sandbox      │
│ Landlock + seccomp + attestation   │
│                                    │
│ HarnessKernel (single finite loop) │
│   validated ContextPlan reference  │
│      ↓                             │
│   BrokeredStreamingModel ──────────┤
│      ↓                             │
│   ToolRegistry: builtin/echo       │
│      ↓                             │
│   RunState / terminal convergence  │
└───────────────┬────────────────────┘
                │ identity-free WorkerEvent
                └────> validate → canonical EventEnvelope
                              ├── durable → SQLite/WAL → replay/snapshot
                              └── live → memory → SSE → Browser
```

一个逻辑 `QueryEngine` 跨越同一 Conversation 的多个 Turn，但每个 Turn 都创建新的 Run、
Worker、`HarnessKernel` 和 model session。模型响应必须回到该 Run 的同一个
`HarnessKernel`，Tool result 也回到同一个 model session。不存在 LangGraph/LangChain
graph、第二套 hidden loop 或由前端拼接状态的旁路。

浏览器只组合两类已经存在的读模型：右侧 Conversation/Turn transcript 来自 session API，
左侧所选 Run 的规范时序来自 live SSE 或经过完整验证的 durable replay。四泳道
User/Harness/LLM/Tool 是展示投影，不是第二套运行状态机；排序仍只服从 EventEnvelope
的 `seq`。为解释一次提交，界面可在 `run.started` 前显示一个由该 Turn durable user
message 推导的 User → Harness 节点，但必须标记为 projection，不能赋予 canonical
`seq/event_id/durability`，也不能写回 journal。Turn 卡片选择对应 Run，事件节点反向定位
拥有它的 Turn；两者都不能改变 Conversation 或 Run 的权威状态。

Web bootstrap token 默认是 checkout-local 的 256-bit 随机值。显式本地轮换由
`set-access-token.sh` 独占完成隐藏输入、受管停止、原子 secret replacement 和重启；
Gateway 只持固定长度 verifier digest，旧 browser session 随进程重启失效。该机制不是
production 无中断轮换或多用户 credential system。

## 一次 Run

1. 浏览器用 bootstrap token 建立登录 session，通过 `POST /api/sessions` 新建持久
   Conversation，再以 CSRF 保护的 `POST /api/sessions/<conversation-id>/runs` 提交消息。
2. `CommandBus` 把命令交给有界 `QueryEngineRegistry`；Registry 为该 Conversation
   canonicalize 唯一逻辑 `QueryEngine`。Engine 固定 Agent/Conversation 身份并只返回
   不可变 Run handle，再委托 `RunService` 为现有 Conversation 新建 Turn → Run 身份。
   `ConversationStore` 在 Agent 专属 `state.sqlite`
   中验证该会话没有 active Run，并读取带 revision、仅由 completed Turn 组成的完整
   user/assistant pairs 快照。受信 `ContextCompiler` 根据该历史、模型画像、Capsule
   generation、有效
   ToolSpec 和本轮消息编译不可变 ContextPlan。
3. Control Plane 先用 expected revision 做 compare-and-swap，再在一个 SQLite 事务中
   保存 `running` Turn、绑定 active Run、写入 durable `run.started`，并在
   `run_journal_state` 为该 Run 保留 cursor high-water `512`；历史在编译期间
   发生漂移则拒绝，不用陈旧 ContextPlan 执行。只有事务成功才接受执行。一个
   Conversation 的数据库约束和控制面状态机共同保证同时最多一个 active Run。
4. Capsule Manager 创建 `.runtime/agents/<agent-id>/runs/<run-id>/`，为该 Run 准备
   独立 HOME、TMP、XDG、input、work 和 output。
5. Control Plane 用 Agent 专属 `worker-env` 在新 process group 中启动 Worker，并
   原子发布强身份 PID record。
6. Worker 在读取消息前完成 FD isolation、rlimits、Landlock、seccomp 与 attestation；
   Control Plane 通过 `/proc` 复核后才发送命令。
7. Control Plane 只把本轮用户消息和 `plan_id`/plan digest/toolset digest 引用发给
   Worker。`HarnessKernel` 用该引用通过 Model Broker IPC v2 发起迭代；Worker 不能读取
   或改写 ContextPlan 中完整的 platform/Agent 指令、Tool manifest、模型画像和预算；
   Worker 只保留运行时内置的同版本 ToolSpec，用于调用校验与本地 Echo 执行。
8. Broker 为该 Run 独占一个 `OllamaRunSession`，从受信 ContextPlan 渲染 system、
   completed history user/assistant pairs 和当前 user message，并从相同 ToolSpec 生成
   Ollama Tool schema。当前不可变 `TurnRuntimeSnapshot` 固化 Agent generation、模型画像、
   ContextPlan、Tool manifest/digest、最多 4 次模型调用、最多 2 次顺序 Tool 调用、累计
   usage 上限和 60 秒 wall deadline。模型可顺序请求两次结构化 `builtin/echo`；每次结果
   都按原 call ID 回流，第二次预算消费后 provider ToolSet 收窄为空，再进行最终模型调用。
   call/result 重复、乱序、并行、超预算或 snapshot 漂移都 fail closed。
9. Ollama session 在完整 provider request 已通过动态 Token/字节 admission、但尚未打开
   HTTP stream 的最后受信边界，计算带 domain separation 的 request digest 和有界元数据。
   Control Plane 把该次调用真实运行时估算的 `provider_usage.started` 与 durable
   `model.request.started` 放入同一 SQLite 事务；Ollama 完整终帧 usage 经 profile 校验后，
   `provider_usage.complete` 与唯一 `model.response.finished` 也在同一事务提交。Broker
   error/cancel 的 response boundary 没有完整 usage，并在 terminal 事务把遗留 ledger
   收敛为 `incomplete`。每次调用因此固定增加两个语义事件、两个 ledger/event 事务，
   而不是逐 token 写盘；任何硬崩溃都不会留下“usage 已完成但 response 尚不存在”的
   不可恢复窗口。Worker 看不到也不能伪造 usage、request digest 或边界事件，terminal
   aggregate 必须与 provider ledger 完全一致。
10. Worker 发出无身份、逐事件的 `WorkerEvent`。Control Plane 验证 schema、状态转换、
   大小、durability 和唯一终态，然后补齐身份、时间和单调 `seq`。
11. durable 语义事件先与 `run_journal_state` 的 latest/count/bytes 在一个 Agent SQLite
    事务提交，随后进入 live window 和 SSE；delta 是 ephemeral，不写盘。持久化失败时
    Run 终止并诚实标记 memory-only failure；若 Conversation 事务仍可写，Turn、provider
    usage、遗留 dispatched operation 和该 partial durable prefix 原子收敛成不可回放、
    零 event/byte 的有界 tombstone，不会留下 retention 永久跳过的伪 active Run。
12. terminal、Turn 终态、provider aggregate 和严格 `run-ui-v2` UI projection snapshot
    在同一
    事务提交；成功时才写完整 assistant content 并释放 active Run，失败或取消不提交
    partial assistant。terminal、取消、
    deadline 或故障后，supervisor 回收进程组并删除 Run root。Agent 的 Conversation、
    manifest、workspace、artifacts、journal 和 worker-env 保留。`run-ui-v2` 增加严格配对的
    model call projection；历史 `run-ui-v1` snapshot 仍可验证读取，但不会被伪装成带新边界
    的 Run。

`operation_ledger` 已提供 `intent → dispatched → outcome|outcome_unknown` 的持久转换和
idempotency/request digest 绑定，作为未来 capability broker 在外部副作用前后的恢复底座。
重启只把遗留 `dispatched` 转为 `outcome_unknown`，不自动重放。当前 Echo 没有特权
executor 使用它，因此不代表已实现有副作用 Tool 或 exactly-once 执行。

## 组件职责

| 组件 | 拥有 | 不应拥有 |
| --- | --- | --- |
| `web.py` | HTTP/SSE、认证边界、session CRUD、请求/响应限制 | Agent loop、模型 transcript、任意路径 |
| `auth.py` | token、bounded session、CSRF | 用户业务状态 |
| `agents.py` | 最多 100 个 Agent 的 registry、provisioning/active/upgrading/deleting 状态和恢复 | live Run/QueryEngine、模型连接 |
| `agent_runtime.py` | 按 Agent generation 惰性激活/排空独立 RunService 与 QueryEngineRegistry；共享单一 Broker | Agent 持久状态、prompt、第二模型队列 |
| `commands.py` | 类型化 start/cancel 命令 | request-derived endpoint/process options |
| `query_engine.py` | 每 Conversation 一个逻辑 Engine、restore/submit/interrupt/delete、Run ownership、retained ContextPlan 元数据检查、最多 100 个实例 | transcript/event/ContextPlan/Worker cache、第二 Agent loop |
| `control.py` | Run、Worker、Broker 调度、ContextPlan ownership、event validator/sequencer | UI 投影、未隔离 Tool 实现 |
| `sessions.py` | Conversation/Turn 事务、Run journal metadata、terminal snapshot、operation/provider ledger、重启恢复 | 模型 prompt、Worker 生命周期、Web principal |
| `worker.py` | 一个 Run 的 bootstrap 与 IPC | 网络 endpoint、持久状态、其它 Agent |
| `kernel.py` | context/model/tool/cancel/terminal 单一循环 | HTTP、SQLite、进程管理 |
| `context.py` | 确定性 sections/provenance、completed-history projection、动态预算与 plan digest、正文隐藏的 operator inspection | provider/network selection、语义 summary、第二份 prompt persistence |
| `ollama.py` | 固定 endpoint/model、模型资格、per-Run transcript、ContextPlan 渲染、协议上限 | 浏览器可配置 provider |
| `tools.py` | 共享 `ToolSpec`、schema/限制/Tool set digest 和本地 Echo dispatch | Shell/Skill/MCP 动态执行 |
| `contracts.py` | command/event 类型与 canonical identity | 持久化策略 |
| `replay.py` | canonical durable payload/state validator、model call 配对、确定性 UI projector、v1/v2 snapshot codec | SQLite 事务、live stream、ContextPlan transcript |
| `state.py` | durable semantic append、严格有界 replay、snapshot-only retention | token delta、完整 live Run truth、语义 summary |
| `capsule.py` | Agent/Run 路径、generation staging/promotion、环境与清理所有权 | registry、HTTP admission |
| `sandbox.py` | fail-closed Worker kernel boundary | capability policy UI |

## 状态所有权

```text
Browser
  transient UI projection; never authoritative

Control Plane memory
  bounded QueryEngine identity map (identity + short operation lock only),
  active Run state, complete immutable ContextPlan, validated provider usage,
  live events, cancellation, Worker identity
  max 100 logical Engines; no Engine transcript/event/model cache
  max 4 active / 64 retained Runs

Worker memory
  one RunState, ContextPlan summary reference, open block, pending Tool, model iteration
  destroyed at terminal

data/agents/<agent-id>/
  manifest.json, workspace/, artifacts/
  state.sqlite[-wal|-shm]: Conversation, Turn, canonical durable events,
    run_journal_state, run-ui-v1/v2 snapshots, operation/provider ledgers

.runtime/agents/<agent-id>/
  worker-env/ (generation 1), generations/<n>/worker-env/,
  logs/, runs/<run-id>/
```

Conversation/Turn transcript 可由 SQLite 跨 Gateway 重启恢复；启动时任何遗留
`running` Turn 都由有界恢复 sequencer 校验并收敛：开放 block 先 discarded，requested-
only Tool 先补 started 再 finished，随后从已保留的 cursor high-water 之后写唯一
`run.failed(control_restarted)`，Turn 转为 `interrupted` 并释放会话。若崩溃时已有
`model.request.started` 而没有 response boundary，恢复 sequencer 先合成唯一
`model.response.finished(error/control_restarted)`，不会把未知 provider outcome 冒充成功。
durable gap 只能由
未落盘 delta 或明示的 reserved recovery range 解释；其它 gap、非法状态或超限输入
整体回滚并 fail closed。

认证的 durable replay API 按持久 Run identity 懒加载 Conversation Engine，在返回任何分页
前严格验证整个有界 Run 的 envelope、canonical payload、ToolSpec、状态机与 snapshot
document 语义，并返回显式 gap 与 digest-bound `run-ui-v1|v2` snapshot。
journal 只保留最近 256 个 Run events；被淘汰的 terminal Run 在同一事务转为
`snapshot_only`，活跃 Run 不 prune；中途 journal 故障产生的内部 tombstone 明确不可回放，
不会以 snapshot-only 或完整历史伪装。这个 UI 投影不是 ContextPlan summary。旧 Worker 和模型
连接不会复活；events endpoint 在没有 live RunRecord 时可固定使用 durable replay source，
以 `stream.gap` / `stream.snapshot` 明示缺失并发送已收敛 terminal。它不恢复旧执行，
ephemeral delta 也不跨进程恢复。

## Conversation、Turn 与 Run

```text
Gateway memory                         Agent Capsule / SQLite
QueryEngine (lazy, replaceable) ─────> Conversation (durable, authoritative)
                                      ├─ Turn 1 (user + status + answer?)
                                      │    └─ Run 1 (isolated execution)
                                      └─ Turn 2
                                           └─ Run 2
```

- QueryEngine 是 Conversation-scoped 的逻辑入口；同一 Gateway 内相同 Conversation
  canonicalize 为同一实例。它不缓存 transcript/revision/active Run/event/model/Worker，
  删除后 retire 并驱逐，Gateway 重启后从 durable Conversation 懒重建。
- Conversation 是可列出、读取恢复和删除的持久容器，不持有 Worker。
- Turn 是一次已接受的用户消息及其 `running|completed|failed|cancelled|interrupted`
  状态；当前每个 Turn 只有一个 Run，不承诺 retry/branch 语义。
- Run 是独立的执行尝试，拥有 ContextPlan、Worker、OllamaRunSession、canonical events
  和临时 Run root；终态后所有执行资源被回收。
- 后续 ContextPlan 只读取 `completed` Turn 的完整 user/assistant pair。失败、取消、重启
  中断或 discarded/partial output 可供 UI 审计状态，但不成为模型历史。
- 一个 Conversation 同时最多有一个 active Run。活跃时发起下一轮或删除返回冲突；
  删除只在空闲时原子移除 Conversation、Turn、Run events、snapshot 及关联 ledger，
  不按 ID 跨 Agent 删除。
- completed-history snapshot 的 revision 被绑定到 Turn 接受 CAS；并发请求不能让基于旧
  历史编译的 ContextPlan 在较新会话状态上启动。

## Claude Code 设计映射

| Claude Code 风格概念 | 当前对应 | 状态 |
| --- | --- | --- |
| 启动/环境初始化 | root lifecycle + host/model qualification | 已跑通 |
| UI / interactive surface | authenticated browser UI | 多轮 Conversation、全 Turn/Run 时间线、事件方向、正文隐藏的上下文检查已跑通 |
| command system | `CommandBus` start/cancel | 最小实现 |
| Query engine | one logical `QueryEngine` per Conversation + bounded registry | restore/submit/interrupt/delete 已跑通；durable state 仍由 Store/RunService 持有 |
| Agent loop | one isolated `HarnessKernel` finite loop per Run | 已跑通 |
| context assembly | trusted immutable `ContextPlan`、provenance/digest/budget、completed Turn 原生 chat roles | 多轮历史和动态完整-pair tail window 已进入真实请求；支持认证 metadata inspection，尚无 workspace 指令注入或语义 summary |
| model abstraction | `StreamingModel` + trusted Ollama Broker | 固定 provider/model |
| Tool registry/dispatch | shared immutable `ToolSpec` + `ToolRegistry` | 仅 Echo |
| state/history | durable Conversation/Turn + canonical event journal + bounded replay/snapshot | transcript、模型调用边界和旧 Run 可恢复；旧 Worker/live SSE 不恢复 |
| permissions/sandbox | broker boundary + Landlock/seccomp | 固定 Worker 路径已实现 |
| sub-agent/task system | parent/child Run、mailbox、scheduler | 未实现 |
| cost/context compaction | 动态模型画像、每调用 provider ledger、80%/60% policy | usage 与确定性 `completed-turn-tail-v1` 已实现；语义 summary/compaction snapshot 未实现 |

参考资料提供结构启发，不是源码依赖或安全证明。来源和版本见
[Claude Code reference provenance](../../references/claude-code/PROVENANCE.md)。

## 资源与失败原则

模型容量不是按模型名称写死。启动时 Broker 从 `/api/show` 读取架构专属原生
`context_length`，再应用受信运行上限与输出预留。当前 `qwen3.5:2b` 报告 `262144`；
运行窗口为 `min(262144, 32768) = 32768`，输出预留 `2048`，硬输入预算 `30720`。
压缩策略阈值按硬输入预算动态计算：80%（`24576`）触发、60%（`18432`）为目标。
这些值进入 ContextPlan 和 `run.started` metadata。每个新 Turn 根据当前模型 profile
重新投影所有 completed pairs；估算超过 80% 阈值时，从最旧端按完整 pair 移出，直到
不高于 60% 目标或没有可移出的 pair。SQLite 中的完整 transcript 不改变，后续换用
更大窗口的模型可重新纳入旧 pair。该 `completed-turn-tail-v1` 是确定性窗口选择，不是
模型 summary 或语义压缩；受保护 sections 与当前 user message 仍超出硬预算时 fail closed。

当前 `utf8-bytes-upper-bound-v1` 是 admission fallback：没有模型 tokenizer 时，每一个
UTF-8 byte 都按一个 token 估算，并计入实际 renderer、完整 Tool manifest 与 `256`
tokens 的 provider template reserve。这是确定性保守上界，不是假装精确的 tokenizer。
窗口策略使用实际模型 profile；provider 报告且经 Control Plane 校验的 usage 是受信
观测，但每个新渲染请求仍用版本化 estimator 做保守 admission。未来 tokenizer 或
summary 不能把不同模型/profile 的计数和快照静默复用。

- 事件、模型 frame/output、active Run、内存 retention、Run wall time、文件树和日志
  都有硬上限；超额使该 Run 失败，不扩容到无界状态。
- Model Broker 全局最多 2 路并发 provider stream；其它请求受 4 active Run 总容量和
  30 秒 slot timeout 约束，超时返回可重试 `model_busy`。
- 每轮最多接受 4096 个 provider 原始帧，并合并为最多 128 个 content IPC frame；Broker
  error 的 code/retryability 由 Control Plane 绑定到 canonical failure。
- 取消和任何失败最终收敛到且只收敛到一个 terminal event；未闭合 block/tool 先以
  durable recovery event 闭合。
- Worker event 先校验再应用；Worker 不能自报 Agent/Run identity 或 sequence。
- durable event 先 append journal 再对 live consumer 可见。journal 不可用时停止
  Worker，不把 memory-only 事件伪装成 durable。
- replay 返回前必须在 512 events / 256 KiB 内完整验证 Run，每页最多 128
  events；snapshot 与单 event 各不超过 65,536 bytes。超限或损坏不返回合法 prefix。
- sandbox/host/model qualification 缺失即拒绝启动或 Run，不提供 unconfined fallback。

## 当前不支持

旧 Worker或模型流跨进程恢复、语义 summary/compaction snapshot、workspace `CLAUDE.md`
注入、文件工具、Shell、Skill、MCP、RAG、
artifact broker、权限交互、子智能体、多用户、TLS、远程部署、正式 observability 和
production release qualification 均未实现。路线见
[runtime rebuild plan](../plans/runtime-rebuild.md)。

---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-18
review_cycle: quarterly
---

# Runtime architecture

## Scope and status

本设计只描述当前根目录 greenfield runtime。它采用 Claude Code “入口/UI → command →
单一 agent loop → context/model/tool/state → 增量输出”的骨架，同时把 Web 部署需要的
控制面、事件 journal 和强沙箱边界显式化。

当前是 walking skeleton，不是兼容层，也不是生产就绪系统。Gateway、Control Plane、
Model Broker 和 SQLite ownership 仍在同一个受信 Python 进程；每个 Run 的 Agent loop
位于独立 Worker 进程。

## 整体图

```text
┌──────────────────────────── Browser ─────────────────────────────┐
│ login · submit/cancel · incremental answer · full event timeline │
└───────────────────────────────┬───────────────────────────────────┘
                                │ HTTP + SSE, 0.0.0.0:20815
                                ▼
┌──────────── trusted Gateway / Control Plane (one process) ────────┐
│ web.py       auth, CSRF, session CRUD, limits, static UI, SSE       │
│ commands.py  typed CommandBus                                      │
│ control.py   RunService, Worker supervisor, validator, sequencer   │
│ sessions.py  per-Agent Conversation/Turn store + recovery          │
│ context.py   trusted history projection + model-capacity policy    │
│ tools.py     immutable ToolSpec + effective prototype Tool set      │
│ state.py     per-Agent durable semantic EventJournal               │
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
                              ├── durable → SQLite/WAL
                              └── live → memory → SSE → Browser
```

模型响应必须回到同一个 `HarnessKernel`，Tool result 也回到同一个 model session。
不存在 LangGraph/LangChain graph、第二套 hidden loop 或由前端拼接状态的旁路。

## 一次 Run

1. 浏览器用 bootstrap token 建立登录 session，通过 `POST /api/sessions` 新建持久
   Conversation，再以 CSRF 保护的 `POST /api/sessions/<conversation-id>/runs` 提交消息。
2. `CommandBus` 校验固定 Agent、Conversation 和消息边界；`RunService` 为现有
   Conversation 新建 Turn → Run 身份。`ConversationStore` 在 Agent 专属 `state.sqlite`
   中验证该会话没有 active Run，并读取带 revision、仅由 completed Turn 组成的完整
   user/assistant pairs 快照。受信 `ContextCompiler` 根据该历史、模型画像、Capsule
   generation、有效
   ToolSpec 和本轮消息编译不可变 ContextPlan。
3. Control Plane 先用 expected revision 做 compare-and-swap，再在一个 SQLite 事务中
   保存 `running` Turn、绑定 active Run 并写入 durable `run.started`；历史在编译期间
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
   Ollama Tool schema。模型可请求一次结构化
   `builtin/echo`，结果按相同 call ID 回流；随后 provider ToolSet 收窄为空，再进行
   下一轮模型调用。重复 Tool call 按协议错误拒绝。
9. Provider 在 Ollama 终帧报告 token usage；Control Plane 对照当前模型 profile 校验并
   累计，Worker 看不到也不能伪造 usage。所有 terminal 都由控制面附加 usage；若当前
   provider 轮次没有可校验终帧，则 `complete=false`，累计值可能只覆盖此前完整轮次。
10. Worker 发出无身份、逐事件的 `WorkerEvent`。Control Plane 验证 schema、状态转换、
   大小、durability 和唯一终态，然后补齐身份、时间和单调 `seq`。
11. durable 语义事件先写 Agent SQLite，随后进入 live window 和 SSE；delta 是
   ephemeral，不写盘。持久化失败时 Run 终止并诚实标记 memory-only failure。
12. 成功 terminal 与完整 assistant content 在同一事务把 Turn 转为 `completed` 并释放
    active Run；失败或取消只保存对应状态，不提交 partial assistant。terminal、取消、
    deadline 或故障后，supervisor 回收进程组并删除 Run root。Agent 的 Conversation、
    manifest、workspace、artifacts、journal 和 worker-env 保留。

## 组件职责

| 组件 | 拥有 | 不应拥有 |
| --- | --- | --- |
| `web.py` | HTTP/SSE、认证边界、session CRUD、请求/响应限制 | Agent loop、模型 transcript、任意路径 |
| `auth.py` | token、bounded session、CSRF | 用户业务状态 |
| `commands.py` | 类型化 start/cancel 命令 | request-derived endpoint/process options |
| `control.py` | Run、Worker、Broker 调度、ContextPlan ownership、event validator/sequencer | UI 投影、未隔离 Tool 实现 |
| `sessions.py` | Agent-scoped Conversation/Turn 事务、单 active Run、重启恢复和删除 | 模型 prompt、Worker 生命周期、Web principal |
| `worker.py` | 一个 Run 的 bootstrap 与 IPC | 网络 endpoint、持久状态、其它 Agent |
| `kernel.py` | context/model/tool/cancel/terminal 单一循环 | HTTP、SQLite、进程管理 |
| `context.py` | 确定性 sections/provenance、completed-history projection、动态预算与 plan digest | provider/network selection、语义 summary |
| `ollama.py` | 固定 endpoint/model、模型资格、per-Run transcript、ContextPlan 渲染、协议上限 | 浏览器可配置 provider |
| `tools.py` | 共享 `ToolSpec`、schema/限制/Tool set digest 和本地 Echo dispatch | Shell/Skill/MCP 动态执行 |
| `contracts.py` | command/event 类型与 canonical identity | 持久化策略 |
| `state.py` | durable semantic append/prune | token delta、完整 live Run truth |
| `capsule.py` | Agent/Run 路径、环境与清理所有权 | 产品级 Agent registry（尚未实现） |
| `sandbox.py` | fail-closed Worker kernel boundary | capability policy UI |

## 状态所有权

```text
Browser
  transient UI projection; never authoritative

Control Plane memory
  active Run state, complete immutable ContextPlan, validated provider usage,
  live events, cancellation, Worker identity
  max 4 active / 64 retained Runs

Worker memory
  one RunState, ContextPlan summary reference, open block, pending Tool, model iteration
  destroyed at terminal

data/agents/<agent-id>/
  manifest.json, workspace/, artifacts/
  state.sqlite[-wal|-shm]: Conversation, Turn and canonical durable events

.runtime/agents/<agent-id>/
  worker-env/, logs/, runs/<run-id>/
```

Conversation/Turn transcript 可由 SQLite 跨 Gateway 重启恢复；启动时任何遗留
`running` Turn 都由有界恢复 sequencer 校验并收敛：开放 block 先 discarded，requested-
only Tool 先补 started 再 finished，随后写唯一 `run.failed(control_restarted)`，Turn 转为
`interrupted` 并释放会话。durable `seq` 只在 open block 时允许出现可由未落盘 delta
解释的 gap；其它 gap、非法状态或超限输入整体回滚并 fail closed。旧 Worker 和模型连接
不会复活。当前仍没有 durable event replay/gap-repair API，也不会重建旧 Run；SSE
`Last-Event-ID` 只对当前 Gateway 内存中保留的 Run 有效，ephemeral delta 不跨进程恢复。

## Conversation、Turn 与 Run

```text
Agent Capsule
  └─ Conversation (durable, user-created/deleted)
       ├─ Turn 1 (durable user input + terminal status + completed answer?)
       │    └─ Run 1 (one isolated Worker/Ollama execution)
       └─ Turn 2
            └─ Run 2
```

- Conversation 是可列出、读取恢复和删除的持久容器，不持有 Worker。
- Turn 是一次已接受的用户消息及其 `running|completed|failed|cancelled|interrupted`
  状态；当前每个 Turn 只有一个 Run，不承诺 retry/branch 语义。
- Run 是独立的执行尝试，拥有 ContextPlan、Worker、OllamaRunSession、canonical events
  和临时 Run root；终态后所有执行资源被回收。
- 后续 ContextPlan 只读取 `completed` Turn 的完整 user/assistant pair。失败、取消、重启
  中断或 discarded/partial output 可供 UI 审计状态，但不成为模型历史。
- 一个 Conversation 同时最多有一个 active Run。活跃时发起下一轮或删除返回冲突；
  删除只在空闲时原子移除 Conversation、Turn 和关联 Run events，不按 ID 跨 Agent 删除。
- completed-history snapshot 的 revision 被绑定到 Turn 接受 CAS；并发请求不能让基于旧
  历史编译的 ContextPlan 在较新会话状态上启动。

## Claude Code 设计映射

| Claude Code 风格概念 | 当前对应 | 状态 |
| --- | --- | --- |
| 启动/环境初始化 | root lifecycle + host/model qualification | 已跑通 |
| UI / interactive surface | authenticated browser UI | 最小实现 |
| command system | `CommandBus` start/cancel | 最小实现 |
| Query/Agent engine | single `HarnessKernel` finite loop | 已跑通 |
| context assembly | trusted immutable `ContextPlan`、provenance/digest/budget、completed Turn 原生 chat roles | 多轮历史和动态完整-pair tail window 已进入真实请求；尚无 workspace 指令注入或语义 summary |
| model abstraction | `StreamingModel` + trusted Ollama Broker | 固定 provider/model |
| Tool registry/dispatch | shared immutable `ToolSpec` + `ToolRegistry` | 仅 Echo |
| state/history | durable Conversation/Turn + canonical event journal | transcript 可恢复；旧 Run/SSE 不恢复 |
| permissions/sandbox | broker boundary + Landlock/seccomp | 固定 Worker 路径已实现 |
| sub-agent/task system | parent/child Run、mailbox、scheduler | 未实现 |
| cost/context compaction | 动态模型画像、经控制面校验的 provider usage、80%/60% policy | 确定性 `completed-turn-tail-v1` 已实现；语义 summary/snapshot 未实现 |

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
- sandbox/host/model qualification 缺失即拒绝启动或 Run，不提供 unconfined fallback。

## 当前不支持

通用 Agent create/upgrade/delete、旧 Run/活跃 SSE 跨进程恢复、durable event replay、
语义 summary/snapshot、workspace `CLAUDE.md` 注入、文件工具、Shell、Skill、MCP、RAG、
artifact broker、权限交互、子智能体、多用户、TLS、远程部署、正式 observability 和
production release qualification 均未实现。路线见
[runtime rebuild plan](../plans/runtime-rebuild.md)。

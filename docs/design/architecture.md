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
│ web.py       auth, CSRF, Host/Origin, limits, static UI, SSE       │
│ commands.py  typed CommandBus                                      │
│ control.py   RunService, Worker supervisor, validator, sequencer   │
│ state.py     per-Agent durable semantic EventJournal               │
│ ollama.py    fixed, bounded model broker                           │
└──────────────┬────────────────────────────────────┬────────────────┘
               │ bounded versioned stdio NDJSON     │ trusted TCP only
               ▼                                    ▼
┌──────── one process per Run ───────┐       iollama:11434
│ worker.py                          │       qwen3.5:2b
│ close FDs → rlimits → sandbox      │
│ Landlock + seccomp + attestation   │
│                                    │
│ HarnessKernel (single finite loop) │
│   ContextCompiler                  │
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

1. 浏览器用 bootstrap token 建立 session，再以 CSRF 保护的 `POST /api/runs` 提交消息。
2. `CommandBus` 校验固定 Agent 和消息边界；`RunService` 新建
   Agent → Conversation → Turn → Run 身份并发布 durable `run.started`。
3. Capsule Manager 创建 `.runtime/agents/<agent-id>/runs/<run-id>/`，为该 Run 准备
   独立 HOME、TMP、XDG、input、work 和 output。
4. Control Plane 用 Agent 专属 `worker-env` 在新 process group 中启动 Worker，并
   原子发布强身份 PID record。
5. Worker 在读取消息前完成 FD isolation、rlimits、Landlock、seccomp 与 attestation；
   Control Plane 通过 `/proc` 复核后才发送命令。
6. `HarnessKernel` 编译固定 prototype sections，通过 stdio 请求受信 Model Broker。
   实际 Ollama system prompt 目前由 Broker 固定拥有；完整 Agent instructions/history/
   compaction 尚未穿过该边界。
7. Broker 为该 Run 独占一个 `OllamaRunSession`，调用固定 `qwen3.5:2b`。模型可请求
   一次结构化 `builtin/echo`，结果按相同 call ID 回流，再进行下一轮模型调用。
8. Worker 发出无身份、逐事件的 `WorkerEvent`。Control Plane 验证 schema、状态转换、
   大小、durability 和唯一终态，然后补齐身份、时间和单调 `seq`。
9. durable 语义事件先写 Agent SQLite，随后进入 live window 和 SSE；delta 是
   ephemeral，不写盘。持久化失败时 Run 终止并诚实标记 memory-only failure。
10. terminal、取消、deadline 或故障后，supervisor 终止/回收进程组；确认消失后整体
    删除 Run root。Agent 的 manifest、workspace、artifacts、journal 和 worker-env 保留。

## 组件职责

| 组件 | 拥有 | 不应拥有 |
| --- | --- | --- |
| `web.py` | HTTP/SSE、认证边界、请求/响应限制 | Agent loop、模型 transcript、任意路径 |
| `auth.py` | token、bounded session、CSRF | 用户业务状态 |
| `commands.py` | 类型化 start/cancel 命令 | request-derived endpoint/process options |
| `control.py` | Run、Worker、Broker 调度、event validator/sequencer | UI 投影、未隔离 Tool 实现 |
| `worker.py` | 一个 Run 的 bootstrap 与 IPC | 网络 endpoint、持久状态、其它 Agent |
| `kernel.py` | context/model/tool/cancel/terminal 单一循环 | HTTP、SQLite、进程管理 |
| `context.py` | 确定性 prompt sections 与 provenance | provider/network selection |
| `ollama.py` | 固定模型、per-Run transcript、协议上限 | 浏览器可配置 provider |
| `tools.py` | 项目拥有的结构化 Tool schema | Shell/Skill/MCP 动态执行 |
| `contracts.py` | command/event 类型与 canonical identity | 持久化策略 |
| `state.py` | durable semantic append/prune | token delta、完整 live Run truth |
| `capsule.py` | Agent/Run 路径、环境与清理所有权 | 产品级 Agent registry（尚未实现） |
| `sandbox.py` | fail-closed Worker kernel boundary | capability policy UI |

## 状态所有权

```text
Browser
  transient UI projection; never authoritative

Control Plane memory
  active Run state, live events, cancellation, Worker identity
  max 4 active / 64 retained Runs

Worker memory
  one RunState, open block, pending Tool, model iteration
  destroyed at terminal

data/agents/<agent-id>/
  manifest.json, workspace/, artifacts/, state.sqlite[-wal|-shm]

.runtime/agents/<agent-id>/
  worker-env/, logs/, runs/<run-id>/
```

当前 journal 不是完整 conversation database。服务重启后不会从 SQLite 重建 Run，也
没有 durable replay/gap-repair API。SSE `Last-Event-ID` 只对仍在 Control Plane 内存中
保留的 Run 有效。

## Claude Code 设计映射

| Claude Code 风格概念 | 当前对应 | 状态 |
| --- | --- | --- |
| 启动/环境初始化 | root lifecycle + host/model qualification | 已跑通 |
| UI / interactive surface | authenticated browser UI | 最小实现 |
| command system | `CommandBus` start/cancel | 最小实现 |
| Query/Agent engine | single `HarnessKernel` finite loop | 已跑通 |
| context assembly | typed `PromptSection` / `ContextCompiler` | 固定占位 |
| model abstraction | `StreamingModel` + trusted Ollama Broker | 固定 provider/model |
| Tool registry/dispatch | typed `ToolRegistry` | 仅 Echo |
| state/history | `RunState` + canonical event journal | 无完整会话恢复 |
| permissions/sandbox | broker boundary + Landlock/seccomp | 固定 Worker 路径已实现 |
| sub-agent/task system | parent/child Run、mailbox、scheduler | 未实现 |
| cost/context compaction | usage/cost budget、summary/snapshot | 未实现 |

参考资料提供结构启发，不是源码依赖或安全证明。来源和版本见
[Claude Code reference provenance](../../references/claude-code/PROVENANCE.md)。

## 资源与失败原则

- 事件、模型 frame/output、active Run、内存 retention、Run wall time、文件树和日志
  都有硬上限；超额使该 Run 失败，不扩容到无界状态。
- 取消和任何失败最终收敛到且只收敛到一个 terminal event；未闭合 block/tool 先以
  durable recovery event 闭合。
- Worker event 先校验再应用；Worker 不能自报 Agent/Run identity 或 sequence。
- durable event 先 append journal 再对 live consumer 可见。journal 不可用时停止
  Worker，不把 memory-only 事件伪装成 durable。
- sandbox/host/model qualification 缺失即拒绝启动或 Run，不提供 unconfined fallback。

## 当前不支持

通用 Agent create/upgrade/delete、跨重启 conversation、context compaction、文件工具、
Shell、Skill、MCP、RAG、artifact broker、权限交互、子智能体、多用户、TLS、远程部署、
正式 observability 和 production release qualification 均未实现。路线见
[runtime rebuild plan](../plans/runtime-rebuild.md)。

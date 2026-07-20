# Agent Builder repository guide

Agent Builder 是一个从零构建的、Claude Code 风格的本地智能体运行时。当前仓库只
包含绿地原型和 Claude Code 研究资料；旧系统已经隔离到 `_legacy-reference/`，不再
属于构建、启动、测试或设计边界。`AGENTS.md` 必须始终是指向本文件的符号链接。

## 当前事实

- Web Gateway 固定监听 `0.0.0.0:20815`，`GET /health` 是唯一无需登录的运行状态
  入口。监听器目前没有 TLS，只能部署在受信、防火墙保护的网络。
- 持久 Agent registry 支持 create/list/get/upgrade/delete；默认 UI 仍使用不可删除的
  prototype Agent `00000000-0000-4000-8000-000000000001`。每个 active generation 惰性
  创建独立 RunService/QueryEngineRegistry，所有 Agent 只共享同一个有界模型 Broker。
- Web UI 和受认证 API 支持会话的新建、列表、读取恢复和删除；同一会话可连续创建
  多个 Turn。公共路径是 `GET|POST /api/sessions`、`GET|DELETE /api/sessions/<id>` 和
  `POST /api/sessions/<id>/runs`。一个会话同时最多有一个 active Run；有活跃 Run 时
  新一轮和删除都会被拒绝，必须先取消或等待终态。
- Gateway 中存在一个有界 `QueryEngineRegistry`，为每个已打开的 Conversation 保持唯一
  逻辑 `QueryEngine`。它负责固定 Agent/Conversation 身份并提供 restore、submit、interrupt
  和 delete 入口；每次 submit 创建新的隔离 Run。Engine 不缓存 transcript、event、
  ContextPlan、模型 session 或 Worker 状态，SQLite/`RunService` 仍是唯一事实源。删除成功
  会 retire 并驱逐 Engine；Gateway 重启后按 conversation ID 从 SQLite 懒重建，不续跑旧
  Worker。
- 真实模型固定为 `iollama:11434` 上的 `qwen3.5:2b`；端点、模型和参数不能由
  浏览器或 Worker 覆盖。启动资格检查从 Ollama `/api/show` 读取原生上下文窗口；
  当前模型报告 `262144` tokens，受信运行策略将实际窗口封顶为 `32768`，预留
  `2048` 输出 tokens，因此硬输入预算为 `30720`。
- Conversation、Turn 和 Run 是不同生命周期：Conversation 是持久容器，Turn 保存一次
  用户提交及其终态，Run 是该 Turn 的独立 Worker 执行。会话和 Turn 位于 Agent 专属
  `state.sqlite`；Gateway 重启会把遗留 `running` Turn 标为 `interrupted`，不会复活旧
  Worker 或模型流。只有 `completed` Turn 的完整 user/assistant pair 会进入后续历史；
  failed、cancelled、interrupted 及任何 partial output 只用于 UI 状态，不进入模型上下文。
  ContextPlan 编译使用带 revision 的历史快照，Turn 接受以 CAS 拒绝编译期间发生的漂移。
- 受信 Control Plane 为每个 Run 从 platform contract、当前 generation 的 Agent instructions、
  已完成会话历史和本轮 user turn 编译不可变 ContextPlan，并由同一 `ToolSpec` 生成模型
  Tool schema 和执行校验。完整 plan 不进入 Worker；Worker/Model Broker IPC v2 只传
  `plan_id`、plan/toolset digest 等引用。workspace `CLAUDE.md` 尚未接入。
- 当前 admission estimator 是 `utf8-bytes-upper-bound-v1`：在没有模型专用 tokenizer
  时按每个 UTF-8 byte 一个 token 做保守上界，并计入实际 section renderer、Tool manifest
  与 `256` tokens 的 provider template reserve。未来压缩决策必须优先使用实际模型
  profile 和经过 Control Plane 校验的 provider usage；每个新 Turn 和同一 Run 内的每次
  provider 调用都对完整渲染 transcript 做硬预算 admission。达到动态 80% 阈值时，
  ContextCompiler 只按完整的已完成 Turn pair 从最旧端移出，直到不高于 60% 目标或没有
  可移出的 pair；持久 transcript 不删除。这是确定性的 tail-window，不是语义摘要，也
  不会编造被省略内容。受保护内容仍超过硬预算时 fail closed。
- 每个 Run 启动一个独立 Worker 进程。Worker 使用 Agent 专属虚拟环境，并强制
  进入 Landlock、seccomp、rlimit、`no_new_privs` 和父进程死亡联动边界。
- Worker 无网络、不能创建子进程、不能直接写文件。目前唯一工具是输入/结果均不超过
  `8192` UTF-8 bytes 的只读 `builtin/echo`。TurnRuntimeSnapshot 固化最多 4 次模型调用、
  2 次顺序 Tool 调用及 usage/deadline；第二次结果回流后 ToolSet 收窄为空。不得把原型
  描述为支持任意 Shell、Skill、MCP 或文件编辑。
- Model Broker 全局最多同时打开 2 路 provider stream；其它请求只能在最多 4 个 active
  Run 的边界内有界等待，30 秒仍未取得 slot 时以可重试 `model_busy` 失败。
- Provider 最多 4096 个原始流帧会被合并为最多 128 个 content IPC frame；Broker 的
  受信错误 code/retryability 由 Control Plane 绑定到 canonical failure，Worker 不能改写。
- `state.sqlite` 使用 WAL，只在会话/Turn 状态和 durable 语义事件边界写入；流式 delta
  保持内存态，不逐 token 落盘。认证的 `/api/runs/<id>/replay` 和 events endpoint 的
  durable fallback 会在返回前严格验证完整有界 Run、canonical payload/ToolSpec、显式 gap
  与 digest-bound UI snapshot。每次真实 provider 调用只允许一个 durable
  `model.request.started`/`model.response.finished` pair，不把 prompt、provider frame 或 token
  delta 写入 journal。时间线可按 Conversation 的 Turn/Run 切换，点击项看到的是完整
  canonical event envelope，不得称为原始 provider 报文。认证的
  `/api/runs/<id>/context` 只提供 no-store、正文隐藏的 ContextPlan/section 元数据或历史
  `run.started` 摘要，不能成为第二份 prompt 持久状态。会话详情和已收敛 durable timeline
  可跨 Gateway 重启恢复，但旧 Worker/模型连接、活跃 SSE 的无缝续接和 ephemeral delta
  不能跨进程恢复。journal 中途故障在存储仍可写时会删除 partial prefix 并留下明确不可
  回放、零 event/byte 的有界 tombstone，不伪造完整历史。
- LangGraph、LangChain 和旧系统均不在新运行时依赖图中。

权威架构见 [docs/design/architecture.md](docs/design/architecture.md)，安全边界见
[SECURITY.md](SECURITY.md)，当前缺口见
[docs/plans/runtime-rebuild.md](docs/plans/runtime-rebuild.md)。

## 快速命令

```bash
./bootstrap.sh                 # 安装固定版本、仅位于 checkout 内的 Python 工具链
./bootstrap.sh --offline       # 只使用 checkout 内已有缓存
./start.sh                     # 资格检查后启动整套当前系统
HARNESS_V2_CONTEXT_REVEAL=1 ./start.sh  # 显式启用独立授权/审计的脱敏诊断正文
./start.sh --qualification-sync-count  # 仅独占 RR：低写放大 libc sync-call 插桩
./stop.sh                      # 停止受管 Gateway 和所有已验证 Worker
./stop.sh --force              # 缩短优雅退出期限，仍执行进程身份校验
./set-access-token.sh          # 隐藏输入、原子轮换 token 并重启 Gateway
./purge.sh --help              # 查看清理范围
./governance.sh                # 文档、路径、依赖和仓库边界治理
```

`purge.sh` 的范围是 `cache|logs|environments|data|dependencies|runtime|all`，执行
删除必须显式传入 `--yes`。`data` 和 `all` 会删除不可重建的 Agent 状态；操作前必须
确认目标和恢复方式。

开发和验证命令必须先加载受控环境：

```bash
source ./env.sh
./.venv/bin/python -m pytest
./governance.sh
```

不得使用系统 `pip`、全局虚拟环境、Conda、用户目录缓存或 `/tmp` 保存项目状态。

## P1-P8：项目核心原则

- **P1 — 高质量运行手册。** `CLAUDE.md` 是编码智能体的简明事实源，并由可执行
  文档治理保证持续准确；不能把易变细节复制到多个文档。
- **P2 — 单一指令入口。** `AGENTS.md` 必须存在且只能是相对符号链接
  `AGENTS.md -> CLAUDE.md`，两者不得形成可漂移副本。
- **P3 — 完整生命周期。** 根目录 `start.sh` 和 `stop.sh` 一键启动、停止当前整套
  服务；失败启动必须回滚，停止必须处理每个已验证 Worker，不能按端口误杀进程。
- **P4 — 部署不出 checkout。** 源码、解释器、依赖、数据、日志、PID 和密钥全部
  位于当前工作目录，不创建或依赖其它目录中的项目安装。
- **P5 — 运行不污染主机。** HOME、TMP、XDG、Python/uv 缓存和 Agent 虚拟环境均
  重定向到 `.runtime/` 或 `.tools/`；持久数据只进入 `data/`。
- **P6 — 安全、稳定和 SSD 友好。** 所有输入均不可信；限制内存、进程、网络、
  文件系统、事件和日志。禁止逐 token 落盘、无界日志、频繁全树扫描和不受控 sync。
- **P7 — 固定 Web 入口。** 用户界面监听 `0.0.0.0:20815`，并有认证、CSRF、
  Origin/Host 校验、请求上限和安全响应头；无 TLS 时不得暴露到不受信网络。
- **P8 — Agent 强隔离和可清除。** 每个 Agent 拥有独立数据目录、运行目录和虚拟
  环境；每个 Run 有独立沙箱目录和 Worker。未来删除 Agent 时必须完整清除其
  Capsule 且无进程、文件、环境或状态残留；当前通用删除流程尚未实现。

更完整的验收解释见 [docs/PRINCIPLES.md](docs/PRINCIPLES.md)。任何设计如果与
P1-P8 冲突，必须先记录决策并更新原则，而不是静默绕过。

## 目录所有权

```text
src/agent_builder_v2/       当前唯一运行时源码
tests/                      当前运行时测试
scripts/                    生命周期内部辅助程序
data/agents/<agent-id>/     Agent 会话/Turn、workspace、artifacts、SQLite/WAL journal
.runtime/control-plane/     Gateway PID、锁和有界轮转日志
.runtime/secrets/           登录密钥
.runtime/agents/<agent-id>/ Agent 虚拟环境和临时 Run 根
.tools/                     checkout-local uv 引导工具
.venv/                     控制面 Python 环境
.runtime/python|cache/      managed Python 与 uv/pip/bytecode 缓存
docs/                       原则、设计、治理和活动计划
references/claude-code/     只读研究资料及来源说明
_legacy-reference/          完全隔离的旧系统快照；非当前项目内容
```

`.runtime/`、`.tools/`、`data/` 的生成状态和 `references/claude-code/materials/`
不得提交。旧归档仅在用户明确要求历史调查时读取；当前源码、测试、脚本、文档和
运行时不得 import、执行、扫描、链接或复制其中内容。

## 架构边界

- `web.py`：HTTP/SSE、认证调用、输入限制和响应安全头；不承载 Agent 循环。
- `commands.py`：用户命令的类型化入口；不得让请求数据直接配置进程或模型。
- `query_engine.py`：Conversation-scoped 逻辑编排、不可变 Run handle、身份校验和有界
  Engine identity map；不得复制持久历史、事件或另建 Agent loop。
- `control.py`：Run 生命周期、Worker supervisor、Model Broker 调度、事件校验和
  canonical sequencing；这是受信控制面。
- `worker.py`：每 Run 进程入口；先完成 FD 清理、资源限制和沙箱握手，再读取消息。
- `kernel.py`：唯一 Agent 主循环，拥有 context → model → tool → model 状态机。
- `context.py`：受信、确定性、带来源的 ContextPlan、模型画像、完成历史和动态预算；
  以完整 completed Turn pair 做 tail-window，不执行语义 summary。
- `ollama.py` / `model.py`：受信 Ollama 代理和有界 Worker IPC。Worker 不得知道
  模型地址或自行联网。
- `tools.py`：项目拥有的不可变 `ToolSpec`、共享 schema/限制和执行校验。目前只允许
  `builtin/echo`。
- `sessions.py`：Agent-scoped Conversation/Turn 存储、单 active Run、恢复和删除事务。
- `contracts.py`：规范 command/event envelope 与 canonical identity。
- `replay.py`：durable payload/state validator、确定性 UI projector 与 snapshot codec。
- `state.py`：durable 语义 journal、完整 Run 验证、分页 replay 和 snapshot-only retention。
- `capsule.py`：Agent/Run 路径、环境和生命周期所有权；路径必须保持 checkout 内。
- `sandbox.py`：Linux fail-closed Worker confinement；不得添加未隔离降级路径。

Worker 只能发出无身份的 `WorkerEvent`。只有受信控制面可以校验状态转换、补齐
Agent/Conversation/Turn/Run 身份、分配单调 `seq` 并写入 durable journal。保持
thinking/content、Tool、取消和终态事件的增量、有序语义；协议变更必须同步更新
[docs/design/event-protocol.md](docs/design/event-protocol.md)。

## 安全和资源规则

1. 文件名、路径、Agent/Run ID、Host/Origin、JSON、模型帧和 Worker 帧均视为不可信。
2. 不得把登录 token 放入 URL、前端可读变量、环境变量、命令参数、日志、trace 或错误
   响应。默认 token 是 256-bit 随机值；显式运维 token 只能通过 `set-access-token.sh`
   的隐藏 TTY 输入轮换，弱 token 只适用于受信、防火墙保护网络。
3. 除 `/health` 和静态页面资源外，业务 API 必须维持登录 session 认证；会话创建、删除、
   发起 Turn 和取消 Run 等状态修改要求 CSRF。当前是共享 bootstrap token 的 single-
   operator 模型，不得声称存在多用户/租户隔离。
4. Model Broker 只连接固定、经资格检查的 Ollama 地址；不得接受请求级 endpoint、
   model、窗口或生成参数。上下文窗口必须从受信模型资格结果和运行策略推导，不能
   相信浏览器、Agent 或 Worker 声明。
5. Worker 只有已继承的有界标准流 capability；不得重新开放 socket、fork、exec、
   mount、namespace、持久 IPC 或直接文件写权限。
6. 新能力必须通过受信 broker、显式 schema、最小权限、超时、字节/条目/并发上限和
   负面安全测试；不能用“开发模式”绕过沙箱。
7. durable 事件和 Conversation/Turn 转换只在语义边界写 SQLite/WAL；
   `assistant.block.delta` 保持 ephemeral。不得反复重写完整历史、逐 token 落盘或在每次
   会话删除后执行 `VACUUM`；日志必须批量刷新、轮转并有总大小上限。
8. 清理进程前校验 owner-only PID record、PID/PGID/start marker、exact argv、cwd、checkout
   和 supervisor→Web→Worker 父链；non-dumpable Worker 仍必须证明直接属于已验证 Web
   child。先停 Worker 再停 Gateway；端口占用不是杀进程权限。

## 测试策略

先运行最小相关测试，再运行完整门禁：

```bash
source ./env.sh
./.venv/bin/python -m pytest tests/test_query_engine.py
./.venv/bin/python -m pytest tests/test_web.py
./.venv/bin/python -m pytest tests/test_worker_integration.py
./.venv/bin/python -m pytest
./governance.sh
```

改动认证、路径、事件、Worker、沙箱、生命周期或持久化时，必须增加失败路径测试。
生命周期改动还要验证 cold start、`/health`、真实 `qwen3.5:2b` Run、正常/强制停止、
stale/伪造 PID 记录以及无残留。测试产物只能写入 `.runtime/`。

## 文档治理

[docs/DOCUMENTATION.md](docs/DOCUMENTATION.md) 定义文档所有权、metadata、触发式
更新、季度审查和废弃流程。行为、命令、端口、路径、协议、安全边界、资源上限或
用户流程改变时，必须在同一个变更中更新对应权威文档。活动工作同步维护
[docs/plans/runtime-rebuild.md](docs/plans/runtime-rebuild.md)。

Claude Code 材料只能用于设计研究，必须先读
[references/claude-code/README.md](references/claude-code/README.md) 和
[references/claude-code/PROVENANCE.md](references/claude-code/PROVENANCE.md)；参考实现
不是本项目的安全证明，也不是可直接复制的依赖。

## DoD（完成定义）

一个变更只有同时满足以下条件才算完成：

- 实现、失败行为、必要的迁移/清理或回滚均已完成，且不依赖旧系统；
- 最小相关测试、完整 `pytest` 和 `./governance.sh` 通过；
- 对外行为、协议、架构、安全和活动计划文档与代码一致；
- `AGENTS.md -> CLAUDE.md` 保持有效，P1-P8 没有被绕过；
- 没有 checkout 外写入、秘密泄露、无界内存/磁盘增长、逐 token 落盘或明显 SSD
  磨损路径；
- 新的执行能力经过 fail-closed confinement、资源上限、取消、超时和负面测试；
- 服务可由根脚本完整启动、健康检查、停止，且没有遗留受管进程或 Run 临时目录；
- 未实现能力仍被明确标记，不能把 walking skeleton 宣称为生产就绪。

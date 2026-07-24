# Agent Builder repository guide

Agent Builder 是一个从零构建的、Claude Code 风格的本地智能体运行时。`0.2.0` 是
GNU/Linux x86_64 上受支持的 single-operator、本地优先 release；当前仓库只包含该运行时
和 Claude Code 研究资料。旧系统已经隔离到 `_legacy-reference/`，不再
属于构建、启动、测试或设计边界。`AGENTS.md` 必须始终是指向本文件的符号链接。

> **Context reliability qualification（2026-07-21）**：`CTX-R01-01..15` 已完成并重新关闭
> context/release Gate。类型化计数域、completed Turn bundle、continuation invariant、Tool
> headroom、单次 overflow recovery、durable next-turn preview 和 semantic-summary-v2 已纳入
> 当前 release；完整合同和证据见
> [completed remediation plan](docs/plans/context-reliability-remediation.md)。

> **Repetition containment qualification（2026-07-24）**：`REP-R03-01..08` 已完成并重新
> 关闭 context/release Gate。受信 sampling、1 MiB raw stream budget、有界 exact-suffix
> detector、立即 Provider close、`repetition_truncated` completed/incomplete-usage 合同和
> marker-bound continuation 已纳入当前 release；稳定证据见
> [runtime rebuild plan](docs/plans/runtime-rebuild.md#rep-r03--model-repetition-containment-and-continuation)。

## 当前事实

- Web Gateway 固定监听 `0.0.0.0:20815`，`GET /health` 是唯一无需登录的运行状态
  入口。监听器目前没有 TLS，只能部署在受信、防火墙保护的网络。
- 持久 Agent registry 支持 create/list/get/rename/upgrade/delete；左侧 Agent 管理区域支持
  创建、切换、重命名和删除普通 Agent。rename 通过可恢复 `renaming` 状态原子更新 registry
  与 manifest，不改变 generation、解释器或 runtime admission；generation upgrade 只位于
  高级操作，用于重建基础运行环境。固定 ID `00000000-0000-4000-8000-000000000001` 是
  不可重命名、不可升级、不可删除的正式系统 Agent。每个 active generation 惰性创建独立
  RunService/QueryEngineRegistry，所有 Agent 只共享同一个有界模型 Broker。
- Web UI 和受认证 API 支持会话的新建、确定性首轮标题、显式重命名、分支/续接、分页读取
  恢复和删除；同一会话可连续创建多个 Turn。详情默认只返回最近 32 个 Turn，客户端只能
  使用绑定 Agent/Conversation/revision 的认证 opaque cursor 向前分页；非成功 Turn 返回不含
  Provider 原文的有界 terminal summary。上下文准备提供 prompt-free 阶段查询和独立取消
  control path：`GET` 在 idle 时返回 `operation_id=null`，在 preparing/cancelling 时返回同一
  个稳定 ID，且该 ID 原样跨越 preparation→Run handoff。取消必须提交从 `GET` 取得的 exact
  ID；`202` 只确认取消请求已受理，客户端必须继续查询到 idle 才能声称收敛。旧 ID 返回
  `stale`，不得取消后来启动的准备操作，从而阻断 ABA。该路径与 handoff 共用信号，不能
  排队等待长摘要完成。
  Web 使用 `/api/agents/<agent-id>/sessions/...` 和
  `/api/agents/<agent-id>/runs/...` 显式绑定当前 Agent；无 Agent ID 的旧路径只兼容系统
  Agent。一个会话同时最多有一个 active Run；有活跃 Run 时该会话的新一轮、分支和删除
  会被拒绝，但其它会话仍可浏览、编辑草稿或独立运行。
- Web UI 采用 conversation-first 壳层：Agent/会话位于左侧导航，中央是 durable Turn 对话
  与固定输入区，四泳道 Run 回放、事件消息体和 ContextPlan 检查位于默认关闭的右侧检查器。
  点击 Turn 才按需打开对应 Run；窄屏导航与检查器互斥，不得为了简化默认界面删除审计事实。
  Conversation 消息只在用户跟随最新内容时自动吸底，用户向上回看后不得强制抢回滚动位置；
  active Run、准备请求、草稿、模型和 compact 选择均按 Conversation 隔离，后台 SSE 只能
  更新所属会话，切回后恢复实时投影。assistant 完成消息使用不执行原始 HTML、不加载远程
  资源且只允许 http/https 外链的受限 DOM Markdown；复制、失败重试预填和 completed-head
  分支均为显式用户动作，不自动重复提交。
  composer 从认证、`no-store` 的服务端 next-turn preview 显示当前 committed state 的固定
  上下文、误差、压缩前余量和硬余量；浏览器不再用 Provider actual 或 byte admission 上界
  自行相减。failed/cancelled/interrupted 后继续绑定最后 completed revision，recovery 绑定
  active plan，model/ToolSet/generation/prompt 漂移会重算或明确显示 unavailable。本轮所有
  Provider 调用累计 input/output 只作为弱化的计算量指标，绝不冒充下一轮上下文。
- Web 输入边界在 Turn admission 前解析版本化 Slash Command；`/status`、`/context`、
  `/model`、`/compact`、`/permissions`、`/cancel`、`/clear` 只调用已有受信服务，响应固定
  标记不调用模型、不创建 Turn。registry/help 位于认证 `/api/commands`；命令 POST 与所有
  修改态保持 CSRF。Web 使用 Agent-scoped Command 路径；model/compact 只返回下一轮 Web
  effect，不另建服务端偏好状态。
- Gateway 中存在一个有界 `QueryEngineRegistry`，为每个已打开的 Conversation 保持唯一
  逻辑 `QueryEngine`。它负责固定 Agent/Conversation 身份并提供 restore、submit、interrupt
  和 delete 入口；每次 submit 创建新的隔离 Run。Engine 不缓存 transcript、event、
  ContextPlan、模型 session 或 Worker 状态，SQLite/`RunService` 仍是唯一事实源。删除成功
  会 retire 并驱逐 Engine；Gateway 重启后按 conversation ID 从 SQLite 懒重建，不续跑旧
  Worker。提交锁只保护短暂 ownership 变更；长时间上下文准备不持锁，第二次提交立即返回
  Conversation conflict，不能在后台静默排队。
- 真实模型固定为 `iollama:11434` 上的 `qwen3.5:2b`；端点、模型和参数不能由
  浏览器或 Worker 覆盖。启动资格检查从 Ollama `/api/show` 读取原生上下文窗口；
  当前模型报告 `262144` tokens，受信运行策略将实际窗口封顶为 `32768`，预留
  `4096` 输出 tokens，因此硬输入预算为 `28672`。普通无 Tool response 固定使用
  `temperature=1.0/top_p=0.95/top_k=20/presence_penalty=1.5/seed=0`，Tool selection 固定
  `temperature=0/seed=0`；两者均由受信 generation policy 固化并进入 profile digest。
  每个 ModelCatalog entry 同时固定有界
  queue/first-frame/stream-idle/turn timeout；只有首帧前零输出 timeout 可透明重试一次，
  部分输出、idle timeout 和 turn deadline 不得重试。每个实际 HTTP attempt 只产生一对
  无 endpoint/prompt/正文的有界 transport 诊断；同 profile 连续两个零首帧失败后开启 30 秒
  短路，成功首帧立即复位，健康表最多 16 项且 Gateway 重启清空。
- Conversation、Turn 和 Run 是不同生命周期：Conversation 是持久容器，Turn 保存一次
  用户提交及其终态，Run 是该 Turn 的独立 Worker 执行。会话和 Turn 位于 Agent 专属
  `state.sqlite`；Gateway 重启会把遗留 `running` Turn 标为 `interrupted`，不会复活旧
  Worker 或模型流。只有 `completed` Turn 的完整 `CompletedTurnContext` bundle 会进入后续历史；
  failed、cancelled、interrupted 及任何 partial output 只用于 UI 状态，不进入模型上下文。
  ContextPlan 编译使用带 revision 的历史快照，Turn 接受以 CAS 拒绝编译期间发生的漂移。
- 受信 Control Plane 为每个 Run 从 platform contract、当前 generation 的 Agent instructions、
  已完成会话历史和本轮 user turn 编译不可变 ContextPlan，并由同一 `ToolSpec` 生成模型
  Tool schema 和执行校验。完整 plan 不进入 Worker；Worker/Model Broker IPC v2 只传
  `plan_id`、plan/toolset digest 等引用。workspace `CLAUDE.md`、有界 Git 状态和 UTC
  环境快照已经接入受信 section registry。
- Context capacity 使用四个不可混用的版本化域：UTF-8/provider-schema
  `AdmissionUpperBound` 只做硬安全准入，request-bound `ProviderObservedUsage` 只描述实际
  调用，校准且带误差的 `SoftContextEstimate` 只做软压缩，`NextTurnProjection` 只描述当前
  committed state。压缩从最旧端移动完整 `CompletedTurnContext` bundle；canonical SQLite
  transcript 不变。soft calibration 只重放同 model profile/renderer/ToolSet/policy scope 的
  最近 16 条完整 SQLite Provider observation，因此 preview、admission、自动压缩和 Gateway
  重启使用同一依据；incomplete/zero usage 不参与。合法 Turn 在首次 Provider 调用前还必须通过 continuation preflight，Tool
  result 依剩余 headroom 选择 full/excerpt/receipt，避免执行后无处回流。
- `semantic-summary-v1` 永久禁止生成，只保留严格旧 codec。operator-owned
  `HARNESS_V2_SEMANTIC_SUMMARY_V2=0|1` 在进程启动前固定 v2；默认 `1` 已通过当前 release
  资格。v2 用独立可信 system prompt 和低权限 user/data source，ToolSet 为空；projection
  conversation-scoped 持久复用并支持 parent + delta 增量，失败安全回退 deterministic
  collapse。SQLite canonical transcript 始终是权威记录；紧急回滚设为 `0`，保留 decoder、
  新表和历史数据，不做 destructive migration。
- 每个 Run 启动一个独立 Worker 进程。Worker 使用 Agent 专属虚拟环境，并强制
  进入 Landlock、seccomp、rlimit、`no_new_privs` 和父进程死亡联动边界。
- Worker 无网络、不能创建子进程、不能直接读写 workspace。当前 EffectiveToolSet 是
  `file/stat`、`file/read_text`、`file/glob`、`file/grep`、`file/edit`、
  `file/write`、`exec/run`；文件和执行能力经有界 IPC 交给 Control Plane。mutation 始终要求 same-Run 完整
  read/absence receipt、精确 diff 审批和 descriptor-anchored atomic commit，不向 Worker
  授予文件描述符。TurnRuntimeSnapshot 固化最多 4 次模型调用、2 次顺序 Tool 调用及
  usage/deadline；第二次结果回流后 ToolSet 收窄为空，Broker 在最后一个 Tool result 之后
  追加固定、受信且不插值结果内容的 finalization system marker，要求模型只返回普通文本。
  `builtin/echo` 只保留在密封 catalog 中用于原型测试和历史 journal 回放，不得进入新 Run
  的 release policy。`exec/run` 支持固定
  `runtime-compile` 与 builtin-only `bounded-bash`，始终审批；后者的显式 parser 只接受
  `printf|pwd|true|false` 单命令，拒绝 expansion/pipe/redirection/env/subshell/glob/rc。
  两者在 PIDFD 监督的无 fork/network singleton Landlock+seccomp domain 内运行。认证管理面
  还能把同一受限命令显式提交为 durable
  background Task；Task 绑定 Agent generation 与 parent Run，独占 `tasks/<task-id>` 根，
  只在 queued/running/terminal 边界写 SQLite，取消、Gateway 重启、Conversation/Agent 删除
  均不重放且先清根再发布终态。不得把受限命令、Skill 或 extension 描述为支持任意
  Shell、package 或 MCP。
- 受信 Agent 指令要求默认直接回答；只有回答依赖外部/workspace 状态，或用户明确要求执行
  操作时才调用 Tool。创作与上下文内自包含问题不得触发 Tool，以避免小模型生成无意义的
  buffered Tool call；算术表达式明确使用常规运算优先级并在回答前复核。这只约束其余已授权
  能力的选择策略。无业务价值的 Echo 已在受信 release policy 层移除，不能仅依赖提示词约束
  小模型。
- Model Broker 全局最多同时打开 2 路 provider stream；其它请求只能在最多 4 个 active
  Run 的边界内有界等待，30 秒仍未取得 slot 时以可重试 `model_busy` 失败。
- `extension/call` 提供 MCP/LSP 共用的 permission/result adapter，但 release catalog 默认为空，
  因而不会进入 EffectiveToolSet。只有 operator 构造期注入的最多 8 个固定 HTTPS JSON-RPC
  spec 才可启用；endpoint/method/DNS IP 被固定，SSRF/private DNS/credential/redirect/frame
  漂移 fail closed。本地 stdio 和请求级 endpoint 永远不能开启，扩展进程不继承 token。
- 认证、CSRF 的 Agent-scoped Skill API 安装/升级/删除 digest-bound 两文件 zip；manifest v1
  只允许 `skill.json + main.py`、空 dependency/capability list，首版 binary dependency allowlist
  因而为空，禁止 sdist/build hook/runtime install。每 Skill 有 Capsule 内独立 `--copies`
  无 pip venv；只有显式安装时 `skill/run` 才进入后续 Turn ToolSet，且始终 ask。执行复用
  singleton Landlock/seccomp/rlimit，网络、fork、exec、workspace 和跨 Agent 访问均拒绝。
- Agent 管理抽屉可显式安装/删除固定 `research-documents` 环境。它持久位于当前 Agent 的
  `data/.../dependencies/` 与 `.runtime/.../dependencies/`，所以跨 Conversation 复用、跨
  Agent 不共享；只允许 checkout-local uv 安装精确版本 binary wheels，禁止 sdist、build
  hook、请求指定包和运行期安装。安装后 `document/extract_text` 才进入未来 Turn ToolSet；
  PDF/DOCX/HTML/Markdown/text 由 singleton Landlock/seccomp runner 有界解析，运行期无网络、
  只读该 Agent 的环境和一次性 Run staging，terminal 后删除文档副本。环境变更先 fence
  Agent runtime，active Run 时拒绝；删除环境或 Agent 必须清理完整 package/env 根。
- `agent/delegate` 是 ask-only brokered capability：父侧只有绑定 parent Run 的 durable
  Task、最多两条各 4 KiB mailbox message 和最多 8 KiB answer；child 必须是另一个 active
  Agent，并在自己的 Capsule/Conversation/Run/Worker/sandbox 中执行。全局最多 2 个、每父
  Run 1 个、depth 1、wall 45 秒；取消、重启、会话/Agent 删除必须收敛 child 且不得自动
  重放。不得用普通模型文本伪造 mailbox，也不得把 child workspace/environment 权限交给父侧。
- Provider 每次流最多 1 MiB/4096 个原始帧，并合并为最多 128 个 content IPC frame。
  普通无 Tool 回答若在有界尾窗内出现至少 3 份、累计至少 512 bytes 的精确 suffix cycle，
  Broker 会截掉尚未发出的重复尾部、追加一次固定 marker 并立即关闭 HTTP stream；Turn 以
  `run.completed.reason=repetition_truncated` 提交，Provider usage 明确为 incomplete 且不参与
  soft calibration。下一轮仍从该 completed bundle 和新 revision 重新执行硬 admission；软
  preview 在取得同 scope 完整 observation 前显示 unavailable，不使用中止调用的零值猜数。
  Broker 的受信错误 code/retryability 由 Control Plane 绑定到 canonical failure，Worker
  不能改写。
- `state.sqlite` 使用 WAL，只在会话/Turn 状态和 durable 语义事件边界写入；流式 delta
  保持内存态，不逐 token 落盘。认证的 `/api/runs/<id>/replay` 和 events endpoint 的
  durable fallback 会在返回前严格验证完整有界 Run、canonical payload/ToolSpec、显式 gap
  与 digest-bound UI snapshot。每个逻辑 provider 调用固定有一个 durable
  `model.request.started`/`model.response.finished` pair；其每个实际 HTTP attempt 另有一个
  `model.transport.attempt` started/finished pair。若实际尝试数为 `N`，该逻辑调用固定产生
  `2 + 2*N` 个 durable 事件，不把 prompt、provider frame、token delta 或 timer tick 写入
  journal。时间线可按 Conversation 的 Turn/Run 切换，点击项看到的是完整
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
发布范围、平台矩阵、备份/回滚和资格门禁见
[docs/design/release.md](docs/design/release.md)。

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
./backup.sh <backup-id>        # stop 后创建 checkout-local 私有 data 备份
./restore.sh backups/<id>.tar --yes  # stop、完整校验并原子恢复 data
./purge.sh --help              # 查看清理范围
./governance.sh                # 文档、路径、依赖和仓库边界治理
./release.sh RR-QUA-YYYYMMDD-NN  # 完整测试、SBOM、真实模型资格和 source artifact
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
  环境；每个 Run 有独立沙箱目录和 Worker。删除 Agent 会先 drain，再完整清除其
  Capsule，并以恢复协议保证无进程、文件、环境或状态残留。

更完整的验收解释见 [docs/PRINCIPLES.md](docs/PRINCIPLES.md)。任何设计如果与
P1-P8 冲突，必须先记录决策并更新原则，而不是静默绕过。

## 目录所有权

```text
src/agent_builder_v2/       当前唯一运行时源码
tests/                      当前运行时测试
scripts/                    生命周期内部辅助程序
data/agents/<agent-id>/     Agent 会话/Turn、workspace、artifacts、SQLite/WAL journal
                            以及持久 dependency bundle source/metadata
.runtime/control-plane/     Gateway PID、锁和有界轮转日志
.runtime/secrets/           登录密钥
.runtime/agents/<agent-id>/ Agent 虚拟环境和临时 Run/Task 根
                            以及可重建、Agent 专属 dependency venv
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
- `context.py`：受信、确定性、带来源的 ContextPlan、模型画像、completed Turn bundle、
  continuation preflight 和类型化动态预算；生成 deterministic collapse，并校验/渲染由
  受信 Broker 提供的 v2 summary snapshot，自身不发起模型调用。
- `completed_context.py` / `context_counts.py` / `semantic_summary_v2.py`：分别拥有完成 Turn
  历史合同、四种计数域与低权限 summary v2 schema；不得复制 canonical transcript。
- `ollama.py` / `model.py`：受信 Ollama 代理和有界 Worker IPC。Worker 不得知道
  模型地址或自行联网。
- `tools.py`：项目拥有的不可变 `ToolSpec`、共享 schema/限制和执行校验；可选能力只有
  在其可信环境/registry 存在时才进入 EffectiveToolSet。
- `sessions.py`：Agent-scoped Conversation/Turn 存储、单 active Run、恢复和删除事务。
- `contracts.py`：规范 command/event envelope 与 canonical identity。
- `replay.py`：durable payload/state validator、确定性 UI projector 与 snapshot codec。
- `state.py`：durable 语义 journal、完整 Run 验证、分页 replay 和 snapshot-only retention。
- `capsule.py`：Agent/Run 路径、环境和生命周期所有权；路径必须保持 checkout 内。
- `tasks.py`：有界 background Task 状态、通知、取消/重启恢复和 singleton 执行调度；
- `subagents.py`：parent Task/link、双向 bounded mailbox、child Run admission、循环/并发/
  deadline fence，以及 cancel/delete/restart 收敛；
  不拥有 shell grammar、任意进程或第二套 Agent loop。
- `skills.py`：Skill archive/manifest/version/integrity、Agent 专属环境与启停删除；文件出现或
  prompt 声明不能建立信任，只有认证安装事务可以。
- `research.py` / `research_bundle.py`：固定研究依赖的 Agent-scoped 原子生命周期、
  descriptor-anchored 文档 staging 和无网络 sandbox parser；不得扩展为模型可控 pip。
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
- 未实现能力仍被明确标记，不能把本地 single-operator 支持范围扩大成互联网、多租户、
  任意代码执行或未资格平台承诺。

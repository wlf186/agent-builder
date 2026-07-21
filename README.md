# Agent Builder

Agent Builder 是一个从零构建的 Claude Code 风格智能体运行时。`0.2.0` 是首个受支持的
single-operator、本地优先版本：认证 Web UI、类型化命令、规范事件流、
可恢复会话与同会话多轮、每 Run 独立强沙箱 Worker、受信模型代理和真实 Ollama
模型调用和显式子智能体委派已经贯通。

支持合同限于 GNU/Linux x86_64、受信且防火墙保护的网络和单一 operator。当前已有通用
Agent Capsule create/upgrade/delete、Capsule 内安全文本读取/搜索、经审批的原子
Edit/Write、固定 allowlist 命令执行及
durable background Task，以及可选的 Agent 专属持久研究/文档环境，并支持从受信目录按 Turn
切换模型；默认目录目前只启用 `qwen3.5:2b`。已有 versioned sandboxed Skill 和独立
Capsule 子智能体，但没有任意 Shell、默认启用的 MCP/LSP、RAG 或
TLS，不应直接暴露到互联网。冻结范围、平台矩阵、发布/备份/回滚和已知限制见
[release contract](docs/design/release.md)。

## 当前链路

```text
Browser :20815
    │ HTTP + SSE
    ▼
Authenticated Web Gateway / trusted Control Plane
    ├── AgentRegistry → lazy per-Agent RuntimeManager
    ├── CommandBus → bounded per-Agent QueryEngineRegistry
    │                   └── one logical QueryEngine per Conversation
    ├── QueryEngine → RunService → ConversationStore / Agent state.sqlite (WAL)
    │                              └── canonical sequencer → durable journal
    ├── prompt-source snapshots + ContextCompiler + EffectiveToolSet → ContextPlan
    ├── trusted Ollama Broker ── TCP ──> iollama:11434 / qwen3.5:2b
    ├── Capability Broker → file/document executors + singleton command runner
    │                       └── Agent-scoped persistent research dependency env
    ├── BackgroundTaskManager → TaskStore → isolated Task root + singleton runner
    ├── SubagentCoordinator → parent Task/mailbox → child Agent Conversation/Run
    └── bounded stdio IPC
             ▼
        one Worker per Run
        Landlock + seccomp + rlimits
             │
             ▼
        HarnessKernel
        context-plan reference → model → local/brokered Tool → model → terminal
```

Web 页面采用 conversation-first 工作台：左侧导航集中 Agent 与会话，中央只保留多轮
对话和固定输入区，默认关闭的右侧“运行详情”承载四泳道时序、事件检查和 ContextPlan
元数据。消息流在用户跟随最新内容时自动吸底，主动回看历史后暂停并提供“回到最新消息”；
composer 持续显示最新一轮 ContextPlan 输入估算 / 实际模型运行窗口及占比。窄屏下导航和
运行详情均变为互斥浮层；键盘可用 Enter 发送、Shift+Enter 换行，高级上下文压缩与 Slash
Command 帮助收进输入区的更多选项。Agent 管理区域可以创建、切换、重命名和删除普通
Agent；重命名只更新显示元数据，不增加 generation、不排空运行时或重建虚拟环境。“重建
运行环境”位于每个普通 Agent 的高级操作中，只用于基础环境、安全策略或 Agent 定义发生
实质变化时切换到下一 generation。固定 ID
`00000000-0000-4000-8000-000000000001` 是正式默认的系统 Agent，不能重命名、升级或删除。
切换 Agent 会同时切换 Agent-scoped Command、会话、Run 和回放视图。页面还可以新建、
选择恢复和删除会话，并在同一会话连续对话。中央按 durable Turn 展示用户消息、成功回答
和非成功终态；点击 Turn 会打开所选 Run 的详情，并把 canonical events 按
User/Harness/LLM/Tool 四条泳道及 `seq` 顺序投影。Turn 与 Run/节点可双向定位，节点支持
步进、播放、类型过滤和常驻详情检查。详情明确区分界面推导事实、逻辑消息、canonical
payload、完整 EventEnvelope 和按需读取的上下文元数据；模型原始请求、响应帧和隐藏
prompt 从未因此暴露。所有消息通过纯文本渲染，不会作为 HTML 执行。

输入以 `/` 开头时不会发送给模型。认证后的 Command registry 目前提供 `/status`、
`/context [run_id]`、`/model [model_id]`、`/compact`、`/permissions`、
`/cancel [run_id]` 和 `/clear`（另有 `/ctx`、`/perms`、`/stop` alias）。Web 的 Slash
Commands 面板展示 schema/help，命令结果明确标记 `model_invoked=false`、
`turn_created=false`；`/model` 与 `/compact` 只设置下一次普通 Turn 的界面参数，不创建
第二份服务端状态。运行中 capability ask 会在输入区显示完整 bounded preview/diff 及
批准/拒绝按钮，决策仍走 authenticated same-origin CSRF API 和原 permission audit。

详细设计见 [architecture](docs/design/architecture.md)、
[event protocol](docs/design/event-protocol.md) 和
[Agent Capsule](docs/design/agent-capsule.md)。

## 环境要求

当前运行时代码接受以下 host；已有完整本机资格证据的平台范围更窄：

- GNU/Linux `x86_64` 已完成当前基础资格；
- GNU/Linux `aarch64` 已有 bootstrap/sandbox 实现，但在原生主机完成等价 cold-checkout
  lifecycle RR 前仍是待资格平台，不能视为受支持部署；
- 可用的 `/proc`、Landlock ABI 6+ 和 seccomp；
- `bash`、`curl`、`flock`、`ps` 及基本 GNU 用户空间工具；
- 可以解析并连接 `iollama:11434`；
- Ollama 已安装具有 completion 与 Tool 能力的 `qwen3.5:2b`。

其它平台没有经过等价的 cold-checkout 验证，不能视为受支持。Worker 沙箱资格或
模型资格失败时启动会 fail closed，不会退回假模型或未隔离运行。

## 模型上下文与预算

受信 `ModelCatalog` 由 operator 在进程构造边界提供，浏览器只能读取有界公开画像并选择
其中的稳定 `model_id`，不能提交 endpoint、provider model name 或 generation options。
当前默认目录只有 `qwen3.5:2b`；Web 的“本轮模型”选择在 Turn 接受前固化 profile，模型
只能在 Turn 之间切换。切换后从 SQLite 完整 durable transcript 按新窗口、能力和 estimator
重编 ContextPlan；不支持 Tool 的模型得到空 ToolSet。provider endpoint `iollama:11434`
仅存在于受信目录和 Broker，不由 `/api/models`、Worker IPC 或 Run 事件暴露。

启动时，受信 Ollama Broker 会从 `/api/show` 的架构专属 `context_length` 读取模型
原生上下文窗口，而不是把 `4096` 或某个模型名称对应的窗口写死。当前
`qwen3.5:2b` 报告 `262144` tokens；运行策略取原生窗口与受信上限 `32768` 的较小值，
目录输出上限与运行窗口的有界比例共同决定输出预留；当前预留 `4096` tokens，所以硬输入
预算为 `28672` tokens。

ContextPlan 同时记录模型 digest、原生/运行窗口、输出预留、估算器、ToolSpec digest
和会话历史投影。未来切换模型时，同一策略会根据资格检查所得窗口重新计算输入预算，
不会把 `28672` 或某个模型名称写进会话数据。达到硬输入预算 80% 时，编译器按
`completed-turn-collapse-v2` 从最旧端把完整的 completed user/assistant Turn pair 替换为
内容无关、带 digest 的 collapse marker，直至估算值不高于 60% 目标；至少保留最近一个
完整 Turn，system/Agent 指令、本轮用户消息、Tool manifest 和输出预留不能被裁剪。
collapse projection 绑定完整来源历史、被折叠和保留的 message IDs、内容 digest、renderer
与 policy，重复编译得到字节一致的模型视图；SQLite 中的 canonical transcript 不变。旧
`completed-turn-tail-v1` boundary 可解码但不能静默按新 renderer 复用。它仍不是语义摘要；
当 deterministic collapse 已触发时，Control Plane 可再发起一次空 ToolSet、无副作用的
`semantic-summary-v1` 子调用；Web 也可勾选“手动生成语义摘要”强制这一层。摘要只接受
`facts/decisions/open_tasks/files/references` 五个有界数组，按不可信历史数据渲染并保留最近
完整 Turn。摘要 snapshot 与 source message IDs/content digest、model/profile、prompt/policy、
renderer、usage 和 provider request digest 一起写入同一 projection boundary。一次失败或
超时立即沿用 deterministic collapse；连续 3 次失败开启 60 秒熔断，不形成 compact loop。
所有投影后仍重新 admission，超硬预算时 fail closed。语义摘要可能丢失或误述信息，完整
SQLite transcript 始终是权威记录。

当前 `utf8-bytes-upper-bound-v1` admission fallback 在没有模型专用 tokenizer 时把每个
UTF-8 byte 按一个 token 估算，并计入实际 section labels/roles、完整 Tool manifest 和
`256` tokens 的 provider template reserve，故意高估常见文本以便安全拒绝超限请求。
窗口决策依据实际模型 profile；provider 报告、Control Plane 校验过的 usage 是受信
观测，估算器仍是新渲染请求的保守 admission 边界。初始 ContextPlan 和每个后续模型
轮次都会对完整投影 transcript + 当前 Tool manifest 重做硬预算 admission；移出所有可
省略的完整历史 pair 后仍超限时，以 `model_context_limit` fail closed，不让 provider
静默截断 prompt。

完整 ContextPlan 只存在于受信 Control Plane。Worker 启动命令仍包含本轮用户消息，
但模型 IPC v2 只携带 plan/toolset 的 ID 与 digest 引用以及受信 Tool result call IDs；
ContextPlan 中的平台/Agent/workspace 指令和完整 Tool manifest 不通过 IPC 交给 Worker，
也不能由浏览器覆盖。每轮由 ToolCatalog 与 policy 解析 EffectiveToolSet；Worker 只接收
有效 Tool ID 子集，并从运行时密封 catalog 解析同版本 ToolSpec 做本地校验与执行。
当前 release runtime ToolSet 包含 file/stat、file/read_text、file/glob、file/grep、
file/edit、file/write、exec/run 和 ask-only agent/delegate，并固定最多 2 次顺序调用：不可变 TurnRuntimeSnapshot 固化最多 4 次模型调用、
2 次 Tool 调用、模型/ContextPlan/Tool manifest、累计 usage 和 240 秒 deadline。每次受信结果
按原 call ID 回流；第二次预算用完后 provider ToolSet 收窄为空，并在最后一个不可信 Tool
result 之后追加固定的受信 finalization marker，明确切换到普通文本最终答复。重复、乱序、
并行或第三次 Tool call 会以专用 `model_tool_loop` fail closed。`builtin/echo` 只留在密封
catalog 中兼容原型测试和旧 journal，不会暴露给新 release Run。

每个 Agent 可选在自己的 `data/agents/<agent-id>/workspace/CLAUDE.md` 放置项目指令。
首版只读取这一精确文件，不向上寻找，也不处理 include。文件必须由当前用户拥有、是
UTF-8 regular file、只有一个硬链、不允许 group/world write，且不超过 32 KiB；推荐：

```bash
chmod 600 data/agents/<agent-id>/workspace/CLAUDE.md
```

缺文件不会报错；不安全、读取中变化或超限会拒绝新 Run。Control Plane 还会加入只含
UTC 日期/时区/Linux 的 allowlisted environment section。只有 Capsule workspace 自身是
Git repository 时才收集最多 16 KiB 的 tracked status；Git 在 2 秒只读 Landlock helper
内运行，输出标记为 untrusted project data，不会提升成 instruction。

模型还可通过 `file/stat`/`file/read_text` 读取当前 Agent 的 workspace 文件，并以
`file/glob`/`file/grep` 做有界、无索引搜索。该请求从
Worker 经有界 capability IPC 回到 Control Plane；Worker 本身没有 workspace 权限。
路径必须是规范相对路径，逐组件拒绝 symlink/xdev/危险目录；目标只接受 owned、单硬链、
非 group/world-writable、非稀疏、稳定的 UTF-8 regular file，最大文件 1 MiB，单次正文
最多 4096 bytes/256 行。结果包含 content/path identity digest 和 truncation receipt，
正文始终按不可信 Tool 数据处理，不生成 sidecar 或搜索索引。搜索限制 depth 16、entries
4096、files 1024、读取 2 MiB、matches 128、结果 12 KiB 和 wall time 1 秒；`.git` 不遍历，
结果按 UTF-8 path 稳定排序并携带 provenance/truncated reason。Grep 默认 literal；regex
只接受无 group/alternation/backreference/brace/`*`/`+` 的受限子集，复杂模式 fail closed。

Agent 管理抽屉中的“研究与文档环境”是一个固定、显式安装的 Capsule capability，不是任意
`pip install`。首次安装由受信 Control Plane 使用 checkout-local uv 把
`pypdf==6.14.2`、`python-docx==1.2.0` 及固定传递依赖以 `--only-binary :all:`、`--no-deps`
安装到 `.runtime/agents/<agent-id>/dependencies/research-documents/`；source metadata 位于
同一 Agent 的 `data/.../dependencies/`。相同版本再次安装直接返回已有记录，因此该 Agent
后续任意 Conversation 都复用现有环境，不重复下载；其它 Agent 看不到也不会受其包变更影响。

安装成功后，未来 Turn 才会得到 `document/extract_text`。它支持当前 Agent workspace 中
最大 16 MiB 的 PDF、DOCX、HTML、Markdown 和 UTF-8 text，并按 `offset_chars/max_chars`
每次最多返回 4096 字符。Control Plane 以 descriptor 锚定方式拒绝 traversal、symlink、
hardlink、危险 mode、跨 device、稀疏或读取竞态，DOCX 还限制 archive entries 和展开总量；
随后只把一次性副本放入该 Run 的 private work root。解析器在 singleton
Landlock/seccomp/rlimit domain 中运行，网络、fork、exec、workspace 和跨 Agent 路径均不可见，
只读固定依赖环境；terminal 后副本被删除。环境安装/删除会 fence 当前 Agent，active Run
存在时拒绝；删除环境不删 workspace/会话，删除 Agent 则连同环境完整清除。通用搜索引擎或
任意联网抓取仍应通过 operator-owned HTTPS extension 接入，不能由文档解析环境自行联网。

`file/edit` 与 `file/write` 只能由受信 Control Plane 执行，并始终进入 Web 可见的 ask
审批。现存文件必须先在同一 Run 完整 `file/read_text`，mutation 绑定 path identity、完整
content digest 和精确 diff；Edit 的旧片段必须恰好出现一次。create 绑定 target-absent 与
parent identity，并用 no-clobber rename；replace 用同目录 `0600` private temp、文件 fsync、
Linux `renameat2(RENAME_EXCHANGE)`、旧 inode/content 复核和 parent fsync，绝不原地写或
跟随 symlink。单次新内容 8 KiB、Edit 片段各 4 KiB、审批 preview 4 KiB、两次 fsync，
每个 Capsule 最多清理 16 个保留命名空间 temp；commit 结果不能证明时记录
`outcome_unknown` 且不会自动重放。

`exec/run` 不是通用终端。它接受固定 `runtime-compile`，或 builtin-only `bounded-bash`
单命令。Bash parser 只允许 `printf|pwd|true|false`，最多 8 个参数，拒绝 substitution、
backtick、env assignment、redirection/heredoc、subshell、pipe、glob、rc、git/package 命令；
再把规范 AST、cwd/env policy 与零 redirection 绑定审批。executable identity、cwd 和 clean
env 均由 Control Plane 固定，模型不能提供 PATH 或 endpoint。每次执行必须由 operator
审批；runner 在同一 PID 中先安装 10 CPU 秒、256 MiB、32 FD、2 MiB/文件等 rlimit，随后
安装 Landlock 和拒绝 network、fork/clone、setsid 的 seccomp；compile 还拒绝后续 exec，
bounded Bash 只允许进入已绑定的 Bash image，语法不能请求其它 exec。随后通过 ready/release
handshake，Control Plane 用 PIDFD 和 `/proc` 复核后才放行，
12 秒 wall、12 KiB stdout+stderr 和 8 MiB allocated output 超限即杀死确切 PID；成功或
失败都会清空输出与 PID record。该 singleton kernel domain 是 v1 的完整后代容器，不依赖
仅能辅助清理的 process group。

同一个 `runtime-compile` 还可由认证、CSRF 管理面从既有 parent Run 显式提交为 background
Task；它不是 shell 字符串或模型隐式 daemon。每 Agent 最多保留 128 个 Task、同时最多 4
个 active，单结果 16 KiB、每 Task 最多 4 条各 4 KiB 通知、终态保留 7 天。Task 有独立
HOME/TMP/XDG/work/output，复用 12 秒 wall 与 singleton sandbox；状态只在
queued/running/terminal 边界提交，不逐 stdout/stderr chunk 写盘。重启把未完成 Task 标为
`interrupted` 且不重放；取消、Conversation 删除、stop 和 Agent delete 均先收敛 runner、
清除精确 Task 根。

`agent/delegate` 只接受另一个 active Agent ID 和 4 KiB message，并始终要求 operator
审批。父 Agent 得到一个绑定 parent Run 的 durable Task 和最多两条 mailbox 事实；子 Agent
在自己的 Capsule、Conversation、Worker、模型 session 与 Landlock/seccomp sandbox 中
执行。全局同时最多 2 个委派、每 parent Run 1 个、depth 1、wall 45 秒，返回父模型的 answer
最多 8 KiB。取消、deadline、重启、父会话删除和 Agent drain 都会收敛 child Run；删除父
会话会继续删除其 child Conversation，删除 child Agent 会删除完整 Capsule。Web 的子智能体
卡片显示父→子/子→父消息，并通过 Agent-scoped replay/context API 单独查看 child Run；
普通 assistant 文本不构成 mailbox 或委派事实。

## Agent、会话与同会话多轮

浏览器登录后的主要 API 是：

| 操作 | 路径 |
| --- | --- |
| Agent create/list/get | `POST|GET /api/agents`、`GET /api/agents/<agent-id>` |
| Agent upgrade/delete | `POST /api/agents/<agent-id>/upgrade`、`DELETE /api/agents/<agent-id>` |
| 研究环境状态/安装/删除 | `GET|POST|DELETE /api/agents/<agent-id>/research-environment` |
| 列出、创建会话 | `GET|POST /api/agents/<agent-id>/sessions` |
| Slash Command 目录/执行 | `GET /api/agents/<agent-id>/commands`、`POST /api/agents/<agent-id>/sessions/<id>/commands` |
| 读取恢复、删除会话 | `GET|DELETE /api/agents/<agent-id>/sessions/<conversation-id>` |
| 在会话内开始下一轮 | `POST /api/agents/<agent-id>/sessions/<conversation-id>/runs` |
| 订阅、取消或回放 Run | `/api/agents/<agent-id>/runs/<run-id>/events|cancel|replay` |
| 检查本轮有效上下文元数据 | `GET /api/agents/<agent-id>/runs/<run-id>/context` |
| 查看/解析待审批 capability | `GET /api/agents/<agent-id>/permissions`、`POST /api/agents/<agent-id>/permissions/<permission-id>` |
| 分页读取 capability 审计 | `GET /api/agents/<agent-id>/runs/<run-id>/capability-audit` |
| 提交固定 background Task | `POST /api/agents/<agent-id>/runs/<run-id>/tasks` |
| 列出/读取/取消 Task | `GET /api/agents/<agent-id>/tasks[/<task-id>]`、`POST /api/agents/<agent-id>/tasks/<task-id>/cancel` |
| 读取 Task 语义通知 | `GET /api/agents/<agent-id>/tasks/<task-id>/notifications` |
| 读取会话的子智能体链路/邮箱 | `GET /api/agents/<agent-id>/sessions/<id>/subagents` |
| 回放/检查指定 Agent Run | `GET /api/agents/<agent-id>/runs/<run-id>/replay|context` |
| 查看当前扩展目录 | `GET /api/extensions`、`GET /api/agents/<agent-id>/extensions` |
| 列出/安装/删除 Skill | `GET|POST /api/agents/<agent-id>/skills`、`DELETE /api/agents/<agent-id>/skills/<skill-id>` |

Conversation 是持久容器，Turn 是一次用户提交及其终态，Run 是执行该 Turn 的独立
Worker/模型会话。一个 Conversation 同时最多有一个 active Run；活跃时再次发送或删除
会返回冲突，先取消或等待终态。成功终态会原子保存完整 user/assistant pair；failed、
cancelled、interrupted Turn 仍在 UI 中保留用户输入和状态，但没有可提交的 assistant，
也不会进入下一轮模型历史。

Gateway 为每个打开的 Conversation 懒创建一个逻辑 `QueryEngine`；同一进程内相同
conversation ID 始终得到同一个 Engine。它固定 Agent/Conversation 身份、把每次提交
映射为新的 Turn/Run，并提供恢复、取消和删除入口。Engine 只持身份、短时入口锁和
retired 标记，不保存消息副本、事件、ContextPlan、模型连接或 Worker；因此刷新或重启
不会产生第二份会话事实。Registry 最多保留 100 个 Engine，与持久会话容量一致；删除
后旧 handle 立即失效并从 Registry 驱逐。

Gateway 启动时会把上次进程遗留的 `running` Turn 收敛为 `interrupted`，释放该会话后
即可继续发送。`GET /api/sessions/<id>` 从 Agent 专属 SQLite 恢复 durable transcript，
不复活旧 Worker。浏览器刷新后，如果同一 Gateway 仍保留该 active Run，前端会从
`running` Turn 的 `run_id` 重新附着，并携带最后应用的 `Last-Event-ID` 做最多 3 次有界
重连。若 live RunRecord 已不存在，events endpoint 固定切换为只读 durable replay source，
以明确 gap/snapshot control 收敛到已持久 terminal；选择已结束会话时，界面也会恢复最近
Run 的 durable 时间线。旧模型流和 Worker 不会复活，ephemeral delta 不跨进程恢复。

事件时序工作台可在同一 Conversation 的全部 Turn/Run 间切换，并用文字区分
`Harness → LLM`、`LLM / Broker → Harness`、`Harness → Tool`、
`Tool/恢复 → Harness`、Harness 内部状态和 replay control。每次真实 provider attempt 只增加
一个 `model.request.started` 和一个 `model.response.finished` durable 边界；逻辑轮次、
attempt 和 provider 调用序号分开显示。精确 context/media overflow 在任何 provider frame
出现前最多触发一次 `model.recovery.started` 和一次重新 admission 的调用，第二次失败不循环，
认证、网络、格式、取消和部分流不触发该路径。事件详情展示的
是完整 canonical envelope，不是 Ollama 原始请求或响应正文。每个 Run 顶部的
User → Harness 节点来自对应 Turn 的 durable user message，是带清晰标签的界面投影，
不是伪造的 EventEnvelope，也不占用 canonical `seq`。

上下文检查接口和 UI 只在用户显式点击时读取，响应强制 `no-store`。Gateway 仍保留该
Run 的不可变 ContextPlan 时，它返回经过重新校验的 section 顺序、role/trust/provenance、
字节/Token 估算、digest、纳入/省略历史计数、窗口和预算；ContextPlan 已退出内存时只返回
validated `run.started` 摘要。两种响应都不返回 system/hidden prompt、用户/assistant 正文
或 provider messages；Conversation 正文继续由会话 API 展示，也不会为了检查功能复制写入
事件 journal。

每次 Turn admission 还会在同一 SQLite 事务保存一条 digest-only context projection
boundary，用于证明该模型视图绑定了哪一个 Conversation revision、model/profile、
instructions/history、Tool policy 和 renderer。它不复制 prompt 正文；后续边界替换使用
旧 digest 做 CAS，因此崩溃不会留下半写 snapshot，Conversation 删除会一并清理。

诊断正文提升默认关闭。只有 operator 在启动前显式设置
`HARNESS_V2_CONTEXT_REVEAL=1` 时才启用独立令牌、CSRF 和有界审计路径；即使启用，
platform/Agent/workspace section 仍永不回显，其它 section 也只提供最大 2048 bytes 的
credential-redacted excerpt。令牌位于 `.runtime/secrets/context-reveal-token`，不得交给
browser JavaScript、放入 URL/命令参数、日志或提交。

这些接口使用同一个项目 bootstrap token，是 single-operator 管理面：所有已认证浏览器
session 都能管理全部 Agent。它不是用户账号或租户隔离系统。所有业务读取都
要求登录 session，创建、删除、发起 Turn 和取消还要求 exact same-origin 与 CSRF。
审批同样要求 CSRF，body 只接受 `approve|deny`；浏览器不能创建 pending request。
当前文件读取使用 policy `allow`，仍会留下完整的 permission/operation audit；因此它不会
出现在 pending 列表。Edit/Write、受限 command、安装后的 Skill 和 subagent delegate 使用
ask policy；extension catalog 默认为空，任意 Shell、stdio MCP 和请求级 endpoint 不存在。

## 一键启动与停止

```bash
./start.sh
# 仅在确需脱敏 prompt 诊断时：
HARNESS_V2_CONTEXT_REVEAL=1 ./start.sh
```

冷 checkout 时，`start.sh` 会按需调用 checkout-local bootstrap。也可以提前执行：

```bash
./bootstrap.sh
./bootstrap.sh --offline   # 仅使用已有的项目内缓存
```

Gateway 固定监听 `0.0.0.0:20815`。启动成功后访问：

- Web UI：`http://<主机地址>:20815`
- 健康检查：`http://127.0.0.1:20815/health`（返回 `release: "0.2.0"`；其中
  `prototype: true` 仅是不可删除系统 Agent 的旧协议兼容字段，不表示整个 release 的支持状态）

首次启动会生成权限为 `0600` 的登录 token：

```text
.runtime/secrets/web-bootstrap-token
```

把 token 内容输入登录页。不要把它放入 URL、shell 命令参数、日志、截图或提交。
登录后使用 HttpOnly session cookie；修改状态的请求还需要 CSRF token。

需要显式轮换时使用交互式命令：

```bash
./set-access-token.sh
```

命令只从无回显终端读取两次，不接受 token argv 或环境变量；它会验证旧 secret path、
停止 Gateway、以同目录 `0600` 临时文件和 fsync/atomic replace 更新 token，再重启并使
全部旧浏览器 session 失效。默认自动生成值仍是 256-bit/64 位 lowercase hex；显式值
只允许 10..128 个 ASCII 字母、数字或 `._~+-`。短口令的熵远低于默认值，在当前无 TLS、
监听 `0.0.0.0` 的部署中只能用于受信且防火墙保护的网络。

停止整个当前系统：

```bash
./stop.sh
./stop.sh --force   # 缩短退出期限；仍校验进程身份
```

脚本只会停止由本 checkout 记录且重新验证过身份的 Gateway/Worker。它不会因为
20815 被占用就杀死未知进程。启动失败会回滚已启动组件；停止会检查每个 Agent 的
Run 记录并清除已确认没有进程占用的临时 Run 根。

一致性备份/恢复会先 stop，并且只读写 checkout 内的 `data/`、`backups/` 和私有 staging：

```bash
./backup.sh before-upgrade-20260720
./restore.sh backups/before-upgrade-20260720.tar --yes
```

恢复会把替换前的数据保留在 `.runtime/recovery/`，不会静默删除。完整测试、依赖审计、
CycloneDX SBOM、真实模型资格和 source artifact 可一键执行：

```bash
./release.sh RR-QUA-20260720-01
```

这三个命令的停机、token、产物与回滚合同见 [release runbook](docs/design/release.md)。

## 项目内状态

所有环境、缓存、密钥、进程记录、日志、临时文件和数据均在本 checkout：

```text
.tools/                         checkout-local uv 引导工具
.venv/                         控制面 Python 环境
.runtime/python|cache/          managed Python、uv/pip/bytecode 缓存
.runtime/home|tmp/              被重定向的 HOME 与临时目录
.runtime/config|share|state/    被重定向的 XDG 配置、数据与状态
.runtime/xdg-runtime/           被重定向的 XDG runtime
.runtime/control-plane/         Gateway PID、生命周期锁、有界轮转日志
.runtime/secrets/               Web bootstrap token
.runtime/agents/<agent-id>/     Agent worker-env、持久 dependency venv、日志和临时 runs
.runtime/qualification|release/ 资格记录、SBOM 和 release artifact
data/agent-registry.sqlite      Agent lifecycle/generation authority
data/agents/<agent-id>/         manifest、workspace、artifacts、dependency metadata、会话/事件 state.sqlite
backups/                        operator 创建的私有 data 备份（默认忽略）
```

`env.sh` 会清理继承的 Conda/virtualenv/package-install 变量，并把 HOME、TMP、XDG、
uv、pip 和 Python bytecode 路径重定向到 checkout 内。不要使用系统 `pip`、全局 npm、
用户 home 缓存或 `/tmp` 作为项目状态目录。

查看可清理范围：

```bash
./purge.sh --help
```

可选范围为 `cache|logs|environments|data|dependencies|runtime|all`，实际删除必须带
`--yes`。`data` 和 `all` 会删除不可重建的 Agent 持久状态。

## 数据与 SSD 边界

- Agent 的 Conversation/Turn 和 canonical events 共用私有 `state.sqlite`；SQLite 使用
  WAL、`synchronous=NORMAL`、16 MiB journal limit 和有界数据库容量。
- `assistant.block.delta` 只在内存和 SSE 中流动，不逐 token 写盘。
- Turn 开始和终态只在语义边界做事务写；completed assistant 只在完成时一次保存，
  不随流式 chunk 重写完整会话。删除会话原子删除其 Turn 和 Run events，不为每次删除
  执行 `VACUUM` 或全库重写；这提供应用层删除，不承诺 SSD 的取证级物理擦除。
- `run.started` 只记录 ContextPlan 的 ID、digest、section 数量和预算元数据，不写完整
  prompt；每种 terminal 都记录 usage。token 数由 provider 报告、Control Plane 按模型
  profile 校验并累计，Worker 不可伪造；`complete=false` 表示当前 provider 轮次没有
  产生可校验终帧，因此累计值可能只覆盖此前完整轮次。
- 旧 Run / 原型测试中的 `builtin/echo` 输入和结果分别限制为 `8192` UTF-8 bytes。
- 旧 Echo canonical 结果仍完整显示在事件中；送给模型的单结果投影最多 `4096` bytes，
  超限时使用带 call ID/原始 bytes/digest/reason 的确定性 receipt，单 Run 投影历史最多
  `16 KiB`，随后重新执行模型 admission。
- 每 Run 最多 512 个 live event、1 MiB live event bytes、256 KiB durable bytes。
- 最多 4 个 active Run、内存保留 64 个近期 Run、journal 保留 256 个近期 Run。
- Model Broker 最多同时打开 2 路 Ollama stream；其余 active Run 最多等待 30 秒，超时
  以可重试 `model_busy` 收敛，不创建无界 provider 请求队列。
- 每个受信 ModelCatalog entry 固化独立的阶段超时画像。默认 qwen 首帧等待 60 秒、帧间
  空闲 20 秒、单次调用 120 秒；首帧前且没有任何 Provider 输出时可透明重试一次。失败分别
  收敛为 `model_first_frame_timeout`、`model_stream_idle_timeout`、
  `model_turn_deadline` 或 `model_transport_timeout`，部分输出后绝不重试。
- 默认 qwen 的单次输出预算为 4096 tokens；这为长回答和模型内部推理留出余量，同时仍受
  12 KiB 可见正文、4096 Provider 帧、120 秒模型调用和 240 秒 Run deadline 的多重上限。
- 最多 4096 个 provider 原始帧会被合并成最多 128 个 content IPC frame；受信 Broker
  error 的 code/retryability 由 Control Plane 写入 canonical `run.failed`，不信任 Worker
  转述。
- 每个 Run 有 240 秒 wall deadline；Run 树最多 1,024 项、16 MiB 逻辑数据、32 MiB
  实际分配数据，并以不高于每秒一次的频率检查。
- Gateway 日志单段 5 MiB，保留 3 个备份，总上界 20 MiB；写入批量刷新。

这些是当前代码的硬边界，不代表完成了长期 soak 或所有硬件磨损验证。

## 验证

```bash
source ./env.sh
./.venv/bin/python -m pytest
./governance.sh
./release.sh RR-QUA-YYYYMMDD-01
```

已启动 Gateway 的有界真实模型 workload、磁盘增长和残留记录使用
[runtime qualification contract](docs/design/qualification.md)；该应用层记录不能替代
不受支持架构、长期 soak、内核/设备物理 flush 或 SMART 证据。独占
资格轮次可用 `./start.sh --qualification-sync-count` 显式加载 checkout-local 的低写放大
libc sync-call counter；正常启动永不加载。其固定共享页只统计 supervisor、Gateway 和
Worker 的 `fsync|fdatasync|msync|syncfs|sync_file_range|sync` symbol 成功/失败，不代表
NVMe 实际写入。完整使用和 A/B 观测者效应要求见资格契约。

真实端到端验收还应执行 `./start.sh`、检查 `/health`、通过 Web 发起一次 Run，确认
模型为 `qwen3.5:2b`、事件到达唯一终态；随后在同一会话用依赖首轮内容的问题验证
第二轮历史，重启 Gateway 后再发第三轮验证恢复后的 completed history 确实进入模型
上下文。删除验收会话后，详情与其 Run event endpoint 都应返回 404；最后执行
`./stop.sh` 并检查无受管进程、PID 文件或 Run 根残留。

## 仓库结构

```text
src/agent_builder_v2/       当前唯一运行时
tests/                      当前运行时的单元、集成和安全回归测试
scripts/                    生命周期辅助程序
docs/                       原则、治理、设计和活动计划
references/claude-code/     Claude Code 研究资料及来源记录
_legacy-reference/          旧系统只读快照，不属于当前运行时
```

旧系统归档仅供用户明确要求时做历史调查；当前代码、脚本、测试和文档不得依赖或扫描
它。Claude Code 材料也是只读设计输入，不是运行时依赖，使用前请阅读
[reference guide](references/claude-code/README.md) 和
[provenance](references/claude-code/PROVENANCE.md)。

## 文档入口

- [项目原则](docs/PRINCIPLES.md)
- [文档治理](docs/DOCUMENTATION.md)
- [安全边界](SECURITY.md)
- [发布与运维合同](docs/design/release.md)
- [运行时重建计划](docs/plans/runtime-rebuild.md)
- [编码智能体指南](CLAUDE.md)

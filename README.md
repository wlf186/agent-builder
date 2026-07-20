# Agent Builder

Agent Builder 是一个从零构建的 Claude Code 风格智能体运行时。目前交付的是一条
可实际体验的 greenfield walking skeleton：认证 Web UI、类型化命令、规范事件流、
可恢复会话与同会话多轮、每 Run 独立强沙箱 Worker、受信模型代理和真实 Ollama
模型调用已经贯通。

它**不是生产就绪产品**。当前只有固定 demo Agent、固定模型和 `builtin/echo`；没有
通用 Agent 管理、Shell、文件工具、Skill、MCP、RAG、子智能体或 TLS。原型适合在
受信、防火墙保护的开发网络中验证架构，不应直接暴露到互联网。

## 当前链路

```text
Browser :20815
    │ HTTP + SSE
    ▼
Authenticated Web Gateway / trusted Control Plane
    ├── CommandBus → bounded QueryEngineRegistry
    │                   └── one logical QueryEngine per Conversation
    ├── QueryEngine → RunService → ConversationStore / Agent state.sqlite (WAL)
    │                              └── canonical sequencer → durable journal
    ├── ContextCompiler + immutable ToolSpec → trusted ContextPlan
    ├── trusted Ollama Broker ── TCP ──> iollama:11434 / qwen3.5:2b
    └── bounded stdio IPC
             ▼
        one Worker per Run
        Landlock + seccomp + rlimits
             │
             ▼
        HarnessKernel
        context-plan reference → model → builtin/echo → model → terminal event
```

Web 页面可以新建、选择恢复和删除会话，并在同一会话连续对话。右侧按 durable Turn
展示用户消息、成功回答和非成功终态；左侧把所选 Run 的 canonical events 按
User/Harness/LLM/Tool 四条泳道及 `seq` 顺序投影。Turn 与 Run/节点可双向定位，节点支持
步进、播放、类型过滤和常驻详情检查。详情明确区分界面推导事实、逻辑消息、canonical
payload、完整 EventEnvelope 和按需读取的上下文元数据；模型原始请求、响应帧和隐藏
prompt 从未因此暴露。所有消息通过纯文本渲染，不会作为 HTML 执行。

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

启动时，受信 Ollama Broker 会从 `/api/show` 的架构专属 `context_length` 读取模型
原生上下文窗口，而不是把 `4096` 或某个模型名称对应的窗口写死。当前
`qwen3.5:2b` 报告 `262144` tokens；运行策略取原生窗口与受信上限 `32768` 的较小值，
并预留 `2048` tokens 给输出，所以当前硬输入预算为 `30720` tokens。

ContextPlan 同时记录模型 digest、原生/运行窗口、输出预留、估算器、ToolSpec digest
和会话历史投影。未来切换模型时，同一策略会根据资格检查所得窗口重新计算输入预算，
不会把 `30720` 或某个模型名称写进会话数据。达到硬输入预算 80% 时，编译器从最旧端
按完整的 completed user/assistant Turn pair 移出历史，直到估算值不高于 60% 目标或
没有可移出的 pair；system/Agent 指令、本轮用户消息、Tool manifest 和输出预留不能被
裁剪。完整会话仍保留在 SQLite，窗口选择只影响本 Run 的模型视图。这是确定性的
tail-window，不是语义摘要；
当前没有模型生成 summary、snapshot 或对省略内容的推断。

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
ContextPlan 中的平台/Agent 指令和完整 Tool manifest 不通过 IPC 交给 Worker，也不能由
浏览器覆盖。Worker 只使用运行时内置的同版本 ToolSpec 做本地校验与执行。
当前 Echo 演示是 one-shot capability：首轮 provider ToolSet 含 Echo，受信结果回流后
后续轮次 ToolSet 收窄为空；模型若仍返回 Tool call 会以协议错误 fail closed。

## 会话与同会话多轮

浏览器登录后的主要 API 是：

| 操作 | 路径 |
| --- | --- |
| 列出、创建会话 | `GET /api/sessions`、`POST /api/sessions` |
| 读取恢复、删除会话 | `GET /api/sessions/<conversation-id>`、`DELETE /api/sessions/<conversation-id>` |
| 在会话内开始下一轮 | `POST /api/sessions/<conversation-id>/runs` |
| 订阅或取消一次 Run | `GET /api/runs/<run-id>/events`、`POST /api/runs/<run-id>/cancel` |
| 分页回放规范事件 | `GET /api/runs/<run-id>/replay` |
| 检查本轮有效上下文元数据 | `GET /api/runs/<run-id>/context` |

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
`Tool/恢复 → Harness`、Harness 内部状态和 replay control。每次真实 provider 调用只增加
一个 `model.request.started` 和一个 `model.response.finished` durable 边界；事件详情展示的
是完整 canonical envelope，不是 Ollama 原始请求或响应正文。每个 Run 顶部的
User → Harness 节点来自对应 Turn 的 durable user message，是带清晰标签的界面投影，
不是伪造的 EventEnvelope，也不占用 canonical `seq`。

上下文检查接口和 UI 只在用户显式点击时读取，响应强制 `no-store`。Gateway 仍保留该
Run 的不可变 ContextPlan 时，它返回经过重新校验的 section 顺序、role/trust/provenance、
字节/Token 估算、digest、纳入/省略历史计数、窗口和预算；ContextPlan 已退出内存时只返回
validated `run.started` 摘要。两种响应都不返回 system/hidden prompt、用户/assistant 正文
或 provider messages；Conversation 正文继续由会话 API 展示，也不会为了检查功能复制写入
事件 journal。

这些接口使用同一个项目 bootstrap token，是 single-operator 管理面：所有已认证浏览器
session 看到同一 demo Agent 的会话。它不是用户账号或租户隔离系统。所有业务读取都
要求登录 session，创建、删除、发起 Turn 和取消还要求 exact same-origin 与 CSRF。

## 一键启动与停止

```bash
./start.sh
```

冷 checkout 时，`start.sh` 会按需调用 checkout-local bootstrap。也可以提前执行：

```bash
./bootstrap.sh
./bootstrap.sh --offline   # 仅使用已有的项目内缓存
```

Gateway 固定监听 `0.0.0.0:20815`。启动成功后访问：

- Web UI：`http://<主机地址>:20815`
- 健康检查：`http://127.0.0.1:20815/health`

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
.runtime/agents/<agent-id>/     Agent worker-env、日志和临时 runs
data/agents/<agent-id>/         manifest、workspace、artifacts、会话/事件 state.sqlite
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
- `builtin/echo` 的输入和结果分别限制为 `8192` UTF-8 bytes。
- 每 Run 最多 512 个 live event、1 MiB live event bytes、256 KiB durable bytes。
- 最多 4 个 active Run、内存保留 64 个近期 Run、journal 保留 256 个近期 Run。
- Model Broker 最多同时打开 2 路 Ollama stream；其余 active Run 最多等待 30 秒，超时
  以可重试 `model_busy` 收敛，不创建无界 provider 请求队列。
- 最多 4096 个 provider 原始帧会被合并成最多 128 个 content IPC frame；受信 Broker
  error 的 code/retryability 由 Control Plane 写入 canonical `run.failed`，不信任 Worker
  转述。
- 每个 Run 有 60 秒 wall deadline；Run 树最多 1,024 项、16 MiB 逻辑数据、32 MiB
  实际分配数据，并以不高于每秒一次的频率检查。
- Gateway 日志单段 5 MiB，保留 3 个备份，总上界 20 MiB；写入批量刷新。

这些是当前代码的硬边界，不代表完成了长期生产 soak 或所有硬件磨损验证。

## 验证

```bash
source ./env.sh
./.venv/bin/python -m pytest
./governance.sh
```

已启动 Gateway 的有界真实模型 workload、磁盘增长和残留记录使用
[runtime qualification contract](docs/design/qualification.md)；该应用层记录不能替代
cold checkout、双架构、lifecycle 故障矩阵、内核/设备物理 flush 或 SMART 证据。独占
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
- [运行时重建计划](docs/plans/runtime-rebuild.md)
- [编码智能体指南](CLAUDE.md)

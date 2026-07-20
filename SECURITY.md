---
owner: security-maintainers
status: maintained
last_reviewed: 2026-07-20
review_cycle: quarterly
---

# Security policy and trust boundaries

本文件描述当前 greenfield 原型实际存在的安全边界。它不是生产认证、形式化证明或
互联网暴露许可。当前 HTTP listener 没有 TLS，只能位于受信、防火墙保护的网络。

## 信任模型

受信组件是 checkout 中经过维护者审核的 Web Gateway、Control Plane、Model Broker、
生命周期脚本和固定 Worker 程序。以下内容一律不可信：

- 浏览器请求、cookie、Host/Origin、JSON、ID、cursor 和取消时序；
- 用户消息、模型输出、Tool arguments、Worker event 和 IPC frame；
- 文件系统已有条目、符号链接、硬链接、PID 文件和进程标识；
- DNS 与模型服务响应；
- `_legacy-reference/` 和 `references/claude-code/materials/` 中的任何内容。

当前用户消息和受信 Control Plane 编译的模型上下文会发送到固定的
`iollama:11434/qwen3.5:2b`。部署者必须把该服务及其数据处理方式纳入自己的信任与
隐私评估。

## Web 边界

- `GET /health` 和静态 UI 资源无需登录；会话、Run、live 事件、durable replay、
  context inspection 和取消接口需要有效登录
  session。会话创建/删除、发起 Turn 和取消 Run 还必须通过 CSRF 与 exact same-origin
  检查。
- 首次启动在 `.runtime/secrets/web-bootstrap-token` 创建 256-bit 随机 token，文件
  权限为 `0600`。`set-access-token.sh` 可通过隐藏 TTY 显式轮换为 10..128 个窄 ASCII
  字符；它不接受 argv/env token，执行 stop → private temp + fsync + atomic replace →
  restart，并使旧 browser session 失效。显式短 token 不再具有 256-bit 熵，只能用于
  受信、防火墙保护网络。服务只比较固定长度 digest，不记录或回显 token。
- 登录有来源级失败速率限制和会话容量/有效期上限。session 使用 HttpOnly cookie；
  状态修改请求还要求匹配的 CSRF token。
- Gateway 校验 Host、same-origin/Origin、Content-Length 和实际流式请求大小；JSON
  body 上限为 16 KiB，用户消息上限为 8,192 UTF-8 bytes。
- 响应包含 CSP、frame、MIME sniffing、referrer 和 no-store 等安全
  header。前端用 `textContent` 渲染模型与事件内容。
- Uvicorn 单 worker、有界并发/backlog/header、短 keep-alive，并关闭 proxy header
  信任与 server banner。
- 当前 bootstrap token 代表整个本地项目，登录 session 没有独立用户 principal；所有
  已认证浏览器都能管理 registry 中全部 Agent 及其会话。这是 single-operator 边界，
  不是多用户、租户或会话所有者隔离。

不要通过反向代理公开服务，除非同时补齐 TLS、可信代理模型、正确的 forwarded
header 验证和相应负面测试；这些尚未实现。

## 模型边界

网络连接只由受信 Control Plane 建立。Ollama host、port 和 model 是代码固定值；
context/predict 参数只能由受信模型画像与运行策略生成，不能从请求、Agent 或 Worker
覆盖。启动时必须完成 DNS/IP 限定、服务健康、模型存在、Tool 能力和 `/api/show`
原生上下文窗口资格检查。当前服务报告 `262144` tokens；Control Plane 将运行窗口
封顶为 `32768`，预留 `2048` 输出 tokens，得到 `30720` 的硬输入预算。若未来模型报告
更小窗口，预算必须随之缩小；不信任模型名称或调用方自行声明的容量。

ContextPlan 由受信 Control Plane 从固定 platform contract、Capsule generation、该会话
已完成的 user/assistant pairs、本轮用户消息、Capsule prompt-source 快照、模型画像和
EffectiveToolSet 编译并以 digest
固定。只有状态为 `completed` 且 assistant 完整落在终态事务中的 pair 可以进入历史；
failed、cancelled、interrupted、discarded block、ephemeral delta 和 Tool result 都不能
污染下一轮上下文。workspace instruction 首版只读取当前 Agent Capsule 内精确的
`workspace/CLAUDE.md`：目录描述符锚定、`O_NOFOLLOW`、regular file、当前 uid、单硬链、
禁止 group/world write、UTF-8、32 KiB 和读前/读后 identity 全部 fail closed；缺文件为
确定性 no-op，不向上遍历、不读 HOME、不支持 include。完整 plan、provider messages 和
隐藏指令正文不得进入 Worker、event、日志或浏览器；`run.started` 只公开 plan ID/digest、
section/历史计数、窗口策略、估算值和预算元数据。认证的
`GET /api/runs/<run-id>/context`（通用 Agent 使用 agent-scoped 等价路径）也只在 RunRecord
仍驻留时返回重新校验的 section 顺序、role/trust/provenance/dependency、独立 budget、
truncation reason、字节/Token 估算和进程内 keyed inspection digest；只有因 Gateway
重启或 retention 而不再驻留的 Run 才只返回已验证 `run.started` 摘要。key 不持久、不返回、
也不复用登录 token，因而浏览器不能把 digest 当作隐藏 section 的离线猜测 oracle。接口
拒绝 query parameter、强制 `no-store`，两种路径都不返回正文或 provider messages，也不为
检查另建 prompt 持久状态。正文提升默认不存在；显式 operator policy 需要独立 256-bit
secret、browser session、same-origin、CSRF 和单独 header，并在返回前写有界审计。即使提升，
platform/Agent/workspace/environment 永远隐藏，其它 section 也只有 2048-byte credential-redacted excerpt；
secret、prompt 和 excerpt 不进入审计、日志或事件。

UTC 日期、时区和平台 section 只由 Control Plane 固定 allowlist 生成，不复制进程环境、
secret、主机路径或机器标识。Git collector 仅在 Capsule workspace 自身存在真实、owned、
非 group/world-writable `.git/` 时运行固定 `/usr/bin/git status`；cwd 固定在该 workspace，
禁用父目录发现、system/global config、optional lock、pager、hooks/fsmonitor/untracked cache，
净化环境并以 2 秒、16 KiB、无 shell、独立 process group 为硬边界。Git 输出是
`project` trust 的 untrusted data，不会成为 platform/workspace instruction；超时、洪泛、
路径或 metadata race 一律拒绝该 Run。Git 不是直接从多线程 Gateway 启动：短命 Python
helper 先进入只允许读取系统运行库与当前 Capsule workspace 的 Landlock 域，再 `execve`
固定 Git；恶意 include、object alternate 或 symlink 因而不能读取 workspace 外路径，且
该子进程无法写入 repository 或其它目录。
保守 admission 会计入实际 renderer、完整 Tool manifest 和 `256` tokens 的 provider
template reserve，并在每个新 Turn 和后续 provider 调用对完整投影 transcript 重算。
超过动态 80% 阈值时只从最旧端移出完整 completed Turn pair，直到不高于 60% 目标或
没有可移出的 pair；durable transcript 不改变，受保护内容仍超出硬预算则 fail closed。
该 tail-window 不会总结、改写或推断被省略内容，不能描述为语义摘要或无损压缩。

Control Plane 与 Worker 只通过继承的 stdin/stdout 交换版本化 NDJSON。Model Broker
IPC v2 只接受受信 plan/toolset 的 ID 与 digest 引用、迭代号和已经由控制面持有的 Tool
result call IDs，不接受 prompt、Tool schema、endpoint 或模型参数。请求 ID、轮次、
Tool result、frame schema、行数、总字节、输出字节、轮数和 timeout 都有上限；Worker
没有 socket 能力，也不知道模型地址。不要将任意 URL 或通用代理能力加入该 IPC。

`ToolSpec` v2 是模型暴露与执行路径的共同契约，包含稳定 Tool/provider ID、受控
string/integer/boolean schema vocabulary、结构化 text result、有限 progress、结果
trust/source、读写/风险/并发属性、timeout、cancellation 和输入输出字节上限。
Control Plane 每次 Run admission 从密封 ToolCatalog 与 deny-precedence policy 重新解析
EffectiveToolSet，并把 catalog/policy/toolset digest 固化在 TurnRuntimeSnapshot；provider、
ContextPlan、Worker 和 executor 使用同一 canonical manifest。Worker 命令只获得有效 Tool ID
列表与 plan reference，必须对密封 catalog 重解析并匹配摘要，不能获得环境、credential、
文件句柄或 executor ambient authority。为未来 permission broker 定义的 ToolUseContext 也只含
Agent/generation/run/call/tool/policy/args/expiry 引用。当前有效集合只能是只读
`builtin/echo`。每个 TurnRuntimeSnapshot 目前最多 2 次顺序 Tool 调用；每次结果必须以
原 call ID 回流，达到预算后 provider ToolSet 收窄为空。重复、乱序、并行或第三次调用
按协议错误拒绝。

Broker 全局 semaphore 最多允许 2 路并行 Ollama stream。其它模型请求只能在 Control
Plane 的最多 4 个 active Run 容量内等待；等待 30 秒仍无 slot 时返回可重试
`model_busy`，不建立无界队列或额外 provider 连接。
Provider 原始流限制为 4096 帧，并合并为最多 128 个 content IPC frame，避免合法流在
Worker/event 上限前失配。Broker error 的 code/retryability 只由 Control Plane 持有并
绑定到 canonical terminal；Worker 的泛化错误不能覆盖它。

exact provider request 完成编码和 admission、但尚未释放 HTTP 前，Broker observer 先
绑定 runtime transcript Token 估算、request byte count 和域分隔 SHA-256 digest，再由
Control Plane 发布唯一 `model.request.started`；正常、错误或取消收敛时发布唯一匹配的
`model.response.finished`。新 Run 以 `run.started.protocol_features` 明确声明该协议，
iteration 连续且 request/response 必须一一配对。事件不含 messages、Tool schema、响应
正文或 provider frame；digest 只用于对账。observer 写入失败时不得发送 HTTP，慢 observer
期间仍保持单 Run single-flight。

## Worker confinement

每个 Run 启动一个新 session/process group 中的 Worker，并在读取用户消息前依次：

1. 关闭并 unshare 所有标准错误以上的继承 FD；
2. 设置 `umask 077`、CPU/地址空间/文件大小/FD/进程数 rlimit、non-dumpable、
   `no_new_privs` 和 parent-death `SIGKILL`；
3. 安装 Landlock ABI 6+ 文件、TCP、abstract Unix socket 和 signal scope 规则；
4. 安装与 `x86_64`/`aarch64` 绑定的 seccomp filter；
5. 向 Control Plane 发送 sandbox attestation，由控制面再通过 `/proc` 复核父进程、
   `NoNewPrivs` 和 seccomp 状态；
6. 只有验证通过后，Control Plane 才发送用户消息。

Worker 可读取其专属解释器、当前源码、显式 staged input、当前 Run 根和最小系统运行
库；它没有直接文件写权限。seccomp 拒绝 socket/socketpair、fork/clone/exec、mount、
namespace、跨进程 inspection/signalling、持久 IPC、memfd、`io_uring`、文件元数据
修改和显式 sync 等不需要的内核接口。缺少任一必要能力时 Run 必须拒绝，不能降级成
未隔离执行。

当前唯一 Tool 是进程内、只读、输入与结果各 `8192` UTF-8 bytes 上限的
`builtin/echo`。Tool schema 同时带 JSON Schema 字符长度提示和项目自有 UTF-8 字节限制；
执行前后以字节限制为安全边界。它不证明任意 Skill、Shell、MCP 或文件工具安全；这些
能力当前禁止。

## 文件系统、进程和清理

- 所有受管路径必须位于 checkout，逐组件拒绝链接、错误所有者、特殊文件和异常
  hardlink。Agent/Run ID 必须匹配严格格式。
- Agent 持久状态在 `data/agents/<agent-id>/`；环境与 Run 临时态在
  `.runtime/agents/<agent-id>/`。各 Agent 之间不共享这些可写路径。
- Conversation、Turn、canonical Run events、`run_journal_state`、UI snapshot、operation
  ledger 和 provider usage 位于该 Agent 的私有 `state.sqlite`；
  conversation/run ID 都按选定 Agent scope 查询。run-only prototype alias 固定默认 Agent；
  通用路径必须显式携带 registry 中 active Agent ID。
- Gateway 为每个 active Agent generation 独立创建有界 `QueryEngineRegistry`；每个 Engine
  永久绑定一个 conversation ID，并在 stream/cancel 前核对 live RunRecord、在 replay 前
  核对 SQLite Run identity 的 Agent 与 Conversation；把 foreign Run 交给错误 Engine handle
  时按不存在处理。该检查是
  内部 identity consistency，不是用户/租户授权：public run-only API 为 live stream/cancel
  使用 RunRecord、为 replay 使用 durable Run identity 找到所属 Engine；当前所有认证
  session 本来就共享 single-operator 管理面。Engine 不缓存消息、event、ContextPlan、模型连接
  或 Worker capability，因此不是第二个状态/
  权限事实源；最多 100 个实例，Conversation 删除后旧 handle fail closed 并立即从
  Registry 驱逐。
- 一个 Conversation 同时只允许一个 active Run。数据库约束和控制面校验共同拒绝并发
  第二轮；ContextPlan 使用的 completed-history snapshot 带 revision，`begin_turn` 以
  compare-and-swap 拒绝编译期间发生的历史漂移。有 active Run 时删除也被拒绝，调用方
  必须先取消或等待唯一终态。删除成功会在事务中移除该会话的 Turn、Run events、
  journal metadata、snapshot 和关联 ledger，
  但这是应用层删除，不承诺 SSD 的取证级物理擦除。
- Run 根是 `.runtime/agents/<agent-id>/runs/<run-id>/`，包含独立 HOME、TMP、XDG、
  input、work 和 output。终态后在确认进程组消失时整体删除。
- PID 文件是当前 uid、权限 `0600`、单 hardlink、固定大小/shape 的原子身份记录。
  `gateway.pid` 同时绑定 supervisor 与 Web child；停止前重新校验两者 start marker、
  exact argv、cwd、同一 PGID 及直接父子链。non-dumpable Worker 只有在自己的 marker/
  PGID/NNP/seccomp 状态、exact PID record 和 `PPid=已验证 Web child` 同时成立时才可由
  sibling lifecycle 进程信号；独立 exact-argv/seccomp 伪装不构成 kill 权限。停止顺序
  为先 Worker 后 Gateway，端口或裸 PID 永远不构成 kill 权限。
- 启动和停止由私有 `flock` 串行化。非法记录指向活进程时 fail closed，要求人工调查，
  不冒险误杀。
- `--qualification-sync-count` 是显式、非默认 RR 模式：只能加载
  `.runtime/qualification-sync/` 中固定 owner-only 路径且 digest 匹配的 shared object 与
  4096-byte counter page，clean-env 只按 supervisor → Gateway → Worker 收窄固定 role；
  Web/API/Agent/模型不能提供 preload 路径或开关。Worker 为原子计数持有该页的共享映射，
  因此该页只是受信代码资格观测，不是面对已攻陷 Worker 的防篡改审计账本，也不参与任何
  运行时授权或安全决策。正常启动不会创建这项 capability。

通用 Agent lifecycle 已实现持久 provisioning/active/upgrading/deleting 状态、generation
staging/promotion、runtime admission fence/drain、进程引用证明、data/runtime/env 删除和
恢复收敛。逐一 staging/rename/commit 故障注入、完整资产清单、双架构与 SSD workload
资格仍未关闭，因此只能称为 walking skeleton，不能称为 production 安全删除。

## 持久化与 SSD 磨损控制

- Conversation/Turn 状态和 canonical events 共用 Agent 私有 `state.sqlite`。Turn 接受
  与 `run.started`、Turn 完成与 terminal 分别在同一语义事务提交；completed assistant
  只写一次，不在流式期间反复重写完整会话。
- `assistant.block.delta` 为 ephemeral，不写 SQLite；只在语义边界 append durable
  event。Gateway 重启时遗留 `running` Turn 原子收敛为 `interrupted`，partial output
  不提交为历史。恢复只读取有数量、字节和 JSON 结构上限的 durable 序列；开放 block
  或 Tool 必须先按 canonical 顺序闭合，再从 Run 接受时保留的 cursor high-water
  `512` 之后写唯一失败终态。无法由 ephemeral delta 或该 reserved range 解释的
  seq gap、非法状态或超限输入整体回滚并 fail closed；已发 cursor 不重用。旧
  Worker、活跃 SSE 和 ephemeral delta 不跨进程恢复。
- 持久 replay 是与 live SSE 分开的认证只读边界。它按 SQLite Run identity scope
  查询，在返回分页前严格验证整个有界 Run、SQL/envelope 绑定、canonical kind payload、
  当前 ToolSpec、状态机和 digest-bound `run-ui-v2` snapshot document；历史
  `run-ui-v1` 仍按其旧 shape 严格验证，不能伪装成带 model boundaries 的新投影。损坏时
  不泄露一个合法 prefix。gap 显式标记
  ephemeral loss 或 retention，不合成原文。该 snapshot 是 UI 投影，不是可进入
  ContextPlan 的 transcript/summary。没有 live RunRecord 时，同一个 events endpoint 会
  冻结到 durable source，以 `stream.gap`/`stream.snapshot` 明示缺口并发送已收敛终态；
  它不会重建旧 Worker、模型连接或遗失的 ephemeral delta，也不会把崩溃前活跃流无缝续跑。
- 每次 provider 调用把 usage `started`、受信的 model/profile/ContextPlan/预算绑定和一个
  不含正文的 request boundary 放入同一事务；完整终帧经校验后，usage `complete` 与唯一
  response boundary 也在同一事务提交，终态事务把遗留 `started` 转为
  `incomplete`。汇总只包括 complete 调用，任一 incomplete 都使 `usage.complete=false`；
  terminal usage 与 ledger 不一致时整个 terminal/Turn/snapshot 事务回滚。价格未知
  保留为 `NULL`，不当作零成本。Gateway 恢复遇到开放 request 时先补一个
  `error/control_restarted` response boundary，再发布失败终态。完整 prompt、逐 token
  usage 或 provider frame 不落盘；因此每次 provider 调用固定只增加两个 event journal
  写入和两个 ledger/event 事务，不随流帧或输出 token 数增长。
- 已接受 Run 中途无法追加 journal 时，live stream 只能使用明确的 ephemeral closure/
  `journal_unavailable` terminal。若 Conversation 事务仍可写，它原子终止 Turn、把
  provider `started` 收敛为 `incomplete`、把遗留 dispatched operation 收敛为
  `outcome_unknown`、删除 partial events/snapshot，并把
  `run_journal_state` 置为零 event/byte 的内部 `pruned` tombstone；durable replay 明确
  unavailable，retention 不会永久保留半截 Run。整个 SQLite 不可写时不伪造上述 durable
  清理，恢复可写后的 startup recovery 才能收敛原有 durable prefix。
- `operation_ledger` 只是未来副作用 broker 的底座：按 request/idempotency digest 先写
  `intent`，释放外部操作前写入 `dispatched` 及 executor identity，再记录终态 outcome。
  重启把遗留 dispatched 转为 `outcome_unknown`，不伪造 outcome digest，不自动重放。
  当前 Echo 不使用特权 executor，因此该表不是 exactly-once 副作用保证。
- SQLite 使用 WAL、`synchronous=NORMAL`、16 MiB journal limit 和有界数据库容量；
  主文件及 sidecar 每次使用均验证为私有普通文件。会话删除不触发逐次 `VACUUM` 或
  全库重写。
- 每 Run 最多 512 events、1 MiB live bytes、256 KiB durable bytes；journal 保留
  最近 256 Runs，控制面内存保留 64 Runs。保留淘汰对每个旧 terminal Run 先严格
  验证并生成 snapshot，再在一个 `BEGIN IMMEDIATE` 事务中删 events 并转为
  `snapshot_only`；活跃 Run 不 prune，损坏或不一致就回滚。snapshot-only replay 只返回
  经校验 snapshot 和 retention gap。
- replay page 最多 128 events，cursor 不超过 1,000,000，全 Run 验证不超过 512
  events / 256 KiB，单 event 和 snapshot 各不超过 65,536 bytes。operation ledger 每
  Agent 最多 4,096 条，provider usage 每 Run 最多 64 条，`state.sqlite` 最大
  512 MiB。达到上限时拒绝或 fail closed，不无界扫描/扩容。
- Gateway 日志以 5 MiB 段轮转，保留 3 个备份；按大小和时间批量 flush，不逐 chunk
  fsync，也不保留无界 stderr。
- Run 树按每秒至多一次检查 1,024 entries、16 MiB logical / 32 MiB allocated 上限；
  超额立即终止 Worker。Worker 的直接写入已由 Landlock 阻止。

这些控制降低明显的写放大路径，但当前尚无长期 soak、真实 SSD SMART 对照或多负载
磨损基线。新增 telemetry、历史、cache 或 artifact 功能必须提交写放大分析和硬上限。

## 错误和秘密处理

不把 token、cookie、用户消息、模型原文、Tool arguments、环境或攻击者控制的异常
文本写入日志。对外错误使用固定 code/message；内部日志只记录有界阶段与异常类型。
任何 debug 或 trace 功能都必须先做递归脱敏、深度/条目/字符串上限和 retention。

不要提交真实 token、cookie、密码、私钥或含凭据的 URL。怀疑泄露时运行
`./set-access-token.sh` 并审查日志/历史；不要手工原地修改 secret 文件，也不要只从
当前文件删除后继续使用同一秘密。

## 已知未完成项

- HTTP 没有 TLS，未建立可信 reverse-proxy 模型；
- 通用 Capsule lifecycle 已贯通，但逐一崩溃点、双架构、soak/SSD 资格尚未关闭；
- 没有任意 Skill/Shell/MCP/File capability broker 或独立 Skill sandbox；
- 已有可跨 Gateway 重启恢复的 durable Conversation/Turn transcript、受限 Run replay/
  UI snapshot，以及 live record 缺失时明确 gap/snapshot 的 SSE durable fallback；但没有
  旧 Worker/模型重建、崩溃瞬间的活跃 SSE 无缝续接或 ephemeral delta 恢复；
- 已有按模型窗口动态计算、以完整 completed Turn pair 为单位的 tail-window；没有模型
  summary、语义 compaction snapshot 或 compaction event，不能声称 UI snapshot 是语义摘要；
- 当前 ContextPlan 已读取 Capsule 精确 `workspace/CLAUDE.md` 与有界 Git/UTC context，但
  没有文件或 Bash capability；
- 尚未完成多架构 cold-checkout、load/soak、故障注入和正式 release gate；
- 模型服务属于独立信任域，其可用性和隐私不由 Worker 沙箱保证。

## 漏洞报告

请通过仓库所有者提供的私有渠道向 security maintainers 报告。报告应包含受影响版本、
最小复现、影响和缓解建议，但不要在公开 issue 中发布 token、用户数据或可直接利用的
细节。若尚无私有渠道，先请求一个安全联系方式，再发送敏感材料。

---
owner: security-maintainers
status: maintained
last_reviewed: 2026-07-18
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

- `GET /health` 和静态 UI 资源无需登录；会话、Run、事件和取消接口需要有效登录
  session。会话创建/删除、发起 Turn 和取消 Run 还必须通过 CSRF 与 exact same-origin
  检查。
- 首次启动在 `.runtime/secrets/web-bootstrap-token` 创建 256-bit 随机 token，文件
  权限为 `0600`。服务只比较 token，不应记录或回显它。
- 登录有来源级失败速率限制和会话容量/有效期上限。session 使用 HttpOnly cookie；
  状态修改请求还要求匹配的 CSRF token。
- Gateway 校验 Host、same-origin/Origin、Content-Length 和实际流式请求大小；JSON
  body 上限为 16 KiB，用户消息上限为 8,192 UTF-8 bytes。
- 响应包含 CSP、frame、MIME sniffing、referrer 和 no-store 等安全
  header。前端用 `textContent` 渲染模型与事件内容。
- Uvicorn 单 worker、有界并发/backlog/header、短 keep-alive，并关闭 proxy header
  信任与 server banner。
- 当前 bootstrap token 代表整个本地项目，登录 session 没有独立用户 principal；所有
  已认证浏览器都能访问固定 demo Agent 的同一组会话。这是 single-operator 边界，
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
已完成的 user/assistant pairs、本轮用户消息、模型画像和有效 ToolSpec 编译并以 digest
固定。只有状态为 `completed` 且 assistant 完整落在终态事务中的 pair 可以进入历史；
failed、cancelled、interrupted、discarded block、ephemeral delta 和 Tool result 都不能
污染下一轮上下文。当前尚未注入 workspace `CLAUDE.md`。完整 plan 和隐藏指令不得进入
Worker、event、日志或浏览器；`run.started` 只公开 plan ID/digest、section/历史计数、
窗口策略、估算值和预算元数据。
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

`ToolSpec` 是模型暴露与执行路径的共同契约，包含稳定 Tool/provider ID、版本、严格输入
schema、读写/风险/并发属性、timeout 和输入输出字节上限。当前有效集合只能是只读
`builtin/echo`；Control Plane 会校验 plan 的 toolset digest，拒绝 Worker 伪造或漂移
后的 Tool 集合。Echo 只在首轮 provider 请求中暴露；一次受信结果回流后 provider
ToolSet 收窄为空，重复 Tool call 按协议错误拒绝。

Broker 全局 semaphore 最多允许 2 路并行 Ollama stream。其它模型请求只能在 Control
Plane 的最多 4 个 active Run 容量内等待；等待 30 秒仍无 slot 时返回可重试
`model_busy`，不建立无界队列或额外 provider 连接。
Provider 原始流限制为 4096 帧，并合并为最多 128 个 content IPC frame，避免合法流在
Worker/event 上限前失配。Broker error 的 code/retryability 只由 Control Plane 持有并
绑定到 canonical terminal；Worker 的泛化错误不能覆盖它。

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
- Conversation、Turn 和 canonical Run events 位于该 Agent 的私有 `state.sqlite`；
  conversation/run ID 都按固定 Agent scope 查询。浏览器不能指定另一个 Agent ID。
- 一个 Conversation 同时只允许一个 active Run。数据库约束和控制面校验共同拒绝并发
  第二轮；ContextPlan 使用的 completed-history snapshot 带 revision，`begin_turn` 以
  compare-and-swap 拒绝编译期间发生的历史漂移。有 active Run 时删除也被拒绝，调用方
  必须先取消或等待唯一终态。删除成功会在事务中移除该会话的 Turn 和关联 Run events，
  但这是应用层删除，不承诺 SSD 的取证级物理擦除。
- Run 根是 `.runtime/agents/<agent-id>/runs/<run-id>/`，包含独立 HOME、TMP、XDG、
  input、work 和 output。终态后在确认进程组消失时整体删除。
- PID 文件是权限为 `0600` 的原子身份记录。停止前重新校验 checkout、PID、PGID、
  Linux start marker、cwd、解释器和模块；端口或裸 PID 不构成 kill 权限。
- 启动和停止由私有 `flock` 串行化。非法记录指向活进程时 fail closed，要求人工调查，
  不冒险误杀。

通用 Agent create/upgrade/delete 尚未实现。P8 要求未来删除 Agent 时同时停止其全部
Run、验证无进程引用、删除持久目录/环境/日志并做残留审计；在该流程和负面测试完成
前，不得声称支持安全删除任意 Agent。

## 持久化与 SSD 磨损控制

- Conversation/Turn 状态和 canonical events 共用 Agent 私有 `state.sqlite`。Turn 接受
  与 `run.started`、Turn 完成与 terminal 分别在同一语义事务提交；completed assistant
  只写一次，不在流式期间反复重写完整会话。
- `assistant.block.delta` 为 ephemeral，不写 SQLite；只在语义边界 append durable
  event。Gateway 重启时遗留 `running` Turn 原子收敛为 `interrupted`，partial output
  不提交为历史。恢复只读取有数量、字节和 JSON 结构上限的 durable 序列；开放 block
  或 Tool 必须先按 canonical 顺序闭合，再写唯一失败终态。无法解释的 seq gap、非法状态
  或超限输入整体回滚并 fail closed；旧 Worker、活跃 SSE 和 ephemeral delta 不跨进程恢复。
- `run.started` 只持久化 ContextPlan 的摘要元数据；每个 terminal 的 `usage` 都由
  provider 报告并经 Control Plane 对照模型 profile 校验、累计，Worker 不能自报或
  修改。`complete=false` 表示当前 provider 轮次未产生可校验终帧，累计值只代表此前
  已验证轮次。完整 prompt、逐 token usage 或 provider frame 不落盘。
- SQLite 使用 WAL、`synchronous=NORMAL`、16 MiB journal limit 和有界数据库容量；
  主文件及 sidecar 每次使用均验证为私有普通文件。会话删除不触发逐次 `VACUUM` 或
  全库重写。
- 每 Run 最多 512 events、1 MiB live bytes、256 KiB durable bytes；journal 保留
  最近 256 Runs，控制面内存保留 64 Runs。
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

不要提交真实 token、cookie、密码、私钥或含凭据的 URL。怀疑泄露时停止服务、轮换
`.runtime/secrets/web-bootstrap-token` 并审查日志/历史；不要只从当前文件删除后继续
使用同一秘密。

## 已知未完成项

- HTTP 没有 TLS，未建立可信 reverse-proxy 模型；
- 只有固定 demo Agent，没有通用 Capsule provisioning、upgrade、delete；
- 没有任意 Skill/Shell/MCP/File capability broker 或独立 Skill sandbox；
- 已有可跨 Gateway 重启恢复的 durable Conversation/Turn transcript，但没有旧 Run
  Worker 重建、durable event replay API、SSE gap repair 或活跃流跨进程续接；
- 已有按模型窗口动态计算、以完整 completed Turn pair 为单位的 tail-window；没有模型
  summary、可恢复 snapshot 或 compaction event，不能声称存在语义摘要；
- 当前 ContextPlan 尚未读取 workspace `CLAUDE.md`，也没有文件或 Bash capability；
- 尚未完成多架构 cold-checkout、load/soak、故障注入和正式 release gate；
- 模型服务属于独立信任域，其可用性和隐私不由 Worker 沙箱保证。

## 漏洞报告

请通过仓库所有者提供的私有渠道向 security maintainers 报告。报告应包含受影响版本、
最小复现、影响和缓解建议，但不要在公开 issue 中发布 token、用户数据或可直接利用的
细节。若尚无私有渠道，先请求一个安全联系方式，再发送敏感材料。

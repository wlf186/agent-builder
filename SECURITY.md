---
owner: security-maintainers
status: maintained
last_reviewed: 2026-07-24
review_cycle: quarterly
---

# Security policy and trust boundaries

本文件描述 `0.2.0` single-operator 本地 release 实际存在的安全边界。它不是形式化证明、
多租户认证或互联网暴露许可。当前 HTTP listener 没有 TLS，只能位于受信、防火墙保护的
网络；完整部署合同见 [release runbook](docs/design/release.md)。

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
  header。用户、事件、错误和命令结果使用 `textContent`；完成的 assistant 消息由 DOM
  白名单解析有限 Markdown，原始 HTML 保持文本，URL 只允许无 userinfo 的 http/https，
  外链固定 `noopener noreferrer nofollow`，不解析图片或加载其它远程资源。
- Uvicorn 单 worker、有界并发/backlog/header、短 keep-alive，并关闭 proxy header
  信任与 server banner。
- 当前 bootstrap token 代表整个本地项目，登录 session 没有独立用户 principal；所有
  已认证浏览器都能管理 registry 中全部 Agent 及其会话。这是 single-operator 边界，
  不是多用户、租户或会话所有者隔离。

不要通过反向代理公开服务。当前 Uvicorn 明确不信任 proxy header，`Secure` cookie 开关
也不会建立 TLS/forwarded-header/Origin 合同；这些能力必须作为独立安全变更实现和测试。

## 模型边界

网络连接只由受信 Control Plane 建立。Ollama host、port 和 model 是代码固定值；
context/predict 参数只能由受信模型画像与运行策略生成，不能从请求、Agent 或 Worker
覆盖。启动时必须完成 DNS/IP 限定、服务健康、模型存在、Tool 能力和 `/api/show`
原生上下文窗口资格检查。当前服务报告 `262144` tokens；Control Plane 将运行窗口
封顶为 `32768`，预留 `4096` 输出 tokens，得到 `28672` 的硬输入预算。若未来模型报告
更小窗口，预算必须随之缩小；不信任模型名称或调用方自行声明的容量。

ContextPlan 由受信 Control Plane 从固定 platform contract、Capsule generation、该会话
已完成的 `CompletedTurnContext` bundles、本轮用户消息、Capsule prompt-source 快照、模型画像和
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

每个新 Run 还持久化一条最多 16 KiB 的 `context-projection-v2` boundary。它绑定
Conversation revision、Agent generation、model/profile、instruction/history/recent segment、
compression、ToolSet/policy 和 renderer identity。普通 collapse 不保存 prompt/section/message
正文；semantic summary 时只额外保存模型生成的有界派生摘要、来源/策略/请求 digest 与
校验后 usage，不保存被折叠原文或 provider 请求正文。复用必须逐字段重建匹配；不匹配 fail
closed 并重算。CAS replacement 保留上一个
完整 boundary 直到新值事务提交，读取本身不触发模型或 Tool。该表随 Conversation 删除
级联清理，每 Run 单行，不按 token/chunk 或 compact 次数追加。

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
达到动态 80% 阈值时，deterministic collapse 只移动最旧的完整 completed Turn bundle，
替换成内容无关的 receipt marker，至少保留最近一个完整 bundle，并在 collapse 后重新执行
admission；durable transcript 不改变，所有可折叠旧 bundle 耗尽后仍超出硬预算则 fail
closed。receipt 绑定来源历史、被折叠/保留 message IDs、内容 digest、renderer 与 policy，
不会总结、改写或推断被省略内容，不能描述为语义摘要或无损压缩。旧 tail boundary 只能
按旧版本解码，不会静默套用新 renderer。

语义摘要 v1 永久禁止生成；Broker 的兼容入口固定返回 `summary_v1_disabled`，只保留严格旧
boundary decoder/replay。Web、Worker、Agent prompt 和 request body 均不能重新开启。
重新资格的 `semantic-summary-v2` 由 operator 在启动前以
`HARNESS_V2_SEMANTIC_SUMMARY_V2=0|1` 固定，当前默认 `1`；生命周期脚本只允许该布尔值穿过
受控环境且不向子进程转发 credential。

v2 规则只位于独立、可信 Provider system message；源 completed Turn JSON 位于 user/data
role，生成结果在主模型中也仅作为 `UNTRUSTED_HISTORICAL_SUMMARY_JSON` conversation data，
不得与 platform/Agent/workspace system sections 合并。实际 request bytes 生成 prompt digest，
policy digest 覆盖 source/output/item/aggregate 上限、timeout/circuit、模型、空 ToolSet 和
renderer。profile/prompt/policy/source/parent/renderer 任一漂移均拒绝复用；恶意原文、模型
复述、Unicode/control/JSON/Markdown 不能提升权限。摘要正文不进入公开 metadata、日志或
事件，auxiliary usage 独立保存在有界 projection 中。

summary row 是 Conversation-scoped 常数上限派生状态；相同 source 跨 Turn/重启复用，扩展
source 只发送 parent snapshot + 新 completed bundle。超限、timeout、invalid、circuit open、
持久化失败或摘要使投影超预算时安全丢弃并回退 deterministic collapse，不修改 canonical
transcript。紧急回滚设 gate 为 `0`，保留新表、v1/v2 decoder 和完整 Conversation，不执行
destructive migration。完整威胁/资格记录见
[Context reliability remediation plan](docs/plans/context-reliability-remediation.md)。

受信 `ModelCatalog` 是 operator-owned 构造依赖，当前只允许一个经 DNS 全量校验后固定
numeric address 的 Ollama endpoint；请求只能选择目录内稳定 `model_id`，不能增加 endpoint、
provider model 或 generation options。`/api/models` 不返回 host/port，Worker IPC 和 Run
事件也不携带 endpoint。每个 Turn 固化 catalog/profile/generation-options digest；切换只在
Turn 之间发生，并从完整 durable transcript 重编。无 Tool 能力的 profile 得到空 ToolSet，
资格缺失、能力漂移或未知 model ID 一律 fail closed。当前受信 generation policy 对普通无
Tool response 固定 `temperature=1.0/top_p=0.95/top_k=20/presence_penalty=1.5/seed=0`，对
Tool selection 固定 `temperature=0/seed=0`；请求正文、浏览器和 Worker 都不能改变相位或
参数。

Control Plane 与 Worker 只通过继承的 stdin/stdout 交换版本化 NDJSON。Model Broker
IPC v2 只接受受信 plan/toolset 的 ID 与 digest 引用、迭代号和已经由控制面持有的 Tool
result call IDs，不接受 prompt、Tool schema、endpoint 或模型参数。请求 ID、轮次、
Tool result、frame schema、行数、总字节、输出字节、轮数和 timeout 都有上限；Worker
没有 socket 能力，也不知道模型地址。不要将任意 URL 或通用代理能力加入该 IPC。

`ToolSpec` v3 是模型暴露与执行路径的共同契约，包含稳定 Tool/provider ID、受控
string/integer/boolean schema vocabulary、结构化 text result、有限 progress、结果
trust/source、读写/风险/并发属性、timeout、cancellation 和输入输出字节上限。
Control Plane 每次 Run admission 从密封 ToolCatalog 与 deny-precedence policy 重新解析
EffectiveToolSet，并把 catalog/policy/toolset digest 固化在 TurnRuntimeSnapshot；provider、
ContextPlan、Worker 和 executor 使用同一 canonical manifest。Worker 命令只获得有效 Tool ID
列表与 plan reference，必须对密封 catalog 重解析并匹配摘要，不能获得环境、credential、
文件句柄或 executor ambient authority。Capability Broker 的 ToolUseContext 也只含
Agent/generation/run/call/tool/ToolSet/policy/args/preview/expiry digest 引用。Control Plane
以 deny precedence 解析 `allow|ask|deny`；无交互 `ask` 变为 deny，Web 只能通过认证、
exact same-origin、CSRF action 解析既有 pending ID，不能创建 approval、替换 arguments/
preview 或提交内部 result/identity。approval、过期、取消与 executor intent/outcome 都在
Agent SQLite 中有界持久；重放只读，遗留 dispatched 操作只变为 `outcome_unknown`，不重试。
当前有效 Tool 集合是只读 `builtin/echo`、`file/stat`、`file/read_text`、`file/glob` 和
`file/grep`。
文件 Tool 通过同一 stdio 上严格绑定的 `capability.request/response` 调用 Control Plane，
Worker 不获得 workspace fd 或路径访问权。每个 TurnRuntimeSnapshot 目前最多 2 次顺序 Tool 调用；每次结果必须以
原 call ID 回流，达到预算后 provider ToolSet 收窄为空。重复、乱序、并行或第三次调用
按协议错误拒绝。

canonical Tool result 与模型视图隔离。当前 Echo canonical result 最大 8192 UTF-8 bytes；
Control Plane 按冻结 v3 policy 将单个 provider projection 限为 4096 bytes、单 Run 历史
限为 16 KiB。超限值不做 attacker-controlled 前缀/后缀截断，而变成只含 call ID、原始
bytes、domain-separated digest 和固定 reason 的 receipt；因此内容不能借“截断边界”注入
伪指令。receipt 仍是 untrusted Tool data。projection 只在内存生成，每个 provider request
在其后重新 admission，不创建 cache/artifact、不逐 token/chunk 写盘；canonical event
保持原值并受既有 Run/Agent journal 上限与 retention 约束。

Broker 全局 semaphore 最多允许 2 路并行 Ollama stream。其它模型请求只能在 Control
Plane 的最多 4 个 active Run 容量内等待；等待 30 秒仍无 slot 时返回可重试
`model_busy`，不建立无界队列或额外 provider 连接。
Provider 原始流限制为 1 MiB/4096 帧，并合并为最多 128 个 content IPC frame，避免合法流
在 Worker/event 上限前失配。普通无 Tool response 另有受信 exact-suffix guard：仅在有界
2048-code-point 尾窗内接受 32..512-byte cycle、至少 3 份且累计至少 512 bytes 的证据，
每 64 个新接收 bytes 最多检查一次。命中后只丢弃尚未发出的重复尾部、追加固定 67-byte
marker，并立即关闭当前 HTTP response，不排空、不重试、不继续占用 Broker slot；显式取消
若先被观察到仍优先。Tool schema 阶段、无可见正文和自然 terminal 不得走该成功路径。
Broker error 的 code/retryability 只由 Control Plane 持有并绑定到 canonical terminal；
Worker 的泛化错误不能覆盖它。

exact provider request 完成编码和 admission、但尚未释放 HTTP 前，Broker observer 先
绑定 runtime transcript Token 估算、request byte count 和域分隔 SHA-256 digest，再由
Control Plane 发布唯一 `model.request.started`；正常、错误或取消收敛时发布唯一匹配的
`model.response.finished`。新 Run 以 `run.started.protocol_features` 明确声明该协议，
iteration 连续且 request/response 必须一一配对。事件不含 messages、Tool schema、响应
正文或 provider frame；digest 只用于对账。该 request observer 写入失败时不得发送 HTTP，
慢 observer 期间仍保持单 Run single-flight。

实际 HTTP attempt 另产生内容安全的 `model.transport.attempt` started/finished 诊断对，只含
固定 identity、attempt/max、phase/outcome、elapsed 和 TTFB。一个逻辑调用始终有两个
request/response boundary；若它实际打开 `N` 次 HTTP attempt，则另有 `2*N` 个 transport
diagnostic，总 durable event 数为 `2 + 2*N`（`N` 可为零）。诊断不含 endpoint、
request/response、错误原文、token 或 cookie，也不按 timer/frame 写盘；每秒等待数字只存在浏览器内存。诊断 observer
异常不得触发重复 HTTP 或改变已取得的模型流；任何已经提交的诊断仍须通过严格配对 replay。
同一受信 model/profile 连续两个零首帧 HTTP attempt 失败后，进程内 circuit 30 秒内在获取
slot/打开 HTTP 前快速拒绝；成功首帧立即复位，partial/idle/cancel 不计，表最多 16 项且
Gateway 重启清空。key 只使用 catalog model ID 与 profile digest，不保存 endpoint 明文。

Provider overflow 恢复是窄的 fail-closed 例外，而不是通用重试器。只有 `400|413` 的严格
JSON 单字段错误、匹配 context/media 超限规则且尚未读取任何 provider frame 时，才允许
同一逻辑 iteration 发起一次 attempt 1。受信恢复投影至少保留最近完整 Turn 与当前用户
输入，沿用冻结 model/profile/ToolSet，重新 admission，并以 durable boundary CAS 和稳定
recovery identity 对账。认证、网络、格式、状态 200 部分流、取消、deadline、第二次超限
均不重试。恢复只替换不可变 base projection，不回滚或重放 Tool result、文件 capability、
Worker 事件或未来外部副作用；每笔真实调用有独立 provider ledger，未知 usage 永久标为
incomplete。

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

`builtin/echo` 是进程内、只读、输入与结果各 `8192` UTF-8 bytes 上限的演示能力。
`file/stat`/`file/read_text` 则只在受信 Control Plane 执行：路径必须是最多 32 个组件、
1024 UTF-8 bytes 的规范 workspace-relative path；workspace 和每个父目录通过 dirfd 逐级
`O_NOFOLLOW|O_DIRECTORY` 打开并验证当前 uid、非 group/world-writable 和同一 device，
不使用先 resolve 再普通 open 的路径。最终对象必须是当前 uid、单 hardlink、非
group/world-writable、非稀疏、最多 1 MiB 且读前/读后 identity 不变的 regular file；
symlink、magiclink、xdev、FIFO/socket/device、hardlink、binary/NUL/control text、无效 UTF-8、
增长/替换竞态和 0.5 秒超时全部 fail closed。`read_text` 单次最多返回 4096 bytes/256 行，
byte offset 必须落在 UTF-8 边界，结果带 path identity、完整 content digest、range 与
truncated reason，可供后续 mutation CAS。文件正文是 untrusted Tool data，只进入既有
有界 canonical semantic event，不创建内容日志、sidecar、临时索引或逐 chunk 写入。
`file/glob`/`file/grep` 复用同一 capture，不调用 shell、不先建立路径清单再做普通 open，
也不创建持久索引。每层目录均用 dirfd/no-follow 打开，逐项拒绝 symlink/magiclink/xdev/
cycle、特殊文件、unsafe owner/mode/hardlink/sparse；`.git` 只验证后跳过。全局上限为 depth
16、entries 4096、files 1024、读取 2 MiB、matches 128、result 12 KiB 和 1 秒。每目录仅
收集至 entry cap 的名称用于 UTF-8 稳定排序，候选随后在同一 dirfd 验证，匹配文件立即通过
READ-01 原语捕获。换行文件名由 canonical JSON 转义；结果包含 Capsule provenance、
path/content receipt 和明确 truncation reason。Grep literal 默认使用有界逐行搜索；regex
只接受最多 256 bytes、无 group/alternation/backreference/brace/`*`/`+` 且至多 8 个 `?`
的子集，每行最多 16 KiB。复杂模式、洪泛、取消和 Capsule 删除竞态均 fail closed。
`file/edit` 与 `file/write` 不扩大 Worker 权限：Worker 只发有界 capability request，受信
Control Plane 才能生成 preview、等待审批并提交。现存 target 必须有同一 Run 的完整
`file/read_text` receipt，且审批、dispatch 与 commit 均绑定 Agent generation、canonical
path identity、完整 content digest、新内容 digest 和准确 diff digest；Edit 旧片段必须
恰好匹配一次。create 使用 target-absent + parent identity receipt 和
`renameat2(RENAME_NOREPLACE)`；replace 使用同目录 owner-only temp、文件 fsync、
`RENAME_EXCHANGE`、交换出的旧 inode/content 验证和 parent fsync。没有原地写、普通 rename
覆盖或 symlink-follow fallback。单次内容 8 KiB、Edit old/new 各 4 KiB、diff/preview 4 KiB、
结果 12 KiB、wall 2 秒、恰好两个成功路径 fsync；每个 target 进程内串行。内部
`.agent-builder-write-*` 命名空间禁止作为 target，启动遍历最多 4096 entries、最多清理
16 个 owned/single-link/不超过 1 MiB 的 stale temp，异常即 fail closed。

mutation 遵循 durable `intent → dispatched → outcome|outcome_unknown`。取消只在 atomic
rename 前生效；rename 后若不能证明最终状态，Broker 写 `outcome_unknown`，重启、重放、
重复审批均不能自动再次 dispatch。竞态失败只能保留竞态方完整内容或已批准完整内容，
不会留下 torn file。审批并不把该机制提升为 exactly-once，也不证明任意 Skill、Shell 或
MCP 安全；这些能力当前禁止。

## Allowlisted command runner

`exec/run` 不是通用 shell。catalog 支持固定 `runtime-compile`，以及显式解析的
`bounded-bash` 单 builtin；后者只允许 `printf|pwd|true|false`，拒绝 substitution、backtick、
env assignment、redirect/heredoc、subshell、pipe、glob、rc、git/package/network 命令。
模型不能提交 executable、cwd、PATH、环境或 endpoint。Control Plane
把 Capsule Python 的 device/inode/size/content digest、固定 argv、完整 runtime source digest、
runner policy 和随机 runner ID 绑定进 approval、request digest 与动态 executor identity。
执行前再次核验 source、空的 private `run/output` identity，并通过已打开的 executable FD
从 `/proc/self/fd` 启动，关闭其余继承 FD，避免 path swap。

runner 是独立于 Worker 的 singleton kernel domain：payload 先把 source 设为 read-only、只给
一个 Run output 目录 create/write/remove 权限，默认看不到 Agent workspace 和 checkout
其它内容；随后 seccomp 拒绝 socket、fork/vfork/clone、setsid/setpgid、namespace、mount、
ptrace、后续 exec 和持久 IPC。PIDFD 精确绑定唯一 PID，Control Plane 还复核 PPid、
NoNewPrivs、Seccomp/filter count、Landlock attestation，写一个有界 runner PID record，最后
才发送 release byte。因为沙箱内无法创建或替换进程，该 PID 本身就是完整 descendant
container；process group 只作辅助，不作为逃逸防线。payload 会实际探测 fork/socket/exec
均返回 `EPERM` 后再执行命令。

固定上限是 CPU 10 秒、wall 12 秒、address space 256 MiB、NPROC 1、32 FD、单文件 2 MiB、
最多 256 个/2 MiB source entries、8 MiB allocated output、12 KiB stdout+stderr/result；
环境只含 19 个明确键，不含 PATH、token 或 inherited variable。成功、非零退出、取消、超时
和已知失败都会 kill/wait 精确 PID、关闭 pipe/pidfd、删除 runner record 并有界清空 output；
无法证明释放后的 PID 已停止时转为 `outcome_unknown` 且永不自动重放。compile payload
只做 checked-hash Python compile；bounded Bash 把 normalized AST、固定
`--noprofile --norc -c`、零 redirection、clean env 和 Bash inode/content digest 绑定 one-shot
审批。Landlock 不暴露 workspace 或 Capsule Python，seccomp 仍拒绝进程/网络；唯一允许的
image replacement 用于进入已打开的 Bash FD。两者都不安装依赖、不写 workspace，也不能
证明任意 Bash 安全。

## Background Task boundary

background Task 不是另一条命令执行后门。提交 API 只接受固定 `runtime-compile`，或带有
同一 parser 限制的 `bounded-bash` script；它绑定既有 parent Run、Agent generation、
catalog/executor identity 与 request digest，并复用上述 singleton runner。浏览器不能提供
executable、环境、cwd 或 endpoint。每 Task 使用 `.runtime/agents/<id>/tasks/<task-id>` 独立私有根；
每 Agent 最多 4 个 active、128 个 retained，wall 12 秒、result 16 KiB、notification
4×4 KiB，终态保留 7 天。

TaskStore 只在 queued、running 和 terminal 边界事务提交，stdout/stderr 只在结束后作为受限
结果整体写入。取消使用同一 PIDFD 路径；NPROC=1 与 fork/clone/setsid/socket denial
使监督 PID 成为完整 descendant domain。terminal 只在 output、PID record 和 Task root 清理
后发布。重启把遗留 queued/running 标为 `interrupted`，从不自动 dispatch；激活、stop、
Conversation delete 和 Agent delete 有界检查进程引用并 fail closed 清理。该协议仍不授权
任意 Bash、Skill、MCP 或子智能体。

## MCP/LSP extension boundary

MCP 与 LSP 共用 `extension/call` ToolSpec、deny-precedence policy、one-shot approval、operation
ledger、result projection 和 byte/time/concurrency 限制。release catalog 默认为空，因此未配置
扩展不会暴露给模型。operator 构造期最多注入 8 个 versioned spec；每个只允许固定 protocol、
extension ID、最多 32 个 method 和 credential-free HTTPS JSON-RPC URL。公开 catalog 只返回
ID/protocol/method/transport，不返回 endpoint。

URL 禁止 userinfo、query、fragment、redirect 和 HTTP；authority 必须在精确 allowlist。DNS
首次解析最多 8 个地址，拒绝 loopback/link-local/private/reserved/multicast/unspecified；需要
私网时只能使用 allowlist 中精确的 IP literal:port。dispatch 前重新解析并要求地址集合字节
一致，再直接连接 pinned IP、以原 hostname 做 TLS 验证，阻断 DNS rebinding。request params
8 KiB、wire frame 64 KiB、result 16 KiB、JSON depth 12/nodes 2048、timeout 5 秒、全局并发 4；
JSON-RPC version/id/result-or-error 必须严格匹配。transport 不接收控制面 token、browser
cookie 或 ambient env。本地 stdio/process transport 与请求定义 endpoint 均没有启用路径，
`AGENT_BUILDER_ALLOW_STDIO_MCP` 也不能从 request 设置；当前不提供不受限 fallback。

## Skill package and execution boundary

Skill 信任只来自 authenticated、same-origin、CSRF install API；workspace 文件、prompt、模型
Tool argument 或 archive 内声明都不能自行启用。archive 必须提供匹配 SHA-256，compressed
12 KiB、expanded 16 KiB、精确 2 entries，只允许根级 `skill.json` 与 `main.py`；拒绝加密、
symlink/special、absolute/traversal、未知 compression、CRC/size 漂移、NUL/非 UTF-8 和无效
Python。manifest schema v1 固定 UUID-like ID、semver、entrypoint、display name，并要求
`capabilities=[]`、`dependencies=[]`。因此首版 binary dependency allowlist 为空，source
distribution、PEP 517/build hook、pip 与运行时安装都没有入口。

每 Agent 最多 16 个 Skill。安装/升级在 data/runtime 各自 private staging，源码与独立
`venv --without-pip --copies` 全在 Capsule；同版本冲突，active execution 时 upgrade/delete
拒绝。package/archive/content/source/executable identity 在 prepare、approval 和 dispatch 再次
核验，symlink/hardlink/mode/tamper fail closed。`skill/run` 只有 registry 非空才进入未来
Turn EffectiveToolSet，并始终 ask；input 4 KiB object、source 8 KiB、result 12 KiB、wall 12 秒。
执行使用 Skill 专属 interpreter、clean env 与独立 Run root，在 singleton Landlock/seccomp/
rlimit domain 内，NPROC=1 且 network/fork/exec/workspace/cross-Agent access 被拒，stdout 仅在
终态整体持久化。Agent drain 先取消 Run，Skill delete 清 package/env，Agent delete 继续由
整个 Capsule residual audit 覆盖；无 unconfined fallback。

## Curated research dependency and document boundary

`research-documents` 是唯一内建 dependency bundle；认证、same-origin、CSRF API 只能对当前
Agent 执行 install/delete，不能提交包名、版本、index、wheel、URL、environment path 或
installer 参数。Control Plane 固定精确版本并调用 checkout-local uv 的
`--only-binary :all:`、`--no-deps`，拒绝 source distribution、PEP 517/build hook、ambient
pip config 和 global/user install。HOME/TMP/XDG/uv cache 都在 checkout，data/runtime staging
均为 private；导入与版本验证成功后才分别 rename 发布。相同 identity 幂等复用，不在每个
Conversation 或 Run 重建环境。

environment data/runtime root 同时存在且 metadata/source digest/interpreter identity 完全匹配
才视为 installed；partial、symlink、错误 owner/mode、版本漂移或损坏均 fail closed。变更前
`AgentRuntimeManager` 设置 generation admission fence；active Run 时拒绝，已激活 runtime 先
关闭后再操作，成功后惰性重建 ToolSet。一个 Agent 的环境根从不加入另一个 Agent 的
Landlock rules；显式删除环境清 package/env，Agent delete 的 residual audit 继续覆盖两者。

`document/extract_text` 仅在环境有效时进入未来 Turn 的 ToolSet。输入只能是当前 Capsule
workspace 的 canonical relative path；descriptor walk 拒绝 traversal/symlink/hardlink、
wrong owner/mode、xdev、sparse、oversize 和 read/rename race。PDF 检查 signature；DOCX 在
解析前限制 4096 entries、64 MiB expanded bytes、加密/重复/traversal/special entry，并要求
核心 parts。最多 16 MiB 输入、512 PDF pages、20,000 DOCX blocks、1,000,000 char offset 和
4096 char result window。

可信 Control Plane 把稳定快照写入本 Run private work root；runner 的 Landlock 只读 package
source、该 Agent 精确 dependency environment、系统 runtime 和该 staging file，只允许既有
Run output root 写入。seccomp/rlimit 继续拒绝 network、fork、clone、exec 与不受控资源；
Worker 本身仍没有 workspace/依赖目录权限。执行成功、失败或取消后都会删除 staging，结果
继续标记 untrusted Tool data。该环境不提供网络搜索、浏览器、任意代码或 runtime package
install；这些能力不能由文档内容或模型参数隐式提升。

## Subagent delegation boundary

`agent/delegate` 不是共享内存 graph 或跨 Agent 文件能力。Tool argument 只允许另一个 active
Agent 的固定 ID 和 4096-byte message，且必须经过与文件/执行能力相同的 one-shot permission
与 operation ledger。父 Agent 只持有 parent Run-bound Task、queued/running/terminal 通知、
两条 durable mailbox message 和最多 8192-byte answer；child 的 transcript、workspace fd、
environment、模型连接、Worker PID 和 Capsule 根不进入父 Tool result。

child 由它自己的 RunService 建立 Conversation/Run，继续受 Agent generation、每 Run Worker、
Landlock/seccomp/rlimit、模型/Tool/token/deadline 上限约束。协调器全局最多 2 个 active、每
parent Run 1 个且 depth 1；active child Run 再委派、self/unknown/deleting Agent、oversize
message 或第 2 个同父委派均 fail closed。委派 wall 45 秒，child answer 超限确定性截断；
mailbox 只有 `parent_to_child` 与 `child_to_parent` 两种带 digest 的方向，UI 不从普通文本
猜测它们。

取消父 Run 会撤销 capability future 并取消 child；deadline、child failure 和 Agent drain
产生唯一失败/取消终态。重启把遗留 parent Task 和 link 标为 `interrupted`，绝不自动重放；
父 Conversation 删除先取消 active child，再删除 retained child Conversation/link/Task，
child Agent 删除由 Capsule manager 清完整 generation。跨 Capsule 读写仍由各自 Landlock
根和 descriptor-anchored executor 拒绝，没有 unconfined fallback。

## Slash Command control boundary

所有 `/...` 输入在 authenticated Web adapter 的 Turn admission 之前解析；registry 固定最多
32 项，当前 7 项，完整输入 4096 UTF-8 bytes、最多 4 个各 256 bytes 参数，拒绝 NUL/
CR/LF/tab、空 token、未知 alias 和额外字段。无 Conversation 的 compatibility Run endpoint
遇到 Slash Command 也拒绝，绝不把它降级成 user prompt。命令结果最多 64 KiB，固定声明
`model_invoked=false`、`turn_created=false`，不写 canonical transcript/event journal。

读取命令仍要求登录；所有命令 POST 都要求 exact same-origin、session 和 CSRF。`/cancel`
和 `/context` 的显式 run ID 必须先核对属于当前 Conversation；`/clear` 与 `/compact` 在
active Run 时拒绝。`/permissions` 最多展示 6 个当前 Conversation 的既有 bounded preview，
批准/拒绝仍只能调用 one-shot permission API，Slash result 本身不是 approval。`/model` 和
`/compact` 只返回受限 Web `ui_effect`，下一次普通 Turn 仍由现有 ModelCatalog/ContextCompiler
重新验证，服务端不保存第二份偏好或压缩状态。diff、错误和命令结果不能成为 HTML；仅完成
assistant 消息进入上述受限 Markdown DOM parser。

上下文 preparation 的状态读取同样要求登录并强制 `no-store`；idle 响应的
`operation_id` 必须为 `null`，preparing/cancelling 响应必须携带该次操作稳定的非空 ID。
取消 POST 要求 exact same-origin、session、CSRF，且 body 只能包含从状态 GET 取得的 exact
`operation_id`。该 ID 在 preparation→active Run 的原子 handoff 中保持不变，使取消不会落入
空窗。`202` 只确认取消请求已受理，不证明 Worker/Run 或摘要已经收敛；客户端必须继续 GET
直到 idle。若旧页面持有的 ID 已被新准备替代，control path 返回 `stale` 且不发取消信号，
防止 ABA 把后来操作误取消。

## 文件系统、进程和清理

- 所有受管路径必须位于 checkout，逐组件拒绝链接、错误所有者、特殊文件和异常
  hardlink。Agent/Run ID 必须匹配严格格式。
- Agent 持久状态在 `data/agents/<agent-id>/`；环境与 Run 临时态在
  `.runtime/agents/<agent-id>/`。各 Agent 之间不共享这些可写路径。
- Conversation、Turn、canonical Run events、`run_journal_state`、UI snapshot、operation
  ledger 和 provider usage 位于该 Agent 的私有 `state.sqlite`；
  conversation/run ID 都按选定 Agent scope 查询。旧 run-only 兼容 alias 固定系统 Agent；
  通用路径必须显式携带 registry 中 active Agent ID。
- Session detail 默认只读取最近 32 个 Turn，`limit` 最大 64；`before` 只能使用服务端返回、
  HMAC 认证且绑定 Agent/Conversation/revision/边界的 opaque cursor。伪造、跨会话、旧 revision
  或 Gateway 重启前的 cursor 一律作为 `invalid_session_cursor` fail closed；服务端使用有界
  SQL 页读取，不加载页外 Turn 正文。前端分页只去重/投影，不修改 canonical transcript。非成功 Turn 的公开
  terminal summary 只含安全 code/stage/retryable/duration，不返回 Provider/Worker 原文。
  rename、continue/branch 和 retry 仍要求认证/CSRF；retry 只恢复草稿，不能绕过任何 admission。
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
恢复收敛。故障注入、完整资产清单、支持平台与 bounded SSD workload 已纳入首发资格；
该结论仍不等于 SSD 物理擦除、长期硬件寿命或未支持架构证明。

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
- 每个逻辑 provider 调用把 usage `started`、受信的 model/profile/ContextPlan/预算绑定和一个
  不含正文的 request boundary 放入同一事务；完整终帧经校验后，usage `complete` 与唯一
  response boundary 也在同一事务提交，终态事务把遗留 `started` 转为
  `incomplete`。汇总只包括 complete 调用，任一 incomplete 都使 `usage.complete=false`；
  terminal usage 与 ledger 不一致时整个 terminal/Turn/snapshot 事务回滚。价格未知
  保留为 `NULL`，不当作零成本。Gateway 恢复遇到开放 request 时先补一个
  `error/control_restarted` response boundary，再发布失败终态。每个实际 HTTP attempt 另写
  一对内容安全的 transport started/finished diagnostic；因此实际 attempt 数为 `N` 时，一个
  逻辑调用的 durable EventEnvelope 总数固定为 `2 + 2*N`。provider usage ledger 只在前后
  两个 request/response boundary 事务中转换；完整 prompt、逐 token usage、provider frame
  和 timer tick 都不落盘，写入量不随输出 token 或流帧数增长。
- exact-suffix guard 命中时，response boundary 固定为
  `outcome=repetition_truncated,input_tokens=0,output_tokens=0,usage_complete=false,error_code=null`，
  terminal 固定为 `run.completed.reason=repetition_truncated`。该零值只表达“未取得可信
  terminal usage”，不进入 calibration，也不作为成本或下一轮上下文事实；唯一 committed
  assistant（含 marker）与 Turn terminal 原子写入 `CompletedTurnContext`。下一轮从新
  revision 重新渲染并执行 `AdmissionUpperBound`，soft preview 在获得匹配 scope 的完整
  observation 前明确 unavailable，避免跨计数域猜数。
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
- 只有 GNU/Linux x86_64 纳入首发资格；`aarch64` 路径保留但不受支持；
- Capability Broker 支持 receipt-bound File read/search/Edit/Write、固定 allowlist command、
  无依赖 sandboxed Skill、默认空 HTTPS extension adapter 和 depth-1 subagent；没有任意
  Shell、package install、stdio MCP 或请求级 endpoint；
- 已有可跨 Gateway 重启恢复的 durable Conversation/Turn transcript、受限 Run replay/
  UI snapshot，以及 live record 缺失时明确 gap/snapshot 的 SSE durable fallback；但没有
  旧 Worker/模型重建、崩溃瞬间的活跃 SSE 无缝续接或 ephemeral delta 恢复；
- 已有按模型窗口动态计算的 deterministic collapse、绑定来源/模型/prompt/policy/usage、
  保留最近完整 Turn 的有界 semantic summary snapshot，以及一次性的精确 provider overflow
  recovery；摘要质量不是事实保证，不能声称 collapse marker 或 UI snapshot 是语义摘要，
  也不能把该窄恢复解释为网络/认证/任意模型错误重试；
- `bounded-bash` 只允许 `printf|pwd|true|false` 的单个固定 grammar 命令，不是终端或通用
  Bash capability；
- 首发只有 bounded 真实模型 workload 与专项 chaos/残留 RR；没有长期 soak、HA、物理
  SMART/SSD 寿命或未支持架构资格；
- 模型服务属于独立信任域，其可用性和隐私不由 Worker 沙箱保证。

## 漏洞报告

请通过仓库所有者提供的私有渠道向 security maintainers 报告。报告应包含受影响版本、
最小复现、影响和缓解建议，但不要在公开 issue 中发布 token、用户数据或可直接利用的
细节。若尚无私有渠道，先请求一个安全联系方式，再发送敏感材料。

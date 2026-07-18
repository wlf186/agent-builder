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

当前用户消息会发送到固定的 `iollama:11434/qwen3.5:2b`。部署者必须把该服务及其
数据处理方式纳入自己的信任与隐私评估。

## Web 边界

- `GET /health` 和静态 UI 资源无需登录；Run、事件和取消接口需要有效 session。
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

不要通过反向代理公开服务，除非同时补齐 TLS、可信代理模型、正确的 forwarded
header 验证和相应负面测试；这些尚未实现。

## 模型边界

网络连接只由受信 Control Plane 建立。Ollama host、port、model、context 和 predict
参数是代码固定值，不能从请求、Agent 或 Worker 覆盖。启动时必须完成 DNS/IP 限定、
服务健康、模型存在和 Tool 能力资格检查。

Control Plane 与 Worker 只通过继承的 stdin/stdout 交换版本化 NDJSON。请求 ID、轮次、
Tool result、frame schema、行数、总字节、输出字节、轮数和 timeout 都有上限；Worker
没有 socket 能力，也不知道模型地址。不要将任意 URL 或通用代理能力加入该 IPC。

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

当前唯一 Tool 是进程内、只读、2,048 字符上限的 `builtin/echo`。它不证明任意 Skill、
Shell、MCP 或文件工具安全；这些能力当前禁止。

## 文件系统、进程和清理

- 所有受管路径必须位于 checkout，逐组件拒绝链接、错误所有者、特殊文件和异常
  hardlink。Agent/Run ID 必须匹配严格格式。
- Agent 持久状态在 `data/agents/<agent-id>/`；环境与 Run 临时态在
  `.runtime/agents/<agent-id>/`。各 Agent 之间不共享这些可写路径。
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

- `assistant.block.delta` 为 ephemeral，不写 SQLite；只在语义边界 append durable
  event，不重复重写完整会话。
- SQLite 使用 WAL、`synchronous=NORMAL` 和 16 MiB journal limit；主文件及 sidecar
  每次使用均验证为私有普通文件。
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
- 没有跨重启 Run 重建、durable replay API、SSE gap repair 或完整 conversation store；
- 尚未完成多架构 cold-checkout、load/soak、故障注入和正式 release gate；
- 模型服务属于独立信任域，其可用性和隐私不由 Worker 沙箱保证。

## 漏洞报告

请通过仓库所有者提供的私有渠道向 security maintainers 报告。报告应包含受影响版本、
最小复现、影响和缓解建议，但不要在公开 issue 中发布 token、用户数据或可直接利用的
细节。若尚无私有渠道，先请求一个安全联系方式，再发送敏感材料。

---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-21
review_cycle: quarterly
---

# Release contract and operator runbook

本文件定义首个受支持版本 `0.2.0` 的冻结范围、部署边界、发布门禁、备份恢复和回滚。
版本号以根目录 `VERSION` 为准，并与 `pyproject.toml` 保持一致；能力的协议细节仍由
[架构](architecture.md)、[事件协议](event-protocol.md)、[Capsule](agent-capsule.md)和
[安全边界](../../SECURITY.md)负责。

## 冻结范围

`0.2.0` 是一个受支持的 **single-operator、本地优先运行时**，不是多租户 SaaS 或互联网
边缘服务。纳入范围为：认证 Web UI、Conversation 多轮与 QueryEngine、Slash Commands、
动态模型窗口与上下文折叠/语义摘要、真实 Ollama Broker、规范增量事件/replay/context
inspection、Agent Capsule、每 Run Landlock/seccomp Worker、permission broker、受限文件
读/搜/原子写、固定 allowlist command/background Task、版本化无依赖 Skill、默认空的固定
HTTPS extension catalog，以及 depth-1 显式子 Agent 委派。

以下能力 deferred，空目录或 adapter 不代表支持：任意 shell/terminal、任意 package 安装、
stdio MCP、默认启用的 MCP/LSP、RAG、浏览器/桌面自动化、多用户 principal/RBAC/租户隔离、
无中断 secret 轮换、跨主机调度、高可用、活跃 Worker/ephemeral stream 跨 Gateway 重建、
互联网直连、原生 TLS 和可信 reverse-proxy header 模型。

## 支持矩阵与部署合同

| 项目 | `0.2.0` 支持范围 |
| --- | --- |
| Host | GNU/Linux `x86_64`、glibc 2.28+、`/proc`、Landlock ABI 6+、seccomp |
| Model | 受信 `iollama:11434` 上通过启动资格的 `qwen3.5:2b`；目录可扩展但每个模型须单独资格 |
| Listener | `0.0.0.0:20815`，HTTP，单个 Uvicorn worker |
| Operator | 一个共享 bootstrap-token 信任域；所有登录会话可管理全部 Agent |
| Network | 仅受信、防火墙保护的主机或 LAN；禁止直接互联网暴露 |
| Storage | 当前 checkout 内的 ext4/同等 POSIX 文件系统；不支持网络文件系统语义声明 |

`aarch64` 的 bootstrap 和 sandbox 代码路径保留，但没有原生等价 RR，因而不是本版本支持
平台。当前版本也没有可信 proxy-header 处理；不要通过 reverse proxy 扩大暴露面。若组织
需要公网或跨不可信网络访问，应在独立变更中实现并验证 TLS termination、可信 proxy
地址、forwarded header、Origin/Host、Secure cookie、WebSocket/SSE timeout 与审计合同，
不能只把现有端口转发出去。`HARNESS_V2_COOKIE_SECURE=1` 只设置 cookie 属性，不会单独
建立这套合同。

首次启动生成 256-bit 随机 token。生产性本地部署应保留该随机值，或用
`./set-access-token.sh` 在隐藏 TTY 中轮换成同等强度值；不得使用 argv/env/URL 传 token。
当前轮换会 stop → fsync/atomic replace → restart，使全部旧 session 失效，存在短暂停机；
它不提供多用户凭据、单 session revoke、轮换审批或独立身份审计。8 小时 session、登录
限速、same-origin、CSRF 与 HttpOnly/SameSite=Strict 不能把共享 token 变成多用户边界。

## 容量 envelope 与 retention

这些是硬边界而非吞吐承诺：最多 100 个 Agent、每 Agent 100 个 Conversation、每
Conversation 128 个 Turn；全局最多 4 个 active Run 和 2 个 provider stream。单 Run 最多
512 live event/1 MiB、256 KiB durable replay、60 秒 wall、16 MiB logical/32 MiB allocated
Run tree。每 Agent 最多 128 个 Task（4 active、终态 7 天）、16 个 Skill；extension catalog
最多 8 个且默认空；subagent 全局 2 个、每 parent Run 1 个、depth 1、45 秒 wall。

Gateway 日志每段 5 MiB、3 个备份，总上界 20 MiB并批量刷新。Conversation、event、
permission、operation、provider-call、Task 和 context-reveal audit 均有数据库行数/字节上限；
删除是应用层事务或有界 retention，不承诺 SSD 取证级擦除。当前没有外部 metrics exporter：
`/health`、canonical terminal、受控日志、SQLite audit 和
[qualification summary](qualification.md)是本版本的本地观测面。不要通过 debug 添加
prompt/token/tool arguments；所有新 telemetry 必须先有递归脱敏、采样、大小与 retention。
`/health.release` 报告 `0.2.0`；保留的 `prototype: true` 只标记不可删除的默认 Agent alias，
不是版本成熟度或支持矩阵字段。

资格 RR 验证 16 个顺序真实模型 Turn、拒绝、取消、删除、restart/stop residual、WAL/log/
temp/cache 峰值和进程 I/O，并用专项纵向 RR 覆盖 Skill、Task、extension、command 与
subagent。它是 bounded soak/load envelope，不是长周期可用性、并发压测、设备寿命或
物理 SMART 证明。本机没有设备权限时必须保留 `ssd_smart_not_observed`；应用指标只能证明
未发现当前 workload 的不合理写放大。

## 运维、备份与恢复

所有命令必须从 checkout 根执行。启动、停止、治理和清理见 [README](../../README.md)。
一致性备份会先彻底停止整套服务，只归档 `data/`，拒绝 symlink、hardlink、特殊文件、
越界路径、超限条目/字节和读取中变化；归档为 owner-only `0600`，保存在被忽略的
`backups/`：

```bash
./backup.sh before-upgrade-20260720
./start.sh
```

恢复同样先 stop，完整验证 manifest、路径、类型、大小和 SHA-256，提取到 checkout 内私有
staging 后原子替换 `data/`。旧数据不会删除，而保留在
`.runtime/recovery/pre-restore-<backup-id>/`，便于人工确认或回滚；已有同名 recovery 时
fail closed，绝不覆盖：

```bash
./restore.sh backups/before-upgrade-20260720.tar --yes
./start.sh
```

备份包含 Conversation、Agent registry、Skill registry 和 audit，但不包含可重建环境、缓存、
日志、token 或运行中进程。必须把 token 独立安全保管；恢复数据不会恢复旧 token/session。
跨版本升级前先备份并停止，更新受审源码/锁文件，运行 `./bootstrap.sh` 和 `./release.sh`。
若新版本失败，停止后切回上一已审核的源码版本，运行 `./bootstrap.sh --offline`（缓存存在时）
并恢复升级前备份。不要手工编辑 SQLite/WAL、manifest 或 recovery tree。

## 发布门禁与产物

```bash
./release.sh RR-QUA-YYYYMMDD-01
```

命令仅接受支持平台，依次执行 pinned bootstrap、完整 pytest、文档/秘密/边界治理、
`pip-audit==2.10.1` vulnerability audit 与 CycloneDX SBOM、受管 stop/start、16 Turn 真实模型
qualification、再次 stop 和正常 restart。任一步非零即不发布 PASS。
门禁会先检查当前 UID 的标准 `.runtime/tmp/pytest-of-<user>`；若它是 owned regular
directory，则不跟随内部链接地整体移动到
`.runtime/test-results/release-quarantine/<RR-ID>-preexisting-pytest`，避免历史可再生测试
symlink 污染资格扫描。pytest 根本身是 symlink、owner/type 异常或 quarantine 已存在时仍
fail closed，不删除或覆盖任何内容。

通过后只在 checkout 内生成：

```text
.runtime/qualification/<RR-ID>/summary.json
.runtime/release/0.2.0/<RR-ID>/sbom.cdx.json
.runtime/release/0.2.0/<RR-ID>/agent-builder-0.2.0.tar.gz
.runtime/release/0.2.0/<RR-ID>/release-manifest.json
```

source archive 由 Git tracked + 非 ignored 当前源码构建，归一化时间/owner/mode，唯一允许的
symlink 是 `AGENTS.md -> CLAUDE.md`。manifest 记录 source revision/dirty 状态、每个 source、
archive、SBOM 和 qualification digest。正式对外分发前必须使用 clean reviewed commit；dirty
worktree 产物只能作为本地 release candidate 证据。CI 从 cold checkout 重建环境、跑完整
测试/治理/pip-audit；发布者还必须核对 CI 对应同一 commit、归档 SHA-256、RR result=pass、
SBOM 无已知漏洞，并用 `./start.sh`/`/health`/Web/`./stop.sh` 做一次产物解包 smoke test。

## 已知限制

- 只有 GNU/Linux x86_64 受支持，且真实模型服务是外部信任域和单点依赖；
- HTTP 只适用于受信网络，没有 TLS、可信反向代理、多用户权限或 HA；
- bounded release workload 不能替代长期 soak、真实并发容量规划、SMART 或硬件磨损监控；
- semantic summary 可能遗漏或误述；SQLite transcript 才是权威记录；
- crash 后不重建旧 Worker、provider stream 或 ephemeral delta，未完成 Turn 收敛为
  `interrupted`；
- Skill v1 没有依赖/capability，extension 默认空，subagent 仅一层，命令不是通用 shell；
- 数据删除和 `purge` 不保证 SSD 物理擦除；恢复保留的旧 data 需由 operator 在确认后按
  明确目标清理。

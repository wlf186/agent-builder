---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-19
review_cycle: quarterly
---

# Agent Capsule and Run isolation

Capsule 是一个 Agent 的所有持久状态、可重建环境和临时 Run 的唯一所有权边界。它的
目的不是只“分文件夹”，而是让创建、运行、停止和未来删除都能以 Agent 为原子范围，
不触碰其它 Agent 或 checkout 外目录。

## 当前目录模型

```text
data/agents/<agent-id>/                  persistent, non-reproducible
├── manifest.json                       private identity/generation metadata
├── state.sqlite[-wal|-shm]             durable semantic event journal
├── workspace/                          reserved; current Worker cannot write it
└── artifacts/                          reserved; current Worker cannot write it

.runtime/agents/<agent-id>/              disposable/reproducible runtime
├── worker-env/                          Agent-specific venv, created without pip
├── logs/                                reserved Agent-scoped logs
└── runs/<run-id>/                       one Run, removed after process exit
    ├── worker.pid                       atomic private process identity record
    ├── home/
    ├── tmp/
    ├── xdg/{cache,config,data,state}/
    ├── input/                           explicit future staging boundary
    ├── work/                            Worker cwd
    └── output/                          future broker pickup boundary
```

当前固定 Agent ID 是 `00000000-0000-4000-8000-000000000001`。manifest schema 和
generation 都是 1。通用 Agent registry/provisioning 尚未实现。

## 路径与环境约束

- Agent/Run ID 必须匹配受限 UUID-like 字符集，任何受管绝对路径必须可相对到 repository
  root，且每个 component 都是当前用户拥有的真实目录。
- symlink、特殊文件、错误 owner、异常 hardlink、mount/device 边界变化一律拒绝。
- `worker-env` 位于 Agent runtime root，当前由 checkout interpreter 创建
  `venv --without-pip`；每个 Agent 路径独立，不在 Agent 间共享可写环境。
- Worker 环境只包含最小 PATH 和 checkout-local HOME/TMP/XDG/PYTHONPATH；不继承
  secrets、Conda、用户 site-package 或模型地址。唯一例外是显式
  `start.sh --qualification-sync-count` RR：Control Plane 只透传固定、经 checkout 路径与
  digest 校验的 qualification preload/counter/role，正常运行不存在该 capability，详情见
  [qualification contract](qualification.md)。
- `input/work/output` 只是路径边界，不代表当前支持文件能力。Landlock 当前不给 Worker
  任何直接写权限。

## Run 生命周期

```text
validate Agent
  → create unique Run root
  → create process session/group
  → atomically publish worker.pid
  → close FDs / rlimits / Landlock / seccomp
  → control-plane attestation
  → send user message and broker capabilities
  → terminal/cancel/deadline/failure
  → reap complete process group
  → revalidate root identity
  → remove entire Run root
```

一个 Run 从接受时起有 60 秒绝对 wall deadline。Control Plane 以每秒至多一次检查
Run 树，限制 1,024 entries、16 MiB logical bytes 和 32 MiB allocated bytes；链接、
特殊文件、跨 device 或超限都会终止 Worker。CPU、地址空间、单文件、FD 和进程数也
有 kernel rlimit。

Worker 使用新 session，Gateway 进程组不能作为“顺带清理”它的依据。`worker.pid`
记录 repository、Agent、Run root、PID、PGID、Linux start marker、interpreter、cwd、
module 和完整命令，并必须是当前 uid、`0600`、单 hardlink、固定大小/shape 的普通文件；
停止/删除前重新比对 `/proc`。Gateway record 还绑定 supervisor → Web child，live Worker
只有在 `PPid` 为该已验证 Web、marker/PGID/NNP/seccomp 和 record 同时匹配时才取得信号
权限。记录不安全且指向活进程时 fail closed，不按缓存 PGID、exact-argv 伪装或端口误杀；
停止顺序先 Worker 后 Gateway。

## 启动恢复与孤儿处理

初始化时对固定 Agent 的 `runs/` 做有界扫描：

- 有完整记录且匹配的 Worker 仍活着：拒绝继续，要求 lifecycle 先安全处理；
- leader 已消失但 group 仍有成员：拒绝删除；
- 无进程引用、身份和路径均安全：删除整个 stale Run root；
- 数量超限、链接、非法 ID/owner/type：fail closed。

`stop.sh` 使用相同强身份原则处理所有 Agent runtime entries。扫描本身有界；端口监听
状态只用于启动/停止诊断，不授予 kill 权限。

## 数据持久性

Run root 是 disposable；terminal 后删除。以下内容保留到 Agent 删除：

- `manifest.json`；
- `workspace/` 与 `artifacts/`（当前为空/保留）；
- `state.sqlite` durable events；
- `worker-env`（可重建，但普通 stop 不删除）。

`purge.sh environments --yes` 可删除可重建环境；`purge.sh data --yes` 是破坏性操作，
会删除不可重建数据。普通 `stop.sh` 不删除 Agent 持久状态或环境。

## 当前隔离保证

当前固定 Worker 路径提供：

- Agent 独立的数据/运行/环境目录；
- Run 独立进程组、HOME/TMP/XDG 和临时根；
- Landlock filesystem/TCP/signal/abstract socket scope；
- seccomp 禁止网络创建、process/exec、mount/namespace、持久 IPC 等；
- `no_new_privs`、non-dumpable、parent-death signal 和硬资源上限；
- 受信、版本化、有界的模型 IPC；
- terminal 后 Run root 清理与下一启动 orphan recovery。

这些保证不覆盖未来任意 Skill/Shell/MCP。新增 capability 应由受信 broker 在最小路径
上执行，或使用更细的子沙箱；不能扩大当前 Worker 直接权限。

## 通用 Agent 删除：尚未实现的契约

P8 要求最终删除操作是幂等、可中断恢复且无残留：

1. 阻止新 Run 并标记 Agent deleting；
2. 取消所有 Run，按强身份终止并回收全部进程组；
3. 证明没有 cwd/open executable/记录再引用 Agent roots；
4. 删除 runtime runs、logs、worker-env 和其它 environment/cache；
5. 事务性删除/注销持久 registry 后删除 `data/agents/<id>`；
6. 扫描 PID records、process groups、filesystem、registry/queue 无 Agent ID；
7. 验证其它 Agent 的目录、进程、journal 和环境未变化；
8. 任一步中断后重试能安全收敛，不留下“半删除但可运行”状态。

在这套状态机、权限模型、失败注入和残留测试完成前，产品只能运行固定 demo Agent，
不得暴露通用 delete API 或声称 P8 已完全实现。

---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-21
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
├── workspace/                          optional CLAUDE.md / Capsule-owned Git tree; Worker cannot read or write it
├── artifacts/                          reserved; current Worker cannot write it
├── skills/<skill-id>/                  current trusted manifest + main.py package
└── dependencies/research-documents/    curated source + immutable install metadata

.runtime/agents/<agent-id>/              disposable/reproducible runtime
├── worker-env/                          generation 1 Agent venv, created without pip
├── generations/<n>/worker-env/          generation n>1 staged/promoted venv
├── logs/                                reserved Agent-scoped logs
├── skills/<skill-id>/                   dedicated copied venv, no pip/dependencies
├── dependencies/research-documents/     Agent-private persistent binary-wheel venv
├── tasks/<task-id>/                     one fixed background Task; removed before terminal
│   ├── home/ tmp/ xdg/{cache,config,data}/
│   └── work/ output/
└── runs/<run-id>/                       one Run, removed after process exit
    ├── worker.pid                       atomic private process identity record
    ├── home/
    ├── tmp/
    ├── xdg/{cache,config,data,state}/
    ├── input/                           explicit future staging boundary
    ├── work/                            Worker cwd
    └── output/                          future broker pickup boundary
```

`data/agent-registry.sqlite` 是最多 100 个 Agent 的 registry authority，状态机为
`provisioning → active → renaming|upgrading|deleting`；系统 Agent ID
`00000000-0000-4000-8000-000000000001` 是不可重命名、不可升级、不可删除的正式默认入口。manifest schema 2 固化
Agent ID、显示名和当前 generation。Gateway 的 `AgentRuntimeManager` 按 active generation
惰性创建独立 RunService/QueryEngineRegistry/CommandBus，并让所有 Agent 共享同一个有界
Ollama Broker；共享 Broker 不共享 Conversation、ContextPlan、Run、Worker 或 SQLite。

## 路径与环境约束

- Agent/Run ID 必须匹配受限 UUID-like 字符集，任何受管绝对路径必须可相对到 repository
  root，且每个 component 都是当前用户拥有的真实目录。
- symlink、特殊文件、错误 owner、异常 hardlink、mount/device 边界变化一律拒绝。
- `worker-env` 位于 Agent runtime root，当前由 checkout interpreter 创建
  `venv --without-pip`；每个 Agent 路径独立，不在 Agent 间共享可写环境。
- 固定 `research-documents` 环境由受信控制面按 Agent 显式安装，精确版本、binary-only、
  无 build hook；普通 stop 和 Conversation 结束不删除，所以同一 Agent 跨会话复用。
  其它 Agent 的 Landlock domain 不包含该路径；请求、模型和 Worker 都不能指定依赖。
- Worker 环境只包含最小 PATH 和 checkout-local HOME/TMP/XDG/PYTHONPATH；不继承
  secrets、Conda、用户 site-package 或模型地址。唯一例外是显式
  `start.sh --qualification-sync-count` RR：Control Plane 只透传固定、经 checkout 路径与
  digest 校验的 qualification preload/counter/role，正常运行不存在该 capability，详情见
  [qualification contract](qualification.md)。
- `input/work/output` 只是路径边界，不代表当前支持文件能力。Landlock 当前不给 Worker
  任何直接写权限。
- Run admission 只从该 Agent 精确的 `workspace/CLAUDE.md` 与 workspace 自有 Git
  repository 生成不可变 prompt-source snapshot；Control Plane 不向上遍历或跨 Agent
  读取，Git helper 也被只读 Landlock 限制在当前 workspace。

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

一个 Run 从接受时起有 240 秒绝对 wall deadline。Control Plane 以每秒至多一次检查
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

每个 Agent generation 激活时对自己的 `runs/` 做有界扫描：

- 有完整记录且匹配的 Worker 仍活着：拒绝继续，要求 lifecycle 先安全处理；
- leader 已消失但 group 仍有成员：拒绝删除；
- 无进程引用、身份和路径均安全：删除整个 stale Run root；
- 数量超限、链接、非法 ID/owner/type：fail closed。

同一有界恢复也适用于最多 128 个 `tasks/` 根。TaskStore 先把遗留 queued/running 记录标为
`interrupted`，不重新 dispatch；随后只有在 `/proc` 中没有 cwd/exe 引用、路径/owner/type
均安全时才删除 Task 根。固定 runner 以 parent-death signal、PIDFD 和 seccomp NPROC=1/
fork+exec denial 保证没有可脱离后代。generation promotion 要求 runs 与 tasks 同时为空；
Agent delete 会分别完成 Run/Task orphan scan，再做整个 Capsule residual audit。

子智能体不共享父 Capsule。parent Capsule 只持有绑定 parent Run 的 Task/link/mailbox；child
Conversation/Run、Worker 根和环境全部位于 child Agent 自己的 data/runtime Capsule。父会话
删除先取消 child Run，再通过 child RunService 删除该 child Conversation；child Agent 删除
仍按本节规则删除整个 generation。重启只把未完成 link/Task 标为 `interrupted`，不会把旧
child Worker 迁入父 Capsule或重新 dispatch。

`stop.sh` 使用相同强身份原则处理所有 Agent runtime entries。扫描本身有界；端口监听
状态只用于启动/停止诊断，不授予 kill 权限。

## 数据持久性

Run root 是 disposable；terminal 后删除。以下内容保留到 Agent 删除：

- `manifest.json`；
- `workspace/`（可含 CLAUDE.md/Git project data）与 `artifacts/`；
- `state.sqlite` durable events、Task state/notifications 与 subagent link/mailbox；
- `skills/` version registry package metadata；Skill package/env 仅在显式删除或 Agent delete 清理；
- `dependencies/research-documents/` 固定 bundle identity 与可重建 venv；显式删除环境或
  Agent delete 清理，Conversation/Run terminal 不清理；
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

## create、rename、upgrade 与 delete

- create 先持久化 `provisioning`，再建立私有 data/runtime/workspace/artifacts、manifest 与
  无 pip Agent venv，最后提交 `active`；启动恢复会继续任何未完成 provisioning。
- rename 先持久化目标显示名与 `renaming`，再以 fsync + atomic replace 更新同一 generation
  的 manifest，最后提交 `active`。它不设置 runtime admission fence，不重建或删除解释器，
  也不触碰 Conversation、workspace、Skill 和依赖环境；任一步重启都按 registry 目标名幂等
  收敛。名称未变化时不产生持久写入。
- upgrade 先由 RuntimeManager 设置 admission fence，取消/排空该 Agent 的 Engines/Runs，
  在 `generations/<n>/` staging 新环境并资格检查，随后用 fsync + atomic manifest replace
  promotion；证明旧 generation 无进程引用并删除其环境后，才提交 registry active。任一步重启都从
  `upgrading + target_generation` 和 manifest 当前值幂等收敛，旧 generation 不再可激活。
- delete 同样先 fence/drain，再持久化 `deleting`。Capsule Manager 有界扫描 `/proc`，拒绝
  cwd/executable 或 PID record 仍引用该 Capsule；证明无引用后删除完整 data/runtime tree，
  验证两者均不存在，再删除 registry row。恢复看到 `deleting` 会继续该收敛过程；预置
  symlink、特殊文件、错误 owner/mode、跨 device 或无法证明的进程引用一律 fail closed。

通用 API 为 `/api/agents` create/list/get、`PATCH /api/agents/<id>` rename、
`/api/agents/<id>/upgrade|DELETE`，以及 Agent-scoped
session/Run/events/context/cancel。所有浏览器身份目前同属 single operator；Agent 目录隔离
不是多租户授权边界。系统 Agent 为保持控制面稳定而拒绝 rename/upgrade/delete；普通 Agent
可在 Web 管理区创建、切换、重命名和删除，generation upgrade 收进带说明及二次确认的高级
操作。切换后所有 Command/Conversation/Run 请求都显式绑定所选 Agent。首发仅支持
已完成 bounded lifecycle/SSD 资格的 GNU/Linux x86_64；`aarch64`、长期 soak、物理 SMART、
多租户和互联网部署仍不在 [release contract](release.md) 的支持范围内。

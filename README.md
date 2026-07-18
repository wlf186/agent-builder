# Agent Builder

Agent Builder 是一个从零构建的 Claude Code 风格智能体运行时。目前交付的是一条
可实际体验的 greenfield walking skeleton：认证 Web UI、类型化命令、规范事件流、
每 Run 独立强沙箱 Worker、受信模型代理和真实 Ollama 模型调用已经贯通。

它**不是生产就绪产品**。当前只有固定 demo Agent、固定模型和 `builtin/echo`；没有
通用 Agent 管理、Shell、文件工具、Skill、MCP、RAG、子智能体或 TLS。原型适合在
受信、防火墙保护的开发网络中验证架构，不应直接暴露到互联网。

## 当前链路

```text
Browser :20815
    │ HTTP + SSE
    ▼
Authenticated Web Gateway / trusted Control Plane
    ├── CommandBus → RunService → canonical sequencer → Agent SQLite journal
    ├── trusted Ollama Broker ── TCP ──> iollama:11434 / qwen3.5:2b
    └── bounded stdio IPC
             ▼
        one Worker per Run
        Landlock + seccomp + rlimits
             │
             ▼
        HarnessKernel
        context → model → builtin/echo → model → terminal event
```

Web 页面会增量显示回答、工具阶段和事件时间线。点击任一事件可查看未经界面截断的
canonical event envelope；消息通过纯文本渲染，不会作为 HTML 执行。

详细设计见 [architecture](docs/design/architecture.md)、
[event protocol](docs/design/event-protocol.md) 和
[Agent Capsule](docs/design/agent-capsule.md)。

## 环境要求

当前资格检查支持：

- GNU/Linux，`x86_64` 或 `aarch64`；
- 可用的 `/proc`、Landlock ABI 6+ 和 seccomp；
- `bash`、`curl`、`flock`、`ps` 及基本 GNU 用户空间工具；
- 可以解析并连接 `iollama:11434`；
- Ollama 已安装具有 completion 与 Tool 能力的 `qwen3.5:2b`。

其它平台没有经过等价的 cold-checkout 验证，不能视为受支持。Worker 沙箱资格或
模型资格失败时启动会 fail closed，不会退回假模型或未隔离运行。

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
data/agents/<agent-id>/         manifest、workspace、artifacts、state.sqlite
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

- SQLite 使用 WAL，`journal_size_limit` 为 16 MiB，只持久化 durable 语义事件。
- `assistant.block.delta` 只在内存和 SSE 中流动，不逐 token 写盘。
- 每 Run 最多 512 个 live event、1 MiB live event bytes、256 KiB durable bytes。
- 最多 4 个 active Run、内存保留 64 个近期 Run、journal 保留 256 个近期 Run。
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

真实端到端验收还应执行 `./start.sh`、检查 `/health`、通过 Web 发起一次 Run，确认
模型为 `qwen3.5:2b`、事件到达唯一终态，再执行 `./stop.sh` 并检查无受管进程残留。

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

---
owner: repository-maintainers
status: maintained
last_reviewed: 2026-07-18
review_cycle: quarterly
---

# Project principles

P1-P8 是当前重建的不可妥协约束。它们既是设计决策顺序，也是可以被测试和审查的
验收条件。原型尚未完全满足的部分必须写成缺口，不能通过措辞假装完成。

## P1 — CLAUDE.md 持续高质量并受治理

`CLAUDE.md` 是编码智能体的简明事实源，包含当前能力、命令、边界、测试和 DoD。
[文档治理](DOCUMENTATION.md) 规定 accountable owner、变更触发、季度审查和机械门禁。

验收证据：行为变化与权威文档同改；metadata 与链接通过治理；过期说明被删除而不是
层叠；不存在第二份可漂移的全局规则。

## P2 — AGENTS.md 是 CLAUDE.md 的软链接

根 `AGENTS.md` 必须存在且为相对符号链接 `CLAUDE.md`。禁止复制内容或反向链接。

验收证据：`test -L AGENTS.md && [ "$(readlink AGENTS.md)" = CLAUDE.md ]`，并由
`./governance.sh` 持续检查。

## P3 — 一键完整启停

根 `./start.sh` 启动当前完整服务，`./stop.sh` 停止 Gateway 和全部已验证 Worker。
启动失败回滚；并发 lifecycle 串行；停止根据强身份记录操作，不按端口误杀。

验收证据：cold checkout → start → ready health → real Run → stop；正常和 `--force`
均无受管进程或 Run 临时目录残留，伪造/stale PID 负面测试通过。

## P4 — 完全部署在当前工作目录

解释器、依赖、源码、密钥、状态和进程记录都归当前 checkout。系统不要求全局包、
用户 profile 修改或其它目录中的项目安装。

验收证据：从干净 shell bootstrap/start 成功；路径审计只出现 checkout 内路径；移动
checkout 后可重新 bootstrap；代码不依赖旧归档。

## P5 — 环境、临时文件和数据不污染其它目录

HOME、TMP、XDG、Python/uv/cache 全部被 `env.sh` 重定向；可重建状态进入 `.tools/`
或 `.runtime/`，持久 Agent 数据只进入 `data/`。每个 Agent 的运行环境也在自身目录。

验收证据：测试捕获所有预期写入；环境变量审计通过；无 `/tmp`、user home、Conda、
global site-packages 或外部 cache 写入。

## P6 — 无重大错误、性能/安全缺陷和不合理 SSD 磨损

所有边界均 fail closed、容量有界、取消/timeout 可终止。token delta 不落盘，日志
轮转并批量 flush，SQLite 事务 append 而非重写全历史，目录扫描低频且有条目上限。

验收证据：完整/负面/故障注入测试；内存、事件、Run 树、日志、WAL 和进程均有硬
上限；新增写路径有 write-amplification 分析；不得以“生产级”替代长期 soak 证据。

## P7 — Web 前端监听 0.0.0.0:20815

用户通过单一 Web Gateway 使用系统。默认 host 是 `0.0.0.0`、port 是 `20815`，包含
登录、Run、取消、增量输出、事件时间线和完整 event envelope 详情。

验收证据：监听/health/UI/真实 SSE Run 浏览器测试；认证、CSRF、Host/Origin、body
limit、CSP 与纯文本渲染负面测试。当前无 TLS，因此只允许受信网络。

## P8 — 每个 Agent 独立且删除无残留

每个 Agent 拥有独立的 `data/agents/<agent-id>/`、
`.runtime/agents/<agent-id>/worker-env` 和 runs；每 Run 是独立进程组、目录和强制
Landlock/seccomp 沙箱。能力由受信 broker 提供，不共享可写环境。

当前 walking skeleton 已贯通通用 Capsule create/upgrade/delete、generation promotion、
按 Agent runtime drain 和残留检查；默认 prototype Agent 为 UI 稳定性不可删除。逐一
崩溃点、双架构和长期 soak/SSD 资格尚未关闭，因此仍不能声称 production-ready P8。

完整验收要求：停止并验证 Agent 的全部进程组，删除 data/runtime/environment/logs，
扫描 PID、cwd、打开文件和注册表无引用；其它 Agent 不受影响；中断后可幂等恢复。

## 决策顺序

当目标冲突时依次选择：

1. 不突破安全、秘密和 checkout containment；
2. 不破坏事件顺序、终态和清理所有权；
3. 保持系统简单、显式、可测试，避免引入框架型隐式状态；
4. 控制资源和写放大；
5. 再优化功能覆盖与开发便利性。

任何例外都必须在实施前更新本文件和 active plan，由相应 owner 审核，并给出撤销条件。

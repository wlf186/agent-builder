---
owner: repository-maintainers
status: maintained
last_reviewed: 2026-07-20
review_cycle: quarterly
---

# Documentation governance

文档是当前系统的一部分，不是重构完成后的补记。本规范通过单一事实源、变更触发、
责任人、定期审查和自动门禁，持续保持 `CLAUDE.md` 与设计/安全文档的质量。

## 权威文档地图

| 主题 | 权威文档 | accountable owner | 必须同步更新的触发器 |
| --- | --- | --- | --- |
| 编码规则、命令、全局边界、DoD | `CLAUDE.md` | repository maintainers | 任一全局规则、命令、目录或完成定义变化 |
| 用户启动、状态、限制、目录 | `README.md` | runtime maintainers | 生命周期、平台、端口、模型、路径或用户流程变化 |
| P1-P8 | `docs/PRINCIPLES.md` | repository maintainers | 原则、验收证据或优先级变化 |
| 文档流程与所有权 | 本文件 | repository maintainers | 文档集合、metadata、审查或治理检查变化 |
| 威胁模型与安全控制 | `SECURITY.md` | security maintainers | trust boundary、认证、模型、沙箱、秘密或资源限制变化 |
| 运行时拓扑和组件边界 | `docs/design/architecture.md` | runtime maintainers | 进程、模块、数据流或依赖方向变化 |
| canonical event / SSE | `docs/design/event-protocol.md` | runtime maintainers | 字段、事件、顺序、持久性、cursor 或限制变化 |
| Agent/Run 所有权与清理 | `docs/design/agent-capsule.md` | runtime maintainers | Capsule 目录、环境、生命周期、隔离或删除变化 |
| 发布、平台、备份与回滚 | `docs/design/release.md` | runtime maintainers | release scope、平台、资格、产物或运维流程变化 |
| 当前路线与缺口 | `docs/plans/runtime-rebuild.md` | plan owner | 工作开始、完成、换方向、阻塞或验收变化 |
| Claude Code 研究范围 | `references/claude-code/*.md` | research maintainers | 材料、版本、hash、来源或使用边界变化 |

一个事实只在一份权威文档中完整定义，其它位置用链接和简短摘要。若代码与文档冲突，
先把它当成缺陷；不能凭“代码才是真相”长期容忍漂移。

## Metadata

除 `CLAUDE.md` 和 `README.md` 外，所有维护中的 Markdown 文档必须以以下 YAML 开头：

```yaml
---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-18
review_cycle: quarterly
---
```

- `owner`：可问责的角色，不写临时智能体名称。
- `status`：`maintained`、`active` 或 `reference`。
- `last_reviewed`：UTC `YYYY-MM-DD`；表示逐项对照实现后的日期，不是格式化日期。
- `review_cycle`：当前统一为 `quarterly`。活动计划还要在方向变化时立即更新。

已完成计划应在记录最终验收后删除；历史由 Git 保存，不在主树堆积过期总结。

## 变更工作流

1. 开工前定位权威文档和受影响的 P1-P8；复杂工作先更新 active plan。
2. 实现期间记录最终行为、失败路径、限制、迁移/清理和安全假设。
3. 同一个变更中更新权威文档；删除已失效内容，不追加互相矛盾的“新说明”。
4. 检查所有本地链接、命令、端口、路径、模型、schema 和大小/时间上限。
5. 运行 `./governance.sh` 以及相关测试；代码审查把文档准确性作为阻断项。
6. 完成后更新 plan 状态和验收证据；未完成项必须保持明确，禁止扩大声明。

以下变化总是触发文档更新：

- CLI flags、启动/停止/清理行为、host/port、环境变量或 host requirements；
- API、认证、CSRF、event schema、SSE、持久性、存储或 retention；
- 模型 endpoint/model/options、Tool、context、Agent loop 或 capability；
- Agent/Run 路径、虚拟环境、沙箱、资源限制、清理和失败策略；
- 用户可见流程、生产就绪程度或已知限制。

## 定期审查

Repository maintainers 每季度、每次 release candidate 前以及重大安全变更后执行审查：

1. 从 `CLAUDE.md`、P1-P8 和 README 逐条映射到脚本/代码/测试；
2. 从代码中的端口、常量、route、event kind 和存储路径反向查权威文档；
3. 运行治理门禁、完整测试和 cold start/health/Run/stop 验收；
4. 检查所有 `last_reviewed`，只有完成实质核对后才更新日期；
5. 删除过期内容，通过 Git 历史保留可追溯性。

## 自动治理门禁

`./governance.sh` 至少应验证：

- `AGENTS.md` 是相对符号链接且准确指向 `CLAUDE.md`；
- P1-P8、DoD、根生命周期命令、`0.0.0.0:20815` 和当前数据路径存在且不漂移；
- 维护文档 metadata 完整、日期/枚举合法、review 未过期；
- Markdown 本地链接有效，主文档不引用不存在的旧路径；
- 当前源码没有导入 `_legacy-reference/`、LangGraph、LangChain 或旧系统；
- 运行时代码不依赖 `references/claude-code/materials/`；
- `.runtime/`、`.tools/`、`data/`、研究材料和旧归档按策略忽略；
- 依赖、秘密模式和 checkout 外路径策略符合当前边界。

治理脚本是最低机械检查，不能替代设计和安全评审。

## 研究资料与旧系统

`references/claude-code/` 是只读设计输入。必须保留来源、版本和 hash，不得把第三方
材料当作项目需求、运行依赖或安全证明。

`_legacy-reference/` 是隔离的旧系统快照，仅当用户明确要求历史调查时读取。当前运行
时、测试、脚本、文档和治理不得 import、执行、扫描或引用其内部文件。主文档只允许
像本段一样说明归档边界，不能链接到其具体实现。

## 文档完成检查

- 读者能从 README 完成启动、登录、Run 和停止；
- 编码智能体能从 CLAUDE 识别 P1-P8、边界、测试与 DoD；
- 安全声明区分“已实现控制”和“未完成/不支持”；
- 架构、事件和 Capsule 文档能精确解释当前代码，不画未来能力为现状；
- 所有数字、路径、模型、端口和命令只有一个权威定义且可验证；
- `./governance.sh` 通过。

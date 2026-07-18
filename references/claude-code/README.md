---
owner: research-maintainers
status: reference
last_reviewed: 2026-07-18
review_cycle: quarterly
---

# Claude Code research materials

本目录保存本次 greenfield runtime 设计使用的 Claude Code 学习资料。它们是**只读研究
输入**，不是本项目源码、依赖、测试 fixture、安全证明或产品规范。

## 推荐阅读顺序

1. `materials/2.1.88/rendered/architecture-overview-overall-diagram.png`：先看整体图；
2. `materials/2.1.88/articles/1 架构总览/Claude Code 源码架构总览 _ Claude Code源码学习 _ 学习 Claude Code.txt`；
3. 同目录的启动流程和“需要哪些模块”；
4. `articles/2 核心机制/` 中的核心引擎、context、Tool、command 和压缩文章；
5. 对具体设计问题，再查 `materials/2.1.88/src/` 中对应的 2.1.88 源码文件。

不要先从单个 Tool 实现反推总体架构。当前项目吸收的是清晰的入口、command system、
single agent loop、context/model/tool/state 分层和增量 UI 语义；Web control plane、
canonical journal、每 Run 强沙箱和 checkout containment 是本项目自己的约束。

## Material layout

```text
materials/                              ignored; local research corpus
├── original/
│   └── claude-code-source-code-deep-dive.zip
└── 2.1.88/
    ├── articles/                       45 extracted text articles
    ├── src/                            1,902 extracted source files
    ├── pdf-pages/                      selected architecture PDF/pages
    └── rendered/                       two convenient overview renders
```

`materials/` 故意不纳入 Git。缺少它不应影响 bootstrap、start、test、governance 或运行。
需要恢复时，从已授权的原始包重新放置并按 [PROVENANCE.md](PROVENANCE.md) 校验 hash；
不要让脚本在运行时自动下载第三方材料。

## Usage rules

- 先核对版本和来源；文章可能是二手解读，源码也只代表标注版本。
- 设计结论必须写入本项目 `docs/design/` 并结合自己的威胁模型和测试，不以“Claude
  Code 也这样做”替代论证。
- 不直接复制大段第三方源码、文档或图片到项目源码/公开文档；遵守适用许可与著作权。
- 不从材料执行脚本、安装包或依赖；源文件只读查看。
- 当前 runtime、test、lifecycle 和 governance 不得扫描或 import `materials/`。
- 用户明确要求对照研究时可以读取；普通实现任务使用本项目权威文档。

旧 Agent Builder 系统不在本目录，它位于独立 `_legacy-reference/`，适用更严格的
“只有用户明确要求历史调查才读取”规则。

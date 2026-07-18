---
owner: research-maintainers
status: reference
last_reviewed: 2026-07-18
review_cycle: quarterly
---

# Provenance and integrity record

## Original bundle

本地原始研究包：

```text
references/claude-code/materials/original/claude-code-source-code-deep-dive.zip
SHA-256: 201ac3ed2bd8e36046bb7d768bf16a0e4d4f1f7102d79f8b0000739ddec9a537
```

该包由用户作为本地参考资料提供。包内名称为 `claude-code source code deep dive`，
包含中文学习 PDF，以及 `0 source/anthropic-ai-claude-code-2.1.88.tgz` 和
`0 source/src.zip`。本仓库没有可验证的上游下载 URL、发布签名、作者身份或统一再分发
许可，因此不得推断这些信息，也不得把 hash 当作上游真实性证明。hash 只证明当前
本地包字节一致。

## Derived local corpus

`materials/2.1.88/` 是从上述本地包整理出的便于研究的 corpus，审查时包含：

- 45 个 `articles/` 文本文件；
- 1,902 个 `src/` 文件；
- 4 个 `pdf-pages/` 文件（架构总览 PDF 与页面图）；
- 2 个 `rendered/` 架构总览图。

这些 derived files 没有独立发布签名。本记录只对原始 zip 固定 hash；若重新提取工具、
字符编码、PDF 渲染器或选择范围改变，应更新计数、说明和 `last_reviewed`，并人工检查
“先看整体图”是否仍可读。

验证原始包：

```bash
sha256sum references/claude-code/materials/original/claude-code-source-code-deep-dive.zip
```

预期输出的第一列必须与上方 SHA-256 完全相同。不要在 hash 不匹配时自动覆盖现有
corpus；先隔离新包并由 research maintainer 调查来源。

## Version boundary

资料以 `2.1.88` 标记。它不是“最新 Claude Code”声明，也不表示所有文章严格对应同
一构建。设计引用应注明这是一次版本化快照，不能把内部文件名或行为当作稳定公共 API。

当前项目没有复制 Claude Code runtime，也不在构建或运行时加载这些材料。项目自己的
实现、测试、许可和安全责任保持独立。

## Retention and access

`materials/` 在 Git 中 ignored，只应保存在有权访问这些资料的本地 checkout。不要将
其打入 release artifact、容器、测试 fixture、日志或 Web static assets。若维护者无法
确认使用/再分发权限，应保留本 provenance 记录但不传播原始或 derived corpus。

---
title: "模型服务配置"
category: advanced
component: ModelServiceDialog
generated: true
related:
  - AgentChat
---
# 模型服务配置

为 AI 智能体配置大语言模型服务。支持智谱AI、阿里云百炼和本地 Ollama 等多种供应商。

## 使用方法

1. 点击设置图标或"添加模型服务"按钮
2. 输入模型服务的唯一名称
3. 选择供应商（智谱AI、阿里云百炼或 Ollama）
4. 输入服务地址（已知供应商会自动填充）
5. 输入 API Key（云服务商必填）
6. 点击"测试连接"验证连通性并获取可用模型
7. 从下拉列表中选择一个模型
8. 切换启用开关并点击"保存"

## 提示

- 通过密码输入框提交的 API Key 仅保存在服务端，后续不会以明文返回
- 保存前先测试连接以确保服务可访问
- 本地 Ollama 不需要 API Key

## 相关

- [AgentChat](/zh/core/agent-chat)

---
title: "MCP 服务配置"
category: advanced
component: MCPServiceDialog
generated: true
related:
  - MCPDiagnosticResult
---
# MCP 服务配置

配置 MCP（模型上下文协议）服务以扩展智能体的外部工具能力。支持 SSE 连接和可选认证。

## 使用方法

1. 点击"添加 MCP 服务"打开配置对话框
2. 输入唯一的服务名称（如：weather-api）
3. 输入 MCP 服务 URL 端点
4. 选择认证类型：无认证、Bearer Token 或 API Key
5. 如需认证，输入凭据信息
6. 可选添加 JSON 格式的自定义请求头
7. 切换启用开关并点击"保存"
8. 使用"诊断"按钮（编辑模式）检查连接健康状态

## 提示

- MCP 服务使用 SSE（服务器推送事件）进行实时通信
- 使用诊断工具排查连接问题
- 自定义请求头必须是有效的 JSON 格式

## 相关

- [MCPDiagnosticResult](/zh/advanced/mcp-diagnostic-result)

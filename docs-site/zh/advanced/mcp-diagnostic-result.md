---
title: "MCP 诊断"
category: advanced
component: MCPDiagnosticResult
generated: true
related:
  - MCPServiceDialog
---
# MCP 诊断

通过多层健康检查诊断 MCP 服务连接问题。查看配置、DNS、网络、TLS 和 MCP 协议层的详细结果。

## 使用方法

1. 在编辑模式下打开 MCP 服务配置
2. 点击"诊断"按钮开始诊断
3. 查看整体状态：正常、降级或不可用
4. 检查每层结果获取详细信息
5. 展开"详细信息"查看原始诊断数据
6. 参考修复建议部分获取解决方案

## 提示

- 每层测试特定方面：配置验证、DNS解析、网络连接、TLS/SSL 和 MCP 协议
- 每层延迟以毫秒为单位测量
- 使用修复建议识别和解决常见问题

## 相关

- [MCPServiceDialog](/zh/advanced/mcp-service-dialog)

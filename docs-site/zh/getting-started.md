---
title: 快速开始
---

# 快速开始

欢迎使用 Agent Builder！本指南将帮助您在5分钟内创建第一个AI智能体。

## 什么是 Agent Builder？

Agent Builder 是一个创建AI助手（智能体）的平台，它可以：

- 用自然语言与您对话
- 使用工具执行任务
- 搜索您的文档
- 与其他智能体协作

## 快速入门

运维人员可在仓库根目录一键启动整套项目内服务：

```bash
./start.sh
```

无需全局 Python、Node、Conda 或容器运行时。启动脚本确认全部服务健康后，
访问 `http://127.0.0.1:20815`。使用 `./stop.sh` 停止本工作目录启动的全部
进程。

全新安装需要受支持的 glibc Linux 主机、网络连接以及足够的磁盘和内存。
首次初始化前请阅读仓库中的
[部署前提](https://github.com/wlf186/agent-builder#supported-deployment)。

### 第零步：配置模型服务

Agent Builder 不自带大语言模型，也不会安装 Ollama。请打开 **模型服务配置**，
添加带 API Key 的云模型服务，或连接到单独安装的本地 Ollama。详细步骤参见
[模型服务指南](/zh/advanced/model-service-dialog)。

### 第一步：创建智能体

1. 点击左侧边栏的 **"创建智能体"** 按钮
2. 输入智能体名称（如"我的助手"）
3. 在 **人设** 字段中描述智能体的功能
4. 点击 **保存** 创建智能体

### 第二步：开始对话

1. 在侧边栏中点击刚创建的智能体
2. 在底部聊天输入框中输入消息
3. 按 **回车键** 或点击 **发送**
4. 观看AI的实时流式响应

### 第三步：添加知识库（可选）

1. 点击智能体设置中的 **知识库** 图标
2. 创建新知识库或选择现有知识库
3. 上传文档（PDF、DOCX、TXT、MD）
4. 您的智能体现在可以搜索这些文档来回答问题

## 下一步

- 了解 [聊天界面](/zh/core/agent-chat) 功能
- 探索 [知识库](/zh/core/knowledge-base-selector)
- 对话前配置 [模型服务](/zh/advanced/model-service-dialog)
- 配置 [MCP服务](/zh/advanced/mcp-service-dialog) 以使用工具

## 需要帮助？

如果遇到问题，请检查智能体状态指示器或联系管理员。

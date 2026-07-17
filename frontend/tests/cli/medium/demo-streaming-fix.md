# 流式输出修复验证

> 来源: `frontend/tests/demo-streaming-fix.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- test3 智能体可用

> 注意: 调试对话输入框使用 `<input type="text">`（不是 textarea），人设与提示词编辑使用 `<textarea>`。

---

### 测试用例 1: TC-001 - test3 agent 基本响应验证

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 截图保存到 test-results/cli/streaming-01-homepage.png
4. 等待并找到包含 "test3" 的 h3 元素（最多等待 15 秒），点击它
5. 等待 3 秒让智能体详情页加载
6. 截图保存到 test-results/cli/streaming-02-agent-selected.png
7. 等待 2 秒让 AgentChat 组件加载完成
8. 找到聊天输入框（`input[type="text"][placeholder]`），输入 "你好"
9. 截图保存到 test-results/cli/streaming-03-message-input.png
10. 按 Enter 发送消息
11. 等待 5 秒观察流式输出开始
12. 截图保存到 test-results/cli/streaming-04-streaming.png
13. 继续等待 15 秒让响应完成
14. 截图保存到 test-results/cli/streaming-05-final-result.png

## 验证
- 页面包含以下关键词之一: "你好"、"高兴"、"助手"、"帮助"、"Hello"
- 这表明 AI 返回了非空响应，流式输出正常工作

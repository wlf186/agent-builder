# UAT: 流式输出修复验证

> 来源: `frontend/tests/uat-streaming-fix.spec.ts`
> 复杂度: complex
> 迭代: AC130-202603151517
> 修复内容: ChatRequest 添加 conversation_id 字段, AgentConfig 属性访问错误修复

## 前置条件
- 服务已启动 (localhost:20815)
- 存在名为 "test3" 的智能体

---

### 测试用例 TC-001: test3 agent 基本响应验证

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/uat-streaming-fix-01-homepage.png`
3. 在 snapshot 中找到 "test3" 智能体卡片（h3 标签），点击选择
4. 等待 3 秒
5. 截图保存到 `test-results/cli/uat-streaming-fix-02-agent-selected.png`
6. 在 snapshot 中找到输入框，输入 "你好"
7. 截图保存到 `test-results/cli/uat-streaming-fix-03-message-input.png`
8. 按 Enter 发送消息
9. 等待 5 秒（流式输出开始）
10. 截图保存到 `test-results/cli/uat-streaming-fix-04-response-streaming.png`
11. 继续等待 15 秒（响应完成）
12. 截图保存到 `test-results/cli/uat-streaming-fix-05-final-result.png`

## 验证
- 页面应包含非空响应内容，包含以下关键词之一: "你好"、"高兴"、"助手"、"帮助"、"Hello"、"help"

---

### 测试用例 TC-002: 流式输出打字机效果验证

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 在 snapshot 中找到 "test3" 智能体卡片（h3 标签），点击选择
3. 等待 2 秒
4. 在 snapshot 中找到输入框，输入 "1+1等于几？"
5. 按 Enter 发送消息
6. 每 2 秒截图一次验证打字机效果，共 3 次，分别保存为 `test-results/cli/uat-streaming-fix-streaming-1.png` 到 `test-results/cli/uat-streaming-fix-streaming-3.png`
7. 等待 10 秒（响应完成）
8. 截图保存到 `test-results/cli/uat-streaming-fix-streaming-final.png`

## 验证
- 页面应包含计算结果，包含以下关键词之一: "2"、"二"、"等于"
- 连续截图应显示内容逐步增加（打字机效果）

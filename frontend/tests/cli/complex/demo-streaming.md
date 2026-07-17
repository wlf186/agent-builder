# 流式对话功能演示

> 来源: `frontend/tests/demo-streaming.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (localhost:20815)

---

### 测试用例 1: 演示流式对话完整流程

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/demo-streaming-01-homepage.png`
3. 等待 2 秒
4. 在 snapshot 中找到 "test3" 智能体卡片，点击选择。如果找不到 test3，选择第一个可用智能体
5. 截图保存到 `test-results/cli/demo-streaming-02-agent-selected.png`
6. 等待 2 秒
7. 在 snapshot 中找到聊天输入框（`input[type="text"]`），输入 "你好"
8. 截图保存到 `test-results/cli/demo-streaming-03-message-entered.png`
9. 等待 1.5 秒
10. 在输入框中按 Enter 发送消息
11. 截图保存到 `test-results/cli/demo-streaming-04-message-sent.png`
12. 等待 2 秒，截图保存到 `test-results/cli/demo-streaming-05-streaming-start.png`
13. 等待流式输出，每 2 秒截图一次，共截图 5 次，分别保存为 `test-results/cli/demo-streaming-06-streaming-1.png` 到 `test-results/cli/demo-streaming-06-streaming-5.png`
14. 截图保存到 `test-results/cli/demo-streaming-07-response-complete.png`
15. 等待 2 秒，截图保存到 `test-results/cli/demo-streaming-08-final-state.png`

## 验证
- 页面应包含有效响应内容（包含 "你好"、"我是"、"帮助" 等关键词，或页面内容长度 > 2000 字符）
- 流式输出过程中应可见打字机效果

---

### 测试用例 2: 验证无控制台错误

## 步骤
1. 在后续操作中注意观察控制台错误（`page.on('console')` 监听 error 类型消息）
2. 访问 `http://localhost:20815`，等待 h3/h2 或智能体卡片元素出现
3. 在 snapshot 中找到第一个智能体卡片（h3 标签），点击选择
4. 等待 1 秒
5. 在 snapshot 中找到聊天输入框（`input[type="text"]`），输入 "演示测试"
6. 按 Enter 发送消息
7. 等待 8 秒
8. 截图保存到 `test-results/cli/demo-streaming-09-no-errors.png`

## 验证
- 控制台不应出现关键错误（包含 "parameter"、"TypeError"、"missing" 等关键词的错误）
- 普通警告可以忽略

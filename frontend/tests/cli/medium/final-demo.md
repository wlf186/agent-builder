# 最终演示 - 完整用户旅程

> 来源: `frontend/tests/final-demo.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- 使用 headed 模式运行（非无头浏览器）

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 截图保存到 test-results/cli/final-demo-01-homepage.png
4. 等待 3 秒

### 选择智能体
5. 找到包含 "test3" 的标题（h3 或 h2，或 data-agent-card 元素），点击它
6. 如果找不到 test3，使用页面上的第一个标题（h3）
7. 截图保存到 test-results/cli/final-demo-02-agent-selected.png
8. 等待 3 秒

### 定位聊天输入框
9. 找到聊天输入框（`input[type="text"][placeholder]`）
10. 截图保存到 test-results/cli/final-demo-03-input-located.png
11. 等待 2 秒

### 输入并发送消息
12. 点击输入框，等待 0.5 秒
13. 在输入框中输入 "你好"
14. 截图保存到 test-results/cli/final-demo-04-message-entered.png
15. 等待 3 秒
16. 按 Enter 发送消息
17. 截图保存到 test-results/cli/final-demo-05-message-sent.png
18. 等待 2 秒

### 展示流式响应（打字机效果）
19. 截图保存到 test-results/cli/final-demo-06-streaming-start.png
20. 每 2 秒截图一次，共 6 次，展示流式输出过程:
    - 截图保存到 test-results/cli/final-demo-07-streaming-1.png
    - 截图保存到 test-results/cli/final-demo-07-streaming-2.png
    - 截图保存到 test-results/cli/final-demo-07-streaming-3.png
    - 截图保存到 test-results/cli/final-demo-07-streaming-4.png
    - 截图保存到 test-results/cli/final-demo-07-streaming-5.png
    - 截图保存到 test-results/cli/final-demo-07-streaming-6.png
21. 截图保存到 test-results/cli/final-demo-08-response-complete.png
22. 等待 3 秒
23. 截图保存到 test-results/cli/final-demo-09-final-state.png

## 验证
- 页面包含以下关键词之一: "你好"、"我是"、"帮助"、"您好"，或页面内容长度超过 3000 字符
- 流式输出（打字机效果）可见

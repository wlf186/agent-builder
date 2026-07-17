# CoinGecko 工具调用演示（暂停模式）

> 来源: `frontend/tests/coingecko-demo-pause.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- test3 智能体已配置 CoinGecko MCP 工具
- 使用 headed 模式运行（非无头浏览器）
- 演示完成后浏览器会保持打开状态

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 截图保存到 test-results/cli/coingecko-pause-01-homepage.png
4. 等待 3 秒

### 选择智能体
5. 找到包含 "test3" 的标题（h3 或 h2，或 data-agent-card 元素），点击它
6. 如果找不到 test3，使用页面上的第一个标题（h3）
7. 截图保存到 test-results/cli/coingecko-pause-02-agent-selected.png
8. 等待 3 秒

### 定位聊天输入框
9. 找到聊天输入框（`input[type="text"][placeholder]`）
10. 截图保存到 test-results/cli/coingecko-pause-03-input-located.png
11. 等待 2 秒

### 输入并发送消息
12. 点击输入框，等待 0.5 秒
13. 在输入框中输入 "btc的最新价格"
14. 截图保存到 test-results/cli/coingecko-pause-04-message-entered.png
15. 等待 3 秒
16. 按 Enter 发送消息
17. 截图保存到 test-results/cli/coingecko-pause-05-message-sent.png
18. 等待 2 秒

### AI 思考过程（thinking）
19. 截图保存到 test-results/cli/coingecko-pause-06-thinking.png
20. 等待 3 秒

### 工具调用（tool_call）
21. 截图保存到 test-results/cli/coingecko-pause-07-tool-calling.png
22. 等待 4 秒

### 工具结果返回（tool_result）
23. 截图保存到 test-results/cli/coingecko-pause-08-tool-result.png
24. 等待 4 秒

### AI 回复完成
25. 等待 5 秒
26. 截图保存到 test-results/cli/coingecko-pause-09-response-complete.png
27. 截图保存到 test-results/cli/coingecko-pause-10-final-state.png

## 验证
- 页面包含以下关键词之一: "价格"、"price"、"USD"、"$"、"BTC"、"bitcoin"
- 这表明 CoinGecko 工具被成功调用并返回了 BTC 价格信息
- 演示完成后浏览器保持打开状态，可手动查看

# CoinGecko 工具调用演示

> 来源: `frontend/tests/coingecko-demo.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- test3 智能体已配置 CoinGecko MCP 工具
- 使用 headed 模式运行（非无头浏览器）

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 滚动到页面顶部触发浏览器重绘（修复 X11 远程投屏渲染问题）
4. 等待 0.1 秒
5. 截图保存到 test-results/cli/coingecko-01-homepage.png
6. 等待 3 秒

### 选择智能体
7. 找到包含 "test3" 的标题（h3 或 h2，或 data-agent-card 元素），点击它
8. 如果找不到 test3，使用页面上的第一个标题（h3）
9. 截图保存到 test-results/cli/coingecko-02-agent-selected.png
10. 等待 3 秒

### 定位聊天输入框
11. 找到聊天输入框（`input[type="text"][placeholder]`）
12. 截图保存到 test-results/cli/coingecko-03-input-located.png
13. 等待 2 秒

### 输入并发送消息
14. 点击输入框，等待 0.5 秒
15. 在输入框中输入 "btc的最新价格"
16. 截图保存到 test-results/cli/coingecko-04-message-entered.png
17. 等待 3 秒
18. 按 Enter 发送消息
19. 截图保存到 test-results/cli/coingecko-05-message-sent.png
20. 等待 2 秒

### 展示 AI 思考过程
21. 截图保存到 test-results/cli/coingecko-06-thinking.png
22. 等待 3 秒

### 展示工具调用
23. 截图保存到 test-results/cli/coingecko-07-tool-calling.png
24. 等待 4 秒

### 展示结果返回
25. 截图保存到 test-results/cli/coingecko-08-result-coming.png
26. 等待 4 秒

### 最终结果
27. 截图保存到 test-results/cli/coingecko-09-final-result.png
28. 等待 3 秒
29. 截图保存到 test-results/cli/coingecko-10-final-state.png

## 验证
- 页面包含以下关键词之一: "价格"、"price"、"USD"、"$"、"BTC"
- 这表明 CoinGecko 工具被成功调用并返回了 BTC 价格信息

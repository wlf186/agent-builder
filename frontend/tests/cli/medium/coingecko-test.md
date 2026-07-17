# CoinGecko 工具验证测试

> 来源: `frontend/tests/coingecko-test.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- test3 智能体已配置 CoinGecko MCP 工具

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 截图保存到 test-results/cli/coingecko-test-01-homepage.png

### 选择智能体
4. 找到包含 "test3" 的标题（h3 或 h2，或 data-agent-card 元素），点击它
5. 如果找不到 test3，使用页面上的第一个标题（h3）
6. 等待 2 秒
7. 截图保存到 test-results/cli/coingecko-test-02-agent-selected.png

### 定位聊天输入框
8. 找到聊天输入框（`input[type="text"][placeholder]`），确认可见
9. 截图保存到 test-results/cli/coingecko-test-03-input-located.png

### 输入并发送消息
10. 点击输入框，等待 0.5 秒
11. 在输入框中输入 "btc的最新价格"
12. 截图保存到 test-results/cli/coingecko-test-04-message-entered.png
13. 等待 1 秒
14. 按 Enter 发送消息
15. 截图保存到 test-results/cli/coingecko-test-05-message-sent.png

### 等待工具调用和响应
16. 等待 3 秒，截图保存到 test-results/cli/coingecko-test-06-tool-calling.png
17. 等待 5 秒，截图保存到 test-results/cli/coingecko-test-07-response-coming.png
18. 等待 5 秒，截图保存到 test-results/cli/coingecko-test-08-response-content.png
19. 截图保存到 test-results/cli/coingecko-test-09-final-state.png

## 验证
- 页面不包含以下错误信息: "处理请求时发生错误"、"请求失败"、"Request failed"
- 页面包含以下价格相关关键词之一: "价格"、"price"、"USD"、"$"、"BTC"、"bitcoin"
- 这表明 CoinGecko 工具被成功调用并返回了价格信息
- 如果未检测到价格信息，需人工检查截图确认结果

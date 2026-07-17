# 3 轮对话演示（暂停模式）

> 来源: `frontend/tests/demo-3-rounds.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- test3 智能体已配置 CoinGecko、cold-jokes、Calculator MCP 工具
- 使用 headed 模式运行（非无头浏览器）

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 滚动到页面顶部触发浏览器重绘（修复 X11 远程投屏渲染问题）
4. 等待 0.1 秒
5. 找到包含 "test3" 的 h3 标题，点击选择 test3 智能体
6. 等待 2 秒
7. 截图保存到 test-results/cli/demo-3-rounds-00-agent-selected.png
8. 找到聊天输入框（`input[type="text"][placeholder]`）

### 第 1 轮: ETH 价格查询（CoinGecko 工具）
9. 在输入框中输入 "ETH的最新价格"
10. 等待 0.5 秒
11. 按 Enter 发送
12. 等待 15 秒（CoinGecko API 可能需要较长时间）
13. 截图保存到 test-results/cli/demo-3-rounds-01-eth-price.png

### 第 2 轮: 冷笑话（cold-jokes 工具）
14. 等待 3 秒让输入框恢复
15. 在输入框中输入 "讲2个冷笑话"
16. 等待 0.5 秒
17. 按 Enter 发送
18. 等待 15 秒
19. 截图保存到 test-results/cli/demo-3-rounds-02-cold-jokes.png

### 第 3 轮: 计算器（Calculator 工具）
20. 在输入框中输入 "32748+392/2+1是多少"
21. 等待 0.5 秒
22. 按 Enter 发送
23. 等待 8 秒
24. 截图保存到 test-results/cli/demo-3-rounds-03-calculator.png

### 最终状态
25. 截图保存到 test-results/cli/demo-3-rounds-04-final-state.png

## 验证
- 第 1 轮: AI 调用 CoinGecko 工具返回 ETH 价格信息
- 第 2 轮: AI 调用 cold-jokes 工具返回笑话内容
- 第 3 轮: AI 调用 Calculator 工具返回计算结果（32945）

# Cold Jokes MCP 工具修复验证

> 来源: `frontend/tests/cold-jokes-uat.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- test3 智能体已配置 cold-jokes MCP 工具

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 截图保存到 test-results/cli/cold-jokes-01-homepage.png
4. 找到包含 "test3" 的 h3 标题，点击选择 test3 智能体
5. 等待 2 秒
6. 截图保存到 test-results/cli/cold-jokes-02-agent-selected.png
7. 找到聊天输入框（`input[type="text"][placeholder]`）
8. 截图保存到 test-results/cli/cold-jokes-03-before-input.png
9. 在输入框中输入 "讲3个冷笑话"
10. 等待 0.5 秒
11. 截图保存到 test-results/cli/cold-jokes-04-message-filled.png
12. 按 Enter 发送消息
13. 等待 10 秒让工具调用完成
14. 截图保存到 test-results/cli/cold-jokes-05-after-response.png
15. 截图保存到 test-results/cli/cold-jokes-06-final-state.png

## 验证
- 页面包含以下关键词之一：笑话、冷笑话、幽默、有趣、为什么、因为、问、答、哈哈、笑、搞笑
- 这表明 cold-jokes MCP 工具被成功调用并返回了笑话内容

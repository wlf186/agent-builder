# 冷笑话 MCP Tool 测试

> 来源: `frontend/tests/cold-jokes-test.spec.ts`
> 复杂度: simple

## 前置条件
- 服务已启动 (localhost:20815)

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. snapshot 找到 "test3" 标题，点击它
4. 等待 2 秒
5. snapshot 找到聊天输入框（input[type="text"][placeholder]），输入 "讲3个冷笑话"
6. 按 Enter 发送
7. 等待 15 秒让助手回复
8. snapshot 查看回复内容
9. 截图保存到 test-results/cli/cold-jokes-test.png

## 验证
- 回复中应包含笑话相关内容（如"冷笑话"、"数学书"、"月亮"、"小明"等关键词）

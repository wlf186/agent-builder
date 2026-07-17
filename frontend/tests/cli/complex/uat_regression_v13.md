# UAT 回归测试 - v1.3 Bug Fixes

> 来源: `frontend/tests/uat_regression_v13.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (前端 localhost:20815, 后端 localhost:20881)
- test001 智能体已配置

---

### TC-005: PDF Skill 执行状态显示 (无重复状态条)

## 步骤

1. 导航到 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/uat-regression-v13/01-homepage.png`
3. 在 snapshot 中找到 "调试对话" 按钮，点击
4. 等待 1 秒
5. 截图保存到 `test-results/cli/uat-regression-v13/02-chat-page-before.png`
6. 找到消息输入框 (`textarea[placeholder*="输入消息"]`)，输入“请使用 pdf 技能读取仓库根目录 README.md 文件”
7. 截图保存到 `test-results/cli/uat-regression-v13/03-message-input.png`
8. 在 snapshot 中找到 "发送" 按钮，点击
9. 等待 30 秒 (技能执行)
10. 截图保存到 `test-results/cli/uat-regression-v13/04-skill-execution-complete.png`
11. 在 snapshot 中统计技能状态条元素数量 (`.skill-status-bar` / `[data-testid="skill-status"]`)
12. 截图保存到 `test-results/cli/uat-regression-v13/05-tc005-final.png`

## 验证
- 技能执行状态区域只显示 0 或 1 个状态条目 (不应重复显示)
- PDF Skill 执行完成

---

### TC-004: 多轮工具调用生成最终回答

## 步骤

1. 在 snapshot 中找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. 找到消息输入框 (`textarea[placeholder*="输入消息"]`)，输入 "请帮我计算 25 * 37 + 123 的结果，然后把结果乘以 2"
4. 截图保存到 `test-results/cli/uat-regression-v13/06-tc004-input.png`
5. 在 snapshot 中找到 "发送" 按钮，点击
6. 等待 60 秒 (多轮工具调用)
7. 截图保存到 `test-results/cli/uat-regression-v13/07-tc004-response.png`
8. 检查消息区域是否有 AI 助手回复 (排除用户消息)
9. 截图保存到 `test-results/cli/uat-regression-v13/08-tc004-final.png`

## 验证
- 多轮工具调用后生成最终回答
- 助手消息数量 > 0

---

### Streaming: 流式输出打字机效果检查

## 步骤

1. 在 snapshot 中找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. 找到消息输入框 (`textarea[placeholder*="输入消息"]`)，输入 "请介绍一下 Agent Builder 平台的主要功能"
4. 在 snapshot 中找到 "发送" 按钮，点击
5. 等待 2 秒 (流式输出进行中)
6. 截图保存到 `test-results/cli/uat-regression-v13/09-streaming-during.png`
7. 等待 20 秒 (流式输出完成)
8. 截图保存到 `test-results/cli/uat-regression-v13/10-streaming-final.png`
9. 检查消息区域是否可见

## 验证
- 流式输出打字机效果平滑
- 消息区域正常显示
- 无消息丢失

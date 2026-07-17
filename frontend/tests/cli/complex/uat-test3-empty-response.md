# UAT: test3 Agent 空响应问题修复验证

> 来源: `frontend/tests/uat-test3-empty-response.spec.ts`
> 复杂度: complex
> 迭代: AC130-202603151517

## 前置条件
- 服务已启动 (localhost:20815)
- 存在 "test3" 智能体
- test3 agent 发送 "你好" 后应返回非空响应

## 步骤

### 测试用例 1: TC-001 test3 agent 基本响应验证（核心测试）

> 验收标准: 发送"你好"后 assistant 返回非空字符串

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/test3-empty-response-01-homepage.png
3. snapshot 找到 "test3" 智能体卡片，点击
4. 等待 3 秒
5. 截图保存到 test-results/cli/test3-empty-response-02-agent-selected.png
6. snapshot 找到调试对话输入框 `input[type="text"]`，验证输入框可见
7. 在输入框中输入 "你好"
8. 截图保存到 test-results/cli/test3-empty-response-03-message-input.png
9. 按回车发送消息
10. 等待 5 秒（流式输出开始）
11. 截图保存到 test-results/cli/test3-empty-response-04-response-streaming.png
12. 等待 15 秒（响应完成）
13. 截图保存到 test-results/cli/test3-empty-response-05-final-result.png

### 验证 (TC-001)
- 页面内容包含 "你好" 以及其他响应文字（包含 "！"、"？"、"很"、"请"、"可以" 或 "我" 等字符）
- 响应非空

### 测试用例 2: TC-002 流式输出功能回归测试

> 验收标准: 流式打字机效果正常

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "test3" 智能体卡片，点击
3. 等待 3 秒
4. snapshot 找到调试对话输入框 `input[type="text"]`，输入 "请做一个简单的自我介绍"
5. 按回车发送
6. 等待 3 秒，截图保存到 test-results/cli/test3-empty-response-streaming-1.png
7. 等待 3 秒，截图保存到 test-results/cli/test3-empty-response-streaming-2.png
8. 等待 3 秒，截图保存到 test-results/cli/test3-empty-response-streaming-3.png
9. 等待 3 秒，截图保存到 test-results/cli/test3-empty-response-streaming-4.png
10. 等待 5 秒（响应完成）
11. 截图保存到 test-results/cli/test3-empty-response-streaming-final.png

### 验证 (TC-002)
- 截图序列显示内容逐步增加（流式打字机效果）
- 最终响应内容长度 > 10 字符

### 测试用例 3: TC-003 thinking/tool_call 事件显示验证

> 验收标准: thinking/tool_call 等事件正常显示

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "test3" 智能体卡片，点击
3. 等待 3 秒
4. snapshot 找到调试对话输入框 `input[type="text"]`，输入 "帮我计算一下25乘以4等于多少"
5. 按回车发送
6. 等待 15 秒
7. 截图保存到 test-results/cli/test3-empty-response-tool-call.png

### 验证 (TC-003)
- 页面内容包含数字 "100"、"25" 或 "4"（计算结果）
- 至少有响应内容返回

### 测试用例 4: TC-004 简单问候响应测试

> 验收标准: 发送简单问候后返回非空响应

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "test3" 智能体卡片，点击
3. 等待 3 秒
4. snapshot 找到调试对话输入框 `input[type="text"]`，输入 "嗨"
5. 按回车发送
6. 等待 10 秒
7. 截图保存到 test-results/cli/test3-empty-response-greeting.png

### 验证 (TC-004)
- 响应内容长度 > 5 字符
- 响应非空

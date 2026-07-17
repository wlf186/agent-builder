# UAT: 调试日志导出功能验收

> 来源: `frontend/tests/uat-debug-log-export.spec.ts`
> 复杂度: complex
> PRD: teams/AC130/iterations/AC130-202603151423/prd.md
> 智能体: test001

## 前置条件
- 服务已启动 (localhost:20815 / localhost:20881)
- 存在 test001 智能体
- 截图目录 `test-results/cli/` 已创建

## 步骤

### 测试用例 1: TC-UAT-001 首页加载验证

> 验收标准: 智能体列表正常显示

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/uat-debug-log-export-001-agents.png

### 验证
- 智能体卡片（.agent-card 或类似元素）可见

### 测试用例 2: TC-UAT-002 进入调试对话页面

> 验收标准: 调试对话区域正常显示

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. 截图保存到 test-results/cli/uat-debug-log-export-002-chat.png

### 验证
- 输入框（textarea 或 input[type="text"]）可见

### 测试用例 3: TC-UAT-003 发送消息并触发工具调用

> 验收标准: 工具调用能正常执行

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "请帮我计算 123 * 456 的结果"
4. 截图保存到 test-results/cli/uat-debug-log-export-003-input.png
5. snapshot 找到 "发送" 按钮，点击
6. 等待 15 秒
7. 截图保存到 test-results/cli/uat-debug-log-export-003-response.png

### 验证
- 页面上有回复消息（.message 或类似元素）

### 测试用例 4: TC-UAT-004 验证日志导出按钮存在

> 验收标准: UI 上有日志导出相关按钮

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. 截图保存到 test-results/cli/uat-debug-log-export-004-button.png

### 验证
- 查找 "下载日志"、"导出日志" 或 "调试日志" 按钮
- 如果按钮未找到，记录功能可能尚未实现

### 测试用例 5: TC-UAT-005 多轮对话完整日志

> 验收标准: 多轮对话后日志包含全部消息

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "你好，请介绍一下你自己"，点击 "发送"
4. 等待 10 秒，截图保存到 test-results/cli/uat-debug-log-export-005-turn1.png
5. snapshot 找到 textarea 输入框，输入 "你能做什么？"，点击 "发送"
6. 等待 10 秒，截图保存到 test-results/cli/uat-debug-log-export-005-turn2.png
7. snapshot 找到 textarea 输入框，输入 "请计算 100 + 200"，点击 "发送"
8. 等待 10 秒，截图保存到 test-results/cli/uat-debug-log-export-005-turn3.png

### 验证
- 三轮对话均有响应
- 截图序列显示对话逐步增加

### 测试用例 6: TC-UAT-006 Trace ID 关联验证

> 验收标准: 前后端日志通过 X-Request-ID 自动关联

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. 在后续操作中注意观察：检查每个 API 请求是否包含 X-Request-ID 头
4. snapshot 找到 textarea 输入框，输入 "测试 Trace ID"，点击 "发送"
5. 等待 10 秒
6. 截图保存到 test-results/cli/uat-debug-log-export-006-trace.png

### 验证
- 浏览器 Network 面板中 API 请求带有 X-Request-ID 头
- 请求和响应的 X-Request-ID 匹配

### 测试用例 7: TC-UAT-007 工具调用详情记录

> 验收标准: 日志中体现 Tool/MCP 调用参数及返回结果

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "请使用计算器工具计算 25 * 37"，点击 "发送"
4. 等待 15 秒
5. 截图保存到 test-results/cli/uat-debug-log-export-007-tool-call.png

### 验证
- 页面上有工具调用指示器（.tool-call、.tool-result 或类似元素）
- 工具调用详情可见

### 测试用例 8: TC-UAT-008 错误场景测试

> 验收标准: 后端异常时，日志包含具体 Error Message 和堆栈

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "请读取 /nonexistent/path/to/file.txt"，点击 "发送"
4. 等待 10 秒
5. 截图保存到 test-results/cli/uat-debug-log-export-008-error.png

### 验证
- 检查是否有错误提示（.error、[role="alert"] 等元素）
- 如果操作未触发错误，记录需要人工确认

### 测试用例 9: TC-UAT-009 性能测试 - 日志采集延迟

> 验收标准: 日志采集延迟 < 1000ms

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "性能测试消息"，点击 "发送"
4. 等待 0.1 秒
5. 截图保存到 test-results/cli/uat-debug-log-export-009-performance.png

### 验证
- 响应时间正常（< 1000ms）
- 注意: 精确测量需在代码层面进行

### 测试用例 10: TC-UAT-010 安全验收 - 敏感信息脱敏

> 验收标准: API Key 等敏感信息已打码

1. 查找 "配置" 或 "设置" 按钮
2. 如果找到，点击进入配置页面
3. 等待 1 秒，截图保存到 test-results/cli/uat-debug-log-export-010-config.png

### 验证
- 需要导出日志后验证敏感信息是否脱敏
- API Key 等敏感字段应显示为打码状态

### 测试用例 11: TC-UAT-011 流式输出完整性验证

> 验收标准: 流式输出过程中日志采集不阻塞

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "请详细介绍一下人工智能的发展历史，从图灵测试讲到 GPT-4"，点击 "发送"
4. 等待 3 秒（流式输出进行中），截图保存到 test-results/cli/uat-debug-log-export-011-streaming-during.png
5. 等待 15 秒（流式输出完成），截图保存到 test-results/cli/uat-debug-log-export-011-streaming-complete.png

### 验证
- 流式输出过程中内容逐步增加
- 打字机效果正常

### 测试用例 12: TC-UAT-012 日志导出功能测试

> 验收标准: 能够成功导出日志文件

1. snapshot 找到 "调试对话" 按钮，点击
2. 等待 1 秒
3. snapshot 找到 textarea 输入框，输入 "测试日志导出功能"，点击 "发送"
4. 等待 10 秒
5. 查找 "下载日志"、"导出日志" 或 "调试日志" 按钮
6. 如果按钮存在：
   - 点击按钮触发下载
   - 等待下载完成
   - 截图保存到 test-results/cli/uat-debug-log-export-012-export-success.png
7. 如果按钮不存在：
   - 截图保存到 test-results/cli/uat-debug-log-export-012-button-not-found.png

### 验证
- 日志文件已下载（文件名应包含 debug/log 等关键词）
- 下载的日志文件不为空

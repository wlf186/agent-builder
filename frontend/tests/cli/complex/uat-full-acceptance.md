# UAT 完整验收测试 - 调试日志导出功能

> 来源: `frontend/tests/uat-full-acceptance.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (localhost:20815)
- 存在 "调试" 按钮可进入调试对话界面

## 步骤

### 测试用例 1: TC-UAT-01 Trace ID 关联验证

> 验收标准: 请求发送时的 X-Request-ID 与响应返回的 X-Request-ID 一致

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/uat-full-acceptance-01-home.png
3. 在后续操作中注意观察：每个 API 请求的 X-Request-ID 头与对应响应的 X-Request-ID 头是否匹配
4. snapshot 找到 "调试" 按钮，点击进入调试对话
5. 等待 2 秒
6. 截图保存到 test-results/cli/uat-full-acceptance-02-debug-chat.png
7. snapshot 找到 textarea 输入框，输入 "测试 Trace ID 关联"
8. 截图保存到 test-results/cli/uat-full-acceptance-03-message-input.png
9. snapshot 找到 "发送" 按钮，点击
10. 等待 10 秒
11. 截图保存到 test-results/cli/uat-full-acceptance-04-response.png

### 验证
- 检查浏览器开发者工具 Network 面板中，API 请求和响应的 X-Request-ID 一致
- 或通过 `page.on('request')` / `page.on('response')` 事件确认 ID 匹配

### 测试用例 2: TC-UAT-02 日志内容完整性验证

> 验收标准: [Request] 包含用户输入和 Agent 名称；[Execution] 包含 SSE chunks 和 tool calls；[Response] 包含模型输出

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "调试" 按钮，点击进入调试对话
3. 等待 2 秒
4. 在后续操作中注意观察：检测到 text/event-stream 类型的响应时记录 SSE 事件
5. snapshot 找到 textarea 输入框，输入 "请使用计算器工具计算 123 * 456"
6. 截图保存到 test-results/cli/uat-full-acceptance-05-content-input.png
7. snapshot 找到 "发送" 按钮，点击
8. 截图保存到 test-results/cli/uat-full-acceptance-06-content-sent.png
9. 等待 15 秒
10. 截图保存到 test-results/cli/uat-full-acceptance-07-content-response.png

### 验证
- 页面上有消息元素（.message / .prose 等）
- 页面上有工具调用相关元素（tool-call 指示器）
- SSE 流式响应被正确检测到

### 测试用例 3: TC-UAT-03 日志导出功能验证

> 验收标准: 点击"下载调试日志"按钮，导出 JSON 格式文件，敏感信息已脱敏

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "调试" 按钮，点击进入调试对话
3. 等待 2 秒
4. snapshot 找到 textarea 输入框，输入 "请介绍一下这个平台的功能"
5. snapshot 找到 "发送" 按钮，点击
6. 等待 10 秒
7. 截图保存到 test-results/cli/uat-full-acceptance-08-before-export.png
8. snapshot 找到 "调试日志" 按钮
9. 如果按钮可见：
   - 点击按钮触发下载
   - 等待 3 秒
   - 截图保存到 test-results/cli/uat-full-acceptance-09-after-download.png
   - 验证下载的 JSON 文件包含 meta、client、server、requestId 字段
   - 验证敏感信息（API Key、Token）已脱敏

### 验证
- "调试日志" 按钮可见
- 下载的文件为有效 JSON 格式
- JSON 包含 meta、client、server 等关键字段
- 内容中不含明文 API Key 或 Token

### 测试用例 4: TC-UAT-04 流式输出验证

> 验收标准: 打字机效果正常，思考过程实时显示，工具调用正确展示

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "调试" 按钮，点击进入调试对话
3. 等待 2 秒
4. snapshot 找到 textarea 输入框，输入 "请详细介绍人工智能的发展历史，包括图灵测试、专家系统、深度学习等关键里程碑"
5. 截图保存到 test-results/cli/uat-full-acceptance-10-stream-input.png
6. snapshot 找到 "发送" 按钮，点击
7. 等待 3 秒，截图保存到 test-results/cli/uat-full-acceptance-11-stream-during.png（流式输出进行中）
8. 等待 5 秒，截图保存到 test-results/cli/uat-full-acceptance-12-stream-mid.png（流式输出中期）
9. 等待 10 秒，截图保存到 test-results/cli/uat-full-acceptance-13-stream-final.png（流式输出完成）

### 验证
- 流式输出过程中内容逐步增加（打字机效果）
- 最终响应内容长度 > 50 字符
- 思考过程和工具调用正确展示

### 测试用例 5: TC-UAT-05 完整用户流程测试

> 验收标准: 端到端流程正常

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/uat-full-acceptance-14-flow-home.png
3. snapshot 找到 "调试" 按钮，点击进入调试对话
4. 等待 2 秒，截图保存到 test-results/cli/uat-full-acceptance-15-flow-chat.png
5. snapshot 找到 textarea 输入框，输入 "请使用计算器计算 (100 + 200) * 3"
6. 截图保存到 test-results/cli/uat-full-acceptance-16-flow-input.png
7. snapshot 找到 "发送" 按钮，点击
8. 等待 12 秒，截图保存到 test-results/cli/uat-full-acceptance-17-flow-response.png
9. snapshot 找到 "调试日志" 按钮
10. 如果按钮可见：
    - 截图保存到 test-results/cli/uat-full-acceptance-18-flow-download-btn.png
    - 点击按钮
    - 等待 2 秒
    - 截图保存到 test-results/cli/uat-full-acceptance-19-flow-after-download.png

### 验证
- 完整流程：主页 -> 调试对话 -> 发送消息 -> 收到回复 -> 下载日志
- 每一步截图正常保存

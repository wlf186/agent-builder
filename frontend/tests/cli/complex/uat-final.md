# UAT 最终验收测试 - 调试日志导出功能

> 来源: `frontend/tests/uat-final.spec.ts`
> 复杂度: complex
> 验收标准（PRD 第5节）:
> - 5.1 功能验收: 完整会话日志、Trace ID 关联、工具调用详情、错误详情
> - 5.2 性能验收: 日志采集延迟、导出速度
> - 5.3 安全验收: 敏感信息脱敏

## 前置条件
- 前端服务已启动 (localhost:20815)
- 后端服务已启动 (localhost:20881)

---

### 测试用例 UAT-FINAL-001: 完整流程 - 发送消息并导出日志

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/uat-final-01-homepage.png`
3. 在 snapshot 中找到调试对话入口（尝试以下元素: 包含 "调试" 文本的链接或按钮、包含 "chat" 的 href 链接）。如果都找不到，直接导航到 `http://localhost:20815/?chat=test001`
4. 等待 2 秒
5. 截图保存到 `test-results/cli/uat-final-02-chat-page.png`
6. 在 snapshot 中找到聊天输入框（尝试 `textarea`, `input[type="text"]`, `input[placeholder*="输入"]`, `textarea[placeholder*="输入"]`），输入 "请介绍一下你自己"
7. 截图保存到 `test-results/cli/uat-final-03-message-filled.png`
8. 在 snapshot 中找到发送按钮（包含 "发送" 文本的按钮、`button[type="submit"]` 或 `button[aria-label*="发送"]`），点击发送
9. 等待 10 秒
10. 截图保存到 `test-results/cli/uat-final-04-after-response.png`
11. 在 snapshot 中找到下载日志按钮（尝试: 包含 "下载日志"、"导出日志"、"调试日志"、"download"、"Download" 文本的按钮，或 `aria-label` 包含 "log" 的按钮）
12. 截图保存到 `test-results/cli/uat-final-05-download-check.png`
13. 点击下载按钮，等待文件下载完成

## 验证
- 日志文件应成功下载
- 日志文件应包含以下关键信息:
  - Request ID (requestId / trace_id / X-Request-ID)
  - Timestamp (timestamp / exportedAt)
  - Client info (client / environment)
- 截图保存到 `test-results/cli/uat-final-06-download-success.png`

---

### 测试用例 UAT-FINAL-002: Trace ID 验证 - 检查请求头

## 步骤
1. 在后续操作中注意观察网络请求头中的 `X-Request-ID` 字段（`page.on('request')` 监听所有请求的 headers）
2. 访问 `http://localhost:20815`，等待页面加载完成
3. 等待 2 秒
4. 截图保存到 `test-results/cli/uat-final-traceid-check.png`

## 验证
- 网络请求中应包含带有 `X-Request-ID` 请求头的请求

---

### 测试用例 UAT-FINAL-003: 后端日志 API 验证

## 步骤
1. 使用以下代码调用后端 API:

```
run-code "async page => {
  const testTraceId = 'test-uat-' + Date.now();
  const response = await page.request.get(
    'http://localhost:20881/api/debug-logs/' + testTraceId
  );
  console.log('API 响应状态: ' + response.status());
  if (response.status() === 200) {
    const data = await response.json();
    console.log('API 返回数据:', JSON.stringify(data).substring(0, 200));
  }
  return response.status();
}"
```

2. 截图保存到 `test-results/cli/uat-final-api-test.png`

## 验证
- API 响应状态码应小于 500
- 404 表示端点可访问（trace_id 不存在但端点正常）
- 200 表示返回了日志数据

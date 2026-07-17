# UAT 验收测试 - 日志导出功能

> 来源: `frontend/tests/uat-log-export.spec.ts`
> 复杂度: complex
> 验证场景:
> 1. 正常对话场景: 验证日志包含完整 Prompt 和 Response
> 2. 工具调用场景: 验证日志包含 Tool Call 参数和返回
> 3. 错误场景: 验证日志包含后端 Error Message 和堆栈
> 4. 导出功能: 验证下载的日志文件格式正确
> 5. 脱敏验证: 确认 API Key 已打码

## 前置条件
- 前端服务已启动 (localhost:20815)
- 后端服务已启动 (localhost:20881)
- 存在名为 "test-uat-log" 的智能体

---

### 测试用例 UC-LOG-001: 前端应该有日志下载按钮

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒

## 验证
- 页面应显示 "下载调试日志" 按钮

---

### 测试用例 UC-LOG-002: 正常对话后应该生成日志

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒
5. 在 snapshot 中找到消息输入框（`textarea[placeholder*="输入消息"]` 或 `textarea[placeholder*="message"]`），输入 "你好，请介绍一下你自己"
6. 在 snapshot 中找到发送按钮（包含 "发送" 文本的按钮或 `button[type="submit"]`），点击发送
7. 等待 5 秒

## 验证
- 页面应显示日志计数（类似 "N 条日志" 的文本）

---

### 测试用例 UC-LOG-003: 工具调用场景日志应该包含工具信息

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒
5. 在 snapshot 中找到消息输入框，输入 "请使用 calculator 工具计算 123 + 456"
6. 按 Enter 发送消息
7. 等待 8 秒（等待响应和工具调用完成）

## 验证
- 页面应显示 "工具调用" 区域

---

### 测试用例 UC-LOG-004: 日志导出功能应该下载文件

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒
5. 点击 "下载调试日志" 按钮
6. 等待下载完成

## 验证
- 应触发文件下载，文件名应匹配 `client_log_` 或 `chat-debug-log-` 前缀
- 如果是 JSON 文件: 应包含 timestamp、userAgent、consoleLogs 字段
- 如果是 TXT 文件: 内容应包含 "日志" 文本

---

### 测试用例 UC-LOG-005: 主页日志下载按钮应该可用

## 步骤
1. 访问 `http://localhost:20815`

## 验证
- 主页应显示 "下载调试日志" 按钮

---

### 测试用例 UC-LOG-101: 日志应该包含请求时间戳

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒
5. 在 snapshot 中找到消息输入框，输入 "Hello"
6. 按 Enter 发送消息
7. 等待 5 秒
8. 点击 "下载调试日志" 按钮
9. 等待下载完成

## 验证
- 下载的日志文件应包含时间戳格式 `YYYY-MM-DDTHH:MM:SS`

---

### 测试用例 UC-LOG-102: 错误场景应该记录错误信息

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒
5. 在 snapshot 中找到消息输入框，输入一个超长消息（10000 个 "A" 字符）

```
run-code "async page => {
  const inputArea = page.locator('textarea[placeholder*=\"输入消息\"], textarea[placeholder*=\"message\"]').first();
  await inputArea.fill('A'.repeat(10000));
  await inputArea.press('Enter');
}"
```

6. 按 Enter 发送消息
7. 等待 3 秒

## 验证
- 即使没有响应，日志指示器也应存在（页面显示日志条数）

---

### 测试用例 UC-LOG-201: 日志不应该包含完整 API Key

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 0.5 秒
3. 在 snapshot 中找到 "test-uat-log" 智能体，点击进入
4. 等待 0.5 秒
5. 点击 "下载调试日志" 按钮
6. 等待下载完成

## 验证
- 下载的日志文件不应包含完整 API Key（不应匹配 `sk-[48个字母数字字符]` 格式）
- 检查以下 API Key 模式是否已脱敏:
  - OpenAI 格式: `sk-[32+字符]`
  - Bearer token: `Bearer [32+字符]`
  - api_key 字段: `api_key` 或 `api-key` 后跟 32+ 字符

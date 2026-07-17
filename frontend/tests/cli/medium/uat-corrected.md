# 修正后的 UAT 验收: 调试日志导出

> 来源: `frontend/tests/uat-corrected.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)

## 步骤

### 测试用例 1: 完整验收流程

#### 步骤 1: 访问首页
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 截图保存到 test-results/cli/uat-corrected-01-homepage.png

#### 步骤 2: 进入调试对话页面
4. 点击 "调试" 按钮
5. 等待 2 秒
6. 截图保存到 test-results/cli/uat-corrected-02-chat-enter.png

#### 步骤 3: 发送测试消息
7. 在 textarea 中输入 "你好，请介绍一下你自己"
8. 截图保存到 test-results/cli/uat-corrected-03-message-filled.png
9. 点击 "发送" 按钮

#### 步骤 4: 等待 AI 响应完成
10. 等待 15 秒让 AI 完成响应
11. 截图保存到 test-results/cli/uat-corrected-04-response-complete.png

#### 步骤 5: 验证下载按钮出现
12. 查找以下任一按钮: "下载调试日志"、"调试日志"、"download"、"Download"
13. 截图保存到 test-results/cli/uat-corrected-05-download-check.png

#### 步骤 6: 点击下载按钮
14. 如果找到了下载按钮，点击它
15. 等待 3 秒
16. 截图保存到 test-results/cli/uat-corrected-06-after-click.png

#### 步骤 7: 验证 JSON 内容
17. 截图保存到 test-results/cli/uat-corrected-07-final.png

> 注意: 下载可能使用 Blob URL，需要手动验证下载的 JSON 文件内容。

### 测试用例 2: Trace ID 全链路验证

> 在后续操作中注意观察网络请求中的 `X-Request-ID` 请求头。

18. 打开 http://localhost:20815
19. 等待页面加载完成
20. 点击 "调试" 按钮
21. 等待 2 秒
22. 在 textarea 中输入 "测试 Trace ID"
23. 点击 "发送" 按钮
24. 等待 10 秒
25. 截图保存到 test-results/cli/uat-corrected-traceid-verification.png

## 验证

### 测试用例 1 验证:
- AI 响应正常完成
- 响应完成后出现下载调试日志按钮（文本为 "下载调试日志"、"调试日志"、"download" 或 "Download" 之一）
- 点击下载后能获取到日志文件
- 日志 JSON 包含正确结构: `meta.version` 为 "1.0"、`meta.requestId` 存在、`client.environment` 存在、`client.chunks` 存在

### 测试用例 2 验证:
- 网络请求中检测到带有 `X-Request-ID` 请求头的请求

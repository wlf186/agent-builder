# UAT 验收测试 - 调试日志导出功能

> 来源: `frontend/tests/uat-real.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (前端 localhost:20815, 后端 localhost:20881)
- test001 智能体已配置

---

### UAT-REAL-001: 完整验收流程

## 步骤

1. 导航到 `http://localhost:20815/?agent=test001&chat=true`，等待页面加载完成
2. 等待 2 秒
3. 截图保存到 `test-results/cli/uat-real/real-01-chat-page.png`
4. 找到消息输入框 (textarea)，输入 "你好，请介绍一下你自己"
5. 截图保存到 `test-results/cli/uat-real/real-02-message-filled.png`
6. 在 snapshot 中找到 "发送" 按钮，点击
7. 等待 15 秒
8. 截图保存到 `test-results/cli/uat-real/real-03-response-done.png`
9. 在 snapshot 中查找下载按钮:
   - 尝试匹配: "下载调试日志" / "调试日志" / "Download"
10. 截图保存到 `test-results/cli/uat-real/real-04-download-check.png`
11. 如果找到下载按钮:
    - 点击下载按钮
    - 等待 3 秒
    - 截图保存到 `test-results/cli/uat-real/real-05-after-download.png`
    - 截图保存到 `test-results/cli/uat-real/real-06-final.png`
12. 如果未找到下载按钮:
    - 截图保存到 `test-results/cli/uat-real/real-05-no-button.png`

## 验证
- 聊天页面正常加载
- 消息发送成功，AI 正常回复
- 下载调试日志按钮存在且可点击 (核心验收)
- 下载功能正常执行 (使用 Blob URL 方式)

---

### UAT-REAL-002: Trace ID 验证

## 步骤

> 在后续操作中注意观察: 监听所有网络请求的 `X-Request-ID` / `x-request-id` 请求头

1. 导航到 `http://localhost:20815/?agent=test001&chat=true`，等待页面加载完成
2. 等待 2 秒
3. 找到消息输入框 (textarea)，输入 "测试 Trace ID"
4. 在 snapshot 中找到 "发送" 按钮，点击
5. 等待 10 秒
6. 截图保存到 `test-results/cli/uat-real/real-traceid.png`

## 验证
- 网络请求中包含 `X-Request-ID` 请求头
- Trace ID 被正确传递和记录

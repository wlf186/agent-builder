# UAT 走查 - 用户代表实际访问验证

> 来源: `frontend/tests/uat-walkthrough.spec.ts`
> 复杂度: complex
> 测试目标: 验证系统页面显示正常，数据加载正确

## 前置条件
- 服务已启动 (localhost:20815)

---

### 测试用例 1: 页面样式正常加载

## 步骤
1. 访问 `http://localhost:20815`，等待 2 秒
2. 截图保存到 `test-results/cli/uat-walkthrough-homepage.png`

## 验证
- 页面背景色不应为纯白色 `rgb(255, 255, 255)`（应为深色主题，如 #0a0a0f）
- 说明 CSS 样式已正确加载

---

### 测试用例 2: 智能体列表不为空

## 步骤
1. 访问 `http://localhost:20815`，等待 3 秒
2. 截图保存到 `test-results/cli/uat-walkthrough-agents.png`

## 验证
- 侧边栏应有智能体列表，不是空列表
- 页面中应能看到 test001 或其他智能体名称

---

### 测试用例 3: API 数据加载正常

## 步骤
1. 在后续操作中注意观察网络请求响应（`page.on('response')` 监听 `/api/` 路径的响应状态和内容）
2. 访问 `http://localhost:20815`
3. 刷新页面，等待 3 秒
4. 在后续操作中注意观察控制台错误（`page.on('console')` 监听 error 类型消息）
5. 等待 2 秒

## 验证
- API 请求应返回 200 状态码
- 控制台不应有 JavaScript 错误（第三方脚本错误可忽略）

---

### 测试用例 4: 模型服务列表不为空

## 步骤
1. 访问 `http://localhost:20815`，等待 2 秒
2. 截图保存到 `test-results/cli/uat-walkthrough-sidebar.png`

## 验证
- 页面中应有模型服务相关内容

---

### 测试用例 5: MCP 服务列表不为空

## 步骤
1. 访问 `http://localhost:20815`，等待 2 秒
2. 截图保存到 `test-results/cli/uat-walkthrough-mcp.png`

## 验证
- 页面中应有 MCP 服务相关内容

---

### 测试用例 6: 检查数字显示

## 步骤
1. 访问 `http://localhost:20815`，等待 3 秒
2. 截图保存到 `test-results/cli/uat-walkthrough-full.png`

## 验证
- 页面不应显示 "0 个" 或 "0 agents" 等空状态数字
- 页面应包含 "test001" 和 "TESTLLM" 等已配置数据

---

### 测试用例 7: 无 JavaScript 错误

## 步骤
1. 在后续操作中注意观察控制台错误（`page.on('console')` 监听 error 类型消息）和页面错误（`page.on('pageerror')` 监听）
2. 访问 `http://localhost:20815`
3. 等待 5 秒

## 验证
- 控制台不应有 JavaScript 错误（第三方脚本错误可忽略）

---

### 测试用例 8: 网络请求检查

## 步骤
1. 在后续操作中注意观察失败的网络请求（`page.on('requestfailed')` 监听）
2. 访问 `http://localhost:20815`
3. 等待 5 秒

## 验证
- 不应有失败的网络请求

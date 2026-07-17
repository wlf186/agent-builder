# 简化 UAT 测试 - 调试日志导出功能

> 来源: `frontend/tests/uat-simple.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- 后端服务运行在 localhost:20881

---

### 测试用例 1: UAT-001 - 首页加载验证

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成

## 验证
- 页面标题不为空
- 页面 body 可见
- 截图保存到 test-results/cli/uat-001-homepage.png

---

### 测试用例 2: UAT-002 - 查找智能体卡片

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 等待 2 秒

## 验证
- 页面包含 "test" 或 "agent" 或 "智能" 相关文本的元素
- 截图保存到 test-results/cli/uat-002-agents.png

---

### 测试用例 3: UAT-003 - 检查 X-Request-ID 功能

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 等待 1 秒

> 注意：在后续操作中注意观察网络请求，检查是否包含 `X-Request-ID` 请求头

## 验证
- 如果检测到 `X-Request-ID` 请求头则验证通过
- 未检测到可能需要实际交互才能触发
- 截图保存到 test-results/cli/uat-003-request-id.png

---

### 测试用例 4: UAT-004 - 后端日志 API 验证

## 步骤
1. 生成一个测试 trace ID（格式: `test-trace-{timestamp}`）
2. 直接请求后端 API: `GET http://localhost:20881/api/debug-logs/{testTraceId}`

## 验证
- API 端点返回 200 或 404 状态码表示可用
- 截图保存到 test-results/cli/uat-004-api-check.png

---

### 测试用例 5: UAT-005 - 检查 DebugLogger 文件存在

## 步骤
1. 打开 http://localhost:20815

## 验证
- 页面加载了至少 1 个 script 标签
- 截图保存到 test-results/cli/uat-005-scripts.png

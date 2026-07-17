# UAT 验收测试 - iteration-2603121000

> 来源: `frontend/tests/iteration-2603121000-uat.spec.ts`
> 复杂度: complex
> 测试目标: 验证文件上传与 Skill 执行功能、流式输出、API 接口、智能体配置

## 前置条件
- 服务已启动 (localhost:20815)
- 已存在 test001、skill-test-pdf、skill-test-doc 智能体
- 测试文件路径：仓库内 `.runtime/test-results/iteration-2603121000/测试1.pdf`

---

### 测试用例 UC-001: 文件上传功能验证

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 2 秒
3. 在页面上查找"skill-test-pdf"文本，如果不可见则刷新页面并等待 2 秒
4. snapshot 找到"skill-test-pdf"文本，点击
5. 等待 2 秒
6. 检查页面上是否有文件上传按钮（`input[type="file"]`）
7. 截图保存到 `test-results/cli/iteration-2603121000-uc001.png`

## 验证
- 文件上传按钮应存在
- skill-test-pdf 智能体应可正常访问

---

### 测试用例 UC-005: test001 例行验证 - 3轮对话

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 2 秒
3. snapshot 找到"test001"文本，点击
4. 等待 2 秒
5. 发送第 1 轮对话：
   - snapshot 找到聊天输入框（`textarea`），输入"现在是几月几号几点几分"
   - 按 Enter 键发送
   - 等待 15 秒
6. 发送第 2 轮对话：
   - snapshot 找到聊天输入框（`textarea`），输入"99/33是多少"
   - 按 Enter 键发送
   - 等待 15 秒
7. 发送第 3 轮对话：
   - snapshot 找到聊天输入框（`textarea`），输入"计算结果再加2.5是多少"
   - 按 Enter 键发送
   - 等待 15 秒
8. 截图保存到 `test-results/cli/iteration-2603121000-uc005.png`

## 验证
- 每轮对话应收到回复（页面有消息/响应元素）
- 3 轮对话均能正常完成

---

### 测试用例 UC-006: 流式输出验证

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 2 秒
3. snapshot 找到"test001"文本，点击
4. 等待 2 秒
5. snapshot 找到聊天输入框（`textarea`），输入"请简单介绍一下你自己"
6. 按 Enter 键发送
7. 等待 5 秒，观察打字机光标效果
8. 等待 10 秒
9. 截图保存到 `test-results/cli/iteration-2603121000-uc006.png`

## 验证
- 应有打字机光标效果（`animate-pulse` 动画元素）
- 流式输出应正常显示

---

### 测试用例 UC-007: 大文件上传验证

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 2 秒
3. snapshot 找到"skill-test-pdf"文本，点击
4. 等待 2 秒
5. 检查文件上传输入框（`input[type="file"]`）是否存在
6. 如果存在：
   - 记录开始时间
   - 上传测试 PDF 文件（`.runtime/test-results/iteration-2603121000/测试1.pdf`）
   - 等待 5 秒
   - 记录上传耗时
7. 截图保存到 `test-results/cli/iteration-2603121000-uc007.png`

## 验证
- 文件上传输入框应存在
- 上传应能在合理时间内完成

---

### 测试用例 API验证: 文件上传接口

## 步骤
1. 使用 `run-code` 执行以下操作：
   - 发送 POST 请求到 `http://localhost:20881/api/agents/skill-test-pdf/files`，上传测试 PDF（multipart form，file 字段名 `test.pdf`，MIME 类型 `application/pdf`）
   - 发送 POST 请求到 `http://localhost:20881/api/agents/skill-test-doc/files`，上传测试 DOCX（multipart form，file 字段名 `test.docx`，MIME 类型 `application/vnd.openxmlformats-officedocument.wordprocessingml.document`）

## 验证
- PDF 上传接口应返回响应（记录状态码）
- DOCX 上传接口应返回响应（记录状态码）

---

### 测试用例 API验证: 智能体对话接口

## 步骤
1. 使用 `run-code` 执行以下操作：
   - 发送 POST 请求到 `http://localhost:20881/api/agents/test001/chat`，data 为 `{"message": "你好"}`

## 验证
- 接口应返回 200 状态码
- 响应中应包含 `response` 字段且内容非空

---

### 测试用例 API验证: 流式输出接口

## 步骤
1. 使用 `run-code` 执行以下操作：
   - 发送 POST 请求到 `http://localhost:20881/api/agents/test001/chat/stream`，data 为 `{"message": "1+1等于几？"}`

## 验证
- 接口应返回 200 状态码
- 响应文本应包含 `"type": "thinking"` 事件
- 响应文本应包含 `"type": "content"` 事件

---

### 测试用例 检查测试智能体配置

## 步骤
1. 使用 `run-code` 执行以下操作：
   - GET 请求 `http://localhost:20881/api/agents/test001`，检查 `model_service` 字段
   - GET 请求 `http://localhost:20881/api/agents/skill-test-pdf`，检查 `skills` 字段
   - GET 请求 `http://localhost:20881/api/agents/skill-test-doc`，检查 `skills` 字段

## 验证
- 各智能体配置应正确返回
- skill-test-pdf 和 skill-test-doc 应有 skills 配置

---

### 测试用例 检查模型服务配置

## 步骤
1. 使用 `run-code` 执行以下操作：
   - GET 请求 `http://localhost:20881/api/model-services`

## 验证
- 应返回模型服务列表
- 每个服务应包含 name、provider、selected_model 字段

---

### 测试用例 检查 Skill 列表

## 步骤
1. 使用 `run-code` 执行以下操作：
   - GET 请求 `http://localhost:20881/api/skills`

## 验证
- 应返回 Skill 列表
- 列表中应包含 PDF 相关的 Skill
- 列表中应包含 DOCX 相关的 Skill

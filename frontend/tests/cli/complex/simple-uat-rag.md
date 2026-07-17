# RAG 知识库管理功能 UAT 测试（简化版）

> 来源: `frontend/tests/simple-uat-rag.spec.ts`
> 复杂度: complex
> 测试目标: 知识库创建、文档上传、智能体关联、对话检索、隔离性测试

## 前置条件
- 服务已启动 (localhost:20815)
- 测试文档路径: `teams/AC130/iterations/202603170949/test_documents/Cyberpunk公司2026员工手册.txt`

---

### 测试用例 测试1: 知识库创建

## 步骤
1. 访问 `http://localhost:20815/knowledge-bases`，等待页面加载完成
2. 截图保存到 `test-results/cli/simple-uat-rag-01-kb-list.png`
3. snapshot 找到"创建知识库"或"Create"按钮，点击
4. 等待 1 秒
5. 截图保存到 `test-results/cli/simple-uat-rag-02-create-dialog.png`
6. 遍历页面上的 `input[type="text"]` 元素，找到 placeholder 包含"名称"的输入框，输入"人力资源库"
7. 遍历页面上的 `textarea` 元素，找到 placeholder 包含"描述"的文本框，输入"包含员工手册、考勤制度、报销流程等人力资源相关文档"
8. 截图保存到 `test-results/cli/simple-uat-rag-03-form-filled.png`
9. snapshot 找到"创建"或"Create"或"Submit"按钮，点击
10. 等待 3 秒
11. 截图保存到 `test-results/cli/simple-uat-rag-04-created.png`

## 验证
- 知识库"人力资源库"应创建成功

---

### 测试用例 测试2: 文档上传

## 步骤
1. 访问 `http://localhost:20815/knowledge-bases`，等待页面加载完成
2. snapshot 找到第一个知识库卡片（`h3` 或 `a` 或包含"card"的元素），点击
3. 等待 2 秒
4. 截图保存到 `test-results/cli/simple-uat-rag-05-kb-detail.png`
5. snapshot 找到文件上传输入框（`input[type="file"]`），上传测试文档
   - 如果文件输入框不可见，先点击"上传"或"Upload"或"添加"按钮，再上传
6. 等待 5 秒
7. 截图保存到 `test-results/cli/simple-uat-rag-06-uploaded.png`

## 验证
- 文档应上传成功

---

### 测试用例 测试3: 智能体关联

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/simple-uat-rag-07-homepage.png`
3. snapshot 找到"创建智能体"或"Create Agent"按钮，点击
4. 等待 1 秒
5. 遍历 `input[type="text"]` 元素，找到 placeholder 包含"名称"或"name"的输入框，输入"行政助手"
6. 遍历 `textarea` 元素，找到 placeholder 包含"人设"或"prompt"的文本框，输入"你是公司的行政助手，负责回答人力资源相关问题。"
7. 截图保存到 `test-results/cli/simple-uat-rag-08-agent-creating.png`
8. 如果页面有复选框（`input[type="checkbox"]`），勾选第一个
9. 等待 500 毫秒
10. 截图保存到 `test-results/cli/simple-uat-rag-09-kb-selected.png`
11. snapshot 找到"保存"或"Save"按钮，点击
12. 等待 2 秒
13. 截图保存到 `test-results/cli/simple-uat-rag-10-agent-saved.png`

## 验证
- 智能体"行政助手"应创建成功并关联知识库

---

### 测试用例 测试4: 对话检索

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到"行政助手"文本，点击
3. 等待 2 秒
4. 截图保存到 `test-results/cli/simple-uat-rag-11-agent-selected.png`
5. snapshot 找到聊天输入框（`input[type="text"][placeholder]` 或 `textarea[placeholder*="输入"]`），输入"公司有几天年假？"
6. 截图保存到 `test-results/cli/simple-uat-rag-12-question-entered.png`
7. 按 Enter 键发送
8. 等待 20 秒
9. 截图保存到 `test-results/cli/simple-uat-rag-13-answer-received.png`

## 验证
- 页面内容应包含"年假"相关文本
- 页面内容应包含"检索"相关文本（RAG 检索被触发）

---

### 测试用例 测试5: 隔离性测试

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到"创建智能体"或"Create Agent"按钮，点击
3. 等待 1 秒
4. 遍历 `input[type="text"]` 元素，找到 placeholder 包含"名称"或"name"的输入框，输入"技术支持"
5. 遍历 `textarea` 元素，找到 placeholder 包含"人设"或"prompt"的文本框，输入"你是公司的技术支持，负责解决技术问题，不负责人力资源相关问题。"
6. snapshot 找到"保存"或"Save"按钮，点击（不勾选任何知识库）
7. 等待 2 秒
8. 截图保存到 `test-results/cli/simple-uat-rag-14-tech-agent-created.png`
9. snapshot 找到聊天输入框（`input[type="text"][placeholder]` 或 `textarea[placeholder*="输入"]`），输入"公司有几天年假？"
10. 按 Enter 键发送
11. 等待 15 秒
12. 截图保存到 `test-results/cli/simple-uat-rag-15-isolation-test.png`

## 验证
- 未关联知识库的智能体不应触发 RAG 检索
- 回应不应包含知识库检索的内容

# RAG 知识库管理功能 UAT 测试（最终版）

> 来源: `frontend/tests/final-uat-rag.spec.ts`
> 复杂度: complex
> 测试目标: 知识库列表访问、知识库详情、文档上传、智能体创建、对话检索、隔离性测试

## 前置条件
- 服务已启动 (localhost:20815)
- 测试文档路径: `teams/AC130/iterations/202603170949/test_documents/Cyberpunk公司2026员工手册.txt`
- 已存在知识库 ID: `kb_7116e7ed`（人力资源库）

---

### 测试用例 测试1: 访问知识库列表

## 步骤
1. 访问 `http://localhost:20815/knowledge-bases`，等待页面加载完成
2. 截图保存到 `test-results/cli/final-uat-rag-01-kb-list.png`

## 验证
- 页面内容应包含"人力资源库"或"知识库"文本

---

### 测试用例 测试2: 进入知识库详情

## 步骤
1. 访问 `http://localhost:20815/knowledge-bases`，等待页面加载完成
2. snapshot 找到"人力资源库"文本，点击
3. 等待页面加载完成
4. 截图保存到 `test-results/cli/final-uat-rag-02-kb-detail.png`

## 验证
- 应成功进入知识库详情页

---

### 测试用例 测试3: 上传文档

## 步骤
1. 访问 `http://localhost:20815/knowledge-bases/kb_7116e7ed`，等待页面加载完成
2. 截图保存到 `test-results/cli/final-uat-rag-03-before-upload.png`
3. snapshot 找到文件上传输入框（`input[type="file"]`）
4. 如果文件上传输入框可见：
   - 上传测试文档
   - 等待 5 秒
   - 截图保存到 `test-results/cli/final-uat-rag-04-uploading.png`
   - 再等待 5 秒
   - 截图保存到 `test-results/cli/final-uat-rag-05-after-upload.png`
5. 如果文件上传输入框不可见：
   - 截图保存到 `test-results/cli/final-uat-rag-03-no-upload-input.png`

## 验证
- 文档应上传成功
- 处理应正常完成

---

### 测试用例 测试4: 创建测试智能体

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/final-uat-rag-06-homepage.png`
3. snapshot 找到"创建智能体"或"Create"按钮，点击
4. 等待 1 秒
5. 截图保存到 `test-results/cli/final-uat-rag-07-create-dialog.png`
6. snapshot 找到名称输入框（`input[placeholder*="名称"]` 或 `input[name="name"]` 或 `input[type="text"]`），输入"UAT行政助手"
7. snapshot 找到人设文本框（`textarea[placeholder*="人设"]` 或 `textarea[name="system_prompt"]`），输入"你是公司的行政助手，负责回答人力资源相关问题。请基于知识库内容回答。"
8. 截图保存到 `test-results/cli/final-uat-rag-08-agent-info-filled.png`
9. 检查页面上的复选框（`input[type="checkbox"]`），如果存在则勾选第一个
10. 等待 500 毫秒
11. 截图保存到 `test-results/cli/final-uat-rag-09-kb-selected.png`
12. snapshot 找到"保存"或"Save"或"创建"或"Create"按钮，点击
13. 等待 2 秒
14. 截图保存到 `test-results/cli/final-uat-rag-10-agent-saved.png`

## 验证
- 智能体"UAT行政助手"应创建成功
- 知识库应已勾选

---

### 测试用例 测试5: 对话检索测试

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到"UAT行政助手"文本，点击
   - 如果找不到，选择页面上第一个可用的智能体（`h3` 或 `h2` 元素）
3. 等待 2 秒
4. 截图保存到 `test-results/cli/final-uat-rag-11-agent-selected.png`
5. snapshot 找到聊天输入框（`input[type="text"][placeholder*="输入"]` 或 `textarea[placeholder*="输入"]`）
6. 如果输入框可见：
   - 输入"公司有几天年假？"
   - 截图保存到 `test-results/cli/final-uat-rag-12-question-filled.png`
   - 按 Enter 键发送
   - 等待 20 秒
   - 截图保存到 `test-results/cli/final-uat-rag-13-answer-received.png`
7. 如果输入框不可见：
   - 截图保存到 `test-results/cli/final-uat-rag-12-no-input.png`

## 验证
- 回答应包含"年假"、"15"或"天"等相关内容
- 页面应显示检索提示（包含"检索"、"知识库"或"来源"等文本）

---

### 测试用例 测试6: 隔离性测试

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到"创建智能体"或"Create"按钮，点击
3. 等待 1 秒
4. snapshot 找到名称输入框（`input[placeholder*="名称"]` 或 `input[name="name"]` 或 `input[type="text"]`），输入"UAT技术支持"
5. snapshot 找到人设文本框（`textarea[placeholder*="人设"]` 或 `textarea[name="system_prompt"]`），输入"你是公司的技术支持，只负责技术问题，不回答人力资源问题。"
6. 截图保存到 `test-results/cli/final-uat-rag-14-tech-agent-creating.png`
7. snapshot 找到"保存"或"Save"或"创建"或"Create"按钮，点击（不勾选任何知识库）
8. 等待 2 秒
9. 截图保存到 `test-results/cli/final-uat-rag-15-tech-agent-created.png`
10. snapshot 找到聊天输入框（`input[type="text"][placeholder*="输入"]` 或 `textarea[placeholder*="输入"]`）
11. 如果输入框可见：
    - 输入"公司有几天年假？"
    - 按 Enter 键发送
    - 等待 15 秒
    - 截图保存到 `test-results/cli/final-uat-rag-16-isolation-answer.png`

## 验证
- 未关联知识库的智能体不应触发 RAG 检索
- 回答应包含"不知道"、"无法"或"技术支持"等文本（拒绝回答或表示无相关知识）

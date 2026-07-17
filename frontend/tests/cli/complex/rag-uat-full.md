# RAG 知识库系统完整验收测试

> 来源: `frontend/tests/rag-uat-full.spec.ts`
> 复杂度: complex
> 测试目标: 验证知识库创建/文档上传、智能体挂载知识库、RAG 检索功能、无知识库时不触发检索、前端 UI 元素

## 前置条件
- 服务已启动 (localhost:20815)
- 测试文档路径: `data/knowledge_base/documents/cyberpunk_employee_handbook.txt` 和 `cyberpunk_code_standards.txt`

---

### 测试用例 步骤1: 检查知识库并上传文档

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到知识库管理按钮（`[data-testid="knowledge-base-button"]` 或包含"知识库"文本的按钮），点击
3. 等待 500 毫秒
4. 检查页面上是否存在"人力资源库"文本
5. 如果不存在：
   - snapshot 找到"创建知识库"按钮，点击
   - 等待 300 毫秒
   - snapshot 找到知识库名称输入框（`input[placeholder*="知识库名称"]`），输入"人力资源库"
   - snapshot 找到描述文本框（`textarea[placeholder*="描述"]`），输入"Cyberpunk公司人力资源相关文档"
   - snapshot 找到"创建"按钮，点击
   - 等待 500 毫秒
6. snapshot 找到"人力资源库"文本，点击进入详情
7. 等待 500 毫秒
8. 检查现有文档数量
9. snapshot 找到文件上传输入框（`input[type="file"]`），上传测试文档（cyberpunk_employee_handbook.txt 和 cyberpunk_code_standards.txt）
10. 等待 2 秒
11. 截图保存到 `test-results/cli/rag-uat-full-01.png`

## 验证
- 知识库"人力资源库"应存在或创建成功
- 文档应上传完成

---

### 测试用例 步骤2: 配置行政助手智能体挂载知识库

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到智能体管理按钮（`[data-testid="agents-button"]` 或包含"智能体"文本的按钮），点击
3. 等待 500 毫秒
4. snapshot 找到包含"UAT行政助手"文本的卡片（`.agent-card` 或 `[data-testid="agent-card"]`），点击
5. 等待 500 毫秒
6. 检查知识库下拉选项中是否有"人力资源库"
7. 如果可用：选择"人力资源库"选项
8. snapshot 找到"保存"按钮，点击
9. 等待 500 毫秒
10. 截图保存到 `test-results/cli/rag-uat-full-02.png`

## 验证
- 知识库选项中应有"人力资源库"
- 保存配置应成功

---

### 测试用例 步骤3: 测试行政助手 RAG 检索

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到智能体选择器（`[data-testid="agent-selector"]` 或 `.agent-selector`），点击
3. snapshot 找到"UAT行政助手"文本，点击
4. 等待 500 毫秒
5. snapshot 找到文本输入框（`input[type="text"][placeholder]`），输入"公司有几天年假？"
6. 按 Enter 键发送
7. 等待 5 秒
8. 检查是否出现 RAG 检索提示（检索指示器）
9. 等待 2 秒
10. 截图保存到 `test-results/cli/rag-uat-full-03.png`

## 验证
- 页面应显示 RAG 检索提示
- 回答应包含"15"或"十五"（年假信息）
- 页面应显示引用来源

---

### 测试用例 步骤4: 测试技术支持不触发RAG

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到智能体选择器（`[data-testid="agent-selector"]` 或 `.agent-selector`），点击
3. snapshot 找到"UAT技术支持"文本，点击
4. 等待 500 毫秒
5. snapshot 找到文本输入框（`input[type="text"][placeholder]`），输入"公司有几天年假？"
6. 按 Enter 键发送
7. 等待 5 秒
8. 截图保存到 `test-results/cli/rag-uat-full-04.png`

## 验证
- 页面不应显示 RAG 检索提示
- 回答应表示不知道或无法回答（包含"不知道"、"无法"或"没有"等文本）

---

### 测试用例 步骤5: 测试代码规范检索

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到智能体选择器（`[data-testid="agent-selector"]` 或 `.agent-selector`），点击
3. snapshot 找到"UAT行政助手"文本，点击
4. 等待 500 毫秒
5. snapshot 找到文本输入框（`input[type="text"][placeholder]`），输入"Python 函数名应该使用什么命名规范？"
6. 按 Enter 键发送
7. 等待 5 秒
8. 截图保存到 `test-results/cli/rag-uat-full-05.png`

## 验证
- 回答应包含命名规范相关信息（包含"下划线"、"snake_case"或"小写"等文本）

---

### 测试用例 前端 RAG 相关 UI 元素验证

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. snapshot 找到智能体选择器（`[data-testid="agent-selector"]` 或 `.agent-selector`），点击
3. snapshot 找到"UAT行政助手"文本，点击
4. 等待 500 毫秒
5. snapshot 找到文本输入框（`input[type="text"][placeholder]`），输入"公司的年假政策是什么？"
6. 按 Enter 键发送
7. 等待 2 秒
8. 检查以下前端 UI 元素：
   - 思考区域（`[class*="thinking"]` 或 `[data-testid="thinking"]`）
   - 检索状态指示器（`[class*="retriev"]` 或 `[data-testid="retrieving"]`）
   - 引用来源显示（`[class*="citation"]` 或 `[data-testid="citation"]`）
   - 消息状态标记（`[class*="status"]` 或 `[data-testid="message-status"]`）
9. 截图保存到 `test-results/cli/rag-uat-full-frontend-ui.png`

## 验证
- 记录各 UI 元素是否存在及数量
- 思考区域、检索指示器、引用来源应至少部分存在

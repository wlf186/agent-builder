# RAG 知识库系统官方验收测试

> 来源: `frontend/tests/rag-uat-official.spec.ts`
> 复杂度: complex
> 测试目标: 验证 AC130-202603170949 功能 - 行政助手触发 RAG 检索、技术支持不触发 RAG、代码规范检索

## 前置条件
- 服务已启动 (localhost:20815)
- 已存在"UAT行政助手"和"UAT技术支持"智能体
- "UAT行政助手"已挂载知识库

---

### 测试用例 RAG-UAT-01: 行政助手应检索知识库并回答年假问题

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 1 秒
3. 在页面上查找"UAT行政助手"文本，如果未找到则向下滚动 500px 后重试
4. snapshot 找到"UAT行政助手"元素，点击
5. 等待 2 秒
6. snapshot 找到文本输入框（`input[type="text"][placeholder]` 或 `input[placeholder*="消息"]`），输入"公司有几天年假？"
7. 按 Enter 键发送
8. 等待 10 秒
9. 截图保存到 `test-results/cli/rag-uat-official-01.png`

## 验证
- 回答应包含"15"或"十五"（年假天数信息）
- 页面上应显示检索/知识库相关提示（包含"检索"、"retriev"或"知识库"等文本）
- 页面上应显示文档来源/引用提示（包含"员工手册"、"来源"或"引用"等文本）

---

### 测试用例 RAG-UAT-02: 技术支持不触发RAG检索

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 1 秒
3. snapshot 找到"UAT技术支持"元素，点击
4. 等待 2 秒
5. snapshot 找到文本输入框（`input[type="text"][placeholder]` 或 `input[placeholder*="消息"]`），输入"公司有几天年假？"
6. 按 Enter 键发送
7. 等待 8 秒
8. 截图保存到 `test-results/cli/rag-uat-official-02.png`

## 验证
- 页面不应显示 RAG 检索提示（不包含"检索"、"retriev"或"Retrieving"等文本）
- 回答应表示不知道或无法回答（包含"不知道"、"无法"、"没有"或"不清楚"等文本）

---

### 测试用例 RAG-UAT-03: 行政助手检索代码规范

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 1 秒
3. snapshot 找到"UAT行政助手"元素，点击
4. 等待 2 秒
5. snapshot 找到文本输入框（`input[type="text"][placeholder]`），输入"Python 函数名应该使用什么命名规范？"
6. 按 Enter 键发送
7. 等待 8 秒
8. 截图保存到 `test-results/cli/rag-uat-official-03.png`

## 验证
- 回答应包含命名规范相关信息（包含"下划线"、"snake_case"、"小写"或"pep"等文本）

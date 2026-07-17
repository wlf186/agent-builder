# RAG 快速验收测试

> 来源: `frontend/tests/rag-quick-uat.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)

---

### 测试用例 1: RAG 验收测试 - 行政助手

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 点击智能体选择器（`.agent-selector`）
4. 在下拉菜单中点击 "UAT行政助手"
5. 等待 1 秒
6. 在聊天输入框（`input[type="text"][placeholder]`）中输入 "公司有几天年假？"
7. 按 Enter 发送
8. 等待 5 秒让 AI 回复

## 验证
- 页面包含 "检索" 或 "retriev"（行政助手应触发 RAG 检索）
- 页面包含 "15" 或 "十五"（年假天数）
- 页面包含 "cyberpunk" 或 "员工手册"（引用来源）
- 截图保存到 test-results/cli/rag-uat-admin.png

---

### 测试用例 2: RAG 验收测试 - 技术支持

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 点击智能体选择器（`.agent-selector`）
4. 在下拉菜单中点击 "UAT技术支持"
5. 等待 1 秒
6. 在聊天输入框（`input[type="text"][placeholder]`）中输入 "公司有几天年假？"
7. 按 Enter 发送
8. 等待 5 秒让 AI 回复

## 验证
- 页面不应包含 "检索" 或 "retriev"（技术支持不应触发 RAG 检索）
- 页面可能包含 "不知道" 或 "无法"（技术支持不知道年假信息）
- 截图保存到 test-results/cli/rag-uat-tech.png

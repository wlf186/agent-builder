# RAG 简单验收测试

> 来源: `frontend/tests/rag-simple-test.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)

---

### 测试用例 1: RAG测试1 - 行政助手

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 等待 1 秒
4. 在智能体卡片列表中找到包含 "UAT行政助手" 文本的卡片，点击它
5. 等待 2 秒
6. 在聊天输入框（`input[type="text"][placeholder]`）中输入 "公司有几天年假？"
7. 按 Enter 发送
8. 等待 8 秒让 AI 回复

## 验证
- 页面内容包含 "15" 或 "十五"（表示年假天数）
- 截图保存到 test-results/cli/rag-simple-admin.png

---

### 测试用例 2: RAG测试2 - 技术支持

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 等待 1 秒
4. 在智能体卡片列表中找到包含 "UAT技术支持" 文本的卡片，点击它
5. 等待 2 秒
6. 在聊天输入框（`input[type="text"][placeholder]`）中输入 "公司有几天年假？"
7. 按 Enter 发送
8. 等待 8 秒让 AI 回复

## 验证
- 页面内容不应包含 "检索" 或 "retriev"（技术支持智能体不应触发 RAG 检索）
- 截图保存到 test-results/cli/rag-simple-tech.png

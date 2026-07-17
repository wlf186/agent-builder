# RAG 知识库对话功能 UAT 测试

> 来源: `frontend/tests/chat-uat-rag.spec.ts`
> 复杂度: complex
> 迭代: AC130-202603170949

## 前置条件
- 服务已启动 (localhost:20815)
- 存在 "UAT行政助手" 智能体（已关联知识库）
- 存在 "UAT技术支持" 智能体（未关联知识库）

---

### 测试用例 1: 关联知识库的智能体对话

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/chat-uat-rag-01-homepage.png`
3. 在 snapshot 中找到 "UAT行政助手" 智能体卡片，点击选择。如果找不到，选择第一个可用智能体
4. 等待 3 秒
5. 截图保存到 `test-results/cli/chat-uat-rag-02-agent-selected.png`
6. 在 snapshot 中找到聊天输入框（尝试以下选择器直到找到可见的: `input[type="text"][placeholder]`, `textarea[placeholder*="输入"]`, `textarea[placeholder*="message"]`, `input[placeholder*="输入"]`, `input[placeholder*="message"]`, `[contenteditable="true"]`。如果都找不到，使用页面中第一个 `input` 或 `textarea`）
7. 截图保存到 `test-results/cli/chat-uat-rag-03-input-found.png`
8. 在输入框中输入 "公司有几天年假？"
9. 截图保存到 `test-results/cli/chat-uat-rag-04-question-filled.png`
10. 按 Enter 发送消息
11. 等待 25 秒（等待回答完成）
12. 截图保存到 `test-results/cli/chat-uat-rag-05-answer-received.png`

## 验证
- 页面应包含 "年假" 相关回答内容
- 页面应包含数字（如 "15"、"10"、"5"）
- 页面应显示检索提示（包含 "检索"、"知识库" 或 "来源" 关键词）

---

### 测试用例 2: 未关联知识库的智能体对话（隔离性测试）

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 在 snapshot 中找到 "UAT技术支持" 智能体卡片，点击选择。如果找不到，选择一个非 "UAT行政助手" 的智能体
3. 等待 3 秒
4. 截图保存到 `test-results/cli/chat-uat-rag-06-tech-agent-selected.png`
5. 在 snapshot 中找到聊天输入框（尝试 `input[type="text"][placeholder]`, `textarea[placeholder*="输入"]`, `textarea[placeholder*="message"]`，如果都找不到使用第一个 `input` 或 `textarea`）
6. 在输入框中输入 "公司有几天年假？"
7. 截图保存到 `test-results/cli/chat-uat-rag-07-tech-question-filled.png`
8. 按 Enter 发送消息
9. 等待 20 秒（等待回答完成）
10. 截图保存到 `test-results/cli/chat-uat-rag-08-tech-answer-received.png`

## 验证
- 未关联知识库的智能体应拒绝回答或表示不知道（包含 "不知道"、"无法"、"不清楚"、"技术支持" 等关键词）
- 页面不应显示检索提示（不应包含 "检索" 或 "知识库" 关键词），验证知识库隔离性

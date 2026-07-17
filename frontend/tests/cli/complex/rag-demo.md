# RAG 知识库系统完整演示

> 来源: `frontend/tests/rag-demo.spec.ts`
> 复杂度: complex
> 演示流程: 创建知识库 -> 上传文档 -> 配置智能体 -> 对话测试

## 前置条件
- 服务已启动 (localhost:20815)
- 存在已配置知识库的智能体（如 "UAT行政助手"、"DEMO" 或 "行政助手"）

---

### 测试用例: 完整演示 - 知识库管理 + 智能体挂载 + RAG 对话

## 步骤
1. 访问 `http://localhost:20815`，等待页面加载完成
2. 等待 2 秒
3. 截图保存到 `test-results/cli/rag-demo-01-homepage.png`

4. 在 snapshot 中找到 "知识库" 菜单项，点击进入知识库管理页面。如果找不到，直接导航到 `http://localhost:20815/knowledge-bases`
5. 等待页面加载完成
6. 截图保存到 `test-results/cli/rag-demo-02-kb-page.png`

7. 检查页面中是否有 "人力资源" 相关知识库
8. 截图保存到 `test-results/cli/rag-demo-03-kb-list.png`

9. 返回主页 `http://localhost:20815`，等待页面加载完成
10. 等待 1.5 秒

11. 在 snapshot 中找到已配置知识库的智能体，按以下优先级尝试: "UAT行政助手" > "DEMO" > "行政助手"。如果都找不到，遍历所有智能体卡片查找包含 "行政"、"DEMO" 或 "UAT" 文本的卡片
12. 等待 2 秒
13. 截图保存到 `test-results/cli/rag-demo-04-agent-selected.png`

14. 检查页面是否显示知识库配置信息（包含 "知识库配置"、"Knowledge Base" 或 "已挂载" 文本）
15. 截图保存到 `test-results/cli/rag-demo-05-kb-config.png`

16. 在 snapshot 中找到聊天输入框（`input[type="text"][placeholder]`），输入 "公司有几天年假？"
17. 按 Enter 发送消息
18. 等待 12 秒（等待 RAG 检索和回答完成）
19. 截图保存到 `test-results/cli/rag-demo-06-rag-response.png`

20. 在聊天输入框中输入 "公司代码命名规范是什么？"
21. 按 Enter 发送消息
22. 等待 10 秒（等待回答完成）
23. 截图保存到 `test-results/cli/rag-demo-07-code-standard.png`

24. 等待 30 秒供查看

## 验证
- 知识库管理页面应可正常访问
- 智能体应已配置知识库
- 第一轮对话:
  - 页面应显示 RAG 检索提示（包含 "检索"、"Retrieving" 或 "知识库" 关键词）
  - 回答应包含年假相关信息（包含 "15"、"十五" 或 "年假" 关键词）
  - 页面应显示引用来源（包含 "来源"、"员工手册" 或 "Source" 关键词）
- 第二轮对话:
  - 回答应包含代码规范信息（包含 "驼峰"、"下划线" 或 "命名" 关键词）

# RAG 知识库系统 - 验收标准完整演示

> 来源: `frontend/tests/rag-acceptance-demo.spec.ts`
> 复杂度: complex
> 测试目标: 完整验收演示 - 创建知识库并上传文档、行政助手挂载知识库、对话测试 RAG 检索、技术支持不触发检索

## 前置条件
- 服务已启动 (localhost:20815)
- 浏览器视口设置为 1920x1080

---

### 测试用例 验收标准完整演示

## 步骤

**验收标准 1: 创建"人力资源库"知识库并上传文档**

1. 设置浏览器视口为 1920x1080
2. 访问 `http://localhost:20815/knowledge-bases`，等待页面加载完成
3. 等待 2 秒，确保页面正确渲染
4. 截图保存到 `test-results/cli/rag-acceptance-01-kb-page.png`
5. 检查页面上是否已存在"人力资源"相关内容
6. 如果已存在，跳过创建步骤；如果不存在，手动创建知识库并上传文档后继续
7. 截图保存到 `test-results/cli/rag-acceptance-02-kb-list.png`

**验收标准 2: 在"行政助手"Agent中挂载知识库**

8. 访问 `http://localhost:20815`，等待页面加载完成
9. 等待 2 秒
10. 截图保存到 `test-results/cli/rag-acceptance-03-homepage.png`
11. 查找并选择"行政助手"或"UAT行政助手"智能体
    - 尝试匹配名称："UAT行政助手"、"行政助手"
    - 如果文本定位失败，遍历卡片元素（`[class*="agent"]`、`[class*="card"]`），查找包含"行政"或"UAT"的卡片
12. 等待 2 秒
13. 截图保存到 `test-results/cli/rag-acceptance-04-agent-selected.png`
14. 检查知识库挂载状态（页面应包含"知识库"和"已挂载"或"人力资源"等文本）
15. 截图保存到 `test-results/cli/rag-acceptance-05-kb-mounted.png`

**验收标准 3: 对话测试 - 行政助手 RAG 检索**

16. snapshot 找到文本输入框（`input[type="text"][placeholder]`），等待可见
17. 输入"公司有几天年假？"
18. 按 Enter 键发送
19. 等待 15 秒
20. 验证以下检查项：
    - 检查1: 页面应包含"正在检索"或"Retrieving"或"知识库检索"（RAG 检索提示）
    - 检查2: 回答应包含"15"或"十五"（准确回答年假天数）
    - 检查3: 页面应包含"来源"或"员工手册"或"Source"或"citation"（引用来源）
    - 检查4: 页面不应包含"错误"或"Error"或"失败"（无报错）
21. 截图保存到 `test-results/cli/rag-acceptance-06-rag-answer.png`

**验收标准 4: 技术支持不触发 RAG 检索**

22. 访问 `http://localhost:20815`，等待页面加载完成
23. 等待 1.5 秒
24. 查找并选择"技术支持"智能体
    - 尝试匹配名称："UAT技术支持"、"技术支持"、"Tech Support"
    - 如果未找到，跳过此验证步骤
25. 如果找到技术支持智能体：
    - 截图保存到 `test-results/cli/rag-acceptance-07-tech-agent.png`
    - snapshot 找到文本输入框（`input[type="text"][placeholder]`），输入"公司有几天年假？"
    - 按 Enter 键发送
    - 等待 10 秒
    - 验证以下检查项：
      - 检查1: 页面不应包含"正在检索"或"Retrieving"或"知识库检索"（未触发 RAG）
      - 检查2: 回答应包含"不知道"或"无法"或"不清楚"或"没有"（表示不知道）
    - 截图保存到 `test-results/cli/rag-acceptance-08-tech-answer.png`

## 验证
- 验收标准1: 知识库创建与文档上传应通过
- 验收标准2: 智能体挂载知识库应通过
- 验收标准3: 行政助手 RAG 检索应显示检索提示并准确回答（包含"15"或"十五"）
- 验收标准4: 技术支持不应触发 RAG 检索，应表示不知道

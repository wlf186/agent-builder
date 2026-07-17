# RAG 知识库系统完整验收测试

> 来源: `frontend/tests/rag-uat-final.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (localhost:20815)
- 存在 "UAT行政助手" 智能体（已挂载知识库）
- 存在 "UAT技术支持" 智能体（未挂载知识库）

## 步骤

### 测试用例 1: 行政助手 RAG 检索

> 验收标准: 行政助手能检索知识库并回答年假相关问题

1. 打开 http://localhost:20815，等待页面加载完成
2. 等待 1 秒
3. 在智能体卡片列表中找到 "UAT行政助手" 卡片，点击
4. 等待 2 秒
5. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，输入 "公司有几天年假？"
6. 按回车发送
7. 等待 8 秒
8. 截图保存到 test-results/cli/rag-uat-final-admin.png

### 验证
- 页面包含检索提示（"检索"、"retriev"、"Retrieving"、"知识库" 等关键词）
- 页面包含答案内容（"15"、"十五"、"15天"）
- 页面可能包含文档引用（"员工手册"、"来源"、"引用" 等关键词）
- 行政助手应能回答年假问题

### 测试用例 2: 技术支持不触发 RAG

> 验收标准: 未挂载知识库的智能体不显示 RAG 检索提示

1. 打开 http://localhost:20815，等待页面加载完成
2. 等待 1 秒
3. 在智能体卡片列表中找到 "UAT技术支持" 卡片，点击
4. 等待 2 秒
5. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，输入 "公司有几天年假？"
6. 按回车发送
7. 等待 8 秒
8. 截图保存到 test-results/cli/rag-uat-final-tech.png

### 验证
- 页面不包含检索提示（"检索"、"retriev"、"Retrieving" 不应出现）
- 页面可能包含 "不知道"、"无法"、"没有"、"不清楚" 等回答
- 技术支持不应显示 RAG 检索提示

### 测试用例 3: 代码规范检索

> 验收标准: 行政助手能检索到代码规范相关信息

1. 打开 http://localhost:20815，等待页面加载完成
2. 等待 1 秒
3. 在智能体卡片列表中找到 "UAT行政助手" 卡片，点击
4. 等待 2 秒
5. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，输入 "Python 函数名应该使用什么命名规范？"
6. 按回车发送
7. 等待 8 秒
8. 截图保存到 test-results/cli/rag-uat-final-code.png

### 验证
- 页面包含命名规范信息（"下划线"、"snake_case"、"小写"、"pep" 等关键词）
- 应能检索到代码规范信息

### 测试用例 4: 前端 UI 元素验证

> 验收标准: RAG 相关 UI 元素正确渲染

1. 打开 http://localhost:20815，等待页面加载完成
2. 等待 1 秒
3. 在智能体卡片列表中找到 "UAT行政助手" 卡片，点击
4. 等待 2 秒
5. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，输入 "公司的年假政策是什么？"
6. 按回车发送
7. 等待 5 秒
8. 截图保存到 test-results/cli/rag-uat-final-ui-check.png

### 验证
- 检查页面中是否包含以下 UI 元素相关文本：
  - 思考区域: "thinking"、"thought"、"思考"
  - 检索状态: "retrieving"、"retrieval"、"检索"
  - 引用来源: "citation"、"source"、"引用"、"来源"
  - 知识库提示: "knowledge"、"知识库"、"KB"
  - 状态标记: "status"、"状态"

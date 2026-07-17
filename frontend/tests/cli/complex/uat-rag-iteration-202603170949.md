# RAG 知识库管理功能 UAT

> 来源: `frontend/tests/uat-rag-iteration-202603170949.spec.ts`
> 复杂度: complex
> 迭代: AC130-202603170949

## 前置条件
- 服务已启动 (localhost:20815)
- 后端 API `/api/knowledge-bases` 可用
- 测试文档 `Cyberpunk公司2026员工手册.txt` 已准备（包含公司简介、考勤制度、年假制度等内容）

## 步骤

### 测试用例 1: 知识库创建功能

> 验收标准: 能成功创建知识库并在列表中显示

1. 打开 http://localhost:20815/knowledge-bases，等待页面加载完成
2. 截图保存到 test-results/cli/rag-iteration-202603170949-01-list.png
3. snapshot 找到 "创建知识库" 按钮，点击
4. 等待 0.5 秒，截图保存到 test-results/cli/rag-iteration-202603170949-02-dialog.png
5. snapshot 找到名称输入框（placeholder 包含 "例如：人力资源库"），输入 "人力资源库"
6. snapshot 找到描述输入框（textarea，placeholder 包含 "描述知识库包含的内容"），输入 "包含员工手册、考勤制度、报销流程等人力资源相关文档"
7. 截图保存到 test-results/cli/rag-iteration-202603170949-03-form-filled.png
8. snapshot 找到 "创建" 按钮（非 disabled 状态），点击
9. 等待 2 秒，截图保存到 test-results/cli/rag-iteration-202603170949-04-created.png

### 验证
- "人力资源库" 出现在知识库列表中（h3 标签包含 "人力资源库"）

### 测试用例 2: 文档上传功能

> 验收标准: 文档上传成功并在列表中显示

1. 打开 http://localhost:20815/knowledge-bases，等待页面加载完成
2. snapshot 找到 "人力资源库" 知识库卡片（h3 标签），点击进入详情页
3. 等待页面加载完成
4. 截图保存到 test-results/cli/rag-iteration-202603170949-05-detail.png
5. snapshot 找到文件上传控件 `input[type="file"]`
6. 上传测试文档（Cyberpunk公司2026员工手册.txt）
7. 等待 3 秒，截图保存到 test-results/cli/rag-iteration-202603170949-06-uploading.png
8. 等待 10 秒（文档处理），截图保存到 test-results/cli/rag-iteration-202603170949-07-processed.png

### 验证
- 文档 "员工手册" 出现在文档列表中

### 测试用例 3: 智能体关联知识库

> 验收标准: 创建智能体并成功关联知识库

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/rag-iteration-202603170949-08-home.png
3. snapshot 找到 "创建智能体" 按钮，点击
4. 等待 1 秒
5. snapshot 找到名称输入框（input[name="name"] 或 placeholder 包含 "名称"），输入 "行政助手"
6. snapshot 找到人设输入框（textarea[name="system_prompt"] 或 placeholder 包含 "人设"），输入 "你是公司的行政助手，负责回答人力资源相关问题。"
7. 截图保存到 test-results/cli/rag-iteration-202603170949-09-create-agent.png
8. 等待 1 秒
9. 查找并勾选知识库复选框（如有）
10. 截图保存到 test-results/cli/rag-iteration-202603170949-10-kb-checkbox.png
11. snapshot 找到 "保存" 按钮，点击
12. 等待 2 秒，截图保存到 test-results/cli/rag-iteration-202603170949-11-saved.png

### 验证
- 智能体创建并配置成功
- 知识库复选框已勾选（如果存在）

### 测试用例 4: 对话检索测试

> 验收标准: 智能体能检索知识库并返回基于文档的准确回答

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "行政助手" 智能体卡片（h3 标签），点击
3. 等待 2 秒
4. 截图保存到 test-results/cli/rag-iteration-202603170949-12-agent-selected.png
5. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，验证可见
6. 在输入框中输入 "公司有几天年假？"
7. 等待 0.5 秒，截图保存到 test-results/cli/rag-iteration-202603170949-13-question.png
8. 按回车发送
9. 截图保存到 test-results/cli/rag-iteration-202603170949-14-sent.png
10. 等待 30 秒
11. 截图保存到 test-results/cli/rag-iteration-202603170949-15-response.png

### 验证
- 回答包含 "年假"、"15天"、"15" 或 "休假" 等关键词
- 可能显示检索提示（"检索"、"知识库"、"来源"、"retriev"）

### 测试用例 5: 隔离性测试

> 验收标准: 未关联知识库的智能体不触发检索

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/rag-iteration-202603170949-16-isolation-home.png
3. snapshot 找到 "创建智能体" 按钮，点击
4. 等待 1 秒
5. snapshot 找到名称输入框，输入 "技术支持"
6. snapshot 找到人设输入框，输入 "你是公司的技术支持，负责解决技术问题，不负责人力资源相关问题。"
7. 截图保存到 test-results/cli/rag-iteration-202603170949-17-create-tech.png
8. snapshot 找到 "保存" 按钮，点击（不勾选知识库）
9. 等待 2 秒
10. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，验证可见
11. 在输入框中输入 "公司有几天年假？"
12. 等待 0.5 秒，截图保存到 test-results/cli/rag-iteration-202603170949-18-isolation-question.png
13. 按回车发送
14. 等待 15 秒
15. 截图保存到 test-results/cli/rag-iteration-202603170949-19-isolation-response.png

### 验证
- 智能体回答包含 "不知道"、"无法回答"、"不清楚" 或 "技术支持" 等拒绝回答的内容
- 页面不包含 "检索"、"知识库"、"员工手册" 等检索提示（知识库隔离正常）

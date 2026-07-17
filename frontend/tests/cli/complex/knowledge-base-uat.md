# 知识库（RAG）管理系统 UAT

> 来源: `frontend/tests/knowledge-base-uat.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (localhost:20815 / localhost:20881)
- 后端 API `/api/knowledge-bases` 可用
- 截图目录 `test-results/cli/` 已创建

## 步骤

### 测试用例 1: 后端 API 健康检查

1. 通过 API 检查知识库接口可用性

### 验证

```run-code
const resp = await fetch('http://localhost:20881/api/knowledge-bases');
console.assert(resp.ok, 'Knowledge bases API should be healthy');
const data = await resp.json();
console.assert(data.knowledge_bases !== undefined, 'Response should have knowledge_bases field');
console.log('API health check PASS');
```

### 测试用例 2: 创建知识库

1. 打开 http://localhost:20815/knowledge-bases，等待页面加载完成
2. 截图保存到 test-results/cli/knowledge-base-uat-01-list.png
3. snapshot 找到 "创建知识库" 按钮，点击
4. 等待 1 秒，截图保存到 test-results/cli/knowledge-base-uat-02-dialog.png
5. 在名称输入框中填写 "人力资源库"
6. 在描述输入框中填写 "包含员工手册、考勤制度、报销流程等人力资源相关文档"
7. 截图保存到 test-results/cli/knowledge-base-uat-03-form-filled.png
8. snapshot 找到对话框内的 "创建" 按钮，点击提交
9. 等待 3 秒，截图保存到 test-results/cli/knowledge-base-uat-04-created.png

### 验证
- "人力资源库" 知识库卡片出现在列表中

### 测试用例 3: 上传文档到知识库

1. 通过 API 获取 "人力资源库" 的 kb_id
2. 打开 http://localhost:20815/knowledge-bases/{kb_id}，等待页面加载完成
3. 截图保存到 test-results/cli/knowledge-base-uat-05-detail.png
4. snapshot 找到 "上传文档" 按钮，点击
5. 等待 1 秒，截图保存到 test-results/cli/knowledge-base-uat-06-upload-dialog.png
6. 使用文件选择器上传测试文档（Markdown 格式，内容包含员工手册信息：公司简介、考勤制度、年假制度等）
7. 等待 2 秒，截图保存到 test-results/cli/knowledge-base-uat-07-file-selected.png
8. snapshot 找到 "上传" 按钮，点击
9. 等待 5 秒，截图保存到 test-results/cli/knowledge-base-uat-08-uploaded.png

### 验证
- 文档上传成功，文档列表中显示上传的文件

### 测试用例 4: Agent 配置知识库挂载

1. 打开 http://localhost:20815，等待页面加载完成
2. 截图保存到 test-results/cli/knowledge-base-uat-09-main.png
3. 检查是否存在 "行政助手" 智能体
4. 如果不存在：
   - snapshot 找到 "创建智能体" 按钮，点击
   - 等待 1 秒，截图保存到 test-results/cli/knowledge-base-uat-10-create-dialog.png
   - 在名称输入框中填写 "行政助手"
   - 在人设/描述输入框中填写 "你是一个行政助手，可以回答关于公司制度的问题。"
   - 截图保存到 test-results/cli/knowledge-base-uat-11-form-filled.png
   - snapshot 找到对话框内的 "创建" 按钮，点击
   - 等待 2 秒
5. snapshot 找到 "行政助手" 智能体卡片，点击进入配置页
6. 等待 1 秒，截图保存到 test-results/cli/knowledge-base-uat-12-config.png
7. snapshot 找到 "知识库" 配置区域，点击展开
8. 等待 0.5 秒，截图保存到 test-results/cli/knowledge-base-uat-13-kb-config.png

### 验证
- 知识库配置区域可见，可展开

### 测试用例 5: RAG 检索对话测试

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "行政助手" 智能体卡片，点击
3. 等待 1 秒
4. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，输入 "公司有几天年假？"
5. 按回车发送
6. 截图保存到 test-results/cli/knowledge-base-uat-14-question-sent.png
7. 等待 10 秒
8. 截图保存到 test-results/cli/knowledge-base-uat-15-response.png

### 验证
- 响应内容包含 "5天"、"年假"、"10天"、"15天" 或 "不知道" 等关键词

### 测试用例 6: 隔离验证（未挂载 Agent）

1. 打开 http://localhost:20815，等待页面加载完成
2. snapshot 找到 "test3" 或其他未挂载知识库的智能体卡片，点击
3. 等待 1 秒
4. snapshot 找到调试对话输入框 `input[type="text"][placeholder]`，输入 "公司有几天年假？"
5. 按回车发送
6. 截图保存到 test-results/cli/knowledge-base-uat-16-isolated-question.png
7. 等待 10 秒
8. 截图保存到 test-results/cli/knowledge-base-uat-17-isolated-response.png

### 验证
- 未挂载知识库的智能体不应触发 RAG 检索（无检索状态显示）

### 测试用例 7: 清理测试数据

```run-code
# 删除测试创建的 "人力资源库"
const resp = await fetch('http://localhost:20881/api/knowledge-bases');
const data = await resp.json();
for (const kb of data.knowledge_bases || []) {
  if (kb.name === '人力资源库') {
    await fetch(`http://localhost:20881/api/knowledge-bases/${kb.kb_id}`, { method: 'DELETE' });
    console.log(`Deleted KB: ${kb.kb_id}`);
  }
}
```

### 验证
- "人力资源库" 已从知识库列表中删除

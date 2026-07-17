# UAT 测试 - 知识库 (RAG) 管理系统

> 来源: `frontend/tests/uat-rag-knowledge-base.spec.ts`
> 复杂度: complex

## 前置条件
- 服务已启动 (前端 localhost:20815, 后端 localhost:20881)
- 测试智能体 `UAT-RAG测试智能体` (测试过程中按需创建)

---

### TC-01: 知识库基础管理

## 步骤

1. 导航到 `http://localhost:20815/knowledge-bases`，等待页面加载完成
2. 截图保存到 `test-results/cli/uat-rag-kb/TC-01-01-知识库列表页.png`
3. 在 snapshot 中找到 "新建知识库" 或 "Create" 按钮，点击
4. 等待 1 秒
5. 截图保存到 `test-results/cli/uat-rag-kb/TC-01-02-点击新建按钮.png`
6. 找到名称输入框，输入 "UAT测试知识库"
7. 找到描述输入框 (textarea)，输入 "UAT自动化测试创建的知识库"
8. 截图保存到 `test-results/cli/uat-rag-kb/TC-01-03-填写表单.png`
9. 在 snapshot 中找到 "创建" / "提交" / "保存" 按钮，点击
10. 等待 3 秒
11. 截图保存到 `test-results/cli/uat-rag-kb/TC-01-04-创建成功.png`

## 验证
- 知识库列表页面正常加载
- 新建按钮可见
- 表单可以填写
- 创建成功后页面显示 "UAT测试知识库" 卡片

---

### TC-02: 文档上传和处理

## 步骤

1. 创建测试文档 (员工手册内容)，保存为 txt 文件
2. 导航到 `http://localhost:20815/knowledge-bases`，等待页面加载完成
3. 在 snapshot 中找到 "UAT测试知识库" 文本，点击进入详情页
4. 等待 2 秒
5. 截图保存到 `test-results/cli/uat-rag-kb/TC-02-01-知识库详情页.png`
6. 找到文件上传 input 元素 (`input[type="file"]`)，上传测试文档
   - 如果 `input[type="file"]` 不可见，先点击 "上传" 或 "添加文档" 按钮
7. 等待 3 秒
8. 截图保存到 `test-results/cli/uat-rag-kb/TC-02-02-上传文档.png`
9. 等待 15 秒 (文档处理时间)
10. 截图保存到 `test-results/cli/uat-rag-kb/TC-02-03-文档状态.png`

## 验证
- 知识库详情页正常打开
- 文档上传成功
- 页面显示文档处理状态 (ready / 完成 / processed / 员工手册)

---

### TC-03: 检索测试

## 步骤

1. 导航到 `http://localhost:20815/knowledge-bases`，等待页面加载完成
2. 在 snapshot 中找到 "UAT测试知识库" 文本，点击进入详情页
3. 等待 2 秒
4. 找到检索输入框 (搜索/检索 placeholder 的 input 或 textarea)
5. 输入 "公司有几天年假？"
6. 截图保存到 `test-results/cli/uat-rag-kb/TC-03-01-输入检索查询.png`
7. 按回车提交检索
8. 等待 5 秒
9. 截图保存到 `test-results/cli/uat-rag-kb/TC-03-02-检索结果.png`

## 验证
- 检索结果中包含 "年假"、"5天"、"15天"、"score" 或 "相似度" 等关键词
- 检索功能正常返回相关结果

---

### TC-04: 智能体挂载知识库

## 步骤

1. 导航到 `http://localhost:20815`，等待页面加载完成
2. 截图保存到 `test-results/cli/uat-rag-kb/TC-04-01-主页.png`
3. 在 snapshot 中查找 "UAT-RAG测试智能体"
   - 如果存在: 点击该智能体
   - 如果不存在:
     1. 点击 "新建智能体" 按钮
     2. 输入名称 "UAT-RAG测试智能体"
     3. 输入描述 "用于UAT测试知识库功能的智能体"
     4. 点击 "保存" / "创建"
     5. 等待 3 秒
4. 截图保存到 `test-results/cli/uat-rag-kb/TC-04-02-智能体配置页.png`
5. 在 snapshot 中找到 "知识库" 选项卡，点击
6. 等待 1 秒
7. 截图保存到 `test-results/cli/uat-rag-kb/TC-04-03-知识库选项卡.png`
8. 勾选知识库 checkbox 或从下拉框选择 "UAT测试知识库"
9. 截图保存到 `test-results/cli/uat-rag-kb/TC-04-04-选择知识库.png`
10. 点击 "保存" / "保存配置" 按钮
11. 等待 2 秒
12. 截图保存到 `test-results/cli/uat-rag-kb/TC-04-05-配置完成.png`

## 验证
- 智能体配置页正常显示
- 知识库选项卡可展开
- 知识库可选择并保存

---

### TC-05: 知识库问答 (核心验收)

## 步骤

1. 导航到 `http://localhost:20815`，等待页面加载完成
2. 在 snapshot 中找到 "UAT-RAG测试智能体" 或第一个可用智能体，点击
3. 等待 2 秒
4. 找到调试对话输入框 (`input[type="text"][placeholder]`)
5. 截图保存到 `test-results/cli/uat-rag-kb/TC-05-01-对话页面.png`
6. 输入 "公司有几天年假？"
7. 截图保存到 `test-results/cli/uat-rag-kb/TC-05-02-输入问题.png`
8. 按回车发送
9. 等待 20 秒
10. 截图保存到 `test-results/cli/uat-rag-kb/TC-05-03-AI回复.png`
11. 截图保存到 `test-results/cli/uat-rag-kb/TC-05-04-最终验收.png`

## 验证
- AI 回复基于知识库内容 (包含 "5天"、"10天"、"15天"、"年假"、"工作满" 等关键词)
- 页面显示检索过程 (包含 "检索"、"知识库"、"retriev"、"正在查询")
- 回复包含引用来源 (包含 "来源"、"引用"、"citation"、"文档")
- 以上任一项通过即视为测试通过

---

### TC-06: 未挂载知识库的智能体测试

## 步骤

1. 导航到 `http://localhost:20815`，等待页面加载完成
2. 选择一个没有挂载知识库的智能体 (如 test3、main-agent、DEMO)
   - 如果找不到指定名称，选择第一个不包含 "UAT-RAG" 的智能体
3. 等待 2 秒
4. 找到调试对话输入框
5. 输入 "公司有几天年假？"
6. 按回车发送
7. 等待 15 秒
8. 截图保存到 `test-results/cli/uat-rag-kb/TC-06-01-未挂载知识库回复.png`

## 验证
- 未挂载知识库的智能体不应触发检索过程
- 页面不显示 "检索"、"知识库"、"员工手册" 等关键词

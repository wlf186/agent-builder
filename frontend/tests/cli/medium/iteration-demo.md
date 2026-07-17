# 知识库功能演示

> 来源: `frontend/tests/iteration-demo.spec.ts`
> 复杂度: medium

## 前置条件
- 服务已启动 (localhost:20815)
- 知识库（如人力资源库 kb_7116e7ed）已创建
- UAT行政助手和 UAT技术支持智能体已配置

---

### 测试用例 1: 知识库列表页

## 步骤
1. 打开 http://localhost:20815/knowledge-bases
2. 等待页面加载完成
3. 等待 2 秒

## 验证
- 页面包含 "知识库" 文字
- 截图保存到 test-results/cli/iteration-demo-01-kb-list.png

---

### 测试用例 2: 知识库详情页

## 步骤
1. 打开 http://localhost:20815/knowledge-bases/kb_7116e7ed
2. 等待页面加载完成
3. 等待 2 秒

## 验证
- 页面包含 "人力资源库" 文字
- 截图保存到 test-results/cli/iteration-demo-02-kb-detail.png

---

### 测试用例 3: 智能体知识库配置

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 找到包含 "UAT行政助手" 的 h3 标题，点击它
4. 等待 2 秒

## 验证
- 页面包含 "UAT行政助手" 文字
- 截图保存到 test-results/cli/iteration-demo-03-agent-config.png

---

### 测试用例 4: 对话检索测试

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 找到包含 "UAT行政助手" 的 h3 标题，点击它
4. 等待 2 秒
5. 在聊天输入框（`input[type="text"][placeholder]`）中输入 "公司有几天年假？"
6. 按 Enter 发送
7. 等待 30 秒

## 验证
- 页面正文内容长度大于 50 字符（AI 有实质性回复）
- 截图保存到 test-results/cli/iteration-demo-04-chat-response.png

---

### 测试用例 5: 隔离性测试

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 找到包含 "UAT技术支持" 的 h3 标题，点击它
4. 等待 2 秒
5. 在聊天输入框（`input[type="text"][placeholder]`）中输入 "公司有几天年假？"
6. 按 Enter 发送
7. 等待 30 秒

## 验证
- 页面正文内容长度大于 50 字符
- UAT技术支持不应调用行政助手的知识库（隔离性）
- 截图保存到 test-results/cli/iteration-demo-05-isolation-test.png

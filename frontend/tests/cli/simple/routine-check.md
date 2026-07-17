# 例行功能验证

> 来源: `frontend/tests/routine-check.spec.ts`
> 复杂度: simple

## 前置条件
- 服务已启动 (localhost:20815)

## 测试用例

### 测试 1: 主页加载正常

1. 打开 http://localhost:20815
2. 等待页面加载完成
3. snapshot 查看页面是否显示 h1 或 h2 标题

**验证**: 页面中可见标题元素

### 测试 2: 智能体列表显示

1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 等待 2 秒
4. snapshot 查看是否有 h3 元素（智能体卡片标题）

**验证**: 至少有一个 h3 元素可见

### 测试 3: 智能体选择和对话

1. 打开 http://localhost:20815
2. 等待页面加载完成
3. snapshot 找到 "test3" 标题，点击它
4. 等待 2 秒
5. snapshot 找到聊天输入框（input[type="text"][placeholder]），输入 "你好"
6. 按 Enter 发送
7. 等待 5 秒让助手回复
8. snapshot 查看回复内容

**验证**: 页面内容长度超过 100 字符（有实质性回复）

### 测试 4: 知识库页面可访问

1. 打开 http://localhost:20815/knowledge-bases
2. 等待页面加载完成
3. 等待 2 秒
4. snapshot 查看页面内容

**验证**: 页面中包含"知识库"文字

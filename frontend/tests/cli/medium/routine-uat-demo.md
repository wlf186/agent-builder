# 例行 Demo 演示

> 来源: `frontend/tests/routine-uat-demo.spec.ts`
> 复杂度: medium
> 模式: headed（playwright-cli 默认 headed，兼容）

## 前置条件
- 服务已启动 (localhost:20815)
- PDF 测试文件位于: `resources/Thinking Fast and Slow (Daniel Kahneman) (Z-Library).pdf`
- MCP 工具: calculator, cold-jokes, coingecko 已配置
- 技能: ab-docx, ab-pdf 已配置

## 步骤

### [1/8] 访问主页
1. 打开 http://localhost:20815
2. 等待页面加载完成

### [2/8] 清理已存在的 DEMO 智能体
3. 在页面上查找标题为 "DEMO" 的智能体卡片（h3 标签，精确匹配 "DEMO"）
4. 如果存在 DEMO 智能体，对每个执行以下操作:
   - 点击该智能体卡片
   - 等待 0.5 秒
   - 查找并点击 "删除" 或 "Delete" 按钮
   - 等待 0.5 秒
   - 查找并点击 "确认" 或 "确定" 或 "OK" 按钮确认删除
   - 等待 1 秒
5. 刷新页面并等待页面加载完成

### [3/8] 创建智能体
6. 点击 "新建"、"创建" 或 "+" 按钮
7. 等待 1 秒让模态框动画完成
8. 在模态框的名称输入框（placeholder 包含 "例如：代码助手" 或 "e.g., Code Assistant"）中输入 "DEMO"
9. 等待 0.3 秒
10. 点击 "创建" 按钮

#### 等待初始化
11. 等待最多 30 秒直到出现 "环境就绪" 或 "已就绪" 文字
12. 如果页面不再显示 "初始化中"、"正在创建"、"loading" 等文字且已等待超过 5 秒，可认为初始化完成
13. 等待 1 秒

### [3/8] 配置工具
14. 向下滚动到页面位置 500
15. 等待 0.5 秒
16. 依次勾选以下工具（找到包含对应文字的 label 或 checkbox 元素并点击）:
    - TE47
    - calculator
    - cold-jokes
    - coingecko
    - ab-docx
    - ab-pdf
17. 每个工具之间等待 0.2 秒

### [4/8] 保存配置
18. 点击 "保存" 或 "Save" 按钮
19. 等待 0.5 秒
20. 确认聊天输入框（`input[type="text"][placeholder]`）可见

### [5/8] 第 1 轮对话 - Calculator 工具
21. 在聊天输入框中输入 "32748+392/2+1是多少"
22. 等待 0.3 秒
23. 按 Enter 发送
24. 等待最多 60 秒，直到页面包含 "calcul" 关键字、不显示 "思考中" 或 "thinking"、且页面内容长度超过 3000 字符

### [6/8] 第 2 轮对话 - Cold Jokes 工具
25. 在聊天输入框中输入 "讲2个冷笑话"
26. 等待 0.3 秒
27. 按 Enter 发送
28. 等待最多 60 秒，直到页面包含 "cold" 关键字、不显示 "思考中" 或 "thinking"、且页面内容长度超过 3000 字符

### [7/8] 第 3 轮对话 - CoinGecko 工具
29. 在聊天输入框中输入 "ETH的最新价格"
30. 等待 0.3 秒
31. 按 Enter 发送
32. 等待最多 60 秒，直到页面包含 "coin" 关键字、不显示 "思考中" 或 "thinking"、且页面内容长度超过 3000 字符

### [8/8] 第 4 轮对话 - PDF 技能
33. 通过文件上传输入框（`input[type="file"]`）上传 PDF 文件: `resources/Thinking Fast and Slow (Daniel Kahneman) (Z-Library).pdf`
34. 等待 2 秒确认上传完成
35. 在聊天输入框中输入 "提取这篇文档的前200字"
36. 等待 0.3 秒
37. 按 Enter 发送
38. 等待最多 60 秒，直到页面包含 "ab-pdf" 或 "pdf" 关键字、不显示 "思考中" 或 "thinking"、且页面内容长度超过 3000 字符

### 完成后
39. 截图保存到 test-results/cli/routine-uat-demo-success.png

## 验证
- DEMO 智能体创建成功且环境初始化完成
- 4 轮对话全部成功完成:
  - 第 1 轮: Calculator 工具被调用，返回计算结果
  - 第 2 轮: Cold Jokes 工具被调用，返回冷笑话
  - 第 3 轮: CoinGecko 工具被调用，返回 ETH 价格信息
  - 第 4 轮: PDF 技能被调用，返回文档提取内容
- 如果任一轮对话超时（60 秒内未满足完成条件），需截图记录并报告失败

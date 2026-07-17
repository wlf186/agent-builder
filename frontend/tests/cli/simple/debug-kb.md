# 知识库页面调试

> 来源: `frontend/tests/debug-kb.spec.ts`
> 复杂度: simple

## 前置条件
- 服务已启动 (localhost:20815)

## 步骤
1. 打开 http://localhost:20815/knowledge-bases
2. 等待页面加载完成
3. 截图保存到 test-results/cli/debug-kb-page.png
4. snapshot 查看所有按钮元素，列出每个按钮的文本内容
5. 检查是否存在"创建知识库"按钮

## 验证
- 列出所有按钮及其文本
- 报告"创建知识库"按钮是否存在

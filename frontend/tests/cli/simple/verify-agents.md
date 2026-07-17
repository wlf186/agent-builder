# 智能体列表验证

> 来源: `frontend/tests/verify-agents.spec.ts`
> 复杂度: simple

## 前置条件
- 服务已启动 (localhost:20815)

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. 等待 3 秒让智能体卡片加载
4. snapshot 获取页面所有 h3 元素文本
5. 截图保存到 test-results/cli/verify-agents.png

## 验证
- 页面中应包含以下智能体名称: test3, test-model-check, finance-sub, main-agent
- 页面中不应包含: test001, test-iteration-2603111255, test007, test2
- 页面中应有"智能体人设"标题

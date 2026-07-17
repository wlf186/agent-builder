# 健康检查

> 来源: `frontend/tests/simple-uat.spec.ts`
> 复杂度: simple

## 前置条件
- 服务已启动 (localhost:20815)

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成（networkidle）
3. 截图保存到 test-results/cli/simple-uat.png

## 验证
- 页面 body 可见
- 无错误提示

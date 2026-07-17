# test001 例行验证 - 3轮对话

> 来源: `frontend/tests/test001-routine.spec.ts`
> 复杂度: simple

## 前置条件
- 服务已启动 (localhost:20815)

## 步骤
1. 打开 http://localhost:20815
2. 等待页面加载完成
3. snapshot 找到 "test001" 文字，点击它
4. 等待 2 秒
5. snapshot 找到输入框，输入 "现在是几月几号几点几分"
6. 按 Enter 发送
7. 等待 10 秒让助手回复
8. snapshot 找到输入框，输入 "99/33是多少"
9. 按 Enter 发送
10. 等待 10 秒让助手回复
11. snapshot 找到输入框，输入 "计算结果再加2.5是多少"
12. 按 Enter 发送
13. 等待 10 秒让助手回复
14. 截图保存到 test-results/cli/test001-routine-result.png

## 验证
- 3 轮对话均有回复
- 截图成功保存

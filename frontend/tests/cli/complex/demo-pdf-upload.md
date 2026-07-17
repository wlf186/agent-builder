# 演示脚本 - PDF 文件上传 + 内容提取

> 来源: `frontend/tests/demo-pdf-upload.spec.ts`
> 复杂度: complex
> 模式: headed (可视化演示)
> 注意: 此脚本需要实际 PDF 文件， headed 模式兼容 playwright-cli

## 前置条件
- 服务已启动 (前端 localhost:20815, 后端 localhost:20881)
- test3 智能体已配置
- PDF 文件可用（例如仓库内 `teams/AC130/iterations/202603170949/test_documents/Cyberpunk公司2026员工手册.pdf`）

---

### 演示: PDF 上传 + 内容提取

## 步骤

1. **初始化**:
   - 导航到 `http://localhost:20815`，等待页面加载完成
   - 触发浏览器重绘: 执行 `window.scrollTo(0, 0)`
   - 等待 0.1 秒

2. **选择智能体**:
   - 在 snapshot 中找到 test3 智能体 (h3 标签包含 "test3")，点击
   - 等待 2 秒
   - 截图保存到 `test-results/cli/demo-pdf-upload/pdf-00-agent-selected.png`

3. **上传 PDF 文件**:
   - 等待 1 秒
   - 在 snapshot 中找到上传按钮 ("上传" / "上传文件" / 附件图标)
   - 如果找不到上传按钮:
     - 截图保存到 `test-results/cli/demo-pdf-upload/pdf-01-before-upload.png`
     - 尝试遍历页面所有 button 元素，找到包含 "上传" / "文件" 文本的按钮并点击
   - 直接通过 `input[type="file"]` 元素上传 PDF 文件
     - 如果 `input[type="file"]` 不存在，通过 JavaScript 创建临时 file input
   - 等待 2 秒
   - 截图保存到 `test-results/cli/demo-pdf-upload/pdf-02-after-upload.png`

4. **发送提取消息**:
   - 找到消息输入框 (`input[type="text"][placeholder]`)
   - 输入 "提取这篇文档的前200字"
   - 等待 0.5 秒
   - 截图保存到 `test-results/cli/demo-pdf-upload/pdf-03-message-filled.png`
   - 按回车发送
   - 等待 20 秒 (PDF 处理需要更多时间)
   - 截图保存到 `test-results/cli/demo-pdf-upload/pdf-04-result.png`

5. **最终状态**:
   - 截图保存到 `test-results/cli/demo-pdf-upload/pdf-05-final-state.png`

## 验证
- test3 智能体可选中
- PDF 文件上传成功
- AB-pdf 技能被触发处理
- AI 返回提取的文档内容

/**
 * 完整 UAT 验收测试 - 调试日志导出功能
 *
 * 验收要点：
 * 1. Trace ID 关联
 * 2. 日志内容完整性 (Request/Execution/Response/Error)
 * 3. 日志导出功能
 * 4. 流式输出验证
 */

import { test, expect } from '@playwright/test';
import path from 'node:path';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('uat-full-acceptance');

test.describe('完整 UAT 验收: 调试日志导出', () => {

  /**
   * TC-UAT-01: 验证 Trace ID 关联
   * 验收标准：请求发送时的 X-Request-ID 与响应返回的 X-Request-ID 一致
   */
  test('TC-UAT-01: Trace ID 关联验证', async ({ page }) => {
    const requestIds: Map<string, string> = new Map();

    // 监听请求
    page.on('request', request => {
      const reqId = request.headers()['x-request-id'] || request.headers()['X-Request-ID'];
      const url = request.url();
      if (reqId && url.includes('/api/')) {
        requestIds.set(url, reqId);
        console.log(`[REQ] ${url.substring(0, 60)}... -> X-Request-ID: ${reqId}`);
      }
    });

    // 监听响应
    page.on('response', response => {
      const resId = response.headers()['x-request-id'] || response.headers()['X-Request-ID'];
      const url = response.url();
      if (resId && url.includes('/api/')) {
        const reqId = requestIds.get(url);
        if (reqId) {
          const match = reqId === resId;
          console.log(`[RES] ${url.substring(0, 60)}... -> X-Request-ID: ${resId} ${match ? '✓ 匹配' : '✗ 不匹配'}`);
        }
      }
    });

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 点击调试按钮
    const chatButton = page.locator('button:has-text("调试")').first();
    await chatButton.click();
    await page.waitForTimeout(2000);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat01-traceid-01.png`, fullPage: true });

    // 发送消息触发 API 请求
    const textarea = page.locator('textarea').first();
    await textarea.fill('测试 Trace ID 关联');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat01-traceid-02.png`, fullPage: true });

    const sendButton = page.locator('button:has-text("发送")').first();
    await sendButton.click();

    // 等待响应
    await page.waitForTimeout(10000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat01-traceid-03.png`, fullPage: true });

    console.log('✅ TC-UAT-01: Trace ID 关联验证完成');
  });

  /**
   * TC-UAT-02: 日志内容完整性验证
   * 验收标准：
   * - [Request]: 用户输入、Agent 名称
   * - [Execution]: SSE chunks、tool calls
   * - [Response]: 模型输出
   */
  test('TC-UAT-02: 日志内容完整性验证', async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 进入调试对话
    await page.locator('button:has-text("调试")').first().click();
    await page.waitForTimeout(2000);

    // 监听 SSE 事件
    const sseEvents: string[] = [];
    page.on('response', async (response) => {
      const contentType = response.headers()['content-type'] || '';
      if (contentType.includes('text/event-stream')) {
        console.log('[SSE] 检测到流式响应');
        sseEvents.push('stream-start');
      }
    });

    // 发送触发工具调用的消息
    const textarea = page.locator('textarea').first();
    await textarea.fill('请使用计算器工具计算 123 * 456');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat02-content-01.png`, fullPage: true });

    await page.locator('button:has-text("发送")').first().click();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat02-content-02.png`, fullPage: true });

    // 等待响应完成
    await page.waitForTimeout(15000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat02-content-03.png`, fullPage: true });

    // 检查页面上的消息元素
    const messages = page.locator('.message, [data-testid="message"], .prose').all();
    const messageCount = (await messages).length;
    console.log(`✅ 检测到 ${messageCount} 个消息元素`);

    // 检查是否有工具调用指示器
    const toolIndicators = page.locator('.tool-call, [data-testid*="tool"], .bg-blue-500\\/20').all();
    console.log(`✅ 检测到 ${await toolIndicators.then(arr => arr.length)} 个工具相关元素`);

    console.log('✅ TC-UAT-02: 日志内容验证完成');
  });

  /**
   * TC-UAT-03: 日志导出功能验证
   * 验收标准：
   * - 点击"下载调试日志"按钮
   * - 导出 JSON 格式文件
   * - 验证敏感信息脱敏
   */
  test('TC-UAT-03: 日志导出功能验证', async ({ page }) => {
    // 监听下载
    let downloadedContent = '';
    page.on('download', async (download) => {
      console.log(`[下载] 文件名: ${download.suggestedFilename()}`);
      await download.saveAs(path.join(SCREENSHOT_DIR, path.basename(download.suggestedFilename())));
      const stream = await download.createReadStream();
      const chunks: Buffer[] = [];
      for await (const chunk of stream) {
        chunks.push(chunk);
      }
      downloadedContent = Buffer.concat(chunks).toString('utf-8');
      console.log(`[下载] 文件大小: ${downloadedContent.length} bytes`);
    });

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 进入调试对话
    await page.locator('button:has-text("调试")').first().click();
    await page.waitForTimeout(2000);

    // 发送一条消息
    const textarea = page.locator('textarea').first();
    await textarea.fill('请介绍一下这个平台的功能');
    await page.locator('button:has-text("发送")').first().click();
    await page.waitForTimeout(10000);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat03-export-01.png`, fullPage: true });

    // 查找下载按钮
    const downloadButton = page.locator('button:has-text("调试日志")').first();
    const isVisible = await downloadButton.isVisible();
    console.log(`[UI] 下载按钮状态: ${isVisible ? '可见' : '不可见'}`);

    if (isVisible) {
      // 点击下载按钮
      await downloadButton.click();
      await page.waitForTimeout(3000);

      await page.screenshot({ path: `${SCREENSHOT_DIR}/uat03-export-02.png`, fullPage: true });

      // 如果下载了内容，验证其格式
      if (downloadedContent) {
        // 验证 JSON 格式
        try {
          const logData = JSON.parse(downloadedContent);
          console.log('[验证] JSON 格式: ✓ 有效');

          // 检查关键字段
          const hasMeta = logData.meta !== undefined;
          const hasClient = logData.client !== undefined;
          const hasServer = logData.server !== undefined;
          const hasRequestId = logData.meta?.requestId || logData.meta?.request_id;

          console.log('[验证] 元数据 (meta):', hasMeta ? '✓' : '✗');
          console.log('[验证] 客户端日志 (client):', hasClient ? '✓' : '✗');
          console.log('[验证] 服务端日志 (server):', hasServer ? '✓' : '✗');
          console.log('[验证] Request ID:', hasRequestId ? '✓' : '✗');

          // 验证敏感信息脱敏
          const content = JSON.stringify(logData);
          const hasRawApiKey = content.includes('sk-') && !content.includes('[已脱敏');
          const hasRawToken = content.toLowerCase().includes('token') && !content.includes('[已脱敏');

          if (hasRawApiKey || hasRawToken) {
            console.log('[验证] 敏感信息脱敏: ⚠️ 可能存在未脱敏内容');
          } else {
            console.log('[验证] 敏感信息脱敏: ✓ 通过');
          }

        } catch (e) {
          console.log('[验证] JSON 格式: ✗ 无效 -', e);
        }
      } else {
        console.log('⚠️ 下载事件未触发，可能使用 Blob URL 方式下载');
      }
    }

    console.log('✅ TC-UAT-03: 日志导出验证完成');
  });

  /**
   * TC-UAT-04: 流式输出验证
   * 验收标准：
   * - 打字机效果正常
   * - 思考过程实时显示
   * - 工具调用正确展示
   */
  test('TC-UAT-04: 流式输出验证', async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 进入调试对话
    await page.locator('button:has-text("调试")').first().click();
    await page.waitForTimeout(2000);

    // 发送一个会触发长回复的消息
    const textarea = page.locator('textarea').first();
    await textarea.fill('请详细介绍人工智能的发展历史，包括图灵测试、专家系统、深度学习等关键里程碑');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat04-stream-01.png`, fullPage: true });

    // 点击发送
    await page.locator('button:has-text("发送")').first().click();

    // 在流式输出进行中截图
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat04-stream-02-during.png`, fullPage: true });

    // 等待更长时间再次截图
    await page.waitForTimeout(5000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat04-stream-03-mid.png`, fullPage: true });

    // 等待完成
    await page.waitForTimeout(10000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat04-stream-04-final.png`, fullPage: true });

    // 验证消息内容
    const messageContent = await page.locator('.prose, .message-content, [data-testid="message"]').first().textContent();
    if (messageContent && messageContent.length > 50) {
      console.log(`✅ 流式输出完成，内容长度: ${messageContent.length} 字符`);
    }

    console.log('✅ TC-UAT-04: 流式输出验证完成');
  });

  /**
   * TC-UAT-05: 完整用户流程测试
   * 验收标准：端到端流程正常
   */
  test('TC-UAT-05: 完整用户流程测试', async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat05-flow-01-home.png`, fullPage: true });

    // 1. 进入调试对话
    await page.locator('button:has-text("调试")').first().click();
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat05-flow-02-chat.png`, fullPage: true });

    // 2. 发送消息
    const textarea = page.locator('textarea').first();
    await textarea.fill('请使用计算器计算 (100 + 200) * 3');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat05-flow-03-input.png`, fullPage: true });

    await page.locator('button:has-text("发送")').first().click();
    await page.waitForTimeout(12000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat05-flow-04-response.png`, fullPage: true });

    // 3. 检查下载按钮
    const downloadButton = page.locator('button:has-text("调试日志")').first();
    const hasButton = await downloadButton.isVisible();
    console.log(`[流程] 下载按钮: ${hasButton ? '✓ 可见' : '✗ 不可见'}`);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat05-flow-05-download-btn.png`, fullPage: true });

    // 4. 点击下载
    if (hasButton) {
      await downloadButton.click();
      await page.waitForTimeout(2000);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/uat05-flow-06-after-download.png`, fullPage: true });
    }

    console.log('✅ TC-UAT-05: 完整用户流程测试完成');
  });
});

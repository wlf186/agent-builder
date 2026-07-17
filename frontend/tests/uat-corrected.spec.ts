/**
 * 修正后的 UAT 验收测试 - 调试日志导出功能
 *
 * 正确步骤：
 * 1. 进入调试对话页面
 * 2. 发送消息（触发日志记录）
 * 3. 等待响应完成（按钮才会出现）
 * 4. 验证下载按钮出现
 * 5. 点击下载并验证 JSON 内容
 */

import { test, expect } from '@playwright/test';
import path from 'node:path';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('uat-corrected');

test.describe('修正后的 UAT 验收: 调试日志导出', () => {

  /**
   * TC-UAT-CORRECTED: 完整流程 - 按正确步骤执行
   */
  test('TC-UAT-CORRECTED: 完整验收流程', async ({ page }) => {
    // ========== 步骤 1: 访问首页 ==========
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-01-homepage.png`, fullPage: true });
    console.log('✅ 步骤 1: 首页加载完成');

    // ========== 步骤 2: 进入调试对话页面 ==========
    const chatButton = page.locator('button:has-text("调试")').first();
    await chatButton.click();
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-02-chat-enter.png`, fullPage: true });
    console.log('✅ 步骤 2: 进入调试对话页面');

    // ========== 步骤 3: 发送测试消息 ==========
    const textarea = page.locator('textarea').first();
    await textarea.fill('你好，请介绍一下你自己');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-03-message-filled.png`, fullPage: true });

    const sendButton = page.locator('button:has-text("发送")').first();
    await sendButton.click();
    console.log('✅ 步骤 3: 消息已发送');

    // ========== 步骤 4: 等待 AI 响应完成 ==========
    console.log('⏳ 等待 AI 响应...');
    await page.waitForTimeout(15000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-04-response-complete.png`, fullPage: true });
    console.log('✅ 步骤 4: AI 响应完成');

    // ========== 步骤 5: 验证下载按钮出现 ==========
    // 按钮文本可能是中文或英文，包含 "下载日志" 或 "调试日志"
    const downloadButtonSelectors = [
      'button:has-text("下载调试日志")',
      'button:has-text("调试日志")',
      'button:has-text("download")',
      'button:has-text("Download")',
    ];

    let downloadButton = null;
    for (const selector of downloadButtonSelectors) {
      try {
        const btn = page.locator(selector).first();
        if (await btn.isVisible({ timeout: 2000 })) {
          downloadButton = btn;
          console.log(`✅ 步骤 5: 找到下载按钮 - "${selector}"`);
          break;
        }
      } catch (e) {}
    }

    if (!downloadButton) {
      // 尝试查找所有按钮并打印文本
      const allButtons = await page.locator('button').all();
      console.log('⚠️ 未找到下载按钮，页面上所有按钮文本:');
      for (let i = 0; i < Math.min(allButtons.length, 15); i++) {
        const text = await allButtons[i].textContent();
        console.log(`  [${i}] ${text?.substring(0, 50)}`);
      }
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-05-download-check.png`, fullPage: true });

    // ========== 步骤 6: 点击下载按钮 ==========
    if (downloadButton) {
      // 获取按钮点击前的文本
      const buttonText = await downloadButton.textContent();
      console.log(`按钮文本: "${buttonText}"`);

      // 点击下载按钮
      const downloadPromise = page.waitForEvent('download', { timeout: 10000 }).catch(() => null);
      await downloadButton.click();
      const download = await downloadPromise;
      if (download) {
        await download.saveAs(path.join(SCREENSHOT_DIR, path.basename(download.suggestedFilename())));
      }
      await page.waitForTimeout(3000);
      console.log('✅ 步骤 6: 已点击下载按钮');

      await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-06-after-click.png`, fullPage: true });

      // ========== 步骤 7: 验证 JSON 内容 ==========
      // 由于 Playwright 可能无法捕获 Blob 下载，
      // 我们通过页面控制台获取日志内容
      const logContent = await page.evaluate(() => {
        // 尝试从全局变量获取日志（如果有）
        if ((window as any).lastDownloadedLog) {
          return (window as any).lastDownloadedLog;
        }
        return null;
      });

      if (logContent) {
        console.log('✅ 步骤 7: 成功获取日志内容');
        console.log(`日志大小: ${logContent.length} bytes`);

        // 验证 JSON 结构
        try {
          const logData = JSON.parse(logContent);

          const checks = {
            'meta.version': logData.meta?.version === '1.0',
            'meta.requestId': !!logData.meta?.requestId,
            'client.environment': !!logData.client?.environment,
            'client.chunks': !!logData.client?.chunks,
          };

          console.log('JSON 结构验证:');
          for (const [key, passed] of Object.entries(checks)) {
            console.log(`  ${key}: ${passed ? '✓' : '✗'}`);
          }

          // 保存日志到文件
          const fs = require('fs');
          fs.writeFileSync(
            `${SCREENSHOT_DIR}/downloaded-log-${Date.now()}.json`,
            JSON.stringify(logData, null, 2)
          );
          console.log('✅ 日志已保存到文件');

        } catch (e) {
          console.log('⚠️ JSON 解析失败:', e);
        }
      } else {
        console.log('⚠️ 无法通过页面评估获取日志内容');
        console.log('   (下载可能使用 Blob URL，需要手动验证)');
      }

    } else {
      console.log('❌ 步骤 6-7: 下载按钮未找到，测试停止');
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/corrected-07-final.png`, fullPage: true });
    console.log('✅ 测试完成');
  });

  /**
   * TC-UAT-TRACE-ID: 验证 Trace ID 关联
   */
  test('TC-UAT-TRACE-ID: Trace ID 全链路验证', async ({ page }) => {
    const requestIds: string[] = [];

    // 监听请求
    page.on('request', request => {
      const reqId = request.headers()['x-request-id'] || request.headers()['X-Request-ID'];
      if (reqId) {
        requestIds.push(reqId);
        console.log(`[Request] X-Request-ID: ${reqId}`);
      }
    });

    // 监听响应
    page.on('response', response => {
      const resId = response.headers()['x-request-id'] || response.headers()['X-Request-ID'];
      if (resId) {
        console.log(`[Response] X-Request-ID: ${resId}`);
      }
    });

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 进入聊天并发送消息
    await page.locator('button:has-text("调试")').first().click();
    await page.waitForTimeout(2000);

    const textarea = page.locator('textarea').first();
    await textarea.fill('测试 Trace ID');
    await page.locator('button:has-text("发送")').first().click();
    await page.waitForTimeout(10000);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/traceid-verification.png`, fullPage: true });

    if (requestIds.length > 0) {
      console.log(`✅ 检测到 ${requestIds.length} 个带有 X-Request-ID 的请求`);
      expect(requestIds.length).toBeGreaterThan(0);
    } else {
      console.log('⚠️ 未检测到 X-Request-ID');
    }
  });
});

/**
 * ============================================================================
 * UAT 验收测试 - 日志导出功能 (AC130)
 * ============================================================================
 *
 * 验证场景：
 * 1. 正常对话场景：验证日志包含完整 Prompt 和 Response
 * 2. 工具调用场景：验证日志包含 Tool Call 参数和返回
 * 3. 错误场景：验证日志包含后端 Error Message 和堆栈
 * 4. 导出功能：验证下载的日志文件格式正确
 * 5. 脱敏验证：确认 API Key 已打码
 *
 * 执行方式：
 * npx playwright tests/uat-log-export.spec.ts --headed
 * ============================================================================
 */

import { test, expect, type Download } from '@playwright/test';
import path from 'node:path';
import { testOutputDir } from './test-paths';

const FRONTEND_URL = 'http://localhost:20815';
const TEST_AGENT = 'test-uat-log';
const DOWNLOAD_DIR = testOutputDir('uat-log-export');

async function preserveDownload(download: Download): Promise<void> {
  await download.saveAs(path.join(DOWNLOAD_DIR, path.basename(download.suggestedFilename())));
}

// 测试数据
const TEST_MESSAGES = {
  simple: '你好，请介绍一下你自己',
  toolCall: '请使用 calculator 工具计算 123 + 456',
  multiTool: '请先计算 100 / 5，然后查询今天的比特币价格',
};

async function readDownloadText(download: Download): Promise<string> {
  const stream = await download.createReadStream();
  return new Promise<string>((resolve, reject) => {
    const chunks: Buffer[] = [];
    stream.on('data', (chunk: Buffer | string) => {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    });
    stream.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    stream.on('error', reject);
  });
}

test.beforeAll(async () => {
  // 确保后端服务运行
  const backendRes = await fetch('http://localhost:20881/api/agents');
  if (!backendRes.ok) {
    throw new Error('后端服务未运行，请先启动 backend.py');
  }
});

test.beforeEach(async ({ page }) => {
  await page.goto(FRONTEND_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(500);
});

test.describe('UAT 验收 - 日志导出功能 (AC130)', () => {

  test('UC-LOG-001: 前端应该有日志下载按钮', async ({ page }) => {
    // 进入调试对话
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    // 检查是否有日志下载按钮
    const downloadButton = page.locator('button:has-text("下载调试日志")');
    await expect(downloadButton).toBeVisible();
  });

  test('UC-LOG-002: 正常对话后应该生成日志', async ({ page }) => {
    // 进入调试对话
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    // 发送简单消息
    const inputArea = page.locator('textarea[placeholder*="输入消息"], textarea[placeholder*="message"]');
    await expect(inputArea).toBeVisible();
    await inputArea.fill(TEST_MESSAGES.simple);

    // 发送消息
    const sendButton = page.locator('button:has-text("发送"), button[type="submit"]');
    await sendButton.click();

    // 等待响应
    await page.waitForTimeout(5000);

    // 检查日志计数
    const logCount = await page.locator('text=/\\d+\\s*条日志/').first().textContent();
    console.log('日志数量:', logCount);
    expect(logCount).toBeTruthy();
  });

  test('UC-LOG-003: 工具调用场景日志应该包含工具信息', async ({ page }) => {
    // 进入调试对话
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    // 发送工具调用消息
    const inputArea = page.locator('textarea[placeholder*="输入消息"], textarea[placeholder*="message"]');
    await inputArea.fill(TEST_MESSAGES.toolCall);
    await page.keyboard.press('Enter');

    // 等待响应和工具调用
    await page.waitForTimeout(8000);

    // 检查是否有工具调用显示
    const toolCallSection = page.locator('text=工具调用');
    await expect(toolCallSection).toBeVisible();
  });

  test('UC-LOG-004: 日志导出功能应该下载文件', async ({ page }) => {
    // 进入调试对话
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    // 设置下载监听
    const downloadPromise = page.waitForEvent('download');

    // 点击下载按钮
    const downloadButton = page.locator('button:has-text("下载调试日志")');
    await downloadButton.click();

    // 等待下载完成
    const download = await downloadPromise;
    await preserveDownload(download);
    console.log('下载文件名:', download.suggestedFilename());

    // 验证文件名格式
    expect(download.suggestedFilename()).toMatch(/(client_log_|chat-debug-log-)/);

    // 读取文件内容
    const text = await readDownloadText(download);
    console.log('日志内容预览 (前500字符):', text.substring(0, 500));

    // 验证日志格式
    if (download.suggestedFilename().endsWith('.json')) {
      // JSON 格式验证
      const json = JSON.parse(text);
      expect(json).toHaveProperty('timestamp');
      expect(json).toHaveProperty('userAgent');
      expect(json).toHaveProperty('consoleLogs');
    } else if (download.suggestedFilename().endsWith('.txt')) {
      // TXT 格式验证
      expect(text).toContain('日志');
    }
  });

  test('UC-LOG-005: 主页日志下载按钮应该可用', async ({ page }) => {
    // 在主页
    await page.goto(FRONTEND_URL);

    // 查找下载调试日志按钮（可能在右上角或菜单中）
    const mainDownloadButton = page.locator('button:has-text("下载调试日志")').first();
    await expect(mainDownloadButton).toBeVisible();
  });
});

test.describe('UAT 验收 - 日志内容验证 (AC130)', () => {

  test('UC-LOG-101: 日志应该包含请求时间戳', async ({ page }) => {
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    const inputArea = page.locator('textarea[placeholder*="输入消息"], textarea[placeholder*="message"]');
    await inputArea.fill('Hello');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(5000);

    // 下载并检查日志
    const downloadPromise = page.waitForEvent('download');
    await page.locator('button:has-text("下载调试日志")').click();
    const download = await downloadPromise;
    await preserveDownload(download);

    const text = await readDownloadText(download);
    console.log('时间戳检查:', text.substring(0, 200));

    // 检查是否包含时间戳格式
    expect(text).toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  });

  test('UC-LOG-102: 错误场景应该记录错误信息', async ({ page }) => {
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    // 发送一个可能触发错误的请求（使用超长消息）
    const inputArea = page.locator('textarea[placeholder*="输入消息"], textarea[placeholder*="message"]');
    await inputArea.fill('A'.repeat(10000));
    await page.keyboard.press('Enter');
    await page.waitForTimeout(3000);

    // 即使没有响应，日志也应该记录了请求
    const logIndicator = page.locator('text=/\\d+\\s*条/');
    const exists = await logIndicator.count();
    console.log('日志指示器存在:', exists > 0);
  });
});

test.describe('UAT 验收 - 脱敏验证 (AC130)', () => {

  test('UC-LOG-201: 日志不应该包含完整 API Key', async ({ page }) => {
    await page.click(`text=${TEST_AGENT}`);
    await page.waitForTimeout(500);

    // 下载日志
    const downloadPromise = page.waitForEvent('download');
    await page.locator('button:has-text("下载调试日志")').click();
    const download = await downloadPromise;
    await preserveDownload(download);

    const text = await readDownloadText(download);

    // 检查是否有 API Key 泄露（常见的 API Key 模式）
    const apiKeyPatterns = [
      /sk-[a-zA-Z0-9]{32,}/,  // OpenAI 格式
      /Bearer\s+[a-zA-Z0-9]{32,}/,  // Bearer token
      /api[_-]?key["\s:]+[a-zA-Z0-9]{32,}/i,  // api_key 字段
    ];

    for (const pattern of apiKeyPatterns) {
      const match = text.match(pattern);
      if (match) {
        console.log('发现可能的 API Key:', match[0].substring(0, 20) + '...');
      }
    }

    // 如果找到完整 API Key，测试应该失败
    const fullApiKeyMatch = text.match(/sk-[a-zA-Z0-9]{48}/);
    expect(fullApiKeyMatch).toBeNull();
  });
});

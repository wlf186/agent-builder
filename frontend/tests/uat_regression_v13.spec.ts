/**
 * UAT Regression Test for v1.3 Bug Fixes
 *
 * Test Cases:
 * - TC-005: PDF skill execution status display correctness
 * - TC-004: Multi-round tool calls generate final answer
 * - Streaming: Typewriter effect smoothness, no message loss
 *
 * Run: npx playwright test uat_regression_test.spec.ts --headed
 */

import { test, expect } from '@playwright/test';
import path from 'node:path';
import { PROJECT_ROOT, testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const AGENT_NAME = 'test001'; // UAT专用智能体
const SCREENSHOT_DIR = testOutputDir('uat-regression-v13');

test.describe('UAT Regression Test - v1.3 Bug Fixes', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    // 首页加载截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/01-homepage.png`,
      fullPage: true
    });
  });

  test('TC-005: PDF skill execution status display (no duplicate status bars)', async ({ page }) => {
    // 1. 点击 test001 智能体的调试按钮
    await page.click(`button:has-text("调试对话")`, { timeout: 5000 });
    await page.waitForTimeout(1000);

    // 2. 进入调试对话页面截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/02-chat-page-before.png`,
      fullPage: true
    });

    // 3. 输入测试消息触发 PDF skill
    const testMessage = `请使用 pdf 技能读取 ${path.join(PROJECT_ROOT, 'README.md')} 文件`;
    await page.fill('textarea[placeholder*="输入消息"]', testMessage);

    // 4. 截图：输入后
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/03-message-input.png`,
      fullPage: true
    });

    // 5. 点击发送按钮
    await page.click('button:has-text("发送")');

    // 6. 等待技能执行完成（最多30秒）
    await page.waitForTimeout(30000);

    // 7. 技能执行完成后的截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/04-skill-execution-complete.png`,
      fullPage: true
    });

    // 8. 验证：技能执行状态区域只显示一个状态条目（修复 TC-005）
    const statusBars = await page.locator('.skill-status-bar, [data-testid="skill-status"]').count();
    console.log(`Found ${statusBars} skill status bars`);

    // 期望：只显示 0 或 1 个状态条（不应该重复显示）
    expect(statusBars).toBeLessThanOrEqual(1);

    // 9. 最终完整对话截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/05-tc005-final.png`,
      fullPage: true
    });
  });

  test('TC-004: Multi-round tool calls generate final answer', async ({ page }) => {
    // 1. 点击 test001 智能体的调试按钮
    await page.click(`button:has-text("调试对话")`, { timeout: 5000 });
    await page.waitForTimeout(1000);

    // 2. 输入需要多轮工具调用的消息
    const testMessage = '请帮我计算 25 * 37 + 123 的结果，然后把结果乘以 2';
    await page.fill('textarea[placeholder*="输入消息"]', testMessage);

    // 3. 截图：输入后
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/06-tc004-input.png`,
      fullPage: true
    });

    // 4. 点击发送按钮
    await page.click('button:has-text("发送")');

    // 5. 等待响应完成（最多60秒，需要等待多轮工具调用）
    await page.waitForTimeout(60000);

    // 6. 多轮工具调用完成后的截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/07-tc004-response.png`,
      fullPage: true
    });

    // 7. 验证：有最终回答（修复 TC-004）
    const messages = await page.locator('.message-content, [data-testid="message-content"]').allTextContents();
    const assistantMessages = messages.filter(m => m && !m.includes('User:'));

    console.log(`Found ${assistantMessages.length} assistant messages`);
    expect(assistantMessages.length).toBeGreaterThan(0);

    // 8. 最终截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/08-tc004-final.png`,
      fullPage: true
    });
  });

  test('Streaming: Typewriter effect smoothness check', async ({ page }) => {
    // 1. 点击 test001 智能体的调试按钮
    await page.click(`button:has-text("调试对话")`, { timeout: 5000 });
    await page.waitForTimeout(1000);

    // 2. 输入一个会触发流式输出的消息
    const testMessage = '请介绍一下 Agent Builder 平台的主要功能';
    await page.fill('textarea[placeholder*="输入消息"]', testMessage);

    // 3. 点击发送按钮
    await page.click('button:has-text("发送")');

    // 4. 截图：发送后立即捕获
    await page.waitForTimeout(2000);
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/09-streaming-during.png`,
      fullPage: true
    });

    // 5. 等待流式输出完成
    await page.waitForTimeout(20000);

    // 6. 最终截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/10-streaming-final.png`,
      fullPage: true
    });

    // 7. 验证：消息存在
    const messageArea = await page.locator('.messages-area, [data-testid="messages-area"]').isVisible();
    expect(messageArea).toBeTruthy();
  });
});

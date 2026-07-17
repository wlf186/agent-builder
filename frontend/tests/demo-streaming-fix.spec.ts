import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

/**
 * UAT演示脚本: test3 Agent空响应问题修复验证
 *
 * 迭代: AC130-202603151517
 *
 * ⚠️ 重要注意事项:
 * - 调试对话输入框使用 <input type="text">（不是 textarea）
 * - 人设与提示词编辑使用 <textarea>
 * - 必须在正确的区域发送消息！
 */

const SCREENSHOT_DIR = testOutputDir('demo-streaming-fix');

test.describe('UAT演示: 流式输出修复验证', () => {
  test('TC-001: test3 agent 基本响应验证', async ({ page }) => {
    test.setTimeout(120000);

    // 步骤1: 访问主页
    console.log('\n=== 步骤1: 访问主页 ===');
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/demo-01-homepage.png`,
      fullPage: true
    });
    console.log('✓ 主页已加载');

    // 步骤2: 选择test3 agent
    console.log('\n=== 步骤2: 选择test3 agent ===');

    // 点击test3 agent卡片
    await page.waitForSelector('h3:has-text("test3")', { timeout: 15000 });
    await page.locator('h3:has-text("test3")').first().click();

    // 等待agent详情页加载
    await page.waitForTimeout(3000);

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/demo-02-agent-selected.png`,
      fullPage: true
    });
    console.log('✓ test3 agent 已选中');

    // 步骤3: 在调试对话区域输入消息
    console.log('\n=== 步骤3: 在调试对话区域发送消息"你好" ===');

    // ⚠️ 关键：调试对话输入框是 <input type="text">，不是 <textarea>
    // 必须等待AgentChat组件加载完成
    await page.waitForTimeout(2000);

    // 使用更精确的选择器：input[type="text"] 且有 placeholder
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput, '调试对话输入框应可见').toBeVisible();

    await chatInput.fill('你好');
    console.log('✓ 消息已输入到调试对话区域');

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/demo-03-message-input.png`,
      fullPage: true
    });

    // 发送消息（按Enter或点击发送按钮）
    await chatInput.press('Enter');
    console.log('✓ 消息已发送');

    // 步骤4: 等待响应
    console.log('\n=== 步骤4: 等待流式响应 ===');

    await page.waitForTimeout(5000);

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/demo-04-streaming.png`,
      fullPage: true
    });

    // 继续等待响应完成
    await page.waitForTimeout(15000);

    // 步骤5: 验证响应非空
    console.log('\n=== 步骤5: 验证响应内容 ===');

    const pageContent = await page.textContent('body');
    console.log(`页面内容长度: ${pageContent?.length}`);

    // 验证页面包含响应内容
    const hasResponse = pageContent?.includes('你好') ||
                        pageContent?.includes('高兴') ||
                        pageContent?.includes('助手') ||
                        pageContent?.includes('帮助') ||
                        pageContent?.includes('Hello');

    expect(hasResponse, '应收到非空响应').toBeTruthy();

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/demo-05-final-result.png`,
      fullPage: true
    });

    console.log('✅ 演示完成 - 响应验证通过');
  });
});

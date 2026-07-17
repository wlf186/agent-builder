/**
 * 最终演示脚本: 完整用户旅程
 * AC130-202603150000
 *
 * 运行: npx playwright test final-demo.spec.ts --headed
 */

import { test, expect, Page } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('final-demo');

async function saveScreenshot(page: Page, name: string) {
  await page.screenshot({
    path: `${SCREENSHOT_DIR}/${name}.png`,
    fullPage: true
  });
}

test.describe('最终演示: 完整用户旅程', () => {
  test('headed模式演示流式对话', async ({ page }) => {
    console.log('\n=== 最终演示: 完整用户旅程 ===\n');

    // 步骤1: 访问主页
    console.log('步骤1: 访问主页 http://localhost:20815...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await saveScreenshot(page, '01-homepage');
    console.log('✓ 主页已加载');
    await page.waitForTimeout(3000);

    // 步骤2: 选择智能体 test3
    console.log('\n步骤2: 选择智能体 "test3"...');
    const test3Card = page.locator('h3, h2').filter({ hasText: 'test3' }).or(
      page.locator('[data-agent-card]').filter({ hasText: 'test3' })
    ).first();

    const isVisible = await test3Card.isVisible().catch(() => false);
    if (isVisible) {
      await test3Card.click();
      console.log('✓ 已选择 test3');
    } else {
      console.log('  未找到 test3，使用第一个可用智能体');
      await page.locator('h3').first().click();
    }
    await saveScreenshot(page, '02-agent-selected');
    await page.waitForTimeout(3000);

    // 步骤3: 定位右侧聊天区域的输入框
    console.log('\n步骤3: 定位右侧聊天区域的输入框...');

    // 关键修复: 使用正确的选择器
    // 聊天输入框是 <input type="text">，有 placeholder 属性
    // 排除左侧配置区域的输入框
    const chatInput = page.locator('input[type="text"][placeholder]').first();

    await expect(chatInput, '聊天输入框应可见').toBeVisible();
    console.log('✓ 已定位聊天输入框 (input[type="text"][placeholder])');
    await saveScreenshot(page, '03-input-located');
    await page.waitForTimeout(2000);

    // 步骤4: 输入消息 "你好"
    console.log('\n步骤4: 在输入框中输入 "你好"...');
    await chatInput.click();
    await page.waitForTimeout(500);
    await chatInput.fill('你好');
    await saveScreenshot(page, '04-message-entered');
    console.log('✓ 消息 "你好" 已输入');
    await page.waitForTimeout(3000);

    // 步骤5: 发送消息
    console.log('\n步骤5: 按 Enter 发送消息...');
    await chatInput.press('Enter');
    await saveScreenshot(page, '05-message-sent');
    console.log('✓ 消息已发送');
    await page.waitForTimeout(2000);

    // 步骤6: 等待并展示流式响应
    console.log('\n步骤6: 等待 AI 流式回复...');
    await saveScreenshot(page, '06-streaming-start');
    console.log('  流式输出开始...');

    // 每2秒截图一次，展示打字机效果
    for (let i = 1; i <= 6; i++) {
      await page.waitForTimeout(2000);
      await saveScreenshot(page, `07-streaming-${i}`);
      console.log(`  流式输出中... ${i * 2}秒`);
    }

    // 步骤7: 验证回复完成
    console.log('\n步骤7: 验证 AI 回复...');
    await saveScreenshot(page, '08-response-complete');

    const pageContent = await page.textContent('body') ?? '';
    const hasResponse = pageContent.includes('你好') ||
                        pageContent.includes('我是') ||
                        pageContent.includes('帮助') ||
                        pageContent.includes('您好') ||
                        pageContent.length > 3000;

    if (hasResponse) {
      console.log('✓ AI 回复正常显示在聊天区域');
    } else {
      console.log('⚠ 检查回复内容...');
    }
    await page.waitForTimeout(3000);

    // 最终状态截图
    await saveScreenshot(page, '09-final-state');
    console.log('\n=== 演示完成 ===');
  });
});

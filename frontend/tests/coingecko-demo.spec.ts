/**
 * CoinGecko 工具调用演示（Headed 模式）
 * AC130-202603151840
 *
 * 运行: npx playwright test coingecko-demo.spec.ts --headed
 */

import { test, expect, Page } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('coingecko-demo');

async function saveScreenshot(page: Page, name: string) {
  await page.screenshot({
    path: `${SCREENSHOT_DIR}/${name}.png`,
    fullPage: true
  });
}

test.describe('CoinGecko 工具演示', () => {
  test('headed模式演示BTC价格查询', async ({ page }) => {
    console.log('\n=== CoinGecko 工具调用演示 ===\n');

    // 步骤1: 访问主页
    console.log('步骤1: 访问主页...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 修复 X11 远程投屏渲染问题：触发浏览器重绘
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(100);

    await saveScreenshot(page, '01-homepage');
    console.log('✓ 主页已加载');
    await page.waitForTimeout(3000);

    // 步骤2: 选择智能体 test3
    console.log('\n步骤2: 选择智能体 test3...');
    const test3Card = page.locator('h3, h2').filter({ hasText: 'test3' }).or(
      page.locator('[data-agent-card]').filter({ hasText: 'test3' })
    ).first();

    const isVisible = await test3Card.isVisible().catch(() => false);
    if (isVisible) {
      await test3Card.click();
      console.log('✓ 已选择 test3');
    } else {
      console.log('  使用第一个可用智能体');
      await page.locator('h3').first().click();
    }
    await saveScreenshot(page, '02-agent-selected');
    await page.waitForTimeout(3000);

    // 步骤3: 定位聊天输入框
    console.log('\n步骤3: 定位聊天输入框...');
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput).toBeVisible();
    console.log('✓ 已定位聊天输入框');
    await saveScreenshot(page, '03-input-located');
    await page.waitForTimeout(2000);

    // 步骤4: 输入消息
    console.log('\n步骤4: 输入 "btc的最新价格"...');
    await chatInput.click();
    await page.waitForTimeout(500);
    await chatInput.fill('btc的最新价格');
    await saveScreenshot(page, '04-message-entered');
    console.log('✓ 消息已输入');
    await page.waitForTimeout(3000);

    // 步骤5: 发送消息
    console.log('\n步骤5: 发送消息...');
    await chatInput.press('Enter');
    await saveScreenshot(page, '05-message-sent');
    console.log('✓ 消息已发送');
    await page.waitForTimeout(2000);

    // 步骤6: 展示思考过程
    console.log('\n步骤6: AI 思考中...');
    await saveScreenshot(page, '06-thinking');
    console.log('  thinking 状态');
    await page.waitForTimeout(3000);

    // 步骤7: 展示工具调用
    console.log('\n步骤7: 工具调用中...');
    await saveScreenshot(page, '07-tool-calling');
    console.log('  CoinGecko 工具调用');
    await page.waitForTimeout(4000);

    // 步骤8: 展示结果返回
    console.log('\n步骤8: 结果返回中...');
    await saveScreenshot(page, '08-result-coming');
    await page.waitForTimeout(4000);

    // 步骤9: 最终结果
    console.log('\n步骤9: 最终结果...');
    await saveScreenshot(page, '09-final-result');

    // 验证
    const pageContent = await page.textContent('body');
    const hasPrice = pageContent?.includes('价格') ||
                      pageContent?.includes('price') ||
                      pageContent?.includes('USD') ||
                      pageContent?.includes('$') ||
                      pageContent?.includes('BTC');

    console.log(`  价格信息: ${hasPrice ? '✓' : '✗'}`);

    await page.waitForTimeout(3000);
    await saveScreenshot(page, '10-final-state');

    console.log('\n=== 演示完成 ===');
  });
});

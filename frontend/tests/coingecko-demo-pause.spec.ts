/**
 * CoinGecko 工具调用演示（暂停模式）
 * AC130-202603151840
 *
 * 运行: npx playwright test coingecko-demo-pause.spec.ts --headed
 *
 * 注意：演示完成后会自动暂停，保持浏览器打开状态
 * 按 F8 或点击 Resume 继续执行并关闭浏览器
 */

import { test, expect, Page } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('coingecko-demo-pause');

async function saveScreenshot(page: Page, name: string) {
  await page.screenshot({
    path: `${SCREENSHOT_DIR}/${name}.png`,
    fullPage: true
  });
}

test.describe('CoinGecko 工具演示（暂停）', () => {
  test('headed模式演示BTC价格查询（暂停等待）', async ({ page }) => {
    // 设置更长的超时时间
    test.setTimeout(120000);  // 2分钟超时
    console.log('\n=== CoinGecko 工具调用演示（暂停模式）===\n');

    // 步骤1: 访问主页
    console.log('步骤1: 访问主页...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
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

    // 步骤6: AI 思考过程
    console.log('\n步骤6: AI 思考中（thinking）...');
    await saveScreenshot(page, '06-thinking');
    console.log('  thinking 状态');
    await page.waitForTimeout(3000);

    // 步骤7: 工具调用
    console.log('\n步骤7: CoinGecko 工具调用（tool_call）...');
    await saveScreenshot(page, '07-tool-calling');
    console.log('  工具调用中');
    await page.waitForTimeout(4000);

    // 步骤8: 工具结果返回
    console.log('\n步骤8: 工具结果返回（tool_result）...');
    await saveScreenshot(page, '08-tool-result');
    console.log('  工具结果返回');
    await page.waitForTimeout(4000);

    // 步骤9: AI 回复完成
    console.log('\n步骤9: 等待 AI 回复完成...');
    await page.waitForTimeout(5000);
    await saveScreenshot(page, '09-response-complete');

    const pageContent = await page.textContent('body');
    const hasPrice = pageContent?.includes('价格') ||
                      pageContent?.includes('price') ||
                      pageContent?.includes('USD') ||
                      pageContent?.includes('$') ||
                      pageContent?.includes('BTC') ||
                      pageContent?.includes('bitcoin');

    console.log(`  价格信息: ${hasPrice ? '✓' : '✗'}`);
    await saveScreenshot(page, '10-final-state');

    console.log('\n=== 演示完成 ===');
    console.log('🔵 浏览器将保持打开状态，等待用户查看');
    console.log('🔵 按 F8 或点击 Playwright Inspector 的 Resume 按钮继续\n');

    // 暂停！保持浏览器打开，等待用户操作
    await page.pause();
  });
});

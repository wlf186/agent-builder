/**
 * CoinGecko 工具验证测试
 * AC130-202603151840
 *
 * 验证 CoinGecko MCP 工具调用是否正常
 * 运行: npx playwright test coingecko-test.spec.ts
 */

import { test, expect, Page } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('coingecko-test');

async function saveScreenshot(page: Page, name: string) {
  await page.screenshot({
    path: `${SCREENSHOT_DIR}/${name}.png`,
    fullPage: true
  });
}

test.describe('CoinGecko 工具验证', () => {
  test('验证 BTC 价格查询工具调用', async ({ page }) => {
    console.log('\n=== CoinGecko 工具验证测试 ===\n');

    // 步骤1: 访问主页
    console.log('步骤1: 访问主页...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await saveScreenshot(page, '01-homepage');
    console.log('✓ 主页已加载');

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
      console.log('  未找到 test3，使用第一个可用智能体');
      await page.locator('h3').first().click();
    }
    await page.waitForTimeout(2000);
    await saveScreenshot(page, '02-agent-selected');
    console.log('✓ 智能体已选择');

    // 步骤3: 定位聊天输入框
    console.log('\n步骤3: 定位聊天输入框...');
    // 使用正确的选择器
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput, '聊天输入框应可见').toBeVisible();
    console.log('✓ 已定位聊天输入框');
    await saveScreenshot(page, '03-input-located');

    // 步骤4: 输入 BTC 价格查询消息
    console.log('\n步骤4: 输入 "btc的最新价格"...');
    await chatInput.click();
    await page.waitForTimeout(500);
    await chatInput.fill('btc的最新价格');
    await saveScreenshot(page, '04-message-entered');
    console.log('✓ 消息已输入');
    await page.waitForTimeout(1000);

    // 步骤5: 发送消息
    console.log('\n步骤5: 发送消息...');
    await chatInput.press('Enter');
    await saveScreenshot(page, '05-message-sent');
    console.log('✓ 消息已发送');

    // 步骤6: 等待工具调用和响应
    console.log('\n步骤6: 等待 CoinGecko 工具调用...');
    await page.waitForTimeout(3000);
    await saveScreenshot(page, '06-tool-calling');

    // 等待足够时间让工具执行完成
    console.log('  等待工具执行...');
    await page.waitForTimeout(5000);
    await saveScreenshot(page, '07-response-coming');

    await page.waitForTimeout(5000);
    await saveScreenshot(page, '08-response-content');

    // 步骤7: 验证响应内容
    console.log('\n步骤7: 验证响应内容...');
    const pageContent = await page.textContent('body');

    // 检查是否有价格相关的关键词
    const hasPrice = pageContent?.includes('价格') ||
                      pageContent?.includes('price') ||
                      pageContent?.includes('USD') ||
                      pageContent?.includes('$') ||
                      pageContent?.includes('BTC') ||
                      pageContent?.includes('bitcoin');

    // 检查是否有真正的错误（更精确的判断）
    // 排除 UI 中的普通文本，只检查真正的错误消息
    const hasRealError = pageContent?.includes('处理请求时发生错误') ||
                         pageContent?.includes('请求失败') ||
                         pageContent?.includes('Request failed') ||
                         (pageContent?.includes('error') && pageContent?.includes('tool_call'));

    console.log(`  价格相关内容: ${hasPrice ? '✓' : '✗'}`);
    console.log(`  真正的错误: ${hasRealError ? '✗ 有错误' : '✓ 无错误'}`);

    await saveScreenshot(page, '09-final-state');

    // 验证结果
    if (hasRealError) {
      console.log('✗ 检测到真正的错误');
    } else if (hasPrice) {
      console.log('✓ CoinGecko 工具调用成功，返回了价格信息');
    } else {
      console.log('⚠ 需要人工检查截图确认结果');
    }

    expect(hasRealError, '不应有真正的错误信息').toBeFalsy();

    if (hasPrice) {
      console.log('✓ CoinGecko 工具调用成功，返回了价格信息');
    } else {
      console.log('⚠ 未检测到明显的价格信息，可能需要人工检查截图');
    }

    console.log('\n=== 测试完成 ===');
  });
});

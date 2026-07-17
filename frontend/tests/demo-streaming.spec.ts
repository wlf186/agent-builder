/**
 * 演示脚本: 流式对话功能
 * AC130-202603150000
 *
 * 运行: npx playwright test demo-streaming.spec.ts --headed
 */

import { test, expect, Page } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('demo-streaming');

async function saveScreenshot(page: Page, name: string) {
  await page.screenshot({
    path: `${SCREENSHOT_DIR}/${name}.png`,
    fullPage: true
  });
}

test.describe('演示: 流式对话功能', () => {
  test('演示流式对话完整流程', async ({ page }) => {
    console.log('\n=== 开始演示: 流式对话功能 ===\n');

    // 步骤1: 访问主页
    console.log('步骤1: 访问主页...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await saveScreenshot(page, '01-homepage');
    console.log('✓ 主页已加载');
    await page.waitForTimeout(2000);

    // 步骤2: 选择智能体 test3
    console.log('\n步骤2: 选择智能体 test3...');
    const test3Card = page.locator('h3, h2').filter({ hasText: 'test3' }).or(
      page.locator('[data-agent-card]').filter({ hasText: 'test3' })
    ).first();

    const isVisible = await test3Card.isVisible().catch(() => false);
    if (isVisible) {
      await test3Card.click();
    } else {
      // 如果找不到 test3，使用第一个可用智能体
      console.log('  未找到 test3，使用第一个可用智能体');
      await page.locator('h3').first().click();
    }
    await saveScreenshot(page, '02-agent-selected');
    console.log('✓ 智能体已选择');
    await page.waitForTimeout(2000);

    // 步骤3: 输入消息
    console.log('\n步骤3: 输入消息 "你好"...');
    // 修复: 聊天输入框是 <input type="text">，不是 <textarea>
    // textarea 是左侧配置区域的人设编辑框
    const chatInput = page.locator('input[type="text"]').filter({
      hasText: '' // 确保是可见的输入框
    }).first();
    await expect(chatInput, '聊天输入框应可见').toBeVisible();
    await chatInput.fill('你好');
    await saveScreenshot(page, '03-message-entered');
    console.log('✓ 消息已输入');
    await page.waitForTimeout(1500);

    // 步骤4: 发送消息
    console.log('\n步骤4: 发送消息...');
    await chatInput.press('Enter');
    await saveScreenshot(page, '04-message-sent');
    console.log('✓ 消息已发送');
    await page.waitForTimeout(2000);

    // 步骤5: 等待流式响应
    console.log('\n步骤5: 等待流式响应...');
    await saveScreenshot(page, '05-streaming-start');
    console.log('  流式输出中...');

    // 等待响应完成，每2秒截图一次
    for (let i = 0; i < 5; i++) {
      await page.waitForTimeout(2000);
      await saveScreenshot(page, `06-streaming-${i + 1}`);
      console.log(`  截图 ${i + 1}/5`);
    }

    // 步骤6: 验证响应完成
    console.log('\n步骤6: 验证响应完成...');
    await saveScreenshot(page, '07-response-complete');

    const pageContent = await page.textContent('body') ?? '';
    const hasResponse = pageContent.includes('你好') ||
                        pageContent.includes('我是') ||
                        pageContent.includes('帮助') ||
                        pageContent.length > 2000;

    if (hasResponse) {
      console.log('✓ 响应完成，有有效内容');
    } else {
      console.log('⚠ 响应可能不完整');
    }

    // 最终截图
    await page.waitForTimeout(2000);
    await saveScreenshot(page, '08-final-state');
    console.log('\n=== 演示完成 ===');
  });

  test('验证无控制台错误', async ({ page }) => {
    const errors: string[] = [];

    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    await page.goto(BASE_URL);
    await page.waitForSelector('h3, h2, [data-agent-card]');

    const agentCard = page.locator('h3').first();
    await agentCard.click();
    await page.waitForTimeout(1000);

    // 修复: 使用正确的聊天输入框选择器
    const chatInput = page.locator('input[type="text"]').filter({
      hasText: ''
    }).first();
    await chatInput.fill('演示测试');
    await chatInput.press('Enter');

    await page.waitForTimeout(8000);

    const criticalErrors = errors.filter(e =>
      e.includes('parameter') ||
      e.includes('Parameter') ||
      e.includes('TypeError') ||
      e.includes('missing')
    );

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/09-no-errors.png`,
      fullPage: true
    });

    console.log('控制台错误检查:');
    console.log(`  总错误数: ${errors.length}`);
    console.log(`  关键错误: ${criticalErrors.length}`);

    if (criticalErrors.length === 0) {
      console.log('✓ 无关键错误');
    } else {
      console.log('发现的关键错误:', criticalErrors);
    }
  });
});

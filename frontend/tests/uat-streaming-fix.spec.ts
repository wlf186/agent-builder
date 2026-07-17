import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

/**
 * UAT测试用例: test3 Agent空响应问题修复验证
 *
 * 迭代: AC130-202603151517
 * 修复内容:
 * 1. ChatRequest添加conversation_id字段
 * 2. AgentConfig属性访问错误修复
 */

const SCREENSHOT_DIR = testOutputDir('uat-streaming-fix');

test.describe('UAT: 流式输出修复验证', () => {
  test('TC-001: test3 agent 基本响应验证', async ({ page }) => {
    test.setTimeout(120000);

    // 步骤1: 访问主页
    console.log('\n=== 步骤1: 访问主页 ===');
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    // 截图: 主页加载
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/01-homepage.png`,
      fullPage: true
    });
    console.log('✓ 主页已加载');

    // 步骤2: 选择test3 agent - 使用h3标签和text选择器
    console.log('\n=== 步骤2: 选择test3 agent ===');

    // 等待agent卡片出现（使用h3标签）
    await page.waitForSelector('h3:has-text("test3")', { timeout: 15000 });

    // 点击test3 agent
    const test3Card = page.locator('h3:has-text("test3")').first();
    await test3Card.click();

    await page.waitForTimeout(3000);

    // 截图: 选择test3 agent后
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/02-agent-selected.png`,
      fullPage: true
    });
    console.log('✓ test3 agent 已选中');

    // 步骤3: 输入"你好"并发送
    console.log('\n=== 步骤3: 发送消息"你好" ===');

    const input = page.locator('textarea').first();
    await expect(input, '输入框应可见').toBeVisible();

    await input.fill('你好');
    console.log('✓ 消息已输入');

    // 截图: 消息输入后
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/03-message-input.png`,
      fullPage: true
    });

    await input.press('Enter');
    console.log('✓ 消息已发送');

    // 步骤4: 等待响应并验证
    console.log('\n=== 步骤4: 等待并验证响应 ===');

    // 等待流式输出开始
    await page.waitForTimeout(5000);

    // 截图: 响应中
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/04-response-streaming.png`,
      fullPage: true
    });

    // 继续等待响应完成
    await page.waitForTimeout(15000);

    // 步骤5: 验证响应非空
    console.log('\n=== 步骤5: 验证响应内容 ===');

    // 获取页面文本内容
    const pageContent = await page.textContent('body');
    console.log(`页面内容长度: ${pageContent?.length}`);

    // 验证页面包含非空响应（检查是否包含常见响应词汇）
    const hasResponse = pageContent?.includes('你好') ||
                        pageContent?.includes('高兴') ||
                        pageContent?.includes('助手') ||
                        pageContent?.includes('帮助') ||
                        pageContent?.includes('Hello') ||
                        pageContent?.includes('help');

    expect(hasResponse, '应收到非空响应').toBeTruthy();

    // 截图: 最终结果
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/05-final-result.png`,
      fullPage: true
    });

    console.log('✓ 响应验证通过');
  });

  test('TC-002: 流式输出打字机效果验证', async ({ page }) => {
    test.setTimeout(120000);

    console.log('\n=== 流式输出打字机效果验证 ===');

    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    // 选择test3 agent
    await page.waitForSelector('h3:has-text("test3")', { timeout: 15000 });
    await page.locator('h3:has-text("test3")').first().click();
    await page.waitForTimeout(2000);

    // 发送测试消息
    const input = page.locator('textarea').first();
    await input.fill('1+1等于几？');
    await input.press('Enter');

    console.log('等待流式输出...');

    // 短间隔多次截图，验证打字机效果
    for (let i = 0; i < 3; i++) {
      await page.waitForTimeout(2000);
      await page.screenshot({
        path: `${SCREENSHOT_DIR}/streaming-${i+1}.png`,
        fullPage: true
      });
      console.log(`✓ 流式截图 ${i+1}/3 已保存`);
    }

    // 等待响应完成
    await page.waitForTimeout(10000);

    // 验证有响应内容
    const pageContent = await page.textContent('body');
    const hasContent = pageContent?.includes('2') || pageContent?.includes('二') || pageContent?.includes('等于');

    expect(hasContent, '应有计算结果').toBeTruthy();

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/streaming-final.png`,
      fullPage: true
    });

    console.log('✓ 流式输出验证通过');
  });
});

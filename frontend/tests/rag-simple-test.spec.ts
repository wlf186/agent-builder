/**
 * RAG 简单验收测试
 */

import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('rag-simple-test');

test('RAG测试1_行政助手', async ({ page }) => {
  console.log('=== RAG测试1: 行政助手 ===');

  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);

  // 查找并点击 UAT行政助手
  const agentCards = page.locator('.agent-card, [class*="AgentCard"]');
  const count = await agentCards.count();
  console.log(`找到 ${count} 个智能体卡片`);

  let found = false;
  for (let i = 0; i < count; i++) {
    const text = await agentCards.nth(i).textContent();
    if (text && text.includes('UAT行政助手')) {
      await agentCards.nth(i).click();
      found = true;
      break;
    }
  }

  expect(found, '应找到 UAT行政助手').toBeTruthy();
  await page.waitForTimeout(2000);

  // 发送问题
  const inputBox = page.locator('input[type="text"][placeholder]').first();
  await inputBox.fill('公司有几天年假？');
  await page.keyboard.press('Enter');

  // 等待回答
  await page.waitForTimeout(8000);

  const content = await page.content();

  const has15 = content.includes('15') || content.includes('十五');
  console.log(`包含15天: ${has15}`);

  await page.screenshot({ path: `${SCREENSHOT_DIR}/rag-simple-admin.png`, fullPage: true });
  console.log('截图已保存');
});

test('RAG测试2_技术支持', async ({ page }) => {
  console.log('=== RAG测试2: 技术支持 ===');

  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);

  // 查找并点击 UAT技术支持
  const agentCards = page.locator('.agent-card, [class*="AgentCard"]');
  const count = await agentCards.count();

  let found = false;
  for (let i = 0; i < count; i++) {
    const text = await agentCards.nth(i).textContent();
    if (text && text.includes('UAT技术支持')) {
      await agentCards.nth(i).click();
      found = true;
      break;
    }
  }

  expect(found, '应找到 UAT技术支持').toBeTruthy();
  await page.waitForTimeout(2000);

  // 发送问题
  const inputBox = page.locator('input[type="text"][placeholder]').first();
  await inputBox.fill('公司有几天年假？');
  await page.keyboard.press('Enter');

  await page.waitForTimeout(8000);

  const content = await page.content();
  const hasRetrieving = content.includes('检索') || content.includes('retriev');
  console.log(`有检索提示: ${hasRetrieving} (应该没有)`);

  await page.screenshot({ path: `${SCREENSHOT_DIR}/rag-simple-tech.png`, fullPage: true });
});

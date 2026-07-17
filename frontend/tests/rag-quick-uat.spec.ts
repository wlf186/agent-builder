/**
 * RAG 快速验收测试
 * 直接验证核心功能
 */

import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('rag-quick-uat');

test('RAG 验收测试 - 行政助手', async ({ page }) => {
  console.log('开始 RAG 验收测试...');

  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');

  // 选择 UAT行政助手
  console.log('1. 选择 UAT行政助手');
  const agentSelector = page.locator('.agent-selector');
  await agentSelector.click();
  await page.locator('text=UAT行政助手').click();
  await page.waitForTimeout(1000);

  // 询问年假问题
  console.log('2. 发问: 公司有几天年假？');
  const inputBox = page.locator('input[type="text"][placeholder]').first();
  await inputBox.fill('公司有几天年假？');
  await page.keyboard.press('Enter');

  // 等待回答
  console.log('3. 等待回答...');
  await page.waitForTimeout(5000);

  // 检查页面内容
  const pageContent = await page.content();
  console.log('页面关键词检查:');

  const hasRetrieving = pageContent.includes('检索') || pageContent.includes('retriev');
  console.log(`  - 检索提示: ${hasRetrieving ? '✓' : '✗'}`);

  const has15 = pageContent.includes('15') || pageContent.includes('十五');
  console.log(`  - 包含"15天": ${has15 ? '✓' : '✗'}`);

  const hasCitation = pageContent.includes('cyberpunk') || pageContent.includes('员工手册');
  console.log(`  - 引用来源: ${hasCitation ? '✓' : '✗'}`);

  // 截图
  await page.screenshot({ path: `${SCREENSHOT_DIR}/rag-uat-admin.png` });
  console.log(`4. 截图已保存: ${SCREENSHOT_DIR}/rag-uat-admin.png`);

  expect(has15, '应回答15天年假').toBeTruthy();
});

test('RAG 验收测试 - 技术支持', async ({ page }) => {
  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');

  // 选择 UAT技术支持
  console.log('1. 选择 UAT技术支持');
  const agentSelector = page.locator('.agent-selector');
  await agentSelector.click();
  await page.locator('text=UAT技术支持').click();
  await page.waitForTimeout(1000);

  // 询问同样的问题
  console.log('2. 发问: 公司有几天年假？');
  const inputBox = page.locator('input[type="text"][placeholder]').first();
  await inputBox.fill('公司有几天年假？');
  await page.keyboard.press('Enter');

  // 等待回答
  console.log('3. 等待回答...');
  await page.waitForTimeout(5000);

  // 检查页面内容
  const pageContent = await page.content();
  console.log('页面关键词检查:');

  const hasRetrieving = pageContent.includes('检索') || pageContent.includes('retriev');
  console.log(`  - 检索提示: ${hasRetrieving ? '✗ (不应该有)' : '✓ (正确)'}`);

  const saysDontKnow = pageContent.includes('不知道') || pageContent.includes('无法');
  console.log(`  - 表示不知道: ${saysDontKnow ? '✓' : '✗'}`);

  // 截图
  await page.screenshot({ path: `${SCREENSHOT_DIR}/rag-uat-tech.png` });
  console.log(`4. 截图已保存: ${SCREENSHOT_DIR}/rag-uat-tech.png`);
});

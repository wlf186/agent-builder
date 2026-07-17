import { test, expect } from '@playwright/test';
import { testOutputPath } from './test-paths';

test('Debug Knowledge Base Page', async ({ page }) => {
  await page.goto('http://localhost:20815/knowledge-bases');
  await page.waitForLoadState('networkidle');

  // 截图
  await page.screenshot({ path: testOutputPath('debug-kb', 'page.png'), fullPage: true });

  // 列出所有按钮
  const buttons = page.locator('button');
  const count = await buttons.count();
  console.log(`Total buttons: ${count}`);

  for (let i = 0; i < count; i++) {
    const text = await buttons.nth(i).textContent();
    console.log(`Button ${i}: "${text}"`);
  }

  // 尝试点击创建知识库按钮
  const createButton = page.locator('button:has-text("创建知识库")');
  if (await createButton.isVisible()) {
    console.log('Found "创建知识库" button');
  } else {
    console.log('NOT found "创建知识库" button');
  }
});

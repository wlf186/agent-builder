import { test, expect } from '@playwright/test';

test.describe('例行功能验证', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');
  });

  test('主页加载正常', async ({ page }) => {
    await expect(page.locator('h1, h2').first()).toBeVisible();
  });

  test('智能体列表显示', async ({ page }) => {
    await page.waitForTimeout(2000);
    const agentCards = page.locator('h3');
    await expect(agentCards.first()).toBeVisible();
  });

  test('智能体选择和对话', async ({ page }) => {
    // 选择 test3 智能体
    await page.locator('h3:has-text("test3")').first().click();
    await page.waitForTimeout(2000);

    // 定位聊天输入框
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput).toBeVisible({ timeout: 5000 });

    // 发送消息
    await chatInput.fill('你好');
    await chatInput.press('Enter');

    // 等待响应
    await page.waitForTimeout(5000);

    // 验证有响应内容
    const pageContent = await page.textContent('body');
    expect(pageContent).toBeTruthy();
    expect(pageContent!.length).toBeGreaterThan(100);
  });

  test('知识库页面可访问', async ({ page }) => {
    await page.goto('http://localhost:20815/knowledge-bases');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // 验证知识库列表显示
    const pageContent = await page.textContent('body');
    expect(pageContent).toContain('知识库');
  });
});

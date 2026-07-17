import { test, expect } from '@playwright/test';

test('simple health check', async ({ page }) => {
  await page.goto('http://localhost:20815');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('body')).toBeVisible();
  console.log('✅ 简单测试通过');
});

import { test } from '@playwright/test';

const BASE_URL = 'http://localhost:20815';

test('调试请求 URL', async ({ page }) => {
  console.log('\n=== 调试请求 URL ===\n');
  
  // 监听网络请求
  page.on('request', request => {
    if (request.url().includes('chat')) {
      console.log(`[请求] ${request.method()} ${request.url()}`);
    }
  });
  
  page.on('response', async response => {
    if (response.url().includes('chat')) {
      console.log(`[响应] ${response.status()} ${response.url()}`);
      const headers = response.headers();
      console.log(`[响应头] content-type: ${headers['content-type']}`);
    }
  });
  
  // 监听浏览器控制台
  page.on('console', msg => {
    const text = msg.text();
    if (text.includes('[DEBUG]') || text.includes('流') || text.includes('stream') || text.includes('fetch')) {
      console.log(`[浏览器] ${text}`);
    }
  });
  
  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);

  // 选择 test3
  const agent = page.locator('text=test3').first();
  if (await agent.count() > 0) {
    await agent.click();
    await page.waitForTimeout(2000);
  }

  // 发送"你好"
  const input = page.locator('input[type="text"][placeholder]').first();
  await input.fill('你好');
  
  console.log(`[测试] ${new Date().toISOString()} 发送消息`);
  await page.keyboard.press('Enter');

  // 等待一段时间观察
  await page.waitForTimeout(35000);
});

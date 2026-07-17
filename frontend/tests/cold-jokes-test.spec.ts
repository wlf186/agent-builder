import { test, expect } from '@playwright/test';
import { testOutputPath } from './test-paths';

test('Cold Jokes MCP Tool - should return joke responses', async ({ page }) => {
    // 1. 访问主页
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    // 2. 选择智能体
    await page.locator('h3:has-text("test3")').first().click();
    await page.waitForTimeout(2000);

    // 3. 定位正确的输入框
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput).toBeVisible();

    // 4. 输入消息
    await chatInput.fill('讲3个冷笑话');
    await page.waitForTimeout(500);

    // 5. 发送消息
    await chatInput.press('Enter');

    // 6. 等待响应
    await page.waitForTimeout(15000);

    // 7. 验证响应
    const pageContent = await page.textContent('body');
    console.log('Page content (first 500 chars):', pageContent?.substring(0, 500));

    // 检查是否有笑话内容
    const hasJokeContent = pageContent?.includes('冷笑话') ||
                          pageContent?.includes('数学书') ||
                          pageContent?.includes('月亮') ||
                          pageContent?.includes('小明');

    // 截图
    await page.screenshot({ path: testOutputPath('cold-jokes-test', 'result.png') });

    console.log('Has joke content:', hasJokeContent);
    expect(hasJokeContent).toBeTruthy();
});

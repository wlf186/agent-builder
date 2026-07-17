import { test } from '@playwright/test';
import { testOutputDir } from './test-paths';

const SCREENSHOT_DIR = testOutputDir('test001-routine');

test('test001例行验证 - 3轮对话', async ({ page }) => {
  test.setTimeout(120000);
  
  await page.goto('http://localhost:20815');
  await page.waitForLoadState('networkidle');
  
  // Click on test001 agent
  await page.click('text=test001');
  await page.waitForTimeout(2000);
  
  const messages = [
    '现在是几月几号几点几分',
    '99/33是多少',
    '计算结果再加2.5是多少'
  ];
  
  for (let i = 0; i < messages.length; i++) {
    console.log(`\n=== 第${i+1}轮对话 ===`);
    console.log(`发送: ${messages[i]}`);
    
    // Find input and send message
    const input = page.locator('textarea').first();
    await input.fill(messages[i]);
    await input.press('Enter');
    
    // Wait for response
    await page.waitForTimeout(10000);
    
    console.log('✓ 等待完成');
  }
  
  // Take screenshot
  await page.screenshot({ path: `${SCREENSHOT_DIR}/test001-routine-result.png`, fullPage: true });
  console.log('\n截图已保存');
});

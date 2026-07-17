import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

/**
 * 演示脚本：3 轮对话演示（暂停模式）
 *
 * 演示步骤:
 * 1. 访问主页并选择 test3 智能体
 * 2. 第1轮: ETH的最新价格 (CoinGecko 工具)
 * 3. 第2轮: 讲2个冷笑话 (cold-jokes 工具)
 * 4. 第3轮: 32748+392/2+1是多少 (Calculator 工具)
 * 5. 暂停，保持浏览器打开
 */

test.use({ headless: false }); // 关键：使用 headed 模式

test.setTimeout(120000); // 设置超时为 120 秒

test('演示: 3 轮对话（CoinGecko + cold-jokes + Calculator）', async ({ page }) => {
  const screenshotDir = testOutputDir('demo-3-rounds');

  // ========== 初始化 ==========
  await page.goto('http://localhost:20815');
  await page.waitForLoadState('networkidle');

  // 修复 X11 远程投屏渲染问题：触发浏览器重绘
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(100);

  console.log('📱 主页加载成功');

  // 选择 test3 智能体
  const test3Agent = page.locator('h3:has-text("test3")').first();
  await expect(test3Agent).toBeVisible({ timeout: 10000 });
  await test3Agent.click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${screenshotDir}/00-agent-selected.png` });
  console.log('🤖 test3 智能体已选择');

  // 定位聊天输入框
  const chatInput = page.locator('input[type="text"][placeholder]').first();
  await expect(chatInput).toBeVisible({ timeout: 5000 });

  // ========== 第1轮：ETH 价格查询 ==========
  console.log('');
  console.log('========== 第1轮对话：ETH 价格查询 ==========');
  await chatInput.fill('ETH的最新价格');
  await page.waitForTimeout(500);
  await chatInput.press('Enter');
  console.log('⏳ 等待 CoinGecko 工具调用...');
  await page.waitForTimeout(15000); // CoinGecko API 可能需要较长时间
  await page.screenshot({ path: `${screenshotDir}/01-round1-eth-price.png`, fullPage: true });
  console.log('✅ 第1轮完成');

  // ========== 第2轮：冷笑话 ==========
  console.log('');
  console.log('========== 第2轮对话：冷笑话 ==========');
  await chatInput.fill('讲2个冷笑话');
  await page.waitForTimeout(500);
  await chatInput.press('Enter');
  console.log('⏳ 等待 cold-jokes 工具调用...');
  await page.waitForTimeout(15000); // 增加等待时间
  await page.screenshot({ path: `${screenshotDir}/02-round2-cold-jokes.png`, fullPage: true });
  console.log('✅ 第2轮完成');

  // 等待输入框恢复可用状态
  await page.waitForTimeout(3000);

  // ========== 第3轮：计算器 ==========
  console.log('');
  console.log('========== 第3轮对话：计算器 ==========');
  await chatInput.fill('32748+392/2+1是多少');
  await page.waitForTimeout(500);
  await chatInput.press('Enter');
  console.log('⏳ 等待 Calculator 工具调用...');
  await page.waitForTimeout(8000);
  await page.screenshot({ path: `${screenshotDir}/03-round3-calculator.png`, fullPage: true });
  console.log('✅ 第3轮完成');

  // ========== 最终状态 ==========
  await page.screenshot({ path: `${screenshotDir}/04-final-state.png`, fullPage: true });
  console.log('');
  console.log('========== 3 轮对话演示完成 ==========');
  console.log('🔍 浏览器窗口保持打开，请查看演示结果...');
  console.log('📸 截图已保存到:', screenshotDir);

  // ========== 暂停：保持浏览器打开 ==========
  console.log('');
  console.log('⏸️  演示暂停 - 按 Ctrl+C 关闭浏览器');

  // 无限期等待，保持浏览器打开
  // 用户可以手动关闭浏览器窗口或按 Ctrl+C
  await page.pause();
});

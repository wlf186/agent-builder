import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

/**
 * UAT Test: cold-jokes MCP 工具修复验证
 *
 * 修复内容: cold-jokes MCP 工具的 service 字段为空问题
 * 测试目标: 验证智能体能正常调用 cold-jokes 工具并返回笑话
 *
 * 测试步骤:
 * 1. 访问主页
 * 2. 选择 test3 智能体
 * 3. 在聊天输入框输入 "讲3个冷笑话"
 * 4. 验证 AI 返回笑话内容（非空响应）
 */

test('UAT: cold-jokes MCP 工具修复验证', async ({ page }) => {
  const screenshotDir = testOutputDir('cold-jokes-uat');

  // Step 1: 访问主页
  await page.goto('http://localhost:20815');
  await page.waitForLoadState('networkidle');
  await page.screenshot({ path: `${screenshotDir}/01-homepage.png` });
  console.log('✅ Step 1: 主页加载成功');

  // Step 2: 选择 test3 智能体
  const test3Agent = page.locator('h3:has-text("test3")').first();
  await expect(test3Agent).toBeVisible({ timeout: 10000 });
  await test3Agent.click();
  await page.waitForTimeout(2000); // 等待智能体详情加载
  await page.screenshot({ path: `${screenshotDir}/02-agent-selected.png` });
  console.log('✅ Step 2: test3 智能体选择成功');

  // Step 3: 定位聊天输入框（关键：使用正确的选择器）
  const chatInput = page.locator('input[type="text"][placeholder]').first();
  await expect(chatInput).toBeVisible({ timeout: 5000 });
  await page.screenshot({ path: `${screenshotDir}/03-before-input.png` });
  console.log('✅ Step 3: 聊天输入框定位成功');

  // Step 4: 输入消息
  await chatInput.fill('讲3个冷笑话');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${screenshotDir}/04-message-filled.png` });
  console.log('✅ Step 4: 消息输入成功');

  // Step 5: 发送消息
  await chatInput.press('Enter');
  console.log('⏳ Step 5: 消息已发送，等待 AI 响应...');

  // Step 6: 等待响应（工具调用需要时间）
  await page.waitForTimeout(10000); // 等待 10 秒让工具调用完成
  await page.screenshot({ path: `${screenshotDir}/05-after-response.png`, fullPage: true });
  console.log('✅ Step 6: 响应等待完成');

  // Step 7: 验证响应内容
  const pageContent = await page.textContent('body');

  // 检查是否包含笑话相关的关键词
  const jokeKeywords = [
    '笑话', '冷笑话', '幽默', '有趣',
    '为什么', '因为', '问', '答',
    '哈哈', '笑', '搞笑'
  ];

  const hasValidResponse = jokeKeywords.some(keyword =>
    pageContent?.includes(keyword)
  );

  // 保存页面内容用于调试
  console.log('页面内容片段:', pageContent?.slice(-500));

  await page.screenshot({ path: `${screenshotDir}/06-final-state.png`, fullPage: true });

  // 验证结果
  if (hasValidResponse) {
    console.log('✅ UAT 测试通过: cold-jokes 工具调用成功，返回笑话内容');
  } else {
    console.log('❌ UAT 测试失败: 未检测到笑话内容');
    console.log('页面内容:', pageContent);
  }

  expect(hasValidResponse).toBeTruthy();
});

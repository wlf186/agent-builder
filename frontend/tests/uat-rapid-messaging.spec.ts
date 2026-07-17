/**
 * UAT 测试：快速连续发送消息
 * 
 * 验证场景：
 * 1. 用户发送第一条消息
 * 2. 智能体回复后，用户立即发送第二条消息
 * 3. 第二条消息应该立即显示在聊天中
 * 
 * Bug 背景：
 * 之前存在循环依赖问题：
 * handleSend -> onConversationChange -> initialMessages -> 覆盖 messages
 * 导致第二条消息被 initialMessages useEffect 覆盖，不显示在 UI 中
 * 
 * 修复方案：
 * 添加 localMessageUpdateRef 标志，在本地消息更新后跳过 initialMessages 的覆盖
 */
import { test, expect } from '@playwright/test';

test.describe('快速连续消息 UAT', () => {
  test('智能体回复后立即发送第二条消息应该正常显示', async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    // 选择 test3 智能体
    const agentCard = page.locator('text=test3').first();
    await agentCard.click();
    await page.waitForTimeout(500);

    const inputField = page.locator('input[type="text"][placeholder]').first();

    // 发送第一条消息
    await inputField.fill('第一条测试消息');
    await inputField.press('Enter');

    // 等待回复完成（输入框变为可用）
    await page.waitForFunction(() => {
      const input = document.querySelector('input[type="text"][placeholder]');
      return input && !input.hasAttribute('disabled');
    }, { timeout: 90000 });

    // 验证第一条消息显示
    await expect(page.locator('text=第一条测试消息')).toBeVisible();

    // 立即发送第二条消息
    await inputField.fill('第二条测试消息');
    await inputField.press('Enter');

    // 等待一小段时间
    await page.waitForTimeout(500);

    // 验证第二条消息立即显示（这是修复的关键点）
    await expect(page.locator('text=第二条测试消息')).toBeVisible({ timeout: 5000 });

    // 等待回复完成
    await page.waitForFunction(() => {
      const input = document.querySelector('input[type="text"][placeholder]');
      return input && !input.hasAttribute('disabled');
    }, { timeout: 90000 });

    // 验证两条消息都存在
    const firstMessageCount = await page.locator('text=第一条测试消息').count();
    const secondMessageCount = await page.locator('text=第二条测试消息').count();
    
    expect(firstMessageCount).toBeGreaterThanOrEqual(1);
    expect(secondMessageCount).toBeGreaterThanOrEqual(1);

    console.log('✅ UAT 测试通过：快速连续发送消息功能正常');
  });

  test('发送消息后输入框应该被清空', async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    // 选择 test3 智能体
    await page.locator('text=test3').first().click();
    await page.waitForTimeout(500);

    const inputField = page.locator('input[type="text"][placeholder]').first();

    // 发送消息
    await inputField.fill('测试清空输入框');
    await inputField.press('Enter');

    // 验证输入框被清空
    await page.waitForTimeout(100);
    const inputValue = await inputField.inputValue();
    expect(inputValue).toBe('');

    console.log('✅ 输入框清空测试通过');
  });
});

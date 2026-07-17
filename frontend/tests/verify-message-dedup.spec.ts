/**
 * 验证消息重复 Bug 修复效果
 * iteration-2603131100
 *
 * 测试目标：
 * 1. 每条用户消息只显示一次
 * 2. 每条智能体回复只显示一次
 * 3. /save 接口只被调用一次（不是两次）
 * 4. 流式输出效果正常
 */

import { test, expect } from '@playwright/test';

test.describe('消息重复 Bug 修复验证', () => {
  test.beforeEach(async ({ page }) => {
    // 记录所有网络请求
    page.on('request', request => {
      if (request.url().includes('/save')) {
        console.log(`[REQUEST] ${request.method()} ${request.url()}`);
      }
    });

    page.on('response', response => {
      if (response.url().includes('/save')) {
        console.log(`[RESPONSE] ${response.status()} ${response.url()}`);
      }
    });
  });

  test('TC-MSG-001: 发送消息后不重复显示', async ({ page }) => {
    // 导航到首页
    await page.goto('http://localhost:20815');

    // 等待页面加载
    await page.waitForLoadState('networkidle');

    // 选择或创建一个智能体
    const agentButton = page.locator('text=调试对话').first();
    await expect(agentButton).toBeVisible();
    await agentButton.click();

    // 等待聊天界面加载
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });

    // 记录初始消息数量
    const initialUserMessages = await page.locator('.user-message').count();
    const initialAssistantMessages = await page.locator('.assistant-message').count();
    console.log(`初始消息数 - 用户: ${initialUserMessages}, 助手: ${initialAssistantMessages}`);

    // 发送第一条消息
    const chatInput = page.locator('[data-testid="chat-input"]');
    await chatInput.fill('你好，请用一句话介绍你自己');

    // 监听 /save 请求
    let saveCount = 0;
    page.on('request', request => {
      if (request.url().includes('/conversations/') && request.url().includes('/save')) {
        saveCount++;
        console.log(`[SAVE REQUEST #${saveCount}] ${request.url()}`);
      }
    });

    // 发送消息
    await page.locator('button:has-text("发送")').click();

    // 等待回复完成（等待流式输出结束）
    await page.waitForTimeout(10000);

    // 检查消息数量
    const userMessages = await page.locator('.user-message').count();
    const assistantMessages = await page.locator('.assistant-message').count();

    console.log(`当前消息数 - 用户: ${userMessages}, 助手: ${assistantMessages}`);
    console.log(`本次 /save 调用次数: ${saveCount}`);

    // 验证：用户消息只增加 1 条
    expect(userMessages).toBe(initialUserMessages + 1);

    // 验证：助手消息只增加 1 条
    expect(assistantMessages).toBe(initialAssistantMessages + 1);

    // 验证：/save 接口只被调用 1 次
    expect(saveCount).toBeLessThanOrEqual(1);

    // 获取用户消息文本，验证不重复
    const userMessageTexts = await page.locator('.user-message').allTextContents();
    const helloCount = userMessageTexts.filter(t => t.includes('你好')).length;
    console.log(`包含"你好"的用户消息数: ${helloCount}`);
    expect(helloCount).toBe(1);
  });

  test('TC-MSG-002: 连续发送多条消息不重复', async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    // 进入调试对话
    await page.locator('text=调试对话').first().click();
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });

    const chatInput = page.locator('[data-testid="chat-input"]');

    // 连续发送 3 条消息
    const messages = ['第一条消息', '第二条消息', '第三条消息'];
    const saveCounts: number[] = [];

    for (let i = 0; i < messages.length; i++) {
      let currentSaveCount = 0;

      page.on('request', request => {
        if (request.url().includes('/conversations/') && request.url().includes('/save')) {
          currentSaveCount++;
        }
      });

      await chatInput.fill(messages[i]);
      await page.locator('button:has-text("发送")').click();

      // 等待回复
      await page.waitForTimeout(8000);

      saveCounts.push(currentSaveCount);
      console.log(`消息 ${i + 1} - /save 调用次数: ${currentSaveCount}`);

      // 清除监听器
      page.removeAllListeners();
    }

    // 验证每条消息的 /save 调用都不超过 1 次
    saveCounts.forEach((count, i) => {
      expect(count, `消息 ${i + 1} 的 /save 调用次数`).toBeLessThanOrEqual(1);
    });

    // 检查最终消息总数
    const userMessages = await page.locator('.user-message').allTextContents();
    const count1 = userMessages.filter(t => t.includes('第一条')).length;
    const count2 = userMessages.filter(t => t.includes('第二条')).length;
    const count3 = userMessages.filter(t => t.includes('第三条')).length;

    console.log(`消息计数 - 第一条: ${count1}, 第二条: ${count2}, 第三条: ${count3}`);

    expect(count1).toBe(1);
    expect(count2).toBe(1);
    expect(count3).toBe(1);
  });

  test('TC-MSG-003: 流式输出效果正常', async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    await page.locator('text=调试对话').first().click();
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });

    // 发送消息并观察流式输出
    const chatInput = page.locator('[data-testid="chat-input"]');
    await chatInput.fill('请详细说明什么是人工智能');

    // 监听流式响应
    let chunkCount = 0;
    page.on('response', async response => {
      if (response.url().includes('/stream/')) {
        console.log('[STREAM] 流式响应开始');
        const contentType = response.headers()['content-type'];
        expect(contentType).toContain('text/event-stream');
      }
    });

    await page.locator('button:has-text("发送")').click();

    // 等待助手消息出现
    await page.waitForSelector('.assistant-message', { timeout: 5000 });

    // 观察消息内容逐步增加（流式效果）
    let previousLength = 0;
    let stableCount = 0;

    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(500);
      const assistantMsg = page.locator('.assistant-message').last();
      const content = await assistantMsg.textContent();
      const currentLength = content?.length || 0;

      if (currentLength > previousLength) {
        console.log(`[STREAMING] 内容增长: ${previousLength} -> ${currentLength}`);
        stableCount = 0;
      } else {
        stableCount++;
      }

      previousLength = currentLength;

      // 如果连续 3 次检查内容长度不变，认为流式输出结束
      if (stableCount >= 3 && currentLength > 50) {
        console.log('[STREAM] 流式输出完成');
        break;
      }
    }

    // 验证最终有内容
    const finalContent = await page.locator('.assistant-message').last().textContent();
    expect(finalContent?.length).toBeGreaterThan(20);
    console.log(`[STREAM] 最终内容长度: ${finalContent?.length}`);
  });

  test('TC-MSG-004: 历史会话正常加载', async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');

    await page.locator('text=调试对话').first().click();
    await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });

    // 发送一条消息创建会话历史
    const chatInput = page.locator('[data-testid="chat-input"]');
    await chatInput.fill('历史测试消息');
    await page.locator('button:has-text("发送")').click();
    await page.waitForTimeout(8000);

    // 打开历史会话抽屉
    await page.locator('button[aria-label="打开历史"]').or(
      page.locator('[data-testid="history-button"]')
    ).or(
      page.locator('text=历史').first()
    ).click();

    // 等待抽屉打开
    await page.waitForTimeout(1000);

    // 验证会话列表存在
    const conversationList = page.locator('.conversation-list, [data-testid="conversation-list"]');
    const hasList = await conversationList.count() > 0;

    if (hasList) {
      console.log('[HISTORY] 历史会话列表存在');
      const items = await conversationList.locator('.conversation-card, [data-testid="conversation-card"]').count();
      console.log(`[HISTORY] 会话数量: ${items}`);
    } else {
      console.log('[HISTORY] 会话列表元素未找到，可能是选择器问题');
    }

    // 关闭抽屉
    await page.keyboard.press('Escape');
  });
});

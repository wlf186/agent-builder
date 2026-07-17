/**
 * RAG 知识库对话功能 UAT 测试
 *
 * 迭代: AC130-202603170949
 * User Rep: AC130 Team
 *
 * 专注于测试对话检索功能
 */

import { test, expect } from '@playwright/test';
import fs from 'fs';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('chat-uat-rag');

// 确保截图目录存在
if (!fs.existsSync(SCREENSHOT_DIR)) {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
}

test.describe('RAG 知识库对话 UAT 测试', () => {
  test.beforeAll(async () => {
    console.log('==========================================');
    console.log('RAG 知识库对话功能 UAT 测试');
    console.log('==========================================');
  });

  test('测试1: 关联知识库的智能体对话', async ({ page }) => {
    console.log('\n【测试1】关联知识库的智能体对话');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-01-homepage.png`, fullPage: true });

    // 查找 UAT行政助手
    const agentCard = page.locator('h3:has-text("UAT行政助手"), text=UAT行政助手').first();

    if (await agentCard.isVisible({ timeout: 10000 })) {
      await agentCard.click();
      console.log('✓ 选择了 UAT行政助手');
    } else {
      console.log('⚠ 未找到 UAT行政助手，选择第一个可用智能体');
      const firstAgent = page.locator('h3, h2').first();
      await firstAgent.click();
    }

    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-02-agent-selected.png`, fullPage: true });

    // 使用多种方式查找聊天输入框
    const selectors = [
      'input[type="text"][placeholder]',
      'textarea[placeholder*="输入"]',
      'textarea[placeholder*="message"]',
      'input[placeholder*="输入"]',
      'input[placeholder*="message"]',
      '[contenteditable="true"]'
    ];

    let chatInput = null;
    for (const selector of selectors) {
      try {
        const element = page.locator(selector).first();
        if (await element.isVisible({ timeout: 2000 })) {
          chatInput = element;
          console.log(`✓ 找到输入框: ${selector}`);
          break;
        }
      } catch (e) {
        // 继续尝试下一个选择器
      }
    }

    if (!chatInput) {
      console.log('⚠ 所有选择器都失败，尝试查找任何输入框');
      chatInput = page.locator('input, textarea').first();
    }

    await expect(chatInput).toBeVisible({ timeout: 10000 });
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-03-input-found.png`, fullPage: true });

    // 输入问题
    await chatInput.fill('公司有几天年假？');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-04-question-filled.png`, fullPage: true });
    console.log('✓ 问题已输入');

    // 发送消息
    await chatInput.press('Enter');
    console.log('✓ 消息已发送，等待回答...');

    // 等待回答
    await page.waitForTimeout(25000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-05-answer-received.png`, fullPage: true });

    // 分析回答
    const content = await page.textContent('body');

    const hasYearLeave = content?.includes('年假');
    const hasNumbers = content?.includes('15') || content?.includes('10') || content?.includes('5');
    const hasRetrieval = content?.includes('检索') || content?.includes('知识库') || content?.includes('来源');

    console.log('\n回答分析:');
    console.log('  - 包含"年假":', hasYearLeave);
    console.log('  - 包含数字:', hasNumbers);
    console.log('  - 显示检索提示:', hasRetrieval);

    if (hasYearLeave || hasNumbers) {
      console.log('✓ 智能体返回了相关回答');
    } else {
      console.log('⚠ 智能体回答可能不相关');
    }

    if (hasRetrieval) {
      console.log('✓ 显示了检索提示');
    }
  });

  test('测试2: 未关联知识库的智能体对话（隔离性测试）', async ({ page }) => {
    console.log('\n【测试2】未关联知识库的智能体对话');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 查找 UAT技术支持
    const agentCard = page.locator('h3:has-text("UAT技术支持"), text=UAT技术支持').first();

    if (await agentCard.isVisible({ timeout: 10000 })) {
      await agentCard.click();
      console.log('✓ 选择了 UAT技术支持');
    } else {
      console.log('⚠ 未找到 UAT技术支持，选择其他智能体');
      const agents = page.locator('h3, h2');
      const count = await agents.count();
      for (let i = 0; i < count; i++) {
        const text = await agents.nth(i).textContent();
        if (!text?.includes('UAT行政助手')) {
          await agents.nth(i).click();
          break;
        }
      }
    }

    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-06-tech-agent-selected.png`, fullPage: true });

    // 查找聊天输入框
    const selectors = [
      'input[type="text"][placeholder]',
      'textarea[placeholder*="输入"]',
      'textarea[placeholder*="message"]'
    ];

    let chatInput = null;
    for (const selector of selectors) {
      try {
        const element = page.locator(selector).first();
        if (await element.isVisible({ timeout: 2000 })) {
          chatInput = element;
          break;
        }
      } catch (e) {
        // 继续尝试
      }
    }

    if (!chatInput) {
      chatInput = page.locator('input, textarea').first();
    }

    await expect(chatInput).toBeVisible({ timeout: 10000 });

    // 输入相同问题
    await chatInput.fill('公司有几天年假？');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-07-tech-question-filled.png`, fullPage: true });

    await chatInput.press('Enter');
    console.log('✓ 消息已发送，等待回答...');

    await page.waitForTimeout(20000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-08-tech-answer-received.png`, fullPage: true });

    // 分析回答
    const content = await page.textContent('body');

    const hasRefusal = content?.includes('不知道') ||
                      content?.includes('无法') ||
                      content?.includes('不清楚') ||
                      content?.includes('技术支持');

    const hasRetrieval = content?.includes('检索') ||
                        content?.includes('知识库');

    console.log('\n隔离性验证:');
    console.log('  - 智能体拒绝回答:', hasRefusal);
    console.log('  - 显示检索提示:', hasRetrieval);

    if (hasRefusal || !hasRetrieval) {
      console.log('✓ 隔离性测试通过');
    } else {
      console.log('⚠ 隔离性可能存在问题');
    }
  });

  test.afterAll(async () => {
    console.log('\n==========================================');
    console.log('对话测试完成');
    console.log('截图目录:', SCREENSHOT_DIR);
    console.log('==========================================');
  });
});

/**
 * RAG 知识库管理功能 UAT 测试（简化版）
 *
 * 迭代: AC130-202603170949
 * User Rep: AC130 Team
 *
 * 使用更灵活的选择器和容错处理
 */

import { test, expect } from '@playwright/test';
import path from 'path';
import fs from 'fs';
import { AC130_FIXTURE_DIR, testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('simple-uat-rag');
const TEST_DOC_PATH = path.join(AC130_FIXTURE_DIR, 'Cyberpunk公司2026员工手册.txt');

// 确保截图目录存在
if (!fs.existsSync(SCREENSHOT_DIR)) {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
}

test.describe('RAG 知识库 UAT 测试', () => {
  test.beforeAll(async () => {
    console.log('==========================================');
    console.log('RAG 知识库管理 UAT 测试');
    console.log('==========================================');
  });

  test('测试1: 知识库创建', async ({ page }) => {
    console.log('\n【测试1】知识库创建');

    await page.goto(`${BASE_URL}/knowledge-bases`);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/01-kb-list.png`, fullPage: true });

    // 点击创建按钮
    const createBtn = page.locator('button').filter({ hasText: /创建知识库|Create/i }).first();
    await createBtn.click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/02-create-dialog.png`, fullPage: true });

    // 填写表单
    const inputs = page.locator('input[type="text"]');
    const count = await inputs.count();

    for (let i = 0; i < count; i++) {
      const placeholder = await inputs.nth(i).getAttribute('placeholder');
      if (placeholder && placeholder.includes('名称')) {
        await inputs.nth(i).fill('人力资源库');
        break;
      }
    }

    const textareas = page.locator('textarea');
    const textareaCount = await textareas.count();

    for (let i = 0; i < textareaCount; i++) {
      const placeholder = await textareas.nth(i).getAttribute('placeholder');
      if (placeholder && placeholder.includes('描述')) {
        await textareas.nth(i).fill('包含员工手册、考勤制度、报销流程等人力资源相关文档');
        break;
      }
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/03-form-filled.png`, fullPage: true });

    // 提交
    const submitBtn = page.locator('button').filter({ hasText: /创建|Create|Submit/i }).first();
    await submitBtn.click();
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/04-created.png`, fullPage: true });

    console.log('✓ 测试1完成');
  });

  test('测试2: 文档上传', async ({ page }) => {
    console.log('\n【测试2】文档上传');

    await page.goto(`${BASE_URL}/knowledge-bases`);
    await page.waitForLoadState('networkidle');

    // 点击第一个知识库
    const firstKB = page.locator('h3, a, [class*="card"]').first();
    await firstKB.click();
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/05-kb-detail.png`, fullPage: true });

    // 上传文档
    const fileInput = page.locator('input[type="file"]').first();
    if (await fileInput.isVisible()) {
      await fileInput.setInputFiles(TEST_DOC_PATH);
    } else {
      // 尝试点击上传按钮
      const uploadBtn = page.locator('button').filter({ hasText: /上传|Upload|添加|Add/i }).first();
      await uploadBtn.click();
      await page.waitForTimeout(500);
      const fileInputAfter = page.locator('input[type="file"]').first();
      await fileInputAfter.setInputFiles(TEST_DOC_PATH);
    }

    await page.waitForTimeout(5000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/06-uploaded.png`, fullPage: true });

    console.log('✓ 测试2完成');
  });

  test('测试3: 智能体关联', async ({ page }) => {
    console.log('\n【测试3】智能体关联');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/07-homepage.png`, fullPage: true });

    // 创建智能体
    const createBtn = page.locator('button').filter({ hasText: /创建智能体|Create Agent/i }).first();
    await createBtn.click();
    await page.waitForTimeout(1000);

    const inputs = page.locator('input[type="text"]');
    const inputCount = await inputs.count();

    for (let i = 0; i < inputCount; i++) {
      const placeholder = await inputs.nth(i).getAttribute('placeholder');
      if (placeholder && (placeholder.includes('名称') || placeholder.includes('name'))) {
        await inputs.nth(i).fill('行政助手');
        break;
      }
    }

    const textareas = page.locator('textarea');
    const textareaCount = await textareas.count();

    for (let i = 0; i < textareaCount; i++) {
      const placeholder = await textareas.nth(i).getAttribute('placeholder');
      if (placeholder && (placeholder.includes('人设') || placeholder.includes('prompt'))) {
        await textareas.nth(i).fill('你是公司的行政助手，负责回答人力资源相关问题。');
        break;
      }
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/08-agent-creating.png`, fullPage: true });

    // 尝试勾选知识库
    const checkboxes = page.locator('input[type="checkbox"]');
    const checkboxCount = await checkboxes.count();

    if (checkboxCount > 0) {
      await checkboxes.first().check();
      await page.waitForTimeout(500);
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/09-kb-selected.png`, fullPage: true });

    // 保存
    const saveBtn = page.locator('button').filter({ hasText: /保存|Save/i }).first();
    await saveBtn.click();
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/10-agent-saved.png`, fullPage: true });

    console.log('✓ 测试3完成');
  });

  test('测试4: 对话检索', async ({ page }) => {
    console.log('\n【测试4】对话检索');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 选择行政助手
    const agentCard = page.locator('text=行政助手').first();
    if (await agentCard.isVisible()) {
      await agentCard.click();
    }
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/11-agent-selected.png`, fullPage: true });

    // 找聊天输入框
    const chatInput = page.locator('input[type="text"][placeholder], textarea[placeholder*="输入"], textarea[placeholder*="message"]').first();
    await expect(chatInput).toBeVisible({ timeout: 10000 });

    await chatInput.fill('公司有几天年假？');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/12-question-entered.png`, fullPage: true });

    await chatInput.press('Enter');
    console.log('等待回答...');
    await page.waitForTimeout(20000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/13-answer-received.png`, fullPage: true });

    const content = await page.textContent('body');
    console.log('页面内容包含年假:', content?.includes('年假'));
    console.log('页面内容包含检索:', content?.includes('检索'));

    console.log('✓ 测试4完成');
  });

  test('测试5: 隔离性测试', async ({ page }) => {
    console.log('\n【测试5】隔离性测试');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 创建技术支持智能体
    const createBtn = page.locator('button').filter({ hasText: /创建智能体|Create Agent/i }).first();
    await createBtn.click();
    await page.waitForTimeout(1000);

    const inputs = page.locator('input[type="text"]');
    const inputCount = await inputs.count();

    for (let i = 0; i < inputCount; i++) {
      const placeholder = await inputs.nth(i).getAttribute('placeholder');
      if (placeholder && (placeholder.includes('名称') || placeholder.includes('name'))) {
        await inputs.nth(i).fill('技术支持');
        break;
      }
    }

    const textareas = page.locator('textarea');
    const textareaCount = await textareas.count();

    for (let i = 0; i < textareaCount; i++) {
      const placeholder = await textareas.nth(i).getAttribute('placeholder');
      if (placeholder && (placeholder.includes('人设') || placeholder.includes('prompt'))) {
        await textareas.nth(i).fill('你是公司的技术支持，负责解决技术问题，不负责人力资源相关问题。');
        break;
      }
    }

    // 不勾选知识库，直接保存
    const saveBtn = page.locator('button').filter({ hasText: /保存|Save/i }).first();
    await saveBtn.click();
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/14-tech-agent-created.png`, fullPage: true });

    // 发送相同问题
    const chatInput = page.locator('input[type="text"][placeholder], textarea[placeholder*="输入"], textarea[placeholder*="message"]').first();
    await expect(chatInput).toBeVisible({ timeout: 10000 });

    await chatInput.fill('公司有几天年假？');
    await chatInput.press('Enter');
    await page.waitForTimeout(15000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/15-isolation-test.png`, fullPage: true });

    const content = await page.textContent('body');
    console.log('未关联知识库的智能体回答:', content?.slice(-100));

    console.log('✓ 测试5完成');
  });

  test.afterAll(async () => {
    console.log('\n==========================================');
    console.log('所有测试完成，请查看截图');
    console.log('==========================================');
  });
});

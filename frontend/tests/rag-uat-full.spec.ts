/**
 * RAG 知识库系统完整验收测试
 *
 * 测试场景：
 * 1. 创建/使用知识库并上传文档
 * 2. 配置智能体挂载知识库
 * 3. 验证 RAG 检索功能
 * 4. 验证无知识库时不触发检索
 */

import { test, expect } from '@playwright/test';
import path from 'node:path';
import { AC130_FIXTURE_DIR, testOutputDir } from './test-paths';

const SCREENSHOT_DIR = testOutputDir('rag-uat-full');

const BASE_URL = 'http://localhost:20815';
const KB_NAME = '人力资源库';
const DOC_NAMES = ['Cyberpunk公司2026员工手册.txt', 'Cyberpunk公司代码规范.txt'];

test.describe('RAG 知识库验收测试', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    // 等待页面加载
    await page.waitForLoadState('networkidle');
  });

  test('步骤1: 检查知识库并上传文档', async ({ page }) => {
    console.log('=== 步骤1: 检查知识库并上传文档 ===');

    // 点击知识库管理
    await page.click('[data-testid="knowledge-base-button"], button:has-text("知识库")');
    await page.waitForTimeout(500);

    // 查找人力资源库
    const kbExists = await page.locator(`text=${KB_NAME}`).count() > 0;
    console.log(`知识库 "${KB_NAME}" ${kbExists ? '已存在' : '不存在'}`);

    if (!kbExists) {
      // 创建新知识库
      await page.click('button:has-text("创建知识库")');
      await page.waitForTimeout(300);

      await page.locator('input[placeholder*="知识库名称"]').fill(KB_NAME);
      await page.locator('textarea[placeholder*="描述"]').fill('Cyberpunk公司人力资源相关文档');
      await page.click('button:has-text("创建")');
      await page.waitForTimeout(500);
    }

    // 进入知识库详情
    await page.click(`text=${KB_NAME}`);
    await page.waitForTimeout(500);

    // 检查现有文档
    const docCount = await page.locator('[data-testid="document-item"], .document-item').count();
    console.log(`现有文档数量: ${docCount}`);

    // 上传测试文档
    const fileInput = page.locator('input[type="file"]');
    const docPath1 = path.join(AC130_FIXTURE_DIR, DOC_NAMES[0]);
    const docPath2 = path.join(AC130_FIXTURE_DIR, DOC_NAMES[1]);

    await fileInput.setInputFiles([docPath1, docPath2]);
    await page.waitForTimeout(2000);

    console.log('✓ 文档上传完成');
  });

  test('步骤2: 配置行政助手智能体挂载知识库', async ({ page }) => {
    console.log('=== 步骤2: 配置行政助手智能体 ===');

    // 导航到智能体管理
    await page.click('[data-testid="agents-button"], button:has-text("智能体")');
    await page.waitForTimeout(500);

    // 找到 UAT行政助手
    const agentCard = page.locator('.agent-card, [data-testid="agent-card"]').filter({
      hasText: 'UAT行政助手'
    });

    await agentCard.click();
    await page.waitForTimeout(500);

    // 检查知识库选项
    const kbDropdown = page.locator('[data-testid="knowledge-base-select"], select:has-text("知识库")');

    // 检查是否可以选择人力资源库
    const hasHRKB = await page.locator('option:has-text("人力资源库")').count() > 0;
    console.log(`人力资源库选项: ${hasHRKB ? '可用' : '不可用'}`);

    if (hasHRKB) {
      await kbDropdown.selectOption(KB_NAME);
      await page.waitForTimeout(300);
      console.log('✓ 已挂载人力资源库到行政助手');
    }

    // 保存配置
    await page.click('button:has-text("保存")');
    await page.waitForTimeout(500);
  });

  test('步骤3: 测试行政助手 RAG 检索', async ({ page }) => {
    console.log('=== 步骤3: 测试行政助手 RAG 检索 ===');

    // 选择 UAT行政助手
    const agentSelector = page.locator('[data-testid="agent-selector"], .agent-selector');
    await agentSelector.click();
    await page.locator(`text=UAT行政助手`).click();
    await page.waitForTimeout(500);

    // 输入问题
    const question = '公司有几天年假？';
    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill(question);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(3000);

    // 检查 RAG 检索提示
    const retrievalIndicator = page.locator('[data-testid="retrieving-indicator"], .retrieving-indicator, text=/检索/');
    const hasRetrievalMsg = await retrievalIndicator.count() > 0;
    console.log(`RAG检索提示: ${hasRetrievalMsg ? '显示' : '未显示'}`);

    // 检查回答是否包含 15 天
    const answerArea = page.locator('[data-testid="chat-message"], .message-content').last();
    await page.waitForTimeout(2000);

    const answerText = await answerArea.textContent();
    console.log('回答内容:', answerText?.substring(0, 200));

    const hasAnswer = answerText?.includes('15') || answerText?.includes('十五');
    console.log(`回答包含年假信息: ${hasAnswer ? '是' : '否'}`);

    // 检查引用来源
    const citation = page.locator('[data-testid="citation"], .citation, [class*="citation"]');
    const hasCitation = await citation.count() > 0;
    console.log(`显示引用来源: ${hasCitation ? '是' : '否'}`);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat-admin-assistant-rag.png` });
    console.log(`✓ 截图已保存: ${SCREENSHOT_DIR}/uat-admin-assistant-rag.png`);

    expect(hasAnswer, '行政助手应回答年假信息').toBeTruthy();
  });

  test('步骤4: 测试技术支持不触发RAG', async ({ page }) => {
    console.log('=== 步骤4: 测试技术支持不触发 RAG ===');

    // 选择 UAT技术支持
    const agentSelector = page.locator('[data-testid="agent-selector"], .agent-selector');
    await agentSelector.click();
    await page.locator(`text=UAT技术支持`).click();
    await page.waitForTimeout(500);

    // 输入同样的问题
    const question = '公司有几天年假？';
    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill(question);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(3000);

    // 检查不应有 RAG 检索提示
    const retrievalIndicator = page.locator('[data-testid="retrieving-indicator"], .retrieving-indicator, text=/检索/');
    const hasRetrievalMsg = await retrievalIndicator.count() > 0;
    console.log(`RAG检索提示: ${hasRetrievalMsg ? '显示（不应该）' : '未显示（正确）'}`);

    // 检查回答 - 应该说不知道或无法回答
    const answerArea = page.locator('[data-testid="chat-message"], .message-content').last();
    const answerText = await answerArea.textContent();
    console.log('回答内容:', answerText?.substring(0, 200));

    const saysDontKnow = answerText?.includes('不知道') || answerText?.includes('无法') || answerText?.includes('没有');
    console.log(`技术支持表示不知道: ${saysDontKnow ? '是' : '否'}`);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat-tech-support-no-rag.png` });
    console.log(`✓ 截图已保存: ${SCREENSHOT_DIR}/uat-tech-support-no-rag.png`);

    expect(!hasRetrievalMsg, '技术支持不应显示RAG检索提示').toBeTruthy();
  });

  test('步骤5: 测试代码规范检索', async ({ page }) => {
    console.log('=== 步骤5: 测试代码规范检索 ===');

    // 选择 UAT行政助手
    const agentSelector = page.locator('[data-testid="agent-selector"], .agent-selector');
    await agentSelector.click();
    await page.locator(`text=UAT行政助手`).click();
    await page.waitForTimeout(500);

    // 询问代码规范问题
    const question = 'Python 函数名应该使用什么命名规范？';
    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill(question);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(3000);

    // 检查回答
    const answerArea = page.locator('[data-testid="chat-message"], .message-content').last();
    const answerText = await answerArea.textContent();

    const hasAnswer = answerText?.includes('下划线') || answerText?.includes('snake_case') || answerText?.includes('小写');
    console.log(`回答包含命名规范信息: ${hasAnswer ? '是' : '否'}`);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uat-code-standards-rag.png` });
    console.log(`✓ 截图已保存: ${SCREENSHOT_DIR}/uat-code-standards-rag.png`);

    expect(hasAnswer, '应能检索到代码规范信息').toBeTruthy();
  });
});

test.describe('RAG 前端显示验证', () => {
  test('验证前端 RAG 相关 UI 元素', async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');

    // 选择 UAT行政助手
    const agentSelector = page.locator('[data-testid="agent-selector"], .agent-selector');
    await agentSelector.click();
    await page.locator(`text=UAT行政助手`).click();
    await page.waitForTimeout(500);

    // 发送问题
    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill('公司的年假政策是什么？');
    await page.keyboard.press('Enter');

    // 等待并检查前端元素
    await page.waitForTimeout(2000);

    console.log('检查前端 UI 元素：');
    console.log('1. 思考区域 (thinking area)');
    const thinkingArea = page.locator('[class*="thinking"], [data-testid="thinking"]');
    const hasThinking = await thinkingArea.count() > 0;
    console.log(`   思考区域: ${hasThinking ? '存在' : '未找到'}`);

    console.log('2. 检索状态指示器');
    const retrievingIndicator = page.locator('[class*="retriev"], [data-testid="retrieving"]');
    const hasRetrieving = await retrievingIndicator.count() > 0;
    console.log(`   检索指示器: ${hasRetrieving ? '存在' : '未找到'}`);

    console.log('3. 引用来源显示');
    const citations = page.locator('[class*="citation"], [data-testid="citation"]');
    const citationCount = await citations.count();
    console.log(`   引用来源: ${citationCount} 个`);

    console.log('4. 消息状态');
    const messageStatus = page.locator('[class*="status"], [data-testid="message-status"]');
    const statusCount = await messageStatus.count();
    console.log(`   状态标记: ${statusCount} 个`);

    // 最终截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/uat-frontend-ui-check.png`,
      fullPage: true
    });
    console.log('✓ 完整页面截图已保存');
  });
});

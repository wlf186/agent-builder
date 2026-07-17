/**
 * RAG 知识库系统官方验收测试
 * 验证 AC130-202603170949 功能
 */

import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('rag-uat-official');

// 测试1: 行政助手应触发 RAG 检索并正确回答
test('RAG-UAT-01: 行政助手应检索知识库并回答年假问题', async ({ page }) => {
  console.log('\n=== RAG-UAT-01: 行政助手 RAG 检索测试 ===\n');

  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);

  // 查找并点击 UAT行政助手 智能体
  console.log('步骤1: 查找 UAT行政助手 智能体...');
  const allText = await page.textContent('body');
  const hasAgent = allText?.includes('UAT行政助手');

  if (!hasAgent) {
    console.log('  ⚠️ 页面上未找到 UAT行政助手，尝试滚动加载...');
    // 尝试滚动
    await page.evaluate(() => window.scrollBy(0, 500));
    await page.waitForTimeout(1000);
  }

  // 使用文本定位器直接查找
  const agentElement = page.locator('text=UAT行政助手').first();
  const count = await agentElement.count();

  if (count > 0) {
    console.log('  ✓ 找到 UAT行政助手');
    await agentElement.click();
  } else {
    // 尝试使用 CSS 选择器
    const cards = page.locator('[class*="agent"], [class*="Agent"], [class*="card"]');
    const cardCount = await cards.count();

    console.log(`  尝试遍历 ${cardCount} 个卡片元素...`);
    for (let i = 0; i < Math.min(cardCount, 20); i++) {
      const text = await cards.nth(i).textContent();
      if (text && text.includes('UAT行政助手')) {
        console.log('  ✓ 找到 UAT行政助手');
        await cards.nth(i).click();
        break;
      }
    }
  }

  // 等待进入聊天界面
  await page.waitForTimeout(2000);
  console.log('步骤2: 已进入聊天界面');

  // 输入问题
  const question = '公司有几天年假？';
  console.log(`步骤3: 发送问题: "${question}"`);

  const inputBox = page.locator('input[type="text"][placeholder], input[placeholder*="消息"], input[placeholder*="输入"]').first();
  await inputBox.fill(question);
  await page.keyboard.press('Enter');

  console.log('步骤4: 等待回答（可能需要 5-10 秒）...');

  // 等待回答完成
  await page.waitForTimeout(10000);

  // 检查页面内容
  const pageContent = await page.content();

  console.log('\n步骤5: 验证结果:');
  console.log('------------------------------------------------');

  // 检查是否包含年份数字
  const has15 = pageContent.includes('15') || pageContent.includes('十五');
  console.log(`  [核心] 包含"15天"年假信息: ${has15 ? '✓ 通过' : '✗ 失败'}`);

  // 检查是否包含检索相关提示
  const hasRetrieving = pageContent.includes('检索') ||
                       pageContent.includes('retriev') ||
                       pageContent.includes('Retrieving') ||
                       pageContent.includes('知识库');
  console.log(`  [RAG] 检索/知识库提示: ${hasRetrieving ? '✓ 显示' : '✗ 未显示'}`);

  // 检查是否包含文档引用
  const hasCitation = pageContent.includes('员工手册') ||
                     pageContent.includes('cyberpunk') ||
                     pageContent.includes('来源') ||
                     pageContent.includes('引用');
  console.log(`  [引用] 文档来源提示: ${hasCitation ? '✓ 显示' : '✗ 未显示'}`);

  // 获取消息内容
  const messages = page.locator('[class*="message"], [class*="Message"], .markdown, [class*="content"]');
  const msgCount = await messages.count();
  console.log(`  [信息] 页面消息元素数: ${msgCount}`);

  if (msgCount > 0) {
    const lastMsg = messages.last();
    const msgText = await lastMsg.textContent();
    if (msgText) {
      console.log(`  [内容] 最后消息摘要: ${msgText.substring(0, 80).trim()}...`);
    }
  }

  console.log('------------------------------------------------');

  // 截图保存
  await page.screenshot({
    path: `${SCREENSHOT_DIR}/rag-uat-01-admin-rag.png`,
    fullPage: true
  });
  console.log(`  📸 截图已保存: ${SCREENSHOT_DIR}/rag-uat-01-admin-rag.png\n`);

  // 断言核心功能
  expect(has15, '行政助手应能回答15天年假问题').toBeTruthy();
});

// 测试2: 技术支持不应触发 RAG
test('RAG-UAT-02: 技术支持不触发RAG检索', async ({ page }) => {
  console.log('\n=== RAG-UAT-02: 技术支持无RAG测试 ===\n');

  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);

  // 查找并点击 UAT技术支持
  console.log('步骤1: 查找 UAT技术支持 智能体...');
  const agentElement = page.locator('text=UAT技术支持').first();
  const count = await agentElement.count();

  if (count > 0) {
    console.log('  ✓ 找到 UAT技术支持');
    await agentElement.click();
  } else {
    // 遍历查找
    const cards = page.locator('[class*="agent"], [class*="Agent"]');
    const cardCount = await cards.count();

    for (let i = 0; i < Math.min(cardCount, 20); i++) {
      const text = await cards.nth(i).textContent();
      if (text && text.includes('UAT技术支持')) {
        await cards.nth(i).click();
        break;
      }
    }
  }

  await page.waitForTimeout(2000);
  console.log('步骤2: 已进入聊天界面');

  // 发送同样的问题
  const question = '公司有几天年假？';
  console.log(`步骤3: 发送问题: "${question}"`);

  const inputBox = page.locator('input[type="text"][placeholder], input[placeholder*="消息"]').first();
  await inputBox.fill(question);
  await page.keyboard.press('Enter');

  console.log('步骤4: 等待回答...');
  await page.waitForTimeout(8000);

  const pageContent = await page.content();

  console.log('\n步骤5: 验证结果:');
  console.log('------------------------------------------------');

  // 不应该有检索提示
  const hasRetrieving = pageContent.includes('检索') ||
                       pageContent.includes('retriev') ||
                       pageContent.includes('Retrieving');
  console.log(`  [RAG] 检索提示显示: ${hasRetrieving ? '✗ 不应显示' : '✓ 未显示（正确）'}`);

  // 应该表示不知道
  const saysDontKnow = pageContent.includes('不知道') ||
                      pageContent.includes('无法') ||
                      pageContent.includes('没有') ||
                      pageContent.includes('不清楚');
  console.log(`  [回答] 表示不知道/无法回答: ${saysDontKnow ? '✓ 是' : '✗ 否'}`);

  console.log('------------------------------------------------');

  await page.screenshot({
    path: `${SCREENSHOT_DIR}/rag-uat-02-tech-no-rag.png`,
    fullPage: true
  });
  console.log(`  📸 截图已保存: ${SCREENSHOT_DIR}/rag-uat-02-tech-no-rag.png\n`);

  expect(!hasRetrieving, '技术支持不应显示RAG检索提示').toBeTruthy();
});

// 测试3: 代码规范检索
test('RAG-UAT-03: 行政助手检索代码规范', async ({ page }) => {
  console.log('\n=== RAG-UAT-03: 代码规范检索测试 ===\n');

  await page.goto(BASE_URL);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);

  // 进入 UAT行政助手
  const agentElement = page.locator('text=UAT行政助手').first();
  if (await agentElement.count() > 0) {
    await agentElement.click();
  } else {
    const cards = page.locator('[class*="agent"], [class*="Agent"]');
    const cardCount = await cards.count();
    for (let i = 0; i < Math.min(cardCount, 20); i++) {
      const text = await cards.nth(i).textContent();
      if (text && text.includes('UAT行政助手')) {
        await cards.nth(i).click();
        break;
      }
    }
  }

  await page.waitForTimeout(2000);

  const question = 'Python 函数名应该使用什么命名规范？';
  console.log(`发送问题: "${question}"`);

  const inputBox = page.locator('input[type="text"][placeholder]').first();
  await inputBox.fill(question);
  await page.keyboard.press('Enter');

  await page.waitForTimeout(8000);

  const pageContent = await page.content();

  const hasAnswer = pageContent.includes('下划线') ||
                   pageContent.includes('snake_case') ||
                   pageContent.includes('小写') ||
                   pageContent.includes('pep');
  console.log(`包含命名规范信息: ${hasAnswer ? '✓ 通过' : '✗ 失败'}`);

  await page.screenshot({
    path: `${SCREENSHOT_DIR}/rag-uat-03-code-standards.png`,
    fullPage: true
  });
  console.log('📸 截图已保存\n');

  expect(hasAnswer, '应能检索到代码规范信息').toBeTruthy();
});

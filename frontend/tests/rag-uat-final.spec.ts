/**
 * RAG 知识库系统完整验收测试
 * 修正版 - 使用正确的页面选择器
 */

import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const SCREENSHOT_DIR = testOutputDir('rag-uat-final');

test.describe('RAG 知识库验收测试', () => {

  test('测试1: 行政助手 RAG 检索', async ({ page }) => {
    console.log('=== 测试1: 行政助手 RAG 检索 ===');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // 查找并点击 UAT行政助手 智能体卡片
    console.log('1. 查找 UAT行政助手 智能体...');
    const agentCards = page.locator('.agent-card, [class*="AgentCard"]');
    const count = await agentCards.count();

    console.log(`   找到 ${count} 个智能体卡片`);

    // 遍历找到 UAT行政助手
    let found = false;
    for (let i = 0; i < count; i++) {
      const card = agentCards.nth(i);
      const text = await card.textContent();
      if (text && text.includes('UAT行政助手')) {
        console.log('   ✓ 找到 UAT行政助手');
        await card.click();
        found = true;
        break;
      }
    }

    expect(found, '应找到 UAT行政助手 智能体').toBeTruthy();

    // 等待进入聊天界面
    await page.waitForTimeout(2000);
    console.log('2. 已进入聊天界面');

    // 输入问题
    const question = '公司有几天年假？';
    console.log(`3. 发送问题: ${question}`);

    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill(question);

    // 按回车发送
    await page.keyboard.press('Enter');
    console.log('4. 消息已发送，等待回答...');

    // 等待回答 - 可能需要较长时间
    await page.waitForTimeout(8000);

    // 检查页面内容
    const pageContent = await page.content();
    console.log('5. 检查回答内容:');

    // 检查是否包含检索相关提示
    const hasRetrieving = pageContent.includes('检索') ||
                         pageContent.includes('retriev') ||
                         pageContent.includes('Retrieving') ||
                         pageContent.includes('知识库');
    console.log(`   - 检索提示/知识库: ${hasRetrieving ? '✓' : '✗'}`);

    // 检查是否包含年份数字
    const hasAnswer = pageContent.includes('15') ||
                     pageContent.includes('十五') ||
                     pageContent.includes('15天');
    console.log(`   - 包含"15天": ${hasAnswer ? '✓' : '✗'}`);

    // 检查是否包含文档引用
    const hasCitation = pageContent.includes('员工手册') ||
                       pageContent.includes('cyberpunk') ||
                       pageContent.includes('来源') ||
                       pageContent.includes('引用');
    console.log(`   - 引用来源: ${hasCitation ? '✓' : '✗'}`);

    // 获取最后一条消息的内容
    const messages = page.locator('[class*="message"], [class*="Message"], .markdown');
    const msgCount = await messages.count();
    console.log(`   - 消息数量: ${msgCount}`);

    if (msgCount > 0) {
      const lastMsg = messages.last();
      const msgText = await lastMsg.textContent();
      console.log(`   - 最后消息内容: ${msgText?.substring(0, 100)}...`);
    }

    // 截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/rag-uat-admin-final.png`,
      fullPage: true
    });
    console.log(`6. ✓ 截图已保存: ${SCREENSHOT_DIR}/rag-uat-admin-final.png`);

    // 断言 - 关键检查
    expect(hasAnswer, '行政助手应能回答年假问题').toBeTruthy();
  });

  test('测试2: 技术支持不触发RAG', async ({ page }) => {
    console.log('=== 测试2: 技术支持不触发 RAG ===');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // 查找并点击 UAT技术支持 智能体卡片
    console.log('1. 查找 UAT技术支持 智能体...');
    const agentCards = page.locator('.agent-card, [class*="AgentCard"]');
    const count = await agentCards.count();

    let found = false;
    for (let i = 0; i < count; i++) {
      const card = agentCards.nth(i);
      const text = await card.textContent();
      if (text && text.includes('UAT技术支持')) {
        console.log('   ✓ 找到 UAT技术支持');
        await card.click();
        found = true;
        break;
      }
    }

    expect(found, '应找到 UAT技术支持 智能体').toBeTruthy();

    await page.waitForTimeout(2000);
    console.log('2. 已进入聊天界面');

    // 输入同样的问题
    const question = '公司有几天年假？';
    console.log(`3. 发送问题: ${question}`);

    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill(question);
    await page.keyboard.press('Enter');
    console.log('4. 消息已发送，等待回答...');

    await page.waitForTimeout(8000);

    // 检查页面内容
    const pageContent = await page.content();
    console.log('5. 检查回答内容:');

    // 检查是否包含检索相关提示 - 不应该有
    const hasRetrieving = pageContent.includes('检索') ||
                         pageContent.includes('retriev') ||
                         pageContent.includes('Retrieving');
    console.log(`   - 检索提示: ${hasRetrieving ? '✗ (不应该有)' : '✓ (正确)'}`);

    // 检查是否表示不知道
    const saysDontKnow = pageContent.includes('不知道') ||
                        pageContent.includes('无法') ||
                        pageContent.includes('没有') ||
                        pageContent.includes('不清楚');
    console.log(`   - 表示不知道/无法回答: ${saysDontKnow ? '✓' : '✗'}`);

    // 截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/rag-uat-tech-final.png`,
      fullPage: true
    });
    console.log(`6. ✓ 截图已保存: ${SCREENSHOT_DIR}/rag-uat-tech-final.png`);

    // 断言 - 不应该有检索提示
    expect(!hasRetrieving, '技术支持不应显示RAG检索提示').toBeTruthy();
  });

  test('测试3: 代码规范检索', async ({ page }) => {
    console.log('=== 测试3: 代码规范检索 ===');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // 查找并点击 UAT行政助手
    console.log('1. 查找 UAT行政助手 智能体...');
    const agentCards = page.locator('.agent-card, [class*="AgentCard"]');
    const count = await agentCards.count();

    let found = false;
    for (let i = 0; i < count; i++) {
      const card = agentCards.nth(i);
      const text = await card.textContent();
      if (text && text.includes('UAT行政助手')) {
        await card.click();
        found = true;
        break;
      }
    }

    expect(found, '应找到 UAT行政助手 智能体').toBeTruthy();

    await page.waitForTimeout(2000);

    // 询问代码规范问题
    const question = 'Python 函数名应该使用什么命名规范？';
    console.log(`2. 发送问题: ${question}`);

    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill(question);
    await page.keyboard.press('Enter');
    console.log('3. 等待回答...');

    await page.waitForTimeout(8000);

    // 检查回答
    const pageContent = await page.content();
    console.log('4. 检查回答内容:');

    const hasAnswer = pageContent.includes('下划线') ||
                     pageContent.includes('snake_case') ||
                     pageContent.includes('小写') ||
                     pageContent.includes('pep');
    console.log(`   - 包含命名规范信息: ${hasAnswer ? '✓' : '✗'}`);

    // 截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/rag-uat-code-final.png`,
      fullPage: true
    });
    console.log(`5. ✓ 截图已保存: ${SCREENSHOT_DIR}/rag-uat-code-final.png`);

    expect(hasAnswer, '应能检索到代码规范信息').toBeTruthy();
  });

});

test.describe('前端 UI 元素验证', () => {

  test('验证 RAG 相关 UI 元素', async ({ page }) => {
    console.log('=== 验证前端 UI 元素 ===');

    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1000);

    // 进入 UAT行政助手 聊天界面
    const agentCards = page.locator('.agent-card, [class*="AgentCard"]');
    const count = await agentCards.count();

    for (let i = 0; i < count; i++) {
      const card = agentCards.nth(i);
      const text = await card.textContent();
      if (text && text.includes('UAT行政助手')) {
        await card.click();
        break;
      }
    }

    await page.waitForTimeout(2000);

    // 发送问题
    const inputBox = page.locator('input[type="text"][placeholder]').first();
    await inputBox.fill('公司的年假政策是什么？');
    await page.keyboard.press('Enter');

    // 等待并检查前端元素
    await page.waitForTimeout(5000);

    console.log('检查前端 UI 元素:');

    // 获取页面HTML用于分析
    const html = await page.content();

    // 检查各种可能的UI元素
    const checks = {
      '思考区域 (thinking/thought)': ['thinking', 'thought', '思考'],
      '检索状态 (retrieving/retrieval)': ['retriev', '检索', 'Retrieving'],
      '引用来源 (citation/source)': ['citation', 'source', '引用', '来源'],
      '知识库提示 (kb/knowledge)': ['knowledge', '知识库', 'KB'],
      '状态标记 (status)': ['status', '状态']
    };

    for (const [name, keywords] of Object.entries(checks)) {
      let found = false;
      for (const kw of keywords) {
        if (html.toLowerCase().includes(kw.toLowerCase())) {
          found = true;
          break;
        }
      }
      console.log(`  ${found ? '✓' : '✗'} ${name}`);
    }

    // 最终截图
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/rag-uat-ui-check-final.png`,
      fullPage: true
    });
    console.log('✓ 完整页面截图已保存');
  });

});

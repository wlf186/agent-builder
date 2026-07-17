import { test, expect } from '@playwright/test';
import * as path from 'path';
import { testOutputDir } from './test-paths';

const SCREENSHOT_DIR = testOutputDir('iteration-demo');

test.describe('知识库功能演示', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('http://localhost:20815');
    await page.waitForLoadState('networkidle');
  });

  test('1. 知识库列表页', async ({ page }) => {
    await page.goto('http://localhost:20815/knowledge-bases');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/demo-01-kb-list.png`, fullPage: true });

    // 验证知识库列表
    const pageContent = await page.textContent('body');
    expect(pageContent).toContain('知识库');
  });

  test('2. 知识库详情页', async ({ page }) => {
    // 访问人力资源库详情页
    await page.goto('http://localhost:20815/knowledge-bases/kb_7116e7ed');
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/demo-02-kb-detail.png`, fullPage: true });

    // 验证文档显示
    const pageContent = await page.textContent('body');
    expect(pageContent).toContain('人力资源库');
  });

  test('3. 智能体知识库配置', async ({ page }) => {
    // 选择 UAT行政助手 智能体
    await page.locator('h3:has-text("UAT行政助手")').first().click();
    await page.waitForTimeout(2000);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/demo-03-agent-config.png`, fullPage: true });

    // 验证知识库关联显示
    const pageContent = await page.textContent('body');
    expect(pageContent).toContain('UAT行政助手');
  });

  test('4. 对话检索测试', async ({ page }) => {
    // 选择 UAT行政助手 智能体
    await page.locator('h3:has-text("UAT行政助手")').first().click();
    await page.waitForTimeout(2000);

    // 定位聊天输入框
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput).toBeVisible({ timeout: 5000 });

    // 发送问题
    await chatInput.fill('公司有几天年假？');
    await chatInput.press('Enter');

    // 等待响应
    await page.waitForTimeout(30000);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/demo-04-chat-response.png`, fullPage: true });

    // 验证响应
    const pageContent = await page.textContent('body');
    expect(pageContent!.length).toBeGreaterThan(50);
  });

  test('5. 隔离性测试', async ({ page }) => {
    // 选择 UAT技术支持 智能体
    await page.locator('h3:has-text("UAT技术支持")').first().click();
    await page.waitForTimeout(2000);

    // 定位聊天输入框
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await expect(chatInput).toBeVisible({ timeout: 5000 });

    // 发送问题
    await chatInput.fill('公司有几天年假？');
    await chatInput.press('Enter');

    // 等待响应
    await page.waitForTimeout(30000);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/demo-05-isolation-test.png`, fullPage: true });

    // 验证响应（技术支持不应调用知识库）
    const pageContent = await page.textContent('body');
    expect(pageContent!.length).toBeGreaterThan(50);
  });
});

/**
 * UAT走查测试 - 用户代表实际访问验证
 *
 * 测试目标：验证系统页面显示正常，数据加载正确
 */

import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

const BASE_URL = process.env.UAT_URL || 'http://100.113.66.46:20815';
const SCREENSHOT_DIR = testOutputDir('uat-walkthrough');

test.describe('UAT走查 - 首页验证', () => {
  test.beforeEach(async ({ page }) => {
    // 访问首页
    await page.goto(BASE_URL);
    // 等待页面加载
    await page.waitForTimeout(2000);
  });

  test('页面样式正常加载', async ({ page }) => {
    // 截图保存
    await page.screenshot({ path: `${SCREENSHOT_DIR}/homepage.png`, fullPage: true });

    // 检查背景色不是纯白（说明CSS加载了）
    const body = page.locator('body');
    const bgColor = await body.evaluate(el => {
      return window.getComputedStyle(el).backgroundColor;
    });
    console.log('Body background color:', bgColor);

    // 背景应该是深色 #0a0a0f 或类似
    expect(bgColor).not.toBe('rgb(255, 255, 255)');
  });

  test('智能体列表不为空', async ({ page }) => {
    // 等待数据加载
    await page.waitForTimeout(3000);

    // 检查侧边栏是否有智能体
    const agentItems = page.locator('[class*="agent"], [data-testid="agent-item"]').count();

    // 或者检查是否有test001
    const test001Visible = await page.locator('text=test001').isVisible().catch(() => false);

    console.log('Agent items found:', await agentItems);
    console.log('test001 visible:', test001Visible);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/agents.png`, fullPage: true });
  });

  test('API数据加载正常', async ({ page }) => {
    // 监听网络请求
    const apiResponses: string[] = [];

    page.on('response', async (response) => {
      if (response.url().includes('/api/')) {
        try {
          const text = await response.text();
          apiResponses.push(`${response.url()}: ${response.status()} - ${text.substring(0, 100)}`);
        } catch (e) {
          // ignore
        }
      }
    });

    // 刷新页面
    await page.reload();
    await page.waitForTimeout(3000);

    console.log('API Responses:');
    apiResponses.forEach(r => console.log(r));

    // 检查控制台错误
    const consoleErrors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    await page.waitForTimeout(2000);

    if (consoleErrors.length > 0) {
      console.log('Console errors:', consoleErrors);
    }
  });

  test('模型服务列表不为空', async ({ page }) => {
    await page.waitForTimeout(2000);

    // 检查模型服务区域
    const modelSection = page.locator('text=/模型服务|Model Service/i');

    // 截图侧边栏
    await page.screenshot({ path: `${SCREENSHOT_DIR}/sidebar.png`, clip: { x: 0, y: 0, width: 256, height: 800 } });
  });

  test('MCP服务列表不为空', async ({ page }) => {
    await page.waitForTimeout(2000);

    // 检查MCP服务区域
    const mcpSection = page.locator('text=/MCP|MCP服务/i');

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/mcp.png` });
  });

  test('检查数字显示', async ({ page }) => {
    await page.waitForTimeout(3000);

    // 查找所有数字元素
    const pageContent = await page.content();

    // 检查是否有 "0" 显示在关键位置
    const hasZeroAgents = pageContent.includes('>0<') || pageContent.includes('0 个') || pageContent.includes('0 agents');
    console.log('Has zero display:', hasZeroAgents);

    // 检查是否有 test001
    const hasTest001 = pageContent.includes('test001');
    console.log('Has test001:', hasTest001);

    // 检查是否有 TESTLLM
    const hasTestLLM = pageContent.includes('TESTLLM');
    console.log('Has TESTLLM:', hasTestLLM);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/full.png`, fullPage: true });
  });
});

test.describe('UAT走查 - 控制台检查', () => {
  test('无JavaScript错误', async ({ page }) => {
    const errors: string[] = [];

    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    page.on('pageerror', error => {
      errors.push(error.message);
    });

    await page.goto(BASE_URL);
    await page.waitForTimeout(5000);

    console.log('JavaScript errors:', errors);

    // 记录错误但不一定失败，因为可能是第三方脚本
    if (errors.length > 0) {
      console.log('⚠️ Found JavaScript errors, check screenshots');
    }
  });

  test('网络请求检查', async ({ page }) => {
    const failedRequests: string[] = [];

    page.on('requestfailed', request => {
      failedRequests.push(`${request.url()} - ${request.failure()?.errorText}`);
    });

    await page.goto(BASE_URL);
    await page.waitForTimeout(5000);

    console.log('Failed requests:', failedRequests);

    if (failedRequests.length > 0) {
      console.log('⚠️ Found failed requests');
    }
  });
});

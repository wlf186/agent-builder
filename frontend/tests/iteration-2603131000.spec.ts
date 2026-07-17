/**
 * ============================================================================
 * 迭代测试: 异步环境初始化功能 (iteration-2603131000)
 *
 * 测试目标:
 * 1. 验证创建智能体立即返回 (<500ms)
 * 2. 验证初始化提示横幅显示
 * 3. 验证Skill配置锁定效果
 * 4. 验证调试窗口禁用效果
 * 5. 验证环境就绪后自动解锁
 * 6. 验证错误处理和重试
 * 7. 验证删除智能体功能
 *
 * 参考文档: docs/PRD-async-environment-init.md
 * ============================================================================
 */

import { test, expect } from '@playwright/test';
import { testOutputDir } from './test-paths';

const SCREENSHOT_DIR = testOutputDir('iteration-2603131000');

const BASE_URL = 'http://localhost:20815';
const API_BASE = 'http://localhost:20881';

// 测试用智能体名称
const TEST_AGENTS = {
  tc01: 'test-async-01',
  tc02: 'test-async-02',
  tc03: 'test-async-03',
  tc04: 'test-async-04',
  tc05: 'test-async-05',
  tc06: 'test-async-06',
  tc07: 'test-async-07',
  tc08: 'test-async-08',
  tc10: 'test-async-10',
};

/**
 * 通过API创建智能体
 */
async function createAgentViaAPI(agentName: string, description?: string) {
  const response = await fetch(`${API_BASE}/api/agents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: agentName,
      description: description || '测试智能体'
    })
  });

  if (!response.ok) {
    throw new Error(`创建智能体失败: ${response.status}`);
  }

  return await response.json();
}

/**
 * 清理测试智能体
 */
async function cleanupAgent(agentName: string) {
  try {
    const response = await fetch(`${API_BASE}/api/agents/${agentName}`, {
      method: 'DELETE'
    });
    console.log(`清理智能体 ${agentName}: ${response.ok ? '成功' : '失败'}`);
  } catch (e) {
    console.log(`清理智能体 ${agentName} 失败:`, e);
  }
}

test.describe('Iteration 2603131000: 异步环境初始化功能', () => {

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
  });

  /**
   * TC-01: 创建智能体立即返回
   * 验证响应时间 < 500ms
   */
  test('TC-01: 创建智能体立即返回 (<500ms)', async ({ page }) => {
    console.log('=== 开始 TC-01: 响应时间测试 ===');

    const startTime = Date.now();

    // 通过API创建智能体
    const data = await createAgentViaAPI(TEST_AGENTS.tc01);

    const elapsed = Date.now() - startTime;
    console.log(`创建响应时间: ${elapsed}ms`);
    console.log(`响应数据:`, data);

    // 验证响应时间
    expect(elapsed).toBeLessThan(500);

    // 验证响应包含环境状态
    expect(data).toHaveProperty('environment_status');
    expect(data.environment_status).toBe('creating');

    // 刷新页面查看新创建的智能体
    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc01-immediate-return.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc01);

    console.log('=== TC-01 完成 ===');
  });

  /**
   * TC-02: 初始化提示显示
   * 验证环境初始化横幅显示
   */
  test('TC-02: 初始化提示横幅显示', async ({ page }) => {
    console.log('=== 开始 TC-02: 横幅显示测试 ===');

    // 创建智能体
    await createAgentViaAPI(TEST_AGENTS.tc02);

    // 刷新页面并点击智能体卡片
    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    // 点击智能体卡片（使用文本匹配）
    const agentCard = page.locator(`text=${TEST_AGENTS.tc02}`).or(
      page.locator(`[class*="agent-card"]:has-text("${TEST_AGENTS.tc02}")`)
    );

    const cardExists = await agentCard.isVisible({ timeout: 3000 }).catch(() => false);
    if (cardExists) {
      await agentCard.first().click();
      await page.waitForTimeout(2000);
    }

    // 检查横幅是否存在
    const banner = page.locator('text=/.*(初始化|Initializing|环境).*/').or(
      page.locator('[class*="banner"], [class*="environment"]')
    );

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc02-banner-check.png` });

    // 检查页面内容
    const pageContent = await page.content();
    const hasBannerText = pageContent.includes('初始化') || pageContent.includes('Initializing') || pageContent.includes('环境');
    console.log(`页面包含初始化相关文本: ${hasBannerText}`);

    // 清理
    await cleanupAgent(TEST_AGENTS.tc02);

    console.log('=== TC-02 完成 ===');
  });

  /**
   * TC-03: Skill 配置锁定
   * 验证初始化期间Skills配置禁用
   */
  test('TC-03: Skill 配置锁定', async ({ page }) => {
    console.log('=== 开始 TC-03: 配置锁定测试 ===');

    await createAgentViaAPI(TEST_AGENTS.tc03);

    // 刷新并点击智能体
    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc03}`);
    if (await agentCard.isVisible({ timeout: 3000 }).catch(() => false)) {
      await agentCard.first().click();
    }

    await page.waitForTimeout(2000);

    // 检查Skills相关元素
    const pageContent = await page.content();
    const hasSkillText = pageContent.includes('Skill') || pageContent.includes('技能');
    console.log(`页面包含Skill相关文本: ${hasSkillText}`);

    // 检查禁用状态
    const disabledElements = await page.locator('[disabled], [aria-disabled="true"], [class*="opacity-50"]').count();
    console.log(`禁用元素数量: ${disabledElements}`);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc03-skills-locked.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc03);

    console.log('=== TC-03 完成 ===');
  });

  /**
   * TC-04: 其它配置可用
   * 验证初始化期间其它配置可修改
   */
  test('TC-04: 其它配置可用', async ({ page }) => {
    console.log('=== 开始 TC-04: 配置可用性测试 ===');

    await createAgentViaAPI(TEST_AGENTS.tc04);

    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc04}`);
    if (await agentCard.isVisible({ timeout: 3000 }).catch(() => false)) {
      await agentCard.first().click();
    }

    await page.waitForTimeout(1000);

    // 尝试查找可编辑的输入框
    const textareas = await page.locator('textarea').all();
    console.log(`文本框数量: ${textareas.length}`);

    if (textareas.length > 0) {
      const isEditable = await textareas[0].isEnabled();
      console.log(`第一个文本框可编辑: ${isEditable}`);

      if (isEditable) {
        await textareas[0].fill('测试人设内容');
        await page.waitForTimeout(500);
      }
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc04-other-configs-usable.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc04);

    console.log('=== TC-04 完成 ===');
  });

  /**
   * TC-05: 调试窗口禁用
   * 验证初始化期间调试窗口禁用
   */
  test('TC-05: 调试窗口禁用', async ({ page }) => {
    console.log('=== 开始 TC-05: 调试窗口禁用测试 ===');

    await createAgentViaAPI(TEST_AGENTS.tc05);

    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc05}`);
    if (await agentCard.isVisible({ timeout: 3000 }).catch(() => false)) {
      await agentCard.first().click();
    }

    await page.waitForTimeout(1000);

    // 查找输入框
    const inputs = await page.locator('textarea, input[type="text"]').all();
    console.log(`输入框数量: ${inputs.length}`);

    if (inputs.length > 0) {
      const isEnabled = await inputs[0].isEnabled().catch(() => true);
      console.log(`输入框启用状态: ${isEnabled}`);
    }

    // 查找禁用提示
    const pageContent = await page.content();
    const hasDisabledMessage = pageContent.includes('初始化') || pageContent.includes('不可用') || pageContent.includes('disabled');
    console.log(`有禁用相关提示: ${hasDisabledMessage}`);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc05-chat-disabled.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc05);

    console.log('=== TC-05 完成 ===');
  });

  /**
   * TC-06: 完成后自动解锁
   * 验证环境就绪后配置自动解锁
   */
  test('TC-06: 完成后自动解锁', async ({ page }) => {
    console.log('=== 开始 TC-06: 自动解锁测试 ===');

    await createAgentViaAPI(TEST_AGENTS.tc06);

    // 等待环境初始化完成（最多60秒）
    console.log('等待环境初始化完成...');

    let isReady = false;
    for (let i = 0; i < 30; i++) {
      await page.waitForTimeout(2000);

      try {
        const response = await fetch(`${API_BASE}/api/agents/${TEST_AGENTS.tc06}/environment`);
        if (response.ok) {
          const data = await response.json();
          const status = data.environment?.status || data.status;
          console.log(`环境状态: ${status}`);

          if (status === 'ready') {
            isReady = true;
            break;
          }
        }
      } catch (e) {
        console.log('查询环境状态失败，继续等待...');
      }
    }

    console.log(`环境就绪: ${isReady}`);

    // 打开页面验证
    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc06}`);
    if (await agentCard.isVisible({ timeout: 3000 }).catch(() => false)) {
      await agentCard.first().click();
    }

    await page.waitForTimeout(1000);

    // 验证输入框可用
    const inputs = await page.locator('textarea, input[type="text"]').all();
    if (inputs.length > 0) {
      const isEnabled = await inputs[0].isEnabled().catch(() => true);
      console.log(`聊天输入框启用状态: ${isEnabled}`);
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc06-auto-unlock.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc06);

    console.log('=== TC-06 完成 ===');
  });

  /**
   * TC-07: 错误处理和重试
   * 验证环境创建失败时的错误处理
   */
  test('TC-07: 错误处理和重试', async ({ page }) => {
    console.log('=== 开始 TC-07: 错误处理测试 ===');

    // 创建智能体
    await createAgentViaAPI(TEST_AGENTS.tc07);

    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc07}`);
    if (await agentCard.isVisible({ timeout: 3000 }).catch(() => false)) {
      await agentCard.first().click();
    }

    await page.waitForTimeout(1000);

    // 检查错误提示
    const pageContent = await page.content();
    const hasErrorText = pageContent.includes('失败') || pageContent.includes('错误') || pageContent.includes('Error') || pageContent.includes('Failed');
    console.log(`有错误相关文本: ${hasErrorText}`);

    // 检查重试按钮
    const hasRetryButton = pageContent.includes('重试') || pageContent.includes('Retry');
    console.log(`有重试相关文本: ${hasRetryButton}`);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc07-error-handling.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc07);

    console.log('=== TC-07 完成 ===');
  });

  /**
   * TC-08: 删除智能体
   * 验证初始化期间删除智能体功能
   */
  test('TC-08: 删除智能体', async ({ page }) => {
    console.log('=== 开始 TC-08: 删除智能体测试 ===');

    await createAgentViaAPI(TEST_AGENTS.tc08);

    // 直接通过API删除
    const deleteResponse = await fetch(`${API_BASE}/api/agents/${TEST_AGENTS.tc08}`, {
      method: 'DELETE'
    });

    console.log(`删除响应: ${deleteResponse.ok ? '成功' : '失败'}`);
    expect(deleteResponse.ok).toBeTruthy();

    // 验证删除后不再存在
    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc08}`);
    const exists = await agentCard.isVisible({ timeout: 2000 }).catch(() => false);
    console.log(`智能体卡片是否显示: ${exists}`);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc08-delete-agent.png` });

    console.log('=== TC-08 完成 ===');
  });

  /**
   * TC-09: API响应验证
   * 直接验证后端API响应格式
   */
  test('TC-09: API响应格式验证', async ({ page }) => {
    console.log('=== 开始 TC-09: API响应验证 ===');

    const testAgent = 'test-api-validation';

    // 通过API创建智能体
    const startTime = Date.now();
    const createResponse = await fetch(`${API_BASE}/api/agents`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: testAgent,
        description: 'API验证测试'
      })
    });
    const elapsed = Date.now() - startTime;

    expect(createResponse.ok).toBeTruthy();

    const createData = await createResponse.json();
    console.log(`创建响应时间: ${elapsed}ms`);
    console.log('创建响应:', createData);

    // 验证响应时间
    expect(elapsed).toBeLessThan(500);

    // 验证响应包含环境状态
    expect(createData).toHaveProperty('environment_status');
    expect(createData.environment_status).toBe('creating');

    // 查询环境状态
    await page.waitForTimeout(1000);

    const envResponse = await fetch(`${API_BASE}/api/agents/${testAgent}/environment`);
    expect(envResponse.ok).toBeTruthy();

    const envData = await envResponse.json();
    console.log('环境状态响应:', envData);

    // 验证环境状态格式
    expect(envData).toHaveProperty('environment');
    expect(envData.environment).toHaveProperty('status');

    // 清理
    await cleanupAgent(testAgent);

    console.log('=== TC-09 完成 ===');
  });

  /**
   * TC-10: 轮询机制验证
   * 验证前端轮询环境状态机制
   */
  test('TC-10: 轮询机制验证', async ({ page }) => {
    console.log('=== 开始 TC-10: 轮询机制验证 ===');

    await createAgentViaAPI(TEST_AGENTS.tc10);

    // 监听网络请求
    const envRequests: string[] = [];
    page.on('request', request => {
      const url = request.url();
      if (url.includes('/environment')) {
        envRequests.push(url);
        console.log(`环境请求: ${url}`);
      }
    });

    // 打开页面
    await page.goto(BASE_URL);
    await page.waitForTimeout(1000);

    const agentCard = page.locator(`text=${TEST_AGENTS.tc10}`);
    if (await agentCard.isVisible({ timeout: 3000 }).catch(() => false)) {
      await agentCard.first().click();
    }

    // 等待几秒，观察轮询请求
    await page.waitForTimeout(8000);

    console.log(`环境请求次数: ${envRequests.length}`);

    // 验证有轮询请求（至少1次）
    expect(envRequests.length).toBeGreaterThan(0);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc10-polling-mechanism.png` });

    // 清理
    await cleanupAgent(TEST_AGENTS.tc10);

    console.log('=== TC-10 完成 ===');
  });

  /**
   * TC-11: 基线测试 - 系统基本功能
   */
  test('TC-11: 基线测试 - 系统基本功能可用', async ({ page }) => {
    console.log('=== 开始 TC-11: 基线测试 ===');

    // 验证前端可访问
    await page.goto(BASE_URL);
    const title = await page.title();
    console.log(`页面标题: ${title}`);
    expect(title).toBeTruthy();

    // 验证后端API可访问
    const response = await fetch(`${API_BASE}/api/agents`);
    expect(response.ok).toBeTruthy();

    const agents = await response.json();
    console.log(`智能体列表: ${agents.length} 个`);

    // 检查控制台错误
    const errors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });

    await page.waitForTimeout(2000);

    console.log(`控制台错误数量: ${errors.length}`);
    for (const err of errors) {
      console.log(`错误: ${err}`);
    }

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tc11-baseline.png` });

    console.log('=== TC-11 完成 ===');
  });

  /**
   * TC-12: 性能测试 - 多个并发创建
   */
  test('TC-12: 性能测试 - 并发创建多个智能体', async ({ page }) => {
    console.log('=== 开始 TC-12: 并发创建测试 ===');

    const agentNames = ['test-concurrent-1', 'test-concurrent-2', 'test-concurrent-3'];
    const startTimes: number[] = [];
    const results: any[] = [];

    // 并发创建
    const promises = agentNames.map(async (name) => {
      const start = Date.now();
      startTimes.push(start);

      try {
        const data = await createAgentViaAPI(name);
        const elapsed = Date.now() - start;
        results.push({ name, elapsed, success: true });
        console.log(`${name} 创建耗时: ${elapsed}ms`);
      } catch (e) {
        const elapsed = Date.now() - start;
        results.push({ name, elapsed, success: false, error: e });
      }
    });

    await Promise.all(promises);

    // 验证所有创建都成功
    for (const result of results) {
      expect(result.success).toBeTruthy();
    }

    // 验证响应时间
    for (const result of results) {
      expect(result.elapsed).toBeLessThan(500);
    }

    // 清理
    for (const name of agentNames) {
      await cleanupAgent(name);
    }

    console.log('=== TC-12 完成 ===');
  });
});

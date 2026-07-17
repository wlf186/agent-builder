import { test, expect } from '@playwright/test';
import { testOutputPath } from './test-paths';

test('验证智能体列表 - 只显示已配置模型服务的智能体', async ({ page }) => {
  test.setTimeout(60000);

  // 访问主页
  await page.goto('http://localhost:20815');
  await page.waitForLoadState('networkidle');

  // 等待智能体卡片加载
  await page.waitForTimeout(3000);

  // 获取所有智能体名称
  const agentCards = await page.locator('h3').allTextContents();
  console.log('前端显示的智能体:', agentCards);

  // 验证应该存在的智能体
  expect(agentCards).toContain('test3');
  expect(agentCards).toContain('test-model-check');
  expect(agentCards).toContain('finance-sub');
  expect(agentCards).toContain('main-agent');

  // 验证不应该存在的智能体（已删除的）
  expect(agentCards).not.toContain('test001');
  expect(agentCards).not.toContain('test-iteration-2603111255');
  expect(agentCards).not.toContain('test007');
  expect(agentCards).not.toContain('test2');

  // 验证"智能体人设"标题
  const personaText = await page.locator('text=智能体人设').first().textContent();
  expect(personaText).toContain('智能体人设');

  // 截图
  await page.screenshot({
    path: testOutputPath('verify-agents', 'list.png'),
    fullPage: true
  });

  console.log('✅ 验证通过 - 智能体列表正确');
});

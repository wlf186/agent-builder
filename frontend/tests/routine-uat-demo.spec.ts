/**
 * Agent Builder 例行Demo演示
 * 触发：用户输入"例行demo"
 * 运行：npx playwright test routine-uat-demo.spec.ts --headed
 */

import { test, expect, Page, Locator } from '@playwright/test';
import * as fs from 'fs';
import path from 'node:path';
import { AC130_PDF_FIXTURE, testOutputDir } from './test-paths';

const BASE_URL = 'http://localhost:20815';
const OUTPUT_DIR = testOutputDir('routine-uat-demo');
const PDF_FILE = AC130_PDF_FIXTURE;
const AGENT_NAME = 'DEMO';
const QUICK = 5000;   // 普通操作 5s
const LLM = 60000;    // LLM响应 60s
const INIT = 30000;   // 环境初始化 30s

function ts(): string {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth()+1).padStart(2,'0')}${String(d.getDate()).padStart(2,'0')}${String(d.getHours()).padStart(2,'0')}${String(d.getMinutes()).padStart(2,'0')}${String(d.getSeconds()).padStart(2,'0')}`;
}

async function saveBug(page: Page, step: string, err: string): Promise<string> {
  const dir = path.join(OUTPUT_DIR, 'bugs', ts());
  fs.mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/screenshot.png`, fullPage: true });
  fs.writeFileSync(`${dir}/error.txt`, `步骤: ${step}\n错误: ${err}`);
  return dir;
}

async function saveOk(page: Page): Promise<string> {
  const dir = path.join(OUTPUT_DIR, 'success', ts());
  fs.mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/screenshot.png`, fullPage: true });
  return dir;
}

test.use({ headless: false });
test.setTimeout(300000);  // 总超时 5分钟

test('例行demo', async ({ page }) => {
  console.log('\n========================================');
  console.log('🚀 Agent Builder 例行Demo');
  console.log('========================================\n');

  // [1/8] 访问主页
  console.log('📍 [1/8] 访问主页...');
  await page.goto(BASE_URL, { timeout: QUICK });
  await page.waitForLoadState('networkidle', { timeout: QUICK });
  console.log('  ✅ 主页加载成功');

  // [2/8] 删除已存在的DEMO智能体（如果有）
  console.log('\n📍 [2/8] 检查并删除已存在的DEMO智能体...');
  const existingAgent = page.locator('h3').filter({ hasText: /^DEMO$/ });
  const agentCount = await existingAgent.count();

  if (agentCount > 0) {
    console.log(`  发现 ${agentCount} 个已存在的DEMO智能体`);
    for (let i = 0; i < agentCount; i++) {
      const agentCard = existingAgent.nth(i);
      await agentCard.click();
      console.log(`  点击第 ${i+1} 个DEMO智能体`);
      await page.waitForTimeout(500);

      // 查找删除按钮
      const deleteBtn = page.getByRole('button', { name: /删除|Delete/ }).first();
      if (await deleteBtn.isVisible().catch(() => false)) {
        await deleteBtn.click();
        console.log(`  点击删除`);
        await page.waitForTimeout(500);

        // 确认删除
        const confirmDelete = page.getByRole('button', { name: /确认|确定|OK/ }).first();
        if (await confirmDelete.isVisible().catch(() => false)) {
          await confirmDelete.click();
          console.log(`  ✅ 已删除第 ${i+1} 个`);
        }
        await page.waitForTimeout(1000);
      }
    }
    // 刷新页面以确保列表更新
    await page.reload({ timeout: QUICK });
    await page.waitForLoadState('networkidle', { timeout: QUICK });
    console.log('  ✅ 清理完成');
  } else {
    console.log('  未找到已存在的DEMO智能体，跳过删除');
  }

  // [3/8] 创建智能体
  console.log('\n📍 [3/8] 创建智能体...');

  // 点击新建按钮
  const newBtn = page.getByRole('button', { name: /新建|创建|\+/ }).first();
  await newBtn.waitFor({ state: 'visible', timeout: QUICK });
  await newBtn.click();
  console.log('  点击新建');
  await page.waitForTimeout(1000);  // 等待模态框动画

  // 输入名称（使用 placeholder 定位模态框中的输入框）
  const nameInput = page.getByPlaceholder(/例如：代码助手|e\.g\., Code Assistant/).first();
  await nameInput.waitFor({ state: 'visible', timeout: QUICK });
  await nameInput.fill(AGENT_NAME);
  console.log(`  输入: ${AGENT_NAME}`);
  await page.waitForTimeout(300);

  // 点击确认按钮（模态框中的创建按钮）
  const confirmBtn = page.getByRole('button', { name: /创建/ }).first();
  await confirmBtn.click();
  console.log('  点击创建');

  // 等待初始化
  console.log(`  等待初始化（${INIT/1000}s）...`);
  const t0 = Date.now();
  while (Date.now() - t0 < INIT) {
    const body = await page.textContent('body') || '';
    if (body.includes('环境就绪') || body.includes('已就绪')) {
      console.log('  ✅ 环境就绪');
      break;
    }
    const loading = await page.locator('text=/初始化中|正在创建|loading/i').isVisible().catch(() => false);
    if (!loading && Date.now() - t0 > 5000) {
      console.log('  ✅ 页面稳定');
      break;
    }
    await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(1000);

  // [3/8] 配置工具
  console.log('\n📍 [3/8] 配置工具...');
  await page.evaluate(() => window.scrollTo(0, 500));
  await page.waitForTimeout(500);

  // TE47
  const te47 = page.locator('label, [class*="check"]').filter({ hasText: 'TE47' }).first();
  if (await te47.isVisible().catch(() => false)) { await te47.click(); console.log('  ✓ TE47'); }

  // MCP
  for (const t of ['calculator', 'cold-jokes', 'coingecko']) {
    const cb = page.locator('label, [class*="check"]').filter({ hasText: t }).first();
    if (await cb.isVisible().catch(() => false)) { await cb.click(); console.log(`  ✓ ${t}`); }
    await page.waitForTimeout(200);
  }

  // Skills
  for (const s of ['ab-docx', 'ab-pdf']) {
    const cb = page.locator('label, [class*="check"]').filter({ hasText: s }).first();
    if (await cb.isVisible().catch(() => false)) { await cb.click(); console.log(`  ✓ ${s}`); }
    await page.waitForTimeout(200);
  }

  // [4/8] 保存
  console.log('\n📍 [4/8] 保存配置...');
  const saveBtn = page.getByRole('button', { name: /保存|Save/ }).first();
  if (await saveBtn.isVisible().catch(() => false)) {
    await saveBtn.click();
    await page.waitForTimeout(500);
    console.log('  ✅ 已保存');
  }

  // 定位聊天输入框
  const chatInput = page.locator('input[type="text"][placeholder]').first();
  await expect(chatInput).toBeVisible({ timeout: QUICK });

  // 4轮对话
  const rounds = [
    { msg: '32748+392/2+1是多少', tool: 'calculator' },
    { msg: '讲2个冷笑话', tool: 'cold-jokes' },
    { msg: 'ETH的最新价格', tool: 'coingecko' },
    { msg: '提取这篇文档的前200字', tool: 'ab-pdf', upload: true }
  ];

  for (let i = 0; i < rounds.length; i++) {
    const r = rounds[i];
    console.log(`\n📍 [${i+5}/8] 第${i+1}轮: ${r.tool}`);

    if (r.upload) {
      console.log('  上传PDF...');
      await page.locator('input[type="file"]').first().setInputFiles(PDF_FILE);
      await page.waitForTimeout(2000);
      console.log('  ✓ 已上传');
    }

    await chatInput.fill(r.msg);
    await page.waitForTimeout(300);
    await chatInput.press('Enter');
    console.log(`  发送: "${r.msg}"`);

    console.log(`  等待响应（${LLM/1000}s）...`);
    const t1 = Date.now();
    let ok = false;
    while (Date.now() - t1 < LLM) {
      const body = await page.textContent('body') || '';
      const hasTool = body.toLowerCase().includes(r.tool.split('-')[0]);
      const noThink = !(await page.locator('text=/思考中|thinking/i').isVisible().catch(() => false));
      if (hasTool && noThink && body.length > 3000) { ok = true; break; }
      await page.waitForTimeout(1000);
    }

    if (!ok) {
      const bug = await saveBug(page, `第${i+1}轮`, '超时');
      console.log(`\n  ❌ 失败: ${bug}`);
      console.log('  💡 修复后重试\n');
      await page.pause();
      throw new Error(`第${i+1}轮失败`);
    }
    console.log('  ✅ 正常');
    await page.waitForTimeout(1000);
  }

  // 成功
  const okDir = await saveOk(page);
  console.log('\n========================================');
  console.log('🎉 例行Demo成功！');
  console.log('========================================');
  console.log(`📸 ${okDir}`);
  console.log('⏳ 5分钟后关闭\n');
  await page.waitForTimeout(300000);
  console.log('👋 结束');
});

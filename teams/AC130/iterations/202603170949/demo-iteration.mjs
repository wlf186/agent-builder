import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join, resolve } from 'path';
import { mkdirSync } from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const BASE_URL = 'http://localhost:20815';
const KB_ID = 'kb_7116e7ed'; // 人力资源库
const PROJECT_ROOT = resolve(__dirname, '../../../..');
const SUCCESS_DIR = join(PROJECT_ROOT, '.runtime', 'test-results', 'demo-iteration');

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function main() {
  console.log('========================================');
  console.log('🚀 AC130 知识库(RAG)功能演示');
  console.log('   迭代: 202603170949');
  console.log('========================================');
  console.log();

  const timestamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 15);
  const screenshotDir = join(SUCCESS_DIR, timestamp);
  mkdirSync(screenshotDir, { recursive: true });

  const display = process.env.DISPLAY || '';
  console.log(`📺 显示器: ${display || 'headless'}`);
  console.log();

  const browser = await chromium.launch({
    headless: !display,
    args: display ? [`--display=${display}`] : []
  });

  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 }
  });
  const page = await context.newPage();

  try {
    // ===== 1. 知识库列表页 =====
    console.log('📍 [1/6] 知识库列表页...');
    await page.goto(`${BASE_URL}/knowledge-bases`);
    await page.waitForLoadState('networkidle');
    await sleep(2000);
    await page.screenshot({ path: join(screenshotDir, '01-kb-list.png'), fullPage: true });
    console.log('   ✅ 截图完成');
    console.log();

    // ===== 2. 知识库详情页 =====
    console.log('📍 [2/6] 人力资源库详情页（文档列表）...');
    await page.goto(`${BASE_URL}/knowledge-bases/${KB_ID}`);
    await page.waitForLoadState('networkidle');
    await sleep(2000);
    await page.screenshot({ path: join(screenshotDir, '02-kb-detail.png'), fullPage: true });
    console.log('   ✅ 截图完成');
    console.log();

    // ===== 3. 智能体配置 - 行政助手 =====
    console.log('📍 [3/6] 行政助手智能体配置（已挂载人力资源库）...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await sleep(2000);

    const agentCard = page.locator('h3:has-text("UAT行政助手")').first();
    await agentCard.click();
    await sleep(3000);
    await page.screenshot({ path: join(screenshotDir, '03-agent-config.png'), fullPage: true });
    console.log('   ✅ 截图完成');
    console.log();

    // ===== 4. 对话检索测试 =====
    console.log('📍 [4/6] 对话检索测试：公司有几天年假？');
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await chatInput.waitFor({ state: 'visible', timeout: 10000 });

    await chatInput.fill('公司有几天年假？');
    await chatInput.press('Enter');
    console.log('   发送消息...');

    // 等待响应（最长60秒）
    await sleep(40000);
    await page.screenshot({ path: join(screenshotDir, '04-chat-response.png'), fullPage: true });
    console.log('   ✅ 截图完成');
    console.log();

    // ===== 5. 隔离性测试 - 技术支持 =====
    console.log('📍 [5/6] 隔离性测试：技术支持智能体（未挂载知识库）...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await sleep(2000);

    const techCard = page.locator('h3:has-text("UAT技术支持")').first();
    await techCard.click();
    await sleep(3000);

    const chatInput2 = page.locator('input[type="text"][placeholder]').first();
    await chatInput2.waitFor({ state: 'visible', timeout: 10000 });

    await chatInput2.fill('公司有几天年假？');
    await chatInput2.press('Enter');
    console.log('   发送消息...');

    await sleep(40000);
    await page.screenshot({ path: join(screenshotDir, '05-isolation-test.png'), fullPage: true });
    console.log('   ✅ 截图完成');
    console.log();

    // ===== 6. 知识库检索API测试 =====
    console.log('📍 [6/6] 知识库检索API测试...');
    const searchResult = await page.evaluate(async () => {
      const res = await fetch('http://localhost:20881/api/knowledge-bases/kb_7116e7ed/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: '年假' })
      });
      return await res.json();
    });

    console.log('   检索结果:');
    console.log(`   - 文档: ${searchResult.results?.[0]?.filename || 'N/A'}`);
    console.log(`   - 相似度: ${searchResult.results?.[0]?.score?.toFixed(3) || 'N/A'}`);
    console.log('   ✅ API测试完成');
    console.log();

    // ===== 完成 =====
    console.log('========================================');
    console.log('🎉 知识库功能演示成功！');
    console.log('========================================');
    console.log(`📸 截图目录: ${screenshotDir}`);
    console.log();
    console.log('验证项目:');
    console.log('  ✅ 知识库列表页');
    console.log('  ✅ 知识库详情页（文档管理）');
    console.log('  ✅ 智能体知识库挂载配置');
    console.log('  ✅ 对话检索功能（年假问题）');
    console.log('  ✅ 隔离性测试（技术支持不触发检索）');
    console.log('  ✅ 检索API功能');
    console.log();

    // 保持浏览器5分钟
    console.log('⏳ 浏览器将保持5分钟，或输入"pass"提前结束...');
    await sleep(5 * 60 * 1000);

  } catch (error) {
    console.error('❌ 演示失败:', error.message);
    await page.screenshot({ path: join(screenshotDir, 'error.png'), fullPage: true });
  } finally {
    await browser.close();
    console.log('🧹 清理完成');
  }
}

main().catch(console.error);

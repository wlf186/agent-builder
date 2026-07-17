import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join, resolve } from 'path';
import { mkdirSync } from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const BASE_URL = 'http://localhost:20815';
const PROJECT_ROOT = resolve(__dirname, '../../../..');
const SUCCESS_DIR = join(PROJECT_ROOT, '.runtime', 'test-results', 'demo-rag');
const KB_ID = 'kb_7116e7ed'; // 人力资源库

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function main() {
  console.log('========================================');
  console.log('🚀 知识库(RAG)功能演示');
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
    // 1. 知识库列表页
    console.log('📍 [1/5] 访问知识库列表页...');
    await page.goto(`${BASE_URL}/knowledge-bases`);
    await page.waitForLoadState('networkidle');
    await sleep(2000);
    await page.screenshot({ path: join(screenshotDir, '01-kb-list.png'), fullPage: true });
    console.log('  ✅ 知识库列表页截图完成');

    // 2. 知识库详情页
    console.log('📍 [2/5] 访问人力资源库详情页...');
    await page.goto(`${BASE_URL}/knowledge-bases/${KB_ID}`);
    await page.waitForLoadState('networkidle');
    await sleep(2000);
    await page.screenshot({ path: join(screenshotDir, '02-kb-detail.png'), fullPage: true });
    console.log('  ✅ 知识库详情页截图完成');

    // 3. 智能体配置页
    console.log('📍 [3/5] 访问智能体配置页...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await sleep(2000);

    // 点击 UAT行政助手
    const agentCard = page.locator('h3:has-text("UAT行政助手")').first();
    await agentCard.click();
    await sleep(3000);
    await page.screenshot({ path: join(screenshotDir, '03-agent-config.png'), fullPage: true });
    console.log('  ✅ 智能体配置页截图完成');

    // 4. 对话检索测试
    console.log('📍 [4/5] 对话检索测试...');
    const chatInput = page.locator('input[type="text"][placeholder]').first();
    await chatInput.waitFor({ state: 'visible', timeout: 5000 });

    await chatInput.fill('公司有几天年假？');
    await chatInput.press('Enter');
    console.log('  发送: "公司有几天年假？"');

    await sleep(30000); // 等待响应
    await page.screenshot({ path: join(screenshotDir, '04-chat-response.png'), fullPage: true });
    console.log('  ✅ 对话响应截图完成');

    // 5. 隔离性测试
    console.log('📍 [5/5] 隔离性测试...');
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await sleep(2000);

    const techCard = page.locator('h3:has-text("UAT技术支持")').first();
    await techCard.click();
    await sleep(3000);

    const chatInput2 = page.locator('input[type="text"][placeholder]').first();
    await chatInput2.waitFor({ state: 'visible', timeout: 5000 });

    await chatInput2.fill('公司有几天年假？');
    await chatInput2.press('Enter');
    console.log('  发送: "公司有几天年假？"（技术支持智能体）');

    await sleep(30000);
    await page.screenshot({ path: join(screenshotDir, '05-isolation-test.png'), fullPage: true });
    console.log('  ✅ 隔离性测试截图完成');

    console.log();
    console.log('========================================');
    console.log('🎉 知识库功能演示成功！');
    console.log('========================================');
    console.log(`📸 截图目录: ${screenshotDir}`);
    console.log();

    // 保持浏览器5分钟
    console.log('⏳ 浏览器将保持5分钟...');
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

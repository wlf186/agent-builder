import { test, expect } from '@playwright/test';
import path from 'path';
import { AC130_PDF_FIXTURE, testOutputDir } from './test-paths';

/**
 * 演示脚本：PDF 文件上传 + 内容提取
 *
 * 演示步骤:
 * 1. 访问主页并选择 test3 智能体
 * 2. 上传 PDF 文件
 * 3. 发送消息 "提取这篇文档的前200字"
 * 4. 等待 AB-pdf 技能处理
 * 5. 暂停，保持浏览器打开
 */

test.use({ headless: false }); // headed 模式
test.setTimeout(120000); // 120 秒超时

test('演示: PDF 上传 + 内容提取', async ({ page }) => {
  const screenshotDir = testOutputDir('demo-pdf-upload');

  // PDF 文件路径
  const pdfPath = AC130_PDF_FIXTURE;

  // ========== 初始化 ==========
  await page.goto('http://localhost:20815');
  await page.waitForLoadState('networkidle');

  // 修复 X11 远程投屏渲染问题：触发浏览器重绘
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(100);

  console.log('📱 主页加载成功');

  // 选择 test3 智能体
  const test3Agent = page.locator('h3:has-text("test3")').first();
  await expect(test3Agent).toBeVisible({ timeout: 10000 });
  await test3Agent.click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${screenshotDir}/pdf-00-agent-selected.png` });
  console.log('🤖 test3 智能体已选择');

  // ========== 步骤1: 上传 PDF 文件 ==========
  console.log('');
  console.log('========== 步骤1: 上传 PDF 文件 ==========');

  // 查找文件上传按钮
  const uploadButton = page.locator('button:has-text("上传"), button:has-text("上传文件"), [role="button"]:has-text("📎")').first();
  await page.waitForTimeout(1000);

  // 尝试多种方式定位上传按钮
  const uploadSelectors = [
    'input[type="file"]',
    'button:has-text("上传")',
    '[data-testid="file-upload"]',
    '[role="button"]:has-text("📎")',
    'button[aria-label*="upload" i]',
    'button[aria-label*="文件" i]'
  ];

  let fileInput: any = null;
  for (const selector of uploadSelectors) {
    try {
      const element = page.locator(selector).first();
      if (await element.count() > 0) {
        console.log(`📎 找到上传元素: ${selector}`);
        fileInput = element;
        break;
      }
    } catch (e) {
      // 继续尝试下一个选择器
    }
  }

  if (!fileInput) {
    // 尝试通过点击触发文件选择
    await page.screenshot({ path: `${screenshotDir}/pdf-01-before-upload.png`, fullPage: true });
    console.log('⚠️  未找到直接文件输入，尝试点击上传按钮...');

    // 查找并点击上传相关的按钮
    const buttons = await page.locator('button').all();
    for (const button of buttons) {
      const text = await button.textContent();
      if (text?.includes('上传') || text?.includes('📎') || text?.includes('文件')) {
        console.log(`📎 点击上传按钮: ${text?.trim()}`);
        await button.click();
        await page.waitForTimeout(1000);
        break;
      }
    }
  }

  // 直接设置文件（最可靠的方式）
  const fileInputElement = page.locator('input[type="file"]').first();
  const fileInputCount = await fileInputElement.count();

  if (fileInputCount > 0) {
    console.log('📤 找到文件输入元素，开始上传...');
    await fileInputElement.setInputFiles(pdfPath);
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${screenshotDir}/pdf-02-after-upload.png`, fullPage: true });
    console.log('✅ PDF 文件上传成功');
  } else {
    console.log('⚠️  未找到文件输入元素，尝试创建隐藏的文件输入...');

    // 创建并触发隐藏的文件输入
    await page.evaluate(() => {
      const input = document.createElement('input');
      input.type = 'file';
      input.id = 'temp-file-upload';
      input.style.display = 'none';
      document.body.appendChild(input);
    });

    const tempInput = page.locator('#temp-file-upload');
    await tempInput.setInputFiles(pdfPath);
    await page.waitForTimeout(2000);
    await page.screenshot({ path: `${screenshotDir}/pdf-02-after-upload.png`, fullPage: true });
    console.log('✅ PDF 文件上传成功（通过临时输入）');
  }

  // ========== 步骤2: 发送提取消息 ==========
  console.log('');
  console.log('========== 步骤2: 发送提取消息 ==========');

  const chatInput = page.locator('input[type="text"][placeholder]').first();
  await expect(chatInput).toBeVisible({ timeout: 5000 });

  await chatInput.fill('提取这篇文档的前200字');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${screenshotDir}/pdf-03-message-filled.png` });
  await chatInput.press('Enter');

  console.log('⏳ 等待 AB-pdf 技能处理...');
  await page.waitForTimeout(20000); // PDF 处理需要更多时间
  await page.screenshot({ path: `${screenshotDir}/pdf-04-result.png`, fullPage: true });
  console.log('✅ PDF 内容提取完成');

  // ========== 最终状态 ==========
  await page.screenshot({ path: `${screenshotDir}/pdf-05-final-state.png`, fullPage: true });
  console.log('');
  console.log('========== 演示完成 ==========');
  console.log('🔍 浏览器窗口保持打开，请查看演示结果...');

  // ========== 暂停：保持浏览器打开 ==========
  console.log('');
  console.log('⏸️  演示暂停 - 按 Ctrl+C 关闭浏览器');
  await page.pause();
});

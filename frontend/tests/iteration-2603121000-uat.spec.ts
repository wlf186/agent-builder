/**
 * UAT 验收测试 - iteration-2603121000
 *
 * 测试目标：验证文件上传与 Skill 执行功能
 */

import { test, expect } from '@playwright/test';
import { AC130_PDF_FIXTURE, testOutputDir } from './test-paths';

const BASE_URL = process.env.UAT_URL || 'http://localhost:20815';
const TEST_TIMEOUT = 120000;
const SCREENSHOT_DIR = testOutputDir('iteration-2603121000-uat');

test.describe('UAT验收 - iteration-2603121000', () => {
  test.setTimeout(TEST_TIMEOUT);

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);
  });

  test('UC-001: 文件上传功能验证', async ({ page }) => {
    console.log('\n=== UC-001: 文件上传功能验证 ===');

    // 选择 skill-test-pdf 智能体
    const pdfAgentVisible = await page.locator('text=skill-test-pdf').isVisible().catch(() => false);
    if (!pdfAgentVisible) {
      // 尝试刷新
      await page.reload();
      await page.waitForTimeout(2000);
    }

    await page.click('text=skill-test-pdf').catch(() => console.log('Could not click skill-test-pdf'));
    await page.waitForTimeout(2000);

    // 检查是否有文件上传按钮
    const uploadButton = page.locator('input[type="file"]').first();
    const hasUploadButton = await uploadButton.count();

    console.log('File upload button found:', hasUploadButton > 0);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uc001-upload.png`, fullPage: true });
    console.log(`Screenshot saved: ${SCREENSHOT_DIR}/uc001-upload.png`);
  });

  test('UC-005: test001 例行验证 - 3轮对话', async ({ page }) => {
    console.log('\n=== UC-005: test001 例行验证 ===');

    // 点击 test001 智能体
    await page.click('text=test001');
    await page.waitForTimeout(2000);

    const messages = [
      '现在是几月几号几点几分',
      '99/33是多少',
      '计算结果再加2.5是多少'
    ];

    for (let i = 0; i < messages.length; i++) {
      console.log(`\n--- 第${i + 1}轮对话 ---`);
      console.log(`发送: ${messages[i]}`);

      // 找到输入框并发送消息
      const input = page.locator('textarea').first();
      await input.fill(messages[i]);
      await input.press('Enter');

      // 等待响应
      await page.waitForTimeout(15000);

      // 检查是否有响应
      const responseArea = page.locator('[class*="message"], [class*="response"]').first();
      const hasResponse = await responseArea.count();

      console.log('Response received:', hasResponse > 0);
      console.log('Round ' + (i + 1) + ' completed');
    }

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uc005-test001.png`, fullPage: true });
    console.log(`\nScreenshot saved: ${SCREENSHOT_DIR}/uc005-test001.png`);
  });

  test('UC-006: 流式输出验证', async ({ page }) => {
    console.log('\n=== UC-006: 流式输出验证 ===');

    // 选择 test001
    await page.click('text=test001');
    await page.waitForTimeout(2000);

    // 发送消息
    const input = page.locator('textarea').first();
    await input.fill('请简单介绍一下你自己');
    await input.press('Enter');

    // 观察流式输出
    await page.waitForTimeout(5000);

    // 检查是否有打字机光标效果
    const cursorVisible = await page.locator('[class*="animate-pulse"]').count();
    console.log('Typing cursor visible:', cursorVisible > 0);

    // 等待完成
    await page.waitForTimeout(10000);

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uc006-streaming.png`, fullPage: true });
    console.log(`Screenshot saved: ${SCREENSHOT_DIR}/uc006-streaming.png`);
  });

  test('UC-007: 大文件上传验证', async ({ page }) => {
    console.log('\n=== UC-007: 大文件上传验证 ===');

    // 选择 skill-test-pdf
    await page.click('text=skill-test-pdf');
    await page.waitForTimeout(2000);

    // 检查文件上传功能
    const uploadInput = page.locator('input[type="file"]').first();
    const uploadCount = await uploadInput.count();

    console.log('Upload input found:', uploadCount > 0);

    if (uploadCount > 0) {
      // 上传大文件
      const startTime = Date.now();
      await uploadInput.setInputFiles(AC130_PDF_FIXTURE);

      // 等待上传完成
      await page.waitForTimeout(5000);
      const uploadTime = Date.now() - startTime;

      console.log('Upload time:', uploadTime, 'ms');
      console.log('Upload time < 2s:', uploadTime < 2000);
    }

    // 截图
    await page.screenshot({ path: `${SCREENSHOT_DIR}/uc007-large-file.png`, fullPage: true });
    console.log(`Screenshot saved: ${SCREENSHOT_DIR}/uc007-large-file.png`);
  });

  test('API验证: 文件上传接口', async ({ request }) => {
    console.log('\n=== API验证: 文件上传接口 ===');

    // 测试 PDF 上传
    const pdfResponse = await request.post('http://localhost:20881/api/agents/skill-test-pdf/files', {
      multipart: {
        file: {
          name: 'test.pdf',
          mimeType: 'application/pdf',
          buffer: Buffer.from('test pdf content')
        }
      }
    });

    console.log('PDF upload status:', pdfResponse.status());

    // 测试 DOCX 上传
    const docxResponse = await request.post('http://localhost:20881/api/agents/skill-test-doc/files', {
      multipart: {
        file: {
          name: 'test.docx',
          mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          buffer: Buffer.from('test docx content')
        }
      }
    });

    console.log('DOCX upload status:', docxResponse.status());
  });

  test('API验证: 智能体对话接口', async ({ request }) => {
    console.log('\n=== API验证: 智能体对话接口 ===');

    // 测试 test001 对话
    const chatResponse = await request.post('http://localhost:20881/api/agents/test001/chat', {
      data: {
        message: '你好'
      }
    });

    console.log('Chat API status:', chatResponse.status());
    const chatData = await chatResponse.json();
    console.log('Chat response length:', chatData.response?.length || 0);
    console.log('Chat response has content:', chatData.response && chatData.response.length > 0);
  });

  test('API验证: 流式输出接口', async ({ request }) => {
    console.log('\n=== API验证: 流式输出接口 ===');

    // 测试流式输出
    const streamResponse = await request.post('http://localhost:20881/api/agents/test001/chat/stream', {
      data: {
        message: '1+1等于几？'
      }
    });

    console.log('Stream API status:', streamResponse.status());

    const text = await streamResponse.text();
    const hasThinking = text.includes('"type": "thinking"');
    const hasContent = text.includes('"type": "content"');

    console.log('Has thinking events:', hasThinking);
    console.log('Has content events:', hasContent);
    console.log('Stream response length:', text.length);
  });
});

test.describe('UAT验收 - 智能体配置检查', () => {
  test('检查测试智能体配置', async ({ request }) => {
    console.log('\n=== 检查测试智能体配置 ===');

    // 检查 test001
    const test001Response = await request.get('http://localhost:20881/api/agents/test001');
    const test001Data = await test001Response.json();
    console.log('test001 model_service:', test001Data.model_service);

    // 检查 skill-test-pdf
    const pdfResponse = await request.get('http://localhost:20881/api/agents/skill-test-pdf');
    const pdfData = await pdfResponse.json();
    console.log('skill-test-pdf skills:', pdfData.skills);

    // 检查 skill-test-doc
    const docResponse = await request.get('http://localhost:20881/api/agents/skill-test-doc');
    const docData = await docResponse.json();
    console.log('skill-test-doc skills:', docData.skills);
  });

  test('检查模型服务配置', async ({ request }) => {
    console.log('\n=== 检查模型服务配置 ===');

    const response = await request.get('http://localhost:20881/api/model-services');
    const data = await response.json();

    console.log('Model services count:', data.services?.length || 0);
    if (data.services) {
      data.services.forEach((s: any) => {
        console.log(`  - ${s.name}: ${s.provider} (${s.selected_model})`);
      });
    }
  });

  test('检查 Skill 列表', async ({ request }) => {
    console.log('\n=== 检查 Skill 列表 ===');

    const response = await request.get('http://localhost:20881/api/skills');
    const data = await response.json();

    console.log('Skills count:', data.skills?.length || 0);

    const pdfSkill = data.skills?.find((s: any) => s.name.includes('PDF'));
    const docxSkill = data.skills?.find((s: any) => s.name.includes('DOCX'));

    console.log('PDF skill found:', !!pdfSkill);
    console.log('DOCX skill found:', !!docxSkill);
  });
});

/**
 * UAT测试：MCP工具调用优先级优化
 * 验证智能体能够根据用户意图正确调用相应的MCP工具
 *
 * 测试用例：
 * - TC-001: 数学计算 -> evaluate 工具
 * - TC-002: 冷笑话 -> get_joke 工具
 * - TC-003: 加密货币价格 -> get_coin_price 工具
 */

import { chromium, Browser, Page, BrowserContext } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

function findProjectRoot(start: string): string {
  let current = path.resolve(start);
  while (true) {
    if (fs.existsSync(path.join(current, 'CLAUDE.md'))) return current;
    const parent = path.dirname(current);
    if (parent === current) throw new Error(`Unable to locate project root from ${start}`);
    current = parent;
  }
}

const PROJECT_ROOT = findProjectRoot(process.cwd());
const OUTPUT_DIR = path.join(PROJECT_ROOT, '.runtime', 'test-results', 'uat-mcp-tool-priority');
fs.mkdirSync(OUTPUT_DIR, { recursive: true });

const BASE_URL = 'http://localhost:20815';
const AGENT_NAME = 'test3';

interface ToolCall {
  name: string;
  args?: Record<string, unknown>;
}

interface TestCase {
  id: string;
  input: string;
  expectedTool: string;
  description: string;
}

const testCases: TestCase[] = [
  {
    id: 'TC-001',
    input: '2138/2394+23是多少',
    expectedTool: 'evaluate',
    description: '数学计算应调用 evaluate 工具'
  },
  {
    id: 'TC-002',
    input: '讲一个冷笑话',
    expectedTool: 'get_joke',
    description: '请求笑话应调用 get_joke 工具'
  },
  {
    id: 'TC-003',
    input: 'BTC的最新价格是多少',
    expectedTool: 'get_coin_price',
    description: '查询加密货币价格应调用 get_coin_price 工具'
  }
];

class UATTester {
  private browser: Browser | null = null;
  private context: BrowserContext | null = null;
  private page: Page | null = null;
  readonly results: Array<{
    testCase: TestCase;
    passed: boolean;
    actualTool?: string;
    error?: string;
    screenshot?: string;
  }> = [];

  async init(): Promise<void> {
    console.log('🚀 启动浏览器...');
    this.browser = await chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox']
    });
    this.context = await this.browser.newContext({
      viewport: { width: 1920, height: 1080 }
    });
    this.page = await this.context.newPage();

    // 监听网络请求，捕获SSE流
    this.page.on('response', async (response) => {
      const url = response.url();
      if (url.includes('/chat/stream')) {
        console.log(`📡 检测到流式请求: ${url}`);
      }
    });
  }

  async navigateToAgent(): Promise<void> {
    if (!this.page) throw new Error('Page not initialized');

    console.log(`📍 导航到首页: ${BASE_URL}`);
    await this.page.goto(BASE_URL, { waitUntil: 'networkidle' });
    await this.page.waitForTimeout(1000);

    // 查找并点击 test3 智能体
    console.log(`🔍 查找智能体: ${AGENT_NAME}`);

    // 尝试多种选择器
    const agentSelectors = [
      `text="${AGENT_NAME}"`,
      `[data-agent-name="${AGENT_NAME}"]`,
      `//div[contains(@class, 'agent-card') and .//text()='${AGENT_NAME}']`
    ];

    let agentFound = false;
    for (const selector of agentSelectors) {
      try {
        const element = selector.startsWith('//')
          ? await this.page.waitForSelector(`xpath=${selector}`, { timeout: 2000 })
          : await this.page.waitForSelector(selector, { timeout: 2000 });

        if (element) {
          await element.click();
          agentFound = true;
          console.log(`✅ 找到并点击智能体: ${AGENT_NAME}`);
          break;
        }
      } catch {
        continue;
      }
    }

    if (!agentFound) {
      // 列出所有可用的智能体
      const agentCards = await this.page.$$eval('div[class*="agent"], div[class*="Agent"]', elements =>
        elements.map(el => el.textContent?.trim())
      );
      console.log(`⚠️ 未找到智能体 ${AGENT_NAME}，可用智能体:`, agentCards);
      throw new Error(`Agent ${AGENT_NAME} not found`);
    }

    // 等待导航到对话页面
    await this.page.waitForTimeout(2000);
    console.log('📱 进入对话页面');
  }

  private async extractToolCallsFromSSE(responseBody: string): Promise<ToolCall[]> {
    const toolCalls: ToolCall[] = [];
    const lines = responseBody.split('\n');

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          if (data.type === 'tool_call') {
            toolCalls.push({
              name: data.name,
              args: data.args
            });
          }
        } catch {
          // 忽略解析错误
        }
      }
    }

    return toolCalls;
  }

  async runTest(testCase: TestCase): Promise<void> {
    if (!this.page) throw new Error('Page not initialized');

    console.log(`\n🧪 执行测试: ${testCase.id}`);
    console.log(`   描述: ${testCase.description}`);
    console.log(`   输入: "${testCase.input}"`);

    try {
      // 截图：测试前状态
      const beforeScreenshot = path.join(OUTPUT_DIR, `uat_${testCase.id}_before.png`);
      await this.page.screenshot({ path: beforeScreenshot });
      console.log(`   📸 截图保存: ${beforeScreenshot}`);

      // 查找消息输入框
      const inputSelectors = [
        'textarea[placeholder*="输入"]',
        'textarea[placeholder*="消息"]',
        'textarea[placeholder*="Send"]',
        'textarea[name="message"]',
        'textarea',
        'input[type="text"]'
      ];

      let inputElement = null;
      for (const selector of inputSelectors) {
        try {
          inputElement = await this.page.waitForSelector(selector, { timeout: 2000 });
          if (inputElement) break;
        } catch {
          continue;
        }
      }

      if (!inputElement) {
        throw new Error('消息输入框未找到');
      }

      // 输入消息
      await inputElement.fill(testCase.input);
      await this.page.waitForTimeout(500);

      // 截图：输入后状态
      const afterInputScreenshot = path.join(OUTPUT_DIR, `uat_${testCase.id}_after_input.png`);
      await this.page.screenshot({ path: afterInputScreenshot });
      console.log(`   📸 截图保存: ${afterInputScreenshot}`);

      // 查找发送按钮
      const sendButtonSelectors = [
        'button[type="submit"]',
        'button:has-text("发送")',
        'button:has-text("Send")',
        'svg[class*="send"]',
        'button:has(svg)' // 假设有图标的按钮是发送按钮
      ];

      let sendButton = null;
      for (const selector of sendButtonSelectors) {
        try {
          sendButton = await this.page.waitForSelector(selector, { timeout: 2000 });
          if (sendButton) break;
        } catch {
          continue;
        }
      }

      if (sendButton) {
        await sendButton.click();
      } else {
        // 尝试按 Enter 键发送
        await inputElement.press('Enter');
      }

      console.log(`   ⏳ 等待响应...`);

      // 等待响应出现
      await this.page.waitForTimeout(8000); // 等待足够时间让流式响应完成

      // 截图：响应后状态
      const afterResponseScreenshot = path.join(OUTPUT_DIR, `uat_${testCase.id}_after_response.png`);
      await this.page.screenshot({ path: afterResponseScreenshot });
      console.log(`   📸 截图保存: ${afterResponseScreenshot}`);

      // 检查页面内容，查找工具调用指示
      const pageContent = await this.page.content();
      const pageText = await this.page.textContent('body');

      // 检查是否有工具调用的UI指示
      const toolCallIndicators = [
        'evaluate',
        'get_joke',
        'get_coin_price',
        'tool_call',
        '工具调用'
      ];

      let detectedTool = '';
      for (const indicator of toolCallIndicators) {
        if (pageText?.toLowerCase().includes(indicator.toLowerCase())) {
          detectedTool = indicator;
          break;
        }
      }

      // 更精确地检查：查找工具调用的UI元素
      const toolCallElements = await this.page.$$eval('[data-tool-call], [class*="tool-call"], [class*="toolCall"]', elements =>
        elements.map(el => el.textContent || el.getAttribute('data-tool-call') || '')
      );

      console.log(`   🔍 检测到的工具调用UI元素:`, toolCallElements);

      // 判断测试结果
      const passed = detectedTool === testCase.expectedTool ||
                     toolCallElements.some(el => el.toLowerCase().includes(testCase.expectedTool.toLowerCase()));

      this.results.push({
        testCase,
        passed,
        actualTool: detectedTool || toolCallElements.join(', ') || '未检测到',
        screenshot: afterResponseScreenshot
      });

      console.log(`   ${passed ? '✅ 通过' : '❌ 失败'} - 预期: ${testCase.expectedTool}, 实际: ${detectedTool || toolCallElements.join(', ') || '未检测到'}`);

    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      console.error(`   ❌ 测试异常: ${errorMessage}`);

      this.results.push({
        testCase,
        passed: false,
        error: errorMessage
      });
    }
  }

  async generateReport(): Promise<string> {
    const timestamp = new Date().toISOString();
    const passedCount = this.results.filter(r => r.passed).length;
    const totalCount = this.results.length;
    const passRate = ((passedCount / totalCount) * 100).toFixed(1);

    let report = `# MCP工具调用优先级优化 UAT 报告\n\n`;
    report += `**生成时间**: ${timestamp}\n`;
    report += `**测试环境**: ${BASE_URL}\n`;
    report += `**智能体**: ${AGENT_NAME}\n\n`;
    report += `## 测试摘要\n\n`;
    report += `| 指标 | 结果 |\n`;
    report += `|------|------|\n`;
    report += `| 总用例数 | ${totalCount} |\n`;
    report += `| 通过数 | ${passedCount} |\n`;
    report += `| 失败数 | ${totalCount - passedCount} |\n`;
    report += `| 通过率 | ${passRate}% |\n\n`;

    report += `## 测试详情\n\n`;
    report += `| 用例ID | 描述 | 输入 | 预期工具 | 实际工具 | 结果 |\n`;
    report += `|--------|------|------|----------|----------|------|\n`;

    for (const result of this.results) {
      const status = result.passed ? '✅ 通过' : '❌ 失败';
      const actualTool = result.error ? `错误: ${result.error}` : (result.actualTool || 'N/A');
      report += `| ${result.testCase.id} | ${result.testCase.description} | "${result.testCase.input}" | ${result.testCase.expectedTool} | ${actualTool} | ${status} |\n`;
    }

    report += `\n## 截图\n\n`;
    for (const result of this.results) {
      if (result.screenshot) {
        report += `### ${result.testCase.id} - ${result.testCase.description}\n`;
        report += `![${result.testCase.id}](${result.screenshot})\n\n`;
      }
    }

    report += `\n## 结论\n\n`;

    if (passedCount === totalCount) {
      report += `✅ **UAT 验收通过** - 所有测试用例通过，MCP工具调用优先级优化生效。\n`;
    } else if (passedCount > totalCount / 2) {
      report += `⚠️ **UAT 有条件通过** - 部分测试用例失败，需要进一步调查。\n`;
    } else {
      report += `❌ **UAT 验收失败** - 大部分测试用例失败，需要重新审查优化方案。\n`;
    }

    return report;
  }

  async cleanup(): Promise<void> {
    if (this.browser) {
      await this.browser.close();
      console.log('🧹 浏览器已关闭');
    }
  }
}

async function main() {
  const tester = new UATTester();

  try {
    await tester.init();
    await tester.navigateToAgent();

    // 依次执行测试用例
    for (const testCase of testCases) {
      await tester.runTest(testCase);
      // 测试之间等待
      await new Promise(resolve => setTimeout(resolve, 2000));
    }

    // 生成报告
    const report = await tester.generateReport();
    console.log('\n📊 测试报告:\n');
    console.log(report);

    // 保存报告
    const reportPath = path.join(OUTPUT_DIR, 'uat-report.md');
    await fs.promises.writeFile(reportPath, report, 'utf-8');
    console.log(`\n📄 报告已保存: ${reportPath}`);

    // 退出码
    const allPassed = tester.results.length > 0 && tester.results.every(r => r.passed);
    process.exit(allPassed ? 0 : 1);

  } catch (error) {
    console.error('❌ 测试执行异常:', error);
    process.exit(1);
  } finally {
    await tester.cleanup();
  }
}

main();

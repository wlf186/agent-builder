#!/usr/bin/env python3
"""
UAT测试：MCP工具调用优先级优化
使用后端 API 直接测试智能体工具调用行为

测试用例：
- TC-001: 数学计算 -> evaluate 工具
- TC-002: 冷笑话 -> get_joke 工具
- TC-003: 加密货币价格 -> get_coin_price 工具
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
import aiohttp

BASE_URL = "http://localhost:20881"
AGENT_NAME = "test3"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / ".runtime" / "test-results" / "uat-mcp-tool-priority"

TEST_CASES = [
    {
        "id": "TC-001",
        "input": "2138/2394+23是多少",
        "expected_tool": "evaluate",
        "description": "数学计算应调用 evaluate 工具"
    },
    {
        "id": "TC-002",
        "input": "讲一个冷笑话",
        "expected_tool": "get_joke",
        "description": "请求笑话应调用 get_joke 工具"
    },
    {
        "id": "TC-003",
        "input": "BTC的最新价格是多少",
        "expected_tool": "get_simple_price",
        "description": "查询加密货币价格应调用 get_simple_price 工具"
    }
]


class UATTester:
    def __init__(self):
        self.session = None
        self.results = []

    async def init(self):
        print("🚀 初始化测试环境...")
        self.session = aiohttp.ClientSession()
        # 检查后端服务
        try:
            async with self.session.get(f"{BASE_URL}/health") as resp:
                if resp.status == 200:
                    print("✅ 后端服务正常")
                else:
                    print(f"⚠️ 后端服务状态异常: {resp.status}")
        except Exception as e:
            print(f"❌ 无法连接到后端服务: {e}")
            raise

    async def run_test(self, test_case):
        print(f"\n🧪 执行测试: {test_case['id']}")
        print(f"   描述: {test_case['description']}")
        print(f"   输入: \"{test_case['input']}\"")

        result = {
            "testCase": test_case,
            "passed": False,
            "actualTool": None,
            "toolCalls": [],
            "error": None,
            "responsePreview": None
        }

        try:
            # 准备请求
            url = f"{BASE_URL}/api/agents/{AGENT_NAME}/chat/stream"
            payload = {
                "message": test_case['input'],
                "history": []
            }

            print(f"   📡 发送请求到: {url}")

            # 发送流式请求
            tool_calls = []
            content_parts = []
            thinking_parts = []

            async with self.session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}: {await resp.text()}")

                # 读取流式响应
                async for line in resp.content:
                    line_str = line.decode('utf-8').strip()

                    if line_str.startswith('data: '):
                        try:
                            data = json.loads(line_str[6:])
                            event_type = data.get('type')

                            if event_type == 'tool_call':
                                tool_name = data.get('name')
                                tool_args = data.get('args')
                                tool_calls.append({
                                    'name': tool_name,
                                    'args': tool_args
                                })
                                print(f"   🔧 检测到工具调用: {tool_name}({tool_args})")

                            elif event_type == 'content':
                                content_parts.append(data.get('content', ''))

                            elif event_type == 'thinking':
                                thinking_parts.append(data.get('content', ''))

                        except json.JSONDecodeError:
                            pass

            # 分析结果
            result['toolCalls'] = tool_calls
            result['responsePreview'] = ''.join(content_parts[-50:]) if content_parts else ''

            # 检查是否调用了预期的工具
            expected_tool = test_case['expected_tool']
            detected_tools = [tc['name'] for tc in tool_calls]

            print(f"   🔍 检测到的工具调用: {detected_tools}")

            if expected_tool in detected_tools:
                result['passed'] = True
                result['actualTool'] = expected_tool
            else:
                result['actualTool'] = ', '.join(detected_tools) if detected_tools else '无'

            status = "✅ 通过" if result['passed'] else "❌ 失败"
            print(f"   {status} - 预期: {expected_tool}, 实际: {result['actualTool']}")

            # 打印响应预览
            if result['responsePreview']:
                print(f"   📝 响应预览: {result['responsePreview'][:100]}...")

        except Exception as e:
            error_msg = str(e)
            print(f"   ❌ 测试异常: {error_msg}")
            result['error'] = error_msg

        self.results.append(result)

    async def generate_report(self):
        timestamp = datetime.now().isoformat()
        passed_count = sum(1 for r in self.results if r['passed'])
        total_count = len(self.results)
        pass_rate = (passed_count / total_count * 100) if total_count > 0 else 0

        report = f"""# MCP工具调用优先级优化 UAT 报告

**生成时间**: {timestamp}
**测试环境**: {BASE_URL}
**智能体**: {AGENT_NAME}
**测试方式**: 后端 API 直接调用

## 测试摘要

| 指标 | 结果 |
|------|------|
| 总用例数 | {total_count} |
| 通过数 | {passed_count} |
| 失败数 | {total_count - passed_count} |
| 通过率 | {pass_rate:.1f}% |

## 测试详情

| 用例ID | 描述 | 输入 | 预期工具 | 实际工具 | 结果 |
|--------|------|------|----------|----------|------|
"""

        for result in self.results:
            tc = result['testCase']
            status = "✅ 通过" if result['passed'] else "❌ 失败"
            actual = result['error'] if result['error'] else (result.get('actualTool') or 'N/A')
            report += f"| {tc['id']} | {tc['description']} | \"{tc['input']}\" | {tc['expected_tool']} | {actual} | {status} |\n"

        # 添加详细响应
        report += "\n## 详细响应\n\n"
        for result in self.results:
            report += f"### {result['testCase']['id']} - {result['testCase']['description']}\n\n"
            if result['toolCalls']:
                report += "**工具调用序列**:\n"
                for i, tc in enumerate(result['toolCalls'], 1):
                    report += f"{i}. `{tc['name']}`({json.dumps(tc['args'], ensure_ascii=False)})\n"
            else:
                report += "**工具调用**: 无\n"

            if result.get('responsePreview'):
                report += f"\n**响应预览**: {result['responsePreview'][:200]}...\n"

            if result.get('error'):
                report += f"\n**错误**: {result['error']}\n"

            report += "\n"

        report += "\n## 结论\n\n"

        if passed_count == total_count:
            report += "✅ **UAT 验收通过** - 所有测试用例通过，MCP工具调用优先级优化生效。\n"
        elif passed_count > total_count / 2:
            report += "⚠️ **UAT 有条件通过** - 部分测试用例失败，需要进一步调查。\n"
            report += "\n**建议**:\n"
            for result in self.results:
                if not result['passed']:
                    report += f"- {result['testCase']['id']}: 检查 {result['testCase']['expected_tool']} 工具配置\n"
        else:
            report += "❌ **UAT 验收失败** - 大部分测试用例失败，需要重新审查优化方案。\n"

        return report

    async def cleanup(self):
        if self.session:
            await self.session.close()
            print("🧹 会话已关闭")


async def main():
    tester = UATTester()

    try:
        await tester.init()

        # 依次执行测试用例
        for test_case in TEST_CASES:
            await tester.run_test(test_case)
            await asyncio.sleep(1)  # 测试之间等待

        # 生成报告
        report = await tester.generate_report()
        print("\n📊 测试报告:\n")
        print(report)

        # 保存报告
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = OUTPUT_DIR / "uat-report.md"

        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)

        print(f"\n📄 报告已保存: {report_path}")

        # 退出码
        all_passed = len(tester.results) > 0 and all(r['passed'] for r in tester.results)
        return 0 if all_passed else 1

    except Exception as e:
        print(f"❌ 测试执行异常: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await tester.cleanup()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)

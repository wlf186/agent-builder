#!/usr/bin/env python3
"""
计算器 MCP 服务 - 提供基本数学计算功能
"""
import asyncio
import json
import sys
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("MCP library not available", file=sys.stderr)
    sys.exit(1)

# 创建服务器实例
server = Server("calculator")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """列出可用的工具"""
    return [
        types.Tool(
            name="add",
            description="计算两个数的和。例如：add(2, 3) = 5",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个数"},
                    "b": {"type": "number", "description": "第二个数"}
                },
                "required": ["a", "b"]
            }
        ),
        types.Tool(
            name="subtract",
            description="计算两个数的差。例如：subtract(5, 3) = 2",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "被减数"},
                    "b": {"type": "number", "description": "减数"}
                },
                "required": ["a", "b"]
            }
        ),
        types.Tool(
            name="multiply",
            description="计算两个数的乘积。例如：multiply(4, 5) = 20",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个数"},
                    "b": {"type": "number", "description": "第二个数"}
                },
                "required": ["a", "b"]
            }
        ),
        types.Tool(
            name="divide",
            description="计算两个数的商。例如：divide(10, 2) = 5。注意：除数不能为0",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "被除数"},
                    "b": {"type": "number", "description": "除数（不能为0）"}
                },
                "required": ["a", "b"]
            }
        ),
        types.Tool(
            name="power",
            description="计算 a 的 b 次方。例如：power(2, 3) = 8",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "底数"},
                    "b": {"type": "number", "description": "指数"}
                },
                "required": ["a", "b"]
            }
        ),
        types.Tool(
            name="sqrt",
            description="计算平方根。例如：sqrt(16) = 4",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "要计算平方根的数（必须非负）"}
                },
                "required": ["a"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """执行工具调用"""
    try:
        if name == "add":
            result = arguments["a"] + arguments["b"]
            return [types.TextContent(type="text", text=f"计算结果: {arguments['a']} + {arguments['b']} = {result}")]

        elif name == "subtract":
            result = arguments["a"] - arguments["b"]
            return [types.TextContent(type="text", text=f"计算结果: {arguments['a']} - {arguments['b']} = {result}")]

        elif name == "multiply":
            result = arguments["a"] * arguments["b"]
            return [types.TextContent(type="text", text=f"计算结果: {arguments['a']} × {arguments['b']} = {result}")]

        elif name == "divide":
            if arguments["b"] == 0:
                return [types.TextContent(type="text", text="错误: 除数不能为0")]
            result = arguments["a"] / arguments["b"]
            return [types.TextContent(type="text", text=f"计算结果: {arguments['a']} ÷ {arguments['b']} = {result}")]

        elif name == "power":
            result = arguments["a"] ** arguments["b"]
            return [types.TextContent(type="text", text=f"计算结果: {arguments['a']} ^ {arguments['b']} = {result}")]

        elif name == "sqrt":
            if arguments["a"] < 0:
                return [types.TextContent(type="text", text="错误: 不能对负数求平方根")]
            result = arguments["a"] ** 0.5
            return [types.TextContent(type="text", text=f"计算结果: √{arguments['a']} = {result}")]

        else:
            return [types.TextContent(type="text", text=f"未知工具: {name}")]

    except Exception:
        return [types.TextContent(type="text", text="计算错误: 参数无效或超出安全范围")]


async def main():
    """启动服务器"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())

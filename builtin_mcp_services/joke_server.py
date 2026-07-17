#!/usr/bin/env python3
"""
冷笑话 MCP 服务 - 提供各种冷笑话
"""
import asyncio
import json
import sys
import random
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
server = Server("cold_jokes")

# 冷笑话数据库 - 高质量笑话库
JOKES = {
    "animal": [
        "为什么北极熊不吃企鹅？因为它们一个在北极，一个在南极，太远了！",
        "蜗牛为什么爬得慢？因为它背着房子走！",
        "为什么鱼不会说话？因为它们只会吐泡泡！",
        "长颈鹿为什么脖子长？因为它的脚离头太远了！",
        "为什么企鹅不怕冷？因为它们穿了燕尾服！",
        "为什么蚂蚁看不见自己的脚？因为它们忙着搬家！",
        "为什么猫咪喜欢打瞌睡？因为它们要充电！",
        "海豚为什么总是微笑？因为它们知道人类听不懂它们的笑话！",
        "为什么熊猫总是黑白照片？因为它们没有彩色的生活！",
        "为什么章鱼有8只手？因为它要抢红包！",
    ],
    "food": [
        "西红柿为什么脸红？因为它看见沙拉在换衣服！",
        "包子为什么哭？因为它被蒸了！",
        "为什么饺子总是很团结？因为它们粘在一起！",
        "面包为什么受伤了？因为它被烤了！",
        "为什么鸡蛋不爱出门？因为它怕被打！",
        "为什么火锅总是很热闹？因为它有很多朋友！",
        "薯条为什么自卑？因为它觉得自己太细了！",
        "奶茶为什么受欢迎？因为它知道怎么甜到人心里！",
        "为什么饺子总是很低调？因为它内涵丰富不张扬！",
        "为什么披萨总是很圆？因为它不想有棱角！",
    ],
    "tech": [
        "程序员为什么喜欢深色模式？因为光明的未来太刺眼了！",
        "为什么 Java 开发者戴眼镜？因为他们看不见 C#（C Sharp）！",
        "为什么电脑会发烧？因为它中了病毒！",
        "WiFi 为什么害羞？因为它不善于连接！",
        "为什么键盘不开心？因为它总是被敲打！",
        "为什么程序员分不清万圣节和圣诞节？因为 Oct 31 = Dec 25！",
        "为什么 AI 不讲笑话？因为它们怕被训练数据起诉！",
        "为什么数据库管理员喜欢钓鱼？因为他们在找主键！",
        "为什么 Python 程序员不用电梯？因为他们喜欢递归！",
        "为什么前端开发喜欢喝咖啡？因为需要保持 CSS（Caffeine Styling System）！",
    ],
    "daily": [
        "为什么电风扇会转？因为它晕了！",
        "闹钟为什么响？因为它想叫醒你！",
        "为什么拖鞋不成对？因为它们总是被分开穿！",
        "镜子为什么能照人？因为它脸皮薄！",
        "为什么雨伞能挡雨？因为它脸大！",
        "为什么洗衣机爱转圈？因为它在跳舞！",
        "为什么遥控器总躲在沙发底下？因为它社恐！",
        "为什么手机总是很忙？因为它要处理很多消息！",
        "为什么空调会滴水？因为它哭了！",
        "为什么拖鞋不出国？因为它没有签证！",
    ],
    "random": [
        "小明问妈妈：为什么我的名字叫小明？妈妈说：因为我喜欢明天的太阳。小明说：那哥哥呢？妈妈说：因为他是我明天以前生的，叫小黑。",
        "有一个人去银行取钱，柜台小姐问他要取多少，他说：取钱还要多少？",
        "老师问：谁知道天上的星星为什么眨眼睛？小明答：因为天太黑了，它们怕黑！",
        "病人：医生，我最近总是觉得自己是一张床单。医生：别担心，这只是你的被套行为。",
        "为什么数学书很忧伤？因为它有太多的问题。",
        "为什么钱包越来越瘦？因为它在减肥！",
        "为什么地球不停转动？因为它睡不着！",
        "为什么袜子总少一只？因为它们在玩捉迷藏！",
        "为什么太阳每天都要上班？因为它是正式员工，没有假期！",
        "为什么月亮有时圆有时缺？因为它在减肥，有时吃多有时吃少！",
    ]
}


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """列出可用的工具"""
    return [
        types.Tool(
            name="get_joke",
            description="获取一个随机冷笑话，让人开心一下",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "笑话类别：animal(动物)、food(食物)、tech(科技)、daily(日常)、random(随机)",
                        "enum": ["animal", "food", "tech", "daily", "random"]
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="list_categories",
            description="列出所有可用的笑话类别",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="get_jokes_by_category",
            description="获取指定类别的所有笑话",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "笑话类别",
                        "enum": ["animal", "food", "tech", "daily", "random"]
                    }
                },
                "required": ["category"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """执行工具调用"""
    try:
        if name == "get_joke":
            category = arguments.get("category", "random")
            if category not in JOKES:
                category = "random"
            joke = random.choice(JOKES[category])
            return [types.TextContent(type="text", text=f"🥶 冷笑话来了：\n\n{joke}\n\n（笑声停不下来...或者根本没开始）")]

        elif name == "list_categories":
            categories = list(JOKES.keys())
            descriptions = {
                "animal": "动物相关",
                "food": "食物相关",
                "tech": "科技相关",
                "daily": "日常生活",
                "random": "随机杂烩"
            }
            result = "可用的笑话类别：\n"
            for cat in categories:
                result += f"  • {cat}: {descriptions.get(cat, '')} ({len(JOKES[cat])}条)\n"
            return [types.TextContent(type="text", text=result)]

        elif name == "get_jokes_by_category":
            category = arguments.get("category")
            if category not in JOKES:
                return [types.TextContent(type="text", text=f"未知的类别: {category}。可用类别: {list(JOKES.keys())}")]
            jokes = JOKES[category]
            result = f"【{category}】类别的所有笑话：\n\n"
            for i, joke in enumerate(jokes, 1):
                result += f"{i}. {joke}\n\n"
            return [types.TextContent(type="text", text=result)]

        else:
            return [types.TextContent(type="text", text=f"未知工具: {name}")]

    except Exception:
        return [types.TextContent(type="text", text="获取笑话失败，请稍后重试")]


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

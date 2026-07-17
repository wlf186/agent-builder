"""
MCP 工具适配器 - 将 MCP 工具转换为 LangChain Tool 对象

支持 bind_tools() 和 tool_choice 参数，实现强制工具调用
"""
import asyncio
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from langchain_core.tools import StructuredTool

from .log_safety import content_length, summarize_arguments

# 使用标准 pydantic 导入（兼容 pydantic v2）
try:
    from pydantic import BaseModel, Field, create_model
except ImportError:
    from pydantic.v1 import BaseModel, Field
    # pydantic v1 fallback
    from pydantic import create_model

if TYPE_CHECKING:
    from .mcp_manager import MCPManager, MCPTool


def summarize_tool_arguments(arguments: Any) -> Dict[str, Any]:
    """Backward-compatible alias for the shared value-free summary."""
    return summarize_arguments(arguments)


def tool_result_length(result: Any) -> int:
    """Backward-compatible alias for value-free content measurement."""
    return content_length(result)


class MCPToolAdapter:
    """
    MCP 工具适配器

    将 MCP 工具转换为 LangChain StructuredTool，支持原生工具绑定
    """

    # 增强映射表覆盖的工具名集合（类级别缓存）
    _ENHANCED_TOOL_NAMES: set = None

    @classmethod
    def _get_enhanced_tool_names(cls) -> set:
        """获取所有有增强配置的工具名集合"""
        if cls._ENHANCED_TOOL_NAMES is None:
            cls._ENHANCED_TOOL_NAMES = (
                set({
                    "evaluate", "add", "subtract", "multiply", "divide",
                    "power", "sqrt", "get_joke", "list_categories",
                    "get_jokes_by_category", "get_coin_price", "get_market_data",
                    "load_skill", "execute_skill", "calculator", "cold-jokes", "coingecko",
                })
            )
        return cls._ENHANCED_TOOL_NAMES

    def __init__(self, mcp_manager: 'MCPManager'):
        """
        初始化适配器

        Args:
            mcp_manager: MCP 管理器实例
        """
        self.mcp_manager = mcp_manager
        self._converted_tools: Dict[str, StructuredTool] = {}

    def convert_tool(self, mcp_tool: 'MCPTool') -> StructuredTool:
        """
        将单个 MCP 工具转换为 LangChain StructuredTool

        Args:
            mcp_tool: MCP 工具对象

        Returns:
            LangChain StructuredTool 对象
        """
        # 如果已经转换过，直接返回缓存
        if mcp_tool.name in self._converted_tools:
            return self._converted_tools[mcp_tool.name]

        # 创建动态参数 schema
        # 如果 MCP 工具有 input_schema，可以尝试解析
        # 否则使用通用的字典参数
        args_schema = self._create_args_schema(mcp_tool)

        # 创建工具执行函数
        def execute_tool(**kwargs) -> str:
            """
            执行 MCP 工具（同步版本）

            【AC130-202603141800 TC-003 修复】
            修复动态 schema 的 kwargs 参数包装问题：
            - 当使用 _create_dynamic_schema 时，参数被包装为 {'kwargs': {...}}
            - 需要 kwargs 展开后传递给 MCP

            注意：由于 MCP 调用是异步的，这里需要在事件循环中运行
            """
            try:
                # ========================================
                # 【TC-003 修复】展开 kwargs 参数
                # ========================================
                # 如果只有一个键 'kwargs' 且其值是字典，说明使用了动态 schema
                # 需要展开 kwargs 的内容作为实际参数
                actual_args = kwargs
                if len(kwargs) == 1 and 'kwargs' in kwargs:
                    kwargs_value = kwargs['kwargs']
                    if isinstance(kwargs_value, dict):
                        actual_args = kwargs_value
                        print(
                            "[DEBUG] 展开动态 schema 参数: "
                            f"{summarize_tool_arguments(actual_args)}"
                        )

                # 获取当前事件循环，如果没有则创建
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                # 如果循环正在运行，使用 create_task
                if loop.is_running():
                    # 在异步上下文中，需要使用不同方式
                    # 这种情况下返回一个占位符，实际调用应该在异步上下文中处理
                    return self._execute_async_wrapper(mcp_tool.name, actual_args)
                else:
                    # 同步上下文，直接运行
                    return loop.run_until_complete(
                        self.mcp_manager.call_tool(mcp_tool.name, actual_args)
                    )
            except Exception as e:
                return f"工具调用错误 ({type(e).__name__})"

        # 创建 StructuredTool
        langchain_tool = StructuredTool(
            name=mcp_tool.name,
            description=self._enhance_description(mcp_tool),
            func=execute_tool,
            args_schema=args_schema,
            coroutine=self._create_async_executor(mcp_tool)
        )

        # 缓存转换结果
        self._converted_tools[mcp_tool.name] = langchain_tool
        return langchain_tool

    def convert_all_tools(self) -> List[StructuredTool]:
        """
        转换所有 MCP 工具

        Returns:
            LangChain StructuredTool 列表
        """
        if not self.mcp_manager.all_tools:
            return []

        tools = []
        for mcp_tool in self.mcp_manager.all_tools:
            try:
                lc_tool = self.convert_tool(mcp_tool)
                tools.append(lc_tool)
            except Exception as e:
                print(
                    f"警告: 转换工具 {mcp_tool.name} 失败: "
                    f"error_type={type(e).__name__}"
                )

        return tools

    def _create_args_schema(self, mcp_tool: 'MCPTool') -> type[BaseModel]:
        """
        创建参数 schema

        Args:
            mcp_tool: MCP 工具

        Returns:
            Pydantic BaseModel 类
        """
        # 如果有 input_schema，尝试解析
        if mcp_tool.input_schema:
            return self._parse_input_schema(mcp_tool.input_schema, mcp_tool.name)

        # 否则使用通用动态 schema
        return self._create_dynamic_schema(mcp_tool.name)

    def _create_dynamic_schema(self, tool_name: str) -> type[BaseModel]:
        """
        创建动态参数 schema

        【AC130-202603141800 修复】
        修复 pydantic v2 类型注解问题

        Args:
            tool_name: 工具名称

        Returns:
            动态创建的 Pydantic BaseModel
        """
        # 使用 pydantic.create_model 动态创建模型，确保类型注解正确
        return create_model(
            f"{tool_name}_input",
            kwargs=(dict, Field(default_factory=dict, description=f"{tool_name} 工具的参数（键值对形式）")),
            __base__=BaseModel
        )

    def _parse_input_schema(self, schema: Dict, tool_name: str) -> type[BaseModel]:
        """
        解析 MCP input_schema 为 Pydantic 模型

        Args:
            schema: MCP input_schema
            tool_name: 工具名称

        Returns:
            Pydantic BaseModel 类
        """
        try:
            # 简化处理：如果 schema 太复杂，使用通用 schema
            if not isinstance(schema, dict) or 'properties' not in schema:
                return self._create_dynamic_schema(tool_name)

            fields = {}
            props = schema.get('properties', {})
            required = schema.get('required', [])

            for prop_name, prop_def in props.items():
                # 创建 Field
                field_kwargs = {"description": prop_def.get('description', '')}

                # 处理默认值
                if 'default' in prop_def:
                    field_kwargs['default'] = prop_def['default']
                elif prop_name not in required:
                    field_kwargs['default'] = None

                # 根据类型设置
                prop_type = prop_def.get('type', 'string')
                if prop_type == 'string':
                    field_type = str
                elif prop_type == 'number' or prop_type == 'integer':
                    field_type = float if prop_type == 'number' else int
                elif prop_type == 'boolean':
                    field_type = bool
                elif prop_type == 'array':
                    field_type = list
                else:
                    field_type = str

                fields[prop_name] = Field(**field_kwargs)

            return type(f"{tool_name}_input", (BaseModel,), fields)

        except Exception:
            # 解析失败，使用通用 schema
            return self._create_dynamic_schema(tool_name)

    def _enhance_description(self, mcp_tool: 'MCPTool') -> str:
        """
        增强工具描述，添加强制性前缀、使用场景提示和触发关键词

        优化：对未映射工具直接返回原始描述，跳过增强流程
        """
        base_desc = mcp_tool.description or mcp_tool.name

        # 短路：工具不在增强映射表中，直接返回原始描述
        if mcp_tool.name not in self._get_enhanced_tool_names():
            return base_desc

        mandatory_prefix = self._get_mandatory_prefix(mcp_tool.name)
        hints = self._get_tool_hints(mcp_tool.name)
        keywords = self._get_trigger_keywords(mcp_tool.name)
        examples = self._get_few_shot_examples(mcp_tool.name)

        enhanced = base_desc

        if mandatory_prefix:
            enhanced = f"{mandatory_prefix}\n\n{enhanced}"
        if hints:
            enhanced = f"{enhanced}\n\n使用场景: {hints}"
        if keywords:
            enhanced = f"{enhanced}\n触发关键词: {keywords}"
        if examples:
            enhanced = f"{enhanced}\n\n调用示例:\n{examples}"

        return enhanced

    def _get_mandatory_prefix(self, tool_name: str) -> str:
        """
        获取工具强制性前缀

        【AC130-202603141800 P1 新增】

        Args:
            tool_name: 工具名称

        Returns:
            强制性前缀文本
        """
        mandatory_prefixes = {
            "evaluate": "🔴 【MANDATORY】数学计算必须使用此工具，禁止自己计算！即使是很简单的计算也必须调用。",
            "add": "🔴 【MANDATORY】加法计算必须使用此工具，禁止自己计算！",
            "subtract": "🔴 【MANDATORY】减法计算必须使用此工具，禁止自己计算！",
            "multiply": "🔴 【MANDATORY】乘法计算必须使用此工具，禁止自己计算！",
            "divide": "🔴 【MANDATORY】除法计算必须使用此工具，禁止自己计算！",
            "power": "🔴 【MANDATORY】幂运算必须使用此工具，禁止自己计算！",
            "sqrt": "🔴 【MANDATORY】开方计算必须使用此工具，禁止自己计算！",
            "get_joke": "🔴 【MANDATORY】获取笑话必须使用此工具，禁止编造笑话！",
            "list_categories": "🔴 【MANDATORY】列出笑话分类必须使用此工具！",
            "get_jokes_by_category": "🔴 【MANDATORY】获取特定分类笑话必须使用此工具！",
            "get_coin_price": "🔴 【MANDATORY】查询加密货币价格必须使用此工具，禁止使用过时数据回答！",
            "get_market_data": "🔴 【MANDATORY】查询市场数据必须使用此工具，禁止使用过时数据回答！",
            "load_skill": "🔴 【MANDATORY】处理 PDF/DOCX 等文件时必须先调用此工具加载技能！",
            "execute_skill": "🔴 【MANDATORY】执行技能脚本处理文件必须使用此工具！",
        }

        return mandatory_prefixes.get(tool_name, "")

    def _get_few_shot_examples(self, tool_name: str) -> str:
        """
        获取工具 Few-shot 调用示例

        【AC130-202603141800 P1 新增】

        Args:
            tool_name: 工具名称

        Returns:
            Few-shot 示例文本
        """
        examples_map = {
            "evaluate": '''- 用户："100+200等于多少" → 调用 {{"expression": "100+200"}}
- 用户："5的平方根" → 调用 {{"expression": "sqrt(5)"}}
- 用户："2.5 * 3 + 10" → 调用 {{"expression": "2.5 * 3 + 10"}}''',
            "get_joke": '''- 用户："讲个笑话" → 调用 {{}}
- 用户："来个冷笑话" → 调用 {{}}
- 用户："逗我开心" → 调用 {{}}''',
            "get_coin_price": '''- 用户："比特币价格" → 调用 {{"coin_id": "bitcoin"}}
- 用户："ETH多少钱" → 调用 {{"coin_id": "ethereum"}}''',
        }

        return examples_map.get(tool_name, "")

    def _get_tool_hints(self, tool_name: str) -> str:
        """
        获取工具使用场景提示

        Args:
            tool_name: 工具名称

        Returns:
            使用场景提示文本
        """
        hints_map = {
            "calculator": "当用户询问数学计算、表达式求值、算术运算时必须使用此工具。不要自己计算！",
            "evaluate": "当用户询问数学计算、表达式求值、算术运算时必须使用此工具。不要自己计算！",
            "get_joke": "当用户要求讲笑话、要娱乐内容、幽默段子时使用此工具",
            "cold-jokes": "当用户要求讲笑话、要娱乐内容、幽默段子时使用此工具",
            "get_coin_price": "当用户询问加密货币价格、币价查询、数字货币行情时使用此工具",
            "coingecko": "当用户询问加密货币价格、币价查询、BTC/ETH/狗狗币价格、数字货币行情时使用此工具",
        }

        return hints_map.get(tool_name, "")

    def _get_trigger_keywords(self, tool_name: str) -> str:
        """
        获取工具触发关键词

        Args:
            tool_name: 工具名称

        Returns:
            触发关键词列表（逗号分隔）
        """
        keywords_map = {
            "calculator": "计算, 等于, 多少, 加减乘除, 求和, 表达式, 数学, 算术",
            "evaluate": "计算, 等于, 多少, 加减乘除, 求和, 表达式, 数学, 算术",
            "get_joke": "笑话, 幽默, 搞笑, 娱乐, 段子, 有趣, 讲个笑话",
            "cold-jokes": "笑话, 幽默, 搞笑, 娱乐, 段子, 有趣, 讲个笑话",
            "get_coin_price": "币价, 加密货币, 数字货币, BTC, ETH, 比特币, 以太坊, 价格, 行情",
            "coingecko": "币价, 加密货币, 数字货币, BTC, ETH, 比特币, 以太坊, 价格, 行情, CoinGecko",
        }

        return keywords_map.get(tool_name, "")

    def _create_async_executor(self, mcp_tool: 'MCPTool'):
        """
        创建异步执行器

        【AC130-202603141800 TC-003 修复】
        修复动态 schema 的 kwargs 参数包装问题

        Args:
            mcp_tool: MCP 工具

        Returns:
            异步执行函数
        """
        async def async_execute(**kwargs) -> str:
            # ========================================
            # 【TC-003 修复】展开 kwargs 参数
            # ========================================
            # 如果只有一个键 'kwargs' 且其值是字典，说明使用了动态 schema
            actual_args = kwargs
            if len(kwargs) == 1 and 'kwargs' in kwargs:
                kwargs_value = kwargs['kwargs']
                if isinstance(kwargs_value, dict):
                    actual_args = kwargs_value
                    print(
                        "[DEBUG] [async] 展开动态 schema 参数: "
                        f"{summarize_tool_arguments(actual_args)}"
                    )

            return await self.mcp_manager.call_tool(mcp_tool.name, actual_args)

        return async_execute

    def _execute_async_wrapper(self, tool_name: str, kwargs: Dict) -> str:
        """
        异步执行包装器（用于同步上下文中的异步调用）

        Args:
            tool_name: 工具名称
            kwargs: 参数

        Returns:
            执行结果或占位符
        """
        # 这种情况表示在异步上下文中尝试同步调用
        # 返回特殊标记，让调用方知道需要在异步上下文中处理
        summary = summarize_tool_arguments(kwargs)
        return (
            f"[ASYNC_PENDING] Tool call to {tool_name} ({summary}) "
            "needs async execution"
        )

    def clear_cache(self):
        """清除转换缓存"""
        self._converted_tools.clear()

    def get_converted_tool(self, tool_name: str) -> Optional[StructuredTool]:
        """
        获取已转换的工具

        Args:
            tool_name: 工具名称

        Returns:
            StructuredTool 或 None
        """
        return self._converted_tools.get(tool_name)


class ToolCallExecutor:
    """
    工具调用执行器

    处理 LangChain 工具调用（来自 bind_tools）并委托给 MCPManager
    """

    def __init__(self, mcp_manager: 'MCPManager', skill_tool=None):
        """
        初始化执行器

        Args:
            mcp_manager: MCP 管理器
            skill_tool: Skill 工具（可选）
        """
        self.mcp_manager = mcp_manager
        self.skill_tool = skill_tool

    async def execute_tool_call(self, tool_name: str, tool_args: Dict) -> str:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            tool_args: 工具参数

        Returns:
            执行结果
        """
        # 检查是否是 skill 工具
        if tool_name == "load_skill" and self.skill_tool:
            skill_name = tool_args.get("skill_name") or tool_args.get("skill", "")
            return await self.skill_tool.execute(skill_name, tool_args.get("list_files", False))

        if tool_name == "execute_skill" and self.skill_tool:
            return await self.skill_tool.execute_script(
                skill_name=tool_args.get("skill_name", ""),
                script_name=tool_args.get("script_name", "main.py"),
                arguments=tool_args.get("arguments", []),
                input_file_ids=tool_args.get("input_file_ids", []),
                timeout=tool_args.get("timeout", 60)
            )

        # MCP 工具调用
        if self.mcp_manager:
            return await self.mcp_manager.call_tool(tool_name, tool_args)

        return f"错误: 未找到工具 {tool_name}"

"""
Skill工具 - 提供按需加载技能和执行脚本的Tool接口
"""
from pathlib import Path
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from .skill_registry import SkillRegistry
from .skill_loader import SkillLoader

if TYPE_CHECKING:
    from .execution_engine import ExecutionEngine


class SkillTool:
    """Skill工具 - 允许LLM按需加载技能和执行脚本"""

    TOOL_NAME = "load_skill"
    EXECUTE_TOOL_NAME = "execute_skill"  # 新增：脚本执行工具

    def __init__(
        self,
        skill_registry: SkillRegistry,
        skills_dir: Path,
        enabled_skills: List[str],
        execution_engine: Optional["ExecutionEngine"] = None,
        agent_name: str = ""
    ):
        """
        初始化SkillTool

        Args:
            skill_registry: Skill注册表
            skills_dir: Skills根目录
            enabled_skills: Agent启用的技能名称列表
            execution_engine: 执行引擎（可选，用于脚本执行）
            agent_name: Agent名称（用于执行脚本）
        """
        self.skill_registry = skill_registry
        self.skills_dir = Path(skills_dir)
        self.enabled_skills = enabled_skills
        self.execution_engine = execution_engine
        self.agent_name = agent_name
        self._loaded_skills: Dict[str, str] = {}  # 缓存已加载的skill内容

    def _match_skill_name(self, query_name: str) -> Optional[str]:
        """
        匹配Skill名称，支持模糊匹配

        匹配策略（按优先级）：
        1. 精确匹配（在enabled_skills列表中）
        2. 规范化后精确匹配（大小写不敏感）
        3. 核心关键词匹配（如 "AB-pdf" 匹配 "AB-PDF Processing Guide"）
        4. 前缀匹配
        5. 包含匹配

        Args:
            query_name: 查询的Skill名称

        Returns:
            匹配到的实际Skill名称，未匹配返回None
        """
        if not query_name:
            return None

        # 1. 精确匹配
        if query_name in self.enabled_skills:
            return query_name

        # 规范化查询名称
        normalized_query = self._normalize_name(query_name)

        # 2. 规范化后精确匹配
        for skill_name in self.enabled_skills:
            normalized_skill = self._normalize_name(skill_name)
            if normalized_query == normalized_skill:
                return skill_name

        # 3. 核心关键词匹配
        # 提取查询名称的核心关键词（如 "AB-pdf" -> ["ab", "pdf"]）
        query_keywords = self._extract_core_keywords(normalized_query)

        if query_keywords:
            for skill_name in self.enabled_skills:
                normalized_skill = self._normalize_name(skill_name)
                skill_keywords = self._extract_core_keywords(normalized_skill)

                # 检查核心关键词是否全部匹配
                if query_keywords and skill_keywords:
                    # 如果查询关键词是技能关键词的子集，且至少包含一个有意义的关键词
                    if set(query_keywords).issubset(set(skill_keywords)):
                        # 确保有实质性的关键词匹配（不只是单个字母）
                        meaningful_matches = [kw for kw in query_keywords if len(kw) > 1]
                        if meaningful_matches:
                            return skill_name

        # 4. 模糊匹配（前缀/包含）
        best_match = None
        best_score = 0

        for skill_name in self.enabled_skills:
            normalized_skill = self._normalize_name(skill_name)

            # 前缀匹配
            if normalized_skill.startswith(normalized_query) or normalized_query.startswith(normalized_skill):
                score = max(len(normalized_query), len(normalized_skill))
                if score > best_score:
                    best_score = score
                    best_match = skill_name

            # 包含匹配
            elif normalized_query in normalized_skill or normalized_skill in normalized_query:
                score = min(len(normalized_query), len(normalized_skill))
                if score > best_score:
                    best_score = score
                    best_match = skill_name

        return best_match

    def _extract_core_keywords(self, normalized_name: str) -> List[str]:
        """
        从规范化名称中提取核心关键词

        例如：
        - "ab-pdf" -> ["ab", "pdf"]
        - "ab-pdf-processing-guide" -> ["ab", "pdf", "processing", "guide"]
        - "ab-docx" -> ["ab", "docx"]

        Args:
            normalized_name: 规范化后的名称（小写连字符格式）

        Returns:
            关键词列表
        """
        if not normalized_name:
            return []

        # 按连字符分割
        parts = normalized_name.split('-')

        # 过滤掉空字符串和太短的无意义词
        keywords = [part.strip() for part in parts if part.strip() and len(part.strip()) > 0]

        return keywords

    def _normalize_name(self, name: str) -> str:
        """
        规范化名称为小写连字符格式
        """
        if not name:
            return name
        import re
        normalized = name.lower()
        normalized = re.sub(r'\s+', '-', normalized)
        normalized = re.sub(r'-+', '-', normalized)
        normalized = normalized.strip('-')
        return normalized

    def _format_available_skills(self) -> str:
        """
        格式化可用的Skill列表，用于错误提示

        Returns:
            格式化的Skill列表字符串
        """
        if not self.enabled_skills:
            return "No skills are currently enabled for this agent."

        lines = ["Available skills for this agent:"]
        for skill_name in self.enabled_skills:
            skill = self.skill_registry.get_skill(skill_name)
            if skill:
                desc = skill.description[:80] + "..." if len(skill.description) > 80 else skill.description
                lines.append(f"  - {skill_name}: {desc}")
            else:
                lines.append(f"  - {skill_name}")
        return "\n".join(lines)

    def get_tool_definition(self) -> Dict[str, Any]:
        """
        返回工具定义，description中包含可用skills列表

        Returns:
            工具定义字典，包含name, description, input_schema
        """
        # 构建可用技能列表
        available_skills = []
        for skill_name in self.enabled_skills:
            skill = self.skill_registry.get_skill(skill_name)
            if skill and skill.enabled:
                available_skills.append({
                    "name": skill.name,
                    "description": skill.description[:100] + "..." if len(skill.description) > 100 else skill.description
                })

        skills_list_str = "\n".join([
            f"  - {s['name']}: {s['description']}"
            for s in available_skills
        ])

        description = f"""Load a skill's detailed guidance and instructions on-demand.

**IMPORTANT**: You MUST use the exact parameter name "skill_name" (not "skill") when calling this tool.

When to use: Call this tool when you need detailed instructions for handling specific tasks like PDF processing, DOCX creation, etc.

Available skills:
{skills_list_str}

This tool returns the full skill content including examples, best practices, and code snippets.
Each skill is only loaded once per session (cached).

Example call format:
{{"tool": "load_skill", "arguments": {{"skill_name": "exact-skill-name-here"}}}}"""

        return {
            "name": self.TOOL_NAME,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "REQUIRED: The exact name of the skill to load from the available skills list above. Must match exactly."
                    },
                    "list_files": {
                        "type": "boolean",
                        "description": "If true, only list the files in the skill directory instead of loading content",
                        "default": False
                    }
                },
                "required": ["skill_name"]
            }
        }

    def get_execute_tool_definition(self) -> Optional[Dict[str, Any]]:
        """
        返回脚本执行工具定义（优化版，包含更清晰的描述和调用示例）

        Returns:
            工具定义字典，如果没有可执行脚本则返回None
        """
        if not self.execution_engine:
            return None

        # 检测哪些Skill有可执行脚本
        executable_skills = self._detect_executable_skills()

        if not executable_skills:
            return None

        skills_list_str = self._format_executable_skills_with_actions(executable_skills)

        description = f"""执行 Skill 脚本处理用户上传的文件。

**何时使用此工具**:
- 用户上传了文件并要求处理（如：提取 PDF 文本、编辑 Word 文档）
- 需要对上传文件进行格式转换、数据分析等操作
- 用户明确提到要使用某个 Skill 的功能

**可用的可执行技能**:
{skills_list_str}

**重要说明**:
1. input_file_ids 参数必须从对话上下文中的 file_context.file_ids 获取，不要编造
2. 上传的文件会被自动放置在脚本的 ./input/ 目录下
3. 如果用户没有上传文件，不要编造 file_ids，可以留空
4. 脚本执行结果以 JSON 格式返回，包含 status、output 等字段

**调用示例**:

场景1：用户上传了 PDF 文件（file_id: abc123），要求提取文本
```json
{{"tool": "execute_skill", "arguments": {{"skill_name": "AB-pdf", "input_file_ids": ["abc123"], "arguments": ["./input/document.pdf", "--action", "extract_text"]}}}}
```

场景2：用户上传了 Word 文档（file_id: def456），要求获取文档信息
```json
{{"tool": "execute_skill", "arguments": {{"skill_name": "AB-docx", "input_file_ids": ["def456"], "arguments": ["./input/document.docx", "--action", "get_info"]}}}}
```

场景3：用户要求创建新的 Word 文档（无需上传文件）
```json
{{"tool": "execute_skill", "arguments": {{"skill_name": "AB-docx", "arguments": ["./output/new_doc.docx", "--action", "create_document", "--data", "{{\\"title\\": \\"测试文档\\", \\"content\\": [\\"段落1\\", \\"段落2\\"]}}"]}}}}
```"""

        return {
            "name": self.EXECUTE_TOOL_NAME,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要执行的 Skill 名称。根据文件类型选择：PDF 文件用 AB-pdf，Word 文档用 AB-docx"
                    },
                    "script_name": {
                        "type": "string",
                        "description": "脚本文件名（默认: main.py）",
                        "default": "main.py"
                    },
                    "arguments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "命令行参数。常用格式：[\"./input/文件名\", \"--action\", \"操作类型\"]。操作类型根据 Skill 不同而不同"
                    },
                    "input_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用户上传文件的 ID 列表。从对话上下文的 file_context.file_ids 获取，如果没有上传文件则留空。文件会被复制到 ./input/ 目录"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "执行超时时间（秒），默认 60 秒",
                        "default": 60
                    }
                },
                "required": ["skill_name"]
            }
        }

    async def execute(self, skill_name: str, list_files: bool = False) -> str:
        """
        加载并返回skill内容

        Args:
            skill_name: 要加载的技能名称
            list_files: 如果为True，只列出文件而不是加载内容

        Returns:
            技能内容或错误信息
        """
        # 如果 skill_name 为空，返回错误提示
        if not skill_name:
            available = ", ".join(self.enabled_skills)
            return f"Error: No skill name provided. Available skills: {available}"

        # 尝试匹配skill名称（支持模糊匹配）
        actual_skill_name = self._match_skill_name(skill_name)
        if not actual_skill_name:
            available_skills = self._format_available_skills()
            return f"Error: Skill '{skill_name}' is not available.\n\n{available_skills}"

        # 使用匹配后的名称
        skill_name = actual_skill_name

        # 检查缓存
        if skill_name in self._loaded_skills:
            if list_files:
                # 即使缓存了，也返回文件列表
                skill = self.skill_registry.get_skill(skill_name)
                if skill and skill.files:
                    return f"Files in skill '{skill_name}':\n" + "\n".join([f"  - {f}" for f in skill.files])
            return f"[Skill '{skill_name}' already loaded]\n\n{self._loaded_skills[skill_name]}"

        # 获取技能配置
        skill = self.skill_registry.get_skill(skill_name)
        if not skill:
            return f"Error: Skill '{skill_name}' not found in registry"

        if list_files:
            # 只列出文件
            if skill.files:
                return f"Files in skill '{skill_name}':\n" + "\n".join([f"  - {f}" for f in skill.files])
            else:
                return f"No files found for skill '{skill_name}'"

        # 加载技能内容
        try:
            content = SkillLoader.load_skill_content(skill, self.skills_dir)
            if not content:
                content = f"# {skill.name}\n\n{skill.description}"

            # 缓存内容
            self._loaded_skills[skill_name] = content

            return f"=== Skill Loaded: {skill.name} ===\n\n{content}\n\n=== End of Skill ==="

        except Exception as e:
            return f"Error loading skill '{skill_name}' ({type(e).__name__})"

    async def execute_script(
        self,
        skill_name: str,
        script_name: str = "main.py",
        arguments: List[str] = None,
        input_file_ids: List[str] = None,
        timeout: int = 60
    ) -> str:
        """
        执行Skill脚本

        Args:
            skill_name: Skill名称
            script_name: 脚本文件名（默认main.py）
            arguments: 命令行参数
            input_file_ids: 输入文件ID列表
            timeout: 超时时间（秒）

        Returns:
            执行结果字符串
        """
        if not self.execution_engine:
            return "Error: Execution engine not available"

        # 技能名和启用列表可由用户配置，日志只记录基数。
        print(f"[DEBUG] execute_script: enabled_skill_count={len(self.enabled_skills)}")

        actual_skill_name = self._match_skill_name(skill_name)
        if not actual_skill_name:
            available_skills = self._format_available_skills()
            return f"Error: Skill '{skill_name}' is not enabled.\n\n{available_skills}"

        # 使用匹配后的名称
        skill_name = actual_skill_name

        # 获取Skill配置
        skill = self.skill_registry.get_skill(skill_name)
        if not skill:
            return f"Error: Skill '{skill_name}' not found"

        # 获取脚本路径
        script_path = self._get_script_path(skill, script_name)
        if not script_path or not script_path.exists():
            available_scripts = self._list_skill_scripts(skill)
            if available_scripts:
                return f"Error: Script '{script_name}' not found. Available scripts: {', '.join(available_scripts)}"
            return f"Error: Skill '{skill_name}' has no executable scripts"

        # 获取技能基础路径（用于复制技能文件到工作目录）
        skill_base_path = self.skills_dir / skill.skill_path / "scripts"

        # 执行脚本
        try:
            record = await self.execution_engine.execute_script(
                agent_name=self.agent_name,
                skill_name=skill_name,
                script_path=script_name,  # 只传递脚本文件名，ExecutionEngine会从skill_base_path复制
                args=arguments or [],
                input_file_ids=input_file_ids or [],
                timeout=timeout,
                skill_base_path=str(skill_base_path)
            )

            # 格式化返回结果
            result_lines = [
                f"=== Execution Result ===",
                f"Status: {record.status.value}",
                f"Duration: {record.duration_ms}ms" if record.duration_ms else "",
            ]

            if record.exit_code is not None:
                result_lines.append(f"Exit Code: {record.exit_code}")

            if record.stdout:
                result_lines.append(f"\n--- Output ---\n{record.stdout}")

            if record.stderr:
                result_lines.append(f"\n--- Error Output ---\n{record.stderr}")

            result_lines.append("\n=== End of Result ===")

            return "\n".join(result_lines)

        except Exception as e:
            return f"Error executing script ({type(e).__name__})"

    def _detect_executable_skills(self) -> List[Dict[str, Any]]:
        """检测哪些Skill有可执行脚本"""
        executable = []
        for skill_name in self.enabled_skills:
            skill = self.skill_registry.get_skill(skill_name)
            if skill:
                scripts = self._list_skill_scripts(skill)
                if scripts:
                    executable.append({
                        "name": skill_name,
                        "description": skill.description[:80] + "..." if len(skill.description) > 80 else skill.description,
                        "scripts": scripts
                    })
        return executable

    def _list_skill_scripts(self, skill) -> List[str]:
        """列出Skill的可执行脚本"""
        scripts_dir = self.skills_dir / skill.skill_path / "scripts"
        if not scripts_dir.exists():
            return []
        return [f.name for f in scripts_dir.glob("*.py")]

    def _get_script_path(self, skill, script_name: str) -> Optional[Path]:
        """获取脚本路径"""
        script_path = self.skills_dir / skill.skill_path / "scripts" / script_name
        if script_path.exists():
            return script_path
        return None

    def _format_executable_skills(self, executable_skills: List[Dict[str, Any]]) -> str:
        """格式化可执行技能列表"""
        lines = []
        for skill in executable_skills:
            scripts_str = ", ".join(skill["scripts"])
            lines.append(f"  - {skill['name']}: {skill['description']}")
            lines.append(f"    Scripts: {scripts_str}")
        return "\n".join(lines)

    def _format_executable_skills_with_actions(self, executable_skills: List[Dict[str, Any]]) -> str:
        """格式化可执行技能列表（包含可用操作）"""
        lines = []
        for skill in executable_skills:
            name = skill['name']
            desc = skill['description']

            # 规范化名称用于比较（支持大小写不敏感匹配）
            normalized_name = self._normalize_name(name)

            # 根据 Skill 名称提供操作建议
            if normalized_name == "ab-pdf":
                actions = """
    可用操作:
      - extract_text: 提取 PDF 文本内容
      - extract_forms: 提取表单字段信息
      - fill_form: 填充表单字段
      - convert_images: 转换为图片"""
            elif normalized_name == "ab-docx":
                actions = """
    可用操作:
      - extract_text: 提取文档文本
      - create_document: 创建新文档
      - convert_to_pdf: 转换为 PDF
      - get_info: 获取文档信息"""
            else:
                actions = ""

            lines.append(f"  - {name}: {desc}{actions}")
        return "\n".join(lines)

    def is_skill_loaded(self, skill_name: str) -> bool:
        """检查技能是否已加载"""
        return skill_name in self._loaded_skills

    def get_loaded_skills(self) -> List[str]:
        """获取已加载的技能列表"""
        return list(self._loaded_skills.keys())

    def clear_cache(self):
        """清除缓存"""
        self._loaded_skills.clear()

    def get_skill_description(self, skill_name: str) -> str:
        """获取指定技能的描述信息

        Args:
            skill_name: 技能名称

        Returns:
            str: 技能描述（来自 skill.yaml 的 description 字段），如果找不到返回空字符串
        """
        if not self.skill_registry:
            return ""

        skill = self.skill_registry.get_skill(skill_name)
        if skill:
            return skill.description or ""
        return ""

    def get_all_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        获取所有工具定义

        Returns:
            工具定义列表
        """
        definitions = [self.get_tool_definition()]

        execute_def = self.get_execute_tool_definition()
        if execute_def:
            definitions.append(execute_def)

        return definitions

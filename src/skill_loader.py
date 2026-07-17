"""
Skill加载器 - 加载Skill内容并组装系统提示词
"""
from pathlib import Path
from typing import List, Optional

from .models import SkillConfig
from .skill_registry import SkillRegistry


class SkillLoader:
    """Skill内容加载器"""

    @staticmethod
    def load_skill_content(skill: SkillConfig, skills_dir: Path) -> str:
        """
        加载单个Skill的SKILL.md内容

        Args:
            skill: Skill配置
            skills_dir: Skills根目录

        Returns:
            Skill的完整内容
        """
        skill_path = skills_dir / skill.skill_path / "SKILL.md"

        if not skill_path.exists():
            return f"# {skill.name}\n\n{skill.description}"

        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"加载Skill内容失败: error_type={type(e).__name__}")
            return f"# {skill.name}\n\n{skill.description}"

    @staticmethod
    def load_skills_for_agent(
        skill_names: List[str],
        registry: SkillRegistry,
        skills_dir: Path
    ) -> str:
        """
        为Agent加载所有选中的Skills，拼接成系统提示词

        Args:
            skill_names: Skill名称列表
            registry: Skill注册表
            skills_dir: Skills根目录

        Returns:
            拼接后的Skills提示词
        """
        if not skill_names:
            return ""

        skills = registry.get_skills_by_names(skill_names)
        if not skills:
            return ""

        loaded_contents = []

        for skill in skills:
            if not skill.enabled:
                continue

            content = SkillLoader.load_skill_content(skill, skills_dir)
            if content:
                loaded_contents.append(f"---\n## Skill: {skill.name}\n\n{content}\n---")

        if not loaded_contents:
            return ""

        return "\n\n".join(loaded_contents)

    @staticmethod
    def get_skill_system_prompt_section(
        skill_names: List[str],
        registry: SkillRegistry,
        skills_dir: Path
    ) -> str:
        """
        获取Skills的系统提示词部分

        Args:
            skill_names: Skill名称列表
            registry: Skill注册表
            skills_dir: Skills根目录

        Returns:
            格式化的Skills系统提示词
        """
        skills_prompt = SkillLoader.load_skills_for_agent(skill_names, registry, skills_dir)

        if not skills_prompt:
            return ""

        return f"""
# 可用技能 (Skills)

以下是你可使用的技能，请在处理相关任务时参考这些技能的指导：

{skills_prompt}

---
"""

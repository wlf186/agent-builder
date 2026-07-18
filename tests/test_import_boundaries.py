"""AST-enforced dependency boundary for the framework-neutral V2 core."""

from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
LEGACY_MODULES = frozenset(
    {path.stem for path in REPOSITORY_ROOT.glob("*.py")}
    | {path.stem for path in (REPOSITORY_ROOT / "src").glob("*.py")}
)


def _is_forbidden(module: str) -> bool:
    return (
        module.split(".", 1)[0] in LEGACY_MODULES
        or module == "src"
        or module.startswith("src.")
        or module.startswith("langgraph")
        or module.startswith("langchain")
    )


def test_v2_source_has_no_framework_or_legacy_imports() -> None:
    violations: list[str] = []

    for source_file in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), source_file)
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_modules.append(node.module)
            for module in imported_modules:
                if _is_forbidden(module):
                    relative_path = source_file.relative_to(REPOSITORY_ROOT)
                    violations.append(f"{relative_path}:{node.lineno}: {module}")

    assert violations == [], "forbidden imports:\n" + "\n".join(violations)

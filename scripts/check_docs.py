#!/usr/bin/env python3
"""Validate repository documentation invariants without third-party packages."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LIFECYCLE_SCRIPTS = ("bootstrap.sh", "start.sh", "stop.sh", "purge.sh")
MAINTAINED_DOC_ROOTS = (
    ROOT / "README.md",
    ROOT / "CLAUDE.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "frontend" / "README.md",
    ROOT / "docs",
    ROOT / "docs-site",
)
IGNORED_DOC_PREFIXES = (
    ROOT / "docs" / "superpowers",
    ROOT / "docs-site" / "node_modules",
    ROOT / "docs-site" / ".vitepress" / "cache",
    ROOT / "docs-site" / ".vitepress" / "dist",
)
README_REQUIRED_HEADINGS = (
    "## Supported deployment",
    "## Host prerequisites",
    "## Network and capacity",
    "## Start the complete stack",
    "## First use",
    "## Troubleshooting",
)
PORT_CONTRACTS = (
    ("FRONTEND_PORT", "Web application", "Frontend"),
    ("BACKEND_PORT", "Authenticated backend", "Backend API"),
    ("MCP_SSE_PORT", "Built-in MCP SSE", "Built-in MCP SSE"),
    ("DOCS_PORT", "User guide", "User guide"),
    ("PHOENIX_PORT", "Phoenix trace dashboard", "Local traces"),
)


def git_visible_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: list[Path] = []
    for raw_path in result.stdout.decode("utf-8").split("\0"):
        if not raw_path:
            continue
        path = ROOT / raw_path
        # A tracked deletion remains in the index until commit. It is not part
        # of the resulting tree and must not make a local pre-commit check fail.
        if path.exists() or path.is_symlink():
            paths.append(path)
    return paths


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def maintained_markdown_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.suffix.lower() != ".md" or path.is_symlink():
            continue
        if any(is_relative_to(path, ignored) for ignored in IGNORED_DOC_PREFIXES):
            continue
        if any(path == root or is_relative_to(path, root) for root in MAINTAINED_DOC_ROOTS):
            files.append(path)
    return files


def validate_agent_guide_symlink(failures: list[str]) -> None:
    agents = ROOT / "AGENTS.md"
    claude = ROOT / "CLAUDE.md"
    if not agents.is_symlink():
        failures.append("AGENTS.md must be a symbolic link")
        return
    if os.readlink(agents) != "CLAUDE.md":
        failures.append("AGENTS.md must link exactly to CLAUDE.md")
    try:
        if agents.resolve(strict=True) != claude.resolve(strict=True):
            failures.append("AGENTS.md does not resolve to CLAUDE.md")
    except FileNotFoundError:
        failures.append("AGENTS.md is a broken symbolic link")


def lifecycle_help(script: str, failures: list[str]) -> str:
    path = ROOT / script
    if not path.is_file():
        failures.append(f"missing lifecycle script: {script}")
        return ""
    if not os.access(path, os.X_OK):
        failures.append(f"lifecycle script is not executable: {script}")
    result = subprocess.run(
        [str(path), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        failures.append(f"{script} --help exited with {result.returncode}")
    return result.stdout + result.stderr


def documented_flags(claude: str, script: str) -> set[str]:
    pattern = re.compile(
        rf"^- `{re.escape(script)}`:(?P<body>.*(?:\n  .*)*)$",
        re.MULTILINE,
    )
    match = pattern.search(claude)
    if not match:
        return set()
    return set(re.findall(r"--[a-z][a-z0-9-]*", match.group("body")))


def validate_lifecycle_commands(
    markdown_files: list[Path], failures: list[str]
) -> None:
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    help_by_script: dict[str, str] = {}

    for script in LIFECYCLE_SCRIPTS:
        for document_name, content in (("README.md", readme), ("CLAUDE.md", claude)):
            if f"./{script}" not in content:
                failures.append(f"{document_name} does not document ./{script}")
        help_text = lifecycle_help(script, failures)
        help_by_script[script] = help_text
        actual = set(re.findall(r"--[a-z][a-z0-9-]*", help_text))
        documented = documented_flags(claude, script)
        if actual != documented:
            failures.append(
                f"{script} option drift: help={sorted(actual)}, "
                f"CLAUDE.md={sorted(documented)}"
            )

    purge_scopes = set(
        re.findall(r"^  ([a-z][a-z-]+)\s{2,}", help_by_script["purge.sh"], re.MULTILINE)
    )
    purge_section = re.search(
        r"^- `purge\.sh`:(?P<body>.*(?:\n  .*)*)$", claude, re.MULTILINE
    )
    documented_scopes = set()
    if purge_section:
        documented_scopes = {
            value
            for value in re.findall(r"`([a-z][a-z-]+)`", purge_section.group("body"))
            if not value.endswith(".sh")
        }
    if purge_scopes != documented_scopes:
        failures.append(
            f"purge scope drift: help={sorted(purge_scopes)}, "
            f"CLAUDE.md={sorted(documented_scopes)}"
        )

    command_pattern = re.compile(r"(?<![A-Za-z0-9_])\./([A-Za-z0-9_./-]+\.sh)\b")
    for document in markdown_files:
        content = document.read_text(encoding="utf-8")
        for match in command_pattern.finditer(content):
            referenced = ROOT / match.group(1)
            if not referenced.is_file():
                relative = document.relative_to(ROOT)
                failures.append(
                    f"{relative}: documented command ./{match.group(1)} does not exist"
                )


def _assignment_default(content: str, variable: str) -> str | None:
    match = re.search(
        rf'^export {re.escape(variable)}="\$\{{{re.escape(variable)}:-([^}}]+)\}}"$',
        content,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def validate_deployment_contract(failures: list[str]) -> None:
    """Keep runtime defaults, operator docs, and browser fallbacks aligned."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    env = (ROOT / "env.sh").read_text(encoding="utf-8")
    security = (ROOT / "src" / "security.py").read_text(encoding="utf-8")
    frontend_package = (ROOT / "frontend" / "package.json").read_text(
        encoding="utf-8"
    )
    frontend_origin = (ROOT / "frontend" / "src" / "lib" / "serverOrigin.ts").read_text(
        encoding="utf-8"
    )
    playwright = (ROOT / "frontend" / "playwright.config.ts").read_text(
        encoding="utf-8"
    )

    for heading in README_REQUIRED_HEADINGS:
        if heading not in readme:
            failures.append(f"README.md is missing required deployment section: {heading}")

    defaults: dict[str, str] = {}
    for variable, readme_label, claude_label in PORT_CONTRACTS:
        value = _assignment_default(env, variable)
        if value is None or not value.isdigit() or not 1 <= int(value) <= 65535:
            failures.append(f"env.sh has no valid default for {variable}")
            continue
        defaults[variable] = value
        if f"| {readme_label} | {value} |" not in readme:
            failures.append(f"README.md port drift for {variable}={value}")
        claude_pattern = re.compile(
            rf"^\| {re.escape(claude_label)} \| `http://127\.0\.0\.1:{value}` \|$",
            re.MULTILINE,
        )
        if not claude_pattern.search(claude):
            failures.append(f"CLAUDE.md port drift for {variable}={value}")

    frontend_port = defaults.get("FRONTEND_PORT")
    if frontend_port:
        expected_url = f"http://127.0.0.1:{frontend_port}"
        for relative in (
            "README.md",
            "docs-site/en/getting-started.md",
            "docs-site/zh/getting-started.md",
        ):
            if expected_url not in (ROOT / relative).read_text(encoding="utf-8"):
                failures.append(f"{relative} does not document {expected_url}")
        if security.count(f'"http://127.0.0.1:{frontend_port}"') != 1:
            failures.append("src/security.py frontend CORS default drift")
        if security.count(f'"http://localhost:{frontend_port}"') != 1:
            failures.append("src/security.py localhost CORS default drift")
        if frontend_package.count(f"${{FRONTEND_PORT:-{frontend_port}}}") != 2:
            failures.append("frontend/package.json default port drift")
        if f"DEFAULT_FRONTEND_PORT = '{frontend_port}'" not in frontend_origin:
            failures.append("frontend server-origin default port drift")
        if expected_url not in playwright:
            failures.append("Playwright default frontend URL drift")

    code_markers: dict[str, tuple[tuple[str, str], ...]] = {
        "FRONTEND_PORT": (
            ("scripts/clean_clone_smoke.py", 'port("FRONTEND_PORT", {port})'),
        ),
        "BACKEND_PORT": (
            ("backend.py", 'os.environ.get("PORT", {port})'),
            (
                "frontend/src/app/api/[...path]/route.ts",
                "http://127.0.0.1:{port}",
            ),
            (
                "frontend/src/app/stream/agents/[name]/chat/route.ts",
                "http://127.0.0.1:{port}",
            ),
            ("scripts/clean_clone_smoke.py", 'port("BACKEND_PORT", {port})'),
        ),
        "MCP_SSE_PORT": (
            ("src/builtin_services.py", 'os.environ.get("MCP_SSE_PORT", "{port}")'),
            ("src/mcp_manager.py", 'os.environ.get("MCP_SSE_PORT", "{port}")'),
            ("scripts/clean_clone_smoke.py", 'port("MCP_SSE_PORT", {port})'),
        ),
        "DOCS_PORT": (
            ("frontend/next.config.ts", "process.env.DOCS_PORT || '{port}'"),
            ("scripts/clean_clone_smoke.py", 'port("DOCS_PORT", {port})'),
        ),
        "PHOENIX_PORT": (
            (
                "frontend/src/app/api/redirect/observability/route.ts",
                "process.env.PHOENIX_PORT || '{port}'",
            ),
            (
                "src/observability/otel_tracer.py",
                "http://127.0.0.1:{port}/v1/traces",
            ),
            ("scripts/clean_clone_smoke.py", 'port("PHOENIX_PORT", {port})'),
        ),
    }
    for variable, contracts in code_markers.items():
        value = defaults.get(variable)
        if value is None:
            continue
        for relative, marker_template in contracts:
            marker = marker_template.format(port=value)
            content = (ROOT / relative).read_text(encoding="utf-8")
            if marker not in content:
                failures.append(f"{relative} default drift for {variable}={value}")

    documentation_markers: dict[str, tuple[tuple[str, str], ...]] = {
        "FRONTEND_PORT": (
            ("frontend/README.md", "http://127.0.0.1:{port}"),
            ("docs-site/en/getting-started.md", "http://127.0.0.1:{port}"),
            ("docs-site/zh/getting-started.md", "http://127.0.0.1:{port}"),
            ("docs/references/playwright-test-cases.md", "http://localhost:{port}"),
        ),
        "BACKEND_PORT": (
            ("frontend/README.md", "http://127.0.0.1:{port}"),
            ("docs/references/api-reference.md", "http://127.0.0.1:{port}"),
            (
                "docs/design-docs/playwright-headed-guide.md",
                "http://localhost:{port}",
            ),
        ),
        "DOCS_PORT": (("frontend/README.md", "port `{port}`"),),
        "PHOENIX_PORT": (
            ("docs-site/en/advanced/observability.md", "http://127.0.0.1:{port}"),
            ("docs-site/zh/advanced/observability.md", "http://127.0.0.1:{port}"),
        ),
    }
    for variable, contracts in documentation_markers.items():
        value = defaults.get(variable)
        if value is None:
            continue
        for relative, marker_template in contracts:
            marker = marker_template.format(port=value)
            content = (ROOT / relative).read_text(encoding="utf-8")
            if marker not in content:
                failures.append(f"{relative} documentation drift for {variable}={value}")

    bootstrap = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    versions = {}
    for variable in ("UV_VERSION", "PYTHON_VERSION", "NODE_VERSION"):
        match = re.search(rf'^{variable}="([^"]+)"$', bootstrap, re.MULTILINE)
        if not match:
            failures.append(f"bootstrap.sh is missing {variable}")
        else:
            versions[variable] = match.group(1)
    if versions:
        version_sentence = (
            f"Bootstrap pins uv {versions.get('UV_VERSION')}, Python "
            f"{versions.get('PYTHON_VERSION')}, and Node.js "
            f"{versions.get('NODE_VERSION')}"
        )
        if version_sentence not in readme:
            failures.append("README.md toolchain versions drift from bootstrap.sh")
        python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
        if python_version != versions.get("PYTHON_VERSION"):
            failures.append(".python-version drifts from bootstrap.sh")


def validate_governance_review_freshness(failures: list[str]) -> None:
    governance = (ROOT / "docs" / "DOCUMENTATION.md").read_text(encoding="utf-8")
    review_dates = [
        date.fromisoformat(value)
        for value in re.findall(
            r"^\| (\d{4}-\d{2}-\d{2}) \| Full audit \|",
            governance,
            re.MULTILINE,
        )
    ]
    if not review_dates:
        failures.append("documentation review ledger has no dated full-audit entry")
        return
    latest = max(review_dates)
    age_days = (date.today() - latest).days
    if age_days < 0:
        failures.append(f"documentation review ledger contains a future date: {latest}")
    elif age_days > 92:
        failures.append(
            f"documentation full-audit ledger is stale ({age_days} days; maximum 92)"
        )


def main() -> int:
    failures: list[str] = []
    paths = git_visible_files()
    markdown_files = maintained_markdown_files(paths)
    validate_agent_guide_symlink(failures)
    validate_lifecycle_commands(markdown_files, failures)
    validate_deployment_contract(failures)
    validate_governance_review_freshness(failures)

    if failures:
        print("Documentation governance failures:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"Validated {len(markdown_files)} maintained Markdown files, "
        f"{len(paths)} tracked/unignored repository files, lifecycle help, "
        "deployment defaults, review freshness, and the AGENTS.md symlink."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

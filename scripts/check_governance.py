#!/usr/bin/env python3
"""Dependency-free governance checks for the greenfield runtime repository."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
MAX_REVIEW_AGE_DAYS = 92
MAX_SCANNED_TEXT_BYTES = 5_000_000

# These trees are either generated state, project-local toolchains, immutable
# reference material, or the quarantined V1 snapshot. They are deliberately
# outside the maintained-source governance surface.
EXCLUDED_ROOTS = (
    Path(".git"),
    Path(".runtime"),
    Path(".tools"),
    Path(".venv"),
    Path("_legacy-reference"),
    Path("references/claude-code/materials"),
)

REQUIRED_DOCUMENTS = (
    Path("CLAUDE.md"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("docs/DOCUMENTATION.md"),
    Path("docs/PRINCIPLES.md"),
    Path("docs/design/architecture.md"),
    Path("docs/design/event-protocol.md"),
    Path("docs/design/agent-capsule.md"),
    Path("docs/plans/runtime-rebuild.md"),
    Path("references/claude-code/README.md"),
    Path("references/claude-code/PROVENANCE.md"),
)

ROOT_COMMANDS = (
    "bootstrap.sh",
    "start.sh",
    "stop.sh",
    "set-access-token.sh",
    "purge.sh",
    "governance.sh",
)

FORBIDDEN_OLD_ROOT_PATHS = (
    Path("harness-v2"),
    Path("backend.py"),
    Path("frontend"),
    Path("docs-site"),
    Path("builtin_mcp_services"),
    Path("skills"),
    Path("teams"),
    Path("rag-kb-demo"),
    Path("package.json"),
    Path("package-lock.json"),
    Path("requirements.txt"),
    Path("start_backend.sh"),
    Path("badcase.md"),
    Path("CONTRIBUTING.md"),
)

FORBIDDEN_CURRENT_RUNTIME_MARKERS = (
    "langgraph",
    "langchain",
    "_legacy-reference",
    "references/",
    ".runtime/harness-v2-prototype",
    "data/harness-v2-prototype",
    "harness-v2/src",
)

FORBIDDEN_MAINTAINED_DOC_PATH_MARKERS = (
    "./harness-v2/",
    "`harness-v2/",
    ".runtime/harness-v2-prototype",
    "data/harness-v2-prototype",
)

VALID_DOCUMENT_STATUSES = frozenset({"maintained", "active", "reference"})
FRONT_MATTER_FIELDS = frozenset(
    {"owner", "status", "last_reviewed", "review_cycle"}
)

CREDENTIAL_PATTERNS = (
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    (
        "GitHub fine-grained token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b")),
    (
        "provider API key",
        re.compile(r"\bsk-(?:live|test|proj)-[A-Za-z0-9_-]{16,}\b"),
    ),
    ("generic provider API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("credential in URL", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@")),
)

MARKDOWN_LINK_PATTERN = re.compile(
    r"!?\[[^\]]*\]\("
    r"(?P<target><[^>]+>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?"
    r"\)",
)
FENCED_CODE_PATTERN = re.compile(
    r"^(?:```|~~~).*?^(?:```|~~~)\s*$", re.MULTILINE | re.DOTALL
)
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]*`")


def relative(path: Path) -> str:
    """Return a stable repository-relative display path."""

    return path.relative_to(ROOT).as_posix()


def is_excluded(relative_path: Path) -> bool:
    """Return whether a path belongs to an explicitly excluded tree."""

    return any(
        relative_path == excluded or excluded in relative_path.parents
        for excluded in EXCLUDED_ROOTS
    )


def repository_files() -> list[Path]:
    """List active repository files without following directory symlinks."""

    files: list[Path] = []
    for directory, names, filenames in os.walk(ROOT, topdown=True, followlinks=False):
        base = Path(directory)
        kept_directories: list[str] = []
        for name in names:
            child = base / name
            child_relative = child.relative_to(ROOT)
            if not is_excluded(child_relative) and not child.is_symlink():
                kept_directories.append(name)
        names[:] = kept_directories
        for filename in filenames:
            path = base / filename
            path_relative = path.relative_to(ROOT)
            if not is_excluded(path_relative):
                files.append(path)
    return sorted(files)


def read_text(path: Path, failures: list[str]) -> str | None:
    """Read a small UTF-8 text file and report malformed maintained input."""

    try:
        if path.stat().st_size > MAX_SCANNED_TEXT_BYTES:
            failures.append(
                f"{relative(path)}: maintained text exceeds "
                f"{MAX_SCANNED_TEXT_BYTES} bytes"
            )
            return None
        raw = path.read_bytes()
    except OSError as error:
        failures.append(f"{relative(path)}: cannot be read: {error}")
        return None
    if b"\0" in raw:
        failures.append(f"{relative(path)}: maintained text contains NUL bytes")
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        failures.append(f"{relative(path)}: is not valid UTF-8: {error}")
        return None


def validate_required_paths(failures: list[str]) -> None:
    for document in REQUIRED_DOCUMENTS:
        path = ROOT / document
        if not path.is_file() or path.is_symlink():
            failures.append(f"missing required regular document: {document.as_posix()}")

    for old_path in FORBIDDEN_OLD_ROOT_PATHS:
        path = ROOT / old_path
        if path.exists() or path.is_symlink():
            failures.append(
                f"old system path must exist only under _legacy-reference/: "
                f"{old_path.as_posix()}"
            )

    source_root = ROOT / "src"
    if source_root.is_dir():
        for direct_python in sorted(source_root.glob("*.py")):
            failures.append(
                f"legacy-style top-level source module is forbidden: "
                f"{relative(direct_python)}"
            )

    for runtime_root_name in ("src", "config"):
        runtime_root = ROOT / runtime_root_name
        if not runtime_root.is_dir() or runtime_root.is_symlink():
            continue
        for directory, names, _ in os.walk(runtime_root, followlinks=False):
            base = Path(directory)
            for name in names:
                child = base / name
                if child.is_symlink():
                    failures.append(
                        f"{relative(child)}: current runtime/config directory "
                        "must not be a symbolic link"
                    )


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
    except OSError:
        failures.append("AGENTS.md is a broken symbolic link")


def parse_front_matter(
    path: Path, content: str, failures: list[str]
) -> dict[str, str] | None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        failures.append(f"{relative(path)}: must start with YAML front matter")
        return None

    end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        failures.append(f"{relative(path)}: YAML front matter is not closed")
        return None

    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_]*)\s*:\s*(.*?)\s*", line)
        if match is None:
            failures.append(
                f"{relative(path)}: unsupported front matter line: {line!r}"
            )
            continue
        key, value = match.groups()
        value = value.strip().strip("\"'")
        if key in values:
            failures.append(f"{relative(path)}: duplicate metadata field {key!r}")
        else:
            values[key] = value

    missing = sorted(FRONT_MATTER_FIELDS - values.keys())
    if missing:
        failures.append(
            f"{relative(path)}: missing metadata fields: {', '.join(missing)}"
        )
        return None
    return values


def validate_document_metadata(markdown_files: list[Path], failures: list[str]) -> None:
    governed: set[Path] = {
        ROOT / "SECURITY.md",
        ROOT / "references/claude-code/README.md",
        ROOT / "references/claude-code/PROVENANCE.md",
    }
    docs_root = ROOT / "docs"
    governed.update(path for path in markdown_files if docs_root in path.parents)

    for path in sorted(governed):
        if not path.is_file() or path.is_symlink():
            continue
        content = read_text(path, failures)
        if content is None:
            continue
        metadata = parse_front_matter(path, content, failures)
        if metadata is None:
            continue

        owner = metadata["owner"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", owner):
            failures.append(f"{relative(path)}: invalid metadata owner {owner!r}")

        status = metadata["status"]
        if status not in VALID_DOCUMENT_STATUSES:
            failures.append(f"{relative(path)}: invalid metadata status {status!r}")
        path_relative = path.relative_to(ROOT)
        if path_relative.parts[:2] == ("references", "claude-code"):
            expected_status = "reference"
        elif path_relative.parts[:2] == ("docs", "plans"):
            expected_status = "active"
        else:
            expected_status = "maintained"
        if status != expected_status:
            failures.append(
                f"{relative(path)}: status must be {expected_status!r}, got {status!r}"
            )
        if metadata["review_cycle"] != "quarterly":
            failures.append(
                f"{relative(path)}: review_cycle must be 'quarterly'"
            )

        try:
            reviewed = date.fromisoformat(metadata["last_reviewed"])
        except ValueError:
            failures.append(
                f"{relative(path)}: last_reviewed must be an ISO date"
            )
            continue
        age = (date.today() - reviewed).days
        if age < 0:
            failures.append(f"{relative(path)}: last_reviewed is in the future")
        elif age > MAX_REVIEW_AGE_DAYS:
            failures.append(
                f"{relative(path)}: review is stale ({age} days; "
                f"maximum {MAX_REVIEW_AGE_DAYS})"
            )


def validate_principles_and_operator_contract(failures: list[str]) -> None:
    contents: dict[str, str] = {}
    for name in ("CLAUDE.md", "README.md", "docs/PRINCIPLES.md"):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            content = read_text(path, failures)
            if content is not None:
                contents[name] = content

    for name in ("CLAUDE.md", "docs/PRINCIPLES.md"):
        content = contents.get(name)
        if content is None:
            continue
        for number in range(1, 9):
            if re.search(rf"(?<![A-Za-z0-9])P{number}(?![A-Za-z0-9])", content) is None:
                failures.append(f"{name}: missing project principle marker P{number}")

    claude = contents.get("CLAUDE.md")
    if claude is not None and re.search(r"(?<![A-Za-z0-9])DoD(?![A-Za-z0-9])", claude) is None:
        failures.append("CLAUDE.md: missing Definition of Done marker DoD")

    for name in ("CLAUDE.md", "README.md"):
        content = contents.get(name)
        if content is None:
            continue
        if "0.0.0.0:20815" not in content:
            failures.append(f"{name}: must document 0.0.0.0:20815")
        for command in ROOT_COMMANDS:
            if f"./{command}" not in content:
                failures.append(f"{name}: must document ./{command}")
        for marker in FORBIDDEN_MAINTAINED_DOC_PATH_MARKERS:
            if marker.casefold() in content.casefold():
                failures.append(f"{name}: contains retired path marker {marker!r}")


def markdown_without_code(content: str) -> str:
    content = FENCED_CODE_PATTERN.sub("", content)
    return INLINE_CODE_PATTERN.sub("", content)


def validate_markdown_links(markdown_files: list[Path], failures: list[str]) -> None:
    for path in markdown_files:
        if path.is_symlink():
            continue
        content = read_text(path, failures)
        if content is None:
            continue
        for match in MARKDOWN_LINK_PATTERN.finditer(markdown_without_code(content)):
            raw_target = match.group("target")
            if raw_target.startswith("<") and raw_target.endswith(">"):
                raw_target = raw_target[1:-1]
            target = unquote(raw_target).strip()
            if not target or target.startswith("#"):
                continue

            parsed = urlsplit(target)
            if parsed.scheme:
                if parsed.scheme.casefold() not in {"http", "https", "mailto"}:
                    failures.append(
                        f"{relative(path)}: unsupported link scheme in {raw_target!r}"
                    )
                continue
            if target.startswith("//"):
                failures.append(
                    f"{relative(path)}: protocol-relative link is forbidden: "
                    f"{raw_target!r}"
                )
                continue
            if parsed.path.startswith("/"):
                failures.append(
                    f"{relative(path)}: repository links must be relative: "
                    f"{raw_target!r}"
                )
                continue
            if not parsed.path:
                continue

            candidate = (path.parent / parsed.path).resolve(strict=False)
            try:
                candidate.relative_to(ROOT)
            except ValueError:
                failures.append(
                    f"{relative(path)}: link escapes the repository: {raw_target!r}"
                )
                continue
            if not candidate.exists():
                failures.append(
                    f"{relative(path)}: broken local link {raw_target!r}"
                )


def active_runtime_files(files: list[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        path_relative = path.relative_to(ROOT)
        parts = path_relative.parts
        if not parts:
            continue
        if parts[0] == "src" or parts[0] == "config":
            selected.append(path)
        elif parts[0] == "scripts" and path.name != "check_governance.py":
            selected.append(path)
        elif len(parts) == 1 and (
            path.suffix in {".sh", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"}
            or path.name in {"uv.lock", ".python-version"}
        ):
            selected.append(path)
    return selected


def shell_code_without_comments_or_heredocs(content: str) -> str:
    """Remove shell prose before checking whether paths are runtime-wired.

    Lifecycle help is expected to explain that the quarantine is never a
    purge target. That prose is not an executable path dependency. This small
    lexer intentionally handles only the quoted/unquoted single-word heredoc
    delimiters used by this repository; ``bash -n`` remains the syntax oracle.
    """

    kept: list[str] = []
    heredoc_end: str | None = None
    heredoc_pattern = re.compile(r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z0-9_]+))")
    for line in content.splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
        match = heredoc_pattern.search(line)
        if match is not None:
            heredoc_end = next(value for value in match.groups() if value is not None)
    return "\n".join(kept)


def validate_runtime_boundaries(files: list[Path], failures: list[str]) -> None:
    for path in active_runtime_files(files):
        if path.is_symlink():
            failures.append(
                f"{relative(path)}: current runtime/config must not be a symbolic link"
            )
            continue
        try:
            if path.stat().st_size > MAX_SCANNED_TEXT_BYTES:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            failures.append(f"{relative(path)}: cannot inspect runtime boundary: {error}")
            continue
        if path.suffix == ".sh":
            content = shell_code_without_comments_or_heredocs(content)
        folded = content.casefold()
        for marker in FORBIDDEN_CURRENT_RUNTIME_MARKERS:
            if marker in folded:
                failures.append(
                    f"{relative(path)}: current runtime references forbidden "
                    f"dependency/path {marker!r}"
                )


def validate_shells(files: list[Path], failures: list[str]) -> int:
    shell_files = [path for path in files if path.suffix == ".sh" and not path.is_symlink()]
    for command in ROOT_COMMANDS:
        path = ROOT / command
        if not path.is_file() or path.is_symlink():
            failures.append(f"missing root lifecycle command: {command}")
            continue
        if not os.access(path, os.X_OK):
            failures.append(f"root lifecycle command is not executable: {command}")

    for path in shell_files:
        try:
            result = subprocess.run(
                ["bash", "-n", str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            failures.append(f"{relative(path)}: bash syntax check failed to run: {error}")
            continue
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            failures.append(
                f"{relative(path)}: bash -n failed"
                + (f": {detail}" if detail else "")
            )
    return len(shell_files)


def validate_credentials(files: list[Path], failures: list[str]) -> int:
    scanned = 0
    for path in files:
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_SCANNED_TEXT_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for label, pattern in CREDENTIAL_PATTERNS:
            if pattern.search(content):
                failures.append(f"{relative(path)}: potential {label}")
    return scanned


def main() -> int:
    failures: list[str] = []
    files = repository_files()
    markdown_files = [
        path for path in files if path.suffix.casefold() == ".md" and not path.is_symlink()
    ]

    validate_required_paths(failures)
    validate_agent_guide_symlink(failures)
    validate_document_metadata(markdown_files, failures)
    validate_principles_and_operator_contract(failures)
    validate_markdown_links(markdown_files, failures)
    validate_runtime_boundaries(files, failures)
    shell_count = validate_shells(files, failures)
    credential_count = validate_credentials(files, failures)

    if failures:
        print("Governance checks failed:", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        "Governance checks passed: "
        f"{len(markdown_files)} Markdown files, "
        f"{shell_count} shell scripts, "
        f"{credential_count} text files scanned."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

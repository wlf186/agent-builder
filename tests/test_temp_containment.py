"""Regression tests for repository-local temporary-file containment."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICE_ROOTS = (
    PROJECT_ROOT / "skills" / "builtin" / "AB-docx" / "scripts" / "office",
    PROJECT_ROOT / "skills" / "builtin" / "AB-xlsx" / "scripts" / "office",
)
WORD_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)


def _load_secure_temp(office_root: Path):
    label = office_root.parents[1].name.replace("-", "_")
    module_name = f"_agent_builder_{label}_secure_temp"
    spec = importlib.util.spec_from_file_location(
        module_name, office_root / "secure_temp.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load secure_temp.py from {office_root}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _subprocess_environment(workspace: Path) -> tuple[dict[str, str], Path]:
    secure_root = workspace / ".tmp"
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(secure_root),
            "TEMP": str(secure_root),
            "TMP": str(secure_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment, secure_root


def _run(
    arguments: list[str], workspace: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_clean_secure_root(secure_root: Path) -> None:
    assert secure_root.is_dir()
    assert not secure_root.is_symlink()
    assert stat.S_IMODE(secure_root.stat().st_mode) == 0o700
    assert list(secure_root.iterdir()) == []


def test_pytest_configures_tempfile_before_test_execution() -> None:
    expected = (PROJECT_ROOT / ".runtime" / "tests" / "tmp").resolve()
    assert Path(tempfile.tempdir).resolve() == expected
    assert Path(tempfile.gettempdir()).resolve() == expected
    assert all(
        Path(os.environ[name]).resolve() == expected
        for name in ("TMPDIR", "TEMP", "TMP")
    )
    assert expected.is_dir()
    assert not expected.is_symlink()
    assert stat.S_IMODE(expected.stat().st_mode) == 0o700
    expected.relative_to(PROJECT_ROOT.resolve())


@pytest.mark.parametrize("office_root", OFFICE_ROOTS)
def test_secure_temp_prefers_tmpdir_and_enforces_mode(
    office_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_secure_temp(office_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configured = workspace / "configured-temp"

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("TMPDIR", str(configured))

    assert helper.secure_temp_root() == configured.resolve()
    assert not configured.is_symlink()
    assert stat.S_IMODE(configured.stat().st_mode) == 0o700

    configured.chmod(0o755)
    assert helper.secure_temp_root() == configured.resolve()
    assert stat.S_IMODE(configured.stat().st_mode) == 0o700


@pytest.mark.parametrize("office_root", OFFICE_ROOTS)
def test_secure_temp_falls_back_only_to_workspace_dot_tmp(
    office_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_secure_temp(office_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.chdir(workspace)
    monkeypatch.delenv("TMPDIR", raising=False)

    assert helper.secure_temp_root() == (workspace / ".tmp").resolve()
    assert stat.S_IMODE((workspace / ".tmp").stat().st_mode) == 0o700


@pytest.mark.parametrize("office_root", OFFICE_ROOTS)
def test_secure_temp_rejects_external_empty_and_symlink_roots(
    office_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_secure_temp(office_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external-temp"

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("TMPDIR", str(external))
    with pytest.raises(helper.UnsafeTempRootError):
        helper.secure_temp_root()
    assert not external.exists()

    monkeypatch.setenv("TMPDIR", "")
    with pytest.raises(helper.UnsafeTempRootError):
        helper.secure_temp_root()

    real_root = workspace / "real-temp"
    real_root.mkdir()
    linked_root = workspace / "linked-temp"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(linked_root))
    with pytest.raises(helper.UnsafeTempRootError):
        helper.secure_temp_root()


def test_office_tempfile_calls_always_specify_a_directory() -> None:
    offenders: list[str] = []
    temporary_calls = {"mkdtemp", "TemporaryDirectory"}

    for office_root in OFFICE_ROOTS:
        for source_path in office_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), source_path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "tempfile"
                    and function.attr in temporary_calls
                ):
                    continue
                if not any(keyword.arg == "dir" for keyword in node.keywords):
                    offenders.append(f"{source_path}:{node.lineno}")

    assert offenders == []


@pytest.mark.parametrize("office_root", OFFICE_ROOTS)
def test_pack_and_validate_use_and_clean_workspace_temp(
    office_root: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / office_root.parents[1].name
    workspace.mkdir()
    input_directory = workspace / "input"
    input_directory.mkdir()
    (input_directory / "[Content_Types].xml").write_text(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        encoding="utf-8",
    )
    output_file = workspace / "packed.xlsx"
    environment, secure_root = _subprocess_environment(workspace)

    packed = _run(
        [
            sys.executable,
            str(office_root / "pack.py"),
            str(input_directory),
            str(output_file),
            "--validate",
            "false",
        ],
        workspace,
        environment,
    )
    assert packed.returncode == 0, packed.stderr
    assert output_file.is_file()
    _assert_clean_secure_root(secure_root)

    validated = _run(
        [sys.executable, str(office_root / "validate.py"), str(output_file)],
        workspace,
        environment,
    )
    assert validated.returncode == 1
    assert "Validation not supported for file type .xlsx" in validated.stdout
    _assert_clean_secure_root(secure_root)


@pytest.mark.parametrize("office_root", OFFICE_ROOTS)
def test_validators_use_and_clean_workspace_temp(
    office_root: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / office_root.parents[1].name
    workspace.mkdir()
    unpacked = workspace / "unpacked"
    (unpacked / "word").mkdir(parents=True)
    original_xml = (
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body><w:p>'
        "<w:r><w:t>base</w:t></w:r></w:p></w:body></w:document>"
    )
    modified_xml = (
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body><w:p>'
        '<w:r><w:t>base</w:t></w:r><w:ins w:author="Claude">'
        "<w:r><w:t>new</w:t></w:r></w:ins></w:p></w:body></w:document>"
    )
    (unpacked / "word" / "document.xml").write_text(
        modified_xml, encoding="utf-8"
    )
    original = workspace / "original.docx"
    with zipfile.ZipFile(original, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", original_xml)

    environment, secure_root = _subprocess_environment(workspace)
    validator_probe = """
import sys
sys.path.insert(0, sys.argv[1])
from validators.docx import DOCXSchemaValidator
from validators.redlining import RedliningValidator

unpacked, original = sys.argv[2], sys.argv[3]
validator = DOCXSchemaValidator(unpacked, original)
assert validator.count_paragraphs_in_original() == 1
validator._validate_single_file_xsd = lambda *args: (False, {"sentinel"})
assert validator._get_original_file_errors(validator.xml_files[0]) == {"sentinel"}
assert RedliningValidator(unpacked, original, author="Claude").validate()
"""
    result = _run(
        [
            sys.executable,
            "-c",
            validator_probe,
            str(office_root),
            str(unpacked),
            str(original),
        ],
        workspace,
        environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_clean_secure_root(secure_root)

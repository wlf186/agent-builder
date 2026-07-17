"""Security regression tests for the built-in DOCX/XLSX archive helpers."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOTS = {
    "docx": PROJECT_ROOT / "skills" / "builtin" / "AB-docx",
    "xlsx": PROJECT_ROOT / "skills" / "builtin" / "AB-xlsx",
}


def _load_helper(label: str, root: Path):
    module_name = f"_agent_builder_{label}_safe_zip"
    path = root / "scripts" / "office" / "safe_zip.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_zip(path: Path, members, *, compression=zipfile.ZIP_STORED) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, content in members:
                if isinstance(name, zipfile.ZipInfo):
                    archive.writestr(name, content)
                else:
                    archive.writestr(name, content)


def _set_encrypted_flag(path: Path) -> None:
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = data.find(signature)
        if position < 0:
            raise AssertionError(f"Missing ZIP header {signature!r}")
        flags = struct.unpack_from("<H", data, position + offset)[0]
        struct.pack_into("<H", data, position + offset, flags | 0x1)
    path.write_bytes(data)


def _increase_declared_size(path: Path) -> None:
    data = bytearray(path.read_bytes())
    position = data.rfind(b"PK\x01\x02")
    if position < 0:
        raise AssertionError("Missing ZIP central directory")
    size = struct.unpack_from("<I", data, position + 24)[0]
    struct.pack_into("<I", data, position + 24, size + 1)
    path.write_bytes(data)


class SafeZipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = {
            label: _load_helper(label, root) for label, root in SKILL_ROOTS.items()
        }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_rejected(self, archive: Path) -> None:
        for label, helper in self.helpers.items():
            destination = self.root / f"out-{label}"
            with self.subTest(skill=label), self.assertRaises(helper.SafeZipError):
                helper.safe_extract_zip(archive, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(self.root.glob(".safe-zip-*")), [])

    def test_helpers_are_identical_and_limits_are_exact(self) -> None:
        helper_sources = [
            (root / "scripts" / "office" / "safe_zip.py").read_bytes()
            for root in SKILL_ROOTS.values()
        ]
        self.assertEqual(helper_sources[0], helper_sources[1])
        for helper in self.helpers.values():
            self.assertEqual(helper.MAX_MEMBERS, 2048)
            self.assertEqual(helper.MAX_TOTAL_UNCOMPRESSED, 50 * 1024 * 1024)
            self.assertEqual(helper.MAX_MEMBER_UNCOMPRESSED, 20 * 1024 * 1024)
            self.assertEqual(helper.MAX_COMPRESSION_RATIO, 100)

    def test_safe_archive_extracts_and_reads_with_restrictive_modes(self) -> None:
        archive = self.root / "safe.docx"
        _write_zip(
            archive,
            [
                ("word/", b""),
                ("word/document.xml", b"<document>safe</document>"),
                ("[Content_Types].xml", b"<Types/>")
            ],
            compression=zipfile.ZIP_DEFLATED,
        )
        for label, helper in self.helpers.items():
            destination = self.root / f"safe-{label}"
            result = helper.safe_extract_zip(archive, destination)
            self.assertEqual(result, destination)
            self.assertEqual(
                (destination / "word" / "document.xml").read_bytes(),
                b"<document>safe</document>",
            )
            self.assertEqual(
                helper.safe_read_zip_member(archive, "word/document.xml"),
                b"<document>safe</document>",
            )
            if sys.platform != "win32":
                self.assertEqual(
                    stat.S_IMODE((destination / "word" / "document.xml").stat().st_mode),
                    0o600,
                )
                self.assertEqual(
                    stat.S_IMODE((destination / "word").stat().st_mode), 0o700
                )

    def test_unsafe_paths_duplicates_encryption_and_special_files_are_rejected(self) -> None:
        cases = []
        for index, name in enumerate(
            (
                "../escape.xml",
                "/absolute.xml",
                "C:/drive.xml",
                "word\\document.xml",
                "word/../../escape.xml",
            )
        ):
            archive = self.root / f"path-{index}.zip"
            _write_zip(archive, [(name, b"unsafe")])
            cases.append(archive)

        duplicate = self.root / "duplicate.zip"
        _write_zip(duplicate, [("word/a.xml", b"one"), ("word/a.xml", b"two")])
        cases.append(duplicate)

        case_collision = self.root / "case-collision.zip"
        _write_zip(case_collision, [("word/a.xml", b"one"), ("WORD/A.XML", b"two")])
        cases.append(case_collision)

        parent_file = self.root / "parent-file.zip"
        _write_zip(parent_file, [("word", b"file"), ("word/document.xml", b"nested")])
        cases.append(parent_file)

        encrypted = self.root / "encrypted.zip"
        _write_zip(encrypted, [("word/document.xml", b"encrypted")])
        _set_encrypted_flag(encrypted)
        cases.append(encrypted)

        for kind, mode in (("symlink", stat.S_IFLNK), ("fifo", stat.S_IFIFO)):
            special = self.root / f"{kind}.zip"
            info = zipfile.ZipInfo(f"word/{kind}")
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            _write_zip(special, [(info, b"target")])
            cases.append(special)

        for archive in cases:
            with self.subTest(archive=archive.name):
                self._assert_rejected(archive)

    def test_member_count_size_total_and_ratio_limits_are_enforced(self) -> None:
        count_archive = self.root / "count.zip"
        _write_zip(count_archive, [("one", b"1"), ("two", b"2")])
        total_archive = self.root / "total.zip"
        _write_zip(total_archive, [("one", b"1234"), ("two", b"5678")])
        member_archive = self.root / "member.zip"
        _write_zip(member_archive, [("large", b"12345")])
        ratio_archive = self.root / "ratio.zip"
        _write_zip(
            ratio_archive,
            [("compressed", b"A" * 20_000)],
            compression=zipfile.ZIP_DEFLATED,
        )

        for label, helper in self.helpers.items():
            for archive, setting, value in (
                (count_archive, "MAX_MEMBERS", 1),
                (total_archive, "MAX_TOTAL_UNCOMPRESSED", 7),
                (member_archive, "MAX_MEMBER_UNCOMPRESSED", 4),
            ):
                destination = self.root / f"limit-{label}-{setting}"
                with self.subTest(skill=label, limit=setting), patch.object(
                    helper, setting, value
                ), self.assertRaises(helper.SafeZipError):
                    helper.safe_extract_zip(archive, destination)
                self.assertFalse(destination.exists())

            destination = self.root / f"ratio-{label}"
            with self.subTest(skill=label, limit="ratio"), self.assertRaises(
                helper.SafeZipError
            ):
                helper.safe_extract_zip(ratio_archive, destination)
            self.assertFalse(destination.exists())

    def test_actual_byte_mismatch_is_atomic_and_leaves_no_partial_output(self) -> None:
        archive = self.root / "mismatch.zip"
        _write_zip(archive, [("first.xml", b"safe"), ("second.xml", b"mismatch")])
        _increase_declared_size(archive)
        self._assert_rejected(archive)

    def test_nonempty_and_symlink_destinations_are_rejected_without_modification(self) -> None:
        archive = self.root / "safe.zip"
        _write_zip(archive, [("document.xml", b"safe")])
        for label, helper in self.helpers.items():
            nonempty = self.root / f"nonempty-{label}"
            nonempty.mkdir()
            sentinel = nonempty / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(helper.SafeZipError):
                helper.safe_extract_zip(archive, nonempty)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

            target = self.root / f"target-{label}"
            target.mkdir()
            symlink = self.root / f"symlink-{label}"
            try:
                symlink.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                continue
            with self.assertRaises(helper.SafeZipError):
                helper.safe_extract_zip(archive, symlink)


class StaticOfficeSecurityTests(unittest.TestCase):
    def test_no_unsafe_extraction_or_external_office_and_diff_commands_remain(self) -> None:
        forbidden_commands = {"soffice", "libreoffice", "pandoc", "git"}
        for skill_root in SKILL_ROOTS.values():
            for path in skill_root.rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn(".extractall(", source, path)
                tree = ast.parse(source, filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        modules = (
                            [alias.name for alias in node.names]
                            if isinstance(node, ast.Import)
                            else [node.module or ""]
                        )
                        self.assertNotIn("subprocess", modules, path)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotIn(node.value.lower(), forbidden_commands, path)

    def test_redlining_uses_bounded_stdlib_diff(self) -> None:
        for skill_root in SKILL_ROOTS.values():
            path = skill_root / "scripts" / "office" / "validators" / "redlining.py"
            source = path.read_text(encoding="utf-8")
            self.assertIn("from difflib import unified_diff", source)
            self.assertIn("MAX_DIFF_INPUT_CHARS", source)
            self.assertIn("MAX_DIFF_OUTPUT_CHARS", source)

    def test_internal_exception_text_is_not_returned_by_office_helpers(self) -> None:
        exception_patterns = ("str(e)", "str(exc)", "{e}", "{exc}")
        relative_files = (
            "scripts/office/safe_zip.py",
            "scripts/office/unpack.py",
            "scripts/office/validate.py",
            "scripts/office/pack.py",
            "scripts/office/helpers/merge_runs.py",
            "scripts/office/helpers/simplify_redlines.py",
            "scripts/office/validators/base.py",
            "scripts/office/validators/docx.py",
            "scripts/office/validators/pptx.py",
            "scripts/office/validators/redlining.py",
        )
        for skill_root in SKILL_ROOTS.values():
            for relative_file in relative_files:
                path = skill_root / relative_file
                source = path.read_text(encoding="utf-8")
                for pattern in exception_patterns:
                    self.assertNotIn(pattern, source, path)

    def test_skill_entrypoints_do_not_publish_absolute_runtime_paths(self) -> None:
        for skill in ("AB-docx", "AB-pdf"):
            path = PROJECT_ROOT / "skills" / "builtin" / skill / "scripts" / "main.py"
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(".absolute()", source, path)


if __name__ == "__main__":
    unittest.main()

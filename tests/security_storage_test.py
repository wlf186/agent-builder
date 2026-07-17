"""Offline tests for ZIP and uploaded-file containment boundaries."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from src.file_storage_manager import FileStorageError, FileStorageManager
    from src.skill_registry import SkillRegistry
    from src.models import FileInfo
except ModuleNotFoundError as exc:  # Keep the stdlib-only pre-bootstrap suite runnable.
    if exc.name != "pydantic":
        raise
    FileStorageManager = None


@unittest.skipIf(FileStorageManager is None, "project dependencies have not been bootstrapped")
class SkillArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = SkillRegistry(self.root / "data", self.root / "skills")

    def tearDown(self):
        self.temporary.cleanup()

    def _archive(self, members, compression=zipfile.ZIP_STORED):
        path = self.root / "skill.zip"
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, content in members:
                archive.writestr(name, content)
        return path

    def test_safe_archive_registers_and_preview_is_contained(self):
        archive = self._archive(
            [
                (
                    "sample/SKILL.md",
                    "---\nname: sample\ndescription: safe test\n---\n# sample\n",
                ),
                ("sample/scripts/main.py", "print('ok')\n"),
            ]
        )
        success, _, config = self.registry.extract_zip_and_register(archive)
        self.assertTrue(success)
        self.assertEqual(config.name, "sample")
        self.assertIn("print", self.registry.get_skill_file_content("sample", "scripts/main.py"))
        self.assertIsNone(self.registry.get_skill_file_content("sample", "../../outside.txt"))

    def test_traversal_and_decompression_bombs_are_rejected(self):
        traversal = self._archive(
            [("../SKILL.md", "# bad"), ("safe/SKILL.md", "# safe")]
        )
        self.assertFalse(self.registry.extract_zip_and_register(traversal)[0])

        bomb = self._archive(
            [
                ("bomb/SKILL.md", "---\nname: bomb\n---\n# bomb\n"),
                ("bomb/payload.bin", b"0" * (2 * 1024 * 1024)),
            ],
            compression=zipfile.ZIP_DEFLATED,
        )
        self.assertFalse(self.registry.extract_zip_and_register(bomb)[0])

    def test_symlink_member_is_rejected(self):
        path = self.root / "symlink.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("sample/SKILL.md", "---\nname: sample\n---\n# sample\n")
            link = zipfile.ZipInfo("sample/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../../outside")
        self.assertFalse(self.registry.extract_zip_and_register(path)[0])


class _Upload:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


@unittest.skipIf(FileStorageManager is None, "project dependencies have not been bootstrapped")
class FileStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = FileStorageManager(self.root / "files")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_streaming_upload_sanitises_filename(self):
        info = await self.manager.upload_stream(
            "agent",
            _Upload([b"abc", b"def"]),
            "../../report.txt",
            "text/plain",
        )
        self.assertEqual(info.filename, "report.txt")
        self.assertEqual((await self.manager.get_file_content("agent", info.file_id)), b"abcdef")

    async def test_streaming_limit_removes_partial_file(self):
        self.manager.MAX_FILE_SIZE = 4
        with self.assertRaises(FileStorageError):
            await self.manager.upload_stream(
                "agent", _Upload([b"abc", b"def"]), "large.bin"
            )
        leftovers = [
            path for path in self.manager.get_agent_storage_path("agent").iterdir()
        ]
        self.assertEqual(leftovers, [])

    async def test_legacy_metadata_cannot_escape_storage_or_workdir(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        metadata = self.manager.get_metadata_path("agent")
        metadata.write_text(
            json.dumps(
                {
                    "files": [
                        FileInfo(
                            file_id="bad00001",
                            agent_name="agent",
                            filename="../../escape.txt",
                            file_size=6,
                            mime_type="text/plain",
                            checksum="unused",
                            file_path="../../outside.txt",
                        ).model_dump()
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(await self.manager.get_file_path("agent", "bad00001"))
        self.assertIsNone(
            await self.manager.copy_file_to_workdir(
                "agent", "bad00001", self.root / "workdir"
            )
        )


if __name__ == "__main__":
    unittest.main()

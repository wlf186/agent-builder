import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.document_processor import DocumentProcessor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_TEST_DIR = PROJECT_ROOT / ".runtime" / "tests"


class DocumentLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME_TEST_DIR.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUNTIME_TEST_DIR)
        self.root = Path(self.temporary.name)
        self.processor = DocumentProcessor()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_docx_compression_bomb_is_rejected_before_parser(self) -> None:
        archive_path = self.root / "bomb.docx"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"0" * (2 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, "压缩比"):
            self.processor._parse_docx(archive_path)

    def test_pdf_page_limit_is_checked_before_iteration(self) -> None:
        class FakePdf:
            def __len__(self):
                return DocumentProcessor.MAX_PDF_PAGES + 1

            def close(self):
                return None

        module = types.SimpleNamespace(PdfDocument=lambda _path: FakePdf())
        with patch.dict(sys.modules, {"pypdfium2": module}):
            with self.assertRaisesRegex(ValueError, "页数超过限制"):
                self.processor._parse_pdf(self.root / "large.pdf")

    def test_chunk_count_is_bounded(self) -> None:
        class FakeSplitter:
            def split_text(self, _text):
                return ["x"] * (DocumentProcessor.MAX_CHUNKS + 1)

        self.processor._splitter = FakeSplitter()
        with self.assertRaisesRegex(ValueError, "分块数超过限制"):
            self.processor.chunk("content", "doc")


if __name__ == "__main__":
    unittest.main()

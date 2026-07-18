"""Test-only import setup for the uninstalled greenfield package."""

from __future__ import annotations

import sys
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = HARNESS_ROOT / "src"

sys.path.insert(0, str(SOURCE_ROOT))

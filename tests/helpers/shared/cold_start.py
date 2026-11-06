from __future__ import annotations
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _empty_config(tmp_path: Path) -> Path:
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    return tmp_path / "config/runtime.json"

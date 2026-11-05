#!/usr/bin/env python3
"""Repository tools: select exactly one business/module/action."""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if __name__ == "__main__":
    from scripts.core.cli import Main
    raise SystemExit(Main())

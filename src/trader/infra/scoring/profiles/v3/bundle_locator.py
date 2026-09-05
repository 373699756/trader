"""Locate the configured V3 training bundle without parsing it."""

from __future__ import annotations

from pathlib import Path


def locate_latest_v3_bundle(training_root: Path) -> Path:
    model_root = training_root / "tomorrow-v3"
    candidates = tuple(path for path in model_root.glob("*/model.json") if path.is_file())
    if not candidates:
        raise FileNotFoundError(model_root / "*/model.json")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))


__all__ = ["locate_latest_v3_bundle"]

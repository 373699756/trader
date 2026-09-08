"""Locate the configured V3 training bundle without parsing it."""

from __future__ import annotations

from pathlib import Path


def locate_latest_bundle(training_root: Path) -> Path:
    model = training_root / "tomorrow-v3" / "model.json"
    if not model.is_file():
        raise FileNotFoundError(model)
    return model


__all__ = ["locate_latest_bundle"]

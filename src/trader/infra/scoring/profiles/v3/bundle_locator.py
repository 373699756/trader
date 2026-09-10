"""Locate the configured V3 training bundle without parsing it."""

from __future__ import annotations

from pathlib import Path


def locate_latest_bundle(training_root: Path) -> Path:
    from trader.infra.scoring.profiles.v3.bundle_store import locate_active_tomorrow_bundle

    return locate_active_tomorrow_bundle(training_root / "tomorrow-v3")


__all__ = ["locate_latest_bundle"]

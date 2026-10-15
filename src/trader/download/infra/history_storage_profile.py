"""Read-only storage classification for history write scheduling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HistoryStorageKind = Literal["hdd", "ssd", "unknown"]
_SYSFS_BLOCK_ROOT = Path("/sys/dev/block")


@dataclass(frozen=True)
class HistoryWriteTuning:
    flush_codes: int
    flush_cells: int
    max_codes: int = 64
    max_cells: int = 32768

    def __post_init__(self) -> None:
        if not 1 <= self.flush_codes <= self.max_codes <= 64 or not 1 <= self.flush_cells <= self.max_cells <= 32768:
            raise ValueError("history write tuning exceeds bounded capacity")


def ClassifyHistoryStorage(root: Path) -> HistoryStorageKind:
    """Classify the device containing root, including a not-yet-created root."""
    try:
        target = root.resolve()
        while not target.exists():
            if target == target.parent:
                return "unknown"
            target = target.parent
        device = target.stat().st_dev
        node = (_SYSFS_BLOCK_ROOT / f"{os.major(device)}:{os.minor(device)}").resolve(strict=True)
        for candidate in (node, *node.parents):
            rotational = candidate / "queue/rotational"
            if rotational.is_file():
                value = rotational.read_text(encoding="ascii").strip()
                return "hdd" if value == "1" else "ssd" if value == "0" else "unknown"
    except (OSError, RuntimeError, ValueError):
        return "unknown"
    return "unknown"


def ChooseHistoryWriteTuning(kind: HistoryStorageKind, *, fresh: bool) -> HistoryWriteTuning:
    if kind == "hdd" and fresh:
        return HistoryWriteTuning(flush_codes=8, flush_cells=16384)
    return HistoryWriteTuning(flush_codes=16, flush_cells=8192)


__all__ = ["HistoryStorageKind", "HistoryWriteTuning", "ClassifyHistoryStorage", "ChooseHistoryWriteTuning"]

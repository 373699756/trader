"""Bounded history write batches selected from shared storage evidence."""

from __future__ import annotations

from dataclasses import dataclass

from trader.infra.storage_types import StorageKind


@dataclass(frozen=True)
class HistoryWriteTuning:
    flush_codes: int
    flush_cells: int
    max_codes: int = 64
    max_cells: int = 32768

    def __post_init__(self) -> None:
        if not 1 <= self.flush_codes <= self.max_codes <= 64 or not 1 <= self.flush_cells <= self.max_cells <= 32768:
            raise ValueError("history write tuning exceeds bounded capacity")


def ChooseHistoryWriteTuning(kind: StorageKind, *, fresh: bool) -> HistoryWriteTuning:
    if kind in {"hdd", "unknown"} and fresh:
        return HistoryWriteTuning(flush_codes=8, flush_cells=16384)
    return HistoryWriteTuning(flush_codes=16, flush_cells=8192)


__all__ = ["HistoryWriteTuning", "ChooseHistoryWriteTuning"]

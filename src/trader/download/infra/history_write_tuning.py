"""Bounded history write batches selected from shared storage evidence."""

from __future__ import annotations

from dataclasses import dataclass

from trader.download.domain.history_sync import HistoryWriteBatchPolicy
from trader.infra.storage_types import StorageKind


@dataclass(frozen=True)
class HistoryWriteTuning:
    flush_codes: int
    flush_cells: int
    max_codes: int
    max_cells: int

    def __post_init__(self) -> None:
        if not 1 <= self.flush_codes <= self.max_codes <= 64 or not 1 <= self.flush_cells <= self.max_cells <= 32768:
            raise ValueError("history write tuning exceeds bounded capacity")


def ChooseHistoryWriteTuning(
    kind: StorageKind,
    policy: HistoryWriteBatchPolicy,
    *,
    fresh: bool,
) -> HistoryWriteTuning:
    effective = "hdd" if kind == "unknown" else kind
    if effective == "hdd":
        codes = policy.hdd_fresh_codes if fresh else policy.hdd_incremental_codes
        cells = policy.hdd_fresh_cells if fresh else policy.hdd_incremental_cells
    else:
        codes = policy.ssd_fresh_codes if fresh else policy.ssd_incremental_codes
        cells = policy.ssd_fresh_cells if fresh else policy.ssd_incremental_cells
    return HistoryWriteTuning(codes, cells, policy.maximum_codes, policy.maximum_cells)


__all__ = ["HistoryWriteTuning", "ChooseHistoryWriteTuning"]

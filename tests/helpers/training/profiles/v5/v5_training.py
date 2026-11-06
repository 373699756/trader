"""Actual V5 CPU fitting and publication on isolated synthetic history."""

from __future__ import annotations
import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from trader.download.domain.baostock_daily import BAOSTOCK_RESEARCH_IDENTITY, BaoStockCalendar
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.training.domain.tomorrow_training_input import REQUIRED_DAILY_FIELDS, FrozenDailyInputDescriptor
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot

ROOT = Path(__file__).resolve().parents[5]


def _point(day: date, position: int, stock: int) -> HistoryTrainingPoint:
    close = 20 * math.exp(position * 0.0001 + 0.04 * math.sin(position / (5 + stock)))
    return HistoryTrainingPoint(
        day, close, 1e8 + position * 2000 + stock * 100, close * 0.998, close * 1.01, close * 0.99, 1e6 + position * 30
    )


def _history(root: Path) -> SimpleNamespace:
    root.mkdir()
    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    codes = tuple(f"60000{i}" for i in range(6))
    snapshot = HistoryTrainingInputSnapshot(
        "complete_manifest",
        "a" * 64,
        "b" * 64,
        BaoStockCalendar(dates).content_hash,
        dates[-1],
        dates[-2],
        BaoStockCalendar(dates),
        codes,
        "d" * 64,
    )
    descriptor = FrozenDailyInputDescriptor(
        manifest_hash=snapshot.snapshot_hash,
        source_identity=BAOSTOCK_RESEARCH_IDENTITY,
        source_cutoff=dates[-1],
        requested_sessions=2000,
        primary_key=("code", "trade_date"),
        fields=REQUIRED_DAILY_FIELDS,
        raw_qfq_layout="same_row",
        row_hash_algorithm="sha256",
        frozen=True,
    )

    def rows(allowed, progress=None):
        for position, day in enumerate(dates):
            if day in allowed:
                for stock, code in enumerate(codes):
                    yield HistoryTrainingRow(code, day, "main", "bank", _point(day, position, stock), "complete")

    return SimpleNamespace(
        snapshot=snapshot,
        history_root=root,
        published_snapshot=SimpleNamespace(partitions=(object(), object())),
        describe_frozen_daily_input=lambda: descriptor,
        training_row_upper_bound=lambda allowed: len(allowed) * len(codes),
        has_industry_window=lambda *_args, **_kwargs: True,
        industry_sample_upper_bounds=lambda *_args: (("bank", 100000),),
        iter_training_rows=rows,
    )

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date

import pytest

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import (
    HistoryRevision,
    HistoryTrainingPoint,
    HistoryTrainingRow,
)


def _side(day: date, adjustment: str) -> BaoStockDailySide:
    return BaoStockDailySide(
        "600001",
        day,
        adjustment,  # type: ignore[arg-type]
        10.0,
        10.5,
        9.5,
        10.2,
        100.0,
        1000.0,
        10.0 if adjustment == "unadjusted" else None,
        0.02 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        "trading",
    )


def _revision(day: date, sequence: int = 1) -> HistoryRevision:
    raw = _side(day, "unadjusted")
    qfq = _side(day, "qfq")
    return HistoryRevision(
        sequence,
        "main",
        BaoStockDailyCell("600001", day, "complete", raw, qfq),
        False,
        "bank",
        "sw",
    )


def test_monthly_revision_has_content_addressed_identity_and_typed_training_row() -> None:
    revision = _revision(date(2026, 9, 10))
    replay = replace(revision, first_seen_sequence=2)

    assert replay.revision_id == revision.revision_id
    assert replay.content_hash != revision.content_hash
    assert revision.training_row is not None
    assert revision.training_row.industry == "bank"
    with pytest.raises(FrozenInstanceError):
        revision.board = "star"  # type: ignore[misc]
    with pytest.raises(ValueError, match="industry"):
        replace(revision, industry_classification=None)


def test_training_row_requires_matching_typed_point() -> None:
    day = date(2026, 9, 10)
    point = HistoryTrainingPoint(day, 10.2, 1000.0)
    row = HistoryTrainingRow("600001", day, "main", " bank ", point)

    assert row.industry == "bank"
    assert row.point == point
    with pytest.raises(ValueError, match="invalid"):
        HistoryTrainingRow("600001", date(2026, 9, 11), "main", "bank", point)

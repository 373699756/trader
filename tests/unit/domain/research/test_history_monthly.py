from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest

from trader.domain.research.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.domain.research.history_monthly import HistoryMonthlyRevision, HistoryTrainingWindow


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


def _revision(day: date, sequence: int = 1) -> HistoryMonthlyRevision:
    raw = _side(day, "unadjusted")
    qfq = _side(day, "qfq")
    return HistoryMonthlyRevision(
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


def test_training_window_requires_exact_consecutive_code_rows() -> None:
    rows = tuple(_revision(date(2026, 1, 1) + timedelta(days=offset)).training_row for offset in range(61))
    complete_rows = tuple(row for row in rows if row is not None)
    window = HistoryTrainingWindow(complete_rows)

    assert len(window.rows) == 61
    assert window.code == "600001"
    assert window.trade_date == complete_rows[-1].trade_date
    with pytest.raises(ValueError, match="61"):
        HistoryTrainingWindow(complete_rows[:-1])

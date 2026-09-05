from datetime import date, datetime

import pytest

from trader.domain.research.h1_point_in_time import (
    H1CapabilityProbe,
    H1PointInTimeRecord,
    H1PointInTimeSpec,
)
from trader.domain.research.historical_screening import HistoricalPriceBar


def _record(strategy: str = "today", observed_at: str = "2026-08-31T11:20:00+08:00") -> H1PointInTimeRecord:
    bar = HistoricalPriceBar(date(2026, 8, 31), 10, 10.2, 10.3, 9.9, 100, 1000, 2, None, "qfq", "fixture")
    digest = "a" * 64
    return H1PointInTimeRecord(
        strategy,
        "600001",
        date(2026, 8, 31),
        datetime.fromisoformat(observed_at),
        bar,
        10.1,
        50,
        500,
        digest,
        digest,
        digest,
    )


def test_h1_spec_is_fixed_and_strategy_anchor_is_explicit() -> None:
    today = H1PointInTimeSpec("today")
    tomorrow = H1PointInTimeSpec("tomorrow")
    assert today.research_identity == "score_h1_point_in_time"
    assert today.anchor_kind == "today_1120"
    assert today.anchor_time.isoformat() == "11:20:00"
    assert tomorrow.anchor_kind == "tomorrow_1450"
    assert tomorrow.anchor_time.isoformat() == "14:50:00"
    assert today.promotion_authority is False
    assert len(today.content_hash) == 64


def test_h1_record_rejects_future_or_late_or_naive_observations() -> None:
    with pytest.raises(ValueError, match="exact"):
        _record(observed_at="2026-08-31T11:21:00+08:00")
    with pytest.raises(ValueError, match="exact"):
        _record(observed_at="2026-08-31T11:19:00+08:00")
    with pytest.raises(ValueError, match="timezone"):
        _record(observed_at="2026-08-31T11:20:00")


def test_capability_probe_requires_qfq_and_point_in_time_evidence() -> None:
    probe = H1CapabilityProbe("fixture", date(2020, 1, 1), True, True, "qfq", True, 500, 3, 1000, 1.5)
    assert probe.point_in_time_anchors_proven
    assert len(probe.content_hash) == 64
    with pytest.raises(ValueError):
        H1CapabilityProbe("fixture", None, True, True, "raw", True, 500, 3, 1000, 1.5)

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.point_in_time_dataset import (
    PointInTimeDateSplit,
    PointInTimeEventFact,
)


def _split() -> PointInTimeDateSplit:
    start = date(2024, 1, 1)
    return PointInTimeDateSplit(
        training_dates=(start,),
        early_stopping_dates=(start + timedelta(days=1),),
        calibration_dates=(start + timedelta(days=2),),
        development_confirmation_embargo_dates=tuple(start + timedelta(days=3 + index) for index in range(5)),
        confirmation_dates=(start + timedelta(days=8),),
        confirmation_holdout_embargo_dates=tuple(start + timedelta(days=9 + index) for index in range(5)),
        terminal_holdout_dates=tuple(start + timedelta(days=14 + index) for index in range(200)),
    )


def test_point_in_time_date_split_is_ordered_disjoint_and_keeps_holdout_closed() -> None:
    split = _split()

    assert len(split.terminal_holdout_dates) == 200
    assert split.development_dates == (
        split.training_dates + split.early_stopping_dates + split.calibration_dates + split.confirmation_dates
    )
    assert split.terminal_holdout_opened is False
    assert split.production_authority is False
    assert len(split.date_set_hash) == 64
    assert len(split.content_hash) == 64


def test_point_in_time_date_split_rejects_overlap_and_short_holdout() -> None:
    split = _split()

    with pytest.raises(ValueError, match="ordered and disjoint"):
        replace(split, calibration_dates=split.training_dates)
    with pytest.raises(ValueError, match="at least 200"):
        replace(split, terminal_holdout_dates=split.terminal_holdout_dates[:-1])


def test_event_fact_rejects_information_not_visible_at_anchor() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    anchor = datetime(2024, 1, 1, 14, 50, tzinfo=shanghai)

    with pytest.raises(ValueError, match="visible"):
        PointInTimeEventFact(
            fact_id="announcement",
            published_at=anchor + timedelta(seconds=1),
            effective_at=anchor,
            anchor_at=anchor,
            content_hash="a" * 64,
        )

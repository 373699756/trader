from datetime import date, timedelta

import pytest

from trader.application.research.tomorrow_v3_training import TomorrowV3TrainingWindow
from trader.domain.research.baostock_daily import build_baostock_v3_split


def test_training_window_never_authorizes_the_latest_two_hundred_dates() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    split = build_baostock_v3_split(dates, parent_manifest_hash="a" * 64)
    window = TomorrowV3TrainingWindow(split)

    assert window.readable_dates.isdisjoint(split.point_in_time_holdout_dates)
    assert split.daily_proxy_holdout_dates[-1] in window.readable_dates
    with pytest.raises(ValueError, match="point-in-time holdout"):
        window.require_readable((split.point_in_time_holdout_dates[0],))


def test_training_window_rejects_dates_outside_the_frozen_manifest() -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    window = TomorrowV3TrainingWindow(build_baostock_v3_split(dates, parent_manifest_hash="a" * 64))

    with pytest.raises(ValueError, match="outside the frozen split"):
        window.require_readable((date(2020, 1, 1),))

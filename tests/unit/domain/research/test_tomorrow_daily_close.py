from __future__ import annotations

from datetime import date, timedelta

import pytest

from trader.domain.research.tomorrow_daily_close import (
    build_expanding_walk_forward,
    split_complete_trading_dates,
)


def _dates(count: int) -> tuple[date, ...]:
    start = date(2020, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def test_complete_day_split_first_reserves_latest_point_in_time_holdout_then_applies_sixty_twenty_twenty() -> None:
    trading_dates = _dates(1_000)

    split = split_complete_trading_dates(trading_dates)

    assert len(split.development_dates) == 475
    assert len(split.first_embargo_dates) == 5
    assert len(split.confirmation_dates) == 155
    assert len(split.second_embargo_dates) == 5
    assert len(split.terminal_holdout_dates) == 160
    assert len(split.point_in_time_reserved_dates) == 200
    assert split.all_dates == trading_dates
    assert split.daily_close_dates == trading_dates[:800]
    assert split.terminal_holdout_dates == trading_dates[640:800]
    assert split.point_in_time_reserved_dates == trading_dates[800:]


def test_complete_day_split_rejects_duplicates_order_errors_and_too_few_dates() -> None:
    trading_dates = _dates(240)

    with pytest.raises(ValueError, match="strictly increasing"):
        split_complete_trading_dates((trading_dates[1], trading_dates[0], *trading_dates[2:]))
    with pytest.raises(ValueError, match="strictly increasing"):
        split_complete_trading_dates((*trading_dates, trading_dates[-1]))
    with pytest.raises(ValueError, match="at least"):
        split_complete_trading_dates(_dates(229))
    with pytest.raises(ValueError, match="reserve at least 200"):
        split_complete_trading_dates(_dates(240), point_in_time_reserve_days=199)


def test_expanding_walk_forward_has_five_ordered_non_overlapping_validation_folds() -> None:
    split = split_complete_trading_dates(_dates(320))

    folds = build_expanding_walk_forward(split.development_dates)

    assert len(folds) == 5
    assert tuple(fold.index for fold in folds) == (1, 2, 3, 4, 5)
    assert all(max(fold.training_dates) < min(fold.validation_dates) for fold in folds)
    assert all(folds[index].training_dates < folds[index + 1].training_dates for index in range(4))
    validation_dates = tuple(day for fold in folds for day in fold.validation_dates)
    assert len(validation_dates) == len(set(validation_dates))
    assert validation_dates == split.development_dates[len(folds[0].training_dates) :]

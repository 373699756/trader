from __future__ import annotations

import pytest

from trader.domain.filters import board_for_code, hard_filter
from trader.domain.models import Board


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600001", Board.MAIN),
        ("000001", Board.MAIN),
        ("300001", Board.CHINEXT),
        ("688001", Board.STAR),
        ("830001", Board.UNSUPPORTED),
        ("invalid", Board.UNSUPPORTED),
    ],
)
def test_board_for_code(code, expected) -> None:
    assert board_for_code(code) is expected


@pytest.mark.parametrize(
    ("code", "pct_change", "allowed", "reason"),
    [
        ("600001", 8.00, True, ""),
        ("600001", 8.01, False, "main_board_too_hot"),
        ("300001", 16.00, True, ""),
        ("300001", 16.01, False, "growth_board_too_hot"),
        ("688001", 16.00, True, ""),
        ("688001", 16.01, False, "growth_board_too_hot"),
    ],
)
def test_hot_price_boundaries(feature_factory, observed_at, code, pct_change, allowed, reason) -> None:
    result = hard_filter(feature_factory(code=code, pct_change=pct_change), observed_at, max_age_seconds=20)

    assert result.allowed is allowed
    if reason:
        assert reason in {item.code for item in result.reasons}


def test_filter_reports_source_threshold_and_actual(feature_factory, observed_at) -> None:
    snapshot = feature_factory(values={"amount_median_20d": 49_999_999.0})

    result = hard_filter(snapshot, observed_at, max_age_seconds=20)

    reason = next(item for item in result.reasons if item.code == "insufficient_liquidity")
    assert reason.threshold == ">= 50000000"
    assert reason.actual == "49999999.0"
    assert reason.source == "fixture"

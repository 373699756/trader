from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

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
    assert reason.actual == 49_999_999.0
    assert reason.source == "fixture"


@pytest.mark.parametrize(
    ("value", "allowed", "reason_code", "actual"),
    [
        (None, False, "missing_liquidity_history", None),
        (float("nan"), False, "invalid_liquidity_history", "nan"),
        (float("inf"), False, "invalid_liquidity_history", "inf"),
        (49_999_999.0, False, "insufficient_liquidity", 49_999_999.0),
        (50_000_000.0, True, "", 50_000_000.0),
    ],
)
def test_liquidity_history_is_required_with_exact_boundary(
    feature_factory,
    observed_at,
    value,
    allowed,
    reason_code,
    actual,
) -> None:
    result = hard_filter(
        feature_factory(values={"amount_median_20d": value}),
        observed_at,
        max_age_seconds=20,
    )

    assert result.allowed is allowed
    if reason_code:
        reason = next(item for item in result.reasons if item.filter_code == reason_code)
        assert reason.threshold == ">= 50000000"
        assert reason.actual == actual
        assert reason.source == "fixture"
        assert reason.observed_at == observed_at


def test_quote_age_boundary_and_future_quote_are_auditable(feature_factory, observed_at) -> None:
    exact = feature_factory()
    exact = replace(exact, quote=replace(exact.quote, source_time=observed_at - timedelta(seconds=20)))
    stale = replace(exact, quote=replace(exact.quote, source_time=observed_at - timedelta(seconds=20.001)))
    future = replace(exact, quote=replace(exact.quote, source_time=observed_at + timedelta(milliseconds=1)))

    assert hard_filter(exact, observed_at, max_age_seconds=20).allowed is True
    assert "stale_quote" in {item.filter_code for item in hard_filter(stale, observed_at, max_age_seconds=20).reasons}
    assert "future_quote" in {item.filter_code for item in hard_filter(future, observed_at, max_age_seconds=20).reasons}


def test_non_finite_and_structurally_invalid_quotes_are_rejected(feature_factory, observed_at) -> None:
    non_finite = feature_factory(pct_change=float("nan"))
    invalid_ohlc = feature_factory()
    invalid_ohlc = replace(invalid_ohlc, quote=replace(invalid_ohlc.quote, high=11.0, price=12.0))
    invalid_deviation = feature_factory()
    invalid_deviation = replace(
        invalid_deviation,
        quote=replace(invalid_deviation.quote, cross_source_deviation_pct=float("inf")),
    )

    assert "invalid_pct_change" in {
        item.filter_code for item in hard_filter(non_finite, observed_at, max_age_seconds=20).reasons
    }
    assert "invalid_quote_structure" in {
        item.filter_code for item in hard_filter(invalid_ohlc, observed_at, max_age_seconds=20).reasons
    }
    assert "invalid_cross_source_deviation" in {
        item.filter_code for item in hard_filter(invalid_deviation, observed_at, max_age_seconds=20).reasons
    }


@pytest.mark.parametrize(
    ("deviation", "verified", "allowed"),
    [(0.5, False, True), (0.5001, False, False), (0.5001, True, True)],
)
def test_cross_source_deviation_boundary(feature_factory, observed_at, deviation, verified, allowed) -> None:
    snapshot = feature_factory()
    snapshot = replace(
        snapshot,
        quote=replace(
            snapshot.quote,
            cross_source_deviation_pct=deviation,
            cross_source_verified=verified,
        ),
    )

    assert hard_filter(snapshot, observed_at, max_age_seconds=20).allowed is allowed

from __future__ import annotations

from dataclasses import replace

import pytest

from trader.domain.recommendation.downside import assess_downside, derive_entry_setup
from trader.domain.recommendation.models import Strategy


def test_heat_alone_does_not_trigger_downside_guard(feature_factory) -> None:
    feature = feature_factory(
        pct_change=11.0,
        values={
            "atr20_pct": 2.0,
            "short_term_overheat": 1.0,
            "intraday_reversal": 0.0,
            "trend_breakdown": 0.0,
        },
    )

    assessment = assess_downside(feature, Strategy.TOMORROW)

    assert assessment.status == "pass"
    assert assessment.reasons == ()


@pytest.mark.parametrize(
    ("values", "reason"),
    (
        ({"atr20_pct": 2.0, "close_location": 35.0}, "intraday_reversal_atr"),
        ({"atr20_pct": 2.0, "trend_breakdown": 1.0}, "trend_breakdown"),
        (
            {"atr20_pct": 2.0, "low_volatility_score": 20.0, "low_drawdown_score": 20.0},
            "low_stability_tail",
        ),
    ),
)
def test_downside_guard_observes_structural_tail_risk(feature_factory, values, reason) -> None:
    feature = feature_factory(high=12.5, price=12.0, values=values)

    assessment = assess_downside(feature, Strategy.TOMORROW)

    assert assessment.status == "observe"
    assert reason in assessment.reasons


def test_risk_off_requires_weak_market_and_weak_close(feature_factory) -> None:
    weak = replace(
        feature_factory(
            values={
                "atr20_pct": 2.0,
                "market_breadth": 30.0,
                "tail_return_30m_pct": -0.01,
                "close_location": 50.0,
            }
        ),
        market_regime="risk_off",
    )
    strong = replace(weak, values={**weak.values, "tail_return_30m_pct": 0.3, "close_location": 80.0})

    assert "risk_off_weak_close" in assess_downside(weak, Strategy.TOMORROW).reasons
    assert "risk_off_weak_close" not in assess_downside(strong, Strategy.TOMORROW).reasons


def test_missing_required_downside_inputs_fail_closed(feature_factory) -> None:
    feature = feature_factory(values={"atr20_pct": None})

    assessment = assess_downside(feature, Strategy.D25)

    assert assessment.status == "observe"
    assert assessment.reasons == ("downside_inputs_missing",)


def test_shrink_pullback_and_volume_breakout_are_deterministic(feature_factory) -> None:
    pullback = feature_factory(
        price=10.05,
        values={
            "ma5": 10.0,
            "ma10": 9.9,
            "ma20": 9.8,
            "ma20_slope_pct": 1.0,
            "volume_to_5d_average": 0.7,
            "prior_high_20d": 11.0,
            "breakout_deviation_pct": -8.64,
            "close_location": 60.0,
            "industry_breadth": 60.0,
        },
    )
    breakout = feature_factory(
        price=10.3,
        values={
            "ma5": 10.0,
            "ma10": 9.9,
            "ma20": 9.8,
            "ma20_slope_pct": 1.0,
            "volume_to_5d_average": 2.0,
            "prior_high_20d": 10.0,
            "breakout_deviation_pct": 3.0,
            "close_location": 70.0,
            "industry_breadth": 60.0,
        },
    )

    assert derive_entry_setup(pullback).setup_type == "shrink_pullback"
    assert derive_entry_setup(pullback).score == 100.0
    assert derive_entry_setup(breakout).setup_type == "volume_breakout"
    assert derive_entry_setup(breakout).score == 100.0


def test_entry_quality_remains_available_without_optional_industry_breadth(feature_factory) -> None:
    feature = feature_factory(
        price=10.3,
        values={
            "ma5": 10.0,
            "ma10": 9.9,
            "ma20": 9.8,
            "ma20_slope_pct": 1.0,
            "volume_to_5d_average": 1.0,
            "prior_high_20d": 10.5,
            "breakout_deviation_pct": -1.9,
            "close_location": 70.0,
            "industry_breadth": None,
        },
    )

    setup = derive_entry_setup(feature)

    assert setup.setup_type == "trend_unconfirmed"
    assert setup.score == 50.0

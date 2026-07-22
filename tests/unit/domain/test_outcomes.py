from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.outcome.evaluation import OutcomeEvaluationRequest, evaluate_outcome
from trader.domain.outcome.models import (
    OutcomeBar,
    OutcomeTarget,
)
from trader.domain.recommendation.models import Strategy


def _evaluate_outcome(target: OutcomeTarget, bars: tuple[OutcomeBar, ...], **kwargs):
    return evaluate_outcome(OutcomeEvaluationRequest(target=target, bars=bars, **kwargs))


def test_t1_outcome_uses_future_low_and_cost_adjusted_excess_return() -> None:
    target = OutcomeTarget("snapshot", Strategy.TOMORROW, "2026-07-20", "600001", 10.0, 2.0)
    bars = (
        OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
        OutcomeBar("2026-07-21", 10.1, 10.5, 9.6, 10.3, 3.0),
    )

    outcome = _evaluate_outcome(
        target,
        bars,
        horizon=1,
        benchmark_returns=(1.0,),
        settled_at=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert outcome.status == "complete"
    assert outcome.mae_pct == pytest.approx(-4.0)
    assert outcome.mae_atr == pytest.approx(-2.0)
    assert outcome.gross_return_pct == pytest.approx(3.0)
    assert outcome.net_excess_return_pct == pytest.approx(1.8)
    assert outcome.severe_drawdown is True


def test_d25_outcome_uses_all_lows_through_horizon() -> None:
    target = OutcomeTarget("snapshot", Strategy.D25, "2026-07-20", "600001", 10.0, 2.0)
    bars = (
        OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
        OutcomeBar("2026-07-21", 10.0, 10.2, 9.8, 10.1, 1.0),
        OutcomeBar("2026-07-22", 10.1, 10.3, 9.4, 10.2, 0.99),
        OutcomeBar("2026-07-23", 10.2, 10.5, 9.7, 10.4, 1.96),
    )

    outcome = _evaluate_outcome(
        target,
        bars,
        horizon=3,
        benchmark_returns=(0.0, 0.0, 0.0),
        settled_at=datetime(2026, 7, 23, 8, tzinfo=timezone.utc),
    )

    assert outcome.minimum_low == 9.4
    assert outcome.mae_pct == pytest.approx(-6.0)
    assert outcome.mae_atr == pytest.approx(-3.0)
    assert outcome.severe_drawdown is True


def test_outcome_rejects_future_window_gaps_and_price_discontinuity() -> None:
    target = OutcomeTarget("snapshot", Strategy.TOMORROW, "2026-07-20", "600001", 10.0, 2.0)
    bars = (
        OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
        OutcomeBar("2026-07-22", 5.0, 5.1, 4.9, 5.0, 0.0),
    )

    outcome = _evaluate_outcome(
        target,
        bars,
        horizon=1,
        benchmark_returns=(),
        settled_at=datetime(2026, 7, 22, 8, tzinfo=timezone.utc),
        expected_sessions=2,
    )

    assert outcome.status == "insufficient_data"
    assert outcome.quality_reason == "missing_or_suspended_session"


def test_outcome_requires_stock_bars_to_match_benchmark_sessions() -> None:
    target = OutcomeTarget("snapshot", Strategy.TOMORROW, "2026-07-20", "600001", 10.0, 2.0)
    bars = (
        OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
        OutcomeBar("2026-07-22", 10.0, 10.2, 9.8, 10.1, 1.0),
    )

    outcome = _evaluate_outcome(
        target,
        bars,
        horizon=1,
        benchmark_returns=(0.5,),
        expected_trade_dates=("2026-07-21",),
        settled_at=datetime(2026, 7, 22, 8, tzinfo=timezone.utc),
    )

    assert outcome.status == "insufficient_data"
    assert outcome.quality_reason == "missing_or_suspended_session"


def test_discontinuity_uses_recommendation_close_instead_of_intraday_anchor() -> None:
    target = OutcomeTarget("snapshot", Strategy.TODAY, "2026-07-20", "600001", 10.0, 2.0)
    bars = (
        OutcomeBar("2026-07-20", 10.0, 11.2, 9.9, 11.0, 10.0),
        OutcomeBar("2026-07-21", 11.0, 11.2, 10.8, 11.11, 1.0),
    )

    outcome = _evaluate_outcome(
        target,
        bars,
        horizon=1,
        benchmark_returns=(0.0,),
        expected_trade_dates=("2026-07-21",),
        settled_at=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert outcome.status == "complete"
    assert outcome.gross_return_pct == pytest.approx(11.1)

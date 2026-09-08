from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.outcome.evaluation import CanonicalOutcomeEvaluator, OutcomeEvaluationRequest, evaluate_outcome
from trader.domain.outcome.models import (
    BenchmarkConstituentReturn,
    BenchmarkReturn,
    OutcomeBar,
    OutcomeTarget,
    outcome_horizons,
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


def test_d25_contract_includes_t4_and_canonical_evaluator_is_cost_parameterized() -> None:
    target = OutcomeTarget("snapshot", Strategy.D25, "2026-07-20", "600001", 10.0, 2.0)
    bars = (
        OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
        OutcomeBar("2026-07-21", 10.0, 10.2, 9.8, 10.1, 1.0),
        OutcomeBar("2026-07-22", 10.1, 10.3, 9.9, 10.2, 0.99),
        OutcomeBar("2026-07-23", 10.2, 10.4, 10.0, 10.3, 0.98),
        OutcomeBar("2026-07-24", 10.3, 10.5, 10.1, 10.4, 0.97),
    )
    evaluator = CanonicalOutcomeEvaluator()

    results = tuple(
        evaluator.evaluate(
            OutcomeEvaluationRequest(
                target=target,
                bars=bars,
                horizon=4,
                benchmark_returns=(0.0, 0.0, 0.0, 0.0),
                settled_at=datetime(2026, 7, 24, 8, tzinfo=timezone.utc),
                round_trip_cost_pct=cost,
            )
        )
        for cost in (0.20, 0.50, 1.00)
    )

    assert outcome_horizons(Strategy.D25) == (2, 3, 4, 5)
    assert tuple(item.net_excess_return_pct for item in results) == pytest.approx((3.8, 3.5, 3.0))


def test_outcome_target_rejects_pending_horizons_outside_strategy_contract() -> None:
    with pytest.raises(ValueError, match="pending outcome horizons"):
        OutcomeTarget(
            "snapshot",
            Strategy.TOMORROW,
            "2026-07-20",
            "600001",
            10.0,
            2.0,
            pending_horizons=(4,),
        )


def test_canonical_benchmark_requires_one_complete_unique_trade_date_population() -> None:
    evaluator = CanonicalOutcomeEvaluator()
    constituents = (
        BenchmarkConstituentReturn("600001", "2026-07-21", 1.0),
        BenchmarkConstituentReturn("000001", "2026-07-21", 3.0),
    )

    assert evaluator.equal_weight_benchmark("2026-07-21", constituents) == BenchmarkReturn("2026-07-21", 2.0)
    assert evaluator.equal_weight_benchmark("2026-07-21", (*constituents, constituents[0])) is None
    assert (
        evaluator.equal_weight_benchmark(
            "2026-07-21",
            (BenchmarkConstituentReturn("600001", "2026-07-20", 1.0),),
        )
        is None
    )


def test_canonical_d25_aggregate_requires_exact_four_complete_horizons() -> None:
    evaluator = CanonicalOutcomeEvaluator()
    target = OutcomeTarget("snapshot", Strategy.D25, "2026-07-20", "600001", 10.0, 2.0)
    bars = tuple(
        OutcomeBar(f"2026-07-{20 + offset:02d}", 10.0, 10.5, 9.8, 10.0 + offset / 10, 1.0) for offset in range(6)
    )
    outcomes = tuple(
        evaluator.evaluate(
            OutcomeEvaluationRequest(
                target=target,
                bars=bars,
                horizon=horizon,
                benchmark_returns=(0.0,) * horizon,
                settled_at=datetime(2026, 7, 25, 8, tzinfo=timezone.utc),
            )
        )
        for horizon in outcome_horizons(Strategy.D25)
    )

    assert evaluator.d25_aggregate(outcomes) == pytest.approx(3.3)
    assert evaluator.d25_aggregate(outcomes[:-1]) is None


@pytest.mark.parametrize("cost", (-0.1, float("nan")))
def test_outcome_request_rejects_invalid_round_trip_cost(cost: float) -> None:
    target = OutcomeTarget("snapshot", Strategy.TOMORROW, "2026-07-20", "600001", 10.0, 2.0)

    with pytest.raises(ValueError, match="round-trip cost"):
        OutcomeEvaluationRequest(
            target=target,
            bars=(),
            horizon=1,
            benchmark_returns=(),
            settled_at=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
            round_trip_cost_pct=cost,
        )


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


@pytest.mark.parametrize("field", ("anchor_price", "atr20_pct"))
def test_outcome_rejects_non_finite_target_values(field: str) -> None:
    values = {"anchor_price": 10.0, "atr20_pct": 2.0}
    values[field] = float("nan")
    target = OutcomeTarget(
        "snapshot",
        Strategy.TOMORROW,
        "2026-07-20",
        "600001",
        values["anchor_price"],
        values["atr20_pct"],
    )
    bars = (
        OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
        OutcomeBar("2026-07-21", 10.0, 10.2, 9.8, 10.1, 1.0),
    )

    outcome = _evaluate_outcome(
        target,
        bars,
        horizon=1,
        benchmark_returns=(0.0,),
        settled_at=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert outcome.status == "insufficient_data"
    assert outcome.quality_reason == "invalid_price_window"

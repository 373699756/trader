from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.score_r6 import evaluate_score_r6_forward
from trader.application.research.score_r6_models import ScoreR6ForwardDay, ScoreR6ForwardPair
from trader.domain.research.score_r6 import ScoreR6ForwardSpec


def _spec() -> ScoreR6ForwardSpec:
    registered = date(2026, 12, 1)
    planned = []
    current = registered
    while len(planned) < 20:
        current += timedelta(days=1)
        if current.weekday() < 5:
            planned.append(current)
    return ScoreR6ForwardSpec(
        research_identity="score_r6_forward_20261201_v1",
        preregistered_on=registered,
        planned_trade_dates=tuple(planned),
        historical_report_hash="1" * 64,
        frozen_candidate_hash="2" * 64,
        trading_calendar_hash="3" * 64,
        rule_identity_hash="4" * 64,
        config_strategy_identity_hash="5" * 64,
    )


def _days(spec: ScoreR6ForwardSpec, *, hybrid_better: bool = False) -> tuple[ScoreR6ForwardDay, ...]:
    days = []
    for trade_date in spec.planned_trade_dates:
        pairs = tuple(
            ScoreR6ForwardPair(
                code=f"60{index:04d}",
                board=("main", "chinext", "star")[index % 3],
                production_weight=0.2 if index < 5 else 0.0,
                local_weight=0.2 if 5 <= index < 10 else 0.0,
                hybrid_weight=0.2 if (10 <= index < 15 if hybrid_better else 5 <= index < 10) else 0.0,
                return_5d_pct=float(index),
                severe_loss=False,
            )
            for index in range(15)
        )
        days.append(
            ScoreR6ForwardDay(
                research_spec_hash=spec.content_hash,
                trade_date=trade_date,
                status="valid",
                pairs=pairs,
                oracle_codes=tuple(f"60{index:04d}" for index in range(5, 10)),
                failure_reason=None,
            )
        )
    return tuple(days)


def test_r6_forward_gate_keeps_hybrid_local_when_independent_gain_is_not_proved() -> None:
    spec = _spec()
    report = evaluate_score_r6_forward(spec, _days(spec), minimum_pair_count=100)

    assert report.status == "local_eligible"
    assert report.local_gate_passed is True
    assert report.hybrid_independent_gain_passed is False
    assert report.production_scope == "local_only"
    assert report.promotion_eligible is True


def test_r6_forward_gate_rejects_incomplete_planned_window() -> None:
    spec = _spec()
    report = evaluate_score_r6_forward(spec, _days(spec)[:-1], minimum_pair_count=100)

    assert report.status == "forward_collecting"
    assert report.promotion_eligible is False
    assert "planned_forward_days_incomplete" in report.failure_reasons


def test_r6_forward_gate_promotes_hybrid_only_after_same_stock_increment_passes() -> None:
    spec = _spec()
    report = evaluate_score_r6_forward(spec, _days(spec, hybrid_better=True), minimum_pair_count=100)

    assert report.status == "hybrid_eligible"
    assert report.hybrid_independent_gain_passed is True
    assert report.hybrid_p_value is not None and report.hybrid_p_value <= 0.05
    assert report.production_scope == "hybrid"

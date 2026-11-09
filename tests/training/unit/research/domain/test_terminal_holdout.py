from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId

from trader.training.domain.evaluation.terminal_holdout import (
    EvaluateTerminalHoldout,
    TerminalHoldoutEvaluation,
    TerminalHoldoutRow,
)


def _rows(count: int = 200, *, positive: bool = True) -> tuple[TerminalHoldoutRow, ...]:
    rows: list[TerminalHoldoutRow] = []
    for index in range(count):
        day = date(2025, 1, 1) + timedelta(days=index)
        for stock in range(20):
            actual = (0.008 + stock / 10000) if positive else -0.01
            selected = stock in {(index + offset) % 20 for offset in range(6)}
            rows.append(
                TerminalHoldoutRow(
                    trade_date=day,
                    code=f"60{stock:04d}",
                    board=("main", "chinext", "star")[stock % 3],
                    industry=f"industry-{stock % 10}",
                    market_state=("up", "down", "sideways")[index % 3],
                    volatility_state=("low", "high")[index % 2],
                    liquidity_state=("high", "low")[index % 2],
                    predicted_net_excess_return=actual,
                    ranking_score=50.0 + stock, local_score=50.0 + stock,
                    actual_net_excess_returns=(actual, actual - 0.003, actual - 0.008),
                    baseline_net_excess_returns=(0.0, -0.003, -0.008),
                    selected=selected,
                    baseline_selected=selected,
                    severe_loss=False,
                    baseline_severe_loss=False,
                    mae_atr20=-0.3,
                    baseline_mae_atr20=-0.3,
                    exit_untradable=False,
                    baseline_exit_untradable=False,
                    capacity=1_000_000.0,
                    baseline_capacity=1_000_000.0,
                    point_in_time_parity=True,
                ),
            )
    return tuple(rows)


def test_terminal_holdout_validates_positive_candidate_against_local_baseline() -> None:
    report = EvaluateTerminalHoldout(
        TerminalHoldoutEvaluation(
            strategy="tomorrow",
            profile_id="v2",
            research_identity="score_tomorrow_historical_candidate",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=_rows(),
        )
    )

    assert report.status == "historical_validated"
    assert report.metrics.evaluated_trade_dates == 200
    assert report.metrics.mean_net_excess_returns[0] > 0
    assert report.production_authority is False
    assert len(report.content_hash) == 64


@pytest.mark.parametrize("profile", ["v1", "v5"])
def test_model_ranking_metrics_use_final_percentile_instead_of_utility(profile: ScoringProfileId) -> None:
    rows = tuple(replace(row, predicted_net_excess_return=-row.predicted_net_excess_return) for row in _rows())
    report = EvaluateTerminalHoldout(TerminalHoldoutEvaluation(
        "tomorrow", "score_tomorrow_historical_candidate", "a" * 64, "b" * 64, rows, profile_id=profile,
    ))
    assert report.metrics.rank_ic == pytest.approx(1.0)
    assert report.metrics.ndcg_at_6 == pytest.approx(1.0)
    assert report.metrics.top_bottom_quintile_spread is not None
    assert report.metrics.top_bottom_quintile_spread > 0


@pytest.mark.parametrize("profile", ["v2", "v3"])
def test_local_score_profiles_reject_an_independent_ranking_score(profile: ScoringProfileId) -> None:
    rows = tuple(replace(row, ranking_score=99.0) for row in _rows())
    with pytest.raises(ValueError, match="local-score profile ranking mismatch"):
        EvaluateTerminalHoldout(TerminalHoldoutEvaluation(
            "tomorrow", "score_tomorrow_historical_candidate", "a" * 64, "b" * 64, rows, profile_id=profile,
        ))


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.01, 100.01, 12.345])
def test_terminal_scores_require_current_rounded_finite_range(score: float) -> None:
    with pytest.raises(ValueError, match="scores must be rounded"):
        replace(_rows(1)[0], ranking_score=score)


def test_terminal_holdout_does_not_open_when_parent_is_rejected() -> None:
    report = EvaluateTerminalHoldout(
        TerminalHoldoutEvaluation(
            strategy="tomorrow",
            profile_id=None,
            research_identity="score_tomorrow_historical_candidate",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=_rows(),
            parent_status="historical_rejected",
            parent_failure_reasons=("candidate_rejected",),
        )
    )

    assert report.status == "historical_rejected"
    assert report.profile_id is None
    assert report.terminal_holdout_opened is False
    assert report.failure_reasons == ("candidate_rejected",)


def test_a_ready_terminal_evaluation_cannot_run_without_a_scoring_profile() -> None:
    with pytest.raises(ValueError, match="profile"):
        EvaluateTerminalHoldout(TerminalHoldoutEvaluation(
            "tomorrow", "score_tomorrow_historical_candidate", "a" * 64, "b" * 64,
            _rows(), profile_id=None,
        ))


def test_terminal_holdout_requires_point_in_time_parity() -> None:
    rows = list(_rows())
    rows[0] = replace(rows[0], point_in_time_parity=False)
    with pytest.raises(ValueError, match="point-in-time"):
        EvaluateTerminalHoldout(
            TerminalHoldoutEvaluation(
                strategy="tomorrow",
                profile_id="v2",
                research_identity="score_tomorrow_historical_candidate",
                parent_hash="a" * 64,
                candidate_hash="b" * 64,
                rows=tuple(rows),
            )
        )


def test_terminal_holdout_reports_data_insufficient_when_a_preregistered_state_is_missing() -> None:
    rows = tuple(replace(row, market_state="up") for row in _rows())
    report = EvaluateTerminalHoldout(
        TerminalHoldoutEvaluation(
            strategy="tomorrow",
            profile_id="v2",
            research_identity="score_tomorrow_historical_candidate",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=rows,
        )
    )

    assert report.status == "historical_data_insufficient"
    assert "state_sample_below_40" in report.failure_reasons


def test_terminal_holdout_reports_unified_top6_risk_execution_and_capacity_metrics() -> None:
    report = EvaluateTerminalHoldout(
        TerminalHoldoutEvaluation(
            strategy="tomorrow",
            profile_id="v2",
            research_identity="score_tomorrow_historical_candidate",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=_rows(),
        )
    )

    assert report.metrics.ndcg_at_6 == pytest.approx(1.0)
    assert report.metrics.win_rate == pytest.approx(1.0)
    assert report.metrics.maximum_drawdown == pytest.approx(0.0)
    assert report.metrics.mean_mae_atr20 == pytest.approx(-0.3)
    assert report.metrics.exit_untradable_rate == pytest.approx(0.0)
    assert report.metrics.capacity == pytest.approx(6_000_000.0)


def test_terminal_holdout_rejects_worse_mae_exit_tradability_and_capacity() -> None:
    rows = tuple(
        replace(row, mae_atr20=-0.5, exit_untradable=True, capacity=500_000.0) if row.selected else row
        for row in _rows()
    )
    report = EvaluateTerminalHoldout(
        TerminalHoldoutEvaluation(
            strategy="tomorrow",
            profile_id="v2",
            research_identity="score_tomorrow_historical_candidate",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=rows,
        )
    )

    assert report.status == "historical_rejected"
    assert "capacity_below_production_baseline" in report.failure_reasons
    assert "mae_atr20_worse" in report.failure_reasons
    assert "exit_untradable_rate_worse" in report.failure_reasons


def test_terminal_holdout_rejects_a_second_terminal_open() -> None:
    report = EvaluateTerminalHoldout(
        TerminalHoldoutEvaluation(
            strategy="tomorrow",
            profile_id="v2",
            research_identity="score_tomorrow_historical_candidate",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=(),
            terminal_holdout_already_opened=True,
        )
    )

    assert report.terminal_holdout_opened is False
    assert report.status == "historical_rejected"
    assert report.failure_reasons == ("terminal_holdout_already_opened",)

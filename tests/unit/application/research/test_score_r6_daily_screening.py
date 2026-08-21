from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
    HistoricalHistoryIdentity,
)
from trader.application.research.score_r6_daily import ScoreR6DailyScreeningService
from trader.application.research.score_r6_daily_models import ScoreR6DailyRow
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC


class _Evidence:
    def __init__(self, rows: tuple[ScoreR6DailyRow, ...], *, coverage: float = 0.98) -> None:
        self._rows = rows
        self._coverage = coverage

    def inspect(self, _identity: str) -> HistoricalArchiveStatus:
        return HistoricalArchiveStatus(
            initialized=True,
            research_identity=SCORE_H0_V1_SPEC.research_identity,
            universe_count=100,
            completed_codes=int(100 * self._coverage),
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
        )

    def manifest(self, _spec) -> HistoricalArchiveManifest:  # noqa: ANN001
        return HistoricalArchiveManifest(
            research_identity=SCORE_H0_V1_SPEC.research_identity,
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
            universe_hash="1" * 64,
            histories_hash="2" * 64,
            histories=(HistoricalHistoryIdentity("600001", 640, "3" * 64),),
        )

    def score_r6_daily_rows(self, _spec) -> tuple[ScoreR6DailyRow, ...]:  # noqa: ANN001
        return self._rows


def _rows() -> tuple[ScoreR6DailyRow, ...]:
    rows: list[ScoreR6DailyRow] = []
    for split_start in (date(2025, 1, 2), date(2026, 1, 2)):
        for day_index in range(4):
            trade_date = split_start + timedelta(days=day_index)
            for stock_index in range(30):
                baseline_pick = stock_index < 6
                trend_pick = 6 <= stock_index < 12
                rows.append(
                    ScoreR6DailyRow(
                        trade_date=trade_date,
                        code=f"60{stock_index:04d}",
                        board="main" if stock_index not in {10, 11} else "chinext",
                        momentum_20_score=100.0 if baseline_pick else 0.0,
                        residual_momentum_score=100.0 if trend_pick else 0.0,
                        trend_efficiency_score=100.0 if trend_pick else 0.0,
                        downside_stability_score=100.0 if baseline_pick or trend_pick else 0.0,
                        drawdown_recovery_score=100.0 if trend_pick else 0.0,
                        liquidity_score=100.0 if baseline_pick or trend_pick else 0.0,
                        residual_return_60_5_pct=5.0,
                        recent_return_5d_pct=5.0,
                        close_ma20_spread_pct=1.0,
                        drawdown_60d_pct=-5.0,
                        downside_volatility_20d_pct=1.0,
                        volatility_20d_pct=1.0,
                        return_5d_pct=2.0 if trend_pick else (0.5 if baseline_pick else -1.0),
                    )
                )
    return tuple(rows)


def test_daily_trend_selects_on_training_and_evaluates_one_frozen_candidate_on_validation() -> None:
    report = ScoreR6DailyScreeningService(_Evidence(_rows()), minimum_split_days=2).execute(SCORE_R6_DAILY_SPEC)

    assert report.status == "forward_required"
    assert report.historical_gate_passed is True
    assert report.selected_candidate is not None
    assert report.training.selected_days == 4
    assert report.validation.selected_days == 4
    assert report.training.mean_net_excess_5d_pct > report.baseline_training.mean_net_excess_5d_pct
    assert report.validation.mean_net_excess_5d_pct > report.baseline_validation.mean_net_excess_5d_pct
    assert report.validation.maximum_board_fraction == 4 / 6
    assert report.failure_reasons == ()
    assert report.promotion_authority is False


def test_daily_trend_does_not_freeze_candidates_when_h0_coverage_is_short() -> None:
    report = ScoreR6DailyScreeningService(_Evidence(_rows(), coverage=0.94), minimum_split_days=2).execute(
        SCORE_R6_DAILY_SPEC
    )

    assert report.status == "insufficient_coverage"
    assert report.selected_candidate is None
    assert report.failure_reasons == ("score_h0_archive_coverage_incomplete",)
    assert report.promotion_authority is False

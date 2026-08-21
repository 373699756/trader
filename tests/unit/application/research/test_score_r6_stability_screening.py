from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
    HistoricalHistoryIdentity,
)
from trader.application.research.score_r6_daily_models import ScoreR6DailyRow
from trader.application.research.score_r6_stability import ScoreR6StabilityScreeningService
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6_stability import SCORE_R6_STABILITY_SPEC


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


class _Parent:
    def __init__(self, *, report_hash: str | None = None) -> None:
        self._report_hash = report_hash or SCORE_R6_STABILITY_SPEC.parent_report_hash

    def inspect(self) -> dict[str, object]:
        return {
            "report_hash": self._report_hash,
            "status": "historical_rejected",
            "historical_gate_passed": False,
            "selected_candidate_hash": SCORE_R6_STABILITY_SPEC.parent_candidate_hash,
            "failure_reasons": ["daily_trend_validation_turnover_failed"],
            "promotion_authority": False,
        }


class _BrokenParent:
    def inspect(self) -> dict[str, object]:
        raise RuntimeError("tampered parent")


def _rows() -> tuple[ScoreR6DailyRow, ...]:
    rows: list[ScoreR6DailyRow] = []
    for split_start in (date(2025, 1, 2), date(2026, 1, 2)):
        for day_index in range(4):
            trade_date = split_start + timedelta(days=day_index)
            active_start = 0 if day_index % 2 == 0 else 2
            for stock_index in range(30):
                trend_group = stock_index < 12
                active = active_start <= stock_index < active_start + 6
                proxy_pick = 12 <= stock_index < 18
                trend_score = 100.0 if active else (98.0 if trend_group else 0.0)
                rows.append(
                    ScoreR6DailyRow(
                        trade_date=trade_date,
                        code=f"60{stock_index:04d}",
                        board="main" if stock_index < 4 else "chinext",
                        momentum_20_score=100.0 if proxy_pick else 0.0,
                        residual_momentum_score=trend_score,
                        trend_efficiency_score=trend_score,
                        downside_stability_score=100.0 if proxy_pick else trend_score,
                        drawdown_recovery_score=trend_score,
                        liquidity_score=100.0 if proxy_pick else trend_score,
                        residual_return_60_5_pct=5.0,
                        recent_return_5d_pct=5.0,
                        close_ma20_spread_pct=1.0,
                        drawdown_60d_pct=-5.0,
                        downside_volatility_20d_pct=1.0,
                        volatility_20d_pct=1.0,
                        return_5d_pct=2.0 if trend_group else (0.5 if proxy_pick else -1.0),
                    )
                )
    return tuple(rows)


def test_stability_selects_once_and_reduces_turnover_without_losing_control_return() -> None:
    report = ScoreR6StabilityScreeningService(_Evidence(_rows()), _Parent(), minimum_split_days=2).execute(
        SCORE_R6_STABILITY_SPEC
    )

    assert report.status == "diagnostic_passed"
    assert report.diagnostic_gate_passed is True
    assert report.selected_candidate is not None
    assert report.training.selected_days == 4
    assert report.diagnostic.selected_days == 4
    assert report.training.mean_turnover < report.parent_training.mean_turnover
    assert report.diagnostic.mean_turnover < report.parent_diagnostic.mean_turnover
    assert report.diagnostic.mean_net_excess_5d_pct == report.parent_diagnostic.mean_net_excess_5d_pct
    assert report.evidence_class == "reused_observed_validation_window"
    assert report.promotion_authority is False
    assert report.failure_reasons == ()


def test_stability_fails_closed_when_the_bound_parent_report_changes() -> None:
    report = ScoreR6StabilityScreeningService(
        _Evidence(_rows()), _Parent(report_hash="f" * 64), minimum_split_days=2
    ).execute(SCORE_R6_STABILITY_SPEC)

    assert report.status == "parent_mismatch"
    assert report.selected_candidate is None
    assert report.failure_reasons == ("score_r6_daily_parent_artifact_mismatch",)
    assert report.promotion_authority is False


def test_stability_fails_closed_when_parent_inspection_raises() -> None:
    report = ScoreR6StabilityScreeningService(_Evidence(_rows()), _BrokenParent(), minimum_split_days=2).execute(
        SCORE_R6_STABILITY_SPEC
    )

    assert report.status == "parent_mismatch"
    assert report.failure_reasons == ("score_r6_daily_parent_artifact_mismatch",)

from __future__ import annotations

from datetime import date

from trader.application.research.historical_backtest import (
    HistoricalBarBacktestService,
    HistoricalScreeningDay,
)
from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
    HistoricalHistoryIdentity,
)
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC


class _Evidence:
    @staticmethod
    def inspect(_identity):
        return HistoricalArchiveStatus(
            initialized=True,
            research_identity="score_h0_v1",
            universe_count=100,
            completed_codes=98,
            bar_count=1000,
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
        )

    @staticmethod
    def screening_days(_spec):
        training = tuple(
            HistoricalScreeningDay(date(2025, 1, day), 100, 10, 1.0, 2.0, 0.2, 0.4, 0.1) for day in range(1, 11)
        )
        validation = tuple(
            HistoricalScreeningDay(date(2026, 1, day), 100, 10, 0.5, 1.0, 0.1, 0.3, 0.2) for day in range(1, 11)
        )
        return (*training, *validation)

    @staticmethod
    def manifest(_spec):
        return HistoricalArchiveManifest(
            research_identity="score_h0_v1",
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
            universe_hash="1" * 64,
            histories_hash="2" * 64,
            histories=(HistoricalHistoryIdentity("600001", 640, "3" * 64),),
        )


def test_bar_backtest_keeps_training_and_validation_separate_and_has_no_promotion_authority() -> None:
    report = HistoricalBarBacktestService(_Evidence(), minimum_split_days=10).execute(SCORE_H0_V1_SPEC)

    assert report.status == "screened"
    assert report.training.trade_dates == 10
    assert report.training.mean_excess_return_1d_pct == 0.8
    assert report.validation.trade_dates == 10
    assert report.validation.mean_excess_return_5d_pct == 0.7
    assert report.validation.severe_loss_rate == 0.2
    assert report.promotion_authority is False
    assert "historical_st_status_not_reconstructed" in report.limitations
    assert report.archive_manifest.universe_hash == "1" * 64
    assert report.archive_manifest.histories[0].content_hash == "3" * 64
    assert report.screening_version == "ohlcv_cross_section"
    assert report.training_window == (date(2024, 7, 1), date(2025, 12, 31))
    assert report.validation_window == (date(2026, 1, 1), date(2026, 7, 31))
    assert report.round_trip_cost_bps == 20
    assert len(report.report_hash) == 64


def test_bar_backtest_reports_insufficient_coverage_without_relabeling_it_as_passed() -> None:
    report = HistoricalBarBacktestService(_Evidence(), minimum_split_days=20).execute(SCORE_H0_V1_SPEC)

    assert report.status == "insufficient_coverage"
    assert report.promotion_authority is False

from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
    HistoricalHistoryIdentity,
)
from trader.application.research.score_r6 import ScoreR6HistoricalScreeningService
from trader.application.research.score_r6_models import ScoreR6HistoricalRow
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6 import SCORE_R6_HISTORICAL_SPEC


class _Evidence:
    def __init__(self, rows: tuple[ScoreR6HistoricalRow, ...], *, coverage: float = 0.98) -> None:
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

    def manifest(self, _spec):
        return HistoricalArchiveManifest(
            research_identity=SCORE_H0_V1_SPEC.research_identity,
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
            universe_hash="1" * 64,
            histories_hash="2" * 64,
            histories=(HistoricalHistoryIdentity("600001", 640, "3" * 64),),
        )

    def score_r6_rows(self, _spec):
        return self._rows


def _rows() -> tuple[ScoreR6HistoricalRow, ...]:
    rows = []
    start = date(2025, 1, 1)
    for day_index in range(12):
        trade_date = start + timedelta(days=day_index)
        for stock_index in range(30):
            score = stock_index / 29 * 100
            rows.append(
                ScoreR6HistoricalRow(
                    trade_date=trade_date,
                    code=f"60{stock_index:04d}",
                    board="main",
                    momentum_score=score,
                    stability_score=score,
                    liquidity_score=score,
                    volatility_20d_pct=5.0 if stock_index % 7 == 0 else 2.0,
                    return_5d_pct=(stock_index - 14) / 10,
                )
            )
    validation_start = date(2026, 1, 1)
    for day_index in range(12):
        trade_date = validation_start + timedelta(days=day_index)
        for stock_index in range(30):
            score = stock_index / 29 * 100
            rows.append(
                ScoreR6HistoricalRow(
                    trade_date=trade_date,
                    code=f"60{stock_index:04d}",
                    board="main",
                    momentum_score=score,
                    stability_score=score,
                    liquidity_score=score,
                    volatility_20d_pct=5.0 if stock_index % 7 == 0 else 2.0,
                    return_5d_pct=(stock_index - 14) / 10,
                )
            )
    return tuple(rows)


def test_r6_selects_on_training_then_only_evaluates_frozen_candidate_on_validation() -> None:
    report = ScoreR6HistoricalScreeningService(
        _Evidence(_rows()), minimum_split_days=10, minimum_board_rows=10_000
    ).execute(SCORE_R6_HISTORICAL_SPEC)

    assert report.status == "historical_screened"
    assert report.training.selected_days == 12
    assert report.validation.selected_days == 12
    assert report.global_candidate.training_metrics_hash == report.training.content_hash
    assert report.global_candidate.validation_metrics_hash == report.validation.content_hash
    assert report.validated_candidate is not None
    assert tuple(item.board for item in report.validated_candidate.boards) == ("main", "chinext", "star")
    assert report.board_candidates[0].source == "global_fallback"
    assert report.board_candidates[0].candidate_hash == report.global_candidate.candidate.content_hash
    assert report.validation_mode == "historical_only"
    assert report.promotion_authority is False
    assert "deepseek_facts_not_reconstructed" in report.limitations


def test_r6_does_not_freeze_candidates_when_h0_coverage_is_short() -> None:
    report = ScoreR6HistoricalScreeningService(_Evidence(_rows(), coverage=0.94), minimum_split_days=10).execute(
        SCORE_R6_HISTORICAL_SPEC
    )

    assert report.status == "insufficient_coverage"
    assert report.global_candidate is None
    assert report.failure_reasons == ("score_h0_archive_coverage_incomplete",)
    assert report.promotion_authority is False

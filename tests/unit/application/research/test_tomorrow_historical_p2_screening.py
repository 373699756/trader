from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
    HistoricalHistoryIdentity,
)
from trader.application.research.tomorrow_historical_p2_screening import (
    TomorrowHistoricalP2ModelFit,
    TomorrowHistoricalP2Row,
    TomorrowHistoricalP2ScreeningService,
)
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.tomorrow_historical_p2 import (
    TOMORROW_HISTORICAL_P2_SPEC,
    TomorrowHistoricalP2ModelArtifact,
)


class _Evidence:
    def __init__(self, rows: tuple[TomorrowHistoricalP2Row, ...], *, coverage: float = 0.98) -> None:
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

    def tomorrow_historical_p2_rows(self, _spec) -> tuple[TomorrowHistoricalP2Row, ...]:  # noqa: ANN001
        return self._rows


class _Trainer:
    def __init__(self) -> None:
        self.calls = 0

    def fit(self, training, validation, candidate):  # noqa: ANN001
        self.calls += 1
        artifact = TomorrowHistoricalP2ModelArtifact(
            candidate_id=candidate.candidate_id,
            feature_ids=tuple(f"feature_{index}" for index in range(6)),
            transformer_means=(0.0,) * 6,
            transformer_scales=(1.0,) * 6,
            linear_intercept=0.0,
            linear_coefficients=(1.0,) + (0.0,) * 5,
            lightgbm_model="fixed-test-model",
            lightgbm_best_iteration=1,
            training_rows=len(training),
            internal_validation_rows=max(1, len(training) // 5),
        )
        return TomorrowHistoricalP2ModelFit(
            artifact=artifact,
            training_predictions=tuple(row.alpha_features[0] for row in training),
            validation_predictions=tuple(row.alpha_features[0] for row in validation),
            validation_model_disagreement=(0.0,) * len(validation),
        )


class _EmptyTrainer(_Trainer):
    def fit(self, training, validation, candidate):  # noqa: ANN001
        fitted = super().fit(training, validation, candidate)
        return TomorrowHistoricalP2ModelFit(
            artifact=fitted.artifact,
            training_predictions=(-1.0,) * len(training),
            validation_predictions=(-1.0,) * len(validation),
            validation_model_disagreement=(0.0,) * len(validation),
        )


def _rows() -> tuple[TomorrowHistoricalP2Row, ...]:
    rows: list[TomorrowHistoricalP2Row] = []
    for start, day_count in ((date(2025, 1, 2), 10), (date(2026, 1, 2), 60)):
        for day_index in range(day_count):
            trade_date = start + timedelta(days=day_index)
            rotation = day_index % 30
            for stock_index in range(30):
                rank = (stock_index - rotation) % 30
                candidate_return = (30 - rank) / 10_000.0
                baseline_score = 100.0 if 6 <= rank < 12 else float(30 - rank)
                rows.append(
                    TomorrowHistoricalP2Row(
                        trade_date=trade_date,
                        code=f"60{stock_index:04d}",
                        board="main" if stock_index % 2 == 0 else "chinext",
                        alpha_features=(candidate_return, 0.0, 0.0, 0.0, 0.0, 0.0),
                        realized_volatility_20d=0.01 + rank / 10_000.0,
                        downside_semivariance_20d=0.01 + rank / 10_000.0,
                        drawdown_recovery_60d=1.0 - rank / 100.0,
                        amihud_20d=0.001 + rank / 100_000.0,
                        average_amount_20d=1_000_000.0,
                        baseline_score=baseline_score,
                        gross_excess_return=candidate_return,
                        mae_atr20=-0.5 if rank < 6 else (-2.0 if 6 <= rank < 12 else -1.0),
                    )
                )
    return tuple(rows)


def test_p2_screen_fits_the_only_candidate_once_and_evaluates_the_frozen_validation() -> None:
    trainer = _Trainer()

    execution = TomorrowHistoricalP2ScreeningService(_Evidence(_rows()), trainer).execute(TOMORROW_HISTORICAL_P2_SPEC)

    assert trainer.calls == 1
    assert execution.report.status == "historical_passed"
    assert execution.report.metrics.validation_pairs == 60 * 30
    assert execution.report.metrics.mean_net_increment_20bp is not None
    assert execution.report.metrics.mean_net_increment_20bp > 0.0
    assert execution.report.metrics.bootstrap_lower_bound_20bp is not None
    assert execution.report.metrics.bootstrap_lower_bound_20bp > 0.0
    assert execution.report.metrics.candidate_severe_loss_rate == 0.0
    assert execution.report.metrics.baseline_severe_loss_rate == 1.0
    assert execution.report.production_authority is False
    assert execution.model_artifact is not None


def test_p2_screen_rejects_incomplete_h0_before_training_and_does_not_try_an_alternative() -> None:
    trainer = _Trainer()

    execution = TomorrowHistoricalP2ScreeningService(_Evidence(_rows(), coverage=0.94), trainer).execute(
        TOMORROW_HISTORICAL_P2_SPEC
    )

    assert trainer.calls == 0
    assert execution.report.status == "historical_rejected"
    assert execution.report.failure_reasons == ("score_h0_archive_coverage_incomplete",)
    assert execution.model_artifact is None


def test_p2_screen_records_a_legal_empty_portfolio_as_a_gate_rejection() -> None:
    execution = TomorrowHistoricalP2ScreeningService(_Evidence(_rows()), _EmptyTrainer()).execute(
        TOMORROW_HISTORICAL_P2_SPEC
    )

    assert execution.report.status == "historical_rejected"
    assert "mean_increment_not_positive" in execution.report.failure_reasons
    assert "stock_concentration_limit" in execution.report.failure_reasons
    assert execution.report.metrics.candidate_severe_loss_rate == 0.0
    assert execution.report.metrics.maximum_board_fraction == 0.0

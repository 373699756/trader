"""Bounded cancellation and result projection for one standard model profile."""

from __future__ import annotations

from collections.abc import Callable

from trader.infra.serialization.canonical import CanonicalValue, ContentHash
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingProgressPort
from trader.training.infra.engine import TrainingRunResult
from trader.training.infra.profile.v1.training import V1TrainingResult


class CancellableTrainingProgress:
    def __init__(self, target: TomorrowTrainingProgressPort, cancelled: Callable[[], bool]) -> None:
        self._target = target
        self._cancelled = cancelled

    def publish(self, progress: TomorrowTrainingProgress) -> None:
        if self._cancelled():
            raise InterruptedError("profile training cancelled")
        self._target.publish(progress)


def ProjectProfileTrainingResult(profile: ScoringProfileId, result: TrainingRunResult) -> dict[str, object]:
    return {
        "schema_version": "profile_training_result",
        "profile": profile,
        "status": result.status,
        "run_id": result.run_id,
        "training_input_hash": result.training_input_hash,
        "reused": result.reused,
        "heads": [
            {
                "strategy": head.strategy.value,
                "status": head.status,
                "model_hash": head.model_hash,
                "report_hash": head.report_hash,
                "training_rows": head.training_rows,
                "validation_rows": head.validation_rows,
                "failure_reasons": list(head.failure_reasons),
                "source_cutoff": head.final_fit.source_cutoff.isoformat() if head.final_fit is not None else None,
                "label_cutoff": head.label_cutoff.isoformat() if head.label_cutoff is not None else None,
                "final_fit_start": head.final_fit.fit_dates[0].isoformat() if head.final_fit is not None else None,
                "final_fit_end": head.final_fit.fit_dates[-1].isoformat() if head.final_fit is not None else None,
                "final_fit_sessions": len(head.final_fit.fit_dates) if head.final_fit is not None else None,
                "diagnostic_fit_start": head.diagnostic_plan.fitting_dates[0].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_fit_end": head.diagnostic_plan.fitting_dates[-1].isoformat() if head.diagnostic_plan is not None else None,
                "calibration_start": head.diagnostic_plan.calibration_dates[0].isoformat() if head.diagnostic_plan is not None else None,
                "calibration_end": head.diagnostic_plan.calibration_dates[-1].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_validation_start": head.diagnostic_plan.validation_dates[0].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_validation_end": head.diagnostic_plan.validation_dates[-1].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_maturity_sessions": head.diagnostic_plan.maturity_sessions if head.diagnostic_plan is not None else None,
                "evaluation_model_role": "pre_refit_diagnostic" if head.final_fit is not None else None,
                "final_model_unseen_validation_passed": False,
                "coverage": [
                    {
                        "partition": item.partition,
                        "target": item.target,
                        "expected_dates": item.expected_dates,
                        "effective_dates": item.effective_dates,
                        "sample_rows": item.sample_rows,
                        "target_rows": item.target_rows,
                        "missing": [
                            {
                                "date": gap.trade_date.isoformat(),
                                "sample_rows": gap.sample_rows,
                                "candidates": gap.candidates,
                                "missing_future_returns": gap.missing_future_returns,
                            }
                            for gap in item.missing
                        ],
                    }
                    for item in head.coverage
                ],
            }
            for head in result.heads
        ],
        "prediction_return_validation_passed": False,
    }


def ProjectLearnedRankTrainingResult(result: V1TrainingResult) -> dict[str, object]:
    return {
        "schema_version": "profile_training_result",
        "profile": "v1",
        "reused": result.reused,
        "status": result.status,
        "production_authority": False,
        "prediction_return_validation_passed": False,
        "run_id": result.run_id,
        "training_input_hash": result.training_input_hash,
        "sample_database_peak_bytes": result.sample_database_peak_bytes,
        "failure_reasons": list(result.failure_reasons),
        "sample_counts": CanonicalValue(result.sample_counts),
        "coverage": [
            {
                "strategy": item.strategy.value,
                "partition": item.partition,
                "expected_dates": item.expected_dates,
                "effective_dates": item.effective_dates,
                "rows": item.rows,
            }
            for item in result.coverage
        ],
        "heads": [
            {
                "strategy": head.strategy.value,
                "status": "engineering_ready",
                "model_hash": head.content_hash,
                "report_hash": ContentHash(
                    (head.diagnostic.content_hash, head.split, head.validation, head.final_fit)
                ),
                "training_rows": head.training_rows,
                "validation_rows": head.validation.predicted_rows,
                "label_cutoff": head.split.label_cutoff.isoformat(),
                "source_cutoff": head.final_fit.source_cutoff.isoformat(),
                "final_fit_start": head.final_fit.fit_dates[0].isoformat(),
                "final_fit_end": head.final_fit.fit_dates[-1].isoformat(),
                "final_fit_sessions": len(head.final_fit.fit_dates),
                "diagnostic_fit_start": head.diagnostic.split.fitting[0].isoformat(),
                "diagnostic_fit_end": head.diagnostic.split.fitting[-1].isoformat(),
                "calibration_start": head.diagnostic.split.calibration[0].isoformat(),
                "calibration_end": head.diagnostic.split.calibration[-1].isoformat(),
                "diagnostic_validation_start": head.diagnostic.split.validation[0].isoformat(),
                "diagnostic_validation_end": head.diagnostic.split.validation[-1].isoformat(),
                "diagnostic_maturity_sessions": head.diagnostic.split.maturity_sessions,
                "evaluation_model_role": "pre_refit_diagnostic",
                "diagnostic_model_hash": head.diagnostic.content_hash,
                "calibration_transfer": True,
                "final_model_unseen_validation_passed": False,
                "mean_absolute_error": head.validation.mean_absolute_error,
                "mean_daily_rank_ic": head.validation.mean_daily_rank_ic,
                "mean_daily_ndcg_at_6": head.validation.mean_daily_ndcg_at_6,
                "prediction_return_validation_passed": False,
            }
            for head in result.heads
        ],
    }


__all__ = ["ProjectProfileTrainingResult", "ProjectLearnedRankTrainingResult", "CancellableTrainingProgress"]

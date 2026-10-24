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
        "status": result.status,
        "production_authority": False,
        "prediction_return_validation_passed": False,
        "run_id": result.run_id,
        "training_input_hash": result.training_input_hash,
        "sample_database_peak_bytes": result.sample_database_peak_bytes,
        "failure_reasons": list(result.failure_reasons),
        "sample_counts": CanonicalValue(result.sample_counts),
        "heads": [
            {
                "strategy": head.strategy.value,
                "status": "engineering_ready",
                "model_hash": head.content_hash,
                "report_hash": ContentHash((head.split, head.validation, result.sample_counts)),
                "training_rows": head.training_rows,
                "validation_rows": head.validation.predicted_rows,
                "label_cutoff": head.split.label_cutoff.isoformat(),
                "mean_absolute_error": head.validation.mean_absolute_error,
                "mean_daily_rank_ic": head.validation.mean_daily_rank_ic,
                "mean_daily_ndcg_at_6": head.validation.mean_daily_ndcg_at_6,
                "prediction_return_validation_passed": False,
            }
            for head in result.heads
        ],
    }


__all__ = ["ProjectProfileTrainingResult", "ProjectLearnedRankTrainingResult", "CancellableTrainingProgress"]

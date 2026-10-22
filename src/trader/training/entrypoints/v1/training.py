"""Explicit command projection for the new V1 engineering training workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from threading import Event

from trader.infra.serialization.canonical import CanonicalValue, ContentHash
from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownSignalController
from trader.training.infra.history.history_training_input import HistoryTrainingInputError
from trader.training.infra.profile.v1.training import V1TrainingResult


def RunLearnedRankTraining(
    runtime: RuntimeSettings, *, execute: Callable[[Callable[[], bool]], V1TrainingResult]
) -> int:
    """Fit an isolated pair of engineering models without granting production authority."""
    cancelled = Event()
    controller = ShutdownSignalController(
        timeout_seconds=runtime.pipeline.shutdown_timeout_seconds, on_first_signal=lambda _deadline: cancelled.set()
    )
    controller.install()
    try:
        result = execute(cancelled.is_set)
    except InterruptedError:
        print(json.dumps({"schema_version": "learned_rank_training_result", "profile": "v1", "status": "cancelled"}))
        return 130
    except (OSError, ValueError, HistoryTrainingInputError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "learned_rank_training_result",
                    "profile": "v1",
                    "status": "failed",
                    "failure_reasons": [type(exc).__name__],
                    "production_authority": False,
                }
            )
        )
        return 1
    finally:
        controller.mark_completed()
        controller.restore()
    print(
        json.dumps(
            {
                "schema_version": "learned_rank_training_result",
                "profile": "v1",
                "status": result.status,
                "production_authority": False,
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
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 130 if cancelled.is_set() else 0 if result.status == "engineering_ready" else 1


__all__ = ["RunLearnedRankTraining"]

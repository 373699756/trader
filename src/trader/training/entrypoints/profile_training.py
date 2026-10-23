"""Bounded cancellation and result projection for one standard model profile."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from threading import Event
from zoneinfo import ZoneInfo

from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownSignalController
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingProgressPort
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining
from trader.training.infra.history.history_training_due import EvaluateAllHistoryTrainingDue
from trader.training.infra.history.history_training_projection import ProjectHeadTrainingStatus
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.training_settings import LoadProfileTrainingSettings


class _CancellableProgress:
    def __init__(self, target: TomorrowTrainingProgressPort, cancelled: Callable[[], bool]) -> None:
        self._target = target
        self._cancelled = cancelled

    def publish(self, progress: TomorrowTrainingProgress) -> None:
        if self._cancelled():
            raise InterruptedError("profile training cancelled")
        self._target.publish(progress)


def RunModelTrainingCommand(runtime: RuntimeSettings, profile: TrainedProfileContract) -> int:
    """Project the explicit offline command; no clients or executors are created here."""
    paths = runtime.data_paths
    cancelled = Event()
    controller = ShutdownSignalController(
        timeout_seconds=runtime.pipeline.shutdown_timeout_seconds, on_first_signal=lambda _deadline: cancelled.set()
    )
    controller.install()
    try:
        with StderrTomorrowTrainingProgress(command_label=f"{profile.profile_id.upper()}训练") as progress:
            result = RunProfileTraining(
                ProfileTrainingRequest(
                    paths.history,
                    paths.models,
                    profile,
                    profile.heads,
                    LoadProfileTrainingSettings(runtime.project_root, profile.profile_id),
                    _CancellableProgress(progress, cancelled.is_set),
                    sample_root=paths.samples,
                )
            )
        maintenance = EvaluateAllHistoryTrainingDue(
            paths.history, paths.models, datetime.now(ZoneInfo("Asia/Shanghai")), selected_profile=profile
        )
        print(
            json.dumps(
                {
                    "schema_version": "profile_training_result",
                    "profile": profile.profile_id,
                    "status": result.status,
                    "run_id": result.run_id,
                    "reused": result.run_id is None and result.status == "engineering_ready",
                    "training_input_hash": result.training_input_hash,
                    "maintenance": [ProjectHeadTrainingStatus(head) for head in maintenance],
                    "heads": [
                        {
                            "strategy": h.strategy.value,
                            "status": h.status,
                            "training_rows": h.training_rows,
                            "validation_rows": h.validation_rows,
                            "failure_reasons": list(h.failure_reasons),
                        }
                        for h in result.heads
                    ],
                    "prediction_return_validation_passed": False,
                },
                ensure_ascii=False,
            )
        )
        return 0 if result.status == "engineering_ready" else 1
    except (InterruptedError, KeyboardInterrupt):
        print(json.dumps({"profile": profile.profile_id, "status": "cancelled"}))
        return 130
    finally:
        controller.mark_completed()
        controller.restore()


__all__ = ["RunModelTrainingCommand"]

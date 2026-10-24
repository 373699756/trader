"""Main-thread cancellation and independent selected-profile result projection."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from datetime import datetime
from threading import Event
from zoneinfo import ZoneInfo

from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownSignalController
from trader.infra.workers import ManagedWorkerExecutor, WorkerFutureExecutor
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingMemoryAdmission
from trader.training.application.selected_training import TrainingLocations
from trader.training.entrypoints.profile_training import (
    CancellableTrainingProgress,
    ProjectLearnedRankTrainingResult,
    ProjectProfileTrainingResult,
)
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
from trader.training.infra.engine import TrainingRunResult
from trader.training.infra.history.history_training_due import EvaluateAllHistoryTrainingDue
from trader.training.infra.history.history_training_projection import ProjectHeadTrainingStatus
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.selected_training import SelectedTrainingRequest, TrainSelectedProfiles


def RunSelectedTraining(
    runtime: RuntimeSettings,
    profiles: tuple[ScoringProfileId, ...],
    *,
    executor: ManagedWorkerExecutor,
    memory_admission: TrainingMemoryAdmission,
    paths: TrainingLocations,
) -> int:
    cancelled = Event()
    controller = ShutdownSignalController(
        timeout_seconds=runtime.pipeline.shutdown_timeout_seconds, on_first_signal=lambda _deadline: cancelled.set()
    )
    started = time.monotonic()
    controller.install()
    try:
        executor.start()
        with ExitStack() as scope:
            progresses = tuple(
                CancellableTrainingProgress(
                    scope.enter_context(StderrTomorrowTrainingProgress(command_label=f"{profile.upper()}训练")),
                    cancelled.is_set,
                )
                for profile in profiles
            )
            run = TrainSelectedProfiles(
                SelectedTrainingRequest(runtime, profiles, progresses, cancelled.is_set, memory_admission, paths),
                WorkerFutureExecutor(executor),
            )
        codes: list[int] = []
        for outcome in run.outcomes:
            result = outcome.result
            if isinstance(result, TrainingRunResult):
                payload = ProjectProfileTrainingResult(outcome.profile, result)
                if not cancelled.is_set():
                    contract = next(
                        item
                        for item in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE)
                        if item.profile_id == outcome.profile
                    )
                    try:
                        maintenance = EvaluateAllHistoryTrainingDue(
                            paths.history,
                            paths.models,
                            datetime.now(ZoneInfo("Asia/Shanghai")),
                            selected_profile=contract,
                        )
                        payload["maintenance"] = [ProjectHeadTrainingStatus(head) for head in maintenance]
                    except (OSError, RuntimeError, ValueError) as exc:
                        payload["maintenance_failure"] = type(exc).__name__
            elif result is not None:
                payload = ProjectLearnedRankTrainingResult(result)
            else:
                payload = {
                    "schema_version": "profile_training_result",
                    "profile": outcome.profile,
                    "status": "cancelled" if outcome.cancelled else "failed",
                    "failure": outcome.failure,
                }
            print(json.dumps(payload, ensure_ascii=False))
            codes.append(
                130
                if outcome.cancelled
                else int(result is None or result.status != "engineering_ready" or "maintenance_failure" in payload)
            )
        code = 130 if cancelled.is_set() or 130 in codes else int(any(codes))
        if len(profiles) > 1:
            print(
                json.dumps(
                    {
                        "schema_version": "selected_training_result",
                        "status": "cancelled" if code == 130 else "failed" if code else "completed",
                        "profiles": profiles,
                        "worker_capacity": len(profiles),
                        "history_scan_passes": run.history_scan_passes,
                        "history_rows_read": run.history_rows_read,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "results": [
                            {"profile": profile, "exit_code": value}
                            for profile, value in zip(profiles, codes, strict=True)
                        ],
                        "not_started": [],
                    }
                )
            )
        return code
    except (KeyboardInterrupt, InterruptedError):
        cancelled.set()
        print(json.dumps({"schema_version": "selected_training_result", "status": "cancelled", "profiles": profiles}))
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"schema_version": "selected_training_result", "status": "failed", "failure": reason}))
        return 1
    finally:
        try:
            executor.stop(wait=True, cancel_futures=True)
        finally:
            controller.mark_completed()
            controller.restore()

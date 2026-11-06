"""Main-thread cancellation and independent selected-profile result projection."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import ExitStack
from datetime import datetime
from threading import Event
from zoneinfo import ZoneInfo

from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.signal_cancellation import ManualCancellation
from trader.infra.workers import ManagedWorkerExecutor, WorkerFutureExecutor
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingFanoutSettings, TrainingMemoryAdmission, TrainingSampleSettings
from trader.training.application.selected_training import SelectedProfileResult, SelectedTrainingOutcome, SelectedTrainingPort, TrainSelectedProfiles, TrainingLocations
from trader.training.entrypoints.profile_training import (
    CancellableTrainingProgress,
    ProjectLearnedRankTrainingResult,
    ProjectProfileTrainingResult,
    ProjectTrainingOperationIssues,
)
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
from trader.training.entrypoints.training_timing_projection import ProjectTrainingExecutionTiming
from trader.training.application.profile_training import TrainingRunResult
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.history.history_training_due import EvaluateAllHistoryTrainingDue
from trader.training.infra.history.history_training_projection import ProjectHeadTrainingStatus
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.selected_training import (
    SelectedTrainingRequest,
)


def RunSelectedTraining(
    runtime: RuntimeSettings,
    profiles: tuple[ScoringProfileId, ...],
    *,
    executor: ManagedWorkerExecutor,
    memory_admission: TrainingMemoryAdmission,
    cpu_budget: TrainingCpuBudget,
    fanout_settings: TrainingFanoutSettings,
    sample_settings: TrainingSampleSettings,
    paths: TrainingLocations, sqlite_policy: SqliteIoPolicy,
    prepare_training: Callable[[SelectedTrainingRequest], SelectedTrainingPort[SelectedProfileResult]],
    memory_capacity_bytes: int | None = None,
) -> int:
    with ManualCancellation(timeout_seconds=runtime.pipeline.shutdown_timeout_seconds) as cancelled:
        return _run_selected_training(
            runtime,
            profiles,
            executor,
            memory_admission,
            paths,
            cancelled,
            cpu_budget=cpu_budget,
            fanout_settings=fanout_settings,
            sample_settings=sample_settings,
            sqlite_policy=sqlite_policy,
            prepare_training=prepare_training,
            memory_capacity_bytes=memory_capacity_bytes,
        )


def _run_selected_training(
    runtime: RuntimeSettings,
    profiles: tuple[ScoringProfileId, ...],
    executor: ManagedWorkerExecutor,
    memory_admission: TrainingMemoryAdmission,
    paths: TrainingLocations,
    cancelled: Event, *, cpu_budget: TrainingCpuBudget, fanout_settings: TrainingFanoutSettings,
    sample_settings: TrainingSampleSettings, sqlite_policy: SqliteIoPolicy,
    prepare_training: Callable[[SelectedTrainingRequest], SelectedTrainingPort[SelectedProfileResult]],
    memory_capacity_bytes: int | None = None,
) -> int:
    started = time.monotonic()
    measurements: TrainingExecutionMeasurements | None = None
    logging.getLogger(__name__).warning(
        "训练CPU预算 | 可用CPU %s | 拟合总线程 %s | 原生库基线线程 %s | 档位 %s",
        cpu_budget.available_cpus, cpu_budget.capacity, cpu_budget.native_threads, ",".join(profiles),
    )
    logging.getLogger(__name__).warning(
        "训练资源配置 | 拟合内存 %s字节 | 扫描批次 %s行 | 每档队列 %s批",
        memory_capacity_bytes, fanout_settings.batch_rows, fanout_settings.queue_batches,
    )
    try:
        measurements = TrainingExecutionMeasurements(profiles, time.monotonic)
        executor.start()
        with ExitStack() as scope:
            outputs = tuple(
                scope.enter_context(StderrTomorrowTrainingProgress(command_label=f"{profile.upper()}训练"))
                for profile in profiles
            )
            progresses = tuple(CancellableTrainingProgress(output, cancelled.is_set) for output in outputs)

            def report_outcome(outcome: SelectedTrainingOutcome[SelectedProfileResult]) -> None:
                output = outputs[profiles.index(outcome.profile)]
                result = outcome.result
                if outcome.cancelled:
                    output.publish_cancelled()
                elif result is None:
                    output.publish_result("failed", outcome.failure)
                else:
                    reasons = (
                        tuple(reason for head in result.heads for reason in head.failure_reasons)
                        if isinstance(result, TrainingRunResult)
                        else result.failure_reasons
                    )
                    output.publish_result(result.status, "; ".join(dict.fromkeys(reasons)) or None)

            selection = SelectedTrainingRequest(
                runtime, profiles, progresses, cancelled.is_set, memory_admission, paths, report_outcome,
                cpu_budget=cpu_budget,
                fanout_settings=fanout_settings,
                sample_settings=sample_settings,
                measurements=measurements,
                memory_capacity_bytes=memory_capacity_bytes,
            )
            run = TrainSelectedProfiles(
                prepare_training(selection), WorkerFutureExecutor(executor),
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
                            selected_profile=contract, sqlite_policy=sqlite_policy,
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
            payload["delivery_issues"] = ProjectTrainingOperationIssues(outcome.delivery_issues)
            payload["stage_seconds"] = dict(outputs[profiles.index(outcome.profile)].stage_durations)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            codes.append(
                130
                if outcome.cancelled
                else int(result is None or result.status != "engineering_ready" or "maintenance_failure" in payload)
            )
        code = 130 if cancelled.is_set() or 130 in codes else int(any(codes))
        print(
            json.dumps(
                {
                    "schema_version": "selected_training_result",
                    "status": "cancelled" if code == 130 else "failed" if code else "completed",
                    "profiles": profiles,
                    "worker_capacity": len(profiles),
                    "available_cpus": cpu_budget.available_cpus,
                    "fitting_cpu_capacity": cpu_budget.capacity,
                    "native_threads": cpu_budget.native_threads,
                    "profile_threads": {
                        "v1": cpu_budget.profile_threads.v1,
                        "v2": cpu_budget.profile_threads.v2,
                        "v3": cpu_budget.profile_threads.v3,
                    },
                    "memory_capacity_bytes": memory_capacity_bytes,
                    "fanout": {
                        "batch_rows": fanout_settings.batch_rows,
                        "queue_batches": fanout_settings.queue_batches,
                    },
                    "sample_resources": {
                        "sqlite_cache_bytes": sample_settings.sqlite_cache_bytes,
                        "sqlite_mmap_bytes": sample_settings.sqlite_mmap_bytes,
                        "matrix_memory_bytes": sample_settings.matrix_memory_bytes,
                        "sequence_cache_bytes": sample_settings.sequence_cache_bytes,
                        "sequence_writer_codes": sample_settings.sequence_writer_codes,
                        "v1_sqlite_cache_bytes": sample_settings.v1_sqlite_cache_bytes,
                        "v1_batch_rows": sample_settings.v1_batch_rows,
                        "tcn_batch_memory_bytes": sample_settings.tcn_batch_memory_bytes,
                    },
                    "peak_reserved_cpu_threads": cpu_budget.peak_reserved_threads,
                    "history_scan_passes": run.history_scan_passes,
                    "history_rows_read": run.history_rows_read,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "timing": ProjectTrainingExecutionTiming(run.timing) if run.timing is not None else None,
                    "results": [
                        {
                            "profile": outcome.profile,
                            "exit_code": value,
                            "post_commit_issues": ProjectTrainingOperationIssues(
                                outcome.result.post_commit_issues if outcome.result is not None else (),
                            ),
                            "delivery_issues": ProjectTrainingOperationIssues(outcome.delivery_issues),
                        }
                        for outcome, value in zip(run.outcomes, codes, strict=True)
                    ],
                    "not_started": [],
                }, indent=2, ensure_ascii=False
            )
        )
        return code
    except (KeyboardInterrupt, InterruptedError, CancelledError):
        cancelled.set()
        print(json.dumps({
            "schema_version": "selected_training_result",
            "status": "cancelled",
            "profiles": profiles,
            "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
        }, indent=2, ensure_ascii=False))
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        if cancelled.is_set():
            print(
                json.dumps({
                    "schema_version": "selected_training_result",
                    "status": "cancelled",
                    "profiles": profiles,
                    "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
                }, indent=2, ensure_ascii=False)
            )
            return 130
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({
            "schema_version": "selected_training_result",
            "status": "failed",
            "failure": reason,
            "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
        }, indent=2, ensure_ascii=False))
        return 1
    finally:
        executor.stop(
            wait=True,
            cancel_futures=True,
            deadline=ShutdownDeadline.start(runtime.pipeline.shutdown_timeout_seconds),
        )

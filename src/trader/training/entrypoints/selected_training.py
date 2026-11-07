"""Main-thread cancellation and independent selected-profile result projection."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import json
import time
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

from trader.infra.settings import RuntimeSettings
from trader.infra.shutdown import ShutdownStep
from trader.infra.signal_cancellation import ManualCancellation, ManualCancellationScope
from trader.infra.workers import ManagedWorkerExecutor, WorkerFutureExecutor
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingFanoutSettings, TrainingMemoryAdmission, TrainingSampleSettings
from trader.training.application.selected_training import SelectedProfileResult, SelectedTrainingOutcome, SelectedTrainingPort, SelectedTrainingRun, TrainSelectedProfiles, TrainingLocations
from trader.training.entrypoints.profile_training import (
    CancellableTrainingProgress,
    ProjectLearnedRankTrainingResult,
    ProjectProfileTrainingResult,
    ProjectTrainingOperationIssues,
)
from trader.training.entrypoints.tomorrow_training_progress import StderrTomorrowTrainingProgress
from trader.training.entrypoints.training_timing_projection import ProjectTrainingExecutionTiming
from trader.training.application.profile_training import TrainingRunResult
from trader.training.application.training_diagnostics import BuildTrainingOperationIssue
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
    started_at: float | None = None,
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
            started_at=started_at,
        )


def _run_selected_training(
    runtime: RuntimeSettings,
    profiles: tuple[ScoringProfileId, ...],
    executor: ManagedWorkerExecutor,
    memory_admission: TrainingMemoryAdmission,
    paths: TrainingLocations,
    cancelled: ManualCancellationScope, *, cpu_budget: TrainingCpuBudget, fanout_settings: TrainingFanoutSettings,
    sample_settings: TrainingSampleSettings, sqlite_policy: SqliteIoPolicy,
    prepare_training: Callable[[SelectedTrainingRequest], SelectedTrainingPort[SelectedProfileResult]],
    memory_capacity_bytes: int | None = None,
    started_at: float | None = None,
) -> int:
    started = time.monotonic()
    measurements: TrainingExecutionMeasurements | None = None
    summary: dict[str, object]
    run: SelectedTrainingRun[SelectedProfileResult] | None = None
    returned_training_code: int | None = None
    progress_closed = False
    observed_outcomes: list[SelectedTrainingOutcome[SelectedProfileResult]] = []
    outcome_lock = Lock()
    reported_profiles: set[ScoringProfileId] = set()
    complete_outcome_summary = False
    outputs: tuple[StderrTomorrowTrainingProgress, ...] = ()
    shutdown_failure: str | None = None
    command_output = StderrTomorrowTrainingProgress(command_label="train", started_at=started_at, start_heartbeat=False)
    command_output.report_notice("资源预检", (
        f"可用CPU {cpu_budget.available_cpus} | 拟合总线程预算 {cpu_budget.capacity} "
        f"| 原生库基线线程 {cpu_budget.native_threads} | 档位 {','.join(profiles)} "
        f"| 拟合内存预算 {memory_capacity_bytes if memory_capacity_bytes is not None else '未提供'}字节 "
        f"| 扫描批次 {fanout_settings.batch_rows}行 | 每档队列 {fanout_settings.queue_batches}批"
    ), state="完成")
    try:
        measurements = TrainingExecutionMeasurements(profiles, time.monotonic)
        executor.start()
        with ExitStack() as scope:
            outputs = tuple(
                scope.enter_context(StderrTomorrowTrainingProgress(
                    command_label=f"train·{profile.upper()}", started_at=command_output.started_at,
                    start_heartbeat=True, cancellation=cancelled,
                ))
                for profile in profiles
            )
            progresses = tuple(CancellableTrainingProgress(output, cancelled.is_set) for output in outputs)

            def report_outcome(outcome: SelectedTrainingOutcome[SelectedProfileResult]) -> None:
                observed = outcome
                try:
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
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    observed = replace(
                        outcome,
                        delivery_issues=(*outcome.delivery_issues, BuildTrainingOperationIssue("outcome_notification", exc)),
                    )
                    raise
                finally:
                    with outcome_lock:
                        observed_outcomes.append(observed)

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
            returned_training_code = (
                130 if cancelled.is_set() or any(outcome.cancelled for outcome in run.outcomes)
                else int(any(outcome.result is None or outcome.result.status != "engineering_ready" for outcome in run.outcomes))
            )
        progress_closed = True
        codes: list[int] = []
        for outcome in run.outcomes:
            result = outcome.result
            payload = _project_outcome(outcome)
            if isinstance(result, TrainingRunResult):
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
            payload["stage_seconds"] = dict(outputs[profiles.index(outcome.profile)].stage_durations)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            reported_profiles.add(outcome.profile)
            codes.append(
                130
                if outcome.cancelled
                else int(result is None or result.status != "engineering_ready" or "maintenance_failure" in payload)
            )
        code = 130 if cancelled.is_set() or 130 in codes else int(any(codes))
        summary = {
            "schema_version": "selected_training_result",
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
        }
        complete_outcome_summary = True
    except (KeyboardInterrupt, InterruptedError, CancelledError) as exc:
        cancelled.set()
        code = 130
        summary = {
            "schema_version": "selected_training_result",
            "profiles": profiles,
            "failure": type(exc).__name__,
            "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        code = 130 if cancelled.is_set() else 1
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        summary = {
            "schema_version": "selected_training_result",
            "profiles": profiles,
            "failure": reason,
            "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
        }
    finally:
        deadline = cancelled.shutdown_deadline()
        try:
            shutdown = executor.stop(wait=True, cancel_futures=True, deadline=deadline)
            if not isinstance(shutdown, ShutdownStep):
                raise TypeError("training executor shutdown requires ShutdownStep")
        except (KeyboardInterrupt, InterruptedError, CancelledError) as exc:
            cancelled.set()
            shutdown_failure = type(exc).__name__
            shutdown = ShutdownStep(
                "training_executor", False, deadline.expired,
                detail=f"executor shutdown cancelled:{type(exc).__name__}",
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            shutdown_failure = type(exc).__name__
            shutdown = ShutdownStep(
                "training_executor", False, deadline.expired,
                detail=f"executor shutdown failed:{type(exc).__name__}",
            )
        cancelled.record_shutdown(shutdown)
    if not complete_outcome_summary:
        with outcome_lock:
            available_outcomes = run.outcomes if run is not None else tuple(observed_outcomes)
        summary["results"] = [
            {
                "profile": outcome.profile,
                "exit_code": 130 if outcome.cancelled else int(outcome.result is None or outcome.result.status != "engineering_ready"),
                "post_commit_issues": ProjectTrainingOperationIssues(
                    outcome.result.post_commit_issues if outcome.result is not None else (),
                ),
                "delivery_issues": ProjectTrainingOperationIssues(outcome.delivery_issues),
            }
            for outcome in available_outcomes
        ]
        for outcome in available_outcomes:
            if outcome.profile not in reported_profiles:
                payload = _project_outcome(outcome)
                payload["stage_seconds"] = dict(outputs[profiles.index(outcome.profile)].stage_durations)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
    training_code = returned_training_code if not progress_closed and returned_training_code is not None else code
    summary["training_status"] = "cancelled" if training_code == 130 else "failed" if training_code else "completed"
    code = 130 if cancelled.is_set() or code == 130 or training_code == 130 else (code or int(not cancelled.shutdown_completed or cancelled.watchdog_failure is not None))
    summary["status"] = "cancelled" if code == 130 else "failed" if code else "completed"
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    summary["shutdown"] = {
        "name": "training_executor",
        "completed": shutdown.completed,
        "timed_out": shutdown.timed_out,
        "cancelled_count": shutdown.cancelled_count,
        "failure": shutdown_failure,
        "watchdog_started": cancelled.watchdog_started,
        "watchdog_failure": type(cancelled.watchdog_failure).__name__ if cancelled.watchdog_failure is not None else None,
    }
    summary["progress_shutdown"] = [
        {"profile": profiles[index], "name": step.name, "completed": step.completed,
         "timed_out": step.timed_out, "cancelled_count": step.cancelled_count, "detail": step.detail}
        for index, output in enumerate(outputs) if (step := output.shutdown_step) is not None
    ]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    command_output.report_notice("结果汇总", f"档位 {','.join(profiles)} | 退出码 {code}", state="取消" if code == 130 else "失败" if code else "完成")
    return code


def _project_outcome(outcome: SelectedTrainingOutcome[SelectedProfileResult]) -> dict[str, object]:
    result = outcome.result
    if isinstance(result, TrainingRunResult):
        payload = ProjectProfileTrainingResult(outcome.profile, result)
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
    return payload

"""Main-thread cancellation and independent selected-profile result projection."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

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
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingFanoutSettings, TrainingMemoryAdmission, TrainingSampleSettings
from trader.training.application.selected_training import SelectedD25IndustryUnavailable, SelectedProfileResult, SelectedTrainingOutcome, SelectedTrainingPort, SelectedTrainingRun, TrainSelectedProfiles, TrainingLocations
from trader.training.entrypoints.profile_training import (
    ProjectLearnedRankTrainingResult,
    ProjectProfileTrainingResult,
    ProjectTrainingOperationIssues,
    ProjectTrainingIndustryCoverage,
    TrainingDateCoverageSummary,
    TrainingDateRangeSummary,
)
from trader.training.application.tomorrow_training import CancellableTrainingProgress
from trader.training.entrypoints.tomorrow_training_progress import (
    ReadTrainingProgressLayout,
    StderrTomorrowTrainingProgress,
    TrainingProgressLayout,
)
from trader.training.entrypoints.training_timing_projection import ProjectTrainingExecutionTiming
from trader.training.entrypoints.report_output import ProjectTrainingError, TrainingErrorSummary, TrainingReportOutput
from trader.training.application.error_diagnostics import CaptureTrainingError, TrainingErrorDiagnostic
from trader.training.application.profile_training import TrainingRunResult
from trader.training.application.training_diagnostics import BuildTrainingOperationIssue
from trader.training.domain.tomorrow_probability import TomorrowProbabilityProfileResult
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.history.history_training_due import EvaluateAllHistoryTrainingDue
from trader.training.infra.history.history_training_expectation import HistoryTrainingExpectation
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.history.history_training_projection import ProjectHeadTrainingStatus
from trader.training.infra.research.close_return.selection import ResearchCloseReturnProfile
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
    load_expectation: Callable[[TrainedProfileContract, datetime], HistoryTrainingExpectation],
    memory_capacity_bytes: int | None = None,
    started_at: float | None = None,
    terminal_columns: int | None = None,
    report_output: TrainingReportOutput | None = None,
    strategy: Strategy | None = None,
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
            load_expectation=load_expectation,
            memory_capacity_bytes=memory_capacity_bytes,
            started_at=started_at,
            terminal_columns=terminal_columns,
            report_output=TrainingReportOutput() if report_output is None else report_output,
            strategy=strategy,
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
    load_expectation: Callable[[TrainedProfileContract, datetime], HistoryTrainingExpectation],
    memory_capacity_bytes: int | None = None,
    started_at: float | None = None,
    terminal_columns: int | None = None,
    report_output: TrainingReportOutput,
    strategy: Strategy | None,
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
    shutdown_diagnostic: TrainingErrorDiagnostic | None = None
    layout = ReadTrainingProgressLayout() if terminal_columns is None else TrainingProgressLayout(terminal_columns)
    command_output = StderrTomorrowTrainingProgress(
        command_label="train", started_at=started_at, start_heartbeat=False, layout=layout,
    )
    command_output.report_notice("资源预检", (
        f"可用CPU {cpu_budget.available_cpus} | 拟合总线程预算 {cpu_budget.capacity} "
        f"| 原生库基线线程 {cpu_budget.native_threads} | 档位 {','.join(profiles)} "
        f"| 拟合内存预算 {memory_capacity_bytes if memory_capacity_bytes is not None else '未提供'}字节 "
        f"| 扫描批次 {fanout_settings.batch_rows}行 | 每档队列 {fanout_settings.queue_batches}批"
    ), state="完成")
    try:
        measurements = TrainingExecutionMeasurements(profiles, time.monotonic)
        if strategy is not Strategy.TOMORROW:
            executor.start()
        with ExitStack() as scope:
            outputs = tuple(
                scope.enter_context(StderrTomorrowTrainingProgress(
                    command_label=f"train·{profile.upper()}", started_at=command_output.started_at,
                    start_heartbeat=True, cancellation=cancelled,
                    layout=layout,
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
                        output.publish_result("failed", TrainingErrorSummary(outcome.diagnostic)
                                              if outcome.diagnostic is not None else outcome.failure)
                    else:
                        reasons = (
                            tuple(reason for head in result.heads for reason in head.failure_reasons)
                            if isinstance(result, TrainingRunResult)
                            else result.failure_reasons
                        )
                        coverage = tuple(
                            item for head in result.heads for item in head.date_coverage
                        ) if isinstance(result, TrainingRunResult) else result.date_coverage
                        coverage_summary = TrainingDateCoverageSummary(coverage)
                        range_summary = TrainingDateRangeSummary(coverage)
                        if range_summary:
                            output.report_notice("日期范围", range_summary, state="完成")
                        details = (*dict.fromkeys(reasons), *((coverage_summary,) if coverage_summary else ()))
                        output.publish_result(result.status, "; ".join(details) or None)
                        diagnostics = (
                            tuple((head.strategy.value, head.diagnostic) for head in result.heads)
                            if isinstance(result, TrainingRunResult) else (("全局", result.diagnostic),)
                        )
                        for head_name, diagnostic in diagnostics:
                            if diagnostic is not None:
                                output.report_notice("错误定位", f"{head_name}；{TrainingErrorSummary(diagnostic)}", state="失败")
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
                begin_shutdown=cancelled.shutdown_deadline,
                strategy=strategy,
            )
            run = TrainSelectedProfiles(
                prepare_training(selection), WorkerFutureExecutor(executor),
            )
            tomorrow = run.tomorrow_probability
            tomorrow_failed = any(not head.result.ready for head in tomorrow)
            for head in tomorrow:
                target = head.target.threshold_bps / 100
                tomorrow_result = head.result
                shortfall = tomorrow_result.window_shortfall
                window = "" if shortfall is None else f" | 可用/所需日 {shortfall.available_days}/{shortfall.required_days}"
                command_output.report_notice(
                    f"Tomorrow ≥{target:g}%模型",
                    f"{head.profile} | {tomorrow_result.failure or tomorrow_result.algorithm or '模型未就绪'}{window}",
                    state="完成" if tomorrow_result.ready else "失败",
                )
            returned_training_code = (
                130 if cancelled.is_set() or any(outcome.cancelled for outcome in run.outcomes)
                else int(tomorrow_failed or any(
                    outcome.result is None or outcome.result.status != "engineering_ready" for outcome in run.outcomes
                ))
            )
        progress_closed = True
        codes: list[int] = []
        d25_targets = dict(run.d25_targets)
        for outcome in run.outcomes:
            result = outcome.result
            payload = _project_outcome(outcome)
            if (d25_target := d25_targets.get(outcome.profile)) is not None:
                payload["d25_target"] = {"identity": d25_target.identity, "threshold_bps": d25_target.threshold_bps}
            if isinstance(result, TrainingRunResult):
                if not cancelled.is_set():
                    contract = (
                        StandardD25Profile(outcome.profile) if outcome.profile in d25_targets
                        else ResearchCloseReturnProfile(outcome.profile)
                    )
                    try:
                        maintenance = EvaluateAllHistoryTrainingDue(
                            paths.history,
                            paths.models,
                            datetime.now(ZoneInfo("Asia/Shanghai")),
                            load_expectation=load_expectation,
                            maintenance_path=paths.training_maintenance,
                            selected_profile=contract, sqlite_policy=sqlite_policy,
                        )
                        payload["maintenance"] = [ProjectHeadTrainingStatus(head) for head in maintenance]
                    except (OSError, RuntimeError, ValueError) as exc:
                        payload["maintenance_failure"] = type(exc).__name__
                        diagnostic = CaptureTrainingError(exc)
                        payload["maintenance_diagnostic"] = ProjectTrainingError(diagnostic)
                        outputs[profiles.index(outcome.profile)].report_notice(
                            "训练维护", "模型结果已返回；维护失败 " + TrainingErrorSummary(diagnostic), state="失败",
                        )
            payload["stage_seconds"] = dict(outputs[profiles.index(outcome.profile)].stage_durations)
            _report_issues(outcome, outputs[profiles.index(outcome.profile)])
            report_output.emit(payload)
            reported_profiles.add(outcome.profile)
            codes.append(
                130
                if outcome.cancelled
                else int(result is None or result.status != "engineering_ready" or "maintenance_failure" in payload)
            )
        code = 130 if cancelled.is_set() or 130 in codes else int(tomorrow_failed or any(codes))
        summary = {
            "industry": ProjectTrainingIndustryCoverage(run.industry_snapshot) if run.industry_snapshot is not None else None,
            "strategy": strategy.value if strategy is not None else None,
            "profiles": profiles,
            "worker_capacity": 0 if strategy is Strategy.TOMORROW else len(profiles),
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
            "history_reads_by_strategy": {
                **({"tomorrow": {"scans": run.tomorrow_scan_passes, "rows": run.tomorrow_rows_read}}
                   if strategy is not Strategy.D25 else {}),
                **({"d25": {"scans": run.history_scan_passes - run.tomorrow_scan_passes,
                            "rows": run.history_rows_read - run.tomorrow_rows_read}} if strategy is not Strategy.TOMORROW else {}),
            },
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
        if tomorrow:
            summary["tomorrow_probability"] = [_project_tomorrow(head) for head in tomorrow]
        complete_outcome_summary = True
    except (KeyboardInterrupt, InterruptedError, CancelledError) as exc:
        cancelled.set()
        code = 130
        summary = {
            "profiles": profiles,
            "failure": type(exc).__name__,
            "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        code = 130 if cancelled.is_set() else 1
        diagnostic = CaptureTrainingError(exc)
        reason = diagnostic.causes[0].code
        command_output.report_notice("执行错误", TrainingErrorSummary(diagnostic), state="失败")
        summary = {
            "profiles": profiles,
            "failure": type(exc).__name__ if reason == "training_operation_failed" else reason,
            "diagnostic": ProjectTrainingError(diagnostic),
            "timing": ProjectTrainingExecutionTiming(measurements.snapshot()) if measurements is not None else None,
        }
        if isinstance(exc, SelectedD25IndustryUnavailable):
            summary["tomorrow_probability"] = [_project_tomorrow(head) for head in exc.tomorrow_probability]
            summary["history_scan_passes"] = exc.history_scan_passes
            summary["history_rows_read"] = exc.history_rows_read
            summary["history_reads_by_strategy"] = {
                "tomorrow": {"scans": exc.history_scan_passes, "rows": exc.history_rows_read},
                "d25": {"scans": 0, "rows": 0},
            }
            summary["not_started"] = profiles
    finally:
        deadline = cancelled.shutdown_deadline()
        try:
            shutdown = executor.stop(wait=True, cancel_futures=True, deadline=deadline)
            if not isinstance(shutdown, ShutdownStep):
                raise TypeError("training executor shutdown requires ShutdownStep")
        except (KeyboardInterrupt, InterruptedError, CancelledError) as exc:
            cancelled.set()
            shutdown_failure = type(exc).__name__
            shutdown_diagnostic = CaptureTrainingError(exc)
            shutdown = ShutdownStep(
                "training_executor", False, deadline.expired,
                detail=f"executor shutdown cancelled:{type(exc).__name__}",
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            shutdown_failure = type(exc).__name__
            shutdown_diagnostic = CaptureTrainingError(exc)
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
                _report_issues(outcome, outputs[profiles.index(outcome.profile)])
                report_output.emit(payload)
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
    if not cancelled.shutdown_completed or shutdown_failure is not None:
        details = TrainingErrorSummary(shutdown_diagnostic) if shutdown_diagnostic is not None else "shutdown_incomplete"
        command_output.report_notice("资源关闭", f"关闭未完成或失败 {details}", state="失败")
    if shutdown_diagnostic is not None:
        summary["shutdown_diagnostic"] = ProjectTrainingError(shutdown_diagnostic)
    report_output.emit(summary)
    command_output.report_notice("结果汇总", f"档位 {','.join(profiles)} | 退出码 {code}", state="取消" if code == 130 else "失败" if code else "完成")
    return code


def _project_tomorrow(head: TomorrowProbabilityProfileResult) -> dict[str, object]:
    result = head.result
    diagnostic = result.diagnostic
    return {
        "target": head.target.identity,
        "threshold_bps": head.target.threshold_bps,
        "profile": head.profile,
        "ready": result.ready,
        "reused": result.reused,
        "history_snapshot_hash": result.history_snapshot_hash,
        "model_identity": result.model_identity,
        "algorithm": result.algorithm,
        "failure": result.failure,
        "window_shortfall": None if result.window_shortfall is None else {
            "partition": result.window_shortfall.partition, "required_days": result.window_shortfall.required_days,
            "available_days": result.window_shortfall.available_days,
        },
        "historical_universe_proven": False,
        "point_in_time_parity": False,
        "fitting_dates": [day.isoformat() for day in result.fitting_dates],
        "early_stopping_dates": [day.isoformat() for day in result.early_stopping_dates],
        "calibration_dates": [day.isoformat() for day in result.calibration_dates],
        "diagnostic": None if diagnostic is None else {
            "model_identity": diagnostic.model_identity,
            "fitting_dates": [day.isoformat() for day in diagnostic.fitting_dates],
            "early_stopping_dates": [day.isoformat() for day in diagnostic.early_stopping_dates],
            "calibration_dates": [day.isoformat() for day in diagnostic.calibration_dates],
            "assessment_dates": [day.isoformat() for day in diagnostic.assessment_dates],
            "rows": diagnostic.rows, "positive_rows": diagnostic.positive_rows, "brier": diagnostic.brier,
            "daily_close_proxy": {"hits": diagnostic.hits, "seats": diagnostic.seats, "unsettled": diagnostic.unsettled},
        },
        "assessment_rows": result.assessment_rows,
        "assessment_positive_rows": result.assessment_positive_rows,
        "assessment_brier": result.assessment_brier,
        "assessment_daily_close_proxy": {
            "hits": result.assessment_hits, "seats": result.assessment_seats, "unsettled": result.assessment_unsettled,
        },
        "pending_labels": result.pending_labels,
        "missing_features": result.missing_features,
    }


def _project_outcome(outcome: SelectedTrainingOutcome[SelectedProfileResult]) -> dict[str, object]:
    result = outcome.result
    if isinstance(result, TrainingRunResult):
        payload = ProjectProfileTrainingResult(outcome.profile, result)
    elif result is not None:
        payload = ProjectLearnedRankTrainingResult(result)
    else:
        payload = {
            "profile": outcome.profile,
            "status": "cancelled" if outcome.cancelled else "failed",
            "failure": outcome.failure,
        }
    payload["delivery_issues"] = ProjectTrainingOperationIssues(outcome.delivery_issues)
    if outcome.diagnostic is not None:
        payload["diagnostic"] = ProjectTrainingError(outcome.diagnostic)
    return payload


def _report_issues(outcome: SelectedTrainingOutcome[SelectedProfileResult], output: StderrTomorrowTrainingProgress) -> None:
    issues = (*outcome.delivery_issues, *(outcome.result.post_commit_issues if outcome.result is not None else ()))
    for issue in issues:
        details = TrainingErrorSummary(issue.diagnostic) if issue.diagnostic is not None else issue.error_type
        output.report_notice("结果交付", f"{issue.stage}；训练结果单独保留；{details}", state="失败")

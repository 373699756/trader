"""JSON projection for the observable scheduler runtime status."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict

from trader.application.ports.model_scoring import ScoringProfileRuntimeStatus
from trader.application.ports.runtime_status import InputQualityStatus, SupplyFunnel
from trader.application.runtime.cadence import CadencePlannerStatus
from trader.application.runtime.runtime_issues import RuntimeIssue
from trader.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.infra.deepseek.reviewer import DeepSeekReviewer


def runtime_status(
    scheduler: SchedulerRuntime,
    reviewer: DeepSeekReviewer,
    market_health: Callable[[], Mapping[str, object]],
    tomorrow_model: ScoringProfileRuntimeStatus | None = None,
) -> dict[str, object]:
    status = scheduler.status()
    deepseek = reviewer.status()
    raw_budget = deepseek.get("budget")
    deepseek_budget = (
        dict(raw_budget)
        if isinstance(raw_budget, Mapping)
        else {
            "available": False,
            "error": "budget_status_unavailable",
        }
    )
    try:
        market_data = dict(market_health())
    except (OSError, RuntimeError, TypeError, ValueError):
        market_data = {"status": "unavailable"}
    strategy_errors = dict(status.strategy_error_codes)
    recent_errors = [_runtime_issue_payload(issue) for issue in status.recent_errors]
    active_issues = [issue for issue in status.recent_errors if issue.recovery_status == "active"]
    observer = asdict(status.observer)
    observer_error = status.observer.last_error_code
    degraded_reasons = [
        f"{issue.strategy.value}:{issue.code}" if issue.strategy is not None else issue.code for issue in active_issues
    ]
    if observer_error:
        degraded_reasons.append(f"observer:{observer_error}")
    issue_count = len(active_issues) + int(bool(observer_error))
    health_level = (
        "error"
        if not status.running or any(issue.severity == "error" for issue in active_issues)
        else "degraded"
        if issue_count
        else "normal"
    )
    return {
        "status": "running" if status.running else "stopped",
        "runtime_started": status.running,
        "runtime_version": status.config_version,
        "phase": status.phase.value,
        "deepseek_budget": deepseek_budget,
        "deepseek": deepseek,
        "market_data": market_data,
        "tomorrow_model": _tomorrow_model_payload(tomorrow_model),
        "company_research": asdict(status.company_research),
        "degraded_reasons": degraded_reasons,
        "health": {"level": health_level, "issue_count": issue_count},
        "recent_errors": recent_errors,
        "last_error": status.last_error_code or (f"observer:{observer_error}" if observer_error else None),
        "observer": observer,
        "scheduler": {
            "config_version": status.config_version,
            "lanes": [asdict(lane) for lane in status.lanes],
            "hybrid_lanes": [asdict(lane) for lane in status.hybrid_lanes],
            "task_lanes": [asdict(lane) for lane in status.task_lanes],
            "cadence": _cadence_payload(status.cadence),
            "control": {
                "running": status.control_running,
                "inflight": status.control_inflight,
                "rejected_count": status.control_rejected_count,
            },
            "strategy_errors": strategy_errors,
            "last_error_code": status.last_error_code,
            "refresh_failure_count": status.refresh_failure_count,
            "decision_failure_count": status.decision_failure_count,
            "review_failure_count": status.review_failure_count,
            "overlay_publish_count": status.overlay_publish_count,
            "overlay_failure_count": status.overlay_failure_count,
            "local_publish_count": status.local_publish_count,
            "hybrid_publish_count": status.hybrid_publish_count,
            "freeze_completed_count": status.freeze_completed_count,
            "freeze_failure_count": status.freeze_failure_count,
            "settlement_completed_count": status.settlement_completed_count,
            "settlement_failure_count": status.settlement_failure_count,
            "input_quality": input_quality_payload(status.input_quality),
        },
    }


def _tomorrow_model_payload(status: ScoringProfileRuntimeStatus | None) -> dict[str, object]:
    if status is None:
        return {"active": False, "status": "not_configured"}
    return {
        "active": status.active,
        "profile_id": status.profile_id,
        "model_id": status.model_id,
        "model_hash": status.model_hash,
        "scoring_version": status.scoring_version,
        "activation_basis": status.activation_basis,
        "historical_status": status.historical_status,
        "historical_failure_reasons": list(status.historical_failure_reasons),
        "monitoring_mode": status.monitoring_mode,
        "automatic_model_update": status.automatic_model_update,
        "loss_probability_status": status.loss_probability_status,
        "training_anchor": status.training_anchor,
        "runtime_anchor": status.runtime_anchor,
        "point_in_time_parity": status.point_in_time_parity,
    }


def _cadence_payload(status: CadencePlannerStatus) -> dict[str, object]:
    return {
        "started_at": status.started_at.isoformat() if status.started_at is not None else None,
        "intervals": {
            task.value: {band.value: seconds for band, seconds in values.items()}
            for task, values in status.intervals.items()
        },
        "next_due": [
            {
                "trade_date": trade_date,
                "band": band.value,
                "task": task.value,
                "due_at": due.isoformat(),
            }
            for (trade_date, band, task), due in sorted(
                status.next_due.items(),
                key=lambda item: (item[0][0], item[0][1].value, item[0][2].value),
            )
        ],
        "schedule_points": [
            {
                "trade_date": key.trade_date,
                "schedule_point": key.schedule_point.value,
                "strategy": key.strategy,
                "status": state.status.value,
                "attempt_count": state.attempt_count,
                "updated_at": state.updated_at.isoformat(),
                "next_retry_at": state.next_retry_at.isoformat() if state.next_retry_at is not None else None,
            }
            for key, state in sorted(
                status.schedule_points.items(),
                key=lambda item: (item[0].trade_date, item[0].schedule_point.value, item[0].strategy),
            )
        ],
    }


def input_quality_payload(statuses: tuple[InputQualityStatus, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for status in statuses:
        summary = status.summary
        result[status.strategy.value] = {
            "status": status.status,
            "publishable": status.publishable,
            "population_count": status.population_count,
            "candidate_count": status.candidate_count,
            "candidate_feature_count": status.candidate_feature_count,
            "population_rejected_count": status.population_rejected_count,
            "candidate_rejected_count": status.candidate_rejected_count,
            "candidate_scored_count": status.candidate_scored_count,
            "security_master_covered_count": status.security_master_covered_count,
            "history_covered_count": status.history_covered_count,
            "history_required_sessions": status.history_required_sessions,
            "candidate_feature_coverage_ratio": status.candidate_feature_coverage_ratio,
            "security_master_coverage_ratio": status.security_master_coverage_ratio,
            "history_coverage_ratio": status.history_coverage_ratio,
            "population_filter_reason_counts": dict(status.population_filter_reason_counts),
            "candidate_filter_reason_counts": dict(status.candidate_filter_reason_counts),
            "candidate_transient_reason_counts": dict(status.candidate_transient_reason_counts),
            "candidate_optional_reason_counts": dict(status.candidate_optional_reason_counts),
            "degraded_reasons": list(status.degraded_reasons),
            "supply_funnel": _supply_funnel_payload(status.supply_funnel),
            "summary": {
                "trade_date": summary.trade_date.isoformat(),
                "quote_total_count": summary.quote_total_count,
                "quote_covered_count": summary.quote_covered_count,
                "quote_missing_count": summary.quote_missing_count,
                "security_identity_missing_count": summary.security_identity_missing_count,
                "latest_quote_source": summary.latest_quote_source,
                "latest_quote_source_time": (
                    summary.latest_quote_source_time.isoformat()
                    if summary.latest_quote_source_time is not None
                    else None
                ),
                "highest_final_score": summary.highest_final_score,
            },
            "supply_reason_counts": dict(status.supply_reason_counts),
            "primary_blocker": status.primary_blocker,
        }
    return result


def _supply_funnel_payload(funnel: SupplyFunnel) -> dict[str, int]:
    return {
        "requested_candidates": funnel.requested_candidates,
        "candidate_features": funnel.candidate_features,
        "security_master": funnel.security_master,
        "history": funnel.history,
        "filter_pass": funnel.filter_pass,
        "filter_observe": funnel.filter_observe,
        "filter_reject": funnel.filter_reject,
        "full_scored": funnel.full_scored,
        "review_eligible": funnel.review_eligible,
        "observation_threshold_met_count": funnel.observation_threshold_met_count,
        "executable_threshold_met_count": funnel.executable_threshold_met_count,
        "action_executable": funnel.action_executable,
        "action_observe": funnel.action_observe,
        "action_unavailable": funnel.action_unavailable,
        "selected_executable": funnel.selected_executable,
        "selected_observe": funnel.selected_observe,
    }


def _runtime_issue_payload(issue: RuntimeIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "severity": issue.severity,
        "strategy": issue.strategy.value if issue.strategy is not None else None,
        "stage": issue.stage,
        "occurred_at": issue.occurred_at.isoformat(),
        "last_occurred_at": issue.last_occurred_at.isoformat(),
        "count": issue.count,
        "recovery_status": issue.recovery_status,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at is not None else None,
    }

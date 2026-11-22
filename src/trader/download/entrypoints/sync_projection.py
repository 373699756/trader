"""Public summary projection for the explicit sync command."""

from typing import Literal

from trader.download.application.company_round_progress import IsCompanyNewsReference
from trader.download.application.progress_message import ProgressState
from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.entrypoints.history_maintenance_projection import ProjectHistoryMaintenanceStatus


def _status(result: SyncResult) -> Literal["cancelled", "failed", "completed", "pending"]:
    if result.cancelled:
        return "cancelled"
    if result.failure is not None or any(task.status == "failed" for task in result.tasks):
        return "failed"
    return "completed" if result.exit_code == 0 else "pending"


def ProjectSyncResult(result: SyncResult) -> dict[str, object]:
    return {
        "status": _status(result),
        "failure": result.failure,
        "tasks": [_task(task) for task in result.tasks],
    }


def ProjectSyncTerminalState(result: SyncResult) -> ProgressState:
    """Distinguish a stopped company round from progress still waiting on work."""
    status = _status(result)
    if status == "cancelled":
        return "取消"
    if status == "failed":
        return "失败"
    if status == "completed":
        return "完成"
    companies = tuple(task.company for task in result.tasks if task.company is not None and task.status == "pending")
    if not companies:
        return "等待"
    steps = tuple(family.result for company in companies for family in company.families)
    if any(step.failure == "source_attempt_unconfirmed" for step in steps):
        return "阻塞"
    if any(company.budget_exhausted for company in companies):
        return "暂停"
    if not steps or any(step.can_continue or step.failure or step.pending_count is None for step in steps):
        return "待继续"
    if (
        all(task.status == "completed" or task.company is not None for task in result.tasks)
        and all(company.processing_complete for company in companies)
        and all(family.result.business_complete or IsCompanyNewsReference(family)
                for company in companies for family in company.families)
    ):
        return "采集结束"
    return "待补证"


def _task(task: SyncTaskResult) -> dict[str, object]:
    summary: dict[str, object] = {"task": task.task, "status": task.status, "reason": task.reason}
    if task.incompatible_company_store is not None:
        summary["incompatible_company_store"] = task.incompatible_company_store.value
    if task.history is not None:
        summary["history"] = ProjectHistoryMaintenanceStatus(task.history)
    if task.company is not None:
        company = task.company
        summary.update(
            {
                "round_id": company.round_id,
                "observed_at": company.scope.cutoff.isoformat(),
                "requested_codes": len(company.scope.codes),
                "processing_complete": company.processing_complete,
                "business_complete": company.business_complete,
                "paused": company.paused,
                "budget_exhausted": company.budget_exhausted,
                "publication_hash": company.publication_hash,
                "families": [
                    {
                        "family": item.family.value,
                        "steps": item.steps,
                        "accepted_inputs": item.accepted_inputs,
                        "request_slots_consumed": item.source_calls,
                        "unknown_attempts": item.unknown_attempts,
                        "observed_attempts": item.source_calls - item.unknown_attempts
                        if item.source_calls is not None and item.unknown_attempts is not None else None,
                        "processing_complete": item.result.processed_scope_complete,
                        "business_complete": item.result.business_complete,
                        "can_continue": item.result.can_continue,
                        "pending_count": item.result.pending_count,
                        "changed_codes": len(item.result.changed_codes),
                        "gaps": item.result.gaps,
                        "failure": item.result.failure,
                    }
                    for item in company.families
                ],
            }
        )
    return summary


__all__ = [
    "ProjectSyncResult",
    "ProjectSyncTerminalState",
]

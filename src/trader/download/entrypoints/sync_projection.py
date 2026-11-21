"""Public summary projection for the explicit sync command."""

from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.entrypoints.history_maintenance_projection import ProjectHistoryMaintenanceStatus


def ProjectSyncResult(result: SyncResult) -> dict[str, object]:
    failed = result.failure is not None or any(task.status == "failed" for task in result.tasks)
    return {
        "status": "cancelled"
        if result.cancelled
        else "failed"
        if failed
        else "completed"
        if result.exit_code == 0
        else "pending",
        "failure": result.failure,
        "tasks": [_task(task) for task in result.tasks],
    }


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


__all__ = ["ProjectSyncResult"]

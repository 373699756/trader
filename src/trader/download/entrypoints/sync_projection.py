"""Public summary projection for the explicit sync command."""

from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.entrypoints.history_maintenance_projection import ProjectHistoryMaintenanceStatus


def ProjectSyncResult(result: SyncResult) -> dict[str, object]:
    failed = result.failure is not None or any(task.status == "failed" for task in result.tasks)
    return {
        "schema_version": "data_sync",
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
    if task.history is not None:
        summary["history"] = ProjectHistoryMaintenanceStatus(task.history)
    if task.market is not None:
        market = task.market
        summary.update(
            {
                "target_date": market.target_date.isoformat() if market.target_date else None,
                "completed_codes": market.completed_codes,
                "pending_codes": market.pending_codes,
                "no_trade_data_codes": market.no_trade_data_codes,
                "latest_missing_codes": market.latest_missing_codes,
                "changed_files": market.changed_files,
                "changed_rows": market.changed_rows,
                "skipped_codes": market.skipped_codes,
                "failure_reason": market.failure_reason,
                "pending_reasons": [{"reason": item.reason, "count": item.count} for item in market.pending_reasons],
            }
        )
    if task.company is not None:
        company = task.company
        summary.update(
            {
                "observed_at": company.observed_at.isoformat(),
                "window_days": 365,
                "requested_codes": company.requested_codes,
                "published_codes": company.published_codes,
                "fetched_pages": company.fetched_pages,
                "changed_rows": company.changed_rows,
                "reused_partitions": company.reused_partitions,
                "pending_partitions": company.pending_partitions,
                "pending_documents": company.pending_documents,
                "news_population_codes": company.news_population_codes,
                "news_coverage": company.news_coverage,
                "failures": company.failures,
                "elapsed_seconds": round(company.elapsed_seconds, 3),
            }
        )
    return summary


__all__ = ["ProjectSyncResult"]

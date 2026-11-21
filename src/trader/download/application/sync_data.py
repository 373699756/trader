"""Sequential data synchronization with independent results and one prepared population."""

from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from typing import Literal

from trader.download.domain.company_sync import CompanySyncRoundResult
from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError, CompanyDownloadStore
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.application.download_failure import DownloadFailurePort
from trader.download.domain.download_failure import DownloadFailureContext, DownloadValidationFailure

SyncTarget = Literal["all", "history", "company"]


class SyncDatabaseError(RuntimeError):
    """Database failure translated by a synchronization adapter."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _failure_category(error: BaseException) -> str:
    if isinstance(error, DownloadValidationFailure):
        return str(error)
    return error.category if isinstance(error, SyncDatabaseError) else type(error).__name__


@dataclass(frozen=True)
class SyncTaskResult:
    task: Literal["history", "company"]
    status: Literal["completed", "pending", "failed", "cancelled"]
    reason: str | None = None
    company: CompanySyncRoundResult | None = None
    history: HistoryMaintenanceStatus | None = None
    incompatible_company_store: CompanyDownloadStore | None = None


@dataclass(frozen=True)
class SyncResult:
    tasks: tuple[SyncTaskResult, ...]
    failure: str | None = None
    cancelled: bool = False

    @property
    def exit_code(self) -> int:
        if self.cancelled:
            return 130
        return 1 if self.failure or any(task.status != "completed" for task in self.tasks) else 0


@dataclass(frozen=True)
class SyncData:
    prepare: Callable[[], PreparedReference]
    history: Callable[[PreparedReference], HistoryMaintenanceStatus]
    company: Callable[[PreparedReference], CompanySyncRoundResult]
    cancelled: Callable[[], bool]
    failures: DownloadFailurePort | None = None

    def execute(self, target: SyncTarget) -> SyncResult:
        if target not in ("all", "history", "company"):
            raise ValueError("sync_target_invalid")
        tasks: list[SyncTaskResult] = []
        try:
            if self.cancelled():
                return SyncResult((), cancelled=True)
            try:
                reference = self.prepare()
                if not reference.eligible_universe:
                    raise ValueError("sync_population_empty")
            except (CancelledError, OSError, RuntimeError, ValueError) as exc:
                if self.cancelled():
                    return SyncResult((), cancelled=True)
                if self.failures is not None:
                    self.failures(DownloadFailureContext("reference_preparation"), exc)
                return SyncResult((), failure=f"preparation_{_failure_category(exc)}")
            selected: tuple[Literal["history", "company"], ...] = (
                ("history", "company") if target == "all" else (target,)
            )
            for task in selected:
                if self.cancelled():
                    return SyncResult(tuple(tasks), cancelled=True)
                try:
                    result = self._execute_task(task, reference)
                    tasks.append(result)
                    if result.status == "cancelled":
                        return SyncResult(tuple(tasks), cancelled=True)
                except CompanyDatabaseIncompatibleError as exc:
                    if self.cancelled():
                        tasks.append(SyncTaskResult(task, "cancelled", incompatible_company_store=exc.store))
                        return SyncResult(tuple(tasks), cancelled=True)
                    tasks.append(SyncTaskResult(task, "failed", "CompanyDatabaseIncompatibleError",
                                               incompatible_company_store=exc.store))
                except (CancelledError, OSError, RuntimeError, ValueError) as exc:
                    if self.cancelled():
                        tasks.append(SyncTaskResult(task, "cancelled"))
                        return SyncResult(tuple(tasks), cancelled=True)
                    tasks.append(SyncTaskResult(task, "failed", _failure_category(exc)))
            return SyncResult(tuple(tasks))
        except KeyboardInterrupt:
            return SyncResult(tuple(tasks), cancelled=True)

    def _execute_task(
        self, task: Literal["history", "company"], reference: PreparedReference
    ) -> SyncTaskResult:
        if task == "history":
            history = self.history(reference)
            stopped = history.state == "cancelled" or self.cancelled()
            ready = not history.needs_retry
            return SyncTaskResult(
                task, "cancelled" if stopped else "failed" if history.state in {"failed", "blocked"}
                else "completed" if ready else "pending", reason=history.reason, history=history
            )
        company = self.company(reference)
        stopped = self.cancelled() or company.cancelled
        return SyncTaskResult(
            task, "cancelled" if stopped else "pending" if company.needs_retry else "completed",
            reason="task:cancelled" if stopped else _company_reason(company), company=company,
        )


def _company_reason(result: CompanySyncRoundResult) -> str | None:
    if result.business_complete:
        return None
    if result.budget_exhausted:
        return "company_round_budget_exhausted"
    for family in result.families:
        if family.result.failure is not None:
            return f"{family.family.value}:{family.result.failure}"
        if family.result.gaps:
            return f"{family.family.value}:{family.result.gaps[0]}"
        if family.result.pending_count is None:
            return f"{family.family.value}:inventory_not_checked"
        if family.result.pending_count:
            return f"{family.family.value}:pending"
        if not family.result.processed_scope_complete:
            return f"{family.family.value}:incomplete"
    return "company_round_incomplete"


__all__ = ["SyncData", "SyncDatabaseError", "SyncResult", "SyncTarget", "SyncTaskResult"]

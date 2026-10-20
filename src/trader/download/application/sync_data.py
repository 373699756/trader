"""Sequential data synchronization with independent results and one prepared population."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.qfq_window import QfqUpdateResult

SyncTarget = Literal["all", "history", "market", "company"]


@dataclass(frozen=True)
class SyncTaskResult:
    task: Literal["history", "market", "company"]
    status: Literal["completed", "pending", "failed", "cancelled"]
    reason: str | None = None
    market: QfqUpdateResult | None = None
    company: CompanyEvidenceSyncResult | None = None
    history: HistoryMaintenanceStatus | None = None


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
    market: Callable[[PreparedReference], QfqUpdateResult]
    company: Callable[[PreparedReference], CompanyEvidenceSyncResult]
    cancelled: Callable[[], bool]

    def execute(self, target: SyncTarget) -> SyncResult:
        if target not in ("all", "history", "market", "company"):
            raise ValueError("sync_target_invalid")
        tasks: list[SyncTaskResult] = []
        try:
            if self.cancelled():
                return SyncResult((), cancelled=True)
            try:
                reference = self.prepare()
                if not reference.eligible_universe:
                    raise ValueError("sync_population_empty")
            except (OSError, RuntimeError, ValueError) as exc:
                return SyncResult((), failure=f"preparation_{type(exc).__name__}", cancelled=self.cancelled())
            selected: tuple[Literal["history", "market", "company"], ...] = (
                ("history", "market", "company") if target == "all" else (target,)
            )
            for task in selected:
                if self.cancelled():
                    return SyncResult(tuple(tasks), cancelled=True)
                try:
                    result = self._execute_task(task, reference)
                    tasks.append(result)
                    if result.status == "cancelled":
                        return SyncResult(tuple(tasks), cancelled=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    tasks.append(SyncTaskResult(task, "failed", type(exc).__name__))
                    if self.cancelled():
                        return SyncResult(tuple(tasks), cancelled=True)
            return SyncResult(tuple(tasks))
        except KeyboardInterrupt:
            return SyncResult(tuple(tasks), cancelled=True)

    def _execute_task(
        self, task: Literal["history", "market", "company"], reference: PreparedReference
    ) -> SyncTaskResult:
        if task == "history":
            history = self.history(reference)
            stopped = history.state == "cancelled" or self.cancelled()
            ready = (
                history.state in {"completed", "already_current"} and history.training_due_reason != "data_incomplete"
            )
            return SyncTaskResult(
                task, "cancelled" if stopped else "completed" if ready else "pending", history=history
            )
        if task == "market":
            market = self.market(reference)
            stopped = market.failure_reason == "cancelled" or self.cancelled()
            return SyncTaskResult(
                task, "cancelled" if stopped else "pending" if market.needs_retry else "completed", market=market
            )
        company = self.company(reference)
        stopped = self.cancelled() or "task:cancelled" in company.failures
        return SyncTaskResult(
            task, "cancelled" if stopped else "pending" if company.needs_retry else "completed", company=company
        )


__all__ = ["SyncData", "SyncResult", "SyncTarget", "SyncTaskResult"]

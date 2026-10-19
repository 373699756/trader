"""Sequential data synchronization with independent results and one prepared population."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.qfq_window import QfqUpdateResult

SyncTarget = Literal["all", "market", "company"]


@dataclass(frozen=True)
class SyncTaskResult:
    task: Literal["market", "company"]
    status: Literal["completed", "pending", "failed", "cancelled"]
    reason: str | None = None
    market: QfqUpdateResult | None = None
    company: CompanyEvidenceSyncResult | None = None


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
    prepare: Callable[[], tuple[BaoStockSecurity, ...]]
    market: Callable[[tuple[BaoStockSecurity, ...]], QfqUpdateResult]
    company: Callable[[tuple[BaoStockSecurity, ...]], CompanyEvidenceSyncResult]
    cancelled: Callable[[], bool]

    def execute(self, target: SyncTarget) -> SyncResult:
        if target not in ("all", "market", "company"):
            raise ValueError("sync_target_invalid")
        tasks: list[SyncTaskResult] = []
        try:
            if self.cancelled():
                return SyncResult((), cancelled=True)
            try:
                population = self.prepare()
                if not population:
                    raise ValueError("sync_population_empty")
            except (OSError, RuntimeError, ValueError) as exc:
                return SyncResult((), failure=f"preparation_{type(exc).__name__}", cancelled=self.cancelled())
            selected: tuple[Literal["market", "company"], ...] = ("market", "company") if target == "all" else (target,)
            for task in selected:
                if self.cancelled():
                    return SyncResult(tuple(tasks), cancelled=True)
                try:
                    if task == "market":
                        market = self.market(population)
                        stopped = market.failure_reason == "cancelled" or self.cancelled()
                        tasks.append(
                            SyncTaskResult(
                                task,
                                "cancelled" if stopped else "pending" if market.needs_retry else "completed",
                                market=market,
                            )
                        )
                    else:
                        company = self.company(population)
                        stopped = self.cancelled() or "cancelled" in company.failures
                        tasks.append(
                            SyncTaskResult(
                                task,
                                "cancelled" if stopped else "pending" if company.needs_retry else "completed",
                                company=company,
                            )
                        )
                    if stopped:
                        return SyncResult(tuple(tasks), cancelled=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    tasks.append(SyncTaskResult(task, "failed", type(exc).__name__))
                    if self.cancelled():
                        return SyncResult(tuple(tasks), cancelled=True)
            return SyncResult(tuple(tasks))
        except KeyboardInterrupt:
            return SyncResult(tuple(tasks), cancelled=True)


__all__ = ["SyncData", "SyncResult", "SyncTarget", "SyncTaskResult"]

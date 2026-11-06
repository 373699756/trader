"""Prepare one immutable reference cut through explicitly injected supplier ports."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Protocol

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.prepared_reference import PreparedReference, SelectEligibleSecurities


class ReferenceDatabaseIncompatibleError(ValueError):
    """The reference repository does not satisfy the current storage contract."""


class IndustryDatabaseIncompatibleError(ValueError):
    """Only the industry download is incompatible with its current contract."""


class IndustryVersionPort(Protocol):
    @property
    def data_version(self) -> str: ...

    @property
    def received_at(self) -> datetime: ...


@dataclass(frozen=True)
class ReferencePreparationDependencies:
    st_evidence: Callable[[tuple[BaoStockSecurity, ...], date], tuple[HistoryStEvidence, ...]]
    calendar: Callable[[], tuple[date, ...]]
    industries: Callable[[datetime, tuple[BaoStockSecurity, ...], tuple[date, ...]], tuple[IndustryVersionPort, ...]]
    publish: Callable[[PreparedReference, tuple[HistoryStEvidence, ...]], object]
    cancelled: Callable[[], bool]


def PrepareReference(
    universe: tuple[BaoStockSecurity, ...],
    observed_at: datetime,
    dependencies: ReferencePreparationDependencies,
    *,
    report: Callable[[ProgressMessage], None] | None = None,
) -> PreparedReference:
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    source_evidence = dependencies.st_evidence(universe, observed_at.date())
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    population = tuple(sorted(universe, key=lambda item: item.code))
    evidence = tuple(
        replace(item, checked_on=observed_at.date()) for item in sorted(source_evidence, key=lambda item: item.code)
    )
    eligible = SelectEligibleSecurities(population, evidence)
    if not eligible:
        raise ValueError("sync_population_empty")
    calendar = dependencies.calendar()
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    _report(report, "当前行业", "开始准备，完成后继续下载")
    try:
        industries = dependencies.industries(observed_at, eligible, calendar)
    except IndustryDatabaseIncompatibleError:
        industries = ()
        _report(
            report, "当前行业",
            "行业下载库结构不兼容；仅清理 market/reference/industry.sqlite3 及配套文件后重新下载行业资料，其他资料同步继续",
            ProgressLevel.WARNING,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        industries = ()
        _report(report, "当前行业", f"准备失败（{type(exc).__name__}），继续资料同步", ProgressLevel.WARNING)
    else:
        missing = len(eligible) - len(industries)
        _report(
            report,
            "当前行业",
            f"准备完成 {len(industries)}/{len(eligible)} 股，缺分类 {missing} 股，继续资料同步" if industries else "行业资料不可用，继续资料同步",
            ProgressLevel.SUCCESS if industries and missing == 0 else ProgressLevel.WARNING,
        )
    completed_at = max(observed_at, *(item.received_at for item in industries)) if industries else observed_at
    reference = PreparedReference(
        population,
        evidence,
        completed_at,
        calendar,
        industries[0].data_version if industries else None,
    )
    if dependencies.cancelled():
        raise RuntimeError("reference_preparation_cancelled")
    _report(report, "参考发布", "开始保存证券、ST资格及交易日历")
    dependencies.publish(reference, source_evidence)
    _report(report, "参考发布", "完成", ProgressLevel.SUCCESS)
    return reference


def _report(
    report: Callable[[ProgressMessage], None] | None,
    stage: str,
    details: str,
    level: ProgressLevel = ProgressLevel.NORMAL,
) -> None:
    if report is not None:
        report(ProgressMessage((ProgressField(stage), ProgressField(details, level))))


__all__ = [
    "IndustryDatabaseIncompatibleError",
    "IndustryVersionPort", "PrepareReference", "ReferenceDatabaseIncompatibleError", "ReferencePreparationDependencies",
]

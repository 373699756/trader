"""Immutable company evidence and resumable date-partition contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

Market = Literal["sse", "szse", "bse"]
Component = Literal["announcements", "financial", "pledge", "unlock", "news"]
EvidenceProgressState = Literal["running", "complete", "reused", "deferred"]


class CompanySourceFailure(RuntimeError):
    def __init__(
        self, category: Literal["access_denied", "rate_limited", "http_error"], *, status_code: int | None = None
    ) -> None:
        self.category = category
        self.status_code = status_code
        super().__init__(category)


@dataclass(frozen=True)
class CompanyEvidenceRequest:
    codes: tuple[str, ...]
    observed_at: datetime
    news_codes: tuple[str, ...] = ()
    budget_seconds: float = 3480.0

    def __post_init__(self) -> None:
        if self.observed_at.utcoffset() is None or not math.isfinite(self.budget_seconds) or self.budget_seconds <= 0:
            raise ValueError("company_evidence_request_invalid")


@dataclass(frozen=True)
class StructuredCompanyQuery:
    component: Literal["financial", "pledge", "unlock"]
    start: date
    end: date
    page: int = 1
    expected_rows: int | None = None
    received_rows: int = 0

    def __post_init__(self) -> None:
        if (
            self.component not in {"financial", "pledge", "unlock"}
            or self.start > self.end
            or self.page < 1
            or self.received_rows < 0
            or (self.expected_rows is not None and self.expected_rows < self.received_rows)
        ):
            raise ValueError("structured_query_invalid")


@dataclass(frozen=True)
class Disclosure:
    source: str
    identity: str
    codes: tuple[str, ...]
    title: str
    published_at: datetime
    url: str
    digest: str
    kind: Literal["announcement", "news"] = "announcement"

    def __post_init__(self) -> None:
        if (
            not self.source
            or not self.identity
            or not self.title
            or not self.codes
            or self.published_at.utcoffset() is None
            or self.kind not in {"announcement", "news"}
        ):
            raise ValueError("company_disclosure_invalid")
        for code in self.codes:
            EvidenceMarket(code)


@dataclass(frozen=True)
class DisclosurePartition:
    market: Market
    start: date
    end: date
    page: int = 1
    expected_rows: int | None = None
    received_rows: int = 0

    def __post_init__(self) -> None:
        if self.start > self.end or self.page < 1 or self.received_rows < 0:
            raise ValueError("invalid disclosure partition")


@dataclass(frozen=True)
class DisclosurePage:
    rows: tuple[Disclosure, ...]
    total: int
    has_more: bool


@dataclass(frozen=True)
class CompanyFinancial:
    code: str
    report_date: date
    published_at: datetime
    basic_eps: float | None
    book_value_per_share: float | None
    revenue_growth_pct: float | None
    net_profit_growth_pct: float | None
    core_profit_growth_pct: float | None
    roe_pct: float | None
    parent_net_profit: float | None
    core_net_profit: float | None

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if self.published_at.utcoffset() is None or any(
            value is not None and (isinstance(value, bool) or not math.isfinite(value))
            for value in (
                self.basic_eps,
                self.book_value_per_share,
                self.revenue_growth_pct,
                self.net_profit_growth_pct,
                self.core_profit_growth_pct,
                self.roe_pct,
                self.parent_net_profit,
                self.core_net_profit,
            )
        ):
            raise ValueError("company_financial_invalid")


@dataclass(frozen=True)
class CompanyRatio:
    code: str
    day: date
    ratio_pct: float

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if not math.isfinite(self.ratio_pct) or not 0 <= self.ratio_pct <= 100:
            raise ValueError("company_ratio_invalid")


@dataclass(frozen=True)
class CompanyDocument:
    disclosure: Disclosure
    text: str
    checked: bool


@dataclass(frozen=True)
class CompanyEvidenceSnapshot:
    code: str
    published_at: datetime
    window_start: date
    window_end: date
    version: str
    announcements: tuple[CompanyDocument, ...]
    announcement_complete: bool
    financials: tuple[CompanyFinancial, ...]
    pledge: CompanyRatio | None
    unlocks: tuple[CompanyRatio, ...]
    unlock_checked: bool
    news: tuple[Disclosure, ...]
    news_checked: bool
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if (
            self.published_at.utcoffset() is None
            or self.window_start > self.window_end
            or any(
                type(flag) is not bool for flag in (self.announcement_complete, self.unlock_checked, self.news_checked)
            )
            or any(not isinstance(item, str) for item in self.failures)
        ):
            raise ValueError("company_snapshot_invalid")


@dataclass(frozen=True)
class StructuredCompanyPage:
    component: Literal["financial", "pledge", "unlock"]
    page: int
    pages: int
    total: int
    financials: tuple[CompanyFinancial, ...] = ()
    ratios: tuple[CompanyRatio, ...] = ()
    raw_count: int | None = None

    def __post_init__(self) -> None:
        if (
            self.component not in {"financial", "pledge", "unlock"}
            or not 1 <= self.page <= self.pages
            or self.total < 0
            or not len(self.financials) + len(self.ratios) <= self.row_count <= self.total
        ):
            raise ValueError("structured_page_invalid")

    @property
    def row_count(self) -> int:
        return len(self.financials) + len(self.ratios) if self.raw_count is None else self.raw_count


@dataclass(frozen=True)
class EvidenceSyncProgress:
    component: str
    completed: int
    pending: int | None
    fetched_pages: int
    changed_rows: int
    reused_partitions: int
    failure: str = ""
    status: EvidenceProgressState = "running"
    scope: DisclosurePartition | StructuredCompanyQuery | None = None
    pending_documents: int = 0


@dataclass(frozen=True)
class CompanyEvidenceSyncResult:
    observed_at: datetime
    requested_codes: int
    published_codes: int
    fetched_pages: int
    changed_rows: int
    reused_partitions: int
    pending_partitions: int
    failures: tuple[str, ...]
    elapsed_seconds: float
    pending_documents: int = 0
    news_population_codes: int = 0
    news_coverage: Literal["published_population", "uncovered"] = "uncovered"

    @property
    def needs_retry(self) -> bool:
        return bool(self.failures or self.pending_partitions or self.pending_documents)


def EvidenceMarket(code: str) -> Market:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("invalid stock code")
    return "sse" if code.startswith("6") else "bse" if code.startswith(("4", "8", "9")) else "szse"


def DisclosureMonths(start: date, end: date) -> tuple[tuple[date, date], ...]:
    ranges: list[tuple[date, date]] = []
    while start <= end:
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        stop = min(end, next_month - timedelta(days=1))
        ranges.append((start, stop))
        start = stop + timedelta(days=1)
    return tuple(ranges)


__all__ = [
    "CompanyEvidenceRequest",
    "StructuredCompanyQuery",
    "CompanyDocument",
    "CompanyEvidenceSnapshot",
    "CompanyEvidenceSyncResult",
    "CompanyFinancial",
    "CompanyRatio",
    "Component",
    "Disclosure",
    "DisclosureMonths",
    "DisclosurePage",
    "DisclosurePartition",
    "EvidenceMarket",
    "EvidenceSyncProgress",
    "EvidenceProgressState",
    "Market",
    "StructuredCompanyPage",
    "CompanySourceFailure",
]

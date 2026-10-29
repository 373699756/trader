"""Immutable company evidence and resumable date-partition contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.prepared_reference import PreparedReference

Market = Literal["sse", "szse", "bse"]
Component = Literal["announcements", "financial", "pledge", "unlock", "news"]
EvidenceProgressState = Literal["running", "complete", "reused", "deferred"]
DocumentRepairMode = Literal["ocr", "limits"]
DocumentRoute = Literal["standard", "ocr", "limits"]


@dataclass(frozen=True)
class CompanyDocumentLimits:
    max_bytes: int = 16 * 1024 * 1024
    max_pages: int = 200
    max_text_characters: int = 1_000_000

    def __post_init__(self) -> None:
        if not (
            1 <= self.max_bytes <= 64 * 1024 * 1024
            and 1 <= self.max_pages <= 1000
            and 80 <= self.max_text_characters <= 2_000_000
        ):
            raise ValueError("company_document_limits_invalid")


@dataclass(frozen=True)
class CompanySyncConfiguration:
    source_timeout_seconds: float
    document_parse_timeout_seconds: float
    request_interval_seconds: float
    document_failure_retry_seconds: float = 86400.0
    document_transient_retry_seconds: float = 900.0
    document_limits: CompanyDocumentLimits = CompanyDocumentLimits()
    extended_document_limits: CompanyDocumentLimits = CompanyDocumentLimits(64 * 1024 * 1024, 1000, 2_000_000)
    document_ocr_timeout_seconds: float = 120.0
    document_ocr_max_pages: int = 50
    document_ocr_dpi: int = 150
    document_ocr_languages: str = "chi_sim+eng"
    directory_batch_pages: int = 20
    document_batch_size: int = 50
    document_workers: int = 1

    def __post_init__(self) -> None:
        values = (
            self.source_timeout_seconds,
            self.document_parse_timeout_seconds,
            self.request_interval_seconds,
            self.document_failure_retry_seconds,
            self.document_transient_retry_seconds,
            self.document_ocr_timeout_seconds,
        )
        if (
            not 1 <= self.directory_batch_pages <= 100
            or not 1 <= self.document_batch_size <= 200
            or self.document_workers not in {1, 2}
            or not 1 <= self.document_ocr_max_pages <= 200
            or not 72 <= self.document_ocr_dpi <= 300
            or self.document_ocr_timeout_seconds > 600
            or not self.document_ocr_languages
            or any(not part.replace("_", "").isalnum() for part in self.document_ocr_languages.split("+"))
            or self.extended_document_limits.max_bytes < self.document_limits.max_bytes
            or self.extended_document_limits.max_pages < self.document_limits.max_pages
            or self.extended_document_limits.max_text_characters < self.document_limits.max_text_characters
        ):
            raise ValueError("company_batch_configuration_invalid")
        if (
            any(not math.isfinite(value) for value in values)
            or not 0 < self.source_timeout_seconds <= 8
            or self.document_parse_timeout_seconds <= 0
            or self.request_interval_seconds < 1
            or self.document_failure_retry_seconds <= 0
            or self.document_transient_retry_seconds <= 0
        ):
            raise ValueError("company_sync_configuration_invalid")


class CompanySourceFailure(RuntimeError):
    def __init__(
        self, category: Literal["access_denied", "rate_limited", "http_error"], *, status_code: int | None = None
    ) -> None:
        self.category = category
        self.status_code = status_code
        super().__init__(category)


class DisclosureCoverageMismatch(ValueError):
    def __init__(self, missing_rows: int, *, refresh_required: bool = False) -> None:
        self.missing_rows = missing_rows
        self.refresh_required = refresh_required
        super().__init__("cninfo_query_coverage_mismatch")


@dataclass(frozen=True)
class CompanyListing:
    code: str
    listed_on: date

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)


@dataclass(frozen=True)
class CompanyEvidenceRequest:
    codes: tuple[str, ...]
    observed_at: datetime
    configuration: CompanySyncConfiguration
    news_codes: tuple[str, ...] = ()
    stock_names: tuple[tuple[str, str], ...] = ()
    event_policy: CompanyEventPolicy = CompanyEventPolicy()
    listings: tuple[CompanyListing, ...] = ()
    document_repair: DocumentRepairMode | None = None

    @classmethod
    def from_reference(
        cls,
        reference: PreparedReference,
        observed_at: datetime,
        configuration: CompanySyncConfiguration,
        event_policy: CompanyEventPolicy,
        *,
        news_codes: tuple[str, ...] = (),
        document_repair: DocumentRepairMode | None = None,
    ) -> CompanyEvidenceRequest:
        population = reference.eligible_universe
        return cls(
            tuple(item.code for item in population),
            observed_at,
            configuration,
            news_codes,
            tuple((item.code, item.name) for item in population),
            event_policy,
            tuple(CompanyListing(item.code, item.listed_on) for item in population),
            document_repair,
        )

    def __post_init__(self) -> None:
        if self.observed_at.utcoffset() is None:
            raise ValueError("company_evidence_request_invalid")
        if len({item.code for item in self.listings}) != len(self.listings) or any(
            item.code not in self.codes or item.listed_on > self.observed_at.date() for item in self.listings
        ):
            raise ValueError("company_listing_identity_invalid")


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
class DisclosureSecurity:
    code: str
    org_id: str

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if not self.org_id or len(self.org_id) > 80 or any(c in self.org_id for c in ";,|\n\r"):
            raise ValueError("cninfo_security_invalid")


@dataclass(frozen=True)
class DisclosureQuery:
    plate: str
    securities: tuple[DisclosureSecurity, ...] = ()

    def __post_init__(self) -> None:
        if self.plate not in {"szmb", "szcy", "shmb", "shkcp", "bj"} or len(self.securities) > 32:
            raise ValueError("cninfo_query_invalid")
        if len({item.code for item in self.securities}) != len(self.securities):
            raise ValueError("cninfo_query_invalid")

    @property
    def key(self) -> str:
        return self.plate + "|" + ";".join(f"{item.code},{item.org_id}" for item in self.securities)


@dataclass(frozen=True)
class DisclosurePartition:
    market: Market
    start: date
    end: date
    page: int = 1
    expected_rows: int | None = None
    received_rows: int = 0
    query: DisclosureQuery | None = None
    page_size: int = 30
    filter_identity: str = "all_announcements"

    def __post_init__(self) -> None:
        if self.start > self.end or self.page < 1 or self.received_rows < 0:
            raise ValueError("invalid disclosure partition")
        if self.page_size not in {30, 50, 100} or self.filter_identity != "all_announcements":
            raise ValueError("company_query_contract_invalid")


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
class DocumentRetry:
    failure: str
    retry_after: datetime


@dataclass(frozen=True)
class CompanyDocumentExtraction:
    text: str
    method: Literal["text", "ocr"]
    payload_hash: bytes
    policy_id: str
    quality: float | None = None

    def __post_init__(self) -> None:
        if (
            len(self.text.strip()) < 80
            or len(self.payload_hash) != 32
            or not self.policy_id
            or (self.quality is not None and not 0 <= self.quality <= 100)
        ):
            raise ValueError("company_document_extraction_invalid")


class CompanyDocumentFailure(ValueError):
    def __init__(self, reason: str, *, payload_hash: bytes | None = None) -> None:
        if not reason.startswith("disclosure_document_") or (payload_hash is not None and len(payload_hash) != 32):
            raise ValueError("company_document_failure_invalid")
        self.reason = reason
        self.payload_hash = payload_hash
        super().__init__(reason)


@dataclass(frozen=True)
class CompanyDocumentPolicies:
    standard: str
    ocr: str
    limits: str

    def __post_init__(self) -> None:
        if not all((self.standard, self.ocr, self.limits)) or len({self.standard, self.ocr, self.limits}) != 3:
            raise ValueError("company_document_policies_invalid")

    def for_route(self, route: DocumentRoute) -> str:
        return {"standard": self.standard, "ocr": self.ocr, "limits": self.limits}[route]


@dataclass(frozen=True)
class CompanyDocumentTask:
    disclosure: Disclosure
    route: DocumentRoute


@dataclass(frozen=True)
class CompanyDocumentWorkload:
    pending: int
    due: int
    cooling: int = 0
    ocr: int = 0
    unextractable: int = 0
    next_retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if min(self.pending, self.due, self.cooling, self.ocr, self.unextractable) < 0 or self.due > self.pending:
            raise ValueError("company_document_workload_invalid")


@dataclass(frozen=True)
class CompanyEvidenceInput:
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
    source_checked_at: datetime | None = None
    unlock_checked_at: datetime | None = None
    unlock_coverage_end: date | None = None

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
class CompanyStockProgress:
    code: str
    name: str = ""
    processed: int = 0
    total: int = 0
    announcements: int = 0
    analyzed: int = 0
    pending: int = 0
    risks: int = 0
    positives: int = 0
    published: bool = False
    coverage_complete: bool = False
    reused: bool = False
    review_count: int = 0


@dataclass(frozen=True)
class CompanyDateProgress:
    window_start: date
    window_end: date
    completed_days: int

    def __post_init__(self) -> None:
        if self.window_start > self.window_end or not 0 <= self.completed_days <= self.total_days:
            raise ValueError("company_date_progress_invalid")

    @property
    def total_days(self) -> int:
        return (self.window_end - self.window_start).days + 1


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
    attempted_documents: int = 0
    deferred_documents: int = 0
    activity: Literal["download", "repair"] = "download"
    stock: CompanyStockProgress | None = None
    dates: CompanyDateProgress | None = None
    failed_documents: int = 0
    batch: int = 0
    known_pages: int = 0
    unknown_queries: int = 0
    document_rate: float | None = None
    document_eta_seconds: float | None = None
    ocr_documents: int = 0
    unextractable_documents: int = 0
    next_document_retry_at: datetime | None = None
    discovering_documents: bool = False


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
    ocr_documents: int = 0
    unextractable_documents: int = 0
    next_document_retry_at: datetime | None = None

    @property
    def needs_retry(self) -> bool:
        return bool(self.failures or self.pending_partitions or self.pending_documents)


_OCR_FAILURES = frozenset(
    {
        "disclosure_document_no_text",
        "disclosure_document_short_text",
        "disclosure_document_unreadable",
        "disclosure_document_ocr_unavailable",
        "disclosure_document_ocr_language_unavailable",
        "disclosure_document_ocr_timeout",
        "disclosure_document_ocr_unreadable",
        "disclosure_document_ocr_low_quality",
        "disclosure_document_ocr_page_limit",
        "disclosure_document_ocr_resource_limit",
    }
)
_LIMIT_FAILURES = frozenset(
    {
        "disclosure_document_oversized",
        "disclosure_document_page_limit",
        "disclosure_document_expanded_page_limit",
        "disclosure_document_text_limit",
    }
)
_TRANSIENT_FAILURES = frozenset(
    {
        "timeout",
        "HttpConnectionError",
        "http_408",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "disclosure_document_parse_timeout",
        "access_denied",
        "rate_limited",
    }
)


def CompanyDocumentRouteForFailure(failure: str) -> DocumentRoute:
    if failure in _OCR_FAILURES:
        return "ocr"
    if failure in _LIMIT_FAILURES:
        return "limits"
    return "standard"


def IsTransientCompanyDocumentFailure(failure: str) -> bool:
    return failure in _TRANSIENT_FAILURES


def CompanyDocumentFailureRules() -> tuple[tuple[str, DocumentRoute, bool], ...]:
    failures = _OCR_FAILURES | _LIMIT_FAILURES | _TRANSIENT_FAILURES
    return tuple(
        (failure, CompanyDocumentRouteForFailure(failure), IsTransientCompanyDocumentFailure(failure))
        for failure in sorted(failures)
    )


def BuildCompanyDocumentPolicies(
    configuration: CompanySyncConfiguration, ocr_identity: str
) -> CompanyDocumentPolicies:
    limits = configuration.document_limits
    expanded = configuration.extended_document_limits
    return CompanyDocumentPolicies(
        standard=(
            f"text_3:{configuration.document_parse_timeout_seconds:g}:"
            f"{limits.max_bytes}:{limits.max_pages}:{limits.max_text_characters}"
        ),
        ocr=(
            f"ocr_1:{ocr_identity}:{configuration.document_ocr_timeout_seconds:g}:"
            f"{configuration.document_ocr_max_pages}:{configuration.document_ocr_dpi}:"
            f"{configuration.document_ocr_languages}"
        ),
        limits=f"limits_1:{expanded.max_bytes}:{expanded.max_pages}:{expanded.max_text_characters}",
    )


def EvidenceMarket(code: str) -> Market:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("invalid stock code")
    return "sse" if code.startswith(("6", "900")) else "bse" if code.startswith(("4", "8", "9")) else "szse"


def DisclosureMonths(start: date, end: date) -> tuple[tuple[date, date], ...]:
    ranges: list[tuple[date, date]] = []
    while start <= end:
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        stop = min(end, next_month - timedelta(days=1))
        ranges.append((start, stop))
        start = stop + timedelta(days=1)
    return tuple(ranges)


__all__ = [
    "BuildCompanyDocumentPolicies",
    "CompanyDocumentExtraction",
    "CompanyDocumentFailure",
    "CompanyDocumentFailureRules",
    "CompanyDocumentPolicies",
    "CompanyDocumentRouteForFailure",
    "CompanyDocumentTask",
    "DisclosureCoverageMismatch",
    "DisclosureSecurity",
    "DisclosureQuery",
    "CompanySyncConfiguration",
    "CompanyDocumentLimits",
    "CompanyDocumentWorkload",
    "CompanyDateProgress",
    "CompanyEvidenceRequest",
    "CompanyListing",
    "StructuredCompanyQuery",
    "CompanyDocument",
    "DocumentRetry",
    "DocumentRepairMode",
    "DocumentRoute",
    "CompanyEvidenceInput",
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
    "IsTransientCompanyDocumentFailure",
    "StructuredCompanyPage",
    "CompanySourceFailure",
]

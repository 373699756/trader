"""Immutable company evidence and resumable date-partition contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from trader.download.domain.company_annual_income import CompanyAnnualIncome

from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.prepared_reference import PreparedReference

Market = Literal["sse", "szse", "bse"]
Component = Literal["announcements", "financial", "pledge", "unlock", "news"]
EvidenceProgressState = Literal["running", "complete", "reused", "deferred"]
DocumentRepairMode = Literal["ocr", "limits"]
DocumentRoute = Literal["standard", "ocr", "limits"]


@dataclass(frozen=True)
class CompanyDocumentLimits:
    max_bytes: int
    max_pages: int
    max_text_characters: int

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
    document_download_timeout_seconds: float
    document_parse_timeout_seconds: float
    request_interval_seconds: float
    document_failure_retry_seconds: float
    document_transient_retry_seconds: float
    document_limits: CompanyDocumentLimits
    extended_document_limits: CompanyDocumentLimits
    document_ocr_timeout_seconds: float
    document_ocr_page_timeout_seconds: float
    document_ocr_max_pages: int
    document_ocr_dpi: int
    document_ocr_languages: str
    temporary_text_capacity_bytes: int
    temporary_disk_reserve_bytes: int
    raw_capacity_bytes: int
    raw_analysis_reserve_bytes: int
    directory_batch_pages: int
    document_batch_size: int
    document_workers: int
    text_workers: int
    ocr_workers: int
    document_queue_limit: int
    publication_batch_size: int
    publication_interval_seconds: float
    text_task_reserve_bytes: int
    ocr_task_reserve_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.source_timeout_seconds,
            self.document_download_timeout_seconds,
            self.document_parse_timeout_seconds,
            self.request_interval_seconds,
            self.document_failure_retry_seconds,
            self.document_transient_retry_seconds,
            self.document_ocr_timeout_seconds,
            self.document_ocr_page_timeout_seconds,
            self.publication_interval_seconds,
        )
        if (
            not 1 <= self.directory_batch_pages <= 100
            or not 1 <= self.document_workers <= 8
            or not 1 <= self.text_workers <= 8
            or not 1 <= self.ocr_workers <= 4
            or not 1 <= self.document_batch_size <= self.document_queue_limit <= 128
            or not 1 <= self.publication_batch_size <= self.document_queue_limit
            or self.publication_interval_seconds <= 0
            or self.text_task_reserve_bytes < self.extended_document_limits.max_text_characters * 8
            or self.ocr_task_reserve_bytes < 300 * 1024 * 1024
            or self.text_workers * self.text_task_reserve_bytes + self.ocr_workers * self.ocr_task_reserve_bytes > self.raw_analysis_reserve_bytes
            or not 1 <= self.document_ocr_max_pages <= 200
            or not 72 <= self.document_ocr_dpi <= 300
            or not 0 < self.document_ocr_timeout_seconds <= 600
            or not 0 < self.document_ocr_page_timeout_seconds <= self.document_ocr_timeout_seconds
            or not self.document_ocr_languages
            or self.temporary_text_capacity_bytes < self.extended_document_limits.max_text_characters * 4
            or self.temporary_disk_reserve_bytes < 1
            or not self.temporary_text_capacity_bytes <= self.raw_analysis_reserve_bytes < self.raw_capacity_bytes
            or self.raw_analysis_reserve_bytes < 320 * 1024 * 1024
            or any(not part.replace("_", "").isalnum() for part in self.document_ocr_languages.split("+"))
            or self.extended_document_limits.max_bytes < self.document_limits.max_bytes
            or self.extended_document_limits.max_pages < self.document_limits.max_pages
            or self.extended_document_limits.max_text_characters < self.document_limits.max_text_characters
        ):
            raise ValueError("company_batch_configuration_invalid")
        if (
            any(not math.isfinite(value) for value in values)
            or not 0 < self.source_timeout_seconds <= 8
            or not self.source_timeout_seconds <= self.document_download_timeout_seconds <= 120
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
    event_policy: CompanyEventPolicy
    news_codes: tuple[str, ...] = ()
    stock_names: tuple[tuple[str, str], ...] = ()
    listings: tuple[CompanyListing, ...] = ()
    fill_missing: bool = False

    @classmethod
    def from_reference(
        cls,
        reference: PreparedReference,
        observed_at: datetime,
        configuration: CompanySyncConfiguration,
        event_policy: CompanyEventPolicy,
        *,
        news_codes: tuple[str, ...] = (),
        fill_missing: bool = False,
    ) -> CompanyEvidenceRequest:
        population = reference.eligible_universe
        return cls(
            codes=tuple(item.code for item in population),
            observed_at=observed_at,
            configuration=configuration,
            event_policy=event_policy,
            news_codes=news_codes,
            stock_names=tuple((item.code, item.name) for item in population),
            listings=tuple(CompanyListing(item.code, item.listed_on) for item in population),
            fill_missing=fill_missing,
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
class DisclosurePartitionRetry:
    retry_at: datetime
    failure: str


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
    terminal_failure: bool = False


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


@dataclass(frozen=True)
class CompanyOcrDiagnostic:
    phase: Literal["metadata", "render", "recognition", "quality"]
    page: int
    pages: int
    characters: int
    confidence: float | None
    segmentation_mode: int


class CompanyDocumentFailure(ValueError):
    def __init__(
        self, reason: str, *, payload_hash: bytes | None = None, diagnostic: CompanyOcrDiagnostic | None = None
    ) -> None:
        if not reason.startswith("disclosure_document_") or (payload_hash is not None and len(payload_hash) != 32):
            raise ValueError("company_document_failure_invalid")
        self.reason = reason
        self.payload_hash = payload_hash
        self.diagnostic = diagnostic
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
class CompanyCoverageWatermark:
    market: Market
    window_start: date
    discovered_day: date | None
    processed_day: date | None


@dataclass(frozen=True)
class CompanyDocumentWorkload:
    pending: int
    due: int
    cooling: int = 0
    ocr: int = 0
    unextractable: int = 0
    next_retry_at: datetime | None = None
    download_pending: int = 0
    local_pending: int = 0
    analyzed_pending: int = 0
    published_inputs: int = 0
    superseded_inputs: int = 0
    raw_bytes: int = 0
    watermarks: tuple[CompanyCoverageWatermark, ...] = ()
    unresolved_failures: int = 0

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
    superseded_evidence_ids: tuple[str, ...] = ()
    annual_incomes: tuple[CompanyAnnualIncome, ...] = ()

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


CompanyOperationKind = Literal[
    "prepare", "inventory", "directory", "download", "text", "ocr", "document_commit",
    "structured", "annual_income", "news", "publication", "collection_commit", "finish",
]


@dataclass(frozen=True)
class CompanyOperationProgress:
    """An operation boundary; execution includes pacing and is not physical HTTP time."""

    token: int
    kind: CompanyOperationKind
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    at: float
    code: str = ""
    name: str = ""
    title: str = ""
    source: str = "local"

    def __post_init__(self) -> None:
        if self.token < 1 or not math.isfinite(self.at) or self.at < 0 or not self.source.strip():
            raise ValueError("company_operation_progress_invalid")
        if self.code:
            EvidenceMarket(self.code)


@dataclass(frozen=True)
class CompanyPublicationProgress:
    """A real formal-publication boundary, independent of per-stock compilation."""

    state: Literal["started", "committed"]
    codes: tuple[str, ...]
    changed_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for code in self.codes:
            EvidenceMarket(code)
        if len(set(self.codes)) != len(self.codes) or not set(self.changed_codes).issubset(self.codes):
            raise ValueError("company_publication_progress_invalid")


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
    pending_documents: int | None = 0
    attempted_documents: int = 0
    deferred_documents: int | None = 0
    activity: Literal["download", "repair"] = "download"
    stock: CompanyStockProgress | None = None
    dates: CompanyDateProgress | None = None
    failed_documents: int = 0
    batch: int = 0
    known_pages: int = 0
    unknown_queries: int = 0
    document_rate: float | None = None
    document_eta_seconds: float | None = None
    ocr_documents: int | None = 0
    unextractable_documents: int | None = 0
    next_document_retry_at: datetime | None = None
    discovering_documents: bool = False
    workload: CompanyDocumentWorkload | None = None
    unresolved_document_failures: int = 0
    operation: CompanyOperationProgress | None = None
    publication: CompanyPublicationProgress | None = None
    population: int | None = None
    failure_document: Disclosure | None = None
    failure_stage: CompanyOperationKind | None = None
    unpublished_documents: int | None = None
    failure_reason: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompanyEvidenceSyncResult:
    """Durable outcomes; null pending counts mean the final inventory was not checked."""

    observed_at: datetime
    requested_codes: int
    published_codes: int
    fetched_pages: int
    changed_rows: int
    reused_partitions: int
    pending_partitions: int | None
    failures: tuple[str, ...]
    elapsed_seconds: float
    pending_documents: int | None = 0
    news_population_codes: int = 0
    news_coverage: Literal["published_population", "uncovered"] = "uncovered"
    ocr_documents: int | None = 0
    unextractable_documents: int | None = 0
    next_document_retry_at: datetime | None = None
    window_days: int = 365

    @property
    def needs_retry(self) -> bool:
        return bool(
            self.failures or self.pending_partitions is None or self.pending_documents is None
            or self.ocr_documents is None or self.unextractable_documents is None
            or self.pending_partitions or self.pending_documents
        )


_OCR_FAILURES = frozenset(
    {
        "disclosure_document_no_text",
        "disclosure_document_short_text",
        "disclosure_document_unreadable",
        "disclosure_document_text_unavailable",
        "disclosure_document_ocr_unavailable",
        "disclosure_document_ocr_language_unavailable",
        "disclosure_document_ocr_timeout",
        "disclosure_document_ocr_timeout_exhausted",
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
        "FileNotFoundError",
        "company_local_document_missing",
        "disclosure_document_ocr_timeout",
        "timeout",
        "HttpConnectionError",
        "HttpError",
        "http_transport_failed",
        "http_compressed_body_invalid",
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
    configuration: CompanySyncConfiguration,
    ocr_identity: str,
    *,
    text_identity: str = "poppler-path",
) -> CompanyDocumentPolicies:
    limits = configuration.document_limits
    expanded = configuration.extended_document_limits
    return CompanyDocumentPolicies(
        standard=(
            f"text_4:{text_identity}:retry_inputs=1:timeout={configuration.document_parse_timeout_seconds:g}:"
            f"{limits.max_bytes}:{limits.max_pages}:{limits.max_text_characters}"
        ),
        ocr=(
            f"ocr_4:psm6_3:retry_layout=orient_rotate_dpi_each_page:retry2:edge4096:{ocr_identity}:timeout={configuration.document_ocr_timeout_seconds:g}:"
            f"page_timeout={configuration.document_ocr_page_timeout_seconds:g}:"
            f"{configuration.document_ocr_max_pages}:{configuration.document_ocr_dpi}:"
            f"{configuration.document_ocr_languages}"
        ),
        limits=f"limits_1:{expanded.max_bytes}:{expanded.max_pages}:{expanded.max_text_characters}",
    )


def CompanyDocumentOutputIdentity(policy: str) -> str:
    """Recovery policies reopen failures without invalidating complete successful outputs."""
    return ":".join(part for part in policy.split(":") if not part.startswith(("timeout=", "page_timeout=", "retry_layout=", "retry_inputs=")))


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
    "CompanyOperationKind",
    "CompanyOperationProgress",
    "CompanyPublicationProgress",
    "CompanyStockProgress",
    "BuildCompanyDocumentPolicies",
    "CompanyDocumentOutputIdentity",
    "CompanyCoverageWatermark",
    "CompanyDocumentExtraction",
    "CompanyDocumentFailure",
    "CompanyOcrDiagnostic",
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
    "DisclosurePartitionRetry",
    "EvidenceMarket",
    "EvidenceSyncProgress",
    "EvidenceProgressState",
    "Market",
    "IsTransientCompanyDocumentFailure",
    "StructuredCompanyPage",
    "CompanySourceFailure",
]

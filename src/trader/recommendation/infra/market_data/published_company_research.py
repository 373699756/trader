"""Preload one company publication and qualify its facts against the startup calendar."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, TypeVar

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEvidenceLink,
    CompanyPositiveEvent,
    CompanyRiskEvent,
    CompanyRiskType,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_evidence import CompanyFinancial
from trader.infra.settings.models import CompanyPreloadSettings
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.domain.evidence.document_reference import CompanyDocumentReference
from trader.recommendation.domain.market.epochs import ResearchEpoch
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.quality import FieldQualityState, FieldValue
from trader.recommendation.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    FinancialReport,
    FinancialReportIsFresh,
    ResearchObservation,
)
from trader.recommendation.domain.scoring.company import BuildCompanyScoringContext, CompanyScoringCalendar, CompanyScoringPolicy
from trader.recommendation.infra.market_data.research_load_status import ResearchComponentCoverage, ResearchLoaderStatus

_LOGGER = logging.getLogger(__name__)
_SQLITE_LOGGER = _LOGGER.getChild("sqlite")
_ReadValue = TypeVar("_ReadValue")
_SQLITE_INTERNAL_FAILURES = frozenset({
    "sqlite_schema_unsupported", "sqlite_required_row_missing", "sqlite_integrity_failed",
})


def _observe_snapshot_read(
    stage: Literal["before", "visit", "after"], read: Callable[[], _ReadValue]
) -> _ReadValue:
    try:
        return read()
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        cause = exc.__cause__
        schema_unsupported = _company_snapshot_failure(exc) == "company_snapshot:sqlite_schema_unsupported"
        if schema_unsupported:
            _LOGGER.warning(
                "公司正式资料结构不兼容（sqlite_schema_unsupported），已停止加载；"
                "请先备份公司资料，在独立目录重新下载验证，确认后替换 company_facts.sqlite3 并重启。stage=%s",
                stage,
            )
        else:
            _LOGGER.warning(
                "company preload read failed stage=%s error_type=%s cause_type=%s",
                stage,
                type(exc).__name__,
                type(cause).__name__ if cause is not None else "none",
            )
        detail = _snapshot_sqlite_failure_detail(exc)
        if detail is not None:
            _SQLITE_LOGGER.log(
                logging.DEBUG if schema_unsupported else logging.WARNING,
                "company preload sqlite failure stage=%s detail=%s", stage, detail,
            )
        raise


def _company_snapshot_failure(exc: BaseException) -> str:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, sqlite3.Error) and len(current.args) == 1:
            code = current.args[0]
            if isinstance(code, str) and code in _SQLITE_INTERNAL_FAILURES:
                return f"company_snapshot:{code}"
        current = current.__cause__ if current.__cause__ is not None else (
            current.__context__ if not current.__suppress_context__ else None
        )
    return f"company_snapshot:{type(exc).__name__}"


def _snapshot_sqlite_failure_detail(exc: BaseException) -> str | None:
    """Project the full causal frame chain without exception messages or local values."""
    chain: list[dict[str, object]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    has_sqlite_failure = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlite_code: int | None = None
        sqlite_name: str | None = None
        internal_code: str | None = None
        if isinstance(current, sqlite3.Error):
            has_sqlite_failure = True
            # Python 3.10 has no native SQLite error attributes. A manually
            # raised DatabaseError also carries neither; preserve that fact.
            raw_code = getattr(current, "sqlite_errorcode", None)
            raw_name = getattr(current, "sqlite_errorname", None)
            if type(raw_code) is int and 0 <= raw_code <= 65535:
                sqlite_code = raw_code
            if isinstance(raw_name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,48}", raw_name):
                sqlite_name = raw_name
            if len(current.args) == 1 and isinstance(current.args[0], str):
                if current.args[0] in _SQLITE_INTERNAL_FAILURES:
                    internal_code = current.args[0]
        frames: list[str] = []
        trace = current.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            module = frame.f_globals.get("__name__")
            if not isinstance(module, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module) is None:
                module = "unknown"
            function = re.sub(r"[^A-Za-z0-9_<>]", "_", frame.f_code.co_name)
            frames.append(f"{module}:{function}:{trace.tb_lineno}")
            trace = trace.tb_next
        chain.append({
            "error_type": type(current).__name__, "sqlite_errorcode": sqlite_code,
            "sqlite_errorname": sqlite_name, "internal_code": internal_code, "frames": frames,
        })
        current = current.__cause__ if current.__cause__ is not None else (
            current.__context__ if not current.__suppress_context__ else None
        )
    return json.dumps(chain, separators=(",", ":"), ensure_ascii=True) if has_sqlite_failure else None


class CompanyEvidenceReadPort(Protocol):
    def publication_version(self) -> str: ...

    def publication_identity(self, *, cancel_requested: Callable[[], bool] | None = None) -> str: ...

    def visit(
        self,
        codes: Sequence[str],
        consume: Callable[[CompanyFacts], None],
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None: ...

    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyEvidenceLink, ...]: ...


class _CompanyInputOptions(TypedDict, total=False):
    reserve_bytes: Callable[[int], bool]
    source_versions: Callable[[], Mapping[str, str]]
    config_version: str


class PublishedCompanyResearch:
    def __init__(
        self,
        reader: CompanyEvidenceReadPort,
        *,
        wall_clock: Callable[[], datetime],
        open_dates: Callable[[], tuple[date, ...]],
        monotonic: Callable[[], float],
        company_policy: CompanyScoringPolicy,
        preload_settings: CompanyPreloadSettings,
        financial_max_age_days: int,
        **options: Unpack[_CompanyInputOptions],
    ) -> None:
        self._reader = reader
        self._wall_clock = wall_clock
        self._open_dates = open_dates
        self._monotonic = monotonic
        self._reserve_bytes = options.get("reserve_bytes")
        self._source_versions = options.get("source_versions")
        self._company_policy = company_policy
        self._preload_settings = preload_settings
        if type(financial_max_age_days) is not int or financial_max_age_days < 1:
            raise ValueError("financial_max_age_days_invalid")
        self._financial_max_age_days = financial_max_age_days
        self._config_version = options.get("config_version", "company")
        self._initialization_lock = threading.Lock()
        self._lock = threading.RLock()
        self._epoch: ResearchEpoch | None = None
        self._last_error = "company_not_loaded"
        self._publication_version = ""
        self._disk_version = ""
        self._publication_observation_failure: str | None = None
        self._initialized = False

    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyDocumentReference, ...]:
        return tuple(
            CompanyDocumentReference(
                item.evidence_id,
                item.title,
                item.url if item.url.startswith(("https://", "http://")) else None,
                item.published_at,
                "extracted" if item.body_checked else "pending",
            )
            for item in self._reader.document_links(code, evidence_ids)
        )

    def initialize(self, codes: Sequence[str], *, cancelled: Callable[[], bool]) -> None:
        with self._initialization_lock:
            self._initialize(codes, cancelled=cancelled)

    def _initialize(self, codes: Sequence[str], *, cancelled: Callable[[], bool]) -> None:
        with self._lock:
            if self._initialized:
                return
            self._initialized = True
        requested = tuple(sorted(set(codes)))
        if not requested or len(requested) > 12_000:
            self._last_error = "company_population_unavailable"
            return
        deadline = self._monotonic() + self._preload_settings.timeout_seconds
        observed_at = ShanghaiNow(self._wall_clock())
        for _ in range(self._preload_settings.publication_attempts):
            if cancelled() or self._monotonic() >= deadline:
                self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                return
            try:
                before, rows = self._capture_publication(requested, observed_at, cancelled, deadline)
                after = _observe_snapshot_read("after", self._reader.publication_version)
            except InterruptedError:
                self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                return
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self._last_error = _company_snapshot_failure(exc)
                return
            if cancelled() or self._monotonic() >= deadline:
                self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                return
            if before != after:
                continue
            self._adopt_projection(rows, observed_at, after, cancelled, deadline)
            return
        self._last_error = "company_publication_changed"

    def _capture_publication(
        self, requested: tuple[str, ...], observed_at: datetime, cancelled: Callable[[], bool], deadline: float
    ) -> tuple[str, dict[str, ResearchObservation]]:
        before = _observe_snapshot_read("before", self._reader.publication_version)
        calendar = _observe_snapshot_read("before", lambda: CompanyScoringCalendar(self._open_dates()))
        rows: dict[str, ResearchObservation] = {}

        def capture(snapshot: CompanyFacts) -> None:
            if cancelled() or self._monotonic() >= deadline:
                raise InterruptedError("company_preload_stopped")
            if snapshot.published_at <= observed_at:
                rows[snapshot.code] = _observation(
                    snapshot, observed_at, before, calendar, self._company_policy,
                    financial_max_age_days=self._financial_max_age_days,
                )

        _observe_snapshot_read(
            "visit",
            lambda: self._reader.visit(
                requested, capture, cancel_requested=lambda: cancelled() or self._monotonic() >= deadline
            ),
        )
        return before, rows

    def _adopt_projection(
        self,
        rows: Mapping[str, ResearchObservation],
        observed_at: datetime,
        publication: str,
        cancelled: Callable[[], bool],
        deadline: float,
    ) -> None:
        epoch = _research_epoch(
            rows,
            observed_at,
            publication,
            self._config_version,
            self._source_versions() if self._source_versions is not None else {},
        )
        # Conservative retained-size reservation: fixed observations/lineage plus bounded event strings.
        size_bytes = sum(8192 + _event_reservation(row) for row in rows.values())
        if cancelled() or self._monotonic() >= deadline:
            self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
            return
        if self._reserve_bytes is not None and not self._reserve_bytes(size_bytes):
            self._last_error = "company_preload_capacity"
            return
        with self._lock:
            self._epoch = epoch
            self._publication_version = self._disk_version = publication
            self._last_error = "" if rows else "company_snapshot:missing"

    @property
    def loaded_identity(self) -> str:
        with self._lock:
            return self._publication_version or "company:unavailable"

    @property
    def input_identity(self) -> str:
        with self._lock:
            return self._epoch.input_identity if self._epoch is not None else "company:unavailable"

    def _required_coverage(self, now: datetime) -> date | None:
        try:
            dates = self._open_dates()
        except (OSError, RuntimeError, ValueError):
            return None
        previous = tuple(day for day in dates if day < now.date())
        return previous[-1] if previous and dates[-1] >= now.date() else None

    def cached(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
        as_of: datetime | None = None,
    ) -> Mapping[str, ResearchObservation]:
        now = ShanghaiNow(as_of if as_of is not None else self._wall_clock())
        required = self._required_coverage(now)
        result: dict[str, ResearchObservation] = {}
        with self._lock:
            epoch = self._epoch
        for code in codes:
            original = epoch.observations.get(code) if epoch is not None else None
            if original is not None and original.snapshot_at is not None and original.snapshot_at > now:
                original = None
            item = (
                _qualified(original, now, required, self._company_policy.events.unlock_days, self._financial_max_age_days)
                if original is not None
                else None
            )
            if item is None or not _action_ready(item):
                if action_restrictions is not None:
                    action_restrictions.setdefault(code, set()).add("research_data_degraded")
            if item is not None and (not fresh_only or _action_ready(item)):
                result[code] = item
        return MappingProxyType(result)

    def load(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, ResearchObservation]:
        return self.cached(
            codes, include_structured=include_structured, action_restrictions=action_restrictions, as_of=observed_at
        )

    def observe_publication(self, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            return
        try:
            identity = self._reader.publication_identity(cancel_requested=cancelled)
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as exc:
            if not cancelled():
                with self._lock:
                    self._publication_observation_failure = f"company_observation:{type(exc).__name__}"
            return
        if not cancelled():
            with self._lock:
                self._disk_version = identity
                self._publication_observation_failure = None if identity else "company_publication_unavailable"

    def status(self) -> ResearchLoaderStatus:
        now = ShanghaiNow(self._wall_clock())
        required = self._required_coverage(now)
        with self._lock:
            epoch = self._epoch
            last_error = self._last_error
            publication_version = self._publication_version
            disk_version = self._disk_version
            observation_failure = self._publication_observation_failure
        rows = tuple(
            _qualified(item, now, required, self._company_policy.events.unlock_days, self._financial_max_age_days)
            for item in (epoch.observations.values() if epoch is not None else ())
        )
        coverages = tuple(ResearchComponentCoverage(item) for item in rows)
        covered = tuple(sum(coverage[index] for coverage in coverages) for index in range(4))
        verified = sum(_action_ready(item) for item in rows)
        partial = sum(
            any(coverage) and not _action_ready(item) for item, coverage in zip(rows, coverages, strict=True)
        )
        return ResearchLoaderStatus(
            entries=len(rows),
            success_count=len(rows),
            error_count=sum(bool(item.source_errors) for item in rows),
            planned_count=len(rows),
            timeout_count=0,
            consecutive_failures=0,
            circuit_open=False,
            latencies_ms=(),
            latest_source_time=max((row.snapshot_at for row in rows if row.snapshot_at is not None), default=None),
            last_error=last_error,
            out_of_order_count=0,
            corporate_risk_covered_count=sum(item.corporate_risk_window_complete for item in rows),
            corporate_risk_fact_count=sum(len(item.corporate_risk_facts) for item in rows),
            corporate_risk_evidence_versions=tuple(sorted({item.corporate_risk_evidence_version for item in rows})),
            verified_count=verified,
            partial_count=partial,
            unavailable_count=len(rows) - verified - partial,
            financial_covered_count=covered[0],
            announcements_covered_count=covered[1],
            pledge_covered_count=covered[2],
            unlock_covered_count=covered[3],
            loaded_identity=publication_version,
            disk_identity=disk_version,
            restart_required=publication_version != disk_version,
            publication_observation_failure=observation_failure,
        )


def _research_epoch(
    rows: Mapping[str, ResearchObservation],
    observed_at: datetime,
    publication: str,
    config_version: str,
    versions: Mapping[str, str],
) -> ResearchEpoch:
    fields: dict[str, Mapping[str, FieldValue]] = {}
    for code, row in rows.items():
        coverage = ResearchComponentCoverage(row)
        components = (
            ("financial", coverage[0]),
            ("announcements", coverage[1]),
            ("pledge", coverage[2]),
            ("unlock", coverage[3]),
            ("corporate_risk", row.corporate_risk_window_complete),
        )
        fields[code] = {
            name: FieldValue(
                name,
                True if covered else None,
                "company",
                row.snapshot_at or observed_at,
                observed_at,
                row.data_version,
                row.data_version,
                FieldQualityState.VALID if covered else FieldQualityState.MISSING,
            )
            for name, covered in components
        }
    return ResearchEpoch(
        observed_at.date(),
        0,
        observed_at,
        observed_at,
        config_version,
        rows,
        {**versions, "company": publication or "company:unavailable"},
        fields,
    )


def _event_reservation(row: ResearchObservation) -> int:
    if row.company_context is None:
        return 0
    facts = row.company_context.facts
    events: tuple[CompanyRiskEvent | CompanyPositiveEvent, ...] = (*facts.risk_events, *facts.positive_events)
    return sum(4096 + 4 * (len(event.summary) + sum(map(len, event.evidence_ids))) for event in events) + 1024 * len(
        facts.review_reasons
    )


def _action_ready(item: ResearchObservation) -> bool:
    return (
        item.financial_history_complete
        and item.corporate_risk_window_complete
        and item.pledge_ratio_pct is not None
        and item.unlock_ratio_pct is not None
        and not any(not failure.startswith("news:") for failure in item.source_errors)
    )


def _qualified_financial(item: ResearchObservation, now: datetime, maximum_age_days: int) -> ResearchObservation:
    if item.financial is None or FinancialReportIsFresh(item.financial, now, maximum_age_days=maximum_age_days):
        return item
    return replace(
        item, financial_history_complete=False,
        source_errors=tuple(sorted({*item.source_errors, "financial_scoring:report_stale"})),
    )


def _qualified(
    item: ResearchObservation, now: datetime, required: date | None, unlock_days: int, financial_max_age_days: int,
) -> ResearchObservation:
    item = _qualified_financial(item, now, financial_max_age_days)
    item = _qualified_unlock(item, now, unlock_days)
    fresh = (
        required is not None
        and item.risk_window_end is not None
        and item.risk_window_end >= required
        and item.snapshot_at is not None
        and item.snapshot_at <= now
    )
    if fresh:
        return item
    # Keep verified facts and disclosed financial reports; missing coverage cannot erase a known risk.
    return replace(
        item,
        company_context=replace(item.company_context, coverage_current=False) if item.company_context else None,
        corporate_risk_window_complete=False,
        pledge_ratio_pct=None,
        unlock_ratio_pct=None,
        news_queried=False,
        source_errors=tuple(sorted(set((*item.source_errors, "risk_coverage:stale")))),
    )


def _qualified_unlock(item: ResearchObservation, now: datetime, days: int) -> ResearchObservation:
    context = item.company_context
    complete = (
        context is not None
        and context.facts.unlock_state is CompanyDataState.COMPLETE
        and context.facts.unlock_checked_at is not None
        and context.facts.unlock_checked_at <= now
        and item.unlock_window_start is not None
        and item.unlock_window_start <= now.date()
        and item.unlock_window_end is not None
        and item.unlock_window_end >= now.date() + timedelta(days=days)
    )
    errors = tuple(error for error in item.source_errors if error != "unlock:coverage_incomplete")
    if not complete:
        errors = (*errors, "unlock:coverage_incomplete")
    ratio = (
        min(
            100.0,
            sum(
                entry.ratio_pct
                for entry in context.facts.unlock_schedule
                if now.date() <= entry.day <= now.date() + timedelta(days=days)
            ),
        )
        if complete and context is not None
        else None
    )
    return replace(
        item,
        unlock_ratio_pct=ratio,
        company_context=replace(context, coverage_current=context.coverage_current and complete) if context else None,
        source_errors=errors,
    )


def _financial_report(financial: CompanyFinancial | None, now: datetime) -> FinancialReport | None:
    if financial is None or financial.published_at > now or financial.report_date > now.date():
        return None
    return FinancialReport(
        financial.report_date, ShanghaiNow(financial.published_at), financial.basic_eps,
        financial.book_value_per_share, financial.revenue_growth_pct, financial.net_profit_growth_pct,
        financial.core_profit_growth_pct, financial.roe_pct, financial.parent_net_profit, financial.core_net_profit,
    )


def _observation(
    snapshot: CompanyFacts, now: datetime, publication: str, calendar: CompanyScoringCalendar, policy: CompanyScoringPolicy,
    *, financial_max_age_days: int,
) -> ResearchObservation:
    report = _financial_report(snapshot.financial, now)
    latest = _financial_report(snapshot.latest_financial, now)
    history = tuple(item for position, item in enumerate((report, latest)) if item is not None and (not position or item != report))
    fresh = FinancialReportIsFresh(report, now, maximum_age_days=financial_max_age_days)
    categories = {
        CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION: CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION,
        CompanyRiskType.FINANCIAL_FRAUD: CorporateRiskCategory.FINANCIAL_FRAUD,
        CompanyRiskType.OFFICIAL_INVESTIGATION: CorporateRiskCategory.OFFICIAL_INVESTIGATION,
        CompanyRiskType.MAJOR_ILLEGAL: CorporateRiskCategory.MAJOR_ILLEGAL,
        CompanyRiskType.FUND_OCCUPATION: CorporateRiskCategory.FUND_OCCUPATION,
        CompanyRiskType.ILLEGAL_GUARANTEE: CorporateRiskCategory.ILLEGAL_GUARANTEE,
        CompanyRiskType.FORCED_DELISTING: CorporateRiskCategory.FORCED_DELISTING,
    }
    facts = tuple(
        CorporateRiskFact(
            categories[event.risk_type],
            ShanghaiNow(event.first_disclosed_at),
            event.event_id,
            "issuer_disclosure",
            ShanghaiNow(event.resolved_at) if event.resolved_at else None,
            event.case_id or "",
            expires_at=ShanghaiNow(event.expires_at) if event.expires_at else None,
            persistent=event.expires_at is None,
        )
        for event in snapshot.risk_events
        if event.risk_type in categories and event.first_disclosed_at <= now
    )
    events: tuple[CompanyRiskEvent | CompanyPositiveEvent, ...] = (*snapshot.risk_events, *snapshot.positive_events)
    evidence = tuple(
        Evidence(
            event.evidence_ids[-1],
            "regulatory_filing",
            event.summary,
            "issuer_disclosure",
            ShanghaiNow(event.last_disclosed_at),
            ShanghaiNow(snapshot.published_at),
            publication,
        )
        for event in events
        if event.last_disclosed_at <= now
    )
    errors = tuple(
        sorted({
            *(f"{item.scope}:{item.reason_code}" for item in snapshot.review_reasons if item.scope != "positive"),
            *(("financial_scoring:report_stale",) if report is not None and not fresh else ()),
        })
    )
    observation = ResearchObservation(
        company_context=BuildCompanyScoringContext(snapshot, calendar, policy),
        data_version=publication,
        snapshot_at=ShanghaiNow(snapshot.published_at),
        loaded_at=now,
        queried_on=snapshot.checked_at.date(),
        pledge_checked_on=snapshot.pledge.day if snapshot.pledge else None,
        financial=report,
        financial_history=history,
        financial_history_complete=report is not None and fresh and snapshot.financial_state is CompanyDataState.COMPLETE,
        announcements_available=snapshot.announcement_state is CompanyDataState.COMPLETE,
        pledge_ratio_pct=snapshot.pledge.ratio_pct
        if snapshot.pledge is not None
        and snapshot.pledge.day <= now.date()
        and snapshot.pledge_state is CompanyDataState.COMPLETE
        else None,
        unlock_window_start=snapshot.checked_at.date(),
        unlock_window_end=snapshot.unlock_coverage_end,
        corporate_risk_facts=facts,
        corporate_risk_window_complete=snapshot.announcement_state is CompanyDataState.COMPLETE,
        risk_window_start=snapshot.announcement_coverage_start,
        risk_window_end=snapshot.announcement_coverage_end,
        corporate_risk_evidence_version=publication,
        evidence=evidence,
        source_errors=errors,
    )
    return _qualified_unlock(observation, now, policy.events.unlock_days)


__all__ = ["CompanyEvidenceReadPort", "PublishedCompanyResearch"]

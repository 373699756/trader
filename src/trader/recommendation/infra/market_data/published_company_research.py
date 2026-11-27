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
    CompanyPositiveEvent,
    CompanyRiskEvent,
    CompanyRiskType,
)
from trader.download.domain.company_document_links import CompanyDocumentLinkSnapshot
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_source_records import CompanyFinancialScoringObservation
from trader.infra.settings.models import CompanyPreloadSettings
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.domain.evidence.document_reference import CompanyDocumentReference
from trader.recommendation.domain.market.epochs import ResearchEpoch
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.quality import FieldQualityState, FieldValue
from trader.recommendation.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    FinancialReportIsFresh,
    FinancialScoringObservationIsVisible,
    ProjectCompanyFinancialReport,
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
    stage: Literal["before", "visit", "after", "financial_scoring"], read: Callable[[], _ReadValue]
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
                "请先备份公司资料，在独立目录重新下载验证，确认后替换 company_published.sqlite3 并重启。stage=%s",
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

    def read_document_links(
        self, codes: Sequence[str], *, cancel_requested: Callable[[], bool] | None = None,
    ) -> CompanyDocumentLinkSnapshot: ...


class CompanyFinancialScoringReadPort(Protocol):
    def read_financial_scoring_observations(
        self,
        codes: Sequence[str],
        *,
        as_of: datetime,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[CompanyFinancialScoringObservation, ...]: ...


class _CompanyInputOptions(TypedDict, total=False):
    reserve_bytes: Callable[[int], bool]
    source_versions: Callable[[], Mapping[str, str]]
    config_version: str
    financial_scoring_reader: CompanyFinancialScoringReadPort


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
        self._financial_scoring_reader = options.get("financial_scoring_reader")
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
        self._coverage_lock = threading.Lock()
        self._lock = threading.RLock()
        self._epoch: ResearchEpoch | None = None
        self._adopted_at: datetime | None = None
        self._coverage_status: tuple[date, date | None, ResearchLoaderStatus] | None = None
        self._last_error = "company_not_loaded"
        self._publication_version = ""
        self._disk_version = ""
        self._publication_observation_failure: str | None = None
        self._initialized = False
        self._planned_count = 0
        self._document_links: Mapping[str, tuple[CompanyDocumentReference, ...]] = MappingProxyType({})

    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyDocumentReference, ...]:
        if (
            len(code) != 6 or not code.isascii() or not code.isdigit() or not 1 <= len(evidence_ids) <= 32
            or any(not identity or len(identity) > 200 for identity in evidence_ids)
        ):
            raise ValueError("company_document_query_invalid")
        requested = frozenset(evidence_ids)
        with self._lock:
            links = self._document_links.get(code, ())
        return tuple(item for item in links if item.evidence_id in requested)

    def initialize(self, codes: Sequence[str], *, cancelled: Callable[[], bool]) -> None:
        with self._initialization_lock:
            self._initialize(codes, cancelled=cancelled)

    def _initialize(self, codes: Sequence[str], *, cancelled: Callable[[], bool]) -> None:
        with self._lock:
            if self._initialized:
                return
            self._initialized = True
            requested = tuple(sorted(set(codes)))
            self._planned_count = len(requested)
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
                before, rows, scoring_failed = self._capture_publication(requested, observed_at, cancelled, deadline)
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
            try:
                links = _observe_snapshot_read(
                    "visit", lambda: self._reader.read_document_links(
                        requested, cancel_requested=lambda: cancelled() or self._monotonic() >= deadline,
                    ),
                )
            except InterruptedError:
                self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                return
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self._last_error = _company_snapshot_failure(exc)
                return
            if links.publication_hash != after:
                continue
            self._adopt_projection(rows, links, observed_at, after, cancelled, deadline, scoring_failed=scoring_failed)
            return
        self._last_error = "company_publication_changed"

    def _capture_publication(
        self, requested: tuple[str, ...], observed_at: datetime, cancelled: Callable[[], bool], deadline: float
    ) -> tuple[str, dict[str, ResearchObservation], bool]:
        before = _observe_snapshot_read("before", self._reader.publication_version)
        calendar = _observe_snapshot_read("before", lambda: CompanyScoringCalendar(self._open_dates()))
        rows: dict[str, ResearchObservation] = {}

        def preload_stopped() -> bool:
            return cancelled() or self._monotonic() >= deadline

        scoring = self._read_financial_scoring(requested, observed_at, preload_stopped) if before else {}

        def capture(snapshot: CompanyFacts) -> None:
            if preload_stopped():
                raise InterruptedError("company_preload_stopped")
            if snapshot.published_at <= observed_at:
                rows[snapshot.code] = _observation(
                    snapshot, observed_at, before, calendar, self._company_policy,
                    financial_max_age_days=self._financial_max_age_days,
                    financial_scoring_observation=scoring.get(snapshot.code) if scoring is not None else None,
                )

        _observe_snapshot_read(
            "visit",
            lambda: self._reader.visit(
                requested, capture, cancel_requested=preload_stopped,
            ),
        )
        return before, rows, scoring is None

    def _read_financial_scoring(
        self,
        codes: tuple[str, ...],
        observed_at: datetime,
        cancelled: Callable[[], bool],
    ) -> dict[str, CompanyFinancialScoringObservation] | None:
        reader = self._financial_scoring_reader
        if reader is None:
            return {}
        try:
            observations = _observe_snapshot_read(
                "financial_scoring",
                lambda: reader.read_financial_scoring_observations(codes, as_of=observed_at, cancel_requested=cancelled),
            )
        except InterruptedError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError):
            # The formal path still checks its own seal and publication identity.
            return None
        return {
            item.observation.financial.code: item for item in observations
            if item.available_at <= observed_at
        }

    def _adopt_projection(
        self,
        rows: Mapping[str, ResearchObservation],
        links: CompanyDocumentLinkSnapshot,
        observed_at: datetime,
        publication: str,
        cancelled: Callable[[], bool],
        deadline: float,
        *,
        scoring_failed: bool,
    ) -> None:
        # Conservative retained-size reservation: fixed observations/lineage plus bounded event strings.
        size_bytes = sum(8192 + _event_reservation(row) for row in rows.values())
        documents: dict[str, list[CompanyDocumentReference]] = {}
        seen: set[tuple[str, str]] = set()
        for item in links.links:
            if cancelled() or self._monotonic() >= deadline:
                self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                return
            if item.code not in rows or item.link.published_at > observed_at:
                continue
            key = (item.code, item.link.evidence_id)
            if key in seen:
                self._last_error = "company_document_link_identity_ambiguous"
                return
            seen.add(key)
            link = item.link
            documents.setdefault(item.code, []).append(CompanyDocumentReference(
                link.evidence_id, link.title, link.url if link.url.startswith(("https://", "http://")) else None,
                link.published_at, "extracted" if link.body_checked else "pending",
            ))
            size_bytes += 1024 + 4 * (len(link.evidence_id) + len(link.title) + len(link.url))
        if cancelled() or self._monotonic() >= deadline:
            self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
            return
        epoch = _research_epoch(
            rows,
            observed_at,
            publication,
            self._config_version,
            self._source_versions() if self._source_versions is not None else {},
        )
        document_links = MappingProxyType({
            code: tuple(sorted(items, key=lambda item: (item.disclosed_at, item.evidence_id), reverse=True))
            for code, items in documents.items()
        })
        if cancelled() or self._monotonic() >= deadline:
            self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
            return
        if self._reserve_bytes is not None and not self._reserve_bytes(size_bytes):
            self._last_error = "company_preload_capacity"
            return
        with self._lock:
            # The projection and its capacity reservation must be complete before it can be adopted.
            self._adopted_at = max(observed_at, ShanghaiNow(self._wall_clock()))
            self._epoch = epoch
            self._coverage_status = None
            self._document_links = document_links
            self._publication_version = self._disk_version = publication
            self._last_error = "" if rows else "company_snapshot:missing"
            if scoring_failed:
                self._last_error = "company_financial_scoring_read_failed"
        self._refresh_coverage_status(lambda: cancelled() or self._monotonic() >= deadline)

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
            adopted_at = self._adopted_at
        for code in codes:
            original = epoch.observations.get(code) if epoch is not None else None
            if original is not None and original.snapshot_at is not None and original.snapshot_at > now:
                original = None
            item = (
                _qualified(
                    original, now, required, self._company_policy.events.unlock_days, self._financial_max_age_days, adopted_at,
                )
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
        else:
            if cancelled():
                return
            with self._lock:
                self._disk_version = identity
                self._publication_observation_failure = None if identity else "company_publication_unavailable"
        self._refresh_coverage_status(cancelled)

    def status(self) -> ResearchLoaderStatus:
        status = self._refresh_coverage_status()
        assert status is not None
        return status

    def _refresh_coverage_status(
        self, cancelled: Callable[[], bool] | None = None,
    ) -> ResearchLoaderStatus | None:
        if cancelled is not None and cancelled():
            return None
        now = ShanghaiNow(self._wall_clock())
        required = self._required_coverage(now)
        cached = self._cached_coverage(now, required)
        if cached is not None:
            return cached
        if not self._coverage_lock.acquire(blocking=cancelled is None):
            # Another refresh is already serving readers. The observer will run again.
            return None
        try:
            cached = self._cached_coverage(now, required)
            if cached is not None:
                return cached
            return self._build_coverage_status(now, required, cancelled)
        finally:
            self._coverage_lock.release()

    def _cached_coverage(self, now: datetime, required: date | None) -> ResearchLoaderStatus | None:
        # After adoption, coverage windows and report ages advance on Shanghai calendar days.
        with self._lock:
            cached = self._coverage_status
            if (self._adopted_at is None or now < self._adopted_at
                    or cached is None or cached[:2] != (now.date(), required)):
                return None
            summary = cached[2]
            if (summary.disk_identity != self._disk_version
                    or summary.publication_observation_failure != self._publication_observation_failure):
                summary = replace(
                    summary, disk_identity=self._disk_version, restart_required=self._publication_version != self._disk_version,
                    publication_observation_failure=self._publication_observation_failure,
                )
                self._coverage_status = (now.date(), required, summary)
            return summary

    def _build_coverage_status(
        self, now: datetime, required: date | None, cancelled: Callable[[], bool] | None,
    ) -> ResearchLoaderStatus | None:
        with self._lock:
            epoch = self._epoch
            planned_count = self._planned_count
            last_error = self._last_error
            publication_version = self._publication_version
            disk_version = self._disk_version
            observation_failure = self._publication_observation_failure
            adopted_at = self._adopted_at
        rows = []
        for item in epoch.observations.values() if epoch is not None else ():
            if cancelled is not None and cancelled():
                return None
            rows.append(_qualified(
                item, now, required, self._company_policy.events.unlock_days, self._financial_max_age_days, adopted_at,
            ))
        coverages = tuple(ResearchComponentCoverage(item) for item in rows)
        covered = tuple(sum(coverage[index] for coverage in coverages) for index in range(4))
        verified = sum(_action_ready(item) for item in rows)
        partial = sum(
            any(coverage) and not _action_ready(item) for item, coverage in zip(rows, coverages, strict=True)
        )
        status = ResearchLoaderStatus(
            entries=len(rows),
            success_count=len(rows),
            error_count=sum(bool(item.source_errors) for item in rows),
            planned_count=planned_count,
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
            unavailable_count=planned_count - verified - partial,
            financial_scoring_observation_count=sum(item.financial_scoring_observation is not None for item in rows),
            financial_covered_count=covered[0],
            announcements_covered_count=covered[1],
            pledge_covered_count=covered[2],
            unlock_covered_count=covered[3],
            loaded_identity=publication_version,
            disk_identity=disk_version,
            restart_required=publication_version != disk_version,
            publication_observation_failure=observation_failure,
        )
        if adopted_at is not None and now >= adopted_at:
            with self._lock:
                if epoch is self._epoch:
                    self._coverage_status = (now.date(), required, status)
        return status


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


def _qualified_financial(
    item: ResearchObservation, now: datetime, maximum_age_days: int, adopted_at: datetime | None,
) -> ResearchObservation:
    if item.financial_scoring_observation is not None:
        item = replace(item, financial_scoring_adopted_at=adopted_at)
        if adopted_at is None or not FinancialScoringObservationIsVisible(item, now):
            item = replace(item, financial_scoring_observation=None, financial_scoring_adopted_at=None)
    if item.financial is None or FinancialReportIsFresh(item.financial, now, maximum_age_days=maximum_age_days):
        return item
    return replace(
        item, financial_history_complete=False,
        source_errors=tuple(sorted({*item.source_errors, "financial_scoring:report_stale"})),
    )


def _qualified(
    item: ResearchObservation, now: datetime, required: date | None, unlock_days: int, financial_max_age_days: int,
    adopted_at: datetime | None,
) -> ResearchObservation:
    item = _qualified_financial(item, now, financial_max_age_days, adopted_at)
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
        company_context=(
            replace(item.company_context, coverage_current=False)
            if item.company_context is not None and item.company_context.coverage_current else item.company_context
        ),
        corporate_risk_window_complete=False,
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
    if context is not None and context.coverage_current and not complete:
        context = replace(context, coverage_current=False)
    if context is item.company_context and ratio == item.unlock_ratio_pct and errors == item.source_errors:
        return item
    return replace(item, unlock_ratio_pct=ratio, company_context=context, source_errors=errors)


def _observation(
    snapshot: CompanyFacts, now: datetime, publication: str, calendar: CompanyScoringCalendar, policy: CompanyScoringPolicy,
    *, financial_max_age_days: int, financial_scoring_observation: CompanyFinancialScoringObservation | None = None,
) -> ResearchObservation:
    report = ProjectCompanyFinancialReport(snapshot.financial, now)
    latest = ProjectCompanyFinancialReport(snapshot.latest_financial, now)
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
        financial_scoring_observation=financial_scoring_observation,
        announcements_available=snapshot.announcement_state is CompanyDataState.COMPLETE,
        pledge_ratio_pct=snapshot.pledge.ratio_pct
        if snapshot.pledge is not None
        and snapshot.pledge.day <= now.date()
        and snapshot.pledge_state is CompanyDataState.COMPLETE
        else None,
        unlock_window_start=ShanghaiNow(snapshot.unlock_checked_at).date() if snapshot.unlock_checked_at is not None else None,
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


__all__ = [
    "CompanyEvidenceReadPort",
    "CompanyFinancialScoringReadPort",
    "PublishedCompanyResearch",
]

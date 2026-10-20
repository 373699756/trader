"""Preload one company publication and qualify its facts against the startup calendar."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.download.domain.company_evidence import CompanyEvidenceSnapshot
from trader.infra.cache_contracts import CanonicalJsonBytes
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.domain.market.epochs import ResearchEpoch
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.quality import FieldQualityState, FieldValue
from trader.recommendation.domain.market.research import (
    AnnouncementRiskVerified,
    CorporateRiskFactsFromAnnouncements,
    FinancialReport,
    ResearchAnnouncement,
    ResearchObservation,
)
from trader.recommendation.infra.market_data.research_load_status import ResearchComponentCoverage, ResearchLoaderStatus


class CompanyEvidenceReadPort(Protocol):
    def publication_version(self) -> str: ...

    def visit(self, codes: Sequence[str], consume: Callable[[CompanyEvidenceSnapshot], None]) -> None: ...


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
        **options: Unpack[_CompanyInputOptions],
    ) -> None:
        self._reader = reader
        self._wall_clock = wall_clock
        self._open_dates = open_dates
        self._monotonic = monotonic
        self._reserve_bytes = options.get("reserve_bytes")
        self._source_versions = options.get("source_versions")
        self._config_version = options.get("config_version", "company")
        self._initialization_lock = threading.Lock()
        self._lock = threading.RLock()
        self._epoch: ResearchEpoch | None = None
        self._last_error = "company_not_loaded"
        self._publication_version = ""
        self._disk_version = ""
        self._initialized = False

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
        deadline = self._monotonic() + 30.0
        observed_at = ShanghaiNow(self._wall_clock())
        for _ in range(3):
            rows: dict[str, ResearchObservation] = {}

            try:
                before = self._reader.publication_version()
                for offset in range(0, len(requested), 64):
                    if cancelled() or self._monotonic() >= deadline:
                        self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                        return
                    self._reader.visit(
                        requested[offset : offset + 64], partial(_capture_published_observation, rows, observed_at)
                    )
                after = self._reader.publication_version()
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self._last_error = f"company_snapshot:{type(exc).__name__}"
                return
            if cancelled() or self._monotonic() >= deadline:
                self._last_error = "company_preload_cancelled" if cancelled() else "company_preload_deadline"
                return
            if before != after:
                continue
            self._adopt_projection(rows, observed_at, after, cancelled, deadline)
            return
        self._last_error = "company_publication_changed"

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
        size_bytes = len(CanonicalJsonBytes(epoch))
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

    def observe_publication(self) -> None:
        try:
            version = self._reader.publication_version()
        except (OSError, RuntimeError, ValueError):
            with self._lock:
                self._last_error = "company_publication_unavailable"
            return
        with self._lock:
            self._disk_version = version
            if self._last_error == "company_publication_unavailable":
                self._last_error = ""

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
            for code in codes:
                original = self._epoch.observations.get(code) if self._epoch is not None else None
                if original is not None and original.snapshot_at is not None and original.snapshot_at > now:
                    original = None
                item = _qualified(original, now, required) if original is not None else None
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

    def status(self) -> ResearchLoaderStatus:
        now = ShanghaiNow(self._wall_clock())
        required = self._required_coverage(now)
        with self._lock:
            rows = tuple(
                _qualified(item, now, required)
                for item in (self._epoch.observations.values() if self._epoch is not None else ())
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
                last_error=self._last_error,
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
                loaded_identity=self._publication_version,
                disk_identity=self._disk_version,
                restart_required=self._publication_version != self._disk_version,
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


def _capture_published_observation(
    rows: dict[str, ResearchObservation], observed_at: datetime, snapshot: CompanyEvidenceSnapshot
) -> None:
    if snapshot.published_at <= observed_at:
        rows[snapshot.code] = _observation(snapshot, observed_at)


def _action_ready(item: ResearchObservation) -> bool:
    return (
        item.financial_history_complete
        and item.corporate_risk_window_complete
        and item.pledge_ratio_pct is not None
        and item.unlock_ratio_pct is not None
        and not any(not failure.startswith("news:") for failure in item.source_errors)
    )


def _qualified(item: ResearchObservation, now: datetime, required: date | None) -> ResearchObservation:
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
        corporate_risk_window_complete=False,
        pledge_ratio_pct=None,
        unlock_ratio_pct=None,
        news_queried=False,
        source_errors=tuple(sorted(set((*item.source_errors, "risk_coverage:stale")))),
    )


def _observation(snapshot: CompanyEvidenceSnapshot, now: datetime) -> ResearchObservation:
    announcements = tuple(
        ResearchAnnouncement(
            item.disclosure.title,
            ShanghaiNow(item.disclosure.published_at),
            item.disclosure.identity,
            "issuer_disclosure",
            item.text,
            item.checked,
        )
        for item in snapshot.announcements
        if now - timedelta(days=365) <= item.disclosure.published_at <= now
    )
    reports = tuple(
        FinancialReport(
            item.report_date,
            ShanghaiNow(item.published_at),
            item.basic_eps,
            item.book_value_per_share,
            item.revenue_growth_pct,
            item.net_profit_growth_pct,
            item.core_profit_growth_pct,
            item.roe_pct,
            item.parent_net_profit,
            item.core_net_profit,
        )
        for item in snapshot.financials
        if item.published_at <= now and item.report_date <= now.date()
    )
    evidence = tuple(
        Evidence(
            item.identity,
            "news",
            item.title[:240],
            item.source,
            ShanghaiNow(item.published_at),
            ShanghaiNow(snapshot.published_at),
            item.digest,
        )
        for item in snapshot.news
        if now - timedelta(days=7) <= item.published_at <= now
    ) + tuple(
        Evidence(
            item.announcement_id,
            "regulatory_filing",
            item.title[:240],
            item.source,
            item.published_at,
            ShanghaiNow(snapshot.published_at),
            snapshot.version,
        )
        for item in announcements
        if item.body_checked
    )
    failures = snapshot.failures
    return ResearchObservation(
        data_version=snapshot.version,
        snapshot_at=ShanghaiNow(snapshot.published_at),
        loaded_at=now,
        queried_on=snapshot.window_end,
        pledge_checked_on=snapshot.pledge.day if snapshot.pledge else None,
        financial=max(reports, key=lambda item: (item.report_date, item.published_at)) if reports else None,
        financial_history=reports,
        financial_history_complete=bool(reports) and not any(item.startswith("financial:") for item in failures),
        announcements=tuple(replace(item, body="", body_checked=False) for item in announcements),
        announcements_available=bool(announcements) or snapshot.announcement_complete,
        pledge_ratio_pct=snapshot.pledge.ratio_pct
        if snapshot.pledge
        and snapshot.pledge.day <= now.date()
        and not any(item.startswith("pledge:") for item in failures)
        else None,
        unlock_ratio_pct=min(100.0, sum(item.ratio_pct for item in snapshot.unlocks))
        if snapshot.unlock_checked and not any(item.startswith("unlock:") for item in failures)
        else None,
        unlock_window_start=snapshot.window_end,
        unlock_window_end=snapshot.window_end + timedelta(days=90),
        corporate_risk_facts=CorporateRiskFactsFromAnnouncements(announcements),
        corporate_risk_window_complete=snapshot.announcement_complete
        and all(
            AnnouncementRiskVerified(item, tuple(row.announcement_id for row in announcements if row.announcement_id))
            for item in announcements
        ),
        risk_window_start=snapshot.window_start,
        risk_window_end=snapshot.window_end,
        corporate_risk_evidence_version=snapshot.version,
        news_queried=snapshot.news_checked,
        evidence=evidence,
        source_errors=failures,
    )


__all__ = ["CompanyEvidenceReadPort", "PublishedCompanyResearch"]

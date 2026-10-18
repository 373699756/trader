"""Read published company evidence; recommendation loads never call suppliers."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Protocol

from trader.download.domain.company_evidence import (
    CompanyEvidenceRequest,
    CompanyEvidenceSnapshot,
    CompanyEvidenceSyncResult,
)
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.research import (
    AnnouncementRiskVerified,
    CorporateRiskFactsFromAnnouncements,
    FinancialReport,
    ResearchAnnouncement,
    ResearchObservation,
)
from trader.recommendation.infra.market_data.research_load_status import (
    ResearchComponentCoverage,
    ResearchLoaderStatus,
    ResearchLoadReport,
)


class CompanyEvidenceReadPort(Protocol):
    def visit(self, codes: Sequence[str], consume: Callable[[CompanyEvidenceSnapshot], None]) -> None: ...


class CompanyEvidenceRefreshPort(Protocol):
    def __call__(self, request: CompanyEvidenceRequest) -> CompanyEvidenceSyncResult: ...


class PublishedCompanyResearch:
    def __init__(
        self,
        reader: CompanyEvidenceReadPort,
        *,
        wall_clock: Callable[[], datetime],
        refresh: CompanyEvidenceRefreshPort | None = None,
    ) -> None:
        self._reader = reader
        self._wall_clock = wall_clock
        self._refresh = refresh
        self._lock = threading.RLock()
        self._observations: Mapping[str, ResearchObservation] = MappingProxyType({})
        self._candidate_codes: tuple[str, ...] = ()
        self._last_error = ""
        self._refresh_error = ""
        self._missing: frozenset[str] = frozenset()

    def cached(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
        as_of: datetime | None = None,
    ) -> Mapping[str, ResearchObservation]:
        now = as_of if as_of is not None else self._wall_clock()
        with self._lock:
            missing = tuple(code for code in codes if code not in self._observations and code not in self._missing)
        if missing:
            self.load_report(missing, now, include_structured=include_structured)
        with self._lock:
            today = now.date()
            if action_restrictions is not None:
                for code in codes:
                    item = self._observations.get(code)
                    if (
                        item is None
                        or item.source_errors
                        or item.queried_on != today
                        or (item.snapshot_at is not None and item.snapshot_at > now)
                    ):
                        action_restrictions.setdefault(code, set()).add("research_data_degraded")
            return MappingProxyType(
                {
                    code: _aged_observation(item, today)
                    for code in codes
                    if (item := self._observations.get(code)) is not None
                    and (not fresh_only or item.queried_on == today)
                    and (item.snapshot_at is None or item.snapshot_at <= now)
                }
            )

    def load(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, ResearchObservation]:
        # This call receives the current candidate/reserve population, but only records it in memory.
        with self._lock:
            self._candidate_codes = tuple(sorted(set(self._candidate_codes).union(codes)))
        return self.cached(
            codes, include_structured=include_structured, action_restrictions=action_restrictions, as_of=observed_at
        )

    def load_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> ResearchLoadReport:
        observations: dict[str, ResearchObservation] = {}

        def consume(snapshot: CompanyEvidenceSnapshot) -> None:
            if snapshot.published_at <= observed_at:
                observations[snapshot.code] = _observation(snapshot, observed_at)

        try:
            self._reader.visit(codes, consume)
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._last_error = f"company_snapshot:{type(exc).__name__}"
                self._missing = self._missing.union(code for code in codes if code not in self._observations)
                existing = MappingProxyType(
                    {
                        code: replace(
                            _aged_observation(self._observations[code], observed_at.date()),
                            corporate_risk_window_complete=False,
                            financial_history_complete=False,
                            source_errors=tuple(
                                sorted(set((*self._observations[code].source_errors, self._last_error)))
                            ),
                        )
                        for code in codes
                        if code in self._observations
                    }
                )
                self._observations = MappingProxyType({**self._observations, **existing})
            return ResearchLoadReport(existing, deferred_codes=tuple(codes))
        with self._lock:
            changed = tuple(code for code, item in observations.items() if self._observations.get(code) != item)
            unavailable = {
                code: replace(
                    _aged_observation(self._observations[code], observed_at.date()),
                    corporate_risk_window_complete=False,
                    financial_history_complete=False,
                    source_errors=tuple(
                        sorted(set((*self._observations[code].source_errors, "company_snapshot:missing")))
                    ),
                )
                for code in codes
                if code not in observations and code in self._observations
            }
            self._observations = MappingProxyType({**self._observations, **unavailable, **observations})
            self._missing = self._missing.difference(observations).union(set(codes).difference(observations))
            self._last_error = ""
        if action_restrictions is not None:
            for code in codes:
                item = observations.get(code)
                if item is None or item.source_errors:
                    action_restrictions.setdefault(code, set()).add("research_data_degraded")
        return ResearchLoadReport(
            MappingProxyType(observations), changed, tuple(code for code in codes if code not in observations)
        )

    def refresh(self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None) -> None:
        # Only the independent 09:15/12:45 background job invokes this injected download use case.
        if self._refresh is None:
            return
        budget = min(120.0, (deadline - self._wall_clock()).total_seconds()) if deadline else 120.0
        if budget <= 0:
            return
        with self._lock:
            news_codes = self._candidate_codes
        result = self._refresh(CompanyEvidenceRequest(tuple(codes), observed_at, news_codes, budget))
        with self._lock:
            self._refresh_error = ";".join(result.failures)
            self._candidate_codes = ()

    def status(self) -> ResearchLoaderStatus:
        with self._lock:
            rows = tuple(_aged_observation(item, self._wall_clock().date()) for item in self._observations.values())
            coverages = tuple(ResearchComponentCoverage(item) for item in rows)
            covered = tuple(sum(coverage[index] for coverage in coverages) for index in range(4))
            verified = sum(all(item) for item in coverages)
            partial = sum(any(item) and not all(item) for item in coverages)
            latest = max((row.snapshot_at for row in rows if row.snapshot_at is not None), default=None)
            return ResearchLoaderStatus(
                entries=len(rows),
                success_count=len(rows),
                error_count=sum(bool(item.source_errors) for item in rows),
                planned_count=len(rows),
                timeout_count=0,
                consecutive_failures=0,
                circuit_open=False,
                latencies_ms=(),
                latest_source_time=latest,
                last_error=self._last_error or self._refresh_error,
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
            )


def _aged_observation(observation: ResearchObservation, today: date) -> ResearchObservation:
    if observation.queried_on is not None and observation.queried_on >= today:
        return observation
    return replace(
        observation,
        corporate_risk_window_complete=False,
        financial_history_complete=False,
        pledge_ratio_pct=None,
        unlock_ratio_pct=None,
        news_queried=False,
        source_errors=tuple(sorted(set((*observation.source_errors, "company_snapshot:stale")))),
    )


def _observation(snapshot: CompanyEvidenceSnapshot, now: datetime) -> ResearchObservation:
    announcements = tuple(
        ResearchAnnouncement(
            item.disclosure.title,
            item.disclosure.published_at,
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
            item.published_at,
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
        if item.published_at <= now
    )
    evidence = tuple(
        Evidence(
            item.identity, "news", item.title[:240], item.source, item.published_at, snapshot.published_at, item.digest
        )
        for item in snapshot.news
        if now - timedelta(days=7) <= item.published_at <= now
    )
    evidence += tuple(
        Evidence(
            item.announcement_id,
            "regulatory_filing",
            item.title[:240],
            item.source,
            item.published_at,
            snapshot.published_at,
            snapshot.version,
        )
        for item in announcements
        if item.body_checked
    )
    fresh = snapshot.window_end >= now.date()
    failures = snapshot.failures + (() if fresh else ("company_snapshot:stale",))
    observation = ResearchObservation(
        data_version=snapshot.version,
        snapshot_at=snapshot.published_at,
        queried_on=snapshot.published_at.date(),
        pledge_checked_on=snapshot.pledge.day if snapshot.pledge else None,
        financial=max(reports, key=lambda item: (item.report_date, item.published_at)) if reports else None,
        financial_history=reports,
        financial_history_complete=bool(reports)
        and fresh
        and not any(item.startswith("financial:") for item in failures),
        announcements=tuple(replace(item, body="", body_checked=False) for item in announcements),
        announcements_available=bool(announcements) or snapshot.announcement_complete,
        pledge_ratio_pct=snapshot.pledge.ratio_pct
        if snapshot.pledge and fresh and not any(item.startswith("pledge:") for item in failures)
        else None,
        unlock_ratio_pct=min(100.0, sum(item.ratio_pct for item in snapshot.unlocks))
        if snapshot.unlock_checked and fresh
        else None,
        corporate_risk_facts=CorporateRiskFactsFromAnnouncements(announcements),
        corporate_risk_window_complete=snapshot.announcement_complete
        and fresh
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
    # Partial window rows are still useful evidence; availability is not the same as complete risk coverage.
    return observation


__all__ = ["CompanyEvidenceReadPort", "CompanyEvidenceRefreshPort", "PublishedCompanyResearch"]

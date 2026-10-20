"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.pipeline.static_market.static_pipeline import StaticMarketPipeline
from trader.recommendation.application.ports.eligibility import IssuerEligibilityPort
from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.application.ports.market_data import (
    FullMarketFeatureBatch,
    MarketDataUnavailableError,
    MarketSnapshotMetadata,
)
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityFact,
    IssuerEligibilityReason,
)
from trader.recommendation.domain.market.models import (
    Board,
    FeatureSnapshot,
    LiveQuote,
    MarketQuote,
)
from trader.recommendation.domain.market.refresh import IntradayRefreshResult, ResearchRefreshResult
from trader.recommendation.infra.market_data.candidate_quote_cache import QuoteCache
from trader.recommendation.infra.market_data.intraday_loader import IntradayLoader
from trader.recommendation.infra.market_data.market_cache_identity import (
    _history_population_codes,
    _normalize_codes,
    _reference_epoch,
    _research_data_version,
)
from trader.recommendation.infra.market_data.market_data_health import MarketDataHealth
from trader.recommendation.infra.market_data.market_task_runner import MarketTaskRunner
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.research_load_status import ResearchComponentCoverage, ResearchLoadReport
from trader.recommendation.infra.market_data.tushare_reference_loader import ReferenceLoader
from trader.training.domain.evaluation.models import OutcomeBar


@dataclass(frozen=True)
class MarketFeatureDependencies:
    quotes: QuoteCache
    history: PublishedHistoryCache
    research: PublishedCompanyResearch
    intraday: IntradayLoader
    references: ReferenceLoader
    runner: MarketTaskRunner
    health: MarketDataHealth
    eligibility: IssuerEligibilityPort
    monotonic: Callable[[], float]
    static_pipeline: StaticMarketPipeline


class MarketFeatureService:
    def __init__(
        self,
        dependencies: MarketFeatureDependencies,
    ) -> None:
        self.quotes = dependencies.quotes
        self.history = dependencies.history
        self.research = dependencies.research
        self.intraday = dependencies.intraday
        self.references = dependencies.references
        self.runner = dependencies.runner
        self.health_reporter = dependencies.health
        self.eligibility = dependencies.eligibility
        self._eligibility_lock = threading.Lock()
        self._latest_market_quotes: tuple[MarketQuote, ...] = ()
        self._static_pipeline = dependencies.static_pipeline
        self._monotonic = dependencies.monotonic

    def reference_version(self) -> str:
        """Return the immutable reference epoch used by scoring caches."""

        return _reference_epoch(self.references.versions())

    def risk_universe_codes(self, observed_at: datetime) -> tuple[str, ...]:
        """Return the official non-ST research population without requiring quotes."""
        reference = self.references.static_reference().reference
        if reference is None:
            return ()
        codes = tuple(item.code for item in reference.records if not item.is_st)
        return self._eligible_codes(codes, observed_at)

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        return self.fetch_market_feature_batch(observed_at, force=force, deadline=deadline).features

    def fetch_market_feature_batch(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> FullMarketFeatureBatch:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("market observation time must be timezone aware")
        observed_at = observed_at.astimezone(SHANGHAI)
        self.history.require_projection()
        reference = self.references.static_reference()
        reference_epoch = reference.reference_epoch
        with self._eligibility_lock:
            cached = self.quotes.cached_market_features(force=force, reference_epoch=reference_epoch)
            original_quotes = self._latest_market_quotes
        if cached is not None:
            quotes = original_quotes
            started = self._monotonic()
            eligibility_batch, eligible_codes, static_stages, filtered = self._static_pipeline.observe(
                quotes, reference, observed_at
            )
            eligible_quotes = tuple(quote for quote in quotes if quote.code in eligible_codes)
            cached_codes = tuple(quote.code for quote in eligible_quotes)
            cached_action_restrictions: dict[str, set[str]] = {}
            research = self.research.cached(
                cached_codes,
                include_structured=True,
                action_restrictions=cached_action_restrictions,
            )
            features = (
                self.quotes.build_market_features(
                    eligible_quotes,
                    self.history.cached(
                        cached_codes,
                        observed_at=observed_at,
                        action_restrictions=cached_action_restrictions,
                    ),
                    observed_at,
                    research_observations=research,
                    action_restrictions=cached_action_restrictions,
                )
                if research
                else tuple(feature for feature in cached if feature.quote.code in eligible_codes)
            )
            features = self._apply_issuer_risk_facts(features, observed_at)
            dynamic = self._static_pipeline.dynamic_snapshot(
                filtered,
                features,
                as_of=max(observed_at, self.runner.wall_clock().astimezone(SHANGHAI)),
                latency_ms=self._elapsed_ms(started),
            )
            return FullMarketFeatureBatch(features, eligibility_batch, static_stages, dynamic)
        acquisition_started = self._monotonic()
        quotes = tuple(
            self.runner.run_data_task_until(
                deadline,
                False,
                self.quotes.gateway.fetch_market,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        collection_ms = self._elapsed_ms(acquisition_started)
        static_started = self._monotonic()
        eligibility_batch, eligible_codes, static_stages, filtered = self._static_pipeline.observe(
            quotes, reference, observed_at
        )
        self.quotes.gateway.record_local_latency("static_eligibility", self._elapsed_ms(static_started))
        raw_quotes = quotes
        quotes = tuple(quote for quote in quotes if quote.code in eligible_codes)
        assembly_started = self._monotonic()
        history_codes = _history_population_codes(quotes)
        action_restrictions: dict[str, set[str]] = {}
        histories = self.history.load(
            history_codes,
            deadline=deadline,
            action_restrictions=action_restrictions,
            observed_at=observed_at,
            recover_tail=False,
        )
        self.quotes.gateway.record_local_latency("history_load", self._elapsed_ms(assembly_started))
        self.runner.ensure_before_deadline(deadline)
        feature_started = self._monotonic()
        features = self._apply_issuer_risk_facts(
            self.quotes.build_market_features(
                quotes,
                histories,
                observed_at,
                research_observations=self.research.cached(
                    history_codes,
                    include_structured=True,
                    action_restrictions=action_restrictions,
                ),
                action_restrictions=action_restrictions,
            ),
            observed_at,
        )
        self.quotes.gateway.record_local_latency("feature_build", self._elapsed_ms(feature_started))
        self.runner.ensure_before_deadline(deadline)
        self.history.update_coverage(history_codes, tuple(quote.data_version for quote in quotes))
        with self._eligibility_lock:
            if self.reference_version() != reference_epoch:
                raise MarketDataUnavailableError("reference_changed_during_market_refresh")
            published = self.quotes.publish_market_features(features, reference_epoch=reference_epoch)
            self._latest_market_quotes = raw_quotes
        dynamic = self._static_pipeline.dynamic_snapshot(
            filtered,
            tuple(published),
            as_of=max(observed_at, self.runner.wall_clock().astimezone(SHANGHAI)),
            latency_ms=collection_ms + self._elapsed_ms(assembly_started),
        )
        return FullMarketFeatureBatch(tuple(published), eligibility_batch, static_stages, dynamic)

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = self._eligible_codes(codes, observed_at)
        if not normalized:
            return ()
        action_restrictions: dict[str, set[str]] = {}
        research = self.research.load(
            normalized,
            observed_at,
            include_structured=include_structured_research,
            action_restrictions=action_restrictions,
        )
        if include_structured_research:
            normalized = self._eligible_codes(normalized, observed_at)
            if not normalized:
                return ()
            research = {code: item for code, item in research.items() if code in set(normalized)}
        self.refresh_candidate_quotes(normalized, observed_at)
        quotes = self.quotes.candidate_snapshot(normalized)
        if {quote.code for quote in quotes} != set(normalized):
            self.fetch_market_features(observed_at)
            quotes = self.quotes.candidate_snapshot(normalized)
        histories = self.history.load(normalized, observed_at=observed_at, action_restrictions=action_restrictions)
        intraday = (
            self.intraday.load(
                _board_fair_codes(normalized, quotes),
                observed_at,
                action_restrictions=action_restrictions,
            )
            if include_intraday_tail
            else None
        )
        features = self._apply_issuer_risk_facts(
            self.quotes.build_candidate_features(
                quotes,
                histories,
                observed_at,
                research_observations=research,
                intraday_minutes=intraday,
                action_restrictions=action_restrictions,
            ),
            observed_at,
        )
        if include_intraday_tail:
            self.intraday.record_feature_coverage(normalized, features)
        return features

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        normalized = self._eligible_codes(codes, observed_at)
        if not normalized:
            return ()
        fetched = tuple(
            self.runner.run_data_task_until(
                deadline,
                True,
                self.quotes.gateway.fetch_candidates,
                normalized,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        self.quotes.update_candidate_quotes(fetched, candidate_cycle=observed_at)
        resolved = self.quotes.candidate_snapshot(normalized)
        action_restrictions: dict[str, set[str]] = {}
        return self.quotes.build_candidate_features(
            resolved,
            self.history.load(
                normalized,
                observed_at=observed_at,
                deadline=deadline,
                action_restrictions=action_restrictions,
            ),
            observed_at,
            research_observations=self.research.cached(
                normalized,
                include_structured=True,
                action_restrictions=action_restrictions,
            ),
            intraday_minutes=None,
            action_restrictions=action_restrictions,
        )

    def refresh_topk_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        fetched = tuple(
            self.runner.run_data_task_until(
                deadline,
                True,
                self.quotes.gateway.fetch_topk_quotes,
                normalized,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        self.quotes.update_candidate_quotes(fetched)
        resolved = self.quotes.candidate_snapshot(normalized)
        action_restrictions: dict[str, set[str]] = {}
        return self.quotes.build_candidate_features(
            resolved,
            self.history.cached(
                normalized,
                observed_at=observed_at,
                fresh_only=True,
                action_restrictions=action_restrictions,
            ),
            observed_at,
            research_observations=self.research.cached(
                normalized,
                include_structured=False,
                action_restrictions=action_restrictions,
            ),
            intraday_minutes=None,
            action_restrictions=action_restrictions,
        )

    def refresh_long_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        normalized = self._eligible_codes(codes, observed_at)
        if not normalized:
            return ()
        quotes = tuple(
            self.quotes.gateway.fetch_long_quotes(
                normalized,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        return tuple(
            FeatureSnapshot(
                quote=quote,
                values={},
                observed_at=observed_at,
                missing_fields=tuple(
                    field
                    for field in ("price", "pct_change", "amount", "turnover_rate", "market_cap")
                    if getattr(quote, field) is None
                ),
            )
            for quote in quotes
        )

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        quotes = self.quotes.market_quotes()
        quotes = self._eligible_quotes(quotes, observed_at)
        if not quotes:
            return ()
        action_restrictions: dict[str, set[str]] = {}
        histories = self.history.cached(
            tuple(quote.code for quote in quotes),
            observed_at=observed_at,
            action_restrictions=action_restrictions,
        )
        features = self.quotes.build_market_features(
            quotes,
            histories,
            observed_at,
            action_restrictions=action_restrictions,
        )
        return self.quotes.publish_market_features(features, reference_epoch=self.reference_version())

    def refresh_stock_risk(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> ResearchRefreshResult:
        requested = self._eligible_codes(codes, observed_at)
        started_at = self.runner.wall_clock()
        self.research.refresh(requested, observed_at, deadline=deadline)
        report = self.research.load_report(
            requested,
            observed_at,
            include_structured=True,
        )
        return _research_refresh_result(
            requested,
            report,
            started_at,
            self.runner.wall_clock(),
            require_structured=True,
        )

    def refresh_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        self.references.refresh_reference_data(self._eligible_codes(codes, observed_at), observed_at, force=force)

    def schedule_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        security_master_codes: Sequence[str] | None = None,
    ) -> None:
        eligible = self._eligible_codes(codes, observed_at)
        eligible_master = self._eligible_codes(
            codes if security_master_codes is None else security_master_codes,
            observed_at,
        )
        self.references.schedule_reference_data(
            eligible,
            observed_at,
            force=force,
            security_master_codes=eligible_master,
        )
        self.history.refresh()

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> IntradayRefreshResult:
        return self.intraday.refresh(self._eligible_codes(codes, observed_at), observed_at)

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = self._eligible_codes(codes, observed_at)
        if not normalized:
            return ()
        action_restrictions: dict[str, set[str]] = {}
        histories = self.history.cached(
            normalized,
            observed_at=observed_at,
            fresh_only=True,
            action_restrictions=action_restrictions,
        )
        research = self.research.cached(
            normalized,
            include_structured=include_structured_research,
            action_restrictions=action_restrictions,
        )
        intraday = (
            self.intraday.cached(normalized, action_restrictions=action_restrictions, observed_at=observed_at)
            if include_intraday_tail
            else None
        )
        features = self._apply_issuer_risk_facts(
            self.quotes.build_candidate_features(
                self.quotes.candidate_snapshot(normalized),
                histories,
                observed_at,
                research_observations=research,
                intraday_minutes=intraday,
                action_restrictions=action_restrictions,
            ),
            observed_at,
        )
        if include_intraday_tail:
            self.intraday.record_feature_coverage(normalized, features)
        return features

    def _eligible_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        normalized = _normalize_codes(codes)
        st_reasons = {
            IssuerEligibilityReason.HISTORICAL_ST,
            IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
        }
        excluded = {item.code for item in self.eligibility.exclusions(observed_at) if item.reason in st_reasons}
        return tuple(code for code in normalized if code not in excluded)

    def _apply_issuer_risk_facts(
        self,
        features: Sequence[FeatureSnapshot],
        observed_at: datetime,
    ) -> tuple[FeatureSnapshot, ...]:
        st_reasons = {
            IssuerEligibilityReason.HISTORICAL_ST,
            IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
        }
        reasons = {
            item.code: item.reason
            for item in self.eligibility.exclusions(observed_at)
            if item.reason is not None and item.reason not in st_reasons
        }
        return tuple(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    execution_restrictions=tuple(
                        dict.fromkeys(
                            (*feature.quote.execution_restrictions, f"issuer_risk:{reasons[feature.quote.code].value}")
                        )
                    ),
                ),
            )
            if feature.quote.code in reasons
            else feature
            for feature in features
        )

    def _eligible_quotes(self, quotes: Sequence[MarketQuote], observed_at: datetime) -> tuple[MarketQuote, ...]:
        eligible = set(self._eligible_codes(tuple(quote.code for quote in quotes), observed_at))
        return tuple(quote for quote in quotes if quote.code in eligible)

    def _record_eligibility(self, facts: Sequence[IssuerEligibilityFact]) -> None:
        try:
            self.eligibility.record(facts)
        except RuntimeError:
            return

    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]:
        normalized = _normalize_codes(codes)
        return self.quotes.current_quotes(normalized)

    def cached_quotes(self, codes: Sequence[str]) -> Mapping[str, MarketQuote]:
        """Return the newest cached full quote without performing external I/O."""
        normalized = _normalize_codes(codes)
        market = {quote.code: quote for quote in self.quotes.market_quotes()}
        candidates = self.quotes.candidate_entries()
        result: dict[str, MarketQuote] = {}
        for code in normalized:
            available = tuple(quote for quote in (market.get(code), candidates.get(code)) if quote is not None)
            if available:
                result[code] = max(
                    available,
                    key=lambda quote: (quote.source_time, quote.received_time, quote.data_version),
                )
        return result

    def read_outcome_bars(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[OutcomeBar, ...]]:
        return self.history.read_outcome_bars(_normalize_codes(codes), observed_at)

    def health(self) -> JsonObject:
        return self.health_reporter.health()

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> MarketSnapshotMetadata:
        return self.health_reporter.snapshot_metadata(codes)


def _research_refresh_result(
    requested: tuple[str, ...],
    report: ResearchLoadReport,
    started_at: datetime,
    completed_at: datetime,
    *,
    require_structured: bool,
) -> ResearchRefreshResult:
    if any(value.tzinfo is None or value.utcoffset() is None for value in (started_at, completed_at)):
        raise ValueError("research refresh timestamps must be timezone-aware")
    observations = report.observations
    deferred = set(report.deferred_codes)
    failed = tuple(
        code
        for code in requested
        if code not in deferred
        and (code not in observations or require_structured and not any(ResearchComponentCoverage(observations[code])))
    )
    failed_set = set(failed)
    completed = tuple(
        code for code in requested if code in observations and code not in deferred and code not in failed_set
    )
    completed_set = set(completed)
    if require_structured:
        covered = tuple(code for code in completed if all(ResearchComponentCoverage(observations[code])))
        partial = tuple(code for code in completed if code not in covered)
    else:
        covered = completed
        partial = ()
    identity_fields = tuple((code, _research_data_version(observations[code])) for code in sorted(observations))
    data_version = f"research-batch:{ContentHash(identity_fields)}"
    return ResearchRefreshResult(
        requested_codes=requested,
        completed_codes=completed,
        changed_codes=tuple(code for code in report.changed_codes if code in completed_set),
        partial_codes=partial,
        failed_codes=failed,
        deferred_codes=report.deferred_codes,
        covered_codes=covered,
        data_version=data_version,
        started_at=started_at.astimezone(SHANGHAI),
        completed_at=completed_at.astimezone(SHANGHAI),
        deadline_reached=report.deadline_reached,
    )


def _board_fair_codes(codes: Sequence[str], quotes: Sequence[MarketQuote]) -> tuple[str, ...]:
    by_code = {quote.code: quote for quote in quotes}
    board_order = (Board.MAIN, Board.CHINEXT, Board.STAR)
    queues = {
        board: deque(code for code in codes if by_code.get(code) is not None and by_code[code].board is board)
        for board in board_order
    }
    trailing = [code for code in codes if code not in by_code or by_code[code].board is Board.UNSUPPORTED]
    ordered: list[str] = []
    while any(queues.values()):
        for board in board_order:
            if queues[board]:
                ordered.append(queues[board].popleft())
    return tuple((*ordered, *trailing))


__all__ = ["MarketFeatureService"]

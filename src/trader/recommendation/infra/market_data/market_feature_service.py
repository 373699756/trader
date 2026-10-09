"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import (
    build_dynamic_market_snapshot,
)
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, stage_output
from trader.recommendation.application.pipeline.static_filter.permanent_filter_service import (
    filter_permanent_eligibility,
)
from trader.recommendation.application.pipeline.static_market.static_market_loader import load_static_market
from trader.recommendation.application.pipeline.static_standardize.normalization_service import normalize_static_market
from trader.recommendation.application.ports.eligibility import IssuerEligibilityPort
from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.application.ports.market_data import (
    FullMarketFeatureBatch,
    MarketDataDeadlineExceededError,
    MarketDataUnavailableError,
    MarketSnapshotMetadata,
)
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    StageReasonAggregate,
)
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityBatch,
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityReasonCount,
    IssuerEligibilityState,
    eligibility_facts_from_quote,
    eligibility_facts_from_research,
)
from trader.recommendation.domain.market.models import (
    Board,
    FeatureSnapshot,
    LiveQuote,
    MarketQuote,
)
from trader.recommendation.domain.market.refresh import ResearchRefreshResult
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.domain.market.static import StaticIssuer, normalize_static_issuer
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
from trader.recommendation.infra.market_data.official_static_reference import StaticReferenceRead
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.research_load_status import ResearchLoadReport, research_component_coverage
from trader.recommendation.infra.market_data.research_observation_loader import ResearchLoader
from trader.recommendation.infra.market_data.static_market_cache import StaticMarketCache
from trader.recommendation.infra.market_data.tushare_reference_loader import ReferenceLoader
from trader.training.domain.evaluation.models import OutcomeBar


@dataclass(frozen=True)
class MarketFeatureDependencies:
    quotes: QuoteCache
    history: PublishedHistoryCache
    research: ResearchLoader
    intraday: IntradayLoader
    references: ReferenceLoader
    runner: MarketTaskRunner
    health: MarketDataHealth
    eligibility: IssuerEligibilityPort
    monotonic: Callable[[], float]


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
        self._static_market = StaticMarketCache()
        self._monotonic = dependencies.monotonic

    def reference_version(self) -> str:
        """Return the immutable reference epoch used by scoring caches."""

        return _reference_epoch(self.references.versions())

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
        reference = self.references.static_reference()
        reference_epoch = reference.reference_epoch
        with self._eligibility_lock:
            cached = self.quotes.cached_market_features(force=force, reference_epoch=reference_epoch)
            original_quotes = self._latest_market_quotes
        if cached is not None:
            cached_features = tuple(cached)
            quotes = original_quotes
            started = self._monotonic()
            eligibility_batch, eligible_codes, static_stages, filtered = self._static_pipeline(
                quotes, reference, observed_at
            )
            features = tuple(feature for feature in cached_features if feature.quote.code in eligible_codes)
            dynamic = build_dynamic_market_snapshot(
                filtered, features, as_of=observed_at, latency_ms=self._elapsed_ms(started)
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
        eligibility_batch, eligible_codes, static_stages, filtered = self._static_pipeline(
            quotes, reference, observed_at
        )
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
        self.runner.ensure_before_deadline(deadline)
        features = self.quotes.build_market_features(
            quotes,
            histories,
            observed_at,
            action_restrictions=action_restrictions,
        )
        self.runner.ensure_before_deadline(deadline)
        self.history.update_coverage(history_codes, tuple(quote.data_version for quote in quotes))
        with self._eligibility_lock:
            if self.reference_version() != reference_epoch:
                raise MarketDataUnavailableError("reference_changed_during_market_refresh")
            published = self.quotes.publish_market_features(features, reference_epoch=reference_epoch)
            self._latest_market_quotes = raw_quotes
        dynamic = build_dynamic_market_snapshot(
            filtered,
            tuple(published),
            as_of=observed_at,
            latency_ms=collection_ms + self._elapsed_ms(assembly_started),
        )
        return FullMarketFeatureBatch(tuple(published), eligibility_batch, static_stages, dynamic)

    def _static_pipeline(
        self,
        quotes: tuple[MarketQuote, ...],
        reference: StaticReferenceRead,
        observed_at: datetime,
    ) -> tuple[
        IssuerEligibilityBatch, frozenset[str], tuple[PipelineStageSnapshot, ...], PipelineStageOutput[StaticIssuer]
    ]:
        observed_at = observed_at.astimezone(SHANGHAI)
        started = self._monotonic()
        read = self._static_market.read(reference, observed_at)
        baseline = read.baseline
        root = f"static:{baseline.identity}:{observed_at.isoformat()}"
        health = baseline.health(
            observed_at, refresh_failed=reference.refresh_failed, ttl_seconds=reference.refresh_ttl_seconds
        )
        reasons: tuple[StageReasonAggregate, ...] = ()
        if not baseline.records:
            reasons = (
                StageReasonAggregate(
                    "static_reference_unavailable", "official population unavailable", 1, Severity.WARNING
                ),
            )
        elif health.state.value != "ready":
            reasons = (
                StageReasonAggregate(
                    "static_reference_degraded", "official population refresh failed or expired", 1, Severity.WARNING
                ),
            )
        source = stage_output(
            PipelineStage.DATA_SOURCE,
            baseline.records,
            input_batch_id=root,
            as_of=observed_at,
            input_count=len(baseline.records),
            reasons=reasons,
            source_health=health,
            latency_ms=0,
        )
        source = self._measured_stage(source, started)
        started = self._monotonic()
        collected = load_static_market(
            source.records,
            input_batch_id=source.snapshot.output_batch_id,
            as_of=observed_at,
            source_health=health,
            expected_count=len(source.records),
            pending_count=0,
            reasons=(
                StageReasonAggregate(
                    "static_baseline_reused" if read.cache_hit else "static_baseline_loaded",
                    "static baseline reused" if read.cache_hit else "static baseline loaded",
                    1,
                    Severity.INFO,
                ),
            )
            if source.records
            else (),
            latency_ms=0,
        )
        collected = self._measured_stage(collected, started)
        started = self._monotonic()
        normalized = normalize_static_market(
            collected,
            normalize_static_issuer,
            as_of=observed_at,
            latency_ms=0,
        )
        normalized = self._measured_stage(normalized, started)
        population = frozenset(item.code for item in normalized.records)
        started = self._monotonic()
        self._record_quote_eligibility(tuple(quote for quote in quotes if quote.code in population), observed_at)
        self._refresh_eligibility_if_due(observed_at)
        exclusions = tuple(item for item in self.eligibility.exclusions(observed_at) if item.code in population)
        exclusions_by_code = {item.code: item for item in exclusions}
        reason_counts = Counter(item.reason for item in exclusions_by_code.values() if item.reason is not None)
        eligible_codes = population.difference(exclusions_by_code)
        decisions = tuple(
            exclusions_by_code[code]
            if code in exclusions_by_code
            else IssuerEligibilityDecision(code, IssuerEligibilityState.ELIGIBLE_UNVERIFIED, observed_at)
            for code in sorted(population)
        )
        filtered = filter_permanent_eligibility(normalized, decisions, as_of=observed_at, latency_ms=0)
        filtered = self._measured_stage(filtered, started)
        batch = IssuerEligibilityBatch(
            input_count=len(population),
            eligible_count=len(eligible_codes),
            reason_counts=tuple(
                IssuerEligibilityReasonCount(reason, reason_counts[reason])
                for reason in sorted(reason_counts, key=lambda item: item.value)
            ),
        )
        return (
            batch,
            frozenset(item.code for item in filtered.records),
            (source.snapshot, collected.snapshot, normalized.snapshot, filtered.snapshot),
            filtered,
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))

    def _measured_stage(
        self, output: PipelineStageOutput[StaticIssuer], started: float
    ) -> PipelineStageOutput[StaticIssuer]:
        return replace(output, snapshot=replace(output.snapshot, latency_ms=self._elapsed_ms(started)))

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
            self._record_research_eligibility(research)
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
        features = self.quotes.build_candidate_features(
            quotes,
            histories,
            observed_at,
            research_observations=research,
            intraday_minutes=intraday,
            action_restrictions=action_restrictions,
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

    def refresh_market_news(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> ResearchRefreshResult:
        requested = self._eligible_codes(codes, observed_at)
        started_at = self.runner.wall_clock()
        report = self.research.load_report(
            requested,
            observed_at,
            include_structured=False,
            force=True,
            deadline=deadline,
        )
        if report.deadline_reached and report.deferred_codes:
            raise MarketDataDeadlineExceededError("research preload exceeded its batch deadline")
        return _research_refresh_result(
            requested,
            report,
            started_at,
            self.runner.wall_clock(),
            require_structured=False,
        )

    def refresh_stock_risk(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> ResearchRefreshResult:
        requested = self._eligible_codes(codes, observed_at)
        started_at = self.runner.wall_clock()
        report = self.research.load_report(
            requested,
            observed_at,
            include_structured=True,
            deadline=deadline,
        )
        self._record_research_eligibility(report.observations)
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

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None:
        self.intraday.load(self._eligible_codes(codes, observed_at), observed_at)

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
            self.intraday.cached(normalized, action_restrictions=action_restrictions) if include_intraday_tail else None
        )
        features = self.quotes.build_candidate_features(
            self.quotes.candidate_snapshot(normalized),
            histories,
            observed_at,
            research_observations=research,
            intraday_minutes=intraday,
            action_restrictions=action_restrictions,
        )
        if include_intraday_tail:
            self.intraday.record_feature_coverage(normalized, features)
        return features

    def _eligible_codes(self, codes: Sequence[str], observed_at: datetime) -> tuple[str, ...]:
        return self.eligibility.filter_codes(_normalize_codes(codes), observed_at)

    def _eligible_quotes(self, quotes: Sequence[MarketQuote], observed_at: datetime) -> tuple[MarketQuote, ...]:
        eligible = set(self._eligible_codes(tuple(quote.code for quote in quotes), observed_at))
        return tuple(quote for quote in quotes if quote.code in eligible)

    def _record_quote_eligibility(self, quotes: Sequence[MarketQuote], observed_at: datetime) -> None:
        eligible = set(self._eligible_codes(tuple(quote.code for quote in quotes), observed_at))
        facts = tuple(
            fact
            for quote in quotes
            if quote.code in eligible
            for fact in eligibility_facts_from_quote(quote, observed_at=observed_at)
        )
        self._record_eligibility(facts)

    def _record_research_eligibility(self, observations: Mapping[str, ResearchObservation]) -> None:
        facts = tuple(
            fact
            for code, observation in observations.items()
            for fact in eligibility_facts_from_research(code, observation)
        )
        self._record_eligibility(facts)

    def _record_eligibility(self, facts: Sequence[IssuerEligibilityFact]) -> None:
        try:
            self.eligibility.record(facts)
        except RuntimeError:
            return

    def _refresh_eligibility_if_due(self, observed_at: datetime) -> None:
        if not self.eligibility.refresh_due(observed_at):
            return
        try:
            self.eligibility.refresh_snapshot(observed_at, source="full_market")
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            self.eligibility.refresh_failed(observed_at, f"eligibility_refresh_failed:{type(exc).__name__.lower()}")

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
    observations = report.observations
    deferred = set(report.deferred_codes)
    failed = tuple(
        code
        for code in requested
        if code not in deferred
        and (
            code not in observations or require_structured and not any(research_component_coverage(observations[code]))
        )
    )
    failed_set = set(failed)
    completed = tuple(
        code for code in requested if code in observations and code not in deferred and code not in failed_set
    )
    completed_set = set(completed)
    if require_structured:
        covered = tuple(code for code in completed if all(research_component_coverage(observations[code])))
        partial = tuple(code for code in completed if code not in covered)
    else:
        covered = completed
        partial = ()
    version_material = "|".join(f"{code}:{_research_data_version(observations[code])}" for code in sorted(observations))
    data_version = (
        f"research-batch:{hashlib.sha256(version_material.encode('utf-8')).hexdigest()[:20]}"
        if version_material
        else "research-batch:empty"
    )
    return ResearchRefreshResult(
        requested_codes=requested,
        completed_codes=completed,
        changed_codes=tuple(code for code in report.changed_codes if code in completed_set),
        partial_codes=partial,
        failed_codes=failed,
        deferred_codes=report.deferred_codes,
        covered_codes=covered,
        data_version=data_version,
        started_at=started_at,
        completed_at=completed_at,
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

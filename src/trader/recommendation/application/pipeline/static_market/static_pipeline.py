"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime

from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import (
    build_dynamic_market_snapshot,
)
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, stage_output
from trader.recommendation.application.pipeline.static_filter.permanent_filter_service import (
    filter_permanent_eligibility,
)
from trader.recommendation.application.pipeline.static_market.static_market_cache import StaticMarketCache
from trader.recommendation.application.pipeline.static_market.static_market_loader import load_static_market
from trader.recommendation.application.pipeline.static_standardize.normalization_service import normalize_static_market
from trader.recommendation.application.ports.eligibility import IssuerEligibilityPort
from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    StageReasonAggregate,
)
from trader.recommendation.domain.market.eligibility import (
    HistoricalStEligibilitySnapshot,
    IssuerEligibilityBatch,
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityReasonCount,
    IssuerEligibilityState,
    eligibility_facts_from_quote,
)
from trader.recommendation.domain.market.models import (
    FeatureSnapshot,
    MarketQuote,
)
from trader.recommendation.domain.market.static import StaticIssuer, normalize_static_issuer


class StaticMarketPipeline:
    """Coordinate source, collection, standardization and permanent eligibility."""

    def __init__(
        self,
        eligibility: IssuerEligibilityPort,
        *,
        monotonic: Callable[[], float],
        historical_st_eligibility: Callable[[], HistoricalStEligibilitySnapshot] | None = None,
    ) -> None:
        self.eligibility = eligibility
        self._monotonic = monotonic
        self._historical_st_eligibility = historical_st_eligibility
        self._static_market = StaticMarketCache()
        self._versions = InputVersionClock()

    def observe(
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
        historical_st = self._historical_st_eligibility() if self._historical_st_eligibility is not None else None
        decisions = tuple(
            exclusions_by_code[code]
            if code in exclusions_by_code
            else IssuerEligibilityDecision(code, self._historical_st_state(code, historical_st), observed_at)
            for code in sorted(population)
        )
        eligible_codes = frozenset(
            decision.code for decision in decisions if decision.state is IssuerEligibilityState.ELIGIBLE_UNVERIFIED
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
            pending_count=sum(decision.state is IssuerEligibilityState.QUALIFICATION_PENDING for decision in decisions),
        )
        return (
            batch,
            frozenset(item.code for item in filtered.records),
            (source.snapshot, collected.snapshot, normalized.snapshot, filtered.snapshot),
            filtered,
        )

    @staticmethod
    def _historical_st_state(code: str, snapshot: HistoricalStEligibilitySnapshot | None) -> IssuerEligibilityState:
        if snapshot is None:
            return IssuerEligibilityState.ELIGIBLE_UNVERIFIED
        if snapshot.status == "ready" and snapshot.eligible(code):
            return IssuerEligibilityState.ELIGIBLE_UNVERIFIED
        return IssuerEligibilityState.QUALIFICATION_PENDING

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))

    def _measured_stage(
        self, output: PipelineStageOutput[StaticIssuer], started: float
    ) -> PipelineStageOutput[StaticIssuer]:
        return replace(output, snapshot=replace(output.snapshot, latency_ms=self._elapsed_ms(started)))

    def _record_quote_eligibility(self, quotes: Sequence[MarketQuote], observed_at: datetime) -> None:
        eligible = set(self.eligibility.filter_codes(tuple(quote.code for quote in quotes), observed_at))
        facts = tuple(
            fact
            for quote in quotes
            if quote.code in eligible
            for fact in eligibility_facts_from_quote(quote, observed_at=observed_at)
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

    def dynamic_snapshot(
        self,
        filtered: PipelineStageOutput[StaticIssuer],
        features: tuple[FeatureSnapshot, ...],
        *,
        as_of: datetime,
        latency_ms: int,
    ) -> PipelineStageOutput[FeatureSnapshot]:
        return build_dynamic_market_snapshot(
            filtered,
            features,
            as_of=as_of,
            data_version=self._versions.features("dynamic", features),
            latency_ms=latency_ms,
        )

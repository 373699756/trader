"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import (
    BuildDynamicMarketSnapshot,
)
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, StageOutput
from trader.recommendation.application.pipeline.static_filter.permanent_filter_service import (
    FilterPermanentEligibility,
)
from trader.recommendation.application.pipeline.static_market.static_market_cache import StaticMarketCache
from trader.recommendation.application.ports.eligibility import IssuerEligibilityPort
from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.candidate.filters import HardFilterPolicy
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    StageReasonAggregate,
)
from trader.recommendation.domain.market.eligibility import (
    EligibilityFactsFromQuote,
    EligibilityFactsFromStaticIssuer,
    HistoricalStEligibilitySnapshot,
    IssuerEligibilityBatch,
    IssuerEligibilityDecision,
    IssuerEligibilityFact,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
    IssuerEligibilityState,
    ResolveIssuerEligibility,
)
from trader.recommendation.domain.market.models import (
    FeatureSnapshot,
    MarketQuote,
)
from trader.recommendation.domain.market.research import CompanyRiskValues, LongResearchPolicy, ResearchObservation
from trader.recommendation.domain.market.static import NormalizeStaticIssuer, StaticIssuer


@dataclass(frozen=True)
class StaticCompanyInputs:
    read: Callable[[Sequence[str], datetime], Mapping[str, ResearchObservation]]
    policy: LongResearchPolicy
    hard_filter: HardFilterPolicy


class StaticMarketPipeline:
    """Coordinate source, collection, standardization and permanent eligibility."""

    def __init__(
        self,
        eligibility: IssuerEligibilityPort,
        *,
        monotonic: Callable[[], float],
        historical_st_eligibility: Callable[[], HistoricalStEligibilitySnapshot] | None = None,
        company: StaticCompanyInputs | None = None,
    ) -> None:
        self.eligibility = eligibility
        self._monotonic = monotonic
        self._historical_st_eligibility = historical_st_eligibility
        self._company = company
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
        normalized_records = tuple(
            value for item in baseline.records if (value := NormalizeStaticIssuer(item)) is not None
        )
        companies = (
            self._company.read(tuple(item.code for item in normalized_records), observed_at) if self._company else {}
        )
        source = StageOutput(
            PipelineStage.LOCAL_DATA,
            normalized_records,
            input_batch_id=root,
            as_of=observed_at,
            input_count=len(baseline.records),
            pending_count=len(baseline.records) - len(normalized_records),
            reasons=reasons,
            source_health=health,
            latency_ms=0,
        )
        source = self._measured_stage(source, started)
        source = replace(
            source,
            snapshot=replace(
                source.snapshot,
                operation_durations=(("cache_reuse" if read.cache_hit else "local_read", source.snapshot.latency_ms),),
            ),
        )
        population = frozenset(item.code for item in source.records)
        started = self._monotonic()
        self._record_static_eligibility(source.records, observed_at)
        self._record_quote_eligibility(tuple(quote for quote in quotes if quote.code in population), observed_at)
        self._refresh_eligibility_if_due(observed_at)
        exclusions = tuple(
            item
            for item in self.eligibility.exclusions(observed_at)
            if item.code in population
            and item.reason
            in {
                IssuerEligibilityReason.HISTORICAL_ST,
                IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING,
            }
        )
        exclusions_by_code = {item.code: item for item in exclusions}
        for issuer in source.records:
            facts = EligibilityFactsFromStaticIssuer(issuer, observed_at=observed_at)
            if facts:
                exclusions_by_code.setdefault(issuer.code, ResolveIssuerEligibility(facts, issuer.code, observed_at))
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
        company_exclusions = {
            code: reason
            for code in eligible_codes
            if (reason := self._company_reason(code, companies.get(code), observed_at)) is not None
        }
        filtered = FilterPermanentEligibility(
            source, decisions, as_of=observed_at, latency_ms=0, company_exclusions=company_exclusions
        )
        filtered = self._measured_stage(filtered, started)
        batch = IssuerEligibilityBatch(
            input_count=len(population),
            eligible_count=len(filtered.records),
            company_reasons=tuple(
                StageReasonAggregate(reason, reason.replace("_", " "), count, Severity.WARNING)
                for reason, count in sorted(Counter(company_exclusions.values()).items())
            ),
            reason_counts=tuple(
                IssuerEligibilityReasonCount(reason, reason_counts[reason])
                for reason in sorted(reason_counts, key=lambda item: item.value)
            ),
            pending_count=sum(decision.state is IssuerEligibilityState.QUALIFICATION_PENDING for decision in decisions),
        )
        return (
            batch,
            frozenset(item.code for item in filtered.records),
            (source.snapshot, filtered.snapshot),
            filtered,
        )

    def _company_reason(self, code: str, observation: ResearchObservation | None, at: datetime) -> str | None:
        if self._company is None:
            return None
        if code in self._company.hard_filter.blacklist_codes:
            return "blacklisted"
        if observation is None:
            return None
        values = CompanyRiskValues(observation, at, self._company.policy)
        return next(
            (
                name
                for name, threshold in self._company.hard_filter.structured_risk_thresholds.items()
                if (value := values.get(name)) is not None and value > threshold
            ),
            None,
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
        facts = tuple(fact for quote in quotes for fact in EligibilityFactsFromQuote(quote, observed_at=observed_at))
        self._record_eligibility(facts)

    def _record_static_eligibility(self, issuers: Sequence[StaticIssuer], observed_at: datetime) -> None:
        self._record_eligibility(
            tuple(
                fact for issuer in issuers for fact in EligibilityFactsFromStaticIssuer(issuer, observed_at=observed_at)
            )
        )

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
        except (OSError, RuntimeError, ValueError) as exc:
            self.eligibility.refresh_failed(observed_at, f"eligibility_refresh_failed:{type(exc).__name__.lower()}")

    def dynamic_snapshot(
        self,
        filtered: PipelineStageOutput[StaticIssuer],
        features: tuple[FeatureSnapshot, ...],
        *,
        as_of: datetime,
        latency_ms: int,
    ) -> PipelineStageOutput[FeatureSnapshot]:
        return BuildDynamicMarketSnapshot(
            filtered,
            features,
            as_of=as_of,
            data_version=self._versions.features("dynamic", features),
            latency_ms=latency_ms,
        )

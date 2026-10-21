"""Market-data port coordinator composed from typed, state-owning components."""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from functools import partial

from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.pipeline.dynamic_market.market_snapshot_service import (
    BuildDynamicMarketSnapshot,
)
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, StageOutput
from trader.recommendation.application.pipeline.static_filter.permanent_filter_service import (
    FilterPermanentEligibility,
)
from trader.recommendation.application.pipeline.static_market.static_market_cache import (
    StaticEligibilityInputs,
    StaticEligibilitySnapshot,
    StaticMarketCache,
)
from trader.recommendation.application.ports.eligibility import IssuerEligibilityPort
from trader.recommendation.application.ports.market_data import MarketDataUnavailableError
from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.evidence.pipeline import (
    PipelineStage,
    PipelineStageSnapshot,
    Severity,
    SourceHealth,
    SourceHealthState,
    StageReasonAggregate,
    StageState,
)
from trader.recommendation.domain.market.eligibility import (
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
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.static import NormalizeStaticIssuer, StaticIssuer


class StaticMarketPipeline:
    """Initialize static eligibility once, then project its audit without filtering again."""

    def __init__(
        self,
        eligibility: IssuerEligibilityPort,
        *,
        cache: StaticMarketCache,
        monotonic: Callable[[], float],
        historical_st_eligibility: Callable[[], HistoricalStEligibilitySnapshot] | None = None,
        blacklist_codes: frozenset[str] = frozenset(),
    ) -> None:
        self.eligibility = eligibility
        self._monotonic = monotonic
        self._historical_st_eligibility = historical_st_eligibility
        self.cache = cache
        self._blacklist_codes = blacklist_codes
        self._snapshot: StaticEligibilitySnapshot | None = None
        self._initialization_lock = threading.Lock()
        self._versions = InputVersionClock()

    def initialize(self, reference: StaticReferenceRead, observed_at: datetime) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("static initialization time must be timezone aware")
        with self._initialization_lock:
            if self._snapshot is not None:
                return
            inputs = StaticEligibilityInputs(
                reference,
                self._historical_st_eligibility() if self._historical_st_eligibility is not None else None,
                self._blacklist_codes,
            )
            self._snapshot = self.cache.initialize(
                inputs, partial(self._build, observed_at=observed_at.astimezone(SHANGHAI))
            )

    def observe(
        self,
        observed_at: datetime,
        *,
        refresh_failed: bool = False,
    ) -> tuple[
        IssuerEligibilityBatch, frozenset[str], tuple[PipelineStageSnapshot, ...], PipelineStageOutput[StaticIssuer]
    ]:
        snapshot = self._snapshot
        if snapshot is None:
            raise MarketDataUnavailableError("static_eligibility_not_initialized")
        if observed_at < snapshot.source.snapshot.as_of:
            raise MarketDataUnavailableError("static_eligibility_from_future")
        health = _source_health(
            replace(snapshot.inputs.reference, refresh_failed=True) if refresh_failed else snapshot.inputs.reference,
            observed_at,
            accepted_at=snapshot.source.snapshot.as_of,
        )
        stages = tuple(_reused_stage(output.snapshot, health) for output in (snapshot.source, snapshot.filtered))
        return snapshot.eligibility, snapshot.eligible_codes, stages, replace(snapshot.filtered, snapshot=stages[1])

    def _build(self, inputs: StaticEligibilityInputs, *, observed_at: datetime) -> StaticEligibilitySnapshot:
        started = self._monotonic()
        reference = inputs.reference
        accepted = reference.reference
        records = accepted.records if accepted is not None and accepted.source_time <= observed_at else ()
        identity = hashlib.sha256(
            repr((inputs.reference, inputs.historical_st, tuple(sorted(inputs.blacklist_codes)))).encode("utf-8")
        ).hexdigest()
        root = f"static:{identity}:{observed_at.isoformat()}"
        health = _source_health(reference, observed_at)
        reasons: tuple[StageReasonAggregate, ...] = ()
        if not records:
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
        normalized_records = tuple(value for item in records if (value := NormalizeStaticIssuer(item)) is not None)
        source = StageOutput(
            PipelineStage.LOCAL_DATA,
            normalized_records,
            input_batch_id=root,
            as_of=observed_at,
            input_count=len(records),
            pending_count=len(records) - len(normalized_records),
            reasons=reasons,
            source_health=health,
            latency_ms=0,
        )
        source = self._measured_stage(source, started)
        source = replace(
            source,
            snapshot=replace(
                source.snapshot,
                operation_durations=(("local_read", source.snapshot.latency_ms),),
            ),
        )
        population = frozenset(item.code for item in source.records)
        started = self._monotonic()
        self._record_static_eligibility(source.records, observed_at)
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
        historical_st = inputs.historical_st
        decisions = tuple(
            exclusions_by_code[code]
            if code in exclusions_by_code
            else IssuerEligibilityDecision(code, self._historical_st_state(code, historical_st), observed_at)
            for code in sorted(population)
        )
        eligible_codes = frozenset(
            decision.code for decision in decisions if decision.state is IssuerEligibilityState.ELIGIBLE_UNVERIFIED
        )
        company_exclusions = {code: "blacklisted" for code in eligible_codes if code in inputs.blacklist_codes}
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
        return StaticEligibilitySnapshot(
            inputs, source, filtered, batch, frozenset(item.code for item in filtered.records)
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


def _source_health(
    reference: StaticReferenceRead, observed_at: datetime, *, accepted_at: datetime | None = None
) -> SourceHealth:
    source = reference.reference
    latest = source.source_time if source is not None and source.source_time <= (accepted_at or observed_at) else None
    latest = latest.astimezone(SHANGHAI) if latest is not None else None
    age = (observed_at - latest).total_seconds() if latest is not None else None
    healthy = age is not None and age < reference.refresh_ttl_seconds and not reference.refresh_failed
    return SourceHealth(
        SourceHealthState.READY if healthy else SourceHealthState.DEGRADED if latest else SourceHealthState.UNAVAILABLE,
        1,
        int(healthy),
        latest,
        age,
    )


def _reused_stage(stage: PipelineStageSnapshot, health: SourceHealth) -> PipelineStageSnapshot:
    state = stage.state
    if stage.output_count and health.state is not SourceHealthState.READY:
        state = StageState.DEGRADED
    return replace(
        stage,
        source_health=health,
        state=state,
        degraded=state is StageState.DEGRADED,
        latency_ms=None,
        operation_durations=(("startup_reuse", None),),
    )

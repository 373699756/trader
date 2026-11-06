"""Offline-only reuse of qualified inputs and complete-board references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import threading
import time
from typing import cast

from trader.infra.cache import BoundedLruCache
from trader.infra.cache_contracts import CacheIdentity, RequestKey
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    FilterFeatureCandidates, ScoredSelectionIdentity, ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import (
    RankingSelectionResult, RankingSelectionService,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.selection.scored_selection import (
    BuildScoredCandidateReferences, FilteredCandidateInputs, RankFilteredCandidates, ScoredCandidateReferences,
    ScorePlannedLocalEvidence,
)

@dataclass(frozen=True)
class ScoringReuseCounts:
    requests: int
    calculations: int
    hits: int
    bypasses: int = 0


class ScoringInputsReuse:
    """Use the existing feature cache budget; no scoring, risks or final seats are cached."""

    def __init__(self, cache: BoundedLruCache) -> None:
        self._cache = cache
        self._baseline = RankingSelectionService()
        self._counts_lock = threading.Lock()
        self._counts = ScoringReuseCounts(0, 0, 0)

    @property
    def counts(self) -> ScoringReuseCounts:
        with self._counts_lock:
            return self._counts

    def _count(self, *, requests: int = 0, calculations: int = 0, hits: int = 0, bypasses: int = 0) -> None:
        with self._counts_lock:
            previous = self._counts
            self._counts = ScoringReuseCounts(
                previous.requests + requests, previous.calculations + calculations,
                previous.hits + hits, previous.bypasses + bypasses,
            )

    def select(
        self,
        features: Sequence[FeatureSnapshot],
        policy: RecommendationPolicy,
        options: ScoredSelectionOptions,
        identity: ScoredSelectionIdentity,
        *,
        execution_gate_reasons: Mapping[str, str] | None = None,
        population_qualifications: FilteredCandidateInputs | None = None,
    ) -> RankingSelectionResult:
        started = time.monotonic()
        self._count(requests=1)
        try:
            digest = CanonicalDigest((
                tuple(features), policy, options, identity, execution_gate_reasons, population_qualifications,
            ))
        except ValueError:
            self._count(bypasses=1)
            return self._baseline.select(
                features, policy, options, identity, execution_gate_reasons=execution_gate_reasons,
                population_qualifications=population_qualifications,
            )
        key = CacheIdentity(
            "quote_factor_inputs", "scoring_inputs_reuse", "complete_population", RequestKey({"input": digest}),
            identity.trade_date.isoformat(), "local_scoring", "qfq", policy.strategy_version, "qualified_board_inputs",
        )
        cached = self._cache.get(key)
        identity_done = time.monotonic()
        calculation_seconds = reference_seconds = 0.0
        calculated = False

        def calculate() -> ScoredCandidateReferences:
            nonlocal calculation_seconds, reference_seconds, calculated
            cached = self._cache.get(key)
            if cached is not None and cached.value is not None:
                return cast(ScoredCandidateReferences, cached.value)
            before = time.monotonic()
            qualified = FilterFeatureCandidates(
                features, policy, options, identity, execution_gate_reasons=execution_gate_reasons,
                population_qualifications=population_qualifications,
            )
            filtered_at = time.monotonic()
            prepared = BuildScoredCandidateReferences(qualified)
            referenced_at = time.monotonic()
            calculation_seconds = filtered_at - before
            reference_seconds = referenced_at - filtered_at
            calculated = True
            self._count(calculations=1)
            self._cache.put(
                key, prepared, data_version=identity.data_version,
                source_time=min((item.quote.source_time for item in features), default=options.evaluated_at),
                received_at=min((item.quote.received_time for item in features), default=options.evaluated_at),
            )
            return prepared

        prepared = (
            cast(ScoredCandidateReferences, cached.value)
            if cached is not None and cached.value is not None
            else cast(ScoredCandidateReferences, self._cache.coalesce(key, calculate))
        )
        prepared_at = time.monotonic()
        if not calculated:
            self._count(hits=1)
        plan = RankFilteredCandidates(prepared)
        ranked = time.monotonic()
        selection = ScorePlannedLocalEvidence(prepared.source.request, plan)
        completed = time.monotonic()
        overhead = (identity_done - started) + max(0.0, prepared_at - identity_done - calculation_seconds - reference_seconds)
        return RankingSelectionResult(selection, (
            ("preparation_identity_and_cache", max(0, int(overhead * 1000))),
            ("candidate_preparation_and_qualification", max(0, int(calculation_seconds * 1000))),
            ("board_reference_build", max(0, int(reference_seconds * 1000))),
            ("candidate_policy_and_ranking", max(0, int((ranked - prepared_at) * 1000))),
            ("local_component_scoring", max(0, int((completed - ranked) * 1000))),
        ))


__all__ = [
    "ScoringInputsReuse",
    "ScoringReuseCounts",
]

"""Current market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from trader.recommendation.application.pipeline.data_source.input_assembly import (
    decision_observed_at as _decision_observed_at,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    merge_overlay_quote as _merge_overlay_quote,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    model_scoring_context as _model_scoring_context,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    overlay_observed_at as _overlay_observed_at,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    selected_quote_features as _selected_quote_features,
)
from trader.recommendation.application.pipeline.data_source.source_quality import (
    decision_failure_code as _decision_failure_code,
)
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    ScoredLocalProjection,
    ScoredReviewProjection,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import (
    LocalScoringContext,
    LocalScoringPort,
    LocalScoringService,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.quality_check.input_quality_service import (
    QualityScoringBatch,
    assess_candidate_input_stage,
)
from trader.recommendation.application.pipeline.quality_check.pipeline_status import (
    build_supply_status,
    update_supply_status_decision,
)
from trader.recommendation.application.pipeline.stage_output import measured_output
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus, ResearchAuditIdentity
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DecisionBuilderPort,
    DecisionUnavailableError,
    ResearchIntent,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.ports.scoring_inputs import AcceptedScoringInputs
from trader.recommendation.domain.publication.decision_identity import (
    DecisionIdentity,
    DecisionOverlay,
    ScoredDecision,
    identity_codes,
)
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class DecisionBuildDependencies:
    policy: RecommendationPolicy
    draft_index: UnifiedDecisionDraftIndex
    now: Callable[[], datetime]
    next_sequence: Callable[[], int] = field(kw_only=True)
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)
    model_scoring: ModelScoringPort | None = None
    local_scoring: LocalScoringPort | None = None
    research_audit_builder: Callable[[ScoredLocalProjection, ScoredDecision], ResearchAuditIdentity | None] = (
        lambda _projection, _decision: None
    )


class LocalDecisionBuilder(DecisionBuilderPort):
    """Own scoring, review parentage and research projections independently of input caches."""

    def __init__(
        self,
        inputs: AcceptedScoringInputs,
        *,
        config_version: str,
        candidate_pool_size: int,
        dependencies: DecisionBuildDependencies,
    ) -> None:
        self._inputs = inputs
        self._config_version = config_version
        self._candidate_pool_size = max(1, candidate_pool_size)
        self._policy = dependencies.policy
        self._draft_index = dependencies.draft_index
        self._now = dependencies.now
        self._monotonic = dependencies.monotonic
        self._research_audit_builder = dependencies.research_audit_builder
        self._model_scoring = dependencies.model_scoring
        self._local_scoring = dependencies.local_scoring or LocalScoringService(self._model_scoring)
        self._lock = threading.RLock()
        self._projections: dict[str, ScoredLocalProjection] = {}
        self._decisions: dict[str, ScoredDecision] = {}
        self._next_sequence = dependencies.next_sequence

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        draft = self._draft_index.snapshot(strategy)
        return draft is not None and draft.trade_date == trade_date

    def build_local(self, request: CycleRequest) -> DecisionIdentity | None:
        if request.strategy is Strategy.LONG:
            return None
        batch = self._inputs.batch(request)
        expected_quality = self._inputs.quality(request.strategy)
        sequence = self._next_sequence()
        if batch is None:
            raise DecisionUnavailableError("current native input is unavailable")
        if batch.candidate_stage is None:
            raise DecisionUnavailableError("candidate observations are unavailable")
        try:
            evaluated_at = _decision_observed_at(batch)
            started = self._monotonic()
            quality_stage = assess_candidate_input_stage(
                batch.candidate_stage,
                as_of=evaluated_at,
                minimum_history_sessions=(
                    self._model_scoring.history_required_sessions(request.strategy)
                    if self._model_scoring is not None
                    and request.phase != "close_fallback"
                    and self._model_scoring.uses_model(request.strategy)
                    else 20
                ),
                latency_ms=0,
                hard_filter=self._policy.hard_filter,
            )
            quality_stage = measured_output(quality_stage, started, self._monotonic)
            native_input = (TomorrowNativeInput if request.strategy is Strategy.TOMORROW else D25NativeInput)(
                batch.request.trade_date,
                batch.request.phase,
                batch.data_version,
                self._config_version,
                evaluated_at,
                batch.market_features,
                batch.requested_codes,
                batch.candidate_features,
                30.0,
                30.0,
                self._candidate_pool_size,
            )
            projection = self._local_scoring.score(
                native_input,
                self._policy,
                sequence=sequence,
                context=LocalScoringContext(
                    model_context=_model_scoring_context(request, batch, self._now()),
                    candidate_stage_counts=batch.candidate_stage_counts,
                    preselection_transient_invalid=batch.preselection_transient_invalid,
                    quality_batch=QualityScoringBatch(quality_stage, native_input),
                    monotonic=self._monotonic,
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise DecisionUnavailableError(_decision_failure_code(exc)) from exc
        quality_status = build_supply_status(
            projection,
            batch.candidate_stage_counts,
            candidate_quote_eligible=batch.candidate_quote_eligible,
            candidate_score_threshold=self._policy.selection.candidate_min_score,
            input_stages=(*batch.input_stages, quality_stage.snapshot),
        )
        projection = replace(
            projection,
            local=replace(projection.local, pipeline=quality_status.pipeline),
        )
        self._inputs.replace_quality(request.strategy, expected_quality, quality_status)
        if not projection.input_quality.publishable:
            self._draft_index.publish(projection.local)
            raise DecisionUnavailableError(projection.input_quality.status)
        with self._lock:
            self._projections[projection.local.version] = projection
            self._decisions[projection.local.version] = projection.local
            self._trim_research_sources()
        return projection.local

    def input_quality_status(self) -> tuple[InputQualityStatus, ...]:
        return self._inputs.input_quality_status()

    def projection(self, version: str) -> ScoredLocalProjection | None:
        with self._lock:
            return self._projections.get(version)

    def register_review(
        self,
        projection: ScoredLocalProjection,
        review: ScoredReviewProjection,
    ) -> ScoredDecision | None:
        decision = review.decision or projection.local
        with self._lock:
            current_quality = self._inputs.quality(decision.strategy)
            if (
                current_quality is not None
                and current_quality.summary.trade_date == decision.trade_date
                and current_quality.stage_snapshots[9] == projection.stages.local_score.snapshot
            ):
                expected_quality = current_quality
                current_quality = update_supply_status_decision(
                    current_quality,
                    projection,
                    decision,
                    candidate_score_threshold=self._policy.selection.candidate_min_score,
                    scored_stages=review.stages,
                )
                decision = replace(decision, pipeline=current_quality.pipeline)
                self._inputs.replace_quality(decision.strategy, expected_quality, current_quality)
            if review.decision is None:
                return None
            self._projections[decision.version] = projection
            self._decisions[decision.version] = decision
            self._trim_research_sources()
            return decision

    def research_audit_factory(self, version: str) -> Callable[[], ResearchAuditIdentity | None]:
        with self._lock:
            projection = self._projections.get(version)
            decision = self._decisions.get(version)
        if projection is None or decision is None:
            return lambda: None
        return lambda: self._research_audit_builder(projection, decision)

    def research_intent(self, decision: ScoredDecision) -> ResearchIntent:
        with self._lock:
            projection = self._projections.get(decision.version)
        if projection is None:
            raise DecisionUnavailableError("research projection is unavailable")
        candidates = projection.native_input.requested_codes
        selected = sorted((item for item in decision.items if item.selected), key=lambda item: item.rank)
        remaining = tuple(item for item in decision.items if not item.selected)
        priority = tuple(dict.fromkeys(item.code for item in (*selected, *remaining)))
        return ResearchIntent(decision.strategy, decision.trade_date, priority, candidates)

    def initial_overlay(self, decision: ScoredDecision) -> DecisionOverlay:
        quotes = tuple(item.quote for item in decision.items if item.selected and item.quote is not None)
        selected_count = sum(item.selected for item in decision.items)
        if len(quotes) != selected_count:
            raise DecisionUnavailableError("decision_quote_unavailable")
        return DecisionOverlay(
            strategy=decision.strategy,
            trade_date=decision.trade_date,
            parent_version=decision.version,
            observed_at=decision.observed_at,
            quotes=quotes,
            sequence=self._next_sequence(),
        )

    def refreshed_overlay(
        self,
        decision: ScoredDecision,
        request: CycleRequest,
        previous: DecisionOverlay | None,
    ) -> DecisionOverlay | None:
        if decision.trade_date != request.trade_date:
            return None
        if previous is not None and previous.parent_version != decision.version:
            return None
        if request.phase == "quote_overlay":
            topk = self._inputs.topk_batch()
            if topk is None or topk.observed_at != request.observed_at:
                raise DecisionUnavailableError("topk quote batch is unavailable")
            features_by_code = {feature.quote.code: feature for feature in topk.features}
        else:
            batch = self._inputs.batch(request)
            if batch is None:
                return None
            features_by_code = _selected_quote_features(batch, identity_codes(decision))
        selected_codes = frozenset(identity_codes(decision))
        features_by_code = {code: feature for code, feature in features_by_code.items() if code in selected_codes}
        observed_at = _overlay_observed_at(request, tuple(features_by_code.values()))
        if previous is not None and previous.observed_at > observed_at:
            return None
        quotes = {quote.code: quote for quote in previous.quotes} if previous is not None else {}
        changed = False
        for feature in features_by_code.values():
            changed = _merge_overlay_quote(quotes, feature, observed_at) or changed
        if not changed:
            return None
        return DecisionOverlay(
            strategy=decision.strategy,
            trade_date=decision.trade_date,
            parent_version=decision.version,
            observed_at=observed_at,
            quotes=tuple(quotes.values()),
            sequence=self._next_sequence(),
        )

    def _trim_research_sources(self) -> None:
        while len(self._decisions) > 64:
            version = next(iter(self._decisions))
            self._decisions.pop(version, None)
            self._projections.pop(version, None)

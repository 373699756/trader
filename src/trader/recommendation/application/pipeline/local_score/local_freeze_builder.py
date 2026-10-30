"""Current market-input and freeze adapters used by the production scheduler."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from trader.recommendation.application.pipeline.data_source.input_assembly import (
    BuildModelScoringContext as _model_scoring_context,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    FreezeObservedAt as _freeze_observed_at,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    MergeOverlayQuote as _merge_overlay_quote,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    OverlayObservedAt as _overlay_observed_at,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    SelectedQuoteFeatures as _selected_quote_features,
)
from trader.recommendation.application.pipeline.data_source.source_quality import (
    FreezeFailureCode as _freeze_failure_code,
)
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    ScoredLocalProjection,
    ScoredReviewProjection,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import (
    LocalScoringContext,
    LocalScoringPort,
    LocalScoringService,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.quality_check.input_quality_service import (
    AssessCandidateInputStage,
    QualityScoringBatch,
)
from trader.recommendation.application.pipeline.quality_check.pipeline_status import (
    BuildSupplyStatus,
    UpdateSupplyStatusFreeze,
)
from trader.recommendation.application.pipeline.stage_output import MeasuredOutput
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus, ResearchAuditIdentity
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    FreezeBuilderPort,
    FreezeUnavailableError,
    ResearchIntent,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.ports.scoring_inputs import AcceptedScoringInputs, ScoringInputIdentity
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.application.runtime.trading_session import SessionGuard, SessionLease, StaleSessionError
from trader.recommendation.domain.publication.freeze_identity import (
    DisplayQuoteCodes,
    FreezeIdentity,
    FreezeOverlay,
    IdentityCodes,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class FreezeBuildDependencies:
    policy: RecommendationPolicy
    draft_index: UnifiedFreezeDraftIndex
    now: Callable[[], datetime]
    next_sequence: Callable[[], int] = field(kw_only=True)
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)
    model_scoring: ModelScoringPort | None = None
    local_scoring: LocalScoringPort | None = None
    research_audit_builder: Callable[[ScoredLocalProjection, ScoredFreeze], ResearchAuditIdentity | None] = (
        lambda _projection, _freeze: None
    )


@dataclass(frozen=True)
class _ReviewInput:
    identity: ScoringInputIdentity
    session: SessionLease | None
    deadline: datetime


class LocalFreezeBuilder(FreezeBuilderPort):
    """Own scoring, review parentage and research projections independently of input caches."""

    def __init__(
        self,
        inputs: AcceptedScoringInputs,
        *,
        config_version: str,
        dependencies: FreezeBuildDependencies,
    ) -> None:
        self._inputs = inputs
        self._config_version = config_version
        self._policy = dependencies.policy
        self._draft_index = dependencies.draft_index
        self._now = dependencies.now
        self._monotonic = dependencies.monotonic
        self._research_audit_builder = dependencies.research_audit_builder
        self._model_scoring = dependencies.model_scoring
        self._local_scoring = dependencies.local_scoring or LocalScoringService(self._model_scoring)
        self._lock = threading.RLock()
        self._projections: dict[str, ScoredLocalProjection] = {}
        self._freezes: dict[str, ScoredFreeze] = {}
        self._review_inputs: dict[str, _ReviewInput] = {}
        self._next_sequence = dependencies.next_sequence

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        draft = self._draft_index.snapshot(strategy)
        return draft is not None and draft.trade_date == trade_date

    def build_local(self, request: CycleRequest) -> FreezeIdentity | None:
        if request.strategy is Strategy.LONG:
            return None
        batch = self._inputs.batch(request)
        expected_quality = self._inputs.quality(request.strategy)
        sequence = self._next_sequence()
        if batch is None:
            raise FreezeUnavailableError("current native input is unavailable")
        if batch.candidate_stage is None:
            raise FreezeUnavailableError("candidate observations are unavailable")
        if batch.quality_identity is None:
            raise FreezeUnavailableError("scoring input identity is unavailable")
        try:
            evaluated_at = _freeze_observed_at(batch)
            started = self._monotonic()
            quality_stage = AssessCandidateInputStage(
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
            quality_stage = MeasuredOutput(quality_stage, started, self._monotonic)
            validation_ms = quality_stage.snapshot.latency_ms
            prepared = batch.candidate_stage.snapshot
            quality_stage = replace(
                quality_stage,
                snapshot=replace(
                    quality_stage.snapshot,
                    latency_ms=(prepared.latency_ms or 0) + (validation_ms or 0),
                    operation_durations=(*prepared.operation_durations, ("validation", validation_ms)),
                ),
            )
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
                preview_context=request.preview_context,
            )
            projection = self._local_scoring.score(
                native_input,
                self._policy,
                sequence=sequence,
                context=LocalScoringContext(
                    model_context=_model_scoring_context(request, batch, self._now(), self._monotonic),
                    candidate_stage_counts=batch.candidate_stage_counts,
                    preselection_transient_invalid=batch.preselection_transient_invalid,
                    quality_batch=QualityScoringBatch(quality_stage, native_input),
                    monotonic=self._monotonic,
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise FreezeUnavailableError(_freeze_failure_code(exc)) from exc
        quality_status = BuildSupplyStatus(
            projection,
            batch.candidate_stage_counts,
            candidate_quote_eligible=batch.candidate_quote_eligible,
            input_stages=(*batch.input_stages[:-1], quality_stage.snapshot),
        )
        quality_status = replace(
            quality_status,
            pipeline=replace(
                quality_status.pipeline,
                elapsed_ms=(
                    max(0, int((self._monotonic() - batch.started_monotonic) * 1000))
                    if batch.started_monotonic is not None
                    else None
                ),
            ),
        )
        projection = replace(
            projection,
            started_monotonic=batch.started_monotonic,
            local=replace(
                projection.local,
                input_versions=(
                    *projection.local.input_versions,
                    ("session_generation", str(batch.quality_identity.generation)),
                    ("source_revision", str(batch.quality_identity.source_revision)),
                ),
                pipeline=quality_status.pipeline,
            ),
        )
        with SessionGuard(request.session), self._lock:
            accepted = self._inputs.replace_quality(
                batch.quality_identity,
                expected_quality,
                quality_status,
                parent_version=projection.local.version,
                session=request.session,
            )
            if not projection.input_quality.publishable:
                if accepted:
                    self._draft_index.publish(projection.local)
                raise FreezeUnavailableError(projection.input_quality.status)
            self._projections[projection.local.version] = projection
            self._freezes[projection.local.version] = projection.local
            if accepted:
                self._review_inputs[projection.local.version] = _ReviewInput(
                    batch.quality_identity, request.session, request.review_deadline
                )
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
    ) -> ScoredFreeze | None:
        with self._lock:
            context = self._review_inputs.get(projection.local.version)
        if context is None:
            return None
        try:
            with SessionGuard(context.session):
                return self._register_current_review(projection, review, context)
        except StaleSessionError:
            return None

    def _register_current_review(
        self, projection: ScoredLocalProjection, review: ScoredReviewProjection, context: _ReviewInput
    ) -> ScoredFreeze | None:
        at = ShanghaiNow(self._now())
        valid_before = (
            context.deadline
            if projection.local.preview_context is not None
            else at.replace(hour=15, minute=0, second=0, microsecond=0)
        )
        if at.date() != context.identity.trade_date or at >= valid_before:
            return None
        freeze = review.freeze or projection.local
        if review.freeze is not None and freeze.parent_version != projection.local.version:
            return None
        current = self._inputs.quality(freeze.strategy)
        if (
            current is None
            or current.identity != context.identity
            or current.parent_version != projection.local.version
        ):
            return None
        quality = UpdateSupplyStatusFreeze(
            current.quality,
            projection,
            freeze,
            scored_stages=review.stages,
        )
        quality = replace(
            quality,
            pipeline=replace(
                quality.pipeline,
                elapsed_ms=(
                    max(0, int((self._monotonic() - projection.started_monotonic) * 1000))
                    if projection.started_monotonic is not None
                    else None
                ),
            ),
        )
        freeze = replace(freeze, pipeline=quality.pipeline)
        if not self._inputs.replace_quality(
            context.identity,
            current,
            quality,
            parent_version=freeze.version,
            session=context.session,
            valid_before=valid_before,
        ):
            return None
        if review.freeze is None:
            return None
        with self._lock:
            self._projections[freeze.version] = projection
            self._freezes[freeze.version] = freeze
            self._trim_research_sources()
        return freeze

    def research_audit_factory(self, version: str) -> Callable[[], ResearchAuditIdentity | None]:
        with self._lock:
            projection = self._projections.get(version)
            freeze = self._freezes.get(version)
        if projection is None or freeze is None:
            return lambda: None
        return lambda: self._research_audit_builder(projection, freeze)

    def research_intent(self, freeze: ScoredFreeze) -> ResearchIntent:
        with self._lock:
            projection = self._projections.get(freeze.version)
        if projection is None:
            raise FreezeUnavailableError("research projection is unavailable")
        candidates = projection.native_input.requested_codes
        selected = sorted((item for item in freeze.items if item.selected), key=lambda item: item.rank)
        remaining = tuple(item for item in freeze.items if not item.selected)
        priority = tuple(dict.fromkeys(item.code for item in (*selected, *remaining)))
        return ResearchIntent(freeze.strategy, freeze.trade_date, priority, candidates)

    def initial_overlay(self, freeze: ScoredFreeze) -> FreezeOverlay:
        quotes = tuple(item.quote for item in freeze.items if item.selected and item.quote is not None)
        selected_count = sum(item.selected for item in freeze.items)
        if len(quotes) != selected_count:
            raise FreezeUnavailableError("freeze_quote_unavailable")
        return FreezeOverlay(
            strategy=freeze.strategy,
            trade_date=freeze.trade_date,
            parent_version=freeze.version,
            observed_at=freeze.observed_at,
            quotes=quotes,
            sequence=self._next_sequence(),
        )

    def refreshed_overlay(
        self,
        freeze: ScoredFreeze,
        request: CycleRequest,
        previous: FreezeOverlay | None,
    ) -> FreezeOverlay | None:
        if freeze.trade_date != request.trade_date:
            return None
        if previous is not None and previous.parent_version != freeze.version:
            return None
        if request.phase == "quote_overlay":
            topk = self._inputs.topk_batch()
            if topk is None or topk.observed_at != request.observed_at:
                raise FreezeUnavailableError("topk quote batch is unavailable")
            features_by_code = {feature.quote.code: feature for feature in topk.features}
        else:
            batch = self._inputs.batch(request)
            if batch is None:
                return None
            features_by_code = _selected_quote_features(batch, IdentityCodes(freeze))
        selected_codes = (
            IdentityCodes(freeze) | DisplayQuoteCodes(freeze)
            if request.phase == "quote_overlay"
            else IdentityCodes(freeze)
        )
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
        return FreezeOverlay(
            strategy=freeze.strategy,
            trade_date=freeze.trade_date,
            parent_version=freeze.version,
            observed_at=observed_at,
            quotes=tuple(quotes.values()),
            sequence=self._next_sequence(),
        )

    def _trim_research_sources(self) -> None:
        # Keep the preceding/current local cuts for each strategy. Running reviews
        # and queued audit factories already own their immutable projection; a
        # late hybrid alias must not extend an obsolete local cut's lifetime.
        retained: dict[Strategy, set[str]] = {}
        for projection in sorted(self._projections.values(), key=lambda item: item.local.sequence, reverse=True):
            versions = retained.setdefault(projection.local.strategy, set())
            if len(versions) < 2:
                versions.add(projection.local.version)
        for version, projection in tuple(self._projections.items()):
            if projection.local.version not in retained[projection.local.strategy]:
                self._projections.pop(version)
                self._freezes.pop(version, None)
                self._review_inputs.pop(version, None)
        while len(self._freezes) > 64:
            version = next(iter(self._freezes))
            self._freezes.pop(version, None)
            self._projections.pop(version, None)
            self._review_inputs.pop(version, None)

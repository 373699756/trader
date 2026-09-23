"""Current market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Protocol

from trader.recommendation.application.long_runtime import LongRuntime
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    SCORED_STRATEGIES,
    CandidatePlanSet,
)
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import (
    CandidateFilteringPort,
    CandidateFilteringService,
)
from trader.recommendation.application.pipeline.final_selection.decision_projection import ScoredLocalProjection
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import (
    LocalScoringContext,
    LocalScoringPort,
    LocalScoringService,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.quality_check.input_quality_service import has_transient_candidate_gap
from trader.recommendation.application.pipeline.quality_check.pipeline_status import (
    build_complete_stage_snapshots,
    build_first_nine_stage_snapshots,
    build_pending_pipeline,
    build_supply_status,
    update_supply_status_decision,
)
from trader.recommendation.application.pipeline.data_source.input_assembly import (
    candidate_batch_is_complete as _candidate_batch_is_complete,
    decision_observed_at as _decision_observed_at,
    merge_overlay_quote as _merge_overlay_quote,
    model_scoring_context as _model_scoring_context,
    overlay_observed_at as _overlay_observed_at,
    quote_order as _quote_order,
    refresh_completed_at as _refresh_completed_at,
    require_codes as _require_codes,
    selected_quote_features as _selected_quote_features,
    task_deadline as _task_deadline,
)
from trader.recommendation.application.pipeline.data_source.input_identity import (
    changed_version_codes as _changed_version_codes,
    data_version as _data_version,
    feature_batch_version as _feature_batch_version,
    quote_versions as _quote_versions,
    stable_digest as _stable_digest,
)
from trader.recommendation.application.pipeline.data_source.source_quality import (
    build_source_stage_output,
    decision_failure_code as _decision_failure_code,
    failure_code as _failure_code,
    uses_fallback as _uses_fallback,
)
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.application.ports.long import LongRefreshRequest
from trader.recommendation.application.ports.market_data import FullMarketFeatureBatch, MarketDataUnavailableError
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus, SupplySummary
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPort,
    DataRefreshUnavailableError,
    DecisionBuilderPort,
    DecisionUnavailableError,
    PipelineTaskRequest,
    RefreshOutcome,
    ResearchIntent,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.domain.market.eligibility import IssuerEligibilityBatch
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.refresh import ResearchRefreshResult
from trader.recommendation.domain.publication.decision_identity import (
    DecisionIdentity,
    DecisionOverlay,
    ScoredDecision,
    identity_codes,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts


@dataclass(frozen=True)
class InputBatch:
    request: CycleRequest
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    data_version: str
    candidate_stage_counts: ScoredCandidateStageCounts | None = None
    candidate_quote_eligible: int = 0
    preselection_transient_invalid: bool = False
    issuer_eligibility: IssuerEligibilityBatch | None = None


@dataclass(frozen=True)
class _SharedInputBatch:
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    candidate_stage_counts: ScoredCandidateStageCounts
    candidate_quote_eligible: int
    preselection_transient_invalid: bool
    issuer_eligibility: IssuerEligibilityBatch


@dataclass(frozen=True)
class _ScoringFeatureBatch:
    features: tuple[FeatureSnapshot, ...]


@dataclass(frozen=True)
class DecisionBuildDependencies:
    long_runtime: LongRuntime
    policy: RecommendationPolicy
    draft_index: UnifiedDecisionDraftIndex
    now: Callable[[], datetime]
    model_scoring: ModelScoringPort | None = None
    candidate_filtering: CandidateFilteringPort | None = None
    local_scoring: LocalScoringPort | None = None
    research_audit_builder: Callable[[ScoredLocalProjection, ScoredDecision], object | None] = (
        lambda _projection, _decision: None
    )


@dataclass(frozen=True)
class _PendingQualityContext:
    observed_at: datetime
    population_count: int
    candidate_plans: CandidatePlanSet
    candidate_feature_counts: dict[Strategy, int]
    primary_blocker: str
    apply_model_eligibility: bool = True
    issuer_eligibility: IssuerEligibilityBatch | None = None


@dataclass(frozen=True)
class _TopKQuoteBatch:
    observed_at: datetime
    features: tuple[FeatureSnapshot, ...]


class MarketReader(Protocol):
    def reference_version(self) -> str: ...

    def fetch_market_feature_batch(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> FullMarketFeatureBatch: ...

    def refresh_topk_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]: ...

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]: ...

    def schedule_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        security_master_codes: Sequence[str] | None = None,
    ) -> None: ...

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]: ...

    def refresh_market_news(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> ResearchRefreshResult: ...

    def refresh_stock_risk(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> ResearchRefreshResult: ...

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None: ...

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...


class MarketDataAdapter(DataRefreshPort, DecisionBuilderPort):
    """Build immutable native inputs for the scheduler."""

    def __init__(
        self,
        market: MarketReader,
        *,
        config_version: str,
        candidate_pool_size: int,
        decision_build: DecisionBuildDependencies,
    ) -> None:
        self._market = market
        self._config_version = config_version
        self._candidate_pool_size = max(1, candidate_pool_size)
        self._long_runtime = decision_build.long_runtime
        self._policy = decision_build.policy
        self._draft_index = decision_build.draft_index
        self._now = decision_build.now
        self._research_audit_builder = decision_build.research_audit_builder
        self._model_scoring = decision_build.model_scoring
        self._candidate_filtering = (
            decision_build.candidate_filtering
            if decision_build.candidate_filtering is not None
            else CandidateFilteringService(
                self._policy,
                self._model_scoring,
                self._candidate_pool_size,
            )
        )
        self._local_scoring = (
            decision_build.local_scoring
            if decision_build.local_scoring is not None
            else LocalScoringService(self._model_scoring)
        )
        self._lock = threading.RLock()
        self._batches: dict[tuple[Strategy, str], InputBatch] = {}
        self._latest_market_features: tuple[FeatureSnapshot, ...] = ()
        self._issuer_eligibility: IssuerEligibilityBatch | None = None
        self._latest_requested_codes: tuple[str, ...] = ()
        self._candidate_plans: CandidatePlanSet | None = None
        self._strategy_requested_codes: dict[Strategy, tuple[str, ...]] = {
            strategy: () for strategy in SCORED_STRATEGIES
        }
        self._strategy_candidate_features: dict[Strategy, tuple[FeatureSnapshot, ...]] = {
            strategy: () for strategy in SCORED_STRATEGIES
        }
        self._strategy_preselection_transient_invalid: dict[Strategy, bool] = {
            strategy: False for strategy in SCORED_STRATEGIES
        }
        self._latest_topk_quotes: _TopKQuoteBatch | None = None
        self._market_version = "market:unavailable"
        self._candidate_version = "candidate:unavailable"
        self._research_version = "research:initial"
        self._intraday_version = "intraday:initial"
        self._market_quote_versions: dict[str, str] = {}
        self._candidate_quote_versions: dict[str, str] = {}
        self._topk_version = "topk:unavailable"
        self._score_feature_batches: dict[tuple[str, bool, tuple[str, ...]], tuple[FeatureSnapshot, ...]] = {}
        self._projections: dict[str, ScoredLocalProjection] = {}
        self._decisions: dict[str, ScoredDecision] = {}
        self._input_quality: dict[Strategy, InputQualityStatus] = {}
        self._sequences = {strategy: 1 for strategy in SCORED_STRATEGIES}

    def invalidate_history(self) -> None:
        with self._lock:
            self._score_feature_batches.clear()

    def refresh_task(self, request: PipelineTaskRequest) -> RefreshOutcome:
        deadline = _task_deadline(request)
        try:
            return self._run_refresh_task(request, deadline)
        except DataRefreshUnavailableError:
            raise
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DataRefreshUnavailableError(_failure_code(exc)) from exc

    def _run_refresh_task(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        handler: Callable[[PipelineTaskRequest, datetime | None], RefreshOutcome] | None = {
            PipelineTask.FULL_MARKET: self._refresh_full_market,
            PipelineTask.CURRENT_QUOTES: self._refresh_full_market,
            PipelineTask.CLOSE_QUOTES: self._refresh_full_market,
            PipelineTask.CANDIDATE_QUOTES: self._refresh_candidates,
            PipelineTask.FINAL_CANDIDATE_QUOTES: self._refresh_candidates,
            PipelineTask.TOPK_QUOTES: self._refresh_topk,
            PipelineTask.INTRADAY_TAIL: self._refresh_intraday_tail,
            PipelineTask.INDUSTRY_HEAT: self._refresh_industry_heat,
            PipelineTask.MARKET_NEWS: self._refresh_market_news,
            PipelineTask.STOCK_RISK: self._refresh_stock_risk,
            PipelineTask.REFERENCE_DATA: self._refresh_reference_data,
        }.get(request.task)
        if handler is None:
            return RefreshOutcome(
                request.task,
                False,
                f"unchanged:{request.task.value}",
                (),
                request.observed_at,
                False,
            )
        return handler(request, deadline)

    def _refresh_full_market(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        market_batch = self._market.fetch_market_feature_batch(request.observed_at, force=True, deadline=deadline)
        features = market_batch.features
        issuer_eligibility = market_batch.issuer_eligibility
        data_version = _feature_batch_version("market", features)
        completed_at = _refresh_completed_at(request, features)
        apply_model_eligibility = request.task is not PipelineTask.CLOSE_QUOTES
        candidate_plans = self._candidate_filtering.plan(
            features,
            None,
            evaluated_at=completed_at,
            data_version=data_version,
            apply_model_eligibility=apply_model_eligibility,
        )
        requested = candidate_plans.physical_union()
        quote_versions = _quote_versions(features)
        self._schedule_reference_data(
            requested,
            tuple(feature.quote.code for feature in features),
            request.observed_at,
        )
        with self._lock:
            changed = data_version != self._market_version
            changed_codes = _changed_version_codes(self._market_quote_versions, quote_versions)
            requested_changed = requested != self._latest_requested_codes
            if changed or requested_changed:
                self._invalidate_scoring_locked()
            self._latest_market_features = features
            self._issuer_eligibility = issuer_eligibility
            self._latest_requested_codes = requested
            self._candidate_plans = candidate_plans
            self._strategy_requested_codes = {
                strategy: candidate_plans.strategy_codes(strategy) for strategy in SCORED_STRATEGIES
            }
            self._strategy_candidate_features = {strategy: () for strategy in SCORED_STRATEGIES}
            self._strategy_preselection_transient_invalid = {strategy: False for strategy in SCORED_STRATEGIES}
            self._market_version = data_version
            self._market_quote_versions = quote_versions
            self._record_pending_quality_locked(
                _PendingQualityContext(
                    request.observed_at,
                    len(features),
                    candidate_plans,
                    {strategy: 0 for strategy in SCORED_STRATEGIES},
                    "candidate_quotes_pending",
                    apply_model_eligibility,
                    issuer_eligibility,
                )
            )
        return RefreshOutcome(
            request.task,
            changed,
            data_version,
            changed_codes if changed else (),
            completed_at,
            _uses_fallback(features, expected_source=None),
        )

    def _refresh_candidates(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        with self._lock:
            population = self._latest_market_features
            issuer_eligibility = self._issuer_eligibility
            initial_plans = self._candidate_plans
            previous_candidate_version = self._candidate_version
            previous_strategy_requested = dict(self._strategy_requested_codes)
            previous_strategy_features = dict(self._strategy_candidate_features)
        if not population or initial_plans is None or issuer_eligibility is None:
            raise DataRefreshUnavailableError("candidate_universe_unavailable")
        refresh_plan = self._candidate_filtering.refresh(
            population,
            initial_plans,
            evaluated_at=request.observed_at,
            data_version=self._market_version,
            refresh_quotes=lambda codes: self._market.refresh_candidate_quotes(
                codes,
                request.observed_at,
                force=True,
                deadline=deadline,
            ),
            can_refill=lambda: deadline is None or self._now() < deadline,
        )
        if refresh_plan.deadline_reached:
            if _candidate_batch_is_complete(previous_strategy_requested, previous_strategy_features):
                return RefreshOutcome(
                    request.task,
                    False,
                    previous_candidate_version,
                    (),
                    request.observed_at,
                    True,
                )
            raise DataRefreshUnavailableError("candidate_refresh_deadline_exceeded")
        features = refresh_plan.features
        final_plans = refresh_plan.plans
        strategy_requested = {strategy: final_plans.strategy_codes(strategy) for strategy in SCORED_STRATEGIES}
        requested = tuple(
            dict.fromkeys(code for strategy in SCORED_STRATEGIES for code in strategy_requested[strategy])
        )
        features_by_code = {feature.quote.code: feature for feature in features}
        final_features = tuple(features_by_code[code] for code in requested if code in features_by_code)
        strategy_features = {
            strategy: tuple(features_by_code[code] for code in strategy_requested[strategy] if code in features_by_code)
            for strategy in SCORED_STRATEGIES
        }
        data_version = _feature_batch_version("candidate", final_features)
        quote_versions = _quote_versions(final_features)
        with self._lock:
            changed = data_version != self._candidate_version
            changed_codes = _changed_version_codes(self._candidate_quote_versions, quote_versions)
            self._candidate_version = data_version
            self._candidate_quote_versions = quote_versions
            if changed:
                self._invalidate_scoring_locked()
            self._latest_requested_codes = requested
            self._strategy_requested_codes = strategy_requested
            self._strategy_candidate_features = strategy_features
            self._strategy_preselection_transient_invalid = {
                strategy: strategy in refresh_plan.transient_invalid_strategies
                or has_transient_candidate_gap(initial_plans.plans[strategy])
                for strategy in SCORED_STRATEGIES
            }
            epoch = self._scoring_epoch_locked(include_intraday_tail=False)
            if len(set(strategy_requested.values())) == 1:
                strategy = SCORED_STRATEGIES[0]
                codes = strategy_requested[strategy]
                self._score_feature_batches[(epoch, False, codes)] = strategy_features[strategy]
            self._record_pending_quality_locked(
                _PendingQualityContext(
                    request.observed_at,
                    len(self._latest_market_features),
                    initial_plans,
                    {strategy: len(strategy_features[strategy]) for strategy in SCORED_STRATEGIES},
                    "scoring_pending",
                    issuer_eligibility=issuer_eligibility,
                )
            )
        return RefreshOutcome(
            request.task,
            changed,
            data_version,
            changed_codes if changed else (),
            _refresh_completed_at(request, final_features),
            _uses_fallback(final_features, expected_source="tencent"),
        )

    def _record_pending_quality_locked(self, context: _PendingQualityContext) -> None:
        batch_id = self._scoring_epoch_locked(include_intraday_tail=False)
        for strategy in SCORED_STRATEGIES:
            existing = self._input_quality.get(strategy)
            if (
                existing is not None
                and existing.summary.trade_date == context.observed_at.date()
                and existing.population_count == context.population_count
                and existing.primary_blocker not in {"candidate_quotes_pending", "scoring_pending"}
            ):
                continue
            stage_counts = context.candidate_plans.plans[strategy].stage_counts
            requested_count = stage_counts.candidate_limit_selected
            candidate_feature_count = min(requested_count, context.candidate_feature_counts[strategy])
            covered = candidate_feature_count
            dynamic_data_pending = max(
                0,
                stage_counts.issuer_eligible_population - stage_counts.input_ready_population,
            )
            pipeline = build_pending_pipeline(
                stage_counts,
                candidate_feature_count=candidate_feature_count,
                primary_blocker=context.primary_blocker,
                candidate_score_threshold=self._policy.selection.candidate_min_score,
            )
            first_nine = build_first_nine_stage_snapshots(
                stage_counts,
                batch_id=f"{batch_id}:{strategy.value}",
                as_of=context.observed_at,
                population_count=context.population_count,
                candidate_feature_count=candidate_feature_count,
                data_pending_count=dynamic_data_pending,
                refresh_pending_count=max(0, requested_count - candidate_feature_count),
                issuer_eligibility=context.issuer_eligibility,
            )
            self._input_quality[strategy] = InputQualityStatus(
                strategy=strategy,
                status="not_ready",
                publishable=False,
                summary=SupplySummary(
                    trade_date=context.observed_at.date(),
                    quote_total_count=requested_count,
                    quote_covered_count=covered,
                    quote_missing_count=requested_count - covered,
                    security_identity_missing_count=0,
                ),
                pipeline=pipeline,
                stage_snapshots=build_complete_stage_snapshots(first_nine, pipeline),
                population_count=context.population_count,
                candidate_count=requested_count,
                candidate_feature_count=candidate_feature_count,
                history_required_sessions=(
                    self._model_scoring.history_required_sessions(strategy)
                    if self._model_scoring is not None and context.apply_model_eligibility
                    else 20
                ),
                population_rejected_count=max(0, context.population_count - requested_count),
                candidate_rejected_count=max(0, requested_count - candidate_feature_count),
                candidate_feature_coverage_ratio=(
                    candidate_feature_count / requested_count if requested_count else 0.0
                ),
                primary_blocker=context.primary_blocker,
            )

    def _refresh_topk(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        if not request.selected_codes:
            return RefreshOutcome(request.task, False, "topk:empty", (), request.observed_at, False)
        features = tuple(
            self._market.refresh_topk_quotes(
                request.selected_codes,
                request.observed_at,
                force=True,
                deadline=deadline,
            )
        )
        data_version = _feature_batch_version("topk", features)
        with self._lock:
            changed = data_version != self._topk_version
            self._topk_version = data_version
            self._latest_topk_quotes = _TopKQuoteBatch(request.observed_at, features)
        return RefreshOutcome(
            request.task,
            changed,
            data_version,
            tuple(feature.quote.code for feature in features) if changed else (),
            _refresh_completed_at(request, features),
            _uses_fallback(features, expected_source="tencent"),
        )

    def _refresh_intraday_tail(
        self,
        request: PipelineTaskRequest,
        _deadline: datetime | None,
    ) -> RefreshOutcome:
        requested = self._requested_codes()
        _require_codes(requested, "intraday_universe_unavailable")
        self._market.refresh_intraday_tail(requested, request.observed_at)
        data_version = f"intraday:{request.observed_at:%Y%m%dT%H%M%S%f}"
        with self._lock:
            self._intraday_version = data_version
            self._invalidate_scoring_locked()
        return RefreshOutcome(request.task, True, data_version, requested, request.observed_at, False)

    def _refresh_industry_heat(
        self,
        request: PipelineTaskRequest,
        _deadline: datetime | None,
    ) -> RefreshOutcome:
        features = tuple(self._market.refresh_industry_heat(request.observed_at))
        version = _feature_batch_version("industry", features)
        return RefreshOutcome(
            request.task,
            bool(features),
            version,
            tuple(feature.quote.code for feature in features),
            _refresh_completed_at(request, features),
            False,
        )

    def _refresh_market_news(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        requested = self._requested_codes()
        _require_codes(requested, "news_universe_unavailable")
        result = self._market.refresh_market_news(requested, request.observed_at, deadline=deadline)
        return self._research_outcome(request, result)

    def _refresh_stock_risk(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        requested = self._requested_codes()
        _require_codes(requested, "risk_universe_unavailable")
        result = self._market.refresh_stock_risk(requested, request.observed_at, deadline=deadline)
        return self._research_outcome(request, result)

    def _refresh_reference_data(
        self,
        request: PipelineTaskRequest,
        _deadline: datetime | None,
    ) -> RefreshOutcome:
        requested = self._requested_codes()
        if requested:
            self._schedule_reference_data(requested, requested, request.observed_at)
        return RefreshOutcome(request.task, False, "reference:scheduled", (), request.observed_at, False)

    def _research_outcome(
        self,
        request: PipelineTaskRequest,
        result: ResearchRefreshResult,
    ) -> RefreshOutcome:
        version = result.data_version or f"research:{request.task.value}:empty"
        changed = bool(result.changed_codes)
        with self._lock:
            if changed:
                self._research_version = version
                self._invalidate_scoring_locked()
        return RefreshOutcome(
            request.task,
            changed,
            version,
            result.changed_codes if changed else (),
            result.completed_at or request.observed_at,
            bool(result.failed_codes or result.deferred_codes or result.deadline_reached),
        )

    def _requested_codes(self) -> tuple[str, ...]:
        with self._lock:
            return self._latest_requested_codes

    def refresh(self, request: CycleRequest) -> None:
        if request.strategy is Strategy.LONG:
            if not self._long_runtime.offer_refresh(LongRefreshRequest(request.observed_at, request.phase, force=True)):
                raise DataRefreshUnavailableError("long_refresh_rejected")
            return
        try:
            shared = self._cached_input(request)
            candidate_features = shared.candidate_features
            if request.strategy is Strategy.TOMORROW and shared.requested_codes:
                scoring_batch = self._score_features(
                    shared.requested_codes,
                    request.observed_at,
                    include_intraday_tail=True,
                )
                candidate_features = scoring_batch.features
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DataRefreshUnavailableError(_failure_code(exc)) from exc
        batch = InputBatch(
            request,
            shared.market_features,
            shared.requested_codes,
            candidate_features,
            _data_version(request, shared.market_features, candidate_features),
            shared.candidate_stage_counts,
            shared.candidate_quote_eligible,
            shared.preselection_transient_invalid,
            shared.issuer_eligibility,
        )
        with self._lock:
            self._batches[(request.strategy, request.input_version)] = batch
            while len(self._batches) > 32:
                self._batches.pop(next(iter(self._batches)))

    def _cached_input(self, request: CycleRequest) -> _SharedInputBatch:
        with self._lock:
            market_features = self._latest_market_features
            issuer_eligibility = self._issuer_eligibility
            requested = self._strategy_requested_codes[request.strategy]
            candidate_features = self._strategy_candidate_features[request.strategy]
            candidate_plans = self._candidate_plans
            preselection_transient_invalid = self._strategy_preselection_transient_invalid[request.strategy]
        if not market_features or candidate_plans is None or issuer_eligibility is None:
            raise DataRefreshUnavailableError("market_snapshot_unavailable")
        scoring_batch = (
            self._score_features(
                requested,
                request.observed_at,
                include_intraday_tail=False,
            )
            if requested
            else _ScoringFeatureBatch(candidate_features)
        )
        return _SharedInputBatch(
            market_features,
            requested,
            scoring_batch.features,
            candidate_plans.plans[request.strategy].stage_counts,
            len(candidate_features),
            preselection_transient_invalid,
            issuer_eligibility,
        )

    def _score_features(
        self,
        requested: tuple[str, ...],
        observed_at: datetime,
        *,
        include_intraday_tail: bool,
    ) -> _ScoringFeatureBatch:
        with self._lock:
            epoch = self._scoring_epoch_locked(include_intraday_tail=include_intraday_tail)
            key = (epoch, include_intraday_tail, requested)
            cached = self._score_feature_batches.get(key)
            if cached is not None:
                return _ScoringFeatureBatch(cached)
        requested_codes = frozenset(requested)
        features = tuple(
            feature
            for feature in self._market.read_candidate_features(
                requested,
                observed_at,
                include_intraday_tail=include_intraday_tail,
                include_structured_research=True,
            )
            if feature.quote.code in requested_codes
        )
        with self._lock:
            if epoch == self._scoring_epoch_locked(include_intraday_tail=include_intraday_tail):
                self._score_feature_batches[key] = features
                while len(self._score_feature_batches) > 8:
                    self._score_feature_batches.pop(next(iter(self._score_feature_batches)))
        return _ScoringFeatureBatch(features)

    def _scoring_epoch_locked(self, *, include_intraday_tail: bool) -> str:
        versions = (
            self._market_version,
            self._candidate_version,
            self._research_version,
            getattr(self._market, "reference_version", lambda: "reference:unknown")(),
            self._intraday_version if include_intraday_tail else "intraday:not_used",
            self._latest_requested_codes,
            tuple((strategy.value, self._strategy_requested_codes[strategy]) for strategy in SCORED_STRATEGIES),
        )
        return f"input:{_stable_digest(versions)}"

    def _invalidate_scoring_locked(self) -> None:
        self._score_feature_batches.clear()

    def _schedule_reference_data(
        self,
        codes: tuple[str, ...],
        security_master_codes: tuple[str, ...],
        observed_at: datetime,
    ) -> None:
        try:
            self._market.schedule_reference_data(
                codes,
                observed_at,
                force=False,
                security_master_codes=security_master_codes,
            )
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError):
            # Reference enrichment is best-effort; missing fields remain visible as controlled degradation.
            return

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        draft = self._draft_index.snapshot(strategy)
        return draft is not None and draft.trade_date == trade_date

    def build_local(self, request: CycleRequest) -> DecisionIdentity | None:
        if request.strategy is Strategy.LONG:
            return None
        with self._lock:
            batch = self._batches.get((request.strategy, request.input_version))
            sequence = self._sequences[request.strategy]
            self._sequences[request.strategy] += 2
        if batch is None:
            raise DecisionUnavailableError("current native input is unavailable")
        try:
            evaluated_at = _decision_observed_at(batch)
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
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise DecisionUnavailableError(_decision_failure_code(exc)) from exc
        quality_status = build_supply_status(
            projection,
            batch.candidate_stage_counts,
            candidate_quote_eligible=batch.candidate_quote_eligible,
            candidate_score_threshold=self._policy.selection.candidate_min_score,
            issuer_eligibility=batch.issuer_eligibility,
        )
        projection = replace(
            projection,
            local=replace(projection.local, pipeline=quality_status.pipeline),
        )
        with self._lock:
            self._input_quality[request.strategy] = quality_status
        if not projection.input_quality.publishable:
            self._draft_index.publish(projection.local)
            raise DecisionUnavailableError(projection.input_quality.status)
        with self._lock:
            self._projections[projection.local.version] = projection
            self._decisions[projection.local.version] = projection.local
            self._trim_research_sources()
        return projection.local

    def input_quality_status(self) -> tuple[InputQualityStatus, ...]:
        with self._lock:
            return tuple(
                self._input_quality[strategy] for strategy in sorted(self._input_quality, key=lambda item: item.value)
            )

    def projection(self, version: str) -> ScoredLocalProjection | None:
        with self._lock:
            return self._projections.get(version)

    def register_hybrid(
        self,
        projection: ScoredLocalProjection,
        decision: ScoredDecision,
    ) -> ScoredDecision:
        with self._lock:
            current_quality = self._input_quality.get(decision.strategy)
            if current_quality is not None and current_quality.summary.trade_date == decision.trade_date:
                current_quality = update_supply_status_decision(
                    current_quality,
                    projection,
                    decision,
                    candidate_score_threshold=self._policy.selection.candidate_min_score,
                )
                decision = replace(decision, pipeline=current_quality.pipeline)
                self._input_quality[decision.strategy] = current_quality
            self._projections[decision.version] = projection
            self._decisions[decision.version] = decision
            self._trim_research_sources()
            return decision

    def research_audit(self, version: str) -> object | None:
        with self._lock:
            projection = self._projections.get(version)
            decision = self._decisions.get(version)
        if projection is None or decision is None:
            return None
        return self._research_audit_builder(projection, decision)

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
            with self._lock:
                topk = self._latest_topk_quotes
            if topk is None or topk.observed_at != request.observed_at:
                raise DecisionUnavailableError("topk quote batch is unavailable")
            features_by_code = {feature.quote.code: feature for feature in topk.features}
        else:
            with self._lock:
                batch = self._batches.get((request.strategy, request.input_version))
            if batch is None:
                return None
            features_by_code = _selected_quote_features(batch, identity_codes(decision))
        selected_codes = frozenset(identity_codes(decision))
        features_by_code = {code: feature for code, feature in features_by_code.items() if code in selected_codes}
        observed_at = _overlay_observed_at(request, tuple(features_by_code.values()))
        if previous is not None and previous.observed_at >= observed_at:
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
        )

    def _trim_research_sources(self) -> None:
        while len(self._decisions) > 64:
            version = next(iter(self._decisions))
            self._decisions.pop(version, None)
            self._projections.pop(version, None)


__all__ = [
    "DecisionBuildDependencies",
    "InputBatch",
    "MarketDataAdapter",
    "build_source_stage_output",
]

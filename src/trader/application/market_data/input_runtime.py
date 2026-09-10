"""Current market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol

from trader.application.decisions.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.long_runtime import LongRuntime
from trader.application.market_data.supply_status import build_supply_status
from trader.application.ports.long import LongRefreshRequest
from trader.application.ports.market import MarketDataUnavailableError, ResearchRefreshResult
from trader.application.ports.model_scoring import ModelScoringContext, ModelScoringPort
from trader.application.ports.runtime_status import InputQualityStatus, SupplyFunnel, SupplySummary
from trader.application.ports.scheduler import (
    CycleRequest,
    DataRefreshPort,
    DataRefreshUnavailableError,
    DecisionBuilderPort,
    DecisionUnavailableError,
    PipelineTaskRequest,
    RefreshOutcome,
    ResearchIntent,
)
from trader.application.ports.scored import D25NativeInput, TodayNativeInput, TomorrowNativeInput
from trader.application.recommendation.candidate_planning import (
    SCORED_STRATEGIES,
    CandidatePlanningContext,
    CandidatePlanSet,
    build_candidate_plans,
    refresh_candidate_reserves,
)
from trader.application.recommendation.policy import RecommendationPolicy
from trader.application.recommendation.scored_projection import (
    ScoredBuildRuntime,
    ScoredLocalProjection,
    build_scored_local,
)
from trader.application.recommendation.scored_quality import has_transient_candidate_gap
from trader.application.research.research_audit import (
    CommittedResearchAudit,
    try_build_committed_research_audit,
)
from trader.application.runtime.cadence import PipelineTask, task_execution_budget_seconds
from trader.application.runtime.schedule import SHANGHAI
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.decision_identity import (
    DecisionIdentity,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
    identity_codes,
)
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.selection.scored_selection import ScoredCandidateStageCounts


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


@dataclass(frozen=True)
class _SharedInputBatch:
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    candidate_stage_counts: ScoredCandidateStageCounts
    candidate_quote_eligible: int
    preselection_transient_invalid: bool


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


@dataclass(frozen=True)
class _TopKQuoteBatch:
    observed_at: datetime
    features: tuple[FeatureSnapshot, ...]


class MarketReader(Protocol):
    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]: ...

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
        self._model_scoring = decision_build.model_scoring
        self._lock = threading.RLock()
        self._batches: dict[tuple[Strategy, str], InputBatch] = {}
        self._latest_market_features: tuple[FeatureSnapshot, ...] = ()
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
        self._sequences = {strategy: 1 for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)}

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
        features = tuple(self._market.fetch_market_features(request.observed_at, force=True, deadline=deadline))
        data_version = _feature_batch_version("market", features)
        completed_at = _refresh_completed_at(request, features)
        candidate_plans = build_candidate_plans(
            features,
            None,
            CandidatePlanningContext(
                evaluated_at=completed_at,
                data_version=data_version,
                policy=self._policy,
                model_scoring=self._model_scoring,
                limit_per_board=self._candidate_pool_size,
            ),
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
                request.observed_at,
                population_count=len(features),
                candidate_plans=candidate_plans,
                candidate_feature_counts={strategy: 0 for strategy in SCORED_STRATEGIES},
                primary_blocker="candidate_quotes_pending",
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
            initial_plans = self._candidate_plans
            previous_candidate_version = self._candidate_version
            previous_strategy_requested = dict(self._strategy_requested_codes)
            previous_strategy_features = dict(self._strategy_candidate_features)
        if not population or initial_plans is None:
            raise DataRefreshUnavailableError("candidate_universe_unavailable")
        refresh_plan = refresh_candidate_reserves(
            population,
            initial_plans,
            CandidatePlanningContext(
                evaluated_at=request.observed_at,
                data_version=self._market_version,
                policy=self._policy,
                model_scoring=self._model_scoring,
                limit_per_board=initial_plans.limit_per_board,
            ),
            lambda codes: self._market.refresh_candidate_quotes(
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
                codes = strategy_requested[Strategy.TODAY]
                self._score_feature_batches[(epoch, False, codes)] = strategy_features[Strategy.TODAY]
            self._record_pending_quality_locked(
                request.observed_at,
                population_count=len(self._latest_market_features),
                candidate_plans=initial_plans,
                candidate_feature_counts={strategy: len(strategy_features[strategy]) for strategy in SCORED_STRATEGIES},
                primary_blocker="scoring_pending",
            )
        return RefreshOutcome(
            request.task,
            changed,
            data_version,
            changed_codes if changed else (),
            _refresh_completed_at(request, final_features),
            _uses_fallback(final_features, expected_source="tencent"),
        )

    def _record_pending_quality_locked(
        self,
        observed_at: datetime,
        *,
        population_count: int,
        candidate_plans: CandidatePlanSet,
        candidate_feature_counts: dict[Strategy, int],
        primary_blocker: str,
    ) -> None:
        for strategy in SCORED_STRATEGIES:
            existing = self._input_quality.get(strategy)
            if (
                existing is not None
                and existing.summary.trade_date == observed_at.date()
                and existing.primary_blocker not in {"candidate_quotes_pending", "scoring_pending"}
            ):
                continue
            stage_counts = candidate_plans.plans[strategy].stage_counts
            requested_count = stage_counts.candidate_limit_selected
            candidate_feature_count = min(requested_count, candidate_feature_counts[strategy])
            covered = candidate_feature_count
            self._input_quality[strategy] = InputQualityStatus(
                strategy=strategy,
                status="not_ready",
                publishable=False,
                summary=SupplySummary(
                    trade_date=observed_at.date(),
                    quote_total_count=requested_count,
                    quote_covered_count=covered,
                    quote_missing_count=requested_count - covered,
                    security_identity_missing_count=0,
                ),
                supply_funnel=SupplyFunnel(
                    requested_candidates=requested_count,
                    candidate_features=candidate_feature_count,
                    issuer_eligible_population=stage_counts.issuer_eligible_population,
                    dynamic_filter_eligible=stage_counts.dynamic_filter_eligible,
                    strategy_history_eligible=stage_counts.strategy_history_eligible,
                    model_input_eligible=stage_counts.model_input_eligible,
                    candidate_score_eligible=stage_counts.candidate_score_eligible,
                    candidate_limit_selected=stage_counts.candidate_limit_selected,
                    candidate_quote_eligible=candidate_feature_count,
                ),
                population_count=population_count,
                candidate_count=requested_count,
                candidate_feature_count=candidate_feature_count,
                history_required_sessions=(
                    self._model_scoring.history_required_sessions(strategy) if self._model_scoring is not None else 20
                ),
                population_rejected_count=max(0, population_count - requested_count),
                candidate_rejected_count=max(0, requested_count - candidate_feature_count),
                candidate_feature_coverage_ratio=(
                    candidate_feature_count / requested_count if requested_count else 0.0
                ),
                primary_blocker=primary_blocker,
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
        )
        with self._lock:
            self._batches[(request.strategy, request.input_version)] = batch
            while len(self._batches) > 32:
                self._batches.pop(next(iter(self._batches)))

    def _cached_input(self, request: CycleRequest) -> _SharedInputBatch:
        with self._lock:
            market_features = self._latest_market_features
            requested = self._strategy_requested_codes[request.strategy]
            candidate_features = self._strategy_candidate_features[request.strategy]
            candidate_plans = self._candidate_plans
            preselection_transient_invalid = self._strategy_preselection_transient_invalid[request.strategy]
        if not market_features or candidate_plans is None:
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
            if request.strategy is Strategy.TODAY:
                today_native = TodayNativeInput(
                    batch.request.trade_date,
                    batch.request.phase,
                    batch.data_version,
                    self._config_version,
                    evaluated_at,
                    batch.market_features,
                    batch.requested_codes,
                    batch.candidate_features,
                    20.0,
                    20.0,
                    self._candidate_pool_size,
                )
                projection = build_scored_local(
                    today_native,
                    self._policy,
                    sequence=sequence,
                    runtime=ScoredBuildRuntime(
                        model_scoring=self._model_scoring,
                        scoring_context=_model_scoring_context(request, batch, self._now()),
                        candidate_stage_counts=batch.candidate_stage_counts,
                        preselection_transient_invalid=batch.preselection_transient_invalid,
                    ),
                )
            else:
                tomorrow_native = (TomorrowNativeInput if request.strategy is Strategy.TOMORROW else D25NativeInput)(
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
                projection = build_scored_local(
                    tomorrow_native,
                    self._policy,
                    sequence=sequence,
                    runtime=ScoredBuildRuntime(
                        model_scoring=self._model_scoring,
                        scoring_context=_model_scoring_context(request, batch, self._now()),
                        candidate_stage_counts=batch.candidate_stage_counts,
                        preselection_transient_invalid=batch.preselection_transient_invalid,
                    ),
                )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise DecisionUnavailableError(_decision_failure_code(exc)) from exc
        with self._lock:
            self._input_quality[request.strategy] = build_supply_status(
                projection,
                batch.candidate_stage_counts,
                candidate_quote_eligible=batch.candidate_quote_eligible,
            )
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
    ) -> None:
        with self._lock:
            self._projections[decision.version] = projection
            self._decisions[decision.version] = decision
            self._trim_research_sources()

    def research_audit(self, version: str) -> CommittedResearchAudit | None:
        with self._lock:
            projection = self._projections.get(version)
            decision = self._decisions.get(version)
        if projection is None or decision is None:
            return None
        return try_build_committed_research_audit(projection, decision)

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


def _quote_order(feature: FeatureSnapshot) -> tuple[datetime, datetime, str]:
    quote = feature.quote
    return quote.source_time, quote.received_time, quote.data_version


def _selected_quote_features(
    batch: InputBatch,
    selected_codes: Collection[str],
) -> dict[str, FeatureSnapshot]:
    features_by_code: dict[str, FeatureSnapshot] = {}
    for feature in (*batch.market_features, *batch.candidate_features):
        if feature.quote.code not in selected_codes:
            continue
        current = features_by_code.get(feature.quote.code)
        if current is None or _quote_order(feature) > _quote_order(current):
            features_by_code[feature.quote.code] = feature
    return features_by_code


def _overlay_observed_at(request: CycleRequest, features: tuple[FeatureSnapshot, ...]) -> datetime:
    target_zone = request.observed_at.tzinfo
    if target_zone is None:
        raise DecisionUnavailableError("overlay_request_time_unavailable")
    values = [request.observed_at]
    for feature in features:
        values.extend((feature.observed_at, feature.quote.received_time))
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise DecisionUnavailableError("overlay_input_time_unavailable")
    observed_at = max(value.astimezone(target_zone) for value in values)
    if observed_at.date() != request.trade_date:
        raise DecisionUnavailableError("overlay_observation_trade_date_mismatch")
    return observed_at


def _merge_overlay_quote(
    quotes: dict[str, DecisionQuote],
    feature: FeatureSnapshot,
    observed_at: datetime,
) -> bool:
    quote = feature.quote
    if quote.price is None or quote.price <= 0.0 or quote.source_time > observed_at:
        return False
    candidate = DecisionQuote(
        code=quote.code,
        price=quote.price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        market_cap=quote.market_cap,
        source=quote.source,
        source_time=quote.source_time,
        data_version=quote.data_version,
    )
    existing = quotes.get(quote.code)
    if existing is not None and (candidate.source_time, candidate.data_version) <= (
        existing.source_time,
        existing.data_version,
    ):
        return False
    quotes[quote.code] = candidate
    return True


def _task_deadline(request: PipelineTaskRequest) -> datetime | None:
    seconds = task_execution_budget_seconds(request.task)
    return request.observed_at + timedelta(seconds=seconds) if seconds is not None else None


def _candidate_batch_is_complete(
    requested: Mapping[Strategy, tuple[str, ...]],
    features: Mapping[Strategy, tuple[FeatureSnapshot, ...]],
) -> bool:
    if not any(requested.values()):
        return False
    return all(
        tuple(item.quote.code for item in features[strategy]) == requested[strategy] for strategy in SCORED_STRATEGIES
    )


def _require_codes(codes: tuple[str, ...], error_code: str) -> None:
    if not codes:
        raise DataRefreshUnavailableError(error_code)


def _data_version(
    request: CycleRequest,
    market_features: tuple[FeatureSnapshot, ...],
    candidate_features: tuple[FeatureSnapshot, ...],
) -> str:
    versions = (
        _feature_batch_version("market", market_features),
        _feature_batch_version("candidate", candidate_features),
    )
    return f"{request.input_version}:{_stable_digest(versions)}"


def _feature_batch_version(kind: str, features: tuple[FeatureSnapshot, ...]) -> str:
    material = tuple(sorted(_feature_identity(feature) for feature in features))
    return f"{kind}:{_stable_digest(material)}"


def _feature_identity(feature: FeatureSnapshot) -> tuple[object, ...]:
    quote = feature.quote
    return (
        quote.code,
        quote.data_version,
        quote.source_time.isoformat(),
        quote.name,
        quote.industry,
        quote.board.value,
        quote.listing_date.isoformat() if quote.listing_date is not None else None,
        quote.listing_age_sessions,
        quote.execution_restrictions,
        tuple(sorted(feature.values.items())),
        feature.history_days,
        feature.market_regime,
        feature.missing_fields,
        tuple(sorted(feature.missing_reasons.items())),
        tuple((item.evidence_id, item.data_version, item.published_at.isoformat()) for item in feature.evidence),
        tuple(repr(item) for item in feature.external_risk_facts),
        feature.board_policy_version,
        feature.competition_group_version,
        feature.parameter_status,
        feature.selection_skip_reason,
        feature.merge_epoch,
    )


def _quote_versions(features: tuple[FeatureSnapshot, ...]) -> dict[str, str]:
    return {
        feature.quote.code: f"{feature.quote.data_version}:{feature.quote.source_time.isoformat()}"
        for feature in features
    }


def _changed_version_codes(previous: dict[str, str], current: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted(code for code in {*previous, *current} if previous.get(code) != current.get(code)))


def _refresh_completed_at(
    request: PipelineTaskRequest,
    features: tuple[FeatureSnapshot, ...],
) -> datetime:
    values = (
        request.observed_at,
        *(feature.observed_at for feature in features),
        *(feature.quote.received_time for feature in features),
    )
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("refresh completion times must be timezone-aware")
    return max(value.astimezone(SHANGHAI) for value in values)


def _uses_fallback(features: tuple[FeatureSnapshot, ...], *, expected_source: str | None) -> bool:
    return any(
        (expected_source is not None and feature.quote.source != expected_source)
        or "market_data_degraded" in feature.quote.execution_restrictions
        for feature in features
    )


def _decision_observed_at(batch: InputBatch) -> datetime:
    target_zone = batch.request.observed_at.tzinfo
    if target_zone is None:
        raise ValueError("decision request time must be timezone-aware")
    values = [batch.request.observed_at]
    for feature in (*batch.market_features, *batch.candidate_features):
        values.extend((feature.observed_at, feature.quote.received_time))
        for evidence in feature.evidence:
            if evidence.received_at is not None:
                values.append(evidence.received_at)
        values.extend(fact.observed_at for fact in feature.external_risk_facts)
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("decision input times must be timezone-aware")
    return max(value.astimezone(target_zone) for value in values)


def _model_scoring_context(
    request: CycleRequest,
    batch: InputBatch,
    now: datetime,
) -> ModelScoringContext:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("model scoring clock must be timezone-aware")
    local_now = now.astimezone(SHANGHAI)
    input_at = _decision_observed_at(batch).astimezone(SHANGHAI)
    input_age_seconds = max(0.0, (local_now - input_at).total_seconds())
    if request.strategy is not Strategy.TOMORROW or request.phase == "close_fallback":
        return ModelScoringContext(input_age_seconds=input_age_seconds)
    deadline = datetime.combine(request.trade_date, time(14, 50), tzinfo=SHANGHAI)
    return ModelScoringContext(
        time_budget_seconds=max(0.0, (deadline - local_now).total_seconds()),
        input_age_seconds=input_age_seconds,
    )


def _stable_digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]


def _failure_code(exc: BaseException) -> str:
    value = str(exc).strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None:
        return value
    name = type(exc).__name__
    return "".join((f"_{character.lower()}" if character.isupper() else character) for character in name).lstrip("_")


def _decision_failure_code(exc: BaseException) -> str:
    if str(exc) == "scored native input cannot contain future features":
        return "future_input_time"
    return _failure_code(exc)


__all__ = [
    "DecisionBuildDependencies",
    "InputBatch",
    "MarketDataAdapter",
]

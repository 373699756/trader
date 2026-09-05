"""Current market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from trader.application.decisions.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.long_runtime import LongRuntime
from trader.application.ports.long import LongRefreshRequest
from trader.application.ports.market import MarketDataUnavailableError, ResearchRefreshResult
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
from trader.application.recommendation.policy import RecommendationPolicy
from trader.application.recommendation.scored_projection import (
    ScoredLocalProjection,
    build_scored_local,
)
from trader.application.recommendation.scored_quality import ScoredInputQuality
from trader.application.recommendation.tomorrow_model_scoring import TomorrowProductionModelScoringService
from trader.application.research.research_audit import (
    CommittedResearchAudit,
    try_build_committed_research_audit,
)
from trader.application.runtime.cadence import PipelineTask, task_execution_budget_seconds
from trader.application.runtime.schedule import SHANGHAI
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.decision_identity import (
    DecisionIdentity,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
    identity_codes,
)
from trader.domain.recommendation.models import RecommendationAction, ScoredDisposition, Strategy
from trader.domain.recommendation.selection.ranking import candidate_score


@dataclass(frozen=True)
class InputBatch:
    request: CycleRequest
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]
    data_version: str


@dataclass(frozen=True)
class _SharedInputBatch:
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    candidate_features: tuple[FeatureSnapshot, ...]


@dataclass(frozen=True)
class _ScoringFeatureBatch:
    features: tuple[FeatureSnapshot, ...]


@dataclass(frozen=True)
class DecisionBuildDependencies:
    long_runtime: LongRuntime
    policy: RecommendationPolicy
    draft_index: UnifiedDecisionDraftIndex
    tomorrow_model: TomorrowProductionModelScoringService | None = None


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
        self._tomorrow_model = decision_build.tomorrow_model
        self._lock = threading.RLock()
        self._batches: dict[tuple[Strategy, str], InputBatch] = {}
        self._latest_market_features: tuple[FeatureSnapshot, ...] = ()
        self._latest_requested_codes: tuple[str, ...] = ()
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
        requested = _candidate_codes(features, self._candidate_pool_size)
        data_version = _feature_batch_version("market", features)
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
            self._market_version = data_version
            self._market_quote_versions = quote_versions
            self._record_pending_quality_locked(
                request.observed_at,
                population_count=len(features),
                requested_count=len(requested),
                candidate_feature_count=0,
                primary_blocker="candidate_quotes_pending",
            )
        return RefreshOutcome(
            request.task,
            changed,
            data_version,
            changed_codes if changed else (),
            _refresh_completed_at(request, features),
            _uses_fallback(features, expected_source=None),
        )

    def _refresh_candidates(
        self,
        request: PipelineTaskRequest,
        deadline: datetime | None,
    ) -> RefreshOutcome:
        requested = self._requested_codes()
        _require_codes(requested, "candidate_universe_unavailable")
        features = tuple(
            self._market.refresh_candidate_quotes(
                requested,
                request.observed_at,
                force=True,
                deadline=deadline,
            )
        )
        data_version = _feature_batch_version("candidate", features)
        quote_versions = _quote_versions(features)
        with self._lock:
            changed = data_version != self._candidate_version
            changed_codes = _changed_version_codes(self._candidate_quote_versions, quote_versions)
            self._candidate_version = data_version
            self._candidate_quote_versions = quote_versions
            if changed:
                self._invalidate_scoring_locked()
            epoch = self._scoring_epoch_locked(include_intraday_tail=False)
            self._score_feature_batches[(epoch, False, requested)] = features
            self._record_pending_quality_locked(
                request.observed_at,
                population_count=len(self._latest_market_features),
                requested_count=len(requested),
                candidate_feature_count=len(features),
                primary_blocker="scoring_pending",
            )
        return RefreshOutcome(
            request.task,
            changed,
            data_version,
            changed_codes if changed else (),
            _refresh_completed_at(request, features),
            _uses_fallback(features, expected_source="tencent"),
        )

    def _record_pending_quality_locked(
        self,
        observed_at: datetime,
        *,
        population_count: int,
        requested_count: int,
        candidate_feature_count: int,
        primary_blocker: str,
    ) -> None:
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            existing = self._input_quality.get(strategy)
            if (
                existing is not None
                and existing.summary.trade_date == observed_at.date()
                and existing.primary_blocker not in {"candidate_quotes_pending", "scoring_pending"}
            ):
                continue
            covered = min(requested_count, candidate_feature_count)
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
                ),
                population_count=population_count,
                candidate_count=requested_count,
                candidate_feature_count=candidate_feature_count,
                history_required_sessions=(
                    self._tomorrow_model.history_required_sessions
                    if strategy is Strategy.TOMORROW and self._tomorrow_model is not None
                    else 20
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
            if request.strategy is Strategy.TOMORROW:
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
        )
        with self._lock:
            self._batches[(request.strategy, request.input_version)] = batch
            while len(self._batches) > 32:
                self._batches.pop(next(iter(self._batches)))

    def _cached_input(self, request: CycleRequest) -> _SharedInputBatch:
        with self._lock:
            market_features = self._latest_market_features
            requested = self._latest_requested_codes
        if not market_features or not requested:
            raise DataRefreshUnavailableError("market_snapshot_unavailable")
        scoring_batch = self._score_features(
            requested,
            request.observed_at,
            include_intraday_tail=False,
        )
        return _SharedInputBatch(
            market_features,
            requested,
            scoring_batch.features,
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
        features = tuple(
            self._market.read_candidate_features(
                requested,
                observed_at,
                include_intraday_tail=include_intraday_tail,
                include_structured_research=True,
            )
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
                projection = build_scored_local(today_native, self._policy, sequence=sequence)
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
                    tomorrow_model=self._tomorrow_model if request.strategy is Strategy.TOMORROW else None,
                )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise DecisionUnavailableError(_decision_failure_code(exc)) from exc
        with self._lock:
            self._input_quality[request.strategy] = _supply_status(projection)
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


def _supply_status(
    projection: ScoredLocalProjection,
) -> InputQualityStatus:
    quality = projection.input_quality
    diagnostics = projection.local.selection_diagnostics
    if diagnostics is None:
        raise ValueError("scored input status requires selection diagnostics")
    requested = set(projection.native_input.requested_codes)
    evaluations = tuple(item for item in projection.selection.evaluations if item.code in requested)
    decision_items = projection.local.items
    funnel = SupplyFunnel(
        requested_candidates=quality.candidate_count,
        candidate_features=quality.candidate_feature_count,
        security_master=quality.security_master_covered_count,
        history=quality.history_covered_count,
        filter_pass=sum(item.disposition is ScoredDisposition.PASS for item in evaluations),
        filter_observe=sum(item.disposition is ScoredDisposition.OBSERVE_ONLY for item in evaluations),
        filter_reject=sum(item.disposition is ScoredDisposition.REJECT for item in evaluations),
        full_scored=quality.candidate_scored_count,
        review_eligible=len(projection.review_candidates),
        observation_threshold_met_count=sum(
            item.final_score >= diagnostics.observation_floor for item in decision_items
        ),
        executable_threshold_met_count=sum(
            item.final_score >= diagnostics.executable_threshold for item in decision_items
        ),
        action_executable=sum(item.action is RecommendationAction.EXECUTABLE for item in decision_items),
        action_observe=sum(item.action is RecommendationAction.OBSERVE for item in decision_items),
        action_unavailable=sum(item.action is RecommendationAction.UNAVAILABLE for item in decision_items),
        selected_executable=sum(
            item.selected and item.action is RecommendationAction.EXECUTABLE for item in decision_items
        ),
        selected_observe=sum(item.selected and item.action is RecommendationAction.OBSERVE for item in decision_items),
    )
    reasons: Counter[str] = Counter()
    for item in evaluations:
        reasons.update(reason.code for reason in item.filter_reasons)
        reasons.update(reason.code for reason in item.optional_flags)
        if item.candidate_audit_pruning_reason:
            reasons[item.candidate_audit_pruning_reason] += 1
        if item.selection_skip_reason:
            reasons[item.selection_skip_reason] += 1
    reasons.update(item.reason for item in decision_items if item.reason)
    reasons.update(risk for item in decision_items for risk in item.risk_codes)
    return InputQualityStatus(
        strategy=projection.local.strategy,
        status=quality.status,
        publishable=quality.publishable,
        summary=_supply_summary(projection),
        supply_funnel=funnel,
        population_count=quality.population_count,
        candidate_count=quality.candidate_count,
        candidate_feature_count=quality.candidate_feature_count,
        population_rejected_count=quality.population_rejected_count,
        candidate_rejected_count=quality.candidate_rejected_count,
        candidate_scored_count=quality.candidate_scored_count,
        security_master_covered_count=quality.security_master_covered_count,
        history_covered_count=quality.history_covered_count,
        history_required_sessions=quality.history_required_sessions,
        candidate_feature_coverage_ratio=quality.candidate_feature_coverage_ratio,
        security_master_coverage_ratio=quality.security_master_coverage_ratio,
        history_coverage_ratio=quality.history_coverage_ratio,
        population_filter_reason_counts=tuple(quality.population_filter_reason_counts.items()),
        candidate_filter_reason_counts=tuple(quality.candidate_filter_reason_counts.items()),
        candidate_transient_reason_counts=tuple(quality.candidate_transient_reason_counts.items()),
        candidate_optional_reason_counts=tuple(quality.candidate_optional_reason_counts.items()),
        degraded_reasons=quality.degraded_reasons,
        supply_reason_counts=tuple(reasons.items()),
        primary_blocker=_primary_supply_blocker(quality, funnel, empty_reason=diagnostics.empty_reason),
    )


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


def _supply_summary(
    projection: ScoredLocalProjection,
) -> SupplySummary:
    requested = set(projection.native_input.requested_codes)
    features = tuple(
        feature for feature in projection.native_input.candidate_features if feature.quote.code in requested
    )
    complete_quotes = tuple(feature.quote for feature in features if _summary_quote_complete(feature))
    latest = max(
        complete_quotes,
        key=lambda quote: (quote.source_time, quote.received_time, quote.data_version),
        default=None,
    )
    highest = max((item.final_score for item in projection.local.items), default=None)
    total = projection.input_quality.candidate_count
    return SupplySummary(
        trade_date=projection.local.trade_date,
        quote_total_count=total,
        quote_covered_count=len(complete_quotes),
        quote_missing_count=max(0, total - len(complete_quotes)),
        security_identity_missing_count=max(
            0,
            total - projection.input_quality.security_master_covered_count,
        ),
        latest_quote_source=latest.source if latest is not None else None,
        latest_quote_source_time=latest.source_time if latest is not None else None,
        highest_final_score=highest,
    )


def _summary_quote_complete(feature: FeatureSnapshot) -> bool:
    quote = feature.quote
    return (
        quote.price is not None
        and math.isfinite(quote.price)
        and quote.price > 0.0
        and quote.pct_change is not None
        and math.isfinite(quote.pct_change)
        and bool(quote.source.strip())
        and quote.source_time.tzinfo is not None
        and quote.source_time.utcoffset() is not None
    )


def _primary_supply_blocker(
    quality: ScoredInputQuality,
    funnel: SupplyFunnel,
    *,
    empty_reason: str | None,
) -> str:
    action_blocker = "no_executable_candidates" if funnel.action_observe else "local_score_below_observation_floor"
    priorities = (
        (quality.candidate_feature_coverage_ratio < 1.0, "candidate_feature_coverage_incomplete"),
        (quality.security_master_coverage_ratio < 1.0, "security_master_coverage_incomplete"),
        (
            quality.status == "transient_invalid_empty"
            and funnel.full_scored == 0
            and quality.history_covered_count < quality.candidate_count,
            "strategy_history_unavailable",
        ),
        (funnel.full_scored == 0, "no_scored_candidates"),
        (empty_reason == "no_positive_net_utility", "no_positive_net_utility"),
        (funnel.review_eligible == 0, "no_review_eligible_candidates"),
        (funnel.action_executable == 0, action_blocker),
        (funnel.selected_executable == 0, "selection_constraints"),
    )
    return next((reason for blocked, reason in priorities if blocked), "ready")


def _candidate_codes(features: tuple[FeatureSnapshot, ...], limit: int) -> tuple[str, ...]:
    selected: list[str] = []
    for board in (Board.MAIN, Board.CHINEXT, Board.STAR):
        ordered = sorted(
            (feature for feature in features if feature.quote.board is board),
            key=lambda feature: (-candidate_score(feature, _CANDIDATE_WEIGHTS), feature.quote.code),
        )
        selected.extend(feature.quote.code for feature in ordered[:limit])
    return tuple(selected)


def _task_deadline(request: PipelineTaskRequest) -> datetime | None:
    seconds = task_execution_budget_seconds(request.task)
    return request.observed_at + timedelta(seconds=seconds) if seconds is not None else None


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


_CANDIDATE_WEIGHTS = {
    "liquidity": 0.25,
    "short_momentum": 0.25,
    "trend": 0.25,
    "data_completeness": 0.25,
}


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

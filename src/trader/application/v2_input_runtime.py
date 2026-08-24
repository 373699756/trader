"""V2 market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from trader.application.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.long_v2_runtime import LongV2Runtime
from trader.application.policy import RecommendationPolicy
from trader.application.ports.long import LongRefreshRequest
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.ports.reviews import DeepSeekReviewUnavailableError, TomorrowDeepSeekReviewPort
from trader.application.ports.runtime_status import V2InputQualityStatus, V2SupplyFunnel, V2SupplySummary
from trader.application.ports.tomorrow import D25NativeInput, TodayNativeInput, TomorrowNativeInput
from trader.application.ports.v2_runtime import (
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DataRefreshPort,
    V2DataRefreshUnavailableError,
    V2DecisionBuilderPort,
    V2DecisionUnavailableError,
    V2DeepSeekUpgradePort,
    V2FreezePort,
    V2FreezeUnavailableError,
    V2ResearchIntent,
    V2ReviewUnavailableError,
)
from trader.application.research_audit import (
    V2CommittedResearchAudit,
    try_build_v2_committed_research_audit,
)
from trader.application.today_v2_freezing import TodayV2FreezeCoordinator
from trader.application.today_v2_projection import (
    TodayV2LocalProjection,
    build_today_v2_hybrid,
    build_today_v2_local,
    validate_review_manifests,
)
from trader.application.tomorrow_quality import TomorrowInputQuality
from trader.application.tomorrow_v2_freezing import TomorrowV2FreezeCoordinator
from trader.application.tomorrow_v2_projection import (
    TomorrowV2LocalProjection,
    build_tomorrow_v2_hybrid,
    build_tomorrow_v2_local,
)
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.decision_identity import (
    DecisionIdentity,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
    identity_codes,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.domain.recommendation.ranking import candidate_score
from trader.domain.recommendation.tomorrow_selection import TomorrowDisposition


@dataclass(frozen=True)
class V2InputBatch:
    request: V2CycleRequest
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
class V2DecisionBuildDependencies:
    long_runtime: LongV2Runtime
    policy: RecommendationPolicy
    draft_index: UnifiedDecisionDraftIndex


class V2MarketReader(Protocol):
    def fetch_market_features(self, observed_at: datetime, *, force: bool = False) -> Sequence[FeatureSnapshot]: ...

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

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]: ...


class V2MarketDataAdapter(V2DataRefreshPort, V2DecisionBuilderPort):
    """Build immutable native inputs for the V2 scheduler."""

    def __init__(
        self,
        market: V2MarketReader,
        *,
        config_version: str,
        candidate_pool_size: int,
        decision_build: V2DecisionBuildDependencies,
    ) -> None:
        self._market = market
        self._config_version = config_version
        self._candidate_pool_size = max(1, candidate_pool_size)
        self._long_runtime = decision_build.long_runtime
        self._policy = decision_build.policy
        self._draft_index = decision_build.draft_index
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._batches: dict[tuple[Strategy, str], V2InputBatch] = {}
        self._shared_inputs: dict[tuple[date, datetime, str], _SharedInputBatch] = {}
        self._shared_failures: dict[tuple[date, datetime, str], str] = {}
        self._shared_loading: set[tuple[date, datetime, str]] = set()
        self._projections: dict[str, TodayV2LocalProjection | TomorrowV2LocalProjection] = {}
        self._decisions: dict[str, ScoredDecision] = {}
        self._input_quality: dict[Strategy, V2InputQualityStatus] = {}
        self._sequences = {strategy: 1 for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)}

    def refresh(self, request: V2CycleRequest) -> None:
        if request.strategy is Strategy.LONG:
            if not self._long_runtime.offer_refresh(LongRefreshRequest(request.observed_at, request.phase, force=True)):
                raise V2DataRefreshUnavailableError("long_refresh_rejected")
            return
        try:
            shared = self._shared_input(request)
            candidate_features = shared.candidate_features
            if request.strategy is Strategy.TOMORROW:
                candidate_features = tuple(
                    self._market.read_candidate_features(
                        shared.requested_codes,
                        request.observed_at,
                        include_intraday_tail=True,
                        include_structured_research=True,
                    )
                )
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise V2DataRefreshUnavailableError(_failure_code(exc)) from exc
        batch = V2InputBatch(
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

    def _shared_input(self, request: V2CycleRequest) -> _SharedInputBatch:
        key = (request.trade_date, request.observed_at, request.phase)
        with self._condition:
            while True:
                cached = self._shared_inputs.get(key)
                if cached is not None:
                    return cached
                failure = self._shared_failures.get(key)
                if failure is not None:
                    raise V2DataRefreshUnavailableError(failure)
                if key not in self._shared_loading:
                    self._shared_loading.add(key)
                    break
                if not self._condition.wait(timeout=30.0):
                    raise V2DataRefreshUnavailableError("shared_input_timeout")
        try:
            market_features = tuple(self._market.fetch_market_features(request.observed_at, force=False))
            requested = _candidate_codes(market_features, self._candidate_pool_size)
            self._schedule_reference_data(
                requested,
                tuple(feature.quote.code for feature in market_features),
                request.observed_at,
            )
            self._market.refresh_candidate_quotes(requested, request.observed_at, force=False)
            candidate_features = tuple(
                self._market.read_candidate_features(
                    requested,
                    request.observed_at,
                    include_intraday_tail=False,
                    include_structured_research=True,
                )
            )
            shared = _SharedInputBatch(market_features, requested, candidate_features)
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            failure = _failure_code(exc)
            with self._condition:
                self._shared_loading.discard(key)
                self._shared_failures[key] = failure
                self._trim_shared_inputs()
                self._condition.notify_all()
            raise V2DataRefreshUnavailableError(failure) from exc
        except BaseException:
            with self._condition:
                self._shared_loading.discard(key)
                self._condition.notify_all()
            raise
        with self._condition:
            self._shared_loading.discard(key)
            self._shared_inputs[key] = shared
            self._trim_shared_inputs()
            self._condition.notify_all()
        return shared

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

    def _trim_shared_inputs(self) -> None:
        while len(self._shared_inputs) > 8:
            self._shared_inputs.pop(next(iter(self._shared_inputs)))
        while len(self._shared_failures) > 8:
            self._shared_failures.pop(next(iter(self._shared_failures)))

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        draft = self._draft_index.snapshot(strategy)
        return draft is not None and draft.trade_date == trade_date

    def build_local(self, request: V2CycleRequest) -> DecisionIdentity | None:
        if request.strategy is Strategy.LONG:
            return None
        with self._lock:
            batch = self._batches.get((request.strategy, request.input_version))
            sequence = self._sequences[request.strategy]
            self._sequences[request.strategy] += 2
        if batch is None:
            raise V2DecisionUnavailableError("V2 native input is unavailable")
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
                projection = build_today_v2_local(today_native, self._policy, sequence=sequence)
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
                projection = build_tomorrow_v2_local(tomorrow_native, self._policy, sequence=sequence)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise V2DecisionUnavailableError(_decision_failure_code(exc)) from exc
        with self._lock:
            self._input_quality[request.strategy] = _supply_status(projection)
        if not projection.input_quality.publishable:
            self._draft_index.publish(projection.local)
            raise V2DecisionUnavailableError(projection.input_quality.status)
        with self._lock:
            self._projections[projection.local.version] = projection
            self._decisions[projection.local.version] = projection.local
            self._trim_research_sources()
        return projection.local

    def input_quality_status(self) -> tuple[V2InputQualityStatus, ...]:
        with self._lock:
            return tuple(
                self._input_quality[strategy] for strategy in sorted(self._input_quality, key=lambda item: item.value)
            )

    def projection(self, version: str) -> TodayV2LocalProjection | TomorrowV2LocalProjection | None:
        with self._lock:
            return self._projections.get(version)

    def register_hybrid(
        self,
        projection: TodayV2LocalProjection | TomorrowV2LocalProjection,
        decision: ScoredDecision,
    ) -> None:
        with self._lock:
            self._projections[decision.version] = projection
            self._decisions[decision.version] = decision
            self._trim_research_sources()

    def research_audit(self, version: str) -> V2CommittedResearchAudit | None:
        with self._lock:
            projection = self._projections.get(version)
            decision = self._decisions.get(version)
        if projection is None or decision is None:
            return None
        return try_build_v2_committed_research_audit(projection, decision)

    def research_intent(self, decision: ScoredDecision) -> V2ResearchIntent:
        with self._lock:
            projection = self._projections.get(decision.version)
        if projection is None:
            raise V2DecisionUnavailableError("research projection is unavailable")
        candidates = projection.native_input.requested_codes
        selected = sorted((item for item in decision.items if item.selected), key=lambda item: item.rank)
        remaining = tuple(item for item in decision.items if not item.selected)
        priority = tuple(dict.fromkeys(item.code for item in (*selected, *remaining)))
        return V2ResearchIntent(decision.strategy, decision.trade_date, priority, candidates)

    def initial_overlay(self, decision: ScoredDecision) -> DecisionOverlay:
        quotes = tuple(item.quote for item in decision.items if item.selected and item.quote is not None)
        selected_count = sum(item.selected for item in decision.items)
        if len(quotes) != selected_count:
            raise V2DecisionUnavailableError("decision_quote_unavailable")
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
        request: V2CycleRequest,
        previous: DecisionOverlay | None,
    ) -> DecisionOverlay | None:
        if decision.trade_date != request.trade_date:
            return None
        if previous is not None and previous.parent_version != decision.version:
            return None
        with self._lock:
            batch = self._batches.get((request.strategy, request.input_version))
        if batch is None:
            return None
        features_by_code = _selected_quote_features(batch, identity_codes(decision))
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
    projection: TodayV2LocalProjection | TomorrowV2LocalProjection,
) -> V2InputQualityStatus:
    quality = projection.input_quality
    requested = set(projection.native_input.requested_codes)
    evaluations = tuple(item for item in projection.selection.evaluations if item.code in requested)
    decision_items = projection.local.items
    funnel = V2SupplyFunnel(
        requested_candidates=quality.candidate_count,
        candidate_features=quality.candidate_feature_count,
        security_master=quality.security_master_covered_count,
        history=quality.history_covered_count,
        filter_pass=sum(item.disposition is TomorrowDisposition.PASS for item in evaluations),
        filter_observe=sum(item.disposition is TomorrowDisposition.OBSERVE_ONLY for item in evaluations),
        filter_reject=sum(item.disposition is TomorrowDisposition.REJECT for item in evaluations),
        full_scored=quality.candidate_scored_count,
        review_eligible=len(projection.review_candidates),
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
    return V2InputQualityStatus(
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
        candidate_feature_coverage_ratio=quality.candidate_feature_coverage_ratio,
        security_master_coverage_ratio=quality.security_master_coverage_ratio,
        history_coverage_ratio=quality.history_coverage_ratio,
        population_filter_reason_counts=tuple(quality.population_filter_reason_counts.items()),
        candidate_filter_reason_counts=tuple(quality.candidate_filter_reason_counts.items()),
        candidate_transient_reason_counts=tuple(quality.candidate_transient_reason_counts.items()),
        candidate_optional_reason_counts=tuple(quality.candidate_optional_reason_counts.items()),
        degraded_reasons=quality.degraded_reasons,
        supply_reason_counts=tuple(reasons.items()),
        primary_blocker=_primary_supply_blocker(quality, funnel),
    )


def _quote_order(feature: FeatureSnapshot) -> tuple[datetime, datetime, str]:
    quote = feature.quote
    return quote.source_time, quote.received_time, quote.data_version


def _selected_quote_features(
    batch: V2InputBatch,
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


def _overlay_observed_at(request: V2CycleRequest, features: tuple[FeatureSnapshot, ...]) -> datetime:
    target_zone = request.observed_at.tzinfo
    if target_zone is None:
        raise V2DecisionUnavailableError("overlay_request_time_unavailable")
    values = [request.observed_at]
    for feature in features:
        values.extend((feature.observed_at, feature.quote.received_time))
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise V2DecisionUnavailableError("overlay_input_time_unavailable")
    observed_at = max(value.astimezone(target_zone) for value in values)
    if observed_at.date() != request.trade_date:
        raise V2DecisionUnavailableError("overlay_observation_trade_date_mismatch")
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
    projection: TodayV2LocalProjection | TomorrowV2LocalProjection,
) -> V2SupplySummary:
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
    return V2SupplySummary(
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
    quality: TomorrowInputQuality,
    funnel: V2SupplyFunnel,
) -> str:
    action_blocker = "no_executable_candidates" if funnel.action_observe else "local_score_below_observation_floor"
    priorities = (
        (quality.candidate_feature_coverage_ratio < 1.0, "candidate_feature_coverage_incomplete"),
        (quality.security_master_coverage_ratio < 1.0, "security_master_coverage_incomplete"),
        (quality.history_coverage_ratio < 0.99, "history_coverage_incomplete"),
        (funnel.full_scored == 0, "no_scored_candidates"),
        (funnel.review_eligible == 0, "no_review_eligible_candidates"),
        (funnel.action_executable == 0, action_blocker),
        (funnel.selected_executable == 0, "selection_constraints"),
    )
    return next((reason for blocked, reason in priorities if blocked), "ready")


class V2DeepSeekAdapter(V2DeepSeekUpgradePort):
    def __init__(
        self,
        reviewer: TomorrowDeepSeekReviewPort,
        policy: RecommendationPolicy,
        data: V2MarketDataAdapter,
    ) -> None:
        self._reviewer = reviewer
        self._policy = policy
        self._data = data

    @property
    def runtime_contract(self) -> SharedDeepSeekRuntimeContract:
        return SharedDeepSeekRuntimeContract(168, True, True)

    def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision | None:
        projection = self._data.projection(local.version)
        if projection is None:
            return None
        candidates = projection.review_candidates
        if not candidates:
            return None
        deadline = request.review_deadline
        try:
            reviews = self._reviewer.review(
                request.strategy,
                tuple(candidate.features for candidate in candidates),
                phase=request.phase,
                deadline=deadline,
                contexts={candidate.code: candidate.context for candidate in candidates},
            )
        except (DeepSeekReviewUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise V2ReviewUnavailableError(type(exc).__name__) from exc
        expected = {
            candidate.code: self._reviewer.evidence_manifest_hash(candidate.features) for candidate in candidates
        }
        if not validate_review_manifests(projection, reviews, expected):
            return None
        if request.strategy is Strategy.TODAY:
            hybrid = build_today_v2_hybrid(projection, self._policy, reviews, review_deadline=deadline)
        else:
            hybrid = build_tomorrow_v2_hybrid(projection, self._policy, reviews, review_deadline=deadline)
        if hybrid is not None:
            self._data.register_hybrid(projection, hybrid)
        return hybrid


class V2FreezeAdapter(V2FreezePort):
    def __init__(
        self,
        today: TodayV2FreezeCoordinator,
        tomorrow: TomorrowV2FreezeCoordinator,
        d25: TomorrowV2FreezeCoordinator,
    ) -> None:
        self._freezers: dict[Strategy, TodayV2FreezeCoordinator | TomorrowV2FreezeCoordinator] = {
            Strategy.TODAY: today,
            Strategy.TOMORROW: tomorrow,
            Strategy.D25: d25,
        }

    def freeze(self, strategy: Strategy, at: datetime, current: DecisionIdentity | None) -> None:
        del at, current
        result = self._freezers[strategy].freeze_scheduled()
        if result.status in {"persistence_failed", "index_commit_conflict"}:
            raise RuntimeError(result.status)

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        at: datetime,
        current: ScoredDecision,
        *,
        recovery_path: Literal["current", "close_rebuild"],
        official_close_version: str,
    ) -> None:
        del at
        freezer = self._freezers.get(strategy)
        if not isinstance(freezer, TomorrowV2FreezeCoordinator):
            raise V2FreezeUnavailableError("close fallback is only available for tomorrow and d25")
        result = freezer.freeze_close_fallback(
            current,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
        )
        if result.status not in {"frozen", "already_frozen"}:
            raise V2FreezeUnavailableError(result.status)


def _candidate_codes(features: tuple[FeatureSnapshot, ...], limit: int) -> tuple[str, ...]:
    selected: list[str] = []
    for board in (Board.MAIN, Board.CHINEXT, Board.STAR):
        ordered = sorted(
            (feature for feature in features if feature.quote.board is board),
            key=lambda feature: (-candidate_score(feature, _CANDIDATE_WEIGHTS), feature.quote.code),
        )
        selected.extend(feature.quote.code for feature in ordered[:limit])
    return tuple(selected)


def _data_version(
    request: V2CycleRequest,
    market_features: tuple[FeatureSnapshot, ...],
    candidate_features: tuple[FeatureSnapshot, ...],
) -> str:
    versions = tuple(
        sorted((feature.quote.code, feature.quote.data_version) for feature in (*market_features, *candidate_features))
    )
    return f"{request.input_version}:{_stable_digest(versions)}"


def _decision_observed_at(batch: V2InputBatch) -> datetime:
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
    "V2DecisionBuildDependencies",
    "V2DeepSeekAdapter",
    "V2FreezeAdapter",
    "V2InputBatch",
    "V2MarketDataAdapter",
]

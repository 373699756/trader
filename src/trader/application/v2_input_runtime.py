"""V2 market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from trader.application.long_v2_runtime import LongV2Runtime
from trader.application.policy import RecommendationPolicy
from trader.application.ports.long import LongRefreshRequest
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.ports.reviews import DeepSeekReviewPort, DeepSeekReviewUnavailableError
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
    V2ReviewUnavailableError,
    V2SettlementPort,
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
from trader.application.tomorrow_v2_freezing import TomorrowV2FreezeCoordinator
from trader.application.tomorrow_v2_projection import (
    TomorrowV2LocalProjection,
    build_tomorrow_v2_hybrid,
    build_tomorrow_v2_local,
)
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.decision_identity import DecisionIdentity, ScoredDecision
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.ranking import candidate_score


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
        long_runtime: LongV2Runtime,
        policy: RecommendationPolicy,
    ) -> None:
        self._market = market
        self._config_version = config_version
        self._candidate_pool_size = max(1, candidate_pool_size)
        self._long_runtime = long_runtime
        self._policy = policy
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._batches: dict[tuple[Strategy, str], V2InputBatch] = {}
        self._shared_inputs: dict[tuple[date, datetime, str], _SharedInputBatch] = {}
        self._shared_failures: dict[tuple[date, datetime, str], str] = {}
        self._shared_loading: set[tuple[date, datetime, str]] = set()
        self._projections: dict[str, TodayV2LocalProjection | TomorrowV2LocalProjection] = {}
        self._decisions: dict[str, ScoredDecision] = {}
        self._sequences = {strategy: 1 for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)}

    def refresh(self, request: V2CycleRequest) -> None:
        if request.strategy is Strategy.LONG:
            self._long_runtime.offer_refresh(LongRefreshRequest(request.observed_at, request.phase, force=True))
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

    def _trim_shared_inputs(self) -> None:
        while len(self._shared_inputs) > 8:
            self._shared_inputs.pop(next(iter(self._shared_inputs)))
        while len(self._shared_failures) > 8:
            self._shared_failures.pop(next(iter(self._shared_failures)))

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
            if request.strategy is Strategy.TODAY:
                today_native = TodayNativeInput(
                    batch.request.trade_date,
                    batch.request.phase,
                    batch.data_version,
                    self._config_version,
                    batch.request.observed_at,
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
                    batch.request.observed_at,
                    batch.market_features,
                    batch.requested_codes,
                    batch.candidate_features,
                    30.0,
                    30.0,
                    self._candidate_pool_size,
                )
                projection = build_tomorrow_v2_local(tomorrow_native, self._policy, sequence=sequence)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise V2DecisionUnavailableError(type(exc).__name__) from exc
        if not projection.input_quality.publishable:
            raise V2DecisionUnavailableError(projection.input_quality.status)
        with self._lock:
            self._projections[projection.local.version] = projection
            self._decisions[projection.local.version] = projection.local
            self._trim_research_sources()
        return projection.local

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

    def _trim_research_sources(self) -> None:
        while len(self._decisions) > 64:
            version = next(iter(self._decisions))
            self._decisions.pop(version, None)
            self._projections.pop(version, None)


class V2DeepSeekAdapter(V2DeepSeekUpgradePort):
    def __init__(self, reviewer: DeepSeekReviewPort, policy: RecommendationPolicy, data: V2MarketDataAdapter) -> None:
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
        manifest = getattr(self._reviewer, "evidence_manifest_hash", None)
        expected = {
            candidate.code: manifest(candidate.features) if callable(manifest) else "" for candidate in candidates
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


class V2NoopSettlement(V2SettlementPort):
    """Outcome settlement is intentionally outside the V2 recommendation product."""

    def settle(self, at: datetime) -> None:
        del at


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


__all__ = [
    "V2DeepSeekAdapter",
    "V2FreezeAdapter",
    "V2InputBatch",
    "V2MarketDataAdapter",
    "V2NoopSettlement",
]

"""Application-owned identities for immutable v16 board scoring caches."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypeVar, cast

from trader.application.cache import BoundedCache, CacheIdentity, build_cache_identity, request_fingerprint
from trader.domain.board_scoring import (
    BOARD_SCHEMA_VERSION,
    MIN_BOARD_SAMPLE,
    BoardCrossSection,
    build_board_cross_section,
)
from trader.domain.models import Board, BoardStrategyPolicy, FeatureSnapshot
from trader.domain.strategies.composition import LocalScoreResult

_T = TypeVar("_T")
SessionDistance = Callable[[str, str], int | None]


@dataclass(frozen=True)
class ScoringCacheContext:
    trade_date: str
    phase: str
    merge_epoch: str
    data_version: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not all((self.trade_date, self.phase, self.merge_epoch, self.data_version)):
            raise ValueError("scoring cache context identity must not be empty")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("scoring cache observed_at must be timezone-aware")


class BoardScoringCache:
    """Thin identity layer over the shared bounded cache implementation."""

    def __init__(
        self,
        cache: BoundedCache[object],
        *,
        config_version: str,
        schema_version: str = BOARD_SCHEMA_VERSION,
        session_distance: SessionDistance | None = None,
    ) -> None:
        if not config_version or not schema_version:
            raise ValueError("board scoring cache versions must not be empty")
        self._cache = cache
        self._config_version = config_version
        self._schema_version = schema_version
        self._session_distance = session_distance or _weekday_session_distance

    def cross_section(
        self,
        board: Board,
        features: Sequence[FeatureSnapshot],
        context: ScoringCacheContext,
    ) -> BoardCrossSection:
        competition_groups, competition_group_version = self._competition_groups(board, features, context)
        feature_version = _feature_version(features)
        identity = self._identity(
            "board_cross_section",
            source="board-scoring",
            subject_key=board.value,
            context=context,
            request={
                "merge_epoch": context.merge_epoch,
                "data_version": context.data_version,
                "feature_version": feature_version,
                "competition_group_version": competition_group_version,
                "codes": tuple(item.quote.code for item in features),
            },
        )
        cached = self._value(identity, BoardCrossSection)
        if cached is not None:
            return cached

        latest_identity = self._latest_cross_section_identity(board)
        fallback = self._value(latest_identity, BoardCrossSection)
        fallback_age = (
            self._session_distance(fallback.trade_date, context.trade_date)
            if fallback is not None and fallback.trade_date != context.trade_date
            else (0 if fallback is not None else None)
        )

        def load() -> BoardCrossSection:
            return build_board_cross_section(
                features,
                board=board,
                merge_epoch=context.merge_epoch,
                trade_date=context.trade_date,
                phase=context.phase,
                data_version=context.data_version,
                schema_version=self._schema_version,
                fallback=fallback,
                fallback_age_sessions=fallback_age,
                competition_groups=competition_groups,
            )

        cross_section = cast(BoardCrossSection, self._cache.coalesce(identity, load))
        self._cache.put(
            identity,
            cross_section,
            data_version=cross_section.population.population_version,
            source_time=context.observed_at,
        )
        if cross_section.population.sample_size >= MIN_BOARD_SAMPLE and cross_section.population.status == "current":
            self._cache.put(
                latest_identity,
                cross_section,
                data_version=f"{context.trade_date}:{cross_section.population.population_version}",
                source_time=context.observed_at,
            )
        return cross_section

    def candidate_batch(
        self,
        policy: BoardStrategyPolicy,
        context: ScoringCacheContext,
        features: Sequence[FeatureSnapshot],
        loader: Callable[[], tuple[FeatureSnapshot, ...]],
    ) -> tuple[FeatureSnapshot, ...]:
        identity = self._policy_identity(
            "candidate_preselection",
            policy,
            context,
            subject_key=policy.board.value,
            request={
                "merge_epoch": context.merge_epoch,
                "policy_id": policy.policy_id,
                "population_versions": tuple(
                    sorted(
                        {
                            item.board_population.population_version
                            for item in features
                            if item.board_population is not None
                        }
                    )
                ),
                "feature_version": _feature_version(features),
                "codes": tuple(item.quote.code for item in features),
            },
        )
        cached = self._value(identity, tuple)
        if cached is not None and all(isinstance(item, FeatureSnapshot) for item in cached):
            return cast(tuple[FeatureSnapshot, ...], cached)
        result = cast(tuple[FeatureSnapshot, ...], self._cache.coalesce(identity, loader))
        self._cache.put(identity, result, data_version=context.merge_epoch, source_time=context.observed_at)
        return result

    def local_score(
        self,
        policy: BoardStrategyPolicy,
        context: ScoringCacheContext,
        feature: FeatureSnapshot,
        loader: Callable[[], LocalScoreResult],
    ) -> LocalScoreResult:
        identity = self._policy_identity(
            "local_score",
            policy,
            context,
            subject_key=feature.quote.code,
            request={
                "merge_epoch": context.merge_epoch,
                "policy_id": policy.policy_id,
                "board_population": (
                    feature.board_population.population_version if feature.board_population is not None else "missing"
                ),
                "quote_version": feature.quote.data_version,
                "feature_version": _feature_version((feature,)),
            },
        )
        cached = self._value(identity, LocalScoreResult)
        if cached is not None:
            return cached
        result = cast(LocalScoreResult, self._cache.coalesce(identity, loader))
        self._cache.put(identity, result, data_version=context.merge_epoch, source_time=context.observed_at)
        return result

    def _competition_groups(
        self,
        board: Board,
        features: Sequence[FeatureSnapshot],
        context: ScoringCacheContext,
    ) -> tuple[Mapping[str, tuple[str, str, str]], str]:
        industry_material = tuple(
            (item.quote.code, item.quote.industry.strip(), item.quote.data_version)
            for item in sorted(features, key=lambda feature: feature.quote.code)
        )
        industry_version = request_fingerprint({"industry": industry_material})[:24]
        manual_group_version = "manual:none:v1"
        composite_version = f"industry:{industry_version}+{manual_group_version}"
        identity = build_cache_identity(
            dataset="competition_group_mapping",
            source=f"board-scoring:{board.value}",
            subject_key="competition-groups",
            request={
                "industry_version": industry_version,
                "manual_group_version": manual_group_version,
            },
            trade_date="versioned",
            phase="all_day",
            source_contract_version="board_scoring_v16",
            config_version=self._config_version,
            schema_version=self._schema_version,
        )
        lookup = self._cache.get(identity)
        cached = lookup.value if lookup is not None and lookup.state not in {"negative", "degraded"} else None
        if isinstance(cached, Mapping) and all(
            isinstance(code, str)
            and isinstance(value, tuple)
            and len(value) == 3
            and all(isinstance(part, str) and part for part in value)
            for code, value in cached.items()
        ):
            return cast(Mapping[str, tuple[str, str, str]], cached), composite_version

        def load() -> dict[str, tuple[str, str, str]]:
            return {
                item.quote.code: (
                    item.quote.industry.strip() or "unknown",
                    "coarse_industry_fallback",
                    composite_version,
                )
                for item in features
            }

        groups = cast(Mapping[str, tuple[str, str, str]], self._cache.coalesce(identity, load))
        self._cache.put(identity, groups, data_version=composite_version, source_time=context.observed_at)
        return groups, composite_version

    def _policy_identity(
        self,
        dataset: str,
        policy: BoardStrategyPolicy,
        context: ScoringCacheContext,
        *,
        subject_key: str,
        request: dict[str, object],
    ) -> CacheIdentity:
        return self._identity(
            dataset,
            source=(
                f"{policy.strategy.value}:{policy.board.value}:{policy.policy_id}"
                if dataset == "candidate_preselection"
                else "board-scoring"
            ),
            subject_key=subject_key,
            context=context,
            request=request,
        )

    def _identity(
        self,
        dataset: str,
        *,
        source: str,
        subject_key: str,
        context: ScoringCacheContext,
        request: dict[str, object],
    ) -> CacheIdentity:
        return build_cache_identity(
            dataset=dataset,
            source=source,
            subject_key=subject_key,
            request=request,
            trade_date=context.trade_date,
            phase=context.phase,
            source_contract_version="board_scoring_v16",
            config_version=self._config_version,
            schema_version=self._schema_version,
        )

    def _latest_cross_section_identity(self, board: Board) -> CacheIdentity:
        return build_cache_identity(
            dataset="board_cross_section",
            source="board-scoring",
            subject_key=board.value,
            request={"kind": "latest_valid", "board": board.value},
            trade_date="latest",
            phase="all_day",
            source_contract_version="board_scoring_v16",
            config_version=self._config_version,
            schema_version=self._schema_version,
        )

    def _value(self, identity: CacheIdentity, expected_type: type[_T]) -> _T | None:
        lookup = self._cache.get(identity)
        if lookup is None or lookup.value is None or lookup.state in {"negative", "degraded"}:
            return None
        value = lookup.value
        return value if isinstance(value, expected_type) else None


def _weekday_session_distance(start: str, end: str) -> int | None:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        return None
    if end_date < start_date:
        return None
    count = 0
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _feature_version(features: Sequence[FeatureSnapshot]) -> str:
    material = tuple(
        (
            item.quote.code,
            item.quote.data_version,
            item.quote.industry,
            item.quote.board.value,
            item.quote.source_time,
            item.quote.received_time,
            tuple(
                _cache_number(value)
                for value in (
                    item.quote.price,
                    item.quote.previous_close,
                    item.quote.open_price,
                    item.quote.high,
                    item.quote.low,
                    item.quote.pct_change,
                    item.quote.change_5m,
                    item.quote.speed,
                    item.quote.volume_ratio,
                    item.quote.turnover_rate,
                    item.quote.amount,
                    item.quote.amplitude,
                    item.quote.market_cap,
                )
            ),
            item.quote.execution_restrictions,
            tuple((name, _cache_number(value)) for name, value in sorted(item.values.items())),
        )
        for item in sorted(features, key=lambda feature: feature.quote.code)
    )
    return request_fingerprint({"features": material})[:24]


def _cache_number(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


__all__ = ["BoardScoringCache", "ScoringCacheContext", "SessionDistance"]

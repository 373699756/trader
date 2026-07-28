"""Application-owned identities for immutable v16 board scoring caches."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TypeVar, cast

from trader.application.cache import (
    BoundedCache,
    CacheIdentity,
    CacheIdentitySpec,
    build_cache_identity,
)
from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
)
from trader.domain.recommendation.models import BoardStrategyPolicy
from trader.domain.recommendation.scoring import (
    BOARD_SCHEMA_VERSION,
    MIN_BOARD_SAMPLE,
    BoardCrossSection,
    BoardCrossSectionRequest,
    build_board_cross_section,
)

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
        identity = self._identity(
            "board_cross_section",
            source="board-scoring",
            subject_key=board.value,
            context=context,
            request={
                "merge_epoch": context.merge_epoch,
                "data_version": context.data_version,
            },
        )
        cached = self._value(identity, BoardCrossSection)
        if cached is not None:
            return cached

        competition_groups, _competition_group_version = self._competition_groups(board, features, context)
        latest_identity = self._latest_cross_section_identity(board)
        fallback = self._value(latest_identity, BoardCrossSection)
        fallback_age = (
            self._session_distance(fallback.trade_date, context.trade_date)
            if fallback is not None and fallback.trade_date != context.trade_date
            else (0 if fallback is not None else None)
        )

        def load() -> BoardCrossSection:
            return build_board_cross_section(
                BoardCrossSectionRequest(
                    features=features,
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
            )

        cross_section = cast(BoardCrossSection, self._cache.coalesce(identity, load))
        compact = replace(cross_section, features=(), normalization={})
        self._cache.put(
            identity,
            compact,
            data_version=cross_section.population.population_version,
            source_time=context.observed_at,
        )
        if cross_section.population.sample_size >= MIN_BOARD_SAMPLE and cross_section.population.status == "current":
            self._cache.put(
                latest_identity,
                compact,
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
                "codes": tuple(item.quote.code for item in features),
            },
        )
        cached = self._value(identity, tuple)
        by_code = {feature.quote.code: feature for feature in features}
        if cached is not None and all(isinstance(item, str) for item in cached):
            cached_codes = cast(tuple[str, ...], cached)
            if all(code in by_code for code in cached_codes):
                return tuple(by_code[code] for code in cached_codes)
        result = loader()
        result_codes = tuple(feature.quote.code for feature in result)
        self._cache.put(identity, result_codes, data_version=context.merge_epoch, source_time=context.observed_at)
        return result

    def _competition_groups(
        self,
        board: Board,
        features: Sequence[FeatureSnapshot],
        context: ScoringCacheContext,
    ) -> tuple[Mapping[str, tuple[str, str, str]], str]:
        industry_version = context.merge_epoch
        manual_group_version = "manual:none:v1"
        composite_version = f"industry:{industry_version}+{manual_group_version}"
        identity = build_cache_identity(
            CacheIdentitySpec(
                dataset="competition_group_mapping",
                source=f"board-scoring:{board.value}",
                subject_key="competition-groups",
                request={
                    "board": board.value,
                    "industry_version": industry_version,
                    "manual_group_version": manual_group_version,
                },
                trade_date="versioned",
                phase="all_day",
                source_contract_version="board_scoring_v16",
                config_version=self._config_version,
                schema_version=self._schema_version,
            )
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
            CacheIdentitySpec(
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
        )

    def _latest_cross_section_identity(self, board: Board) -> CacheIdentity:
        return build_cache_identity(
            CacheIdentitySpec(
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


__all__ = ["BoardScoringCache", "ScoringCacheContext", "SessionDistance"]

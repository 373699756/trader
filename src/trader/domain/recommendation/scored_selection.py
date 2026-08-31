"""Pure deterministic filtering, local scoring, and stable selection for scored strategies."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from trader.domain.market.factors import clamp, round_score
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.filters import HardFilterPolicy, hard_filter
from trader.domain.recommendation.models import BoardStrategyPolicy, FilterAudit, Strategy
from trader.domain.recommendation.scoring import (
    BoardCrossSection,
    BoardCrossSectionRequest,
    apply_board_policy,
    board_candidate_components,
    board_candidate_score,
    build_board_cross_section,
    candidate_fields,
    project_board_policy,
    score_board_strategy,
)
from trader.domain.recommendation.strategies.composition import LocalScoreResult
from trader.domain.review.models import RiskFact, RiskRule
from trader.domain.review.rules import aggregate_risk_penalty, derive_local_risk_facts

_SUPPORTED_BOARDS = (Board.MAIN, Board.CHINEXT, Board.STAR)
_SHANGHAI_TIMEZONE = "Asia/Shanghai"


class ScoredDisposition(str, Enum):
    PASS = "pass"
    OBSERVE_ONLY = "observe_only"
    REJECT = "reject"


@dataclass(frozen=True)
class BoardCrossSectionFallback:
    cross_section: BoardCrossSection
    age_sessions: int

    def __post_init__(self) -> None:
        if self.age_sessions < 0:
            raise ValueError("fallback age cannot be negative")


@dataclass(frozen=True)
class ScoredSelectionPolicy:
    board_policies: Mapping[Board, BoardStrategyPolicy]
    risk_rules: Mapping[str, RiskRule]
    max_age_seconds: float
    local_risk_cap: float
    candidate_limit_per_board: int = 120
    top_k: int = 10
    maximum_per_industry: int = 2
    minimum_local_score: float = 0.0
    hard_filter: HardFilterPolicy = field(default_factory=HardFilterPolicy)
    strategy: Strategy = Strategy.TOMORROW

    def __post_init__(self) -> None:
        policies = dict(self.board_policies)
        rules = dict(self.risk_rules)
        if set(policies) != set(_SUPPORTED_BOARDS):
            raise ValueError("scored selection requires one policy for each supported board")
        if any(policy.board is not board or policy.strategy is not self.strategy for board, policy in policies.items()):
            raise ValueError("scored selection board policies must match their board")
        if not math.isfinite(self.max_age_seconds) or self.max_age_seconds < 0.0:
            raise ValueError("maximum quote age must be finite and non-negative")
        if not math.isfinite(self.local_risk_cap) or self.local_risk_cap < 0.0:
            raise ValueError("local risk cap must be finite and non-negative")
        if not 1 <= self.candidate_limit_per_board <= 120:
            raise ValueError("candidate limit per board must be between 1 and 120")
        if not 0 <= self.top_k <= 10:
            raise ValueError("scored TopK must be between 0 and 10")
        if self.maximum_per_industry < 1:
            raise ValueError("maximum per industry must be positive")
        if not math.isfinite(self.minimum_local_score) or not 0.0 <= self.minimum_local_score <= 100.0:
            raise ValueError("minimum local score must be in [0, 100]")
        object.__setattr__(self, "board_policies", MappingProxyType(policies))
        object.__setattr__(self, "risk_rules", MappingProxyType(rules))


@dataclass(frozen=True)
class ScoredSelectionRequest:
    features: Sequence[FeatureSnapshot]
    evaluated_at: datetime
    trade_date: str
    phase: str
    data_version: str
    merge_epoch: str
    policy: ScoredSelectionPolicy
    candidate_features: Sequence[FeatureSnapshot] | None = None
    fallbacks: Mapping[Board, BoardCrossSectionFallback] = field(default_factory=lambda: MappingProxyType({}))
    local_score_overrides: Mapping[str, LocalScoreResult] | None = None
    population_evaluated_at: datetime | None = None
    population_max_age_seconds: float | None = None
    minimum_history_sessions: int = 20

    def __post_init__(self) -> None:
        features = tuple(self.features)
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluation time must be timezone-aware")
        if getattr(self.evaluated_at.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
            raise ValueError("evaluation time must use Asia/Shanghai")
        if not all((self.trade_date, self.phase, self.data_version, self.merge_epoch)):
            raise ValueError("scored selection identity must not be empty")
        codes = tuple(item.quote.code for item in features)
        if len(codes) != len(set(codes)):
            raise ValueError("scored selection features must contain unique codes")
        if any(item.observed_at > self.evaluated_at for item in features):
            raise ValueError("scored selection cannot use future features")
        if self.minimum_history_sessions < 1:
            raise ValueError("scored selection minimum history sessions must be positive")
        population_evaluated_at, population_max_age_seconds = _validated_population_window(
            features,
            candidate_evaluated_at=self.evaluated_at,
            population_evaluated_at=self.population_evaluated_at,
            population_max_age_seconds=self.population_max_age_seconds,
            default_max_age_seconds=self.policy.max_age_seconds,
        )
        candidate_features = _validated_candidate_features(
            self.candidate_features,
            population_codes=set(codes),
            evaluated_at=self.evaluated_at,
        )
        fallbacks = dict(self.fallbacks)
        if any(
            board not in _SUPPORTED_BOARDS or item.cross_section.board is not board for board, item in fallbacks.items()
        ):
            raise ValueError("scored fallbacks must match a supported board")
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "candidate_features", candidate_features)
        object.__setattr__(self, "fallbacks", MappingProxyType(fallbacks))
        object.__setattr__(self, "population_evaluated_at", population_evaluated_at)
        object.__setattr__(self, "population_max_age_seconds", population_max_age_seconds)
        if self.local_score_overrides is not None:
            overrides = dict(self.local_score_overrides)
            if not set(overrides).issubset(set(codes)):
                raise ValueError("local score overrides must belong to the scored population")
            object.__setattr__(self, "local_score_overrides", MappingProxyType(overrides))


def _validated_candidate_features(
    values: Sequence[FeatureSnapshot] | None,
    *,
    population_codes: set[str],
    evaluated_at: datetime,
) -> tuple[FeatureSnapshot, ...] | None:
    if values is None:
        return None
    candidates = tuple(values)
    codes = tuple(item.quote.code for item in candidates)
    if len(codes) != len(set(codes)):
        raise ValueError("scored selection candidates must contain unique codes")
    if not set(codes).issubset(population_codes):
        raise ValueError("scored selection candidates must belong to the population")
    if any(item.observed_at > evaluated_at for item in candidates):
        raise ValueError("scored selection cannot use future candidates")
    return candidates


def _validated_population_window(
    features: tuple[FeatureSnapshot, ...],
    *,
    candidate_evaluated_at: datetime,
    population_evaluated_at: datetime | None,
    population_max_age_seconds: float | None,
    default_max_age_seconds: float,
) -> tuple[datetime, float]:
    evaluated_at = population_evaluated_at or candidate_evaluated_at
    max_age_seconds = population_max_age_seconds if population_max_age_seconds is not None else default_max_age_seconds
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("population evaluation time must be timezone-aware")
    if getattr(evaluated_at.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError("population evaluation time must use Asia/Shanghai")
    if evaluated_at > candidate_evaluated_at:
        raise ValueError("population evaluation time cannot exceed candidate evaluation time")
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0.0:
        raise ValueError("population maximum quote age must be finite and non-negative")
    if any(
        value > evaluated_at
        for item in features
        for value in (item.observed_at, item.quote.source_time, item.quote.received_time)
    ):
        raise ValueError("scored population cannot contain data after its evaluation time")
    return evaluated_at, max_age_seconds


@dataclass(frozen=True)
class ScoredStockEvaluation:
    features: FeatureSnapshot
    disposition: ScoredDisposition
    filter_reasons: tuple[FilterAudit, ...] = ()
    optional_flags: tuple[FilterAudit, ...] = ()
    candidate_missing_ratio: float | None = None
    candidate_components: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    candidate_score: float | None = None
    candidate_rank: int = 0
    candidate_audit_rank: int = 0
    candidate_audit_pruning_reason: str = ""
    local_components: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    local_base_score: float | None = None
    local_risk_penalty: float | None = None
    local_score: float | None = None
    local_risk_facts: tuple[RiskFact, ...] = ()
    board_rank: int = 0
    rank: int = 0
    selection_skip_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_components", MappingProxyType(dict(self.candidate_components)))
        object.__setattr__(self, "local_components", MappingProxyType(dict(self.local_components)))

    @property
    def code(self) -> str:
        return self.features.quote.code


@dataclass(frozen=True)
class ScoredSelectionResult:
    evaluations: tuple[ScoredStockEvaluation, ...]
    scored_candidates: tuple[ScoredStockEvaluation, ...]
    observations: tuple[ScoredStockEvaluation, ...]
    selected: tuple[ScoredStockEvaluation, ...]
    population_versions: Mapping[Board, str]
    hard_filter_reason_counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    population_rejected_count: int = 0
    population_filter_reason_counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.population_rejected_count < 0:
            raise ValueError("scored population rejected count cannot be negative")
        object.__setattr__(self, "population_versions", MappingProxyType(dict(self.population_versions)))
        for name in (
            "hard_filter_reason_counts",
            "population_filter_reason_counts",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


def select_scored(request: ScoredSelectionRequest) -> ScoredSelectionResult:
    population_evaluations = _filter_features(
        request.features,
        request,
        evaluated_at=request.population_evaluated_at,
        max_age_seconds=request.population_max_age_seconds,
    )
    evaluations = dict(population_evaluations)
    candidate_evaluations = (
        population_evaluations
        if request.candidate_features is None
        else _filter_features(
            request.candidate_features,
            request,
            evaluated_at=request.evaluated_at,
            max_age_seconds=request.policy.max_age_seconds,
        )
    )
    evaluations.update(candidate_evaluations)
    population_versions: dict[Board, str] = {}
    scored_codes: list[str] = []
    for board in _SUPPORTED_BOARDS:
        population = tuple(
            item.features
            for item in population_evaluations.values()
            if item.disposition is not ScoredDisposition.REJECT and item.features.quote.board is board
        )
        candidates = tuple(
            item.features
            for item in candidate_evaluations.values()
            if item.disposition is not ScoredDisposition.REJECT and item.features.quote.board is board
        )
        if not population:
            continue
        fallback = request.fallbacks.get(board)
        cross_section = build_board_cross_section(
            BoardCrossSectionRequest(
                features=population,
                board=board,
                merge_epoch=request.merge_epoch,
                trade_date=request.trade_date,
                phase=request.phase,
                data_version=request.data_version,
                fallback=fallback.cross_section if fallback is not None else None,
                fallback_age_sessions=fallback.age_sessions if fallback is not None else None,
            )
        )
        population_versions[board] = cross_section.population.population_version
        policy = request.policy.board_policies[board]
        _audit_board_population(
            apply_board_policy(cross_section, request.policy.strategy, policy),
            policy,
            evaluations,
        )
        enriched = (
            apply_board_policy(cross_section, request.policy.strategy, policy)
            if request.candidate_features is None
            else project_board_policy(cross_section, request.policy.strategy, policy, candidates)
        )
        scored_codes.extend(_score_board_candidates(enriched, policy, request, evaluations))

    _rank_local_candidates(scored_codes, evaluations)
    selected_codes = _select_global(scored_codes, evaluations, request.policy)
    ordered_evaluations = tuple(evaluations[code] for code in sorted(evaluations))
    scored = tuple(
        sorted(
            (evaluations[code] for code in scored_codes),
            key=_local_order,
        )
    )
    observations = tuple(item for item in scored if item.disposition is ScoredDisposition.OBSERVE_ONLY)
    selected = tuple(evaluations[code] for code in selected_codes)
    population_filter_reason_counts: Counter[str] = Counter(
        reason.code for item in population_evaluations.values() for reason in item.filter_reasons
    )
    hard_filter_reason_counts = population_filter_reason_counts.copy()
    if request.candidate_features is not None:
        hard_filter_reason_counts.update(
            reason.code for item in candidate_evaluations.values() for reason in item.filter_reasons
        )
    return ScoredSelectionResult(
        ordered_evaluations,
        scored,
        observations,
        selected,
        population_versions,
        dict(sorted(hard_filter_reason_counts.items())),
        sum(item.disposition is ScoredDisposition.REJECT for item in population_evaluations.values()),
        dict(sorted(population_filter_reason_counts.items())),
    )


def _audit_board_population(
    features: Sequence[FeatureSnapshot],
    policy: BoardStrategyPolicy,
    evaluations: dict[str, ScoredStockEvaluation],
) -> None:
    ranked: list[tuple[bool, float, str]] = []
    required_fields = candidate_fields(policy.strategy)
    for feature in features:
        code = feature.quote.code
        current = evaluations[code]
        missing_ratio = feature.missing_ratio(required_fields)
        components = board_candidate_components(feature, policy)
        score = board_candidate_score(feature, policy)
        pruning_reason = ""
        if missing_ratio > 0.30:
            pruning_reason = "candidate_core_missing"
        elif score < policy.candidate_min_score:
            pruning_reason = "candidate_score_below_minimum"
        else:
            ranked.append((feature.board_data_reliability < policy.minimum_reliability, score, code))
        evaluations[code] = replace(
            current,
            features=feature,
            candidate_missing_ratio=round(missing_ratio, 6),
            candidate_components={name: round(value, 6) for name, value in components.items()},
            candidate_score=round_score(score),
            candidate_audit_pruning_reason=pruning_reason,
        )
    ranked.sort(key=lambda item: (item[0], -item[1], item[2]))
    for rank, (_unreliable, _score, code) in enumerate(ranked, start=1):
        evaluations[code] = replace(evaluations[code], candidate_audit_rank=rank)


def _filter_features(
    features: Sequence[FeatureSnapshot],
    request: ScoredSelectionRequest,
    *,
    evaluated_at: datetime | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, ScoredStockEvaluation]:
    filter_time = evaluated_at or request.evaluated_at
    quote_max_age = request.policy.max_age_seconds if max_age_seconds is None else max_age_seconds
    result: dict[str, ScoredStockEvaluation] = {}
    for feature in sorted(features, key=lambda item: item.quote.code):
        filtered = hard_filter(
            feature,
            filter_time,
            max_age_seconds=quote_max_age,
            policy=request.policy.hard_filter,
        )
        normalized = replace(feature, quote=replace(feature.quote, board=filtered.board))
        if not filtered.allowed:
            disposition = ScoredDisposition.REJECT
        elif filtered.optional_flags or feature.quote.execution_restrictions:
            disposition = ScoredDisposition.OBSERVE_ONLY
        else:
            disposition = ScoredDisposition.PASS
        result[feature.quote.code] = ScoredStockEvaluation(
            features=normalized,
            disposition=disposition,
            filter_reasons=filtered.reasons,
            optional_flags=filtered.optional_flags,
        )
    return result


def _score_board_candidates(
    features: Sequence[FeatureSnapshot],
    policy: BoardStrategyPolicy,
    request: ScoredSelectionRequest,
    evaluations: dict[str, ScoredStockEvaluation],
) -> tuple[str, ...]:
    candidates: list[tuple[bool, float, FeatureSnapshot, float]] = []
    required_fields = candidate_fields(request.policy.strategy)
    for feature in features:
        code = feature.quote.code
        current = evaluations[code]
        input_skip_reason = _score_input_skip_reason(feature, request)
        if input_skip_reason is not None:
            evaluations[code] = replace(current, selection_skip_reason=input_skip_reason)
            continue
        missing_ratio = feature.missing_ratio(required_fields)
        disposition = current.disposition
        optional_flags = current.optional_flags
        if feature.board_data_reliability < policy.minimum_reliability:
            disposition = ScoredDisposition.OBSERVE_ONLY
            optional_flags = (*optional_flags, _reliability_audit(feature, policy.minimum_reliability))
        current = replace(
            current,
            features=feature,
            disposition=disposition,
            optional_flags=optional_flags,
            candidate_missing_ratio=round(missing_ratio, 6),
            candidate_components={
                name: round(value, 6) for name, value in board_candidate_components(feature, policy).items()
            },
        )
        if missing_ratio > 0.30:
            evaluations[code] = replace(current, selection_skip_reason="candidate_core_missing")
            continue
        candidate_score = board_candidate_score(feature, policy)
        current = replace(current, candidate_score=round_score(candidate_score))
        if candidate_score < policy.candidate_min_score:
            evaluations[code] = replace(current, selection_skip_reason="candidate_score_below_minimum")
            continue
        evaluations[code] = current
        candidates.append(
            (
                feature.board_data_reliability < policy.minimum_reliability,
                candidate_score,
                feature,
                missing_ratio,
            )
        )
    candidates.sort(key=lambda row: (row[0], -row[1], row[2].quote.code))
    selected = candidates[: request.policy.candidate_limit_per_board]
    for _unreliable, _score, feature, _missing in candidates[request.policy.candidate_limit_per_board :]:
        evaluations[feature.quote.code] = replace(
            evaluations[feature.quote.code],
            selection_skip_reason="board_candidate_limit",
        )
    selected_codes: list[str] = []
    for candidate_rank, (_unreliable, _score, feature, _missing) in enumerate(selected, start=1):
        code = feature.quote.code
        local = (
            request.local_score_overrides.get(code)
            if request.local_score_overrides is not None
            else score_board_strategy(feature, policy)
        )
        if local is None:
            evaluations[code] = replace(
                evaluations[code],
                selection_skip_reason="production_model_features_missing",
            )
            continue
        local_facts = derive_local_risk_facts(
            feature,
            request.evaluated_at,
            request.policy.risk_rules,
            strategy=request.policy.strategy,
        )
        penalty = aggregate_risk_penalty(local_facts, cap=request.policy.local_risk_cap)
        current = evaluations[code]
        if any(fact.veto for fact in local_facts):
            current = replace(
                current,
                disposition=ScoredDisposition.OBSERVE_ONLY,
                selection_skip_reason="local_risk_veto",
            )
        evaluations[code] = replace(
            current,
            candidate_rank=candidate_rank,
            local_components=local.components,
            local_base_score=round_score(local.base_score),
            local_risk_penalty=round_score(penalty),
            local_score=round_score(clamp(local.base_score - penalty)),
            local_risk_facts=local_facts,
        )
        selected_codes.append(code)
    return tuple(selected_codes)


def _score_input_skip_reason(feature: FeatureSnapshot, request: ScoredSelectionRequest) -> str | None:
    if feature.history_days < request.minimum_history_sessions:
        return "strategy_history_insufficient"
    if request.local_score_overrides is not None and feature.quote.code not in request.local_score_overrides:
        return "production_model_features_missing"
    return None


def _rank_local_candidates(
    scored_codes: Sequence[str],
    evaluations: dict[str, ScoredStockEvaluation],
) -> None:
    by_board: dict[Board, list[ScoredStockEvaluation]] = {board: [] for board in _SUPPORTED_BOARDS}
    for code in scored_codes:
        item = evaluations[code]
        by_board[item.features.quote.board].append(item)
    for items in by_board.values():
        items.sort(key=_local_order)
        for rank, item in enumerate(items, start=1):
            evaluations[item.code] = replace(item, board_rank=rank)


def _select_global(
    scored_codes: Sequence[str],
    evaluations: dict[str, ScoredStockEvaluation],
    policy: ScoredSelectionPolicy,
) -> tuple[str, ...]:
    for code in scored_codes:
        item = evaluations[code]
        if (
            item.disposition is ScoredDisposition.PASS
            and item.local_score is not None
            and item.local_score < policy.minimum_local_score
        ):
            evaluations[code] = replace(item, selection_skip_reason="local_score_below_minimum")
    eligible = sorted(
        (
            evaluations[code]
            for code in scored_codes
            if evaluations[code].disposition is ScoredDisposition.PASS
            and (evaluations[code].local_score or 0.0) >= policy.minimum_local_score
        ),
        key=_local_order,
    )
    selected: list[str] = []
    industry_counts: dict[str, int] = {}
    for item in eligible:
        industry = item.features.quote.industry.strip() or "unknown"
        if len(selected) >= policy.top_k:
            evaluations[item.code] = replace(item, selection_skip_reason="top_k_limit")
            continue
        if industry_counts.get(industry, 0) >= policy.maximum_per_industry:
            evaluations[item.code] = replace(item, selection_skip_reason="industry_limit")
            continue
        selected.append(item.code)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        evaluations[item.code] = replace(item, rank=len(selected))
    return tuple(selected)


def _local_order(item: ScoredStockEvaluation) -> tuple[float, float, str]:
    return (-(item.local_score or 0.0), -(item.candidate_score or 0.0), item.code)


def _reliability_audit(feature: FeatureSnapshot, threshold: float) -> FilterAudit:
    return FilterAudit(
        stock_code=feature.quote.code,
        filter_code="board_data_reliability_below_threshold",
        threshold=f">= {threshold:g}",
        actual=round(feature.board_data_reliability, 6),
        source=feature.quote.source,
        observed_at=feature.quote.source_time,
    )


__all__ = [
    "BoardCrossSectionFallback",
    "ScoredDisposition",
    "ScoredSelectionPolicy",
    "ScoredSelectionRequest",
    "ScoredSelectionResult",
    "ScoredStockEvaluation",
    "select_scored",
]

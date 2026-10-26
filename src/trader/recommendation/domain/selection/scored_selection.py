"""Pure deterministic filtering, local scoring, and stable selection for scored strategies."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import pairwise
from types import MappingProxyType
from zoneinfo import ZoneInfo

from trader.recommendation.domain.candidate.filters import (
    ApplyFilters,
    FilterTier,
    HardFilterPolicy,
    LevelOneFilterRules,
    LevelTwoFilterRules,
)
from trader.recommendation.domain.evidence.review import RiskRule
from trader.recommendation.domain.market.factors import RoundScore
from trader.recommendation.domain.market.models import Board, FeatureSnapshot
from trader.recommendation.domain.publication.models import (
    BoardStrategyPolicy,
    FilterAudit,
    ScoredDisposition,
    ScoredSelectionResult,
    ScoredStockEvaluation,
    Strategy,
)
from trader.recommendation.domain.risk.rules import DeduplicateRiskFacts, DeriveLocalRiskFacts
from trader.recommendation.domain.scoring.company import (
    AssessCompanyAdjustments,
    BuildScoreLedger,
    CompanyFinancialScores,
    CompanyScoringPolicy,
    ScoreLedgerInput,
)
from trader.recommendation.domain.scoring.scoring import (
    ApplyBoardPolicy,
    BoardCrossSection,
    BoardCrossSectionRequest,
    BuildBoardCrossSection,
    ProjectBoardPolicy,
    ScoreBoardStrategy,
)

_SUPPORTED_BOARDS = (Board.MAIN, Board.CHINEXT, Board.STAR)
_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_STRUCTURED_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_DEFERRED_FILTER_REASONS = frozenset(
    {
        "future_quote",
        "invalid_amount",
        "invalid_cross_source_deviation",
        "invalid_pct_change",
        "invalid_price",
        "invalid_quote_structure",
        "invalid_quote_time",
        "stale_quote",
        "missing_liquidity_history",
        "invalid_liquidity_history",
    }
)


def SplitFilterReasonCounts(
    counts: Mapping[str, int],
) -> tuple[Mapping[str, int], Mapping[str, int]]:
    """Return the (business rejection, input readiness) partitions of a reason distribution."""
    business: dict[str, int] = {}
    readiness: dict[str, int] = {}
    for reason, count in counts.items():
        (readiness if reason in _DEFERRED_FILTER_REASONS else business)[reason] = count
    return MappingProxyType(business), MappingProxyType(readiness)


@dataclass(frozen=True)
class BoardCrossSectionFallback:
    cross_section: BoardCrossSection
    age_sessions: int

    def __post_init__(self) -> None:
        if self.age_sessions < 0:
            raise ValueError("fallback age cannot be negative")


@dataclass(frozen=True)
class _FilteredFeatures:
    evaluations: Mapping[str, ScoredStockEvaluation]
    issuer_eligible_count: int
    input_ready_count: int


@dataclass(frozen=True)
class ScoredSelectionPolicy:
    board_policies: Mapping[Board, BoardStrategyPolicy]
    risk_rules: Mapping[str, RiskRule]
    max_age_seconds: float
    local_risk_cap: float
    top_k: int = 10
    minimum_local_score: float = 0.0
    hard_filter: HardFilterPolicy = field(default_factory=HardFilterPolicy)
    strategy: Strategy = Strategy.TOMORROW
    company_scoring: CompanyScoringPolicy = CompanyScoringPolicy()

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
        if not 0 <= self.top_k <= 10:
            raise ValueError("scored TopK must be between 0 and 10")
        if not math.isfinite(self.minimum_local_score) or not 0.0 <= self.minimum_local_score <= 100.0:
            raise ValueError("minimum local score must be in [0, 100]")
        object.__setattr__(self, "board_policies", MappingProxyType(policies))
        object.__setattr__(self, "risk_rules", MappingProxyType(rules))


@dataclass(frozen=True)
class ScoredCandidateStageCounts:
    """Counts from the qualification and ranking pass.

    ``input_ready_population`` counts issuer-eligible stocks whose inputs are present and
    fresh, so missing or expired inputs never appear as dynamic-filter rejection.
    """

    issuer_eligible_population: int
    input_ready_population: int
    dynamic_filter_eligible: int
    strategy_history_eligible: int
    model_input_eligible: int
    scoring_input_eligible: int
    scoring_input_count: int

    def __post_init__(self) -> None:
        counts = (
            self.issuer_eligible_population,
            self.input_ready_population,
            self.dynamic_filter_eligible,
            self.strategy_history_eligible,
            self.model_input_eligible,
            self.scoring_input_eligible,
            self.scoring_input_count,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts) or any(
            left < right for left, right in pairwise(counts)
        ):
            raise ValueError("candidate stage counts must be non-negative and monotonic")


@dataclass(frozen=True)
class ScoredCandidatePlan:
    evaluations: tuple[ScoredStockEvaluation, ...]
    reserves: Mapping[Board, tuple[str, ...]]
    population_versions: Mapping[Board, str]
    stage_counts: ScoredCandidateStageCounts
    hard_filter_reason_counts: Mapping[str, int]
    population_rejected_count: int
    population_filter_reason_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        reserves = {board: tuple(codes) for board, codes in self.reserves.items()}
        if any(board not in _SUPPORTED_BOARDS for board in reserves):
            raise ValueError("candidate reserves require supported boards")
        flattened = tuple(code for board in _SUPPORTED_BOARDS for code in reserves.get(board, ()))
        if len(flattened) != len(set(flattened)):
            raise ValueError("candidate reserves must contain unique codes")
        object.__setattr__(self, "reserves", MappingProxyType(reserves))
        object.__setattr__(self, "population_versions", MappingProxyType(dict(self.population_versions)))
        object.__setattr__(
            self,
            "hard_filter_reason_counts",
            MappingProxyType(dict(self.hard_filter_reason_counts)),
        )
        object.__setattr__(
            self,
            "population_filter_reason_counts",
            MappingProxyType(dict(self.population_filter_reason_counts)),
        )
        if not isinstance(self.population_rejected_count, int) or isinstance(self.population_rejected_count, bool):
            raise ValueError("candidate population rejected count must be an integer")
        if self.population_rejected_count < 0:
            raise ValueError("candidate population rejected count cannot be negative")

    def qualified_codes(self) -> tuple[str, ...]:
        return tuple(code for board in _SUPPORTED_BOARDS for code in self.reserves.get(board, ()))


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
    execution_gate_reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    population_evaluated_at: datetime | None = None
    population_max_age_seconds: float | None = None
    minimum_history_sessions: int = 20
    model_input_eligible_codes: frozenset[str] | None = None

    def __post_init__(self) -> None:
        features, codes = _validated_selection_features(self)
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
        gate_reasons = dict(self.execution_gate_reasons)
        if not set(gate_reasons).issubset(codes):
            raise ValueError("execution gates must belong to the scored population")
        if any(_STRUCTURED_REASON.fullmatch(reason) is None for reason in gate_reasons.values()):
            raise ValueError("execution gate reasons must be structured")
        object.__setattr__(self, "execution_gate_reasons", MappingProxyType(gate_reasons))
        if self.model_input_eligible_codes is not None:
            eligible_codes = frozenset(self.model_input_eligible_codes)
            if not eligible_codes.issubset(codes):
                raise ValueError("model input eligible codes must belong to the scored population")
            object.__setattr__(self, "model_input_eligible_codes", eligible_codes)


def _validated_selection_features(
    request: ScoredSelectionRequest,
) -> tuple[tuple[FeatureSnapshot, ...], set[str]]:
    features = tuple(request.features)
    if request.evaluated_at.tzinfo is None or request.evaluated_at.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    if not (
        isinstance(request.evaluated_at.tzinfo, ZoneInfo) and request.evaluated_at.tzinfo.key == _SHANGHAI_TIMEZONE
    ):
        raise ValueError("evaluation time must use Asia/Shanghai")
    if not all((request.trade_date, request.phase, request.data_version, request.merge_epoch)):
        raise ValueError("scored selection identity must not be empty")
    codes = tuple(item.quote.code for item in features)
    if len(codes) != len(set(codes)):
        raise ValueError("scored selection features must contain unique codes")
    if any(item.observed_at > request.evaluated_at for item in features):
        raise ValueError("scored selection cannot use future features")
    if request.minimum_history_sessions < 1:
        raise ValueError("scored selection minimum history sessions must be positive")
    return features, set(codes)


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
    if not (isinstance(evaluated_at.tzinfo, ZoneInfo) and evaluated_at.tzinfo.key == _SHANGHAI_TIMEZONE):
        raise ValueError("population evaluation time must use Asia/Shanghai")
    if evaluated_at > candidate_evaluated_at:
        raise ValueError("population evaluation time cannot exceed candidate evaluation time")
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0.0:
        raise ValueError("population maximum quote age must be finite and non-negative")
    if any(value > evaluated_at for item in features for value in (item.observed_at, item.quote.received_time)):
        raise ValueError("scored population cannot contain data after its evaluation time")
    return evaluated_at, max_age_seconds


def SelectScored(request: ScoredSelectionRequest) -> ScoredSelectionResult:
    return ApplySelectionRisks(
        ScoreLocalEvidence(request),
        LocalRiskPolicy(request.policy.risk_rules, request.policy.local_risk_cap, request.policy.company_scoring),
        request.evaluated_at,
        request.policy.strategy,
    )


def ScoreLocalEvidence(request: ScoredSelectionRequest) -> ScoredSelectionResult:
    plan = PlanScoredCandidates(request)
    evaluations = {item.code: item for item in plan.evaluations}
    scored_codes: list[str] = []
    for board in _SUPPORTED_BOARDS:
        policy = request.policy.board_policies[board]
        scored_codes.extend(
            _score_board_candidates(
                plan.reserves.get(board, ()),
                policy,
                request,
                evaluations,
            )
        )

    _rank_local_candidates(scored_codes, evaluations)
    ordered_evaluations = tuple(evaluations[code] for code in sorted(evaluations))
    scored = tuple(sorted((evaluations[code] for code in scored_codes), key=_local_order))
    observations = tuple(item for item in scored if item.disposition is ScoredDisposition.OBSERVE_ONLY)
    return ScoredSelectionResult(
        evaluations=ordered_evaluations,
        scored_candidates=scored,
        observations=observations,
        population_versions=plan.population_versions,
        hard_filter_reason_counts=plan.hard_filter_reason_counts,
        population_rejected_count=plan.population_rejected_count,
        population_filter_reason_counts=plan.population_filter_reason_counts,
        stage_counts=plan.stage_counts,
    )


@dataclass(frozen=True)
class FilteredCandidateInputs:
    """Immutable result of the qualification pass, consumed by candidate ranking."""

    request: ScoredSelectionRequest
    population: _FilteredFeatures
    candidates: _FilteredFeatures


@dataclass(frozen=True)
class SelectedCandidateInput:
    code: str
    features: FeatureSnapshot | None = None

    def __post_init__(self) -> None:
        if self.features is not None and self.features.quote.code != self.code:
            raise ValueError("directed input must match its selected candidate")


def FilterCandidateInputs(request: ScoredSelectionRequest) -> FilteredCandidateInputs:
    population_filtered = _filter_features(
        request.features,
        request,
        evaluated_at=request.population_evaluated_at,
        max_age_seconds=request.population_max_age_seconds,
    )
    candidate_filtered = (
        population_filtered
        if request.candidate_features is None
        else _filter_features(
            request.candidate_features,
            request,
            evaluated_at=request.evaluated_at,
            max_age_seconds=request.policy.max_age_seconds,
        )
    )
    return FilteredCandidateInputs(request, population_filtered, candidate_filtered)


def PlanScoredCandidates(request: ScoredSelectionRequest) -> ScoredCandidatePlan:
    return RankFilteredCandidates(FilterCandidateInputs(request))


def RankFilteredCandidates(source: FilteredCandidateInputs) -> ScoredCandidatePlan:
    request = source.request
    population_filtered = source.population
    candidate_filtered = source.candidates
    population_evaluations = population_filtered.evaluations
    candidate_evaluations = candidate_filtered.evaluations
    evaluations = dict(population_evaluations)
    evaluations.update(candidate_evaluations)
    population_versions: dict[Board, str] = {}
    reserves: dict[Board, tuple[str, ...]] = {}
    for board in _SUPPORTED_BOARDS:
        population = tuple(
            item.features
            for item in population_evaluations.values()
            if _business_ready(item) and item.features.quote.board is board
        )
        candidates = tuple(
            item.features
            for item in candidate_evaluations.values()
            if _business_ready(item) and item.features.quote.board is board
        )
        if not population:
            continue
        fallback = request.fallbacks.get(board)
        cross_section = BuildBoardCrossSection(
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
        enriched_population = ApplyBoardPolicy(cross_section, request.policy.strategy, policy)
        enriched = (
            enriched_population
            if request.candidate_features is None
            else ProjectBoardPolicy(cross_section, request.policy.strategy, policy, candidates)
        )
        reserves[board] = _rank_board_candidates(
            enriched,
            policy,
            request,
            evaluations,
        )

    if request.candidate_features is None:
        assert request.population_evaluated_at is not None
        assert request.population_max_age_seconds is not None
        issuer_eligible = population_filtered.issuer_eligible_count
        input_ready = population_filtered.input_ready_count
    else:
        issuer_eligible = candidate_filtered.issuer_eligible_count
        input_ready = candidate_filtered.input_ready_count
    dynamic_eligible = sum(_business_ready(item) for item in candidate_evaluations.values())
    history_eligible = sum(
        _business_ready(item) and item.features.history_days >= request.minimum_history_sessions
        for item in candidate_evaluations.values()
    )
    model_input_eligible = sum(
        _business_ready(item)
        and item.features.history_days >= request.minimum_history_sessions
        and _model_input_is_eligible(item.code, request)
        for item in candidate_evaluations.values()
    )
    scoring_input_eligible = sum(len(codes) for codes in reserves.values())
    population_filter_reason_counts = dict(
        sorted(
            Counter(reason.code for item in population_evaluations.values() for reason in item.filter_reasons).items()
        )
    )
    hard_filter_reason_counts = Counter(population_filter_reason_counts)
    if request.candidate_features is not None:
        hard_filter_reason_counts.update(
            reason.code for item in candidate_evaluations.values() for reason in item.filter_reasons
        )
    return ScoredCandidatePlan(
        evaluations=tuple(evaluations[code] for code in sorted(evaluations)),
        reserves=reserves,
        population_versions=population_versions,
        stage_counts=ScoredCandidateStageCounts(
            issuer_eligible_population=issuer_eligible,
            input_ready_population=input_ready,
            dynamic_filter_eligible=dynamic_eligible,
            strategy_history_eligible=history_eligible,
            model_input_eligible=model_input_eligible,
            scoring_input_eligible=scoring_input_eligible,
            scoring_input_count=sum(len(codes) for codes in reserves.values()),
        ),
        hard_filter_reason_counts=dict(sorted(hard_filter_reason_counts.items())),
        population_rejected_count=sum(
            item.disposition is ScoredDisposition.REJECT for item in population_evaluations.values()
        ),
        population_filter_reason_counts=population_filter_reason_counts,
    )


def _filter_features(
    features: Sequence[FeatureSnapshot],
    request: ScoredSelectionRequest,
    *,
    evaluated_at: datetime | None = None,
    max_age_seconds: float | None = None,
) -> _FilteredFeatures:
    filter_time = evaluated_at or request.evaluated_at
    quote_max_age = request.policy.max_age_seconds if max_age_seconds is None else max_age_seconds
    rules = (
        *LevelOneFilterRules(max_age_seconds=quote_max_age, policy=request.policy.hard_filter),
        *LevelTwoFilterRules(
            max_age_seconds=quote_max_age,
            policy=request.policy.hard_filter,
        ),
    )
    permanent_filter_codes = frozenset(rule.name for rule in rules if rule.tier is FilterTier.ISSUER_PERMANENT)
    result: dict[str, ScoredStockEvaluation] = {}
    issuer_eligible_count = 0
    input_ready_count = 0
    for feature in sorted(features, key=lambda item: item.quote.code):
        filtered = ApplyFilters(feature, rules, now=filter_time)
        issuer_eligible = not any(reason.code in permanent_filter_codes for reason in filtered.reasons)
        if issuer_eligible:
            issuer_eligible_count += 1
        normalized = (
            feature
            if feature.quote.board is filtered.board
            else replace(feature, quote=replace(feature.quote, board=filtered.board))
        )
        deferred = (*filtered.deferred,)
        if issuer_eligible and not deferred:
            input_ready_count += 1
        filter_reasons = (*filtered.reasons, *deferred)
        if filtered.reasons:
            disposition = ScoredDisposition.REJECT
        elif deferred or filtered.optional_flags or feature.quote.execution_restrictions:
            disposition = ScoredDisposition.OBSERVE_ONLY
        else:
            disposition = ScoredDisposition.PASS
        result[feature.quote.code] = ScoredStockEvaluation(
            features=normalized,
            disposition=disposition,
            filter_reasons=filter_reasons,
            optional_flags=filtered.optional_flags,
            selection_skip_reason=""
            if filtered.reasons
            else _deferred_reason_codes(reason.code for reason in deferred),
        )
    return _FilteredFeatures(MappingProxyType(result), issuer_eligible_count, input_ready_count)


def _rank_board_candidates(
    features: Sequence[FeatureSnapshot],
    policy: BoardStrategyPolicy,
    request: ScoredSelectionRequest,
    evaluations: dict[str, ScoredStockEvaluation],
) -> tuple[str, ...]:
    eligible: list[str] = []
    for feature in features:
        current = evaluations[feature.quote.code]
        reason = _score_input_skip_reason(feature, request)
        if reason is not None:
            evaluations[current.code] = replace(current, features=feature, selection_skip_reason=reason)
            continue
        flags = current.optional_flags
        disposition = current.disposition
        if feature.board_data_reliability < policy.minimum_reliability:
            disposition = ScoredDisposition.OBSERVE_ONLY
            flags = (*flags, _reliability_audit(feature, policy.minimum_reliability))
        evaluations[current.code] = replace(current, features=feature, disposition=disposition, optional_flags=flags)
        eligible.append(current.code)
    return tuple(sorted(eligible))


def _score_board_candidates(
    ranked_codes: Sequence[str],
    policy: BoardStrategyPolicy,
    request: ScoredSelectionRequest,
    evaluations: dict[str, ScoredStockEvaluation],
) -> tuple[str, ...]:
    selected = tuple(ranked_codes)
    selected_codes: list[str] = []
    for candidate_rank, code in enumerate(selected, start=1):
        if code in request.execution_gate_reasons:
            evaluations[code] = replace(
                evaluations[code],
                execution_gate_reason=request.execution_gate_reasons[code],
                selection_skip_reason=request.execution_gate_reasons[code],
            )
            continue
        feature = evaluations[code].features
        local = ScoreBoardStrategy(feature, policy)
        current = evaluations[code]
        evaluations[code] = replace(
            current,
            candidate_rank=candidate_rank,
            local_components=local.components,
            local_base_score=local.base_score,
            local_risk_penalty=0.0,
            local_score=RoundScore(local.base_score),
            local_risk_facts=(),
            execution_gate_reason=request.execution_gate_reasons.get(code, ""),
        )
        selected_codes.append(code)
    return tuple(selected_codes)


@dataclass(frozen=True, slots=True)
class LocalRiskPolicy:
    rules: Mapping[str, RiskRule]
    cap: float
    company: CompanyScoringPolicy

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))


def ApplySelectionRisks(
    selection: ScoredSelectionResult,
    policy: LocalRiskPolicy,
    evaluated_at: datetime,
    strategy: Strategy,
) -> ScoredSelectionResult:
    evaluations = {item.code: item for item in selection.evaluations}
    scored_codes: list[str] = []
    for original in selection.scored_candidates:
        facts = DeriveLocalRiskFacts(original.features, evaluated_at, policy.rules, strategy=strategy)
        adjustments = AssessCompanyAdjustments(
            original.features.company_context,
            policy.company,
            strategy.value,
            evaluated_at,
            financial_scores=CompanyFinancialScores(
                original.features.optional_value("quality_score"),
                original.features.optional_value("value_score"),
                original.features.optional_value("growth_score"),
            ),
        )
        facts = DeduplicateRiskFacts((*facts, *adjustments.risks), rules=policy.rules)
        ledger = BuildScoreLedger(
            ScoreLedgerInput(
                original.local_base_score or 0.0,
                adjustments,
                facts,
                financial_contribution=original.local_components.get("financial_contribution", 0.0),
                growth_contribution=original.local_components.get("growth_contribution", 0.0),
            ),
            strategy=strategy.value,
            policy=policy.company,
            risk_cap=policy.cap,
        )
        evaluations[original.code] = replace(
            original,
            local_base_score=RoundScore(ledger.base_score),
            score_ledger=ledger,
            local_risk_facts=facts,
            local_risk_penalty=RoundScore(ledger.local_risk_penalty),
            local_score=ledger.local_score,
            disposition=ScoredDisposition.OBSERVE_ONLY if any(fact.veto for fact in facts) else original.disposition,
            selection_skip_reason="local_risk_veto"
            if any(fact.veto for fact in facts)
            else original.selection_skip_reason,
        )
        scored_codes.append(original.code)
    _rank_local_candidates(scored_codes, evaluations)
    scored = tuple(sorted((evaluations[code] for code in scored_codes), key=_local_order))
    return replace(
        selection,
        evaluations=tuple(evaluations[code] for code in sorted(evaluations)),
        scored_candidates=scored,
        observations=tuple(item for item in scored if item.disposition is ScoredDisposition.OBSERVE_ONLY),
    )


def _score_input_skip_reason(feature: FeatureSnapshot, request: ScoredSelectionRequest) -> str | None:
    if feature.model_history_quality is not None and feature.model_history_quality.failure:
        return feature.model_history_quality.failure
    if feature.history_days < request.minimum_history_sessions:
        return "strategy_history_insufficient"
    if not _model_input_is_eligible(feature.quote.code, request):
        return "production_model_features_missing"
    return None


def _business_ready(item: ScoredStockEvaluation) -> bool:
    return item.disposition is not ScoredDisposition.REJECT and not _DEFERRED_FILTER_REASONS.intersection(
        reason.code for reason in item.filter_reasons
    )


def _deferred_reason_codes(reasons: Iterable[str]) -> str:
    pending = _DEFERRED_FILTER_REASONS.intersection(reasons)
    if not pending:
        return ""
    return "refresh_pending" if pending.intersection({"stale_quote", "future_quote"}) else "data_pending"


def _model_input_is_eligible(code: str, request: ScoredSelectionRequest) -> bool:
    if request.model_input_eligible_codes is not None:
        return code in request.model_input_eligible_codes
    return True


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


def _local_order(item: ScoredStockEvaluation) -> tuple[float, str]:
    return (-(item.local_score or 0.0), item.code)


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
    "ScoredCandidatePlan",
    "ScoredCandidateStageCounts",
    "FilteredCandidateInputs",
    "FilterCandidateInputs",
    "RankFilteredCandidates",
    "ScoredSelectionPolicy",
    "ScoredSelectionRequest",
    "PlanScoredCandidates",
    "SelectScored",
    "ScoreLocalEvidence",
    "ApplySelectionRisks",
]

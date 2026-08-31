"""Classify native scored input before an empty decision can be published."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from trader.application.ports.scored import ScoredNativeInput
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.models import ScoredDisposition, ScoredSelectionResult

ScoredInputQualityStatus = Literal[
    "ready",
    "business_empty",
    "transient_invalid_empty",
    "not_ready",
]

_TRANSIENT_FILTER_REASONS = frozenset(
    {
        "stale_quote",
        "missing_liquidity_history",
        "invalid_liquidity_history",
    }
)
_TRANSIENT_SELECTION_REASONS = frozenset(
    {
        "candidate_core_missing",
        "production_model_features_missing",
        "strategy_history_insufficient",
    }
)
_SECURITY_IDENTITY_RESTRICTIONS = frozenset(
    {
        "board_identity_degraded",
        "missing_listing_date",
        "missing_listing_age_sessions",
    }
)


@dataclass(frozen=True)
class ScoredInputQuality:
    status: ScoredInputQualityStatus
    population_count: int
    candidate_count: int
    candidate_feature_count: int
    population_rejected_count: int
    candidate_rejected_count: int
    candidate_scored_count: int
    security_master_covered_count: int
    history_covered_count: int
    history_required_sessions: int
    candidate_feature_coverage_ratio: float
    security_master_coverage_ratio: float
    history_coverage_ratio: float
    population_filter_reason_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_filter_reason_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_transient_reason_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_optional_reason_counts: Mapping[str, int] = field(default_factory=dict)
    degraded_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "population_count",
            "candidate_count",
            "candidate_feature_count",
            "population_rejected_count",
            "candidate_rejected_count",
            "candidate_scored_count",
            "security_master_covered_count",
            "history_covered_count",
        ):
            if getattr(self, name) < 0:
                raise ValueError("scored input quality counts cannot be negative")
        _validate_history_requirement(self.history_required_sessions)
        if self.population_rejected_count > self.population_count:
            raise ValueError("scored rejected population cannot exceed population")
        if max(self.candidate_rejected_count, self.candidate_scored_count) > self.candidate_count:
            raise ValueError("scored candidate quality counts cannot exceed candidates")
        if (
            max(
                self.candidate_feature_count,
                self.security_master_covered_count,
                self.history_covered_count,
            )
            > self.candidate_count
        ):
            raise ValueError("scored candidate coverage counts cannot exceed candidates")
        for name in (
            "candidate_feature_coverage_ratio",
            "security_master_coverage_ratio",
            "history_coverage_ratio",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError("scored candidate coverage ratios must be in [0, 1]")
        for name in (
            "population_filter_reason_counts",
            "candidate_filter_reason_counts",
            "candidate_transient_reason_counts",
            "candidate_optional_reason_counts",
        ):
            values = dict(getattr(self, name))
            if any(not key or value < 0 for key, value in values.items()):
                raise ValueError("scored input quality reason counts must be non-negative")
            object.__setattr__(self, name, MappingProxyType(dict(sorted(values.items()))))
        object.__setattr__(self, "degraded_reasons", tuple(sorted(set(self.degraded_reasons))))

    @property
    def publishable(self) -> bool:
        return self.status in {"ready", "business_empty"}


def assess_scored_input_quality(
    native_input: ScoredNativeInput,
    selection: ScoredSelectionResult,
    *,
    minimum_history_sessions: int = 20,
    profile_history_qualified_codes: Collection[str] | None = None,
) -> ScoredInputQuality:
    if minimum_history_sessions < 1:
        raise ValueError("scored input minimum history sessions must be positive")
    requested_codes = set(native_input.requested_codes)
    candidate_codes = {feature.quote.code for feature in native_input.candidate_features}
    history_qualified_codes = (
        frozenset(profile_history_qualified_codes) if profile_history_qualified_codes is not None else None
    )
    if history_qualified_codes is not None and not history_qualified_codes <= candidate_codes:
        raise ValueError("profile history-qualified codes must be explicit candidates")
    evaluations = {item.code: item for item in selection.evaluations}
    candidate_evaluations = tuple(evaluations[code] for code in sorted(candidate_codes) if code in evaluations)
    if len(candidate_evaluations) != len(candidate_codes):
        raise ValueError("scored input quality requires every explicit candidate evaluation")
    candidate_filter_counts: Counter[str] = Counter(
        reason.code for item in candidate_evaluations for reason in item.filter_reasons
    )
    optional_counts: Counter[str] = Counter(
        code
        for item in candidate_evaluations
        for code in {
            *(reason.code for reason in item.optional_flags),
            *item.features.quote.execution_restrictions,
        }
    )
    transient_counts: Counter[str] = Counter(
        {code: count for code, count in candidate_filter_counts.items() if code in _TRANSIENT_FILTER_REASONS}
    )
    transient_counts.update(
        item.selection_skip_reason
        for item in candidate_evaluations
        if item.selection_skip_reason in _TRANSIENT_SELECTION_REASONS
    )
    candidate_scored_count = sum(item.local_score is not None for item in candidate_evaluations)
    candidate_by_code = {feature.quote.code: feature for feature in native_input.candidate_features}
    evaluated_features = {code: item.features for code, item in evaluations.items() if code in candidate_codes}
    security_master_covered_count = sum(
        _security_master_complete(evaluated_features.get(code)) for code in requested_codes
    )
    history_covered_count = sum(
        _history_complete(candidate_by_code.get(code), minimum_history_sessions=minimum_history_sessions)
        and (history_qualified_codes is None or code in history_qualified_codes)
        for code in requested_codes
    )
    requested_count = len(requested_codes)
    candidate_feature_coverage_ratio = _coverage_ratio(len(candidate_codes), requested_count)
    security_master_coverage_ratio = _coverage_ratio(security_master_covered_count, requested_count)
    history_coverage_ratio = _coverage_ratio(history_covered_count, requested_count)
    blocking_coverage_reasons = tuple(
        reason
        for failed, reason in (
            (candidate_feature_coverage_ratio < 1.0, "candidate_feature_coverage_incomplete"),
            (security_master_coverage_ratio < 1.0, "security_master_coverage_incomplete"),
        )
        if failed
    )
    history_reasons = ("strategy_history_coverage_partial",) if history_coverage_ratio < 1.0 else ()
    if not requested_codes or blocking_coverage_reasons:
        status: ScoredInputQualityStatus = "not_ready"
    elif candidate_scored_count:
        status = "ready"
    elif transient_counts:
        status = "transient_invalid_empty"
    else:
        status = "business_empty"
    population_filter_counts = dict(selection.population_filter_reason_counts)
    return ScoredInputQuality(
        status=status,
        population_count=len(native_input.market_features),
        candidate_count=requested_count,
        candidate_feature_count=len(candidate_codes),
        population_rejected_count=selection.population_rejected_count,
        candidate_rejected_count=sum(item.disposition is ScoredDisposition.REJECT for item in candidate_evaluations),
        candidate_scored_count=candidate_scored_count,
        security_master_covered_count=security_master_covered_count,
        history_covered_count=history_covered_count,
        history_required_sessions=minimum_history_sessions,
        candidate_feature_coverage_ratio=candidate_feature_coverage_ratio,
        security_master_coverage_ratio=security_master_coverage_ratio,
        history_coverage_ratio=history_coverage_ratio,
        population_filter_reason_counts=population_filter_counts,
        candidate_filter_reason_counts=candidate_filter_counts,
        candidate_transient_reason_counts=transient_counts,
        candidate_optional_reason_counts=optional_counts,
        degraded_reasons=(*tuple(optional_counts), *blocking_coverage_reasons, *history_reasons),
    )


def _coverage_ratio(covered: int, total: int) -> float:
    return round(covered / total, 6) if total else 0.0


def _validate_history_requirement(value: int) -> None:
    if value < 1:
        raise ValueError("scored input quality history requirement must be positive")


def _security_master_complete(feature: FeatureSnapshot | None) -> bool:
    if feature is None:
        return False
    quote = feature.quote
    return quote.board is not Board.UNSUPPORTED and not _SECURITY_IDENTITY_RESTRICTIONS.intersection(
        quote.execution_restrictions
    )


def _history_complete(feature: FeatureSnapshot | None, *, minimum_history_sessions: int) -> bool:
    if feature is None or feature.history_days < minimum_history_sessions:
        return False
    amount_median = feature.optional_value("amount_median_20d")
    return amount_median is not None and math.isfinite(amount_median) and amount_median > 0.0


__all__ = [
    "ScoredInputQuality",
    "ScoredInputQualityStatus",
    "assess_scored_input_quality",
]

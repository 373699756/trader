"""Classify native tomorrow input before an empty decision can be published."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from trader.application.ports.tomorrow import TomorrowNativeInput
from trader.domain.recommendation.tomorrow_selection import (
    TomorrowDisposition,
    TomorrowSelectionResult,
)

TomorrowInputQualityStatus = Literal[
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
_TRANSIENT_SELECTION_REASONS = frozenset({"candidate_core_missing"})


@dataclass(frozen=True)
class TomorrowInputQuality:
    status: TomorrowInputQualityStatus
    population_count: int
    candidate_count: int
    population_rejected_count: int
    candidate_rejected_count: int
    candidate_scored_count: int
    population_filter_reason_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_filter_reason_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_transient_reason_counts: Mapping[str, int] = field(default_factory=dict)
    candidate_optional_reason_counts: Mapping[str, int] = field(default_factory=dict)
    degraded_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "population_count",
            "candidate_count",
            "population_rejected_count",
            "candidate_rejected_count",
            "candidate_scored_count",
        ):
            if getattr(self, name) < 0:
                raise ValueError("tomorrow input quality counts cannot be negative")
        if self.population_rejected_count > self.population_count:
            raise ValueError("tomorrow rejected population cannot exceed population")
        if max(self.candidate_rejected_count, self.candidate_scored_count) > self.candidate_count:
            raise ValueError("tomorrow candidate quality counts cannot exceed candidates")
        for name in (
            "population_filter_reason_counts",
            "candidate_filter_reason_counts",
            "candidate_transient_reason_counts",
            "candidate_optional_reason_counts",
        ):
            values = dict(getattr(self, name))
            if any(not key or value < 0 for key, value in values.items()):
                raise ValueError("tomorrow input quality reason counts must be non-negative")
            object.__setattr__(self, name, MappingProxyType(dict(sorted(values.items()))))
        object.__setattr__(self, "degraded_reasons", tuple(sorted(set(self.degraded_reasons))))

    @property
    def publishable(self) -> bool:
        return self.status in {"ready", "business_empty"}

    def to_status(self) -> dict[str, object]:
        return {
            "status": self.status,
            "publishable": self.publishable,
            "population_count": self.population_count,
            "candidate_count": self.candidate_count,
            "population_rejected_count": self.population_rejected_count,
            "candidate_rejected_count": self.candidate_rejected_count,
            "candidate_scored_count": self.candidate_scored_count,
            "population_filter_reason_counts": dict(self.population_filter_reason_counts),
            "candidate_filter_reason_counts": dict(self.candidate_filter_reason_counts),
            "candidate_transient_reason_counts": dict(self.candidate_transient_reason_counts),
            "candidate_optional_reason_counts": dict(self.candidate_optional_reason_counts),
            "degraded_reasons": self.degraded_reasons,
        }


def assess_tomorrow_input_quality(
    native_input: TomorrowNativeInput,
    selection: TomorrowSelectionResult,
) -> TomorrowInputQuality:
    candidate_codes = {feature.quote.code for feature in native_input.candidate_features}
    evaluations = {item.code: item for item in selection.evaluations}
    candidate_evaluations = tuple(evaluations[code] for code in sorted(candidate_codes) if code in evaluations)
    if len(candidate_evaluations) != len(candidate_codes):
        raise ValueError("tomorrow input quality requires every explicit candidate evaluation")
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
    if not candidate_codes:
        status: TomorrowInputQualityStatus = "not_ready"
    elif candidate_scored_count:
        status = "ready"
    elif transient_counts:
        status = "transient_invalid_empty"
    else:
        status = "business_empty"
    population_filter_counts = dict(selection.population_filter_reason_counts)
    return TomorrowInputQuality(
        status=status,
        population_count=len(native_input.market_features),
        candidate_count=len(candidate_codes),
        population_rejected_count=selection.population_rejected_count,
        candidate_rejected_count=sum(item.disposition is TomorrowDisposition.REJECT for item in candidate_evaluations),
        candidate_scored_count=candidate_scored_count,
        population_filter_reason_counts=population_filter_counts,
        candidate_filter_reason_counts=candidate_filter_counts,
        candidate_transient_reason_counts=transient_counts,
        candidate_optional_reason_counts=optional_counts,
        degraded_reasons=tuple(optional_counts),
    )


__all__ = [
    "TomorrowInputQuality",
    "TomorrowInputQualityStatus",
    "assess_tomorrow_input_quality",
]

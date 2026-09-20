"""Deterministic profile-owned missing-value policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trader.recommendation.domain.evidence.quality import (
    MissingField,
    MissingSeverity,
    QualityAssessment,
    QualityState,
)
from trader.recommendation.domain.market.models import FeatureSnapshot


@dataclass(frozen=True, slots=True)
class MissingFieldRule:
    field_id: str
    severity: MissingSeverity
    penalty: Decimal
    model_required: bool

    def __post_init__(self) -> None:
        MissingField(self.field_id, self.severity, self.penalty)


@dataclass(frozen=True, slots=True)
class MissingValuePolicy:
    rules: tuple[MissingFieldRule, ...]

    def __post_init__(self) -> None:
        if not self.rules or len({item.field_id for item in self.rules}) != len(self.rules):
            raise ValueError("missing-value policy requires unique field rules")


def assess_missing_values(feature: FeatureSnapshot, policy: MissingValuePolicy) -> QualityAssessment:
    missing = tuple(
        MissingField(rule.field_id, rule.severity, rule.penalty)
        for rule in policy.rules
        if feature.optional_value(rule.field_id) is None
    )
    critical = any(item.severity is MissingSeverity.CRITICAL for item in missing)
    penalty = Decimal("100") if critical else min(Decimal("100"), sum((item.penalty for item in missing), Decimal("0")))
    missing_ids = {item.field_id for item in missing}
    model_input_eligible = not critical and not any(
        rule.model_required and rule.field_id in missing_ids for rule in policy.rules
    )
    return QualityAssessment(
        code=feature.quote.code,
        missing_fields=missing,
        highest_severity=(
            MissingSeverity.CRITICAL if critical else MissingSeverity.GENERAL if missing else None
        ),
        quality_penalty=penalty,
        model_input_eligible=model_input_eligible,
        state=(QualityState.CRITICAL_MISSING if critical else QualityState.DEGRADED if missing else QualityState.READY),
    )


__all__ = ["MissingFieldRule", "MissingValuePolicy", "assess_missing_values"]

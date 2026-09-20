"""Typed quality assessment consumed by recommendation scoring."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class MissingSeverity(str, Enum):
    GENERAL = "general"
    CRITICAL = "critical"


class QualityState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    CRITICAL_MISSING = "critical_missing"
    REFRESH_PENDING = "refresh_pending"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MissingField:
    field_id: str
    severity: MissingSeverity
    penalty: Decimal

    def __post_init__(self) -> None:
        if not self.field_id.strip() or not Decimal("0") <= self.penalty <= Decimal("100"):
            raise ValueError("missing-field policy is invalid")
        if self.severity is MissingSeverity.CRITICAL and self.penalty != Decimal("100"):
            raise ValueError("critical missing fields must deduct the complete score")


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    code: str
    missing_fields: tuple[MissingField, ...]
    highest_severity: MissingSeverity | None
    quality_penalty: Decimal
    model_input_eligible: bool
    state: QualityState

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("quality assessment code must be normalized")
        if len({item.field_id for item in self.missing_fields}) != len(self.missing_fields):
            raise ValueError("quality assessment missing fields must be unique")
        if not Decimal("0") <= self.quality_penalty <= Decimal("100"):
            raise ValueError("quality penalty must be in [0, 100]")
        expected_severity = (
            MissingSeverity.CRITICAL
            if any(item.severity is MissingSeverity.CRITICAL for item in self.missing_fields)
            else MissingSeverity.GENERAL if self.missing_fields else None
        )
        if self.highest_severity is not expected_severity:
            raise ValueError("quality severity must match missing fields")
        if expected_severity is MissingSeverity.CRITICAL:
            if self.quality_penalty != Decimal("100") or self.model_input_eligible:
                raise ValueError("critical missing input must score zero and skip the model")


__all__ = ["MissingField", "MissingSeverity", "QualityAssessment", "QualityState"]

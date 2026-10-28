"""Immutable score explanation and explicit persisted freeze schema."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import TypeAlias

LedgerJson: TypeAlias = str | int | float | bool | None | list["LedgerJson"] | dict[str, "LedgerJson"]


@dataclass(frozen=True, slots=True)
class ScoreAdjustmentItem:
    code: str
    kind: str
    label: str
    level: int
    rule_points: float
    applied_points: float
    reason: str
    evidence_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"financial_bonus", "positive_bonus", "risk", "review"}:
            raise ValueError("score_adjustment_kind_invalid")
        if any(not math.isfinite(x) or x < 0 for x in (self.rule_points, self.applied_points)):
            raise ValueError("score_adjustment_points_invalid")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("score_adjustment_expiry_requires_timezone")


@dataclass(frozen=True, slots=True)
class ScoreLedger:
    weighted_base_score: float
    requested_bonus: float
    capped_bonus: float
    applied_bonus: float
    base_score: float
    raw_risk_penalty: float
    local_risk_penalty: float
    applied_risk_penalty: float
    local_score: float
    rounding_adjustment: float
    financial_contribution: float
    growth_contribution: float
    company_risk_penalty: float
    company_risk_level: int | None
    company_coverage_end: date | None
    enabled: bool
    items: tuple[ScoreAdjustmentItem, ...]

    def __post_init__(self) -> None:
        values = (
            self.weighted_base_score,
            self.requested_bonus,
            self.capped_bonus,
            self.applied_bonus,
            self.base_score,
            self.raw_risk_penalty,
            self.local_risk_penalty,
            self.applied_risk_penalty,
            self.local_score,
            self.financial_contribution,
            self.growth_contribution,
            self.company_risk_penalty,
        )
        if any(not math.isfinite(x) or x < 0 for x in values):
            raise ValueError("score_ledger_values_invalid")
        if not math.isfinite(self.rounding_adjustment) or abs(self.rounding_adjustment) > 0.02:
            raise ValueError("score_ledger_rounding_invalid")
        if (
            not self.applied_bonus <= self.capped_bonus <= self.requested_bonus
            or not self.applied_risk_penalty <= self.local_risk_penalty <= self.raw_risk_penalty
        ):
            raise ValueError("score_ledger_order_invalid")
        if (
            abs(self.base_score - self.weighted_base_score - self.applied_bonus) > 1e-9
            or abs(self.local_score - (self.base_score - self.applied_risk_penalty)) > 0.005001
        ):
            raise ValueError("score_ledger_identity_invalid")
        if (
            not 0 <= self.weighted_base_score <= 100
            or not 0 <= self.base_score <= 100
            or not 0 <= self.local_score <= 100
        ):
            raise ValueError("score_ledger_bounds_invalid")


def ScoreLedgerPayload(ledger: ScoreLedger) -> dict[str, LedgerJson]:
    return {
        "weighted_base_score": float(ledger.weighted_base_score),
        "requested_bonus": float(ledger.requested_bonus),
        "capped_bonus": float(ledger.capped_bonus),
        "applied_bonus": float(ledger.applied_bonus),
        "base_score": float(ledger.base_score),
        "raw_risk_penalty": float(ledger.raw_risk_penalty),
        "local_risk_penalty": float(ledger.local_risk_penalty),
        "applied_risk_penalty": float(ledger.applied_risk_penalty),
        "local_score": float(ledger.local_score),
        "rounding_adjustment": float(ledger.rounding_adjustment),
        "financial_contribution": float(ledger.financial_contribution),
        "growth_contribution": float(ledger.growth_contribution),
        "company_risk_penalty": float(ledger.company_risk_penalty),
        "company_risk_level": ledger.company_risk_level,
        "enabled": ledger.enabled,
        "company_coverage_end": ledger.company_coverage_end.isoformat() if ledger.company_coverage_end else None,
        "items": [
            {
                "code": item.code,
                "kind": item.kind,
                "label": item.label,
                "level": item.level,
                "rule_points": float(item.rule_points),
                "applied_points": float(item.applied_points),
                "reason": item.reason,
                "evidence_ids": list(item.evidence_ids),
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            }
            for item in ledger.items
        ],
    }

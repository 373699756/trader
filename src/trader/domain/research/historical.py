"""Immutable Score-R2 interface values shared with the future E1 adapter."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

ResearchBoard = Literal["main", "chinext", "star"]
ResearchQualityStatus = Literal["complete", "degraded"]
ResearchSelectionPool = Literal["formal", "observation"]
SUPPORTED_RESEARCH_BOARDS: tuple[ResearchBoard, ...] = ("main", "chinext", "star")
_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_WEIGHT_TOLERANCE = 1e-9
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ResearchDataLineage:
    source: str
    source_time: datetime
    received_at: datetime
    quality_status: ResearchQualityStatus
    content_version: str
    content_hash: str

    def __post_init__(self) -> None:
        if not self.source or not self.content_version or _SHA256.fullmatch(self.content_hash) is None:
            raise ValueError("research data lineage identity is invalid")
        _require_shanghai_time(self.source_time, "lineage source_time")
        _require_shanghai_time(self.received_at, "lineage received_at")
        if self.received_at < self.source_time:
            raise ValueError("research data cannot be received before its source time")
        if self.quality_status not in {"complete", "degraded"}:
            raise ValueError("research data quality status is invalid")


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    weight: float
    value: float | None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("score component name must not be empty")
        if not math.isfinite(self.weight) or not 0.0 <= self.weight <= 1.0:
            raise ValueError("score component weight must be finite and in [0, 1]")
        if self.value is not None and (not math.isfinite(self.value) or not 0.0 <= self.value <= 100.0):
            raise ValueError("score component value must be finite and in [0, 100]")


@dataclass(frozen=True)
class HistoricalCandidateSummary:
    """Cheap point-in-time summary emitted only after production hard filters pass."""

    code: str
    board: ResearchBoard
    feature_as_of: datetime
    lineage: ResearchDataLineage
    candidate_components: tuple[ScoreComponent, ...]
    final_components: tuple[ScoreComponent, ...]
    industry: str = "unknown"
    eligible_pools: tuple[ResearchSelectionPool, ...] = ("formal", "observation")
    mandatory_local_risk_penalty: float = 0.0
    recorded_deepseek_score: float | None = None
    recorded_deepseek_risk_penalty: float = 0.0
    production_candidate_score: float = 0.0
    production_top120: bool = False
    candidate_core_missing_ratio: float = 0.0

    def __post_init__(self) -> None:
        _require_code(self.code)
        if self.board not in SUPPORTED_RESEARCH_BOARDS:
            raise ValueError("historical candidate requires a supported research board")
        _require_shanghai_time(self.feature_as_of, "feature_as_of")
        if self.lineage.received_at > self.feature_as_of:
            raise ValueError("candidate lineage cannot be received after feature_as_of")
        _validate_components(self.candidate_components)
        _validate_components(self.final_components)
        _validate_candidate_research_identity(self)
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "eligible_pools", tuple(sorted(set(self.eligible_pools))))


def _validate_candidate_research_identity(candidate: HistoricalCandidateSummary) -> None:
    if not candidate.industry.strip():
        raise ValueError("historical candidate industry must not be empty")
    pools = tuple(sorted(set(candidate.eligible_pools)))
    if not pools or any(pool not in {"formal", "observation"} for pool in pools):
        raise ValueError("historical candidate requires a supported selection pool")
    _validate_penalty(candidate.mandatory_local_risk_penalty, "mandatory local risk penalty")
    _validate_penalty(candidate.recorded_deepseek_risk_penalty, "recorded DeepSeek risk penalty")
    if candidate.recorded_deepseek_score is not None:
        _validate_score(candidate.recorded_deepseek_score, "recorded DeepSeek score")
    _validate_score(candidate.production_candidate_score, "production candidate score")
    if candidate.production_top120 and candidate.production_candidate_score < 50.0:
        raise ValueError("production Top120 candidate must satisfy the production candidate score gate")
    if (
        not math.isfinite(candidate.candidate_core_missing_ratio)
        or not 0.0 <= candidate.candidate_core_missing_ratio <= 1.0
    ):
        raise ValueError("candidate core missing ratio must be finite and in [0, 1]")
    if candidate.production_top120 and candidate.candidate_core_missing_ratio > 0.30:
        raise ValueError("production Top120 candidate must satisfy the core missing gate")


@dataclass(frozen=True)
class CostSettlementBasis:
    code: str
    board: ResearchBoard
    decision_date: date
    label_date: date
    gross_excess_return: float
    mae_atr20: float
    turnover: float

    def __post_init__(self) -> None:
        _require_code(self.code)
        if self.board not in SUPPORTED_RESEARCH_BOARDS:
            raise ValueError("settlement requires a supported research board")
        if self.label_date <= self.decision_date:
            raise ValueError("settlement label date must follow the decision date")
        for value in (self.gross_excess_return, self.mae_atr20, self.turnover):
            if not math.isfinite(value):
                raise ValueError("settlement values must be finite")
        if self.turnover < 0.0:
            raise ValueError("settlement turnover cannot be negative")


def _validate_components(components: tuple[ScoreComponent, ...]) -> None:
    if not components:
        raise ValueError("score components must not be empty")
    if len({item.name for item in components}) != len(components):
        raise ValueError("score component names must be unique")
    if not math.isclose(sum(item.weight for item in components), 1.0, rel_tol=0.0, abs_tol=_WEIGHT_TOLERANCE):
        raise ValueError("score component weights must sum to one")


def coverage_shrunk_score(components: tuple[ScoreComponent, ...]) -> float:
    """Return the preregistered missing-to-neutral Score-R2 research score."""

    _validate_components(components)
    known = tuple((component.weight, component.value) for component in components if component.value is not None)
    coverage = sum(weight for weight, _value in known)
    if coverage == 0.0:
        return 50.0
    weighted = sum(weight * value for weight, value in known if value is not None)
    return round(50.0 + coverage * (weighted / coverage - 50.0), 12)


def optimistic_final_upper_bound(
    components: tuple[ScoreComponent, ...],
    *,
    mandatory_local_risk_penalty: float,
    recorded_deepseek_score: float | None = None,
    recorded_deepseek_risk_penalty: float = 0.0,
) -> float:
    """Compute a safe Score-R2 upper bound without manufacturing model facts."""

    _validate_components(components)
    _validate_penalty(mandatory_local_risk_penalty, "mandatory local risk penalty")
    _validate_penalty(recorded_deepseek_risk_penalty, "recorded DeepSeek risk penalty")
    optimistic = sum(
        component.weight * (100.0 if component.value is None else component.value) for component in components
    )
    local_upper_bound = min(100.0, max(0.0, optimistic - mandatory_local_risk_penalty))
    if recorded_deepseek_score is None:
        if recorded_deepseek_risk_penalty != 0.0:
            raise ValueError("recorded DeepSeek risk penalty requires a recorded score")
        return local_upper_bound
    _validate_score(recorded_deepseek_score, "recorded DeepSeek score")
    hybrid = local_upper_bound * 0.68 + recorded_deepseek_score * 0.32 - recorded_deepseek_risk_penalty
    return min(100.0, max(0.0, hybrid))


def optimistic_component_upper_bound(components: tuple[ScoreComponent, ...]) -> float:
    """Return the weighted upper bound for a candidate or final score family."""

    _validate_components(components)
    return sum(component.weight * (100.0 if component.value is None else component.value) for component in components)


def _validate_score(value: float, label: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError(f"{label} must be finite and in [0, 100]")


def _validate_penalty(value: float, label: str) -> None:
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")


def _require_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("research stock code must contain exactly six digits")


def _require_shanghai_time(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{name} must use Asia/Shanghai")


__all__ = [
    "SUPPORTED_RESEARCH_BOARDS",
    "CostSettlementBasis",
    "HistoricalCandidateSummary",
    "ResearchDataLineage",
    "ResearchBoard",
    "ResearchSelectionPool",
    "ScoreComponent",
    "coverage_shrunk_score",
    "optimistic_final_upper_bound",
    "optimistic_component_upper_bound",
]

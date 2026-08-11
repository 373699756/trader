"""Immutable Score-R2 interface values shared with the future E1 adapter."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

ResearchBoard = Literal["main", "chinext", "star"]
ResearchQualityStatus = Literal["complete", "degraded"]
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

    def __post_init__(self) -> None:
        _require_code(self.code)
        if self.board not in SUPPORTED_RESEARCH_BOARDS:
            raise ValueError("historical candidate requires a supported research board")
        _require_shanghai_time(self.feature_as_of, "feature_as_of")
        if self.lineage.received_at > self.feature_as_of:
            raise ValueError("candidate lineage cannot be received after feature_as_of")
        _validate_components(self.candidate_components)
        _validate_components(self.final_components)


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
    "ScoreComponent",
]

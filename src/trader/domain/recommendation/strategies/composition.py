"""Shared recommendation score composition primitives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.domain.market.factors import weighted_score

WEIGHTED_EVIDENCE_SCORE_SCALE = "weighted_evidence_quality_0_100"


@dataclass(frozen=True)
class LocalScoreResult:
    components: Mapping[str, float]
    base_score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))


def compose(components: Mapping[str, float], weights: Mapping[str, float]) -> LocalScoreResult:
    return LocalScoreResult(components=components, base_score=weighted_score(components, weights))


__all__ = ["LocalScoreResult", "WEIGHTED_EVIDENCE_SCORE_SCALE", "compose"]

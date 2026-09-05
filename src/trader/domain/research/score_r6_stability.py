"""Immutable preregistration for daily ranking stability research."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date

from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC, ScoreR6DailyCandidate

_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")
_PARENT_REPORT_HASH = "aaa9a270aaecd0844c5786996a0318e6663812432a23f0c153fd33f256294ae2"
_PARENT_CANDIDATE_HASH = "c7d312a737a89eb6825aeb86a2c529caa7862de3038a9da35abfdd9bf2451c38"


@dataclass(frozen=True)
class ScoreR6StabilityCandidate:
    rank_persistence_bonus: float
    previous_score_weight: float
    entrant_turnover_penalty: float
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        values = (
            self.rank_persistence_bonus,
            self.previous_score_weight,
            self.entrant_turnover_penalty,
        )
        if values == (0.0, 0.0, 0.0):
            raise ValueError("daily stability candidate cannot be the all-zero control")
        if self.rank_persistence_bonus not in SCORE_R6_STABILITY_SPEC.rank_persistence_bonuses:
            raise ValueError("rank persistence bonus is outside the preregistered grid")
        if self.previous_score_weight not in SCORE_R6_STABILITY_SPEC.previous_score_weights:
            raise ValueError("previous score weight is outside the preregistered grid")
        if self.entrant_turnover_penalty not in SCORE_R6_STABILITY_SPEC.entrant_turnover_penalties:
            raise ValueError("entrant turnover penalty is outside the preregistered grid")
        object.__setattr__(self, "content_hash", _content_hash(self))


@dataclass(frozen=True)
class ScoreR6StabilitySpec:
    research_identity: str
    preregistered_on: date
    parent_research_identity: str
    parent_research_spec_hash: str
    parent_report_hash: str
    parent_candidate_hash: str
    parent_candidate: ScoreR6DailyCandidate
    rank_persistence_bonuses: tuple[float, ...]
    previous_score_weights: tuple[float, ...]
    entrant_turnover_penalties: tuple[float, ...]
    minimum_archive_coverage: float
    minimum_split_days: int
    minimum_selected_days: int
    minimum_turnover_reduction: float
    maximum_net_excess_loss_pct: float
    maximum_severe_loss_increase: float
    proxy_turnover_tolerance: float
    stability_tolerance: float
    recall_tolerance: float
    stock_concentration_tolerance: float
    objective_severe_coefficient: float
    objective_turnover_coefficient: float
    objective_stability_coefficient: float
    objective_recall_coefficient: float
    evidence_class: str
    data_schema_version: str
    report_schema_version: str
    promotion_authority: bool = False
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.research_identity != "score_r6_daily_stability" or _IDENTITY.fullmatch(self.research_identity) is None:
            raise ValueError("daily stability research identity is fixed")
        if self.preregistered_on != date(2026, 8, 21):
            raise ValueError("daily stability preregistration date is fixed")
        if (
            self.parent_research_identity != SCORE_R6_DAILY_SPEC.research_identity
            or self.parent_research_spec_hash != SCORE_R6_DAILY_SPEC.content_hash
            or self.parent_report_hash != _PARENT_REPORT_HASH
            or self.parent_candidate_hash != _PARENT_CANDIDATE_HASH
            or self.parent_candidate.content_hash != _PARENT_CANDIDATE_HASH
        ):
            raise ValueError("daily stability parent binding is fixed")
        if (
            self.rank_persistence_bonuses != (0.0, 2.0, 4.0)
            or self.previous_score_weights != (0.0, 0.25, 0.5)
            or self.entrant_turnover_penalties != (0.0, 2.0, 4.0)
        ):
            raise ValueError("daily stability candidate grid is fixed")
        fixed = (
            self.minimum_archive_coverage,
            self.minimum_split_days,
            self.minimum_selected_days,
            self.minimum_turnover_reduction,
            self.maximum_net_excess_loss_pct,
            self.maximum_severe_loss_increase,
            self.proxy_turnover_tolerance,
            self.stability_tolerance,
            self.recall_tolerance,
            self.stock_concentration_tolerance,
            self.objective_severe_coefficient,
            self.objective_turnover_coefficient,
            self.objective_stability_coefficient,
            self.objective_recall_coefficient,
        )
        if fixed != (0.95, 100, 100, 0.03, 0.10, 0.01, 0.05, 0.10, 0.0, 0.05, 8.0, 0.50, 0.25, 0.10):
            raise ValueError("daily stability metrics contract is fixed")
        if self.evidence_class != "reused_observed_validation_window":
            raise ValueError("daily stability evidence class is fixed")
        if (
            self.data_schema_version != SCORE_R6_DAILY_SPEC.data_schema_version
            or self.report_schema_version != "score_r6_daily_stability_report"
        ):
            raise ValueError("daily stability schema versions are fixed")
        if self.promotion_authority:
            raise ValueError("daily stability diagnostic cannot promote production")
        object.__setattr__(self, "content_hash", _content_hash(self))


def iter_score_r6_stability_candidates(
    spec: ScoreR6StabilitySpec,
) -> tuple[ScoreR6StabilityCandidate, ...]:
    candidates = tuple(
        ScoreR6StabilityCandidate(bonus, weight, penalty)
        for bonus in spec.rank_persistence_bonuses
        for weight in spec.previous_score_weights
        for penalty in spec.entrant_turnover_penalties
        if (bonus, weight, penalty) != (0.0, 0.0, 0.0)
    )
    return tuple(sorted(candidates, key=lambda item: item.content_hash))


def _content_hash(value: object) -> str:
    payload = {
        item.name: _canonical(getattr(value, item.name))
        for item in dataclasses.fields(value)  # type: ignore[arg-type]
        if item.init
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {item.name: _canonical(getattr(value, item.name)) for item in dataclasses.fields(value) if item.init}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("daily stability spec values must be finite")
    return value


_PARENT_CANDIDATE = ScoreR6DailyCandidate((2000, 3000, 3000, 1500, 500), 75, 8.0, -12.0)

SCORE_R6_STABILITY_SPEC = ScoreR6StabilitySpec(
    research_identity="score_r6_daily_stability",
    preregistered_on=date(2026, 8, 21),
    parent_research_identity=SCORE_R6_DAILY_SPEC.research_identity,
    parent_research_spec_hash=SCORE_R6_DAILY_SPEC.content_hash,
    parent_report_hash=_PARENT_REPORT_HASH,
    parent_candidate_hash=_PARENT_CANDIDATE_HASH,
    parent_candidate=_PARENT_CANDIDATE,
    rank_persistence_bonuses=(0.0, 2.0, 4.0),
    previous_score_weights=(0.0, 0.25, 0.5),
    entrant_turnover_penalties=(0.0, 2.0, 4.0),
    minimum_archive_coverage=0.95,
    minimum_split_days=100,
    minimum_selected_days=100,
    minimum_turnover_reduction=0.03,
    maximum_net_excess_loss_pct=0.10,
    maximum_severe_loss_increase=0.01,
    proxy_turnover_tolerance=0.05,
    stability_tolerance=0.10,
    recall_tolerance=0.0,
    stock_concentration_tolerance=0.05,
    objective_severe_coefficient=8.0,
    objective_turnover_coefficient=0.50,
    objective_stability_coefficient=0.25,
    objective_recall_coefficient=0.10,
    evidence_class="reused_observed_validation_window",
    data_schema_version=SCORE_R6_DAILY_SPEC.data_schema_version,
    report_schema_version="score_r6_daily_stability_report",
)

__all__ = [
    "SCORE_R6_STABILITY_SPEC",
    "ScoreR6StabilityCandidate",
    "ScoreR6StabilitySpec",
    "iter_score_r6_stability_candidates",
]

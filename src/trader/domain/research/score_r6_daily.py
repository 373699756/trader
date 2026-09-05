"""Immutable preregistration for risk-adjusted daily trend research."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date

from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC

_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")
_WEIGHT_SCALE = 10_000


@dataclass(frozen=True)
class ScoreR6DailyCandidate:
    weight_units: tuple[int, int, int, int, int]
    action_threshold: int
    recent_return_cap_pct: float
    drawdown_floor_pct: float
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if min(self.weight_units) < 0 or sum(self.weight_units) != _WEIGHT_SCALE:
            raise ValueError("daily trend weights must be nonnegative and sum exactly to one")
        if self.weight_units not in SCORE_R6_DAILY_SPEC.weight_candidates:
            raise ValueError("daily trend weights are outside the preregistered grid")
        if self.action_threshold not in SCORE_R6_DAILY_SPEC.action_thresholds:
            raise ValueError("daily trend threshold is outside the preregistered grid")
        if self.recent_return_cap_pct not in SCORE_R6_DAILY_SPEC.recent_return_caps_pct:
            raise ValueError("daily trend recent-return cap is outside the preregistered grid")
        if self.drawdown_floor_pct not in SCORE_R6_DAILY_SPEC.drawdown_floors_pct:
            raise ValueError("daily trend drawdown floor is outside the preregistered grid")
        object.__setattr__(self, "content_hash", _content_hash(self))

    @property
    def weights(self) -> tuple[float, float, float, float, float]:
        return tuple(value / _WEIGHT_SCALE for value in self.weight_units)  # type: ignore[return-value]


@dataclass(frozen=True)
class ScoreR6DailySpec:
    research_identity: str
    preregistered_on: date
    parent_research_identity: str
    parent_research_spec_hash: str
    component_names: tuple[str, str, str, str, str]
    weight_candidates: tuple[tuple[int, int, int, int, int], ...]
    action_thresholds: tuple[int, ...]
    recent_return_caps_pct: tuple[float, ...]
    drawdown_floors_pct: tuple[float, ...]
    minimum_archive_coverage: float
    minimum_split_days: int
    minimum_selected_days: int
    selection_limit: int
    maximum_per_board: int
    round_trip_cost_bps: int
    severe_loss_threshold_pct: float
    minimum_validation_gain_pct: float
    validation_turnover_tolerance: float
    validation_stability_tolerance: float
    validation_recall_tolerance: float
    validation_stock_concentration_tolerance: float
    objective_severe_coefficient: float
    objective_turnover_coefficient: float
    objective_stability_coefficient: float
    objective_recall_coefficient: float
    data_schema_version: str
    report_schema_version: str
    promotion_authority: bool = False
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_identity(self)
        _validate_candidate_grid(self)
        _validate_limits(self)
        _validate_metrics_contract(self)
        object.__setattr__(self, "content_hash", _content_hash(self))


def _validate_identity(spec: ScoreR6DailySpec) -> None:
    if spec.research_identity != "score_r6_daily_trend" or _IDENTITY.fullmatch(spec.research_identity) is None:
        raise ValueError("daily trend research identity is fixed")
    if spec.preregistered_on != date(2026, 8, 21):
        raise ValueError("daily trend preregistration date is fixed")
    if (
        spec.parent_research_identity != SCORE_H0_V1_SPEC.research_identity
        or spec.parent_research_spec_hash != SCORE_H0_V1_SPEC.content_hash
    ):
        raise ValueError("daily trend research must bind Score-H0")


def _validate_candidate_grid(spec: ScoreR6DailySpec) -> None:
    if spec.component_names != (
        "residual_momentum",
        "trend_efficiency",
        "downside_stability",
        "drawdown_recovery",
        "liquidity",
    ):
        raise ValueError("daily trend components are fixed")
    if spec.weight_candidates != _WEIGHT_CANDIDATES:
        raise ValueError("daily trend weight grid is fixed")
    if spec.action_thresholds != (70, 75, 80):
        raise ValueError("daily trend thresholds are fixed")
    if spec.recent_return_caps_pct != (8.0, 12.0) or spec.drawdown_floors_pct != (-12.0, -18.0):
        raise ValueError("daily trend hard-gate grid is fixed")


def _validate_limits(spec: ScoreR6DailySpec) -> None:
    if not 0.0 < spec.minimum_archive_coverage <= 1.0:
        raise ValueError("daily trend archive coverage is invalid")
    if min(spec.minimum_split_days, spec.minimum_selected_days, spec.selection_limit, spec.maximum_per_board) < 1:
        raise ValueError("daily trend sample and selection limits must be positive")
    if spec.selection_limit != 6 or spec.maximum_per_board != 4:
        raise ValueError("daily trend Top6 board constraint is fixed")


def _validate_metrics_contract(spec: ScoreR6DailySpec) -> None:
    fixed = (
        spec.round_trip_cost_bps,
        spec.severe_loss_threshold_pct,
        spec.minimum_validation_gain_pct,
        spec.validation_turnover_tolerance,
        spec.validation_stability_tolerance,
        spec.validation_recall_tolerance,
        spec.validation_stock_concentration_tolerance,
        spec.objective_severe_coefficient,
        spec.objective_turnover_coefficient,
        spec.objective_stability_coefficient,
        spec.objective_recall_coefficient,
    )
    if fixed != (20, -8.0, 0.10, 0.05, 0.10, 0.0, 0.05, 8.0, 0.05, 0.25, 0.10):
        raise ValueError("daily trend objective and validation gates are fixed")
    if (
        spec.data_schema_version != "score_r6_daily_trend_row"
        or spec.report_schema_version != "score_r6_daily_trend_report"
    ):
        raise ValueError("daily trend schema versions are fixed")
    if spec.promotion_authority:
        raise ValueError("daily trend historical research cannot promote production")


def iter_score_r6_daily_candidates(spec: ScoreR6DailySpec) -> tuple[ScoreR6DailyCandidate, ...]:
    candidates = tuple(
        ScoreR6DailyCandidate(weights, threshold, recent_cap, drawdown_floor)
        for weights in spec.weight_candidates
        for threshold in spec.action_thresholds
        for recent_cap in spec.recent_return_caps_pct
        for drawdown_floor in spec.drawdown_floors_pct
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
        raise ValueError("daily trend spec values must be finite")
    return value


_WEIGHT_CANDIDATES = (
    (3000, 2500, 2000, 1500, 1000),
    (2000, 3000, 3000, 1500, 500),
    (1500, 2500, 3500, 2000, 500),
    (4000, 2500, 1500, 1000, 1000),
)

SCORE_R6_DAILY_SPEC = ScoreR6DailySpec(
    research_identity="score_r6_daily_trend",
    preregistered_on=date(2026, 8, 21),
    parent_research_identity=SCORE_H0_V1_SPEC.research_identity,
    parent_research_spec_hash=SCORE_H0_V1_SPEC.content_hash,
    component_names=(
        "residual_momentum",
        "trend_efficiency",
        "downside_stability",
        "drawdown_recovery",
        "liquidity",
    ),
    weight_candidates=_WEIGHT_CANDIDATES,
    action_thresholds=(70, 75, 80),
    recent_return_caps_pct=(8.0, 12.0),
    drawdown_floors_pct=(-12.0, -18.0),
    minimum_archive_coverage=0.95,
    minimum_split_days=100,
    minimum_selected_days=100,
    selection_limit=6,
    maximum_per_board=4,
    round_trip_cost_bps=20,
    severe_loss_threshold_pct=-8.0,
    minimum_validation_gain_pct=0.10,
    validation_turnover_tolerance=0.05,
    validation_stability_tolerance=0.10,
    validation_recall_tolerance=0.0,
    validation_stock_concentration_tolerance=0.05,
    objective_severe_coefficient=8.0,
    objective_turnover_coefficient=0.05,
    objective_stability_coefficient=0.25,
    objective_recall_coefficient=0.10,
    data_schema_version="score_r6_daily_trend_row",
    report_schema_version="score_r6_daily_trend_report",
)

__all__ = [
    "SCORE_R6_DAILY_SPEC",
    "ScoreR6DailyCandidate",
    "ScoreR6DailySpec",
    "iter_score_r6_daily_candidates",
]

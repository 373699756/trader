"""Pure preregistration contracts for Score-R6 parameter research."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC

_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WEIGHT_SCALE = 10_000


@dataclass(frozen=True)
class ScoreR6HistoricalSpec:
    research_identity: str
    preregistered_on: date
    parent_research_identity: str
    parent_research_spec_hash: str
    component_names: tuple[str, str, str]
    current_weight_units: tuple[int, int, int]
    production_component_names: tuple[str, ...]
    current_production_board_weight_units: tuple[tuple[str, tuple[int, ...]], ...]
    candidate_simplex_step_units: int
    candidate_simplex_targets: tuple[tuple[int, int, int], ...]
    shrinkage_lambda_bps: tuple[int, ...]
    maximum_component_offset_units: int
    regularization_strength: float
    action_thresholds: tuple[int, ...]
    risk_penalties: tuple[int, ...]
    high_volatility_threshold_pct: float
    minimum_archive_coverage: float
    minimum_split_days: int
    minimum_board_rows: int
    minimum_board_days: int
    minimum_selected_days: int
    round_trip_cost_bps: int
    severe_loss_threshold_pct: float
    objective_severe_coefficient: float
    objective_turnover_coefficient: float
    objective_stability_coefficient: float
    objective_recall_coefficient: float
    validation_severe_tolerance: float
    validation_turnover_tolerance: float
    validation_stability_tolerance: float
    validation_recall_tolerance: float
    validation_stock_concentration_tolerance: float
    validation_board_concentration_tolerance: float
    objective_version: str
    tie_break_version: str
    promotion_authority: bool = False
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_historical_spec_identity(self)
        _validate_historical_spec_weights(self)
        _validate_historical_spec_grid(self)
        _validate_historical_spec_samples(self)
        _validate_historical_spec_objective(self)
        object.__setattr__(self, "content_hash", _content_hash(self))


def _validate_historical_spec_identity(spec: ScoreR6HistoricalSpec) -> None:
    if _IDENTITY.fullmatch(spec.research_identity) is None:
        raise ValueError("Score-R6 historical identity is invalid")
    if spec.research_identity != "score_r6_historical" or spec.preregistered_on != date(2026, 9, 1):
        raise ValueError("Score-R6 historical preregistration identity is fixed")
    if (
        spec.parent_research_identity != SCORE_H0_V1_SPEC.research_identity
        or spec.parent_research_spec_hash != SCORE_H0_V1_SPEC.content_hash
    ):
        raise ValueError("Score-R6 historical spec must bind Score-H0")
    if spec.promotion_authority:
        raise ValueError("Score-R6 retrospective screening cannot promote production")


def _validate_historical_spec_weights(spec: ScoreR6HistoricalSpec) -> None:
    if spec.component_names != ("momentum", "stability", "liquidity"):
        raise ValueError("Score-R6 historical components are fixed")
    if sum(spec.current_weight_units) != _WEIGHT_SCALE or min(spec.current_weight_units) < 0:
        raise ValueError("Score-R6 current weights must be nonnegative and sum exactly to one")
    if spec.production_component_names != _PRODUCTION_COMPONENTS:
        raise ValueError("Score-R6 production components are fixed")
    if spec.current_production_board_weight_units != _CURRENT_PRODUCTION_BOARD_WEIGHT_UNITS:
        raise ValueError("Score-R6 current production board weights are fixed")
    if not 0 <= spec.maximum_component_offset_units <= _WEIGHT_SCALE:
        raise ValueError("Score-R6 maximum component offset is invalid")


def _validate_historical_spec_grid(spec: ScoreR6HistoricalSpec) -> None:
    if spec.candidate_simplex_step_units <= 0 or _WEIGHT_SCALE % spec.candidate_simplex_step_units:
        raise ValueError("Score-R6 simplex step must divide the exact weight scale")
    if spec.candidate_simplex_targets != (
        (5000, 3000, 2000),
        (7000, 2000, 1000),
        (3000, 6000, 1000),
        (3000, 2000, 5000),
        (4000, 4000, 2000),
    ):
        raise ValueError("Score-R6 simplex target grid is fixed")
    if spec.shrinkage_lambda_bps != (0, 2500, 5000):
        raise ValueError("Score-R6 shrinkage lambda grid is fixed")
    if spec.action_thresholds != (76, 78, 80):
        raise ValueError("Score-R6 action threshold grid is fixed")
    if spec.risk_penalties != (3, 4, 5):
        raise ValueError("Score-R6 risk penalty grid is fixed")


def _validate_historical_spec_samples(spec: ScoreR6HistoricalSpec) -> None:
    if spec.regularization_strength != 0.15:
        raise ValueError("Score-R6 regularization strength is fixed")
    if spec.high_volatility_threshold_pct != 4.0:
        raise ValueError("Score-R6 reconstructed risk threshold is fixed")
    if not 0.0 < spec.minimum_archive_coverage <= 1.0:
        raise ValueError("Score-R6 archive coverage is invalid")
    if min(spec.minimum_split_days, spec.minimum_board_rows, spec.minimum_board_days, spec.minimum_selected_days) < 1:
        raise ValueError("Score-R6 sample gates must be positive")
    if spec.round_trip_cost_bps != SCORE_H0_V1_SPEC.round_trip_cost_bps:
        raise ValueError("Score-R6 historical cost must match Score-H0")


def _validate_historical_spec_objective(spec: ScoreR6HistoricalSpec) -> None:
    objective = (
        spec.severe_loss_threshold_pct,
        spec.objective_severe_coefficient,
        spec.objective_turnover_coefficient,
        spec.objective_stability_coefficient,
        spec.objective_recall_coefficient,
    )
    if objective != (-8.0, 2.0, 0.05, 0.25, 0.10):
        raise ValueError("Score-R6 historical objective coefficients are fixed")
    validation = (
        spec.validation_severe_tolerance,
        spec.validation_turnover_tolerance,
        spec.validation_stability_tolerance,
        spec.validation_recall_tolerance,
        spec.validation_stock_concentration_tolerance,
        spec.validation_board_concentration_tolerance,
    )
    if validation != (0.01, 0.05, 0.05, 0.02, 0.05, 0.05):
        raise ValueError("Score-R6 historical validation tolerances are fixed")
    if spec.objective_version != "net_excess_drawdown_turnover_stability_v1":
        raise ValueError("Score-R6 objective version is invalid")
    if spec.tie_break_version != "objective_offset_threshold_penalty_hash":
        raise ValueError("Score-R6 tie-break version is invalid")


@dataclass(frozen=True)
class ScoreR6Candidate:
    weight_units: tuple[int, int, int]
    action_threshold: int
    risk_penalty: int
    candidate_weight_units: tuple[int, int, int] = (5000, 3000, 2000)
    lambda_bps: int = 0
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if min(self.weight_units) < 0 or sum(self.weight_units) != _WEIGHT_SCALE:
            raise ValueError("Score-R6 candidate weights must be nonnegative and sum exactly to one")
        if self.action_threshold not in SCORE_R6_HISTORICAL_SPEC.action_thresholds:
            raise ValueError("Score-R6 candidate threshold is outside the preregistered grid")
        if self.risk_penalty not in SCORE_R6_HISTORICAL_SPEC.risk_penalties:
            raise ValueError("Score-R6 candidate risk penalty is outside the preregistered grid")
        if min(self.candidate_weight_units) < 0 or sum(self.candidate_weight_units) != _WEIGHT_SCALE:
            raise ValueError("Score-R6 simplex target must be nonnegative and sum exactly to one")
        if self.lambda_bps not in SCORE_R6_HISTORICAL_SPEC.shrinkage_lambda_bps:
            raise ValueError("Score-R6 candidate lambda is outside the preregistered grid")
        object.__setattr__(self, "content_hash", _content_hash(self))

    @property
    def weights(self) -> tuple[float, float, float]:
        return tuple(value / _WEIGHT_SCALE for value in self.weight_units)  # type: ignore[return-value]


@dataclass(frozen=True)
class ScoreR6ProductionBoardWeights:
    board: str
    component_names: tuple[str, ...]
    weight_units: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.board not in {"main", "chinext", "star"}:
            raise ValueError("Score-R6 production board is invalid")
        if self.component_names != _PRODUCTION_COMPONENTS or len(self.weight_units) != len(self.component_names):
            raise ValueError("Score-R6 production component identity is invalid")
        if min(self.weight_units) < 0 or sum(self.weight_units) != _WEIGHT_SCALE:
            raise ValueError("Score-R6 production weights must be nonnegative and sum exactly to one")


@dataclass(frozen=True)
class ScoreR6ProductionCandidate:
    historical_candidate_hash: str
    boards: tuple[ScoreR6ProductionBoardWeights, ...]
    action_threshold: int
    risk_penalty: int
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.historical_candidate_hash) is None:
            raise ValueError("Score-R6 production candidate must bind its historical candidate")
        if tuple(item.board for item in self.boards) != ("main", "chinext", "star"):
            raise ValueError("Score-R6 production candidate must bind all three boards")
        if self.action_threshold not in (76, 78, 80) or self.risk_penalty not in (3, 4, 5):
            raise ValueError("Score-R6 production threshold or penalty is outside the frozen grid")
        object.__setattr__(self, "content_hash", _content_hash(self))


def iter_score_r6_candidates(spec: ScoreR6HistoricalSpec) -> tuple[ScoreR6Candidate, ...]:
    """Build the complete finite joint grid without reading any outcome evidence."""

    current = spec.current_weight_units
    step = spec.candidate_simplex_step_units
    unique: dict[tuple[tuple[int, int, int], int, int], ScoreR6Candidate] = {}
    for simplex in spec.candidate_simplex_targets:
        if any(value % step for value in simplex):
            raise AssertionError("Score-R6 target is outside its preregistered simplex step")
        for lambda_bps in spec.shrinkage_lambda_bps:
            shrunk = (
                current[0] + ((simplex[0] - current[0]) * lambda_bps // _WEIGHT_SCALE),
                current[1] + ((simplex[1] - current[1]) * lambda_bps // _WEIGHT_SCALE),
                current[2] + ((simplex[2] - current[2]) * lambda_bps // _WEIGHT_SCALE),
            )
            if sum(shrunk) != _WEIGHT_SCALE:
                raise AssertionError("Score-R6 exact shrinkage changed the weight sum")
            if (
                max(abs(value - base) for value, base in zip(shrunk, current, strict=True))
                > spec.maximum_component_offset_units
            ):
                continue
            for threshold in spec.action_thresholds:
                for penalty in spec.risk_penalties:
                    target = current if lambda_bps == 0 else simplex
                    candidate = ScoreR6Candidate(shrunk, threshold, penalty, target, lambda_bps)
                    materialize_score_r6_production_candidate(candidate, spec)
                    unique.setdefault((shrunk, threshold, penalty), candidate)
    return tuple(unique[key] for key in sorted(unique))


def materialize_score_r6_production_candidate(
    candidate: ScoreR6Candidate,
    spec: ScoreR6HistoricalSpec,
) -> ScoreR6ProductionCandidate:
    """Apply the frozen target/lambda to each board's actual production weights."""

    boards: list[ScoreR6ProductionBoardWeights] = []
    adjustable_indices = (2, 3, 1)  # momentum->trend, stability->stability, liquidity->turnover_flow
    current_by_board = dict(spec.current_production_board_weight_units)
    for board in ("main", "chinext", "star"):
        current = current_by_board[board]
        adjustable_mass = sum(current[index] for index in adjustable_indices)
        numerators = tuple(
            current[index] * (_WEIGHT_SCALE - candidate.lambda_bps) * _WEIGHT_SCALE
            + adjustable_mass * target * candidate.lambda_bps
            for index, target in zip(adjustable_indices, candidate.candidate_weight_units, strict=True)
        )
        adjusted = _allocate_exact(numerators, _WEIGHT_SCALE * _WEIGHT_SCALE, adjustable_mass)
        weights = list(current)
        for index, value in zip(adjustable_indices, adjusted, strict=True):
            weights[index] = value
        if (
            max(abs(value - base) for value, base in zip(weights, current, strict=True))
            > spec.maximum_component_offset_units
        ):
            raise ValueError("Score-R6 production candidate exceeds the preregistered component offset")
        boards.append(ScoreR6ProductionBoardWeights(board, spec.production_component_names, tuple(weights)))
    return ScoreR6ProductionCandidate(
        candidate.content_hash,
        tuple(boards),
        candidate.action_threshold,
        candidate.risk_penalty,
    )


def _allocate_exact(numerators: tuple[int, ...], denominator: int, required_total: int) -> tuple[int, ...]:
    floors = [value // denominator for value in numerators]
    remaining = required_total - sum(floors)
    order = sorted(range(len(numerators)), key=lambda index: (-(numerators[index] % denominator), index))
    for index in order[:remaining]:
        floors[index] += 1
    if min(floors) < 0 or sum(floors) != required_total:
        raise AssertionError("Score-R6 exact production allocation failed")
    return tuple(floors)


def _content_hash(value: object) -> str:
    payload = {
        field.name: _canonical(getattr(value, field.name))
        for field in dataclasses.fields(value)  # type: ignore[arg-type]
        if field.init
    }
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in dataclasses.fields(value) if field.init}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


_PRODUCTION_COMPONENTS = (
    "tail_structure",
    "turnover_flow",
    "trend",
    "stability",
    "market_state",
    "entry_quality",
)
_CURRENT_PRODUCTION_BOARD_WEIGHT_UNITS = (
    ("main", (1667, 556, 2222, 2777, 1111, 1667)),
    ("chinext", (2500, 1875, 1875, 1250, 625, 1875)),
    ("star", (1667, 556, 2778, 2777, 555, 1667)),
)


SCORE_R6_HISTORICAL_SPEC = ScoreR6HistoricalSpec(
    research_identity="score_r6_historical",
    preregistered_on=date(2026, 9, 1),
    parent_research_identity=SCORE_H0_V1_SPEC.research_identity,
    parent_research_spec_hash=SCORE_H0_V1_SPEC.content_hash,
    component_names=("momentum", "stability", "liquidity"),
    current_weight_units=(5000, 3000, 2000),
    production_component_names=_PRODUCTION_COMPONENTS,
    current_production_board_weight_units=_CURRENT_PRODUCTION_BOARD_WEIGHT_UNITS,
    candidate_simplex_step_units=1000,
    candidate_simplex_targets=(
        (5000, 3000, 2000),
        (7000, 2000, 1000),
        (3000, 6000, 1000),
        (3000, 2000, 5000),
        (4000, 4000, 2000),
    ),
    shrinkage_lambda_bps=(0, 2500, 5000),
    maximum_component_offset_units=1500,
    regularization_strength=0.15,
    action_thresholds=(76, 78, 80),
    risk_penalties=(3, 4, 5),
    high_volatility_threshold_pct=4.0,
    minimum_archive_coverage=0.95,
    minimum_split_days=100,
    minimum_board_rows=5000,
    minimum_board_days=100,
    minimum_selected_days=80,
    round_trip_cost_bps=20,
    severe_loss_threshold_pct=-8.0,
    objective_severe_coefficient=2.0,
    objective_turnover_coefficient=0.05,
    objective_stability_coefficient=0.25,
    objective_recall_coefficient=0.10,
    validation_severe_tolerance=0.01,
    validation_turnover_tolerance=0.05,
    validation_stability_tolerance=0.05,
    validation_recall_tolerance=0.02,
    validation_stock_concentration_tolerance=0.05,
    validation_board_concentration_tolerance=0.05,
    objective_version="net_excess_drawdown_turnover_stability_v1",
    tie_break_version="objective_offset_threshold_penalty_hash",
)


__all__ = [
    "SCORE_R6_HISTORICAL_SPEC",
    "ScoreR6Candidate",
    "ScoreR6HistoricalSpec",
    "ScoreR6ProductionBoardWeights",
    "ScoreR6ProductionCandidate",
    "iter_score_r6_candidates",
    "materialize_score_r6_production_candidate",
]

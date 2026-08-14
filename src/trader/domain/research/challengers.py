"""Pure, preregistered Score-R4 challenger parameters and transformations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from trader.domain.research.historical import ResearchBoard

R4_PARAMETER_SET_VERSION = "score_r4_preregistered_parameters_v1"
R4_ENTRY_PARAMETER_VERSION = "score_r4_entry_parameters_v1"
R4_HEAT_PARAMETER_VERSION = "score_r4_heat_parameters_v1"

ChallengerVariantId = Literal[
    "continuous_entry",
    "coverage_shrink",
    "candidate_upper_bound",
    "heat_weak_structure",
    "combined_v1",
]
MembershipDirection = Literal["higher", "lower"]


@dataclass(frozen=True)
class EntryTransition:
    name: str
    direction: MembershipDirection
    lower_endpoint: float
    production_threshold: float
    upper_endpoint: float

    def __post_init__(self) -> None:
        if not self.name or not self.lower_endpoint < self.production_threshold < self.upper_endpoint:
            raise ValueError("entry transition endpoints must bracket the production threshold")


@dataclass(frozen=True)
class HeatBand:
    board: ResearchBoard
    lower_inclusive: float
    hard_cap_inclusive: float


@dataclass(frozen=True)
class WeakStructureThresholds:
    close_location_maximum: float = 35.0
    tail_return_30m_pct_maximum: float = -0.5
    intraday_drawdown_pct_minimum: float = 3.0


ENTRY_TRANSITIONS = (
    EntryTransition("ma5_ma10_spread_pct", "higher", -0.5, 0.0, 0.5),
    EntryTransition("ma10_ma20_spread_pct", "higher", -0.5, 0.0, 0.5),
    EntryTransition("ma20_slope_pct", "higher", -0.2, 0.0, 0.2),
    EntryTransition("price_ma5_distance_pct", "lower", 0.8, 1.0, 1.2),
    EntryTransition("price_ma10_distance_pct", "lower", 1.6, 2.0, 2.4),
    EntryTransition("pullback_volume_to_5d_average", "lower", 0.6, 0.7, 0.8),
    EntryTransition("price_ma20_spread_pct", "higher", -0.5, 0.0, 0.5),
    EntryTransition("price_prior_high_20d_spread_pct", "higher", -0.5, 0.0, 0.5),
    EntryTransition("breakout_volume_to_5d_average", "higher", 1.8, 2.0, 2.2),
    EntryTransition("close_location", "higher", 65.0, 70.0, 75.0),
    EntryTransition("breakout_deviation_pct", "lower", 4.0, 5.0, 6.0),
)
HEAT_BANDS = (HeatBand("main", 6.0, 8.0), HeatBand("chinext", 12.0, 16.0), HeatBand("star", 12.0, 16.0))
WEAK_STRUCTURE_THRESHOLDS = WeakStructureThresholds()


@dataclass(frozen=True)
class ChallengerSpecification:
    variant_id: ChallengerVariantId
    variant_version: str
    continuous_entry: bool = False
    coverage_shrink: bool = False
    candidate_upper_bound: bool = False
    heat_weak_structure: bool = False
    parameter_set_version: str = R4_PARAMETER_SET_VERSION

    def __post_init__(self) -> None:
        expected = {
            "continuous_entry": ("continuous_entry_v1", (True, False, False, False)),
            "coverage_shrink": ("coverage_shrink_v1", (False, True, False, False)),
            "candidate_upper_bound": ("candidate_upper_bound_v1", (False, False, True, False)),
            "heat_weak_structure": ("heat_weak_structure_v1", (False, False, False, True)),
            "combined_v1": ("combined_v1", (True, True, True, True)),
        }
        version, switches = expected[self.variant_id]
        actual = (
            self.continuous_entry,
            self.coverage_shrink,
            self.candidate_upper_bound,
            self.heat_weak_structure,
        )
        if self.variant_version != version or actual != switches:
            raise ValueError("Score-R4 challenger version and behavior switches do not match")
        if self.parameter_set_version != R4_PARAMETER_SET_VERSION:
            raise ValueError("Score-R4 challenger parameter-set identity is invalid")


_CHALLENGERS = (
    ChallengerSpecification("continuous_entry", "continuous_entry_v1", continuous_entry=True),
    ChallengerSpecification("coverage_shrink", "coverage_shrink_v1", coverage_shrink=True),
    ChallengerSpecification("candidate_upper_bound", "candidate_upper_bound_v1", candidate_upper_bound=True),
    ChallengerSpecification("heat_weak_structure", "heat_weak_structure_v1", heat_weak_structure=True),
    ChallengerSpecification(
        "combined_v1",
        "combined_v1",
        continuous_entry=True,
        coverage_shrink=True,
        candidate_upper_bound=True,
        heat_weak_structure=True,
    ),
)


@dataclass(frozen=True)
class R4ParameterManifest:
    parameter_set_version: str
    entry_parameter_version: str
    heat_parameter_version: str
    variants: tuple[ChallengerSpecification, ...]
    entry_transitions: tuple[EntryTransition, ...]
    heat_bands: tuple[HeatBand, ...]
    weak_structure: WeakStructureThresholds


_PARAMETER_MANIFEST = R4ParameterManifest(
    R4_PARAMETER_SET_VERSION,
    R4_ENTRY_PARAMETER_VERSION,
    R4_HEAT_PARAMETER_VERSION,
    _CHALLENGERS,
    ENTRY_TRANSITIONS,
    HEAT_BANDS,
    WEAK_STRUCTURE_THRESHOLDS,
)


def challenger_registry() -> tuple[ChallengerSpecification, ...]:
    """Return the fixed immutable five-variant family in manifest order."""

    return _CHALLENGERS


def challenger_parameter_manifest() -> R4ParameterManifest:
    """Return every frozen threshold and version that identifies an R4 replay."""

    return _PARAMETER_MANIFEST


@dataclass(frozen=True)
class ContinuousEntryInputs:
    price: float | None
    ma5: float | None
    ma10: float | None
    ma20: float | None
    ma20_slope_pct: float | None
    volume_to_5d_average: float | None
    prior_high_20d: float | None
    breakout_deviation_pct: float | None
    close_location: float | None

    def __post_init__(self) -> None:
        for value in (
            self.price,
            self.ma5,
            self.ma10,
            self.ma20,
            self.ma20_slope_pct,
            self.volume_to_5d_average,
            self.prior_high_20d,
            self.breakout_deviation_pct,
            self.close_location,
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError("continuous-entry inputs must be finite when present")
        for value in (self.price, self.ma5, self.ma10, self.ma20, self.prior_high_20d):
            if value is not None and value <= 0.0:
                raise ValueError("continuous-entry price and moving-average inputs must be positive")
        if self.volume_to_5d_average is not None and self.volume_to_5d_average < 0.0:
            raise ValueError("continuous-entry volume ratio cannot be negative")
        if self.close_location is not None and not 0.0 <= self.close_location <= 100.0:
            raise ValueError("continuous-entry close location must be in [0, 100]")


@dataclass(frozen=True)
class ContinuousEntryAssessment:
    status: Literal["scored", "critical_missing"]
    pullback_score: float | None
    breakout_score: float | None
    score: float | None
    parameter_version: str = R4_ENTRY_PARAMETER_VERSION

    def __post_init__(self) -> None:
        if self.parameter_version != R4_ENTRY_PARAMETER_VERSION:
            raise ValueError("continuous-entry parameter identity is invalid")
        for value in (self.pullback_score, self.breakout_score, self.score):
            if value is not None and not 0.0 <= value <= 100.0:
                raise ValueError("continuous-entry membership must be in [0, 100]")
        if self.status == "scored" and self.score is None:
            raise ValueError("scored continuous entry requires a score")
        if self.status == "critical_missing" and self.score is not None:
            raise ValueError("critical-missing continuous entry cannot manufacture a score")


def assess_continuous_entry(inputs: ContinuousEntryInputs) -> ContinuousEntryAssessment:
    """Apply the frozen centered piecewise memberships to both entry shapes."""

    pullback = _pullback_score(inputs)
    breakout = _breakout_score(inputs)
    known = tuple(value for value in (pullback, breakout) if value is not None)
    if known and (len(known) == 2 or max(known) == 100.0):
        return ContinuousEntryAssessment("scored", pullback, breakout, max(known))
    return ContinuousEntryAssessment("critical_missing", pullback, breakout, None)


def _pullback_score(inputs: ContinuousEntryInputs) -> float | None:
    required = (
        inputs.price,
        inputs.ma5,
        inputs.ma10,
        inputs.ma20,
        inputs.ma20_slope_pct,
        inputs.volume_to_5d_average,
    )
    if any(value is None for value in required):
        return None
    assert inputs.price is not None
    assert inputs.ma5 is not None and inputs.ma10 is not None and inputs.ma20 is not None
    assert inputs.ma20_slope_pct is not None and inputs.volume_to_5d_average is not None
    support = max(
        _membership("price_ma5_distance_pct", abs(inputs.price / inputs.ma5 - 1.0) * 100.0),
        _membership("price_ma10_distance_pct", abs(inputs.price / inputs.ma10 - 1.0) * 100.0),
    )
    return min(
        _membership("ma5_ma10_spread_pct", _spread(inputs.ma5, inputs.ma10)),
        _membership("ma10_ma20_spread_pct", _spread(inputs.ma10, inputs.ma20)),
        _membership("ma20_slope_pct", inputs.ma20_slope_pct),
        support,
        _membership("pullback_volume_to_5d_average", inputs.volume_to_5d_average),
        _membership("price_ma20_spread_pct", _spread(inputs.price, inputs.ma20)),
    )


def _breakout_score(inputs: ContinuousEntryInputs) -> float | None:
    required = (
        inputs.price,
        inputs.volume_to_5d_average,
        inputs.prior_high_20d,
        inputs.breakout_deviation_pct,
        inputs.close_location,
    )
    if any(value is None for value in required):
        return None
    assert inputs.price is not None and inputs.prior_high_20d is not None
    assert inputs.volume_to_5d_average is not None
    assert inputs.breakout_deviation_pct is not None and inputs.close_location is not None
    return min(
        _membership("price_prior_high_20d_spread_pct", _spread(inputs.price, inputs.prior_high_20d)),
        _membership("breakout_volume_to_5d_average", inputs.volume_to_5d_average),
        _membership("close_location", inputs.close_location),
        _membership("breakout_deviation_pct", inputs.breakout_deviation_pct),
    )


@dataclass(frozen=True)
class HeatWeakStructureInputs:
    change_pct: float | None
    close_location: float | None
    tail_return_30m_pct: float | None
    intraday_drawdown_pct: float | None

    def __post_init__(self) -> None:
        for value in (
            self.change_pct,
            self.close_location,
            self.tail_return_30m_pct,
            self.intraday_drawdown_pct,
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError("heat/weak-structure inputs must be finite when present")
        if self.close_location is not None and not 0.0 <= self.close_location <= 100.0:
            raise ValueError("heat/weak-structure close location must be in [0, 100]")
        if self.intraday_drawdown_pct is not None and self.intraday_drawdown_pct < 0.0:
            raise ValueError("intraday drawdown must be a non-negative magnitude")


@dataclass(frozen=True)
class HeatWeakStructureAssessment:
    in_high_heat_band: bool | None
    force_observe_only: bool
    reasons: tuple[str, ...]
    parameter_version: str = R4_HEAT_PARAMETER_VERSION

    def __post_init__(self) -> None:
        allowed = {"weak_close", "tail_weakening", "intraday_drawdown", "weak_structure_missing"}
        if self.parameter_version != R4_HEAT_PARAMETER_VERSION or any(reason not in allowed for reason in self.reasons):
            raise ValueError("heat/weak-structure assessment identity is invalid")
        if self.force_observe_only != bool(self.reasons):
            raise ValueError("heat/weak-structure observe state must match its reasons")


def assess_heat_weak_structure(
    board: ResearchBoard,
    inputs: HeatWeakStructureInputs,
) -> HeatWeakStructureAssessment:
    """Apply the frozen board heat band and conjunctive weak-structure rule."""

    band = next(item for item in HEAT_BANDS if item.board == board)
    lower, hard_cap = band.lower_inclusive, band.hard_cap_inclusive
    if inputs.change_pct is None:
        return HeatWeakStructureAssessment(None, True, ("weak_structure_missing",))
    if inputs.change_pct > hard_cap:
        raise ValueError("hard heat cap reject identity cannot enter Score-R4")
    if inputs.change_pct < lower:
        return HeatWeakStructureAssessment(False, False, ())
    weak_values = (inputs.close_location, inputs.tail_return_30m_pct, inputs.intraday_drawdown_pct)
    reasons: list[str] = []
    thresholds = WEAK_STRUCTURE_THRESHOLDS
    if inputs.close_location is not None and inputs.close_location <= thresholds.close_location_maximum:
        reasons.append("weak_close")
    if inputs.tail_return_30m_pct is not None and inputs.tail_return_30m_pct <= thresholds.tail_return_30m_pct_maximum:
        reasons.append("tail_weakening")
    if (
        inputs.intraday_drawdown_pct is not None
        and inputs.intraday_drawdown_pct >= thresholds.intraday_drawdown_pct_minimum
    ):
        reasons.append("intraday_drawdown")
    if not reasons and any(value is None for value in weak_values):
        reasons.append("weak_structure_missing")
    return HeatWeakStructureAssessment(True, bool(reasons), tuple(reasons))


def _spread(left: float, right: float) -> float:
    return (left / right - 1.0) * 100.0


def _membership(name: str, value: float) -> float:
    parameter = next(item for item in ENTRY_TRANSITIONS if item.name == name)
    if parameter.direction == "higher":
        return _higher_membership(value, parameter.lower_endpoint, parameter.upper_endpoint)
    return _lower_membership(value, parameter.lower_endpoint, parameter.upper_endpoint)


def _higher_membership(value: float, zero_at: float, full_at: float) -> float:
    if value <= zero_at:
        return 0.0
    if value >= full_at:
        return 100.0
    return (value - zero_at) / (full_at - zero_at) * 100.0


def _lower_membership(value: float, full_at: float, zero_at: float) -> float:
    return 100.0 - _higher_membership(value, full_at, zero_at)


__all__ = [
    "R4_ENTRY_PARAMETER_VERSION",
    "R4_HEAT_PARAMETER_VERSION",
    "R4_PARAMETER_SET_VERSION",
    "ChallengerSpecification",
    "ChallengerVariantId",
    "ContinuousEntryAssessment",
    "ContinuousEntryInputs",
    "HeatWeakStructureAssessment",
    "HeatWeakStructureInputs",
    "R4ParameterManifest",
    "assess_continuous_entry",
    "assess_heat_weak_structure",
    "challenger_parameter_manifest",
    "challenger_registry",
]

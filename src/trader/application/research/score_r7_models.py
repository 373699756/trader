"""Immutable human-review dossier values for Score-R7."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.application.research.replay_models import canonical_hash

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESEARCH_IDENTITY = re.compile(r"^score_r6_forward_[a-z0-9_]{1,48}$")
_WEIGHT_SCALE = 10_000
_COMPONENT_NAMES = ("tail_structure", "turnover_flow", "trend", "stability", "market_state", "entry_quality")
_EXPECTED_SENSITIVITY = tuple((cost, block) for cost in (20, 50, 100) for block in (3, 5, 10))
_EXPECTED_ABLATIONS = ("hybrid_vs_local", "local_vs_production")
_EXPECTED_GATE_IDS = (
    "hybrid_confidence_lower_pct",
    "hybrid_mean_increment_pct",
    "hybrid_p_value",
    "local_maximum_board_fraction",
    "local_maximum_stock_weight",
    "local_mean_gain_pct",
    "local_recall",
    "local_severe_rate_delta",
    "local_stability_delta",
    "local_turnover_delta",
)


@dataclass(frozen=True)
class ScoreR7ParameterProposal:
    candidate_hash: str
    component_names: tuple[str, ...]
    board_weight_units: tuple[tuple[str, tuple[int, ...]], ...]
    action_threshold: int
    risk_penalty: int
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _hash(self.candidate_hash)
        if self.component_names != _COMPONENT_NAMES:
            raise ValueError("Score-R7 proposal component order is invalid")
        if tuple(item[0] for item in self.board_weight_units) != ("main", "chinext", "star"):
            raise ValueError("Score-R7 proposal must bind all three boards")
        for _board, weights in self.board_weight_units:
            if len(weights) != 6 or min(weights) < 0 or sum(weights) != _WEIGHT_SCALE:
                raise ValueError("Score-R7 proposal weights must be nonnegative and sum exactly to one")
        if self.action_threshold not in (76, 78, 80) or self.risk_penalty not in (3, 4, 5):
            raise ValueError("Score-R7 proposal is outside the frozen Score-R6 grid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR7SensitivityResult:
    cost_bps: int
    block_days: int
    sample_days: int
    local_mean_gain_pct: float
    local_confidence_lower_pct: float
    local_confidence_upper_pct: float
    local_p_value: float
    local_bootstrap_seed: int
    hybrid_mean_increment_pct: float
    hybrid_confidence_lower_pct: float
    hybrid_confidence_upper_pct: float
    hybrid_p_value: float
    hybrid_bootstrap_seed: int

    def __post_init__(self) -> None:
        if (self.cost_bps, self.block_days) not in _EXPECTED_SENSITIVITY or self.sample_days < self.block_days:
            raise ValueError("Score-R7 sensitivity identity is invalid")
        numeric = (
            self.local_mean_gain_pct,
            self.local_confidence_lower_pct,
            self.local_confidence_upper_pct,
            self.local_p_value,
            self.hybrid_mean_increment_pct,
            self.hybrid_confidence_lower_pct,
            self.hybrid_confidence_upper_pct,
            self.hybrid_p_value,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("Score-R7 sensitivity metrics must be finite")
        if (
            not 0.0 <= self.local_p_value <= 1.0
            or not 0.0 <= self.hybrid_p_value <= 1.0
            or min(self.local_bootstrap_seed, self.hybrid_bootstrap_seed) < 0
            or self.local_bootstrap_seed == self.hybrid_bootstrap_seed
        ):
            raise ValueError("Score-R7 sensitivity bootstrap identity is invalid")
        if self.local_confidence_lower_pct > self.local_confidence_upper_pct:
            raise ValueError("Score-R7 local sensitivity confidence interval is inverted")
        if self.hybrid_confidence_lower_pct > self.hybrid_confidence_upper_pct:
            raise ValueError("Score-R7 sensitivity confidence interval is inverted")


@dataclass(frozen=True)
class ScoreR7SampleCounts:
    planned_days: int
    valid_days: int
    failed_days: int
    pair_count: int

    def __post_init__(self) -> None:
        if min(self.planned_days, self.valid_days, self.failed_days, self.pair_count) < 0:
            raise ValueError("Score-R7 sample counts cannot be negative")
        if self.valid_days + self.failed_days != self.planned_days:
            raise ValueError("Score-R7 day counts are inconsistent")


@dataclass(frozen=True)
class ScoreR7GateResult:
    gate_id: str
    actual_value: float
    comparison: Literal["at_least", "at_most", "greater_than"]
    threshold: float
    passed: bool
    required_for_scope: bool

    def __post_init__(self) -> None:
        if self.gate_id not in _EXPECTED_GATE_IDS or self.comparison not in {"at_least", "at_most", "greater_than"}:
            raise ValueError("Score-R7 gate identity is invalid")
        if not math.isfinite(self.actual_value) or not math.isfinite(self.threshold):
            raise ValueError("Score-R7 gate values must be finite")
        expected = {
            "at_least": self.actual_value >= self.threshold,
            "at_most": self.actual_value <= self.threshold,
            "greater_than": self.actual_value > self.threshold,
        }[self.comparison]
        if self.passed != expected:
            raise ValueError("Score-R7 gate result does not match its comparison")


@dataclass(frozen=True)
class ScoreR7PromotionDossier:
    dossier_identity: str
    source_research_identity: str
    historical_report_hash: str
    forward_spec_hash: str
    forward_report_hash: str
    day_manifest_hashes: tuple[str, ...]
    trading_calendar_hash: str
    rule_identity_hash: str
    config_strategy_identity_hash: str
    data_schema_version: str
    strategy_version: str
    fusion_version: str
    engine_version: str
    statistical_program_version: str
    production_scope: Literal["local_only", "hybrid"]
    proposed_parameters: ScoreR7ParameterProposal
    sensitivity: tuple[ScoreR7SensitivityResult, ...]
    gate_results: tuple[ScoreR7GateResult, ...]
    failed_trade_dates: tuple[date, ...]
    sample_counts: ScoreR7SampleCounts
    ablation_ids: tuple[str, ...]
    maximum_stock_weight: float
    maximum_board_fraction: float
    residual_risks: tuple[str, ...]
    manual_review_status: Literal["pending"] = "pending"
    production_change_authorized: Literal[False] = False
    schema_version: str = "score_r7_promotion_dossier_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_dossier_identity(self)
        _validate_dossier_evidence(self)
        _validate_dossier_gates(self)
        _validate_dossier_review_boundary(self)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_dossier_identity(dossier: ScoreR7PromotionDossier) -> None:
    if dossier.dossier_identity != f"{dossier.source_research_identity}_promotion_dossier_v1":
        raise ValueError("Score-R7 dossier identity must bind its source research")
    if _RESEARCH_IDENTITY.fullmatch(dossier.source_research_identity) is None:
        raise ValueError("Score-R7 dossier source identity is invalid")
    if dossier.production_scope not in {"local_only", "hybrid"}:
        raise ValueError("Score-R7 production scope is invalid")
    if dossier.schema_version != "score_r7_promotion_dossier_v1":
        raise ValueError("Score-R7 dossier schema is invalid")


def _validate_dossier_evidence(dossier: ScoreR7PromotionDossier) -> None:
    hashes = (
        dossier.historical_report_hash,
        dossier.forward_spec_hash,
        dossier.forward_report_hash,
        dossier.trading_calendar_hash,
        dossier.rule_identity_hash,
        dossier.config_strategy_identity_hash,
        *dossier.day_manifest_hashes,
    )
    for value in hashes:
        _hash(value)
    if len(dossier.day_manifest_hashes) != 20 or dossier.sample_counts.planned_days != 20:
        raise ValueError("Score-R7 dossier must bind the complete twenty-day manifest")
    if tuple((item.cost_bps, item.block_days) for item in dossier.sensitivity) != _EXPECTED_SENSITIVITY:
        raise ValueError("Score-R7 dossier must contain the fixed cost and block sensitivity grid")
    if dossier.failed_trade_dates or dossier.sample_counts.failed_days:
        raise ValueError("Score-R7 promotion dossier cannot hide failed forward days")


def _validate_dossier_gates(dossier: ScoreR7PromotionDossier) -> None:
    if tuple(item.gate_id for item in dossier.gate_results) != _EXPECTED_GATE_IDS:
        raise ValueError("Score-R7 dossier gate manifest is incomplete")
    local_gates = tuple(item for item in dossier.gate_results if item.gate_id.startswith("local_"))
    hybrid_gates = tuple(item for item in dossier.gate_results if item.gate_id.startswith("hybrid_"))
    if not all(item.required_for_scope and item.passed for item in local_gates):
        raise ValueError("Score-R7 local scope requires every local gate")
    hybrid_required = dossier.production_scope == "hybrid"
    if any(item.required_for_scope != hybrid_required for item in hybrid_gates):
        raise ValueError("Score-R7 hybrid gate scope is inconsistent")
    if hybrid_required != all(item.passed for item in hybrid_gates):
        raise ValueError("Score-R7 hybrid scope must match every hybrid gate")


def _validate_dossier_review_boundary(dossier: ScoreR7PromotionDossier) -> None:
    if dossier.ablation_ids != _EXPECTED_ABLATIONS:
        raise ValueError("Score-R7 dossier ablation manifest is incomplete")
    concentrations = (dossier.maximum_stock_weight, dossier.maximum_board_fraction)
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in concentrations):
        raise ValueError("Score-R7 concentration must be in [0, 1]")
    if not dossier.residual_risks or tuple(sorted(set(dossier.residual_risks))) != dossier.residual_risks:
        raise ValueError("Score-R7 residual risks must be nonempty, unique, and ordered")
    if dossier.manual_review_status != "pending" or dossier.production_change_authorized:
        raise ValueError("Score-R7 dossier cannot authorize a production change")


def _hash(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("Score-R7 evidence identity must be SHA-256")


__all__ = [
    "ScoreR7ParameterProposal",
    "ScoreR7GateResult",
    "ScoreR7PromotionDossier",
    "ScoreR7SampleCounts",
    "ScoreR7SensitivityResult",
]

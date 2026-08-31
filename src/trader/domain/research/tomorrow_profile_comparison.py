"""Immutable, production-isolated V1/V2 paired-research identities."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from trader.domain.research.paired_statistics import PreregisteredBootstrapResult

TomorrowProfileId = Literal["v1", "v2"]
PairEvidenceState = Literal["collecting", "power_ready", "review_ready", "rejected"]
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TomorrowProfileComparisonSpec:
    research_identity: str
    registered_on: date
    historical_evidence_hash: str
    historical_daily_difference_std_pct: float
    historical_long_run_difference_std_pct: float
    minimum_economic_effect_pct: float
    required_independent_days: int
    minimum_paired_candidates: int
    cost_rates_pct: tuple[float, float, float] = (0.20, 0.50, 1.00)
    primary_cost_pct: float = 0.20
    bootstrap_block_days: int = 5
    bootstrap_repetitions: int = 10_000
    bootstrap_master_seed: int = 20260831
    alpha: float = 0.05
    target_power: float = 0.80
    severe_loss_mae_atr20: float = -1.5
    maximum_turnover_increase: float = 0.05
    maximum_stock_positive_fraction: float = 0.10
    maximum_top_five_positive_fraction: float = 0.30
    top_k: int = 6
    multiplicity_rule: str = "single_v2_minus_v1_primary_hypothesis_v1"
    stopping_rule: str = "evaluate_once_at_power_then_terminal_v1"
    power_variance_estimator: str = "newey_west_bartlett_lag4_v1"
    production_authority: bool = False
    automatic_profile_switch: bool = False
    schema_version: str = "tomorrow_v1_v2_comparison_spec_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.research_identity != "tomorrow_v1_v2_paired_forward_v1"
            or self.registered_on != date(2026, 8, 31)
            or len(self.historical_evidence_hash) != 64
            or self.cost_rates_pct != (0.20, 0.50, 1.00)
            or self.primary_cost_pct != 0.20
            or self.bootstrap_block_days != 5
            or self.bootstrap_repetitions != 10_000
            or self.bootstrap_master_seed != 20260831
            or self.alpha != 0.05
            or self.target_power != 0.80
            or self.severe_loss_mae_atr20 != -1.5
            or self.maximum_turnover_increase != 0.05
            or self.maximum_stock_positive_fraction != 0.10
            or self.maximum_top_five_positive_fraction != 0.30
            or self.top_k != 6
            or self.multiplicity_rule != "single_v2_minus_v1_primary_hypothesis_v1"
            or self.stopping_rule != "evaluate_once_at_power_then_terminal_v1"
            or self.power_variance_estimator != "newey_west_bartlett_lag4_v1"
            or self.production_authority
            or self.automatic_profile_switch
            or self.schema_version != "tomorrow_v1_v2_comparison_spec_v1"
        ):
            raise ValueError("Tomorrow profile comparison contract is not the frozen V1 identity")
        if (
            not math.isfinite(self.historical_daily_difference_std_pct)
            or self.historical_daily_difference_std_pct <= 0.0
            or not math.isfinite(self.historical_long_run_difference_std_pct)
            or self.historical_long_run_difference_std_pct <= 0.0
            or not math.isfinite(self.minimum_economic_effect_pct)
            or self.minimum_economic_effect_pct <= 0.0
            or self.required_independent_days < self.bootstrap_block_days
            or self.minimum_paired_candidates < 1
        ):
            raise ValueError("Tomorrow profile comparison power inputs are invalid")
        expected_days = required_independent_days(
            self.historical_long_run_difference_std_pct,
            self.minimum_economic_effect_pct,
            alpha=self.alpha,
            target_power=self.target_power,
        )
        if self.required_independent_days != expected_days:
            raise ValueError("Tomorrow profile comparison days must be derived from frozen power inputs")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowProfilePrediction:
    profile_id: TomorrowProfileId
    model_version: str
    predicted_excess_return_pct: float
    estimated_cost_pct: float
    predicted_net_excess_pct: float
    signal_score: float
    local_score: float
    model_disagreement_pct: float
    action: str
    selected: bool
    rank: int

    def __post_init__(self) -> None:
        if self.profile_id not in {"v1", "v2"} or not self.model_version or not self.action:
            raise ValueError("Tomorrow profile prediction identity is incomplete")
        values = (
            self.predicted_excess_return_pct,
            self.estimated_cost_pct,
            self.predicted_net_excess_pct,
            self.signal_score,
            self.local_score,
            self.model_disagreement_pct,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Tomorrow profile prediction values must be finite")
        if self.estimated_cost_pct < 0.0 or self.model_disagreement_pct < 0.0:
            raise ValueError("Tomorrow profile risk and cost values cannot be negative")
        if not 0.0 <= self.signal_score <= 100.0 or not 0.0 <= self.local_score <= 100.0:
            raise ValueError("Tomorrow profile scores must be in [0, 100]")
        if self.rank < 0 or self.selected != (self.rank > 0):
            raise ValueError("Tomorrow profile selection identity is invalid")


@dataclass(frozen=True)
class TomorrowProfilePair:
    input_version: str
    trade_date: date
    code: str
    board: str
    industry: str
    anchor_price: float
    atr20_pct: float | None
    v1: TomorrowProfilePrediction
    v2: TomorrowProfilePrediction
    schema_version: str = "tomorrow_v1_v2_prediction_pair_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not all((self.input_version, self.code, self.board, self.industry)):
            raise ValueError("Tomorrow profile pair identity is incomplete")
        if self.v1.profile_id != "v1" or self.v2.profile_id != "v2":
            raise ValueError("Tomorrow profile pair must contain V1 and V2")
        if not math.isfinite(self.anchor_price) or self.anchor_price <= 0.0:
            raise ValueError("Tomorrow profile pair anchor price must be positive")
        if self.atr20_pct is not None and (not math.isfinite(self.atr20_pct) or self.atr20_pct <= 0.0):
            raise ValueError("Tomorrow profile pair ATR20 must be positive when present")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowProfilePairManifest:
    spec_hash: str
    input_version: str
    trade_date: date
    observed_at: datetime
    active_profile_id: TomorrowProfileId
    v1_model_version: str
    v2_model_version: str
    common_candidate_count: int
    v1_scorable_count: int
    v2_scorable_count: int
    pairs: tuple[TomorrowProfilePair, ...]
    deepseek_request_delta: int = 0
    production_authority: bool = False
    schema_version: str = "tomorrow_v1_v2_prediction_manifest_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            len(self.spec_hash) != 64
            or not all(
                (
                    self.input_version,
                    self.v1_model_version,
                    self.v2_model_version,
                )
            )
            or self.active_profile_id not in {"v1", "v2"}
            or self.observed_at.tzinfo != _SHANGHAI
            or self.trade_date != self.observed_at.date()
            or min(self.common_candidate_count, self.v1_scorable_count, self.v2_scorable_count) < 0
            or self.deepseek_request_delta != 0
            or self.production_authority
        ):
            raise ValueError("Tomorrow profile comparison manifest identity is invalid")
        pairs = tuple(sorted(self.pairs, key=lambda item: item.code))
        if (
            len({item.code for item in pairs}) != len(pairs)
            or len(pairs) != self.common_candidate_count
            or any(item.input_version != self.input_version or item.trade_date != self.trade_date for item in pairs)
        ):
            raise ValueError("Tomorrow profile comparison manifest pairs are invalid")
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowProfileComparisonStatus:
    initialized: bool
    spec_hash: str
    prediction_manifests: int
    paired_predictions: int
    formal_manifests: int
    settled_pairs: int
    complete_pairs: int
    independent_days: int
    required_independent_days: int
    minimum_paired_candidates: int
    state: PairEvidenceState
    latest_prediction_date: date | None
    latest_settlement_date: date | None
    production_authority: bool = False
    automatic_profile_switch: bool = False
    error_code: str = ""

    def __post_init__(self) -> None:
        counts = (
            self.prediction_manifests,
            self.paired_predictions,
            self.formal_manifests,
            self.settled_pairs,
            self.complete_pairs,
            self.independent_days,
            self.required_independent_days,
            self.minimum_paired_candidates,
        )
        if (
            len(self.spec_hash) != 64
            or any(value < 0 for value in counts)
            or self.complete_pairs > self.settled_pairs
            or self.formal_manifests > self.prediction_manifests
            or self.state not in {"collecting", "power_ready", "review_ready", "rejected"}
            or self.production_authority
            or self.automatic_profile_switch
        ):
            raise ValueError("Tomorrow profile comparison status is invalid")


@dataclass(frozen=True)
class TomorrowProfileLayerMetrics:
    profile_id: TomorrowProfileId
    candidate_pairs: int
    portfolio_days: int
    mean_candidate_net_excess_pct: float | None
    mean_rank_ic: float | None
    top_bottom_quintile_spread_pct: float | None
    mean_portfolio_net_excess_20bp_pct: float | None
    mean_portfolio_net_excess_50bp_pct: float | None
    mean_portfolio_net_excess_100bp_pct: float | None
    severe_loss_rate: float | None
    mean_turnover: float | None
    maximum_stock_positive_fraction: float | None
    top_five_positive_fraction: float | None


@dataclass(frozen=True)
class TomorrowProfileComparisonReport:
    spec_hash: str
    independent_days: int
    paired_candidates: int
    v1: TomorrowProfileLayerMetrics
    v2: TomorrowProfileLayerMetrics
    daily_v2_minus_v1_20bp_pct: tuple[float, ...]
    daily_v2_minus_v1_50bp_pct: tuple[float, ...]
    daily_v2_minus_v1_100bp_pct: tuple[float, ...]
    primary_bootstrap: PreregisteredBootstrapResult
    gate_failures: tuple[str, ...]
    state: PairEvidenceState
    manual_review_eligible: bool
    production_authority: bool = False
    automatic_profile_switch: bool = False
    schema_version: str = "tomorrow_v1_v2_comparison_report_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            len(self.spec_hash) != 64
            or self.independent_days < 0
            or self.paired_candidates < 0
            or len(self.daily_v2_minus_v1_20bp_pct) != self.independent_days
            or len(self.daily_v2_minus_v1_50bp_pct) != self.independent_days
            or len(self.daily_v2_minus_v1_100bp_pct) != self.independent_days
            or self.production_authority
            or self.automatic_profile_switch
            or self.manual_review_eligible != (self.state == "review_ready")
        ):
            raise ValueError("Tomorrow profile comparison report is invalid")
        object.__setattr__(self, "gate_failures", tuple(sorted(set(self.gate_failures))))
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowV2RiskChallengerSpec:
    research_identity: str
    registered_on: date
    parent_comparison_spec_hash: str
    label: str = "mae_atr20_le_negative_1_5_v1"
    model_family: str = "ridge_logistic_platt_v1"
    feature_ids: tuple[str, ...] = (
        "v2_predicted_net_excess_pct",
        "v2_model_disagreement_pct",
        "signal_score",
        "atr20_pct",
        "estimated_cost_pct",
    )
    minimum_training_days: int = 60
    calibration_days: int = 20
    independent_test_days: int = 40
    embargo_days: int = 1
    brier_improvement_required: float = 0.0
    maximum_ece: float = 0.05
    cost_rates_pct: tuple[float, float, float] = (0.20, 0.50, 1.00)
    stopping_rule: str = "single_walk_forward_evaluation_then_terminal_v1"
    production_authority: bool = False
    online_learning: bool = False
    schema_version: str = "tomorrow_v2_risk_challenger_spec_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.research_identity != "tomorrow_v2_risk_challenger_v1"
            or self.registered_on != date(2026, 8, 31)
            or len(self.parent_comparison_spec_hash) != 64
            or self.label != "mae_atr20_le_negative_1_5_v1"
            or self.model_family != "ridge_logistic_platt_v1"
            or self.minimum_training_days != 60
            or self.calibration_days != 20
            or self.independent_test_days != 40
            or self.embargo_days != 1
            or self.brier_improvement_required != 0.0
            or self.maximum_ece != 0.05
            or self.cost_rates_pct != (0.20, 0.50, 1.00)
            or self.stopping_rule != "single_walk_forward_evaluation_then_terminal_v1"
            or self.production_authority
            or self.online_learning
        ):
            raise ValueError("Tomorrow V2 risk challenger contract is not frozen")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def required_independent_days(
    daily_std_pct: float,
    minimum_effect_pct: float,
    *,
    alpha: float = 0.05,
    target_power: float = 0.80,
) -> int:
    """Two-sided normal approximation frozen before forward outcomes are visible."""

    if (
        not math.isfinite(daily_std_pct)
        or daily_std_pct <= 0.0
        or not math.isfinite(minimum_effect_pct)
        or minimum_effect_pct <= 0.0
        or alpha != 0.05
        or target_power != 0.80
    ):
        raise ValueError("unsupported Tomorrow comparison power inputs")
    z_alpha = 1.959963984540054
    z_power = 0.8416212335729143
    return max(5, math.ceil(((z_alpha + z_power) * daily_std_pct / minimum_effect_pct) ** 2))


def newey_west_long_run_std(values: tuple[float, ...], *, lag_days: int) -> float:
    """Bartlett-kernel long-run standard deviation with a frozen finite lag."""

    if len(values) <= lag_days or lag_days < 1 or any(not math.isfinite(value) for value in values):
        raise ValueError("Newey-West inputs are invalid")
    mean = math.fsum(values) / len(values)
    centered = tuple(value - mean for value in values)
    gamma_zero = math.fsum(value * value for value in centered) / len(centered)
    long_run_variance = gamma_zero
    for lag in range(1, lag_days + 1):
        covariance = math.fsum(centered[index] * centered[index - lag] for index in range(lag, len(centered)))
        covariance /= len(centered)
        long_run_variance += 2.0 * (1.0 - lag / (lag_days + 1.0)) * covariance
    if not math.isfinite(long_run_variance) or long_run_variance <= 0.0:
        raise ValueError("Newey-West long-run variance is not positive")
    return math.sqrt(long_run_variance)


def canonical_hash(value: object) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical(getattr(value, item.name)) for item in dataclasses.fields(value) if item.init}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


TOMORROW_PROFILE_COMPARISON_SPEC = TomorrowProfileComparisonSpec(
    research_identity="tomorrow_v1_v2_paired_forward_v1",
    registered_on=date(2026, 8, 31),
    historical_evidence_hash="47e2b9bfd4d404521f8251e2e51c491aa96c1bc0d8423dea95e63320daa6e3bf",
    historical_daily_difference_std_pct=3.831660055646444,
    historical_long_run_difference_std_pct=4.074760363819412,
    minimum_economic_effect_pct=0.50,
    required_independent_days=522,
    minimum_paired_candidates=300,
)

TOMORROW_V2_RISK_CHALLENGER_SPEC = TomorrowV2RiskChallengerSpec(
    research_identity="tomorrow_v2_risk_challenger_v1",
    registered_on=date(2026, 8, 31),
    parent_comparison_spec_hash=TOMORROW_PROFILE_COMPARISON_SPEC.content_hash,
)


__all__ = [
    "PairEvidenceState",
    "TomorrowProfileComparisonSpec",
    "TomorrowProfileComparisonStatus",
    "TomorrowProfileComparisonReport",
    "TomorrowProfileLayerMetrics",
    "TomorrowProfileId",
    "TomorrowProfilePair",
    "TomorrowProfilePairManifest",
    "TomorrowProfilePrediction",
    "TOMORROW_PROFILE_COMPARISON_SPEC",
    "TOMORROW_V2_RISK_CHALLENGER_SPEC",
    "TomorrowV2RiskChallengerSpec",
    "canonical_hash",
    "newey_west_long_run_std",
    "required_independent_days",
]

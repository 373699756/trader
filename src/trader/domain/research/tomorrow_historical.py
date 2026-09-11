"""Frozen, production-isolated contract for accelerated Tomorrow historical screening."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.domain.research.historical_screening import HISTORICAL_SCREENING_SPEC

HistoricalFieldStatus = Literal["eligible", "not_reconstructed"]
HistoricalFieldRole = Literal["alpha", "residualization", "risk", "cost", "capacity", "excluded"]

TOMORROW_HISTORICAL_CANDIDATE_ID = "daily_reconstructible_ensemble"
_COST_RATES = (0.002, 0.005, 0.01)
_EXCLUDED_EVIDENCE_IDENTITIES = (
    "historical_research_baseline",
    "preregistered_research",
    "tomorrow_shadow_baseline",
)


@dataclass(frozen=True)
class HistoricalFieldEligibility:
    field_id: str
    status: HistoricalFieldStatus
    role: HistoricalFieldRole
    source_semantics: str

    def __post_init__(self) -> None:
        if not self.field_id or not self.source_semantics:
            raise ValueError("Historical field identity is incomplete")
        if self.status not in {"eligible", "not_reconstructed"}:
            raise ValueError("Historical field status is invalid")
        if self.role not in {"alpha", "residualization", "risk", "cost", "capacity", "excluded"}:
            raise ValueError("Historical field role is invalid")
        if self.status == "not_reconstructed" and self.role != "excluded":
            raise ValueError("historical screening non-reconstructed fields must remain excluded")
        if self.status == "eligible" and self.role == "excluded":
            raise ValueError("historical screening eligible fields require an executable role")


_FIELD_ELIGIBILITY = (
    *(
        HistoricalFieldEligibility(name, "eligible", "alpha", "lagged_qfq_close")
        for name in TOMORROW_MODEL_FEATURE_MANIFEST.names[:3]
    ),
    *(
        HistoricalFieldEligibility(name, "eligible", "alpha", "lagged_qfq_cross_section")
        for name in TOMORROW_MODEL_FEATURE_MANIFEST.names[3:]
    ),
    HistoricalFieldEligibility(
        "market_cross_section", "eligible", "residualization", "same_manifest_market_cross_section"
    ),
    HistoricalFieldEligibility(
        "board_cross_section", "eligible", "residualization", "same_manifest_code_board_cross_section"
    ),
    HistoricalFieldEligibility("realized_volatility_20d", "eligible", "risk", "lagged_qfq_ohlc"),
    HistoricalFieldEligibility("downside_semivariance_20d", "eligible", "risk", "lagged_qfq_ohlc"),
    HistoricalFieldEligibility("drawdown_recovery_60d", "eligible", "risk", "lagged_qfq_ohlc"),
    HistoricalFieldEligibility("amihud_20d", "eligible", "cost", "lagged_qfq_return_amount"),
    HistoricalFieldEligibility("average_amount_20d", "eligible", "capacity", "lagged_amount"),
    HistoricalFieldEligibility(
        "historical_st_status", "not_reconstructed", "excluded", "historical_effective_time_unavailable"
    ),
    HistoricalFieldEligibility(
        "historical_industry", "not_reconstructed", "excluded", "historical_effective_time_unavailable"
    ),
    HistoricalFieldEligibility(
        "historical_market_cap", "not_reconstructed", "excluded", "historical_effective_time_unavailable"
    ),
    HistoricalFieldEligibility(
        "intraday_1450_tail", "not_reconstructed", "excluded", "intraday_observation_unavailable"
    ),
    HistoricalFieldEligibility(
        "financial_disclosure_point_in_time",
        "not_reconstructed",
        "excluded",
        "published_and_received_time_unavailable",
    ),
    HistoricalFieldEligibility(
        "announcement_disclosure_point_in_time",
        "not_reconstructed",
        "excluded",
        "published_and_received_time_unavailable",
    ),
    HistoricalFieldEligibility(
        "corporate_risk_point_in_time",
        "not_reconstructed",
        "excluded",
        "historical_effective_time_unavailable",
    ),
    HistoricalFieldEligibility(
        "deepseek_facts_point_in_time", "not_reconstructed", "excluded", "received_time_unavailable"
    ),
)

_CANDIDATE_FEATURE_IDS = tuple(
    item.field_id
    for item in _FIELD_ELIGIBILITY
    if item.status == "eligible" and item.role in {"alpha", "risk", "cost", "capacity"}
)


@dataclass(frozen=True)
class TomorrowHistoricalModelArtifact:
    candidate_id: str
    feature_ids: tuple[str, ...]
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    linear_intercept: float
    linear_coefficients: tuple[float, ...]
    lightgbm_model: str
    lightgbm_best_iteration: int
    training_rows: int
    internal_validation_rows: int
    schema_version: str = "tomorrow_historical_model"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        width = len(self.feature_ids)
        if (
            self.candidate_id != TOMORROW_HISTORICAL_CANDIDATE_ID
            or width < 1
            or len(set(self.feature_ids)) != width
            or len(self.transformer_means) != width
            or len(self.transformer_scales) != width
            or len(self.linear_coefficients) != width
            or not self.lightgbm_model
            or self.lightgbm_best_iteration < 1
            or self.training_rows < 1
            or not 1 <= self.internal_validation_rows < self.training_rows
            or self.schema_version != "tomorrow_historical_model"
        ):
            raise ValueError("Tomorrow historical model artifact identity is invalid")
        numeric = (
            *self.transformer_means,
            *self.transformer_scales,
            self.linear_intercept,
            *self.linear_coefficients,
        )
        if any(not math.isfinite(value) for value in numeric) or any(value <= 0.0 for value in self.transformer_scales):
            raise ValueError("Tomorrow historical model artifact parameters are invalid")
        object.__setattr__(self, "content_hash", _canonical_hash(self))


@dataclass(frozen=True)
class TomorrowHistoricalCandidate:
    candidate_id: str
    feature_ids: tuple[str, ...]
    model_families: tuple[str, ...]
    model_weights: tuple[float, ...]
    model_random_seed: int
    linear_ridge: float
    lightgbm_max_depth: int
    lightgbm_num_leaves: int
    lightgbm_min_data_in_leaf: int
    lightgbm_learning_rate: float
    lightgbm_num_boost_round: int
    lightgbm_early_stopping_rounds: int
    lightgbm_num_threads: int
    identity: str

    def __post_init__(self) -> None:
        if (
            self.candidate_id != TOMORROW_HISTORICAL_CANDIDATE_ID
            or self.feature_ids != _CANDIDATE_FEATURE_IDS
            or self.model_families != ("linear", "lightgbm")
            or self.model_weights != (0.5, 0.5)
            or not math.isclose(math.fsum(self.model_weights), 1.0, abs_tol=1e-12)
            or self.model_random_seed != 20260830
            or self.linear_ridge != 1e-3
            or self.lightgbm_max_depth != 3
            or self.lightgbm_num_leaves != 7
            or self.lightgbm_min_data_in_leaf != 20
            or self.lightgbm_learning_rate != 0.05
            or self.lightgbm_num_boost_round != 200
            or self.lightgbm_early_stopping_rounds != 20
            or self.lightgbm_num_threads != 1
            or self.identity != TOMORROW_HISTORICAL_CANDIDATE_ID
        ):
            raise ValueError("Tomorrow historical candidate family is frozen")


_CANDIDATE = TomorrowHistoricalCandidate(
    candidate_id=TOMORROW_HISTORICAL_CANDIDATE_ID,
    feature_ids=_CANDIDATE_FEATURE_IDS,
    model_families=("linear", "lightgbm"),
    model_weights=(0.5, 0.5),
    model_random_seed=20260830,
    linear_ridge=1e-3,
    lightgbm_max_depth=3,
    lightgbm_num_leaves=7,
    lightgbm_min_data_in_leaf=20,
    lightgbm_learning_rate=0.05,
    lightgbm_num_boost_round=200,
    lightgbm_early_stopping_rounds=20,
    lightgbm_num_threads=1,
    identity=TOMORROW_HISTORICAL_CANDIDATE_ID,
)


@dataclass(frozen=True)
class TomorrowHistoricalSpec:
    research_identity: str
    registered_on: date
    source_research_identity: str
    source_spec_hash: str
    source_cutoff: date
    training_window: tuple[date, date]
    validation_window: tuple[date, date]
    field_eligibility: tuple[HistoricalFieldEligibility, ...]
    candidate: TomorrowHistoricalCandidate
    comparator_id: str = "historical_ohlcv_cross_section"
    selection_rule: str = "single_candidate_pass_or_stop"
    portfolio_sort_order: tuple[str, ...] = (
        "net_utility_desc",
        "severe_loss_probability_asc",
        "model_disagreement_asc",
        "code_asc",
    )
    allow_empty_portfolio: bool = True
    label_version: str = "tomorrow_close_to_next_close_market_excess"
    cost_rates: tuple[float, ...] = _COST_RATES
    minimum_archive_coverage: float = 0.95
    minimum_validation_pairs: int = 300
    bootstrap_block_days: int = 5
    bootstrap_repetitions: int = 10_000
    bootstrap_master_seed: int = 20260830
    bootstrap_alpha: float = 0.05
    severe_loss_mae_atr20: float = -1.5
    maximum_turnover_increase: float = 0.05
    maximum_stock_positive_fraction: float = 0.10
    maximum_top_five_positive_fraction: float = 0.30
    top_k: int = 6
    maximum_board_fraction: float = 0.60
    historical_industry_constraint: str = "not_reconstructed_forward_required"
    excluded_evidence_identities: tuple[str, ...] = _EXCLUDED_EVIDENCE_IDENTITIES
    forward_research_identity: None = None
    forward_trade_dates: tuple[date, ...] = ()
    report_schema_version: str = "score_tomorrow_historical_report"
    production_authority: bool = False
    schema_version: str = "score_tomorrow_historical_spec"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if (
            self.research_identity != "score_tomorrow_historical"
            or self.registered_on != date(2026, 8, 30)
            or self.source_research_identity != HISTORICAL_SCREENING_SPEC.research_identity
            or self.source_spec_hash != HISTORICAL_SCREENING_SPEC.content_hash
            or self.source_cutoff != HISTORICAL_SCREENING_SPEC.source_cutoff
            or self.training_window
            != (HISTORICAL_SCREENING_SPEC.training_start, HISTORICAL_SCREENING_SPEC.training_end)
            or self.validation_window
            != (HISTORICAL_SCREENING_SPEC.validation_start, HISTORICAL_SCREENING_SPEC.validation_end)
        ):
            raise ValueError("Tomorrow historical source identity is frozen")
        if self.field_eligibility != _FIELD_ELIGIBILITY:
            raise ValueError("Tomorrow historical field eligibility matrix is frozen")
        if self.candidate != _CANDIDATE:
            raise ValueError("Tomorrow historical candidate family is frozen")
        if self.forward_research_identity is not None or self.forward_trade_dates:
            raise ValueError("Tomorrow historical spec cannot bind a forward identity or calendar")
        if (
            self.comparator_id != "historical_ohlcv_cross_section"
            or self.selection_rule != "single_candidate_pass_or_stop"
            or self.portfolio_sort_order
            != (
                "net_utility_desc",
                "severe_loss_probability_asc",
                "model_disagreement_asc",
                "code_asc",
            )
            or not self.allow_empty_portfolio
            or self.label_version != "tomorrow_close_to_next_close_market_excess"
            or self.cost_rates != _COST_RATES
            or self.minimum_archive_coverage != 0.95
            or self.minimum_validation_pairs != 300
            or self.bootstrap_block_days != 5
            or self.bootstrap_repetitions != 10_000
            or self.bootstrap_master_seed != 20260830
            or self.bootstrap_alpha != 0.05
            or self.severe_loss_mae_atr20 != -1.5
            or self.maximum_turnover_increase != 0.05
            or self.maximum_stock_positive_fraction != 0.10
            or self.maximum_top_five_positive_fraction != 0.30
            or self.top_k != 6
            or self.maximum_board_fraction != 0.60
            or self.historical_industry_constraint != "not_reconstructed_forward_required"
            or self.excluded_evidence_identities != _EXCLUDED_EVIDENCE_IDENTITIES
            or self.report_schema_version != "score_tomorrow_historical_report"
        ):
            raise ValueError("Tomorrow historical gates are frozen")
        if self.production_authority or self.schema_version != "score_tomorrow_historical_spec":
            raise ValueError("Tomorrow historical spec cannot authorize production")
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in dataclasses.fields(value) if field.init}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


TOMORROW_HISTORICAL_SPEC = TomorrowHistoricalSpec(
    research_identity="score_tomorrow_historical",
    registered_on=date(2026, 8, 30),
    source_research_identity=HISTORICAL_SCREENING_SPEC.research_identity,
    source_spec_hash=HISTORICAL_SCREENING_SPEC.content_hash,
    source_cutoff=HISTORICAL_SCREENING_SPEC.source_cutoff,
    training_window=(HISTORICAL_SCREENING_SPEC.training_start, HISTORICAL_SCREENING_SPEC.training_end),
    validation_window=(HISTORICAL_SCREENING_SPEC.validation_start, HISTORICAL_SCREENING_SPEC.validation_end),
    field_eligibility=_FIELD_ELIGIBILITY,
    candidate=_CANDIDATE,
)


__all__ = [
    "TOMORROW_HISTORICAL_CANDIDATE_ID",
    "TOMORROW_HISTORICAL_SPEC",
    "HistoricalFieldEligibility",
    "HistoricalFieldRole",
    "HistoricalFieldStatus",
    "TomorrowHistoricalCandidate",
    "TomorrowHistoricalModelArtifact",
    "TomorrowHistoricalSpec",
]

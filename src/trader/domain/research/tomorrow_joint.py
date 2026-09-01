"""Pure contracts for the production-isolated Tomorrow joint-model study."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Literal

import numpy as np

from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    PreregisteredBootstrapResult,
    PreregisteredHolmDecision,
    fixed_family_holm,
    paired_moving_block_statistics,
)

TomorrowJointCandidateId = Literal["c3", "v1_c3", "v1_v2_c3"]
TomorrowJointPredictionSemantics = Literal["pre_base_score_cost_adjusted_net_excess"]

TOMORROW_JOINT_CANDIDATES: tuple[TomorrowJointCandidateId, ...] = ("c3", "v1_c3", "v1_v2_c3")
TOMORROW_JOINT_LAMBDAS = (0.1, 1.0, 10.0, 100.0)
_CANDIDATE_INDICES: dict[TomorrowJointCandidateId, tuple[int, ...]] = {
    "c3": (2,),
    "v1_c3": (0, 2),
    "v1_v2_c3": (0, 1, 2),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JOINT_PROFILES: tuple[str, ...] = ("v1", "v2", "c3")


@dataclass(frozen=True)
class TomorrowJointInsufficientTerminal:
    """Fail-closed joint-study terminal when raw profile rows are unavailable."""

    parent_completion_hash: str
    parent_profile_hashes: tuple[tuple[str, str], ...]
    status: Literal["historical_data_insufficient"]
    failure_reasons: tuple[str, ...]
    candidate_family_hash: str | None = None
    prediction_rows: int | None = None
    holm_test_count: int | None = None
    model_artifact_hash: str | None = None
    terminal_holdout_status: Literal["terminal_holdout_not_opened"] = "terminal_holdout_not_opened"
    production_authority: bool = False
    schema_version: str = "tomorrow_joint_insufficient_terminal_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.parent_completion_hash) is None:
            raise ValueError("Tomorrow joint parent completion hash is invalid")
        profiles = tuple(sorted(self.parent_profile_hashes, key=lambda item: _JOINT_PROFILES.index(item[0])))
        if tuple(item[0] for item in profiles) != _JOINT_PROFILES or any(
            _SHA256.fullmatch(item[1]) is None for item in profiles
        ):
            raise ValueError("Tomorrow joint parent profile hashes are invalid")
        if self.status != "historical_data_insufficient" or not self.failure_reasons:
            raise ValueError("Tomorrow joint terminal requires bounded insufficient reasons")
        if any(value is not None for value in (self.candidate_family_hash, self.model_artifact_hash)):
            raise ValueError("Tomorrow joint insufficient terminal cannot claim candidates or a model")
        if self.prediction_rows is not None or self.holm_test_count is not None:
            raise ValueError("Tomorrow joint insufficient terminal cannot claim predictions or Holm tests")
        if self.terminal_holdout_status != "terminal_holdout_not_opened" or self.production_authority:
            raise ValueError("Tomorrow joint terminal cannot open holdout or authorize production")
        if self.schema_version != "tomorrow_joint_insufficient_terminal_v1":
            raise ValueError("Tomorrow joint terminal schema is invalid")
        object.__setattr__(self, "parent_profile_hashes", profiles)
        object.__setattr__(self, "failure_reasons", tuple(sorted(set(self.failure_reasons))))
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def seal_tomorrow_joint_insufficient_terminal(
    *,
    parent_completion_hash: str,
    parent_profile_hashes: tuple[tuple[str, str], ...],
    failure_reasons: tuple[str, ...],
) -> TomorrowJointInsufficientTerminal:
    """Seal joint ownership without reading dates, rows, predictions, or outcomes."""

    return TomorrowJointInsufficientTerminal(
        parent_completion_hash=parent_completion_hash,
        parent_profile_hashes=parent_profile_hashes,
        status="historical_data_insufficient",
        failure_reasons=failure_reasons,
    )


@dataclass(frozen=True, order=True)
class TomorrowJointRowKey:
    trade_date: date
    code: str

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("Tomorrow joint row code must contain six digits")


@dataclass(frozen=True)
class TomorrowJointAlignedRow:
    trade_date: date
    code: str
    candidate_order: int
    label_matured_at: date
    actual_net_excess_20bp: float
    actual_net_excess_50bp: float
    severe_loss: bool
    v1_predicted_net_excess_20bp: float
    v2_predicted_net_excess_20bp: float
    c3_predicted_net_excess_20bp: float

    def __post_init__(self) -> None:
        TomorrowJointRowKey(self.trade_date, self.code)
        values = (
            self.actual_net_excess_20bp,
            self.actual_net_excess_50bp,
            self.v1_predicted_net_excess_20bp,
            self.v2_predicted_net_excess_20bp,
            self.c3_predicted_net_excess_20bp,
        )
        if self.candidate_order < 0 or self.label_matured_at <= self.trade_date:
            raise ValueError("Tomorrow joint row requires an ordered, mature label")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Tomorrow joint row values must be finite")

    @property
    def key(self) -> TomorrowJointRowKey:
        return TomorrowJointRowKey(self.trade_date, self.code)

    @property
    def predictors(self) -> tuple[float, float, float]:
        return (
            self.v1_predicted_net_excess_20bp,
            self.v2_predicted_net_excess_20bp,
            self.c3_predicted_net_excess_20bp,
        )


@dataclass(frozen=True)
class TomorrowJointWeights:
    v1: float
    v2: float
    c3: float

    def __post_init__(self) -> None:
        values = (self.v1, self.v2, self.c3)
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("Tomorrow joint weights must be finite and non-negative")
        if not math.isclose(math.fsum(values), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("Tomorrow joint weights must sum to one")

    @property
    def values(self) -> tuple[float, float, float]:
        return self.v1, self.v2, self.c3


@dataclass(frozen=True)
class TomorrowJointCandidateFit:
    candidate_id: TomorrowJointCandidateId
    regularization_lambda: float
    weights: TomorrowJointWeights
    training_rows: int
    training_mean_squared_error: float
    production_authority: bool = False

    def __post_init__(self) -> None:
        if self.candidate_id not in {"v1_c3", "v1_v2_c3"}:
            raise ValueError("Tomorrow joint candidate fit requires a fused candidate")
        if self.regularization_lambda not in TOMORROW_JOINT_LAMBDAS:
            raise ValueError("Tomorrow joint lambda is not preregistered")
        allowed = frozenset(_CANDIDATE_INDICES[self.candidate_id])
        if any(value != 0.0 for index, value in enumerate(self.weights.values) if index not in allowed):
            raise ValueError("Tomorrow joint candidate fit uses an excluded predictor")
        if self.training_rows < 1:
            raise ValueError("Tomorrow joint candidate fit requires training rows")
        if not math.isfinite(self.training_mean_squared_error) or self.training_mean_squared_error < 0.0:
            raise ValueError("Tomorrow joint training loss must be finite and non-negative")
        if self.production_authority:
            raise ValueError("Tomorrow joint candidate fit cannot authorize production")


@dataclass(frozen=True)
class TomorrowJointFittedModel:
    candidate_id: TomorrowJointCandidateId
    regularization_lambda: float | None
    weights: TomorrowJointWeights
    training_rows: int
    tuning_rows: int
    tuning_mean_squared_error: float
    production_authority: bool = False

    def __post_init__(self) -> None:
        if self.candidate_id not in TOMORROW_JOINT_CANDIDATES:
            raise ValueError("Tomorrow joint candidate is not preregistered")
        if self.candidate_id == "c3":
            if self.regularization_lambda is not None or self.weights.values != (0.0, 0.0, 1.0):
                raise ValueError("C3 candidate must remain the unfused challenger")
        elif self.regularization_lambda not in TOMORROW_JOINT_LAMBDAS:
            raise ValueError("Tomorrow joint lambda is not preregistered")
        allowed = frozenset(_CANDIDATE_INDICES[self.candidate_id])
        if any(value != 0.0 for index, value in enumerate(self.weights.values) if index not in allowed):
            raise ValueError("Tomorrow joint model uses a predictor outside its candidate")
        if self.training_rows < 1 or self.tuning_rows < 1:
            raise ValueError("Tomorrow joint model requires development evidence")
        if not math.isfinite(self.tuning_mean_squared_error) or self.tuning_mean_squared_error < 0.0:
            raise ValueError("Tomorrow joint tuning loss must be finite and non-negative")
        if self.production_authority:
            raise ValueError("Tomorrow joint research cannot authorize production")


@dataclass(frozen=True)
class TomorrowJointCandidateFamily:
    candidates: tuple[TomorrowJointFittedModel, ...]
    selection_status: Literal["portfolio_evidence_required"] = "portfolio_evidence_required"
    production_authority: bool = False

    def __post_init__(self) -> None:
        if tuple(item.candidate_id for item in self.candidates) != TOMORROW_JOINT_CANDIDATES:
            raise ValueError("Tomorrow joint family must retain all fixed candidates")
        if self.selection_status != "portfolio_evidence_required" or self.production_authority:
            raise ValueError("Tomorrow joint family cannot preselect or authorize a candidate")


@dataclass(frozen=True)
class TomorrowJointPrediction:
    candidate_id: TomorrowJointCandidateId
    trade_date: date
    code: str
    candidate_order: int
    label_matured_at: date
    actual_net_excess_20bp: float
    actual_net_excess_50bp: float
    severe_loss: bool
    v1_predicted_net_excess_20bp: float
    v2_predicted_net_excess_20bp: float
    c3_predicted_net_excess_20bp: float
    predicted_net_excess_20bp: float
    weights: TomorrowJointWeights
    prediction_semantics: TomorrowJointPredictionSemantics = "pre_base_score_cost_adjusted_net_excess"

    def __post_init__(self) -> None:
        TomorrowJointRowKey(self.trade_date, self.code)
        if self.candidate_id not in TOMORROW_JOINT_CANDIDATES:
            raise ValueError("Tomorrow joint prediction candidate is not preregistered")
        values = (
            self.actual_net_excess_20bp,
            self.actual_net_excess_50bp,
            self.v1_predicted_net_excess_20bp,
            self.v2_predicted_net_excess_20bp,
            self.c3_predicted_net_excess_20bp,
            self.predicted_net_excess_20bp,
        )
        if self.prediction_semantics != "pre_base_score_cost_adjusted_net_excess":
            raise ValueError("Tomorrow joint output must remain before base-score mapping")
        if self.candidate_order < 0 or self.label_matured_at <= self.trade_date:
            raise ValueError("Tomorrow joint prediction requires an ordered, mature label")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Tomorrow joint prediction values must be finite")


@dataclass(frozen=True)
class TomorrowJointDailyPortfolioEvidence:
    """Research-only daily portfolio metrics produced by an external sealed replay."""

    trade_date: date
    v1_net_excess_20bp: float
    joint_net_excess_20bp: float
    v1_net_excess_50bp: float
    joint_net_excess_50bp: float
    v1_severe_loss_rate: float
    joint_severe_loss_rate: float
    v1_turnover: float
    joint_turnover: float
    active_profile_id: Literal["v1", "v2"] = "v1"
    active_net_excess_20bp: float | None = None
    active_net_excess_50bp: float | None = None
    v1_capacity: float = 1.0
    joint_capacity: float = 1.0
    v1_concentration: float = 0.0
    joint_concentration: float = 0.0

    def __post_init__(self) -> None:
        returns = (
            self.v1_net_excess_20bp,
            self.joint_net_excess_20bp,
            self.v1_net_excess_50bp,
            self.joint_net_excess_50bp,
        )
        rates = (
            self.v1_severe_loss_rate,
            self.joint_severe_loss_rate,
            self.v1_turnover,
            self.joint_turnover,
            self.v1_concentration,
            self.joint_concentration,
        )
        if any(not math.isfinite(value) for value in returns):
            raise ValueError("Tomorrow joint portfolio returns must be finite")
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in rates):
            raise ValueError("Tomorrow joint portfolio rates must be in [0, 1]")
        if any(not math.isfinite(value) or value < 0.0 for value in (self.v1_capacity, self.joint_capacity)):
            raise ValueError("Tomorrow joint capacity must be finite and non-negative")
        if self.active_profile_id == "v2":
            if self.active_net_excess_20bp is None or self.active_net_excess_50bp is None:
                raise ValueError("Tomorrow joint V2-active evidence requires paired active returns")
        elif self.active_profile_id != "v1":
            raise ValueError("Tomorrow joint active profile must be V1 or V2")
        active_values = (self.active_net_excess_20bp, self.active_net_excess_50bp)
        if any(value is not None and not math.isfinite(value) for value in active_values):
            raise ValueError("Tomorrow joint active profile returns must be finite")

    @property
    def active_return_20bp(self) -> float:
        if self.active_profile_id == "v1":
            return self.v1_net_excess_20bp
        if self.active_net_excess_20bp is None:
            raise AssertionError("validated V2 active evidence must contain 20bp return")
        return self.active_net_excess_20bp

    @property
    def active_return_50bp(self) -> float:
        if self.active_profile_id == "v1":
            return self.v1_net_excess_50bp
        if self.active_net_excess_50bp is None:
            raise AssertionError("validated V2 active evidence must contain 50bp return")
        return self.active_net_excess_50bp


@dataclass(frozen=True)
class TomorrowJointEvidenceIdentity:
    key: TomorrowJointRowKey
    candidate_order: int
    label_matured_at: date
    actual_net_excess_20bp: float
    actual_net_excess_50bp: float
    severe_loss: bool


@dataclass(frozen=True)
class TomorrowJointValidationReport:
    candidate_id: TomorrowJointCandidateId
    weights: TomorrowJointWeights
    evidence_identity: tuple[TomorrowJointEvidenceIdentity, ...]
    trade_dates: int
    paired_increment_20bp: float
    paired_increment_50bp: float
    paired_daily_increments_20bp: tuple[float, ...]
    paired_daily_increments_50bp: tuple[float, ...]
    bootstrap_20bp: PreregisteredBootstrapResult
    bootstrap_50bp: PreregisteredBootstrapResult
    joint_mean_net_excess_20bp: float
    joint_mean_net_excess_50bp: float
    severe_loss_rate_delta: float
    turnover_increment_percentage_points: float
    mean_rank_ic: float | None
    mean_q5_minus_q1_20bp: float | None
    failure_reasons: tuple[str, ...]
    passed: bool
    production_authority: bool = False
    active_profile_id: Literal["v1", "v2"] = "v1"
    paired_active_increment_20bp: float = 0.0
    paired_active_increment_50bp: float = 0.0
    active_bootstrap_20bp: PreregisteredBootstrapResult | None = None
    active_bootstrap_50bp: PreregisteredBootstrapResult | None = None
    capacity_delta: float = 0.0
    concentration_delta: float = 0.0

    def __post_init__(self) -> None:
        _validate_joint_report_identity(self)
        _validate_joint_report_metrics(self)
        _validate_joint_report_outcome(self)


def _validate_joint_report_identity(report: TomorrowJointValidationReport) -> None:
    if report.candidate_id not in TOMORROW_JOINT_CANDIDATES:
        raise ValueError("Tomorrow joint validation candidate is not preregistered")
    if report.trade_dates < 1 or report.production_authority:
        raise ValueError("Tomorrow joint validation is historical research only")
    if not report.evidence_identity or len({item.key for item in report.evidence_identity}) != len(
        report.evidence_identity
    ):
        raise ValueError("Tomorrow joint validation requires unique aligned evidence identity")
    if (
        len(report.paired_daily_increments_20bp) != report.trade_dates
        or len(report.paired_daily_increments_50bp) != report.trade_dates
        or report.bootstrap_20bp.sample_count != report.trade_dates
        or report.bootstrap_50bp.sample_count != report.trade_dates
    ):
        raise ValueError("Tomorrow joint validation evidence counts must align")


def _validate_joint_report_metrics(report: TomorrowJointValidationReport) -> None:
    metrics = (
        report.paired_increment_20bp,
        report.paired_increment_50bp,
        *report.paired_daily_increments_20bp,
        *report.paired_daily_increments_50bp,
        report.joint_mean_net_excess_20bp,
        report.joint_mean_net_excess_50bp,
        report.severe_loss_rate_delta,
        report.turnover_increment_percentage_points,
        report.paired_active_increment_20bp,
        report.paired_active_increment_50bp,
        report.capacity_delta,
        report.concentration_delta,
    )
    optional_metrics = (report.mean_rank_ic, report.mean_q5_minus_q1_20bp)
    if any(not math.isfinite(value) for value in metrics) or any(
        value is not None and not math.isfinite(value) for value in optional_metrics
    ):
        raise ValueError("Tomorrow joint validation metrics must be finite")
    if report.failure_reasons != tuple(sorted(set(report.failure_reasons))):
        raise ValueError("Tomorrow joint validation failures must be canonical")


def _validate_joint_report_outcome(report: TomorrowJointValidationReport) -> None:
    if (
        not math.isclose(report.paired_increment_20bp, _mean(report.paired_daily_increments_20bp), abs_tol=1e-12)
        or not math.isclose(report.paired_increment_50bp, _mean(report.paired_daily_increments_50bp), abs_tol=1e-12)
        or not math.isclose(report.bootstrap_20bp.observed_mean or 0.0, report.paired_increment_20bp, abs_tol=1e-12)
        or not math.isclose(report.bootstrap_50bp.observed_mean or 0.0, report.paired_increment_50bp, abs_tol=1e-12)
    ):
        raise ValueError("Tomorrow joint paired metrics must bind their daily evidence")
    if report.passed == bool(report.failure_reasons):
        raise ValueError("Tomorrow joint validation status and failures disagree")
    if report.active_profile_id not in {"v1", "v2"}:
        raise ValueError("Tomorrow joint validation active profile is invalid")
    if report.active_profile_id == "v2" and (
        report.active_bootstrap_20bp is None or report.active_bootstrap_50bp is None
    ):
        raise ValueError("Tomorrow joint V2-active validation requires paired bootstrap evidence")


@dataclass(frozen=True)
class TomorrowJointCandidateSelection:
    candidate_family: TomorrowJointCandidateFamily
    reports: tuple[TomorrowJointValidationReport, ...]
    selected_model: TomorrowJointFittedModel | None
    status: Literal["selected_by_portfolio_evidence", "no_candidate_passed"]
    production_authority: bool = False

    def __post_init__(self) -> None:
        if tuple(item.candidate_id for item in self.reports) != TOMORROW_JOINT_CANDIDATES:
            raise ValueError("Tomorrow joint selection requires all candidate reports")
        models = {item.candidate_id: item for item in self.candidate_family.candidates}
        if any(models[report.candidate_id].weights != report.weights for report in self.reports):
            raise ValueError("Tomorrow joint reports must bind the frozen candidate weights")
        if len({report.evidence_identity for report in self.reports}) != 1:
            raise ValueError("Tomorrow joint reports must use identical dates, codes, order, costs, and labels")
        expected_status = "selected_by_portfolio_evidence" if self.selected_model is not None else "no_candidate_passed"
        if self.status != expected_status or self.production_authority:
            raise ValueError("Tomorrow joint selection status is inconsistent")
        if self.selected_model is not None and not next(
            report.passed for report in self.reports if report.candidate_id == self.selected_model.candidate_id
        ):
            raise ValueError("Tomorrow joint selected model must pass every validation gate")


@dataclass(frozen=True)
class TomorrowJointFamilyConfirmation:
    candidate_family: TomorrowJointCandidateFamily
    reports: tuple[TomorrowJointValidationReport, ...]
    holm: tuple[PreregisteredHolmDecision, ...]
    selected_model: TomorrowJointFittedModel | None
    status: Literal["historical_candidate_ready", "historical_rejected"]
    fallback_to_c3: bool
    terminal_holdout_status: Literal["terminal_holdout_not_opened"] = "terminal_holdout_not_opened"
    production_authority: bool = False
    schema_version: str = "tomorrow_joint_confirmation_report_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        report_ids = tuple(item.candidate_id for item in self.reports)
        holm_ids = tuple(item.challenger_id for item in self.holm)
        if report_ids != TOMORROW_JOINT_CANDIDATES or holm_ids != TOMORROW_JOINT_CANDIDATES:
            raise ValueError("Tomorrow joint confirmation must retain the complete fixed family")
        expected = "historical_candidate_ready" if self.selected_model is not None else "historical_rejected"
        if self.status != expected:
            raise ValueError("Tomorrow joint confirmation status is inconsistent")
        if self.fallback_to_c3 != (self.selected_model is not None and self.selected_model.candidate_id == "c3"):
            raise ValueError("Tomorrow joint C3 fallback marker is inconsistent")
        if (
            self.production_authority
            or self.terminal_holdout_status != "terminal_holdout_not_opened"
            or self.schema_version != "tomorrow_joint_confirmation_report_v1"
        ):
            raise ValueError("Tomorrow joint confirmation cannot authorize production or open terminal holdout")
        object.__setattr__(self, "content_hash", _canonical_hash(self))


def fit_tomorrow_joint_candidate(
    rows: tuple[TomorrowJointAlignedRow, ...],
    *,
    candidate_id: TomorrowJointCandidateId,
    regularization_lambda: float,
) -> TomorrowJointCandidateFit:
    """Fit one convex candidate on a development-training slice."""

    ordered = _validate_aligned_rows(rows)
    if candidate_id == "c3" or candidate_id not in TOMORROW_JOINT_CANDIDATES:
        raise ValueError("Only fused candidates accept a regularization lambda")
    if regularization_lambda not in TOMORROW_JOINT_LAMBDAS:
        raise ValueError("Tomorrow joint lambda is not preregistered")
    weights = _fit_simplex_weights(ordered, _CANDIDATE_INDICES[candidate_id], regularization_lambda)
    loss = _mean_squared_error(ordered, weights)
    return TomorrowJointCandidateFit(
        candidate_id=candidate_id,
        regularization_lambda=regularization_lambda,
        weights=weights,
        training_rows=len(ordered),
        training_mean_squared_error=loss,
    )


def fit_tomorrow_joint_candidate_family(
    training_rows: tuple[TomorrowJointAlignedRow, ...],
    tuning_rows: tuple[TomorrowJointAlignedRow, ...],
) -> TomorrowJointCandidateFamily:
    """Freeze all structures, selecting only each structure's lambda by prediction loss."""

    training = _validate_aligned_rows(training_rows)
    tuning = _validate_aligned_rows(tuning_rows)
    if max(row.trade_date for row in training) >= min(row.trade_date for row in tuning):
        raise ValueError("Tomorrow joint training dates must strictly precede tuning dates")
    if max(row.label_matured_at for row in training) >= min(row.trade_date for row in tuning):
        raise ValueError("Tomorrow joint training labels must mature strictly before tuning dates")
    candidates: list[TomorrowJointFittedModel] = [
        TomorrowJointFittedModel(
            candidate_id="c3",
            regularization_lambda=None,
            weights=TomorrowJointWeights(0.0, 0.0, 1.0),
            training_rows=len(training),
            tuning_rows=len(tuning),
            tuning_mean_squared_error=_mean_squared_error(tuning, TomorrowJointWeights(0.0, 0.0, 1.0)),
        )
    ]
    for candidate_id in ("v1_c3", "v1_v2_c3"):
        lambda_candidates: list[TomorrowJointFittedModel] = []
        for regularization_lambda in TOMORROW_JOINT_LAMBDAS:
            trained = fit_tomorrow_joint_candidate(
                training,
                candidate_id=candidate_id,
                regularization_lambda=regularization_lambda,
            )
            lambda_candidates.append(
                TomorrowJointFittedModel(
                    candidate_id=candidate_id,
                    regularization_lambda=regularization_lambda,
                    weights=trained.weights,
                    training_rows=len(training),
                    tuning_rows=len(tuning),
                    tuning_mean_squared_error=_mean_squared_error(tuning, trained.weights),
                )
            )
        candidates.append(
            min(
                lambda_candidates,
                key=lambda item: (item.tuning_mean_squared_error, -(item.regularization_lambda or 0.0)),
            )
        )
    return TomorrowJointCandidateFamily(tuple(candidates))


def predict_tomorrow_joint(
    model: TomorrowJointFittedModel,
    rows: tuple[TomorrowJointAlignedRow, ...],
) -> tuple[TomorrowJointPrediction, ...]:
    """Produce exactly one raw net-excess prediction for every aligned row."""

    ordered = _validate_aligned_rows(rows)
    result: list[TomorrowJointPrediction] = []
    for row in ordered:
        prediction = math.fsum(
            value * weight for value, weight in zip(row.predictors, model.weights.values, strict=True)
        )
        result.append(
            TomorrowJointPrediction(
                candidate_id=model.candidate_id,
                trade_date=row.trade_date,
                code=row.code,
                candidate_order=row.candidate_order,
                label_matured_at=row.label_matured_at,
                actual_net_excess_20bp=row.actual_net_excess_20bp,
                actual_net_excess_50bp=row.actual_net_excess_50bp,
                severe_loss=row.severe_loss,
                v1_predicted_net_excess_20bp=row.v1_predicted_net_excess_20bp,
                v2_predicted_net_excess_20bp=row.v2_predicted_net_excess_20bp,
                c3_predicted_net_excess_20bp=row.c3_predicted_net_excess_20bp,
                predicted_net_excess_20bp=prediction,
                weights=model.weights,
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class _JointValidationInputs:
    candidate_id: TomorrowJointCandidateId
    weights: TomorrowJointWeights
    evidence_dates: tuple[date, ...]
    ordered_evidence: tuple[TomorrowJointDailyPortfolioEvidence, ...]
    paired_20: tuple[float, ...]
    paired_50: tuple[float, ...]
    active_profile: Literal["v1", "v2"]
    paired_active_20: tuple[float, ...]
    paired_active_50: tuple[float, ...]


def _prepare_joint_validation(
    predictions: tuple[TomorrowJointPrediction, ...],
    portfolio_evidence: tuple[TomorrowJointDailyPortfolioEvidence, ...],
) -> _JointValidationInputs:
    if not predictions or not portfolio_evidence:
        raise ValueError("Tomorrow joint validation requires prediction and portfolio evidence")
    candidate_ids = {item.candidate_id for item in predictions}
    weights = {item.weights for item in predictions}
    if len(candidate_ids) != 1 or len(weights) != 1:
        raise ValueError("Tomorrow joint validation requires one frozen candidate")
    prediction_dates = {item.trade_date for item in predictions}
    evidence_dates = tuple(item.trade_date for item in portfolio_evidence)
    if len(set(evidence_dates)) != len(evidence_dates) or prediction_dates != set(evidence_dates):
        raise ValueError("Tomorrow joint validation dates must align exactly")
    ordered = tuple(sorted(portfolio_evidence, key=lambda item: item.trade_date))
    active_profiles = {item.active_profile_id for item in ordered}
    if len(active_profiles) != 1:
        raise ValueError("Tomorrow joint active profile must remain fixed across validation dates")
    return _JointValidationInputs(
        next(iter(candidate_ids)),
        next(iter(weights)),
        evidence_dates,
        ordered,
        tuple(item.joint_net_excess_20bp - item.v1_net_excess_20bp for item in ordered),
        tuple(item.joint_net_excess_50bp - item.v1_net_excess_50bp for item in ordered),
        next(iter(active_profiles)),
        tuple(item.joint_net_excess_20bp - item.active_return_20bp for item in ordered),
        tuple(item.joint_net_excess_50bp - item.active_return_50bp for item in ordered),
    )


def _joint_bootstraps(
    inputs: _JointValidationInputs,
) -> tuple[
    PreregisteredBootstrapResult,
    PreregisteredBootstrapResult,
    PreregisteredBootstrapResult,
    PreregisteredBootstrapResult,
]:
    return tuple(
        paired_moving_block_statistics(
            values,
            plan=PreregisteredBootstrapPlan(f"tomorrow_joint_{label}_v1", 20260901, "joint", 5, 10_000),
        )
        for label, values in (
            ("20bp", inputs.paired_20),
            ("50bp", inputs.paired_50),
            ("active_20bp", inputs.paired_active_20),
            ("active_50bp", inputs.paired_active_50),
        )
    )  # type: ignore[return-value]


def _joint_rank_metrics(
    predictions: tuple[TomorrowJointPrediction, ...],
    evidence_dates: tuple[date, ...],
) -> tuple[float | None, float | None]:
    rank_ics: list[float] = []
    q_spreads: list[float] = []
    for trade_date in sorted(evidence_dates):
        daily = tuple(
            sorted((item for item in predictions if item.trade_date == trade_date), key=lambda item: item.code)
        )
        rank_ic = _spearman(
            tuple(item.predicted_net_excess_20bp for item in daily),
            tuple(item.actual_net_excess_20bp for item in daily),
        )
        if rank_ic is not None:
            rank_ics.append(rank_ic)
        spread = _q5_minus_q1(daily)
        if spread is not None:
            q_spreads.append(spread)
    return (_mean(tuple(rank_ics)) if rank_ics else None, _mean(tuple(q_spreads)) if q_spreads else None)


def _joint_return_failures(
    paired_mean_20: float,
    paired_mean_50: float,
    joint_mean_20: float,
    joint_mean_50: float,
    bootstraps: tuple[PreregisteredBootstrapResult, ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    for failed, reason in (
        (joint_mean_20 <= 0.0, "absolute_20bp_not_positive"),
        (joint_mean_50 <= 0.0, "absolute_50bp_not_positive"),
        (paired_mean_20 <= 0.0, "paired_20bp_not_positive"),
        (paired_mean_50 <= 0.0, "paired_50bp_not_positive"),
        (
            bootstraps[0].confidence_lower is None or bootstraps[0].confidence_lower <= 0.0,
            "bootstrap_20bp_lower_not_positive",
        ),
        (
            bootstraps[1].confidence_lower is None or bootstraps[1].confidence_lower <= 0.0,
            "bootstrap_50bp_lower_not_positive",
        ),
    ):
        if failed:
            failures.append(reason)
    return tuple(failures)


def _joint_risk_failures(
    severe_delta: float,
    turnover_delta_pp: float,
    capacity_delta: float,
    concentration_delta: float,
) -> tuple[str, ...]:
    return tuple(
        reason
        for failed, reason in (
            (severe_delta > 0.0, "severe_loss_rate_worse_than_v1"),
            (turnover_delta_pp > 5.0, "turnover_increment_above_5pp"),
            (capacity_delta < 0.0, "capacity_worse_than_v1"),
            (concentration_delta > 0.0, "concentration_worse_than_v1"),
        )
        if failed
    )


def _joint_active_failures(
    inputs: _JointValidationInputs,
    bootstraps: tuple[PreregisteredBootstrapResult, ...],
) -> tuple[str, ...]:
    if inputs.active_profile != "v2":
        return ()
    return tuple(
        reason
        for failed, reason in (
            (
                _mean(inputs.paired_active_20) <= 0.0
                or bootstraps[2].confidence_lower is None
                or bootstraps[2].confidence_lower <= 0.0,
                "active_profile_20bp_gate_failed",
            ),
            (
                _mean(inputs.paired_active_50) <= 0.0
                or bootstraps[3].confidence_lower is None
                or bootstraps[3].confidence_lower <= 0.0,
                "active_profile_50bp_gate_failed",
            ),
        )
        if failed
    )


def evaluate_tomorrow_joint_validation(
    predictions: tuple[TomorrowJointPrediction, ...],
    portfolio_evidence: tuple[TomorrowJointDailyPortfolioEvidence, ...],
) -> TomorrowJointValidationReport:
    """Evaluate paired profit and diagnostic gates without producing actions or Top6."""

    inputs = _prepare_joint_validation(predictions, portfolio_evidence)
    bootstraps = _joint_bootstraps(inputs)
    paired_mean_20 = _mean(inputs.paired_20)
    paired_mean_50 = _mean(inputs.paired_50)
    joint_mean_20 = _mean(tuple(item.joint_net_excess_20bp for item in inputs.ordered_evidence))
    joint_mean_50 = _mean(tuple(item.joint_net_excess_50bp for item in inputs.ordered_evidence))
    severe_delta = _mean(
        tuple(item.joint_severe_loss_rate - item.v1_severe_loss_rate for item in inputs.ordered_evidence)
    )
    turnover_delta_pp = 100.0 * _mean(tuple(item.joint_turnover - item.v1_turnover for item in inputs.ordered_evidence))
    capacity_delta = _mean(tuple(item.joint_capacity - item.v1_capacity for item in inputs.ordered_evidence))
    concentration_delta = _mean(
        tuple(item.joint_concentration - item.v1_concentration for item in inputs.ordered_evidence)
    )
    mean_rank_ic, mean_q_spread = _joint_rank_metrics(predictions, inputs.evidence_dates)
    failures = [
        *_joint_return_failures(paired_mean_20, paired_mean_50, joint_mean_20, joint_mean_50, bootstraps),
        *_joint_risk_failures(severe_delta, turnover_delta_pp, capacity_delta, concentration_delta),
        *_joint_active_failures(inputs, bootstraps),
    ]
    if mean_rank_ic is None or mean_rank_ic <= 0.0:
        failures.append("rank_ic_not_positive")
    if mean_q_spread is None or mean_q_spread <= 0.0:
        failures.append("q5_minus_q1_not_positive")
    unique_failures = tuple(sorted(set(failures)))
    return TomorrowJointValidationReport(
        candidate_id=inputs.candidate_id,
        weights=inputs.weights,
        evidence_identity=tuple(
            TomorrowJointEvidenceIdentity(
                key=TomorrowJointRowKey(item.trade_date, item.code),
                candidate_order=item.candidate_order,
                label_matured_at=item.label_matured_at,
                actual_net_excess_20bp=item.actual_net_excess_20bp,
                actual_net_excess_50bp=item.actual_net_excess_50bp,
                severe_loss=item.severe_loss,
            )
            for item in sorted(predictions, key=lambda item: (item.trade_date, item.candidate_order, item.code))
        ),
        trade_dates=len(inputs.evidence_dates),
        paired_increment_20bp=paired_mean_20,
        paired_increment_50bp=paired_mean_50,
        paired_daily_increments_20bp=inputs.paired_20,
        paired_daily_increments_50bp=inputs.paired_50,
        bootstrap_20bp=bootstraps[0],
        bootstrap_50bp=bootstraps[1],
        joint_mean_net_excess_20bp=joint_mean_20,
        joint_mean_net_excess_50bp=joint_mean_50,
        severe_loss_rate_delta=severe_delta,
        turnover_increment_percentage_points=turnover_delta_pp,
        mean_rank_ic=mean_rank_ic,
        mean_q5_minus_q1_20bp=mean_q_spread,
        failure_reasons=unique_failures,
        passed=not unique_failures,
        active_profile_id=inputs.active_profile,
        paired_active_increment_20bp=_mean(inputs.paired_active_20),
        paired_active_increment_50bp=_mean(inputs.paired_active_50),
        active_bootstrap_20bp=bootstraps[2],
        active_bootstrap_50bp=bootstraps[3],
        capacity_delta=capacity_delta,
        concentration_delta=concentration_delta,
    )


def select_tomorrow_joint_candidate(
    candidate_family: TomorrowJointCandidateFamily,
    reports: tuple[TomorrowJointValidationReport, ...],
) -> TomorrowJointCandidateSelection:
    """Select a structure only from complete portfolio profit/risk evidence."""

    ordered_reports = tuple(sorted(reports, key=lambda item: TOMORROW_JOINT_CANDIDATES.index(item.candidate_id)))
    if tuple(item.candidate_id for item in ordered_reports) != TOMORROW_JOINT_CANDIDATES:
        raise ValueError("Tomorrow joint selection requires one report for every fixed candidate")
    models = {item.candidate_id: item for item in candidate_family.candidates}
    passing = tuple(item for item in ordered_reports if item.passed)
    selected = None
    if passing:
        best = max(
            passing,
            key=lambda item: (
                item.paired_increment_20bp,
                item.bootstrap_20bp.confidence_lower or -math.inf,
                item.paired_increment_50bp,
                item.bootstrap_50bp.confidence_lower or -math.inf,
                -TOMORROW_JOINT_CANDIDATES.index(item.candidate_id),
            ),
        )
        selected = models[best.candidate_id]
    return TomorrowJointCandidateSelection(
        candidate_family=candidate_family,
        reports=ordered_reports,
        selected_model=selected,
        status="selected_by_portfolio_evidence" if selected is not None else "no_candidate_passed",
    )


def confirm_tomorrow_joint_family(
    candidate_family: TomorrowJointCandidateFamily,
    reports: tuple[TomorrowJointValidationReport, ...],
    *,
    alpha: float = 0.05,
) -> TomorrowJointFamilyConfirmation:
    """Apply one Holm family and require any fused model to beat frozen C3 evidence."""

    ordered_reports = tuple(sorted(reports, key=lambda item: TOMORROW_JOINT_CANDIDATES.index(item.candidate_id)))
    if tuple(item.candidate_id for item in ordered_reports) != TOMORROW_JOINT_CANDIDATES:
        raise ValueError("Tomorrow joint confirmation requires all three fixed candidates")
    if len({item.evidence_identity for item in ordered_reports}) != 1:
        raise ValueError("Tomorrow joint confirmation candidates must use identical evidence")
    if len({item.active_profile_id for item in ordered_reports}) != 1:
        raise ValueError("Tomorrow joint confirmation candidates must use one active profile")
    models = {item.candidate_id: item for item in candidate_family.candidates}
    if any(models[item.candidate_id].weights != item.weights for item in ordered_reports):
        raise ValueError("Tomorrow joint confirmation reports do not bind the frozen family")
    raw_holm = fixed_family_holm(
        {item.candidate_id: item.bootstrap_20bp.p_value for item in ordered_reports},
        family=TOMORROW_JOINT_CANDIDATES,
        alpha=alpha,
    )
    holm_by_id = {item.challenger_id: item for item in raw_holm}
    holm = tuple(holm_by_id[candidate_id] for candidate_id in TOMORROW_JOINT_CANDIDATES)
    c3 = ordered_reports[0]
    fused = tuple(
        item
        for item in ordered_reports[1:]
        if item.passed
        and holm_by_id[item.candidate_id].rejected_null
        and item.paired_increment_20bp > c3.paired_increment_20bp
        and item.paired_increment_50bp > c3.paired_increment_50bp
        and (item.bootstrap_20bp.confidence_lower or -math.inf) > (c3.bootstrap_20bp.confidence_lower or -math.inf)
        and (item.bootstrap_50bp.confidence_lower or -math.inf) > (c3.bootstrap_50bp.confidence_lower or -math.inf)
    )
    selected: TomorrowJointFittedModel | None = None
    if fused:
        best = max(
            fused,
            key=lambda item: (
                item.paired_increment_20bp,
                item.paired_increment_50bp,
                -TOMORROW_JOINT_CANDIDATES.index(item.candidate_id),
            ),
        )
        selected = models[best.candidate_id]
    elif c3.passed and holm_by_id["c3"].rejected_null:
        selected = models["c3"]
    return TomorrowJointFamilyConfirmation(
        candidate_family=candidate_family,
        reports=ordered_reports,
        holm=holm,
        selected_model=selected,
        status="historical_candidate_ready" if selected is not None else "historical_rejected",
        fallback_to_c3=selected is not None and selected.candidate_id == "c3",
    )


def _fit_simplex_weights(
    rows: tuple[TomorrowJointAlignedRow, ...],
    allowed_indices: tuple[int, ...],
    regularization_lambda: float,
) -> TomorrowJointWeights:
    x = np.asarray(tuple(tuple(row.predictors[index] for index in allowed_indices) for row in rows), dtype=float)
    y = np.asarray(tuple(row.actual_net_excess_20bp for row in rows), dtype=float)
    anchor = np.full(len(allowed_indices), 1.0 / len(allowed_indices), dtype=float)
    best_objective = math.inf
    best = np.zeros(len(allowed_indices), dtype=float)
    for support_size in range(1, len(allowed_indices) + 1):
        for support in combinations(range(len(allowed_indices)), support_size):
            selected = np.asarray(support, dtype=int)
            subset_x = x[:, selected]
            subset_anchor = anchor[selected]
            matrix = subset_x.T @ subset_x / len(rows) + regularization_lambda * np.eye(support_size)
            target = subset_x.T @ y / len(rows) + regularization_lambda * subset_anchor
            kkt = np.block(
                [
                    [matrix, np.ones((support_size, 1), dtype=float)],
                    [np.ones((1, support_size), dtype=float), np.zeros((1, 1), dtype=float)],
                ]
            )
            rhs = np.concatenate((target, np.asarray((1.0,), dtype=float)))
            try:
                solved = np.linalg.solve(kkt, rhs)[:support_size]
            except np.linalg.LinAlgError:
                continue
            if np.any(solved < -1e-12):
                continue
            candidate = np.zeros(len(allowed_indices), dtype=float)
            candidate[selected] = np.maximum(solved, 0.0)
            candidate /= candidate.sum()
            residual = x @ candidate - y
            objective = float(np.mean(residual * residual) + regularization_lambda * np.sum((candidate - anchor) ** 2))
            if objective < best_objective:
                best_objective = objective
                best = candidate
    if not math.isfinite(best_objective):
        raise ValueError("Tomorrow joint simplex fit has no feasible solution")
    expanded = [0.0, 0.0, 0.0]
    for local_index, predictor_index in enumerate(allowed_indices):
        expanded[predictor_index] = float(best[local_index])
    expanded = [0.0 if abs(value) < 1e-12 else value for value in expanded]
    total = math.fsum(expanded)
    expanded = [value / total for value in expanded]
    return TomorrowJointWeights(*expanded)


def _validate_aligned_rows(rows: tuple[TomorrowJointAlignedRow, ...]) -> tuple[TomorrowJointAlignedRow, ...]:
    if not rows:
        raise ValueError("Tomorrow joint rows cannot be empty")
    ordered = tuple(sorted(rows, key=lambda item: (item.trade_date, item.candidate_order, item.code)))
    keys = tuple(row.key for row in ordered)
    orders = tuple((row.trade_date, row.candidate_order) for row in ordered)
    if len(set(keys)) != len(keys) or len(set(orders)) != len(orders):
        raise ValueError("Tomorrow joint rows require unique keys and candidate order")
    return ordered


def _mean_squared_error(rows: tuple[TomorrowJointAlignedRow, ...], weights: TomorrowJointWeights) -> float:
    errors = tuple(
        math.fsum(value * weight for value, weight in zip(row.predictors, weights.values, strict=True))
        - row.actual_net_excess_20bp
        for row in rows
    )
    return math.fsum(value * value for value in errors) / len(errors)


def _mean(values: tuple[float, ...]) -> float:
    return math.fsum(values) / len(values)


def _spearman(left: tuple[float, ...], right: tuple[float, ...]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = _mean(left_ranks)
    right_mean = _mean(right_ranks)
    covariance = math.fsum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_ranks, right_ranks, strict=True)
    )
    left_variance = math.fsum((value - left_mean) ** 2 for value in left_ranks)
    right_variance = math.fsum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_variance * right_variance)
    return covariance / denominator if denominator > 0.0 else None


def _average_ranks(values: tuple[float, ...]) -> tuple[float, ...]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    result = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for index in range(start, end):
            result[ordered[index][0]] = rank
        start = end
    return tuple(result)


def _q5_minus_q1(rows: tuple[TomorrowJointPrediction, ...]) -> float | None:
    if len(rows) < 5:
        return None
    ordered = tuple(sorted(rows, key=lambda item: (item.predicted_net_excess_20bp, item.code)))
    size = len(ordered) // 5
    lower = _mean(tuple(item.actual_net_excess_20bp for item in ordered[:size]))
    upper = _mean(tuple(item.actual_net_excess_20bp for item in ordered[-size:]))
    return upper - lower


def _canonical_hash(value: object) -> str:
    def encode(item: object) -> object:
        if dataclasses.is_dataclass(item):
            return {field.name: encode(getattr(item, field.name)) for field in dataclasses.fields(item) if field.init}
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, (tuple, list)):
            return [encode(child) for child in item]
        if isinstance(item, dict):
            return {str(key): encode(child) for key, child in item.items()}
        return item

    payload = json.dumps(encode(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "TOMORROW_JOINT_CANDIDATES",
    "TOMORROW_JOINT_LAMBDAS",
    "TomorrowJointAlignedRow",
    "TomorrowJointCandidateFamily",
    "TomorrowJointCandidateFit",
    "TomorrowJointCandidateSelection",
    "TomorrowJointCandidateId",
    "TomorrowJointDailyPortfolioEvidence",
    "TomorrowJointEvidenceIdentity",
    "TomorrowJointFittedModel",
    "TomorrowJointFamilyConfirmation",
    "TomorrowJointInsufficientTerminal",
    "TomorrowJointPrediction",
    "TomorrowJointPredictionSemantics",
    "TomorrowJointRowKey",
    "TomorrowJointValidationReport",
    "TomorrowJointWeights",
    "evaluate_tomorrow_joint_validation",
    "confirm_tomorrow_joint_family",
    "fit_tomorrow_joint_candidate",
    "fit_tomorrow_joint_candidate_family",
    "predict_tomorrow_joint",
    "select_tomorrow_joint_candidate",
    "seal_tomorrow_joint_insufficient_terminal",
]

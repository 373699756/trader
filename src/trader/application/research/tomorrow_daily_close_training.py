"""Immutable contracts for production-isolated Tomorrow daily-close training."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.tomorrow_daily_close import ExpandingWalkForwardFold

DailyCloseBoard = Literal["main", "chinext", "star"]
DailyCloseTrainingStatus = Literal[
    "historical_data_insufficient",
    "historical_rejected",
    "historical_daily_close_proxy_validated",
]
CorrectionDimension = Literal["board", "liquidity", "volatility"]
BaseModelKind = Literal["ridge", "lightgbm", "ridge_lightgbm_ensemble"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,96}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")
_CODE = re.compile(r"^\d{6}$")
_BOARDS = {"main", "chinext", "star"}
_RESEARCH_IDENTITY = "score_tomorrow_daily_close_challenger_v1"
_LABEL_ID = "d1_close_market_excess_after_cost_v1"
_PROXY_ANCHOR = "15:00_close"
_COST_RATES = (0.002, 0.005, 0.01)


@dataclass(frozen=True)
class DailyCloseSourceSample:
    """Boundary input that may still fail historical hard-filter evidence checks."""

    trade_date: date
    label_maturity_date: date
    code: str
    board: DailyCloseBoard
    feature_values: tuple[float, ...]
    net_excess_returns: tuple[float, float, float]
    hard_filter_passed: bool
    hard_filter_evidence_complete: bool
    filter_evidence_hash: str | None
    source_row_hash: str

    def __post_init__(self) -> None:
        _validate_code(self.code)
        if self.board not in _BOARDS:
            raise ValueError("daily-close source board is invalid")
        if self.label_maturity_date <= self.trade_date:
            raise ValueError("daily-close label must mature after the feature date")
        if (
            not self.feature_values
            or len(self.net_excess_returns) != len(_COST_RATES)
            or not _all_finite((*self.feature_values, *self.net_excess_returns))
        ):
            raise ValueError("daily-close source numeric values must be finite and non-empty")
        _validate_hash(self.source_row_hash, "daily-close source row")
        if self.hard_filter_evidence_complete:
            if self.filter_evidence_hash is None:
                raise ValueError("complete hard-filter evidence requires a hash")
            _validate_hash(self.filter_evidence_hash, "daily-close filter evidence")
        elif self.filter_evidence_hash is not None:
            raise ValueError("incomplete hard-filter evidence cannot claim a complete hash")


@dataclass(frozen=True)
class DailyCloseFeatureRow:
    trade_date: date
    label_maturity_date: date
    code: str
    board: DailyCloseBoard
    feature_values: tuple[float, ...]
    net_excess_returns: tuple[float, float, float]
    filter_evidence_hash: str
    source_row_hash: str

    def __post_init__(self) -> None:
        _validate_code(self.code)
        if self.board not in _BOARDS or self.label_maturity_date <= self.trade_date:
            raise ValueError("daily-close feature row identity is invalid")
        if (
            not self.feature_values
            or len(self.net_excess_returns) != len(_COST_RATES)
            or not _all_finite((*self.feature_values, *self.net_excess_returns))
        ):
            raise ValueError("daily-close feature row numeric values must be finite and non-empty")
        _validate_hash(self.filter_evidence_hash, "daily-close filter evidence")
        _validate_hash(self.source_row_hash, "daily-close source row")


@dataclass(frozen=True)
class DatasetManifest:
    source_archive_hash: str
    filter_spec_hash: str
    trading_dates: tuple[date, ...]
    feature_names: tuple[str, ...]
    feature_units: tuple[str, ...]
    source_rows: int
    accepted_rows: int
    rejected_filter_evidence_rows: int
    rejected_hard_filter_rows: int
    research_identity: str = _RESEARCH_IDENTITY
    label_id: str = _LABEL_ID
    proxy_anchor: str = _PROXY_ANCHOR
    cost_rates: tuple[float, float, float] = _COST_RATES
    schema_version: str = "tomorrow_daily_close_dataset_manifest_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_hash(self.source_archive_hash, "daily-close source archive")
        _validate_hash(self.filter_spec_hash, "daily-close filter spec")
        _validate_feature_contract(self.feature_names, self.feature_units)
        _strict_dates(self.trading_dates)
        counts = (
            self.source_rows,
            self.accepted_rows,
            self.rejected_filter_evidence_rows,
            self.rejected_hard_filter_rows,
        )
        if min(counts) < 0 or self.source_rows != sum(counts[1:]):
            raise ValueError("daily-close manifest row counts are inconsistent")
        if (
            self.research_identity != _RESEARCH_IDENTITY
            or self.label_id != _LABEL_ID
            or self.proxy_anchor != _PROXY_ANCHOR
            or self.cost_rates != _COST_RATES
            or self.schema_version != "tomorrow_daily_close_dataset_manifest_v1"
        ):
            raise ValueError("daily-close manifest identity is invalid")
        if self.production_authority:
            raise ValueError("daily-close research manifest cannot authorize production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class FeatureDataset:
    manifest: DatasetManifest
    rows: tuple[DailyCloseFeatureRow, ...]
    schema_version: str = "tomorrow_daily_close_feature_dataset_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        rows = tuple(sorted(self.rows, key=lambda item: (item.trade_date, item.code)))
        identities = tuple((row.trade_date, row.code) for row in rows)
        if len(identities) != len(set(identities)):
            raise ValueError("daily-close feature rows must be unique by date and code")
        if len(rows) != self.manifest.accepted_rows:
            raise ValueError("daily-close feature rows do not match their manifest")
        if any(row.trade_date not in self.manifest.trading_dates for row in rows):
            raise ValueError("daily-close feature rows contain dates outside the manifest")
        if any(len(row.feature_values) != len(self.manifest.feature_names) for row in rows):
            raise ValueError("daily-close feature row width does not match the manifest")
        if self.schema_version != "tomorrow_daily_close_feature_dataset_v1":
            raise ValueError("daily-close feature dataset schema is invalid")
        if self.production_authority:
            raise ValueError("daily-close feature dataset cannot authorize production")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ValidationMetrics:
    evaluated_trade_dates: int
    evaluated_rows: int
    net_excess_return_20bp: float | None
    net_excess_return_50bp: float | None
    bootstrap_lower_bound_20bp: float | None
    bootstrap_lower_bound_50bp: float | None
    control_severe_loss_rate: float | None
    candidate_severe_loss_rate: float | None
    turnover_increase: float | None
    rank_ic: float | None
    top_bottom_quintile_spread: float | None

    def __post_init__(self) -> None:
        if min(self.evaluated_trade_dates, self.evaluated_rows) < 0:
            raise ValueError("daily-close validation counts cannot be negative")
        values = (
            self.net_excess_return_20bp,
            self.net_excess_return_50bp,
            self.bootstrap_lower_bound_20bp,
            self.bootstrap_lower_bound_50bp,
            self.control_severe_loss_rate,
            self.candidate_severe_loss_rate,
            self.turnover_increase,
            self.rank_ic,
            self.top_bottom_quintile_spread,
        )
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("daily-close validation metrics must be finite when present")
        severe_loss_rates = (self.control_severe_loss_rate, self.candidate_severe_loss_rate)
        if any(value is not None and not 0.0 <= value <= 1.0 for value in severe_loss_rates):
            raise ValueError("daily-close severe loss rates must be in [0, 1]")

    @classmethod
    def empty(cls) -> ValidationMetrics:
        return cls(0, 0, None, None, None, None, None, None, None, None, None)


@dataclass(frozen=True)
class ValidationReport:
    status: DailyCloseTrainingStatus
    manifest_hash: str
    candidate_model_artifact_hash: str | None
    metrics: ValidationMetrics
    failure_reasons: tuple[str, ...]
    research_identity: str = _RESEARCH_IDENTITY
    proxy_anchor: str = _PROXY_ANCHOR
    schema_version: str = "tomorrow_daily_close_validation_report_v1"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_validation_report_identity(self)
        reasons = _validate_validation_report_outcome(self)
        if self.production_authority or self.automatic_model_update:
            raise ValueError("daily-close validation report cannot authorize production or automatic updates")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_validation_report_identity(report: ValidationReport) -> None:
    if report.status not in {
        "historical_data_insufficient",
        "historical_rejected",
        "historical_daily_close_proxy_validated",
    }:
        raise ValueError("daily-close validation terminal status is invalid")
    _validate_hash(report.manifest_hash, "daily-close validation manifest")
    if report.candidate_model_artifact_hash is not None:
        _validate_hash(report.candidate_model_artifact_hash, "daily-close candidate model artifact")
    if (
        report.research_identity != _RESEARCH_IDENTITY
        or report.proxy_anchor != _PROXY_ANCHOR
        or report.schema_version != "tomorrow_daily_close_validation_report_v1"
    ):
        raise ValueError("daily-close validation report identity is invalid")


def _validate_validation_report_outcome(report: ValidationReport) -> tuple[str, ...]:
    reasons = tuple(sorted(set(report.failure_reasons)))
    if any(_REASON.fullmatch(reason) is None for reason in reasons):
        raise ValueError("daily-close validation failure reasons are invalid")
    if report.status == "historical_daily_close_proxy_validated":
        if reasons:
            raise ValueError("validated daily-close report requires no failure reasons")
        if report.candidate_model_artifact_hash is None:
            raise ValueError("validated daily-close report requires a candidate model artifact")
        if not _passes_validation_metrics(report.metrics):
            raise ValueError("validated daily-close report must satisfy registered return and risk gates")
    elif not reasons:
        raise ValueError("rejected or insufficient daily-close report requires bounded failure reasons")
    if report.status == "historical_data_insufficient" and report.candidate_model_artifact_hash is not None:
        raise ValueError("insufficient daily-close evidence cannot bind a candidate model artifact")
    return reasons


@dataclass(frozen=True)
class ModelDependencyVersion:
    name: str
    version: str

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.name) is None or not self.version.strip() or len(self.version) > 64:
            raise ValueError("daily-close model dependency identity is invalid")


@dataclass(frozen=True)
class StratumCorrection:
    dimension: CorrectionDimension
    key: str
    sample_count: int
    minimum_sample_count: int
    shrinkage_constant: float
    correction: float

    def __post_init__(self) -> None:
        if self.dimension not in {"board", "liquidity", "volatility"}:
            raise ValueError("daily-close stratum correction dimension is invalid")
        if (
            not self.key.strip()
            or self.minimum_sample_count < 1
            or self.sample_count < self.minimum_sample_count
            or not math.isfinite(self.shrinkage_constant)
            or self.shrinkage_constant <= 0.0
            or not math.isfinite(self.correction)
        ):
            raise ValueError("daily-close stratum correction is invalid")


@dataclass(frozen=True)
class StockResidualCorrection:
    code: str
    sample_count: int
    distinct_trade_dates: int
    shrinkage_constant: float
    prediction_cross_section_stddev: float
    correction: float

    def __post_init__(self) -> None:
        _validate_code(self.code)
        values = (self.shrinkage_constant, self.prediction_cross_section_stddev, self.correction)
        if (
            self.sample_count < 250
            or self.distinct_trade_dates < 120
            or not _all_finite(values)
            or self.shrinkage_constant < 1_000.0
            or self.prediction_cross_section_stddev <= 0.0
        ):
            raise ValueError("daily-close stock residual correction violates its shrinkage contract")
        if abs(self.correction) > self.prediction_cross_section_stddev * 0.10:
            raise ValueError("daily-close stock residual correction exceeds 10% of prediction stddev")


@dataclass(frozen=True)
class CandidateModelArtifact:
    model_id: str
    candidate_id: str
    base_model_kind: BaseModelKind
    manifest_hash: str
    filter_spec_hash: str
    confirmation_report_hash: str
    feature_names: tuple[str, ...]
    feature_units: tuple[str, ...]
    preprocessing_means: tuple[float, ...]
    preprocessing_scales: tuple[float, ...]
    ridge_intercept: float | None
    ridge_coefficients: tuple[float, ...] | None
    lightgbm_model_text: str | None
    lightgbm_best_iteration: int | None
    stratum_corrections: tuple[StratumCorrection, ...]
    stock_residual_corrections: tuple[StockResidualCorrection, ...]
    trained_from: date
    trained_through: date
    dependencies: tuple[ModelDependencyVersion, ...]
    schema_version: str = "tomorrow_daily_close_candidate_model_artifact_v1"
    production_authority: bool = False
    automatic_model_update: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        width = _validate_candidate_model_identity(self)
        _validate_candidate_model_payload(self, width)
        strata, stocks, dependencies = _validate_candidate_model_collections(self)
        _validate_candidate_model_metadata(self)
        object.__setattr__(self, "stratum_corrections", strata)
        object.__setattr__(self, "stock_residual_corrections", stocks)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_candidate_model_identity(artifact: CandidateModelArtifact) -> int:
    if _IDENTITY.fullmatch(artifact.model_id) is None or _IDENTITY.fullmatch(artifact.candidate_id) is None:
        raise ValueError("daily-close candidate model identity is invalid")
    if artifact.base_model_kind not in {"ridge", "lightgbm", "ridge_lightgbm_ensemble"}:
        raise ValueError("daily-close candidate base model kind is invalid")
    _validate_hash(artifact.manifest_hash, "daily-close candidate manifest")
    _validate_hash(artifact.filter_spec_hash, "daily-close candidate filter spec")
    _validate_hash(artifact.confirmation_report_hash, "daily-close confirmation report")
    _validate_feature_contract(artifact.feature_names, artifact.feature_units)
    width = len(artifact.feature_names)
    if len(artifact.preprocessing_means) != width or len(artifact.preprocessing_scales) != width:
        raise ValueError("daily-close candidate model vector widths are inconsistent")
    return width


def _validate_candidate_model_payload(artifact: CandidateModelArtifact, width: int) -> None:
    if (artifact.ridge_intercept is None) != (artifact.ridge_coefficients is None):
        raise ValueError("daily-close candidate base model payload does not match its kind")
    if (artifact.lightgbm_model_text is None) != (artifact.lightgbm_best_iteration is None):
        raise ValueError("daily-close candidate base model payload does not match its kind")
    has_ridge = artifact.ridge_intercept is not None
    has_lightgbm = artifact.lightgbm_model_text is not None
    expected_payloads = {
        "ridge": (True, False),
        "lightgbm": (False, True),
        "ridge_lightgbm_ensemble": (True, True),
    }
    if (has_ridge, has_lightgbm) != expected_payloads[artifact.base_model_kind]:
        raise ValueError("daily-close candidate base model payload does not match its kind")
    if has_ridge and artifact.ridge_coefficients is not None and len(artifact.ridge_coefficients) != width:
        raise ValueError("daily-close candidate model vector widths are inconsistent")
    ridge_numeric = (
        ()
        if artifact.ridge_intercept is None or artifact.ridge_coefficients is None
        else (artifact.ridge_intercept, *artifact.ridge_coefficients)
    )
    numeric = (*artifact.preprocessing_means, *artifact.preprocessing_scales, *ridge_numeric)
    if not _all_finite(numeric) or any(value <= 0.0 for value in artifact.preprocessing_scales):
        raise ValueError("daily-close candidate model numeric parameters are invalid")
    if has_lightgbm and (
        artifact.lightgbm_model_text is None
        or not artifact.lightgbm_model_text.strip()
        or artifact.lightgbm_best_iteration is None
        or artifact.lightgbm_best_iteration < 1
    ):
        raise ValueError("daily-close LightGBM artifact is incomplete")


def _validate_candidate_model_collections(
    artifact: CandidateModelArtifact,
) -> tuple[tuple[StratumCorrection, ...], tuple[StockResidualCorrection, ...], tuple[ModelDependencyVersion, ...]]:
    strata = tuple(sorted(artifact.stratum_corrections, key=lambda item: (item.dimension, item.key)))
    stocks = tuple(sorted(artifact.stock_residual_corrections, key=lambda item: item.code))
    dependencies = tuple(sorted(artifact.dependencies, key=lambda item: item.name))
    if len({(item.dimension, item.key) for item in strata}) != len(strata):
        raise ValueError("daily-close stratum corrections must be unique")
    if len({item.code for item in stocks}) != len(stocks):
        raise ValueError("daily-close stock residual corrections must be unique")
    if not dependencies or len({item.name for item in dependencies}) != len(dependencies):
        raise ValueError("daily-close model dependencies must be present and unique")
    return strata, stocks, dependencies


def _validate_candidate_model_metadata(artifact: CandidateModelArtifact) -> None:
    if artifact.trained_from > artifact.trained_through:
        raise ValueError("daily-close candidate model training dates are invalid")
    if artifact.schema_version != "tomorrow_daily_close_candidate_model_artifact_v1":
        raise ValueError("daily-close candidate model schema is invalid")
    if artifact.production_authority or artifact.automatic_model_update:
        raise ValueError("daily-close candidate model cannot authorize production or automatic updates")


def build_feature_dataset(
    samples: tuple[DailyCloseSourceSample, ...],
    *,
    feature_names: tuple[str, ...],
    feature_units: tuple[str, ...],
    source_archive_hash: str,
    filter_spec_hash: str,
) -> FeatureDataset:
    """Admit only hard-filter-passing samples with complete point-in-time evidence."""

    if not samples:
        raise ValueError("daily-close source samples cannot be empty")
    _validate_feature_contract(feature_names, feature_units)
    ordered = tuple(sorted(samples, key=lambda item: (item.trade_date, item.code)))
    identities = tuple((item.trade_date, item.code) for item in ordered)
    if len(identities) != len(set(identities)):
        raise ValueError("daily-close source samples must be unique by date and code")
    if any(len(item.feature_values) != len(feature_names) for item in ordered):
        raise ValueError("daily-close source sample width does not match the feature contract")

    rejected_evidence = sum(not item.hard_filter_evidence_complete for item in ordered)
    rejected_filter = sum(item.hard_filter_evidence_complete and not item.hard_filter_passed for item in ordered)
    admitted = tuple(
        DailyCloseFeatureRow(
            trade_date=item.trade_date,
            label_maturity_date=item.label_maturity_date,
            code=item.code,
            board=item.board,
            feature_values=item.feature_values,
            net_excess_returns=item.net_excess_returns,
            filter_evidence_hash=item.filter_evidence_hash,
            source_row_hash=item.source_row_hash,
        )
        for item in ordered
        if item.hard_filter_evidence_complete and item.hard_filter_passed and item.filter_evidence_hash is not None
    )
    trading_dates = tuple(sorted({item.trade_date for item in ordered}))
    if not admitted:
        raise ValueError("daily-close source contains no hard-filter eligible samples")
    manifest = DatasetManifest(
        source_archive_hash=source_archive_hash,
        filter_spec_hash=filter_spec_hash,
        trading_dates=trading_dates,
        feature_names=feature_names,
        feature_units=feature_units,
        source_rows=len(ordered),
        accepted_rows=len(admitted),
        rejected_filter_evidence_rows=rejected_evidence,
        rejected_hard_filter_rows=rejected_filter,
    )
    return FeatureDataset(manifest=manifest, rows=admitted)


def select_mature_fold_training_rows(
    dataset: FeatureDataset,
    fold: ExpandingWalkForwardFold,
) -> tuple[DailyCloseFeatureRow, ...]:
    """Select only labels known strictly before a fold's first validation session."""

    return select_mature_training_rows(dataset, fold.training_dates, prediction_start=fold.validation_dates[0])


def select_mature_training_rows(
    dataset: FeatureDataset,
    training_dates: tuple[date, ...],
    *,
    prediction_start: date,
) -> tuple[DailyCloseFeatureRow, ...]:
    """Prevent labels that mature at or after an evaluation boundary from entering fitting."""

    if not training_dates or any(
        left >= right for left, right in zip(training_dates, training_dates[1:], strict=False)
    ):
        raise ValueError("daily-close training dates must be strictly increasing")
    if training_dates[-1] >= prediction_start:
        raise ValueError("daily-close training dates must precede the prediction boundary")
    eligible_dates = frozenset(training_dates)
    return tuple(
        row for row in dataset.rows if row.trade_date in eligible_dates and row.label_maturity_date < prediction_start
    )


def _validate_feature_contract(names: tuple[str, ...], units: tuple[str, ...]) -> None:
    if not names or len(names) != len(units) or len(set(names)) != len(names):
        raise ValueError("daily-close feature names and units are inconsistent")
    if any(_IDENTITY.fullmatch(name) is None for name in names):
        raise ValueError("daily-close feature names are invalid")
    if any(not unit.strip() or len(unit) > 32 for unit in units):
        raise ValueError("daily-close feature units are invalid")


def _passes_validation_metrics(metrics: ValidationMetrics) -> bool:
    required = (
        metrics.net_excess_return_20bp,
        metrics.net_excess_return_50bp,
        metrics.bootstrap_lower_bound_20bp,
        metrics.bootstrap_lower_bound_50bp,
        metrics.control_severe_loss_rate,
        metrics.candidate_severe_loss_rate,
        metrics.turnover_increase,
        metrics.rank_ic,
        metrics.top_bottom_quintile_spread,
    )
    return (
        metrics.evaluated_trade_dates > 0
        and metrics.evaluated_rows > 0
        and all(value is not None for value in required)
        and metrics.net_excess_return_20bp is not None
        and metrics.net_excess_return_20bp > 0.0
        and metrics.net_excess_return_50bp is not None
        and metrics.net_excess_return_50bp > 0.0
        and metrics.bootstrap_lower_bound_20bp is not None
        and metrics.bootstrap_lower_bound_20bp > 0.0
        and metrics.bootstrap_lower_bound_50bp is not None
        and metrics.bootstrap_lower_bound_50bp > 0.0
        and metrics.control_severe_loss_rate is not None
        and metrics.candidate_severe_loss_rate is not None
        and metrics.candidate_severe_loss_rate <= metrics.control_severe_loss_rate
        and metrics.turnover_increase is not None
        and metrics.turnover_increase <= 0.05
        and metrics.rank_ic is not None
        and metrics.rank_ic > 0.0
        and metrics.top_bottom_quintile_spread is not None
        and metrics.top_bottom_quintile_spread > 0.0
    )


def _strict_dates(values: tuple[date, ...]) -> None:
    if not values or any(left >= right for left, right in zip(values, values[1:], strict=False)):
        raise ValueError("daily-close manifest trading dates must be strictly increasing")


def _validate_hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} identity must be SHA-256")


def _validate_code(code: str) -> None:
    if _CODE.fullmatch(code) is None:
        raise ValueError("daily-close stock code must contain exactly six digits")


def _all_finite(values: tuple[float, ...]) -> bool:
    return all(math.isfinite(value) for value in values)


__all__ = [
    "CandidateModelArtifact",
    "BaseModelKind",
    "CorrectionDimension",
    "DailyCloseBoard",
    "DailyCloseFeatureRow",
    "DailyCloseSourceSample",
    "DailyCloseTrainingStatus",
    "DatasetManifest",
    "FeatureDataset",
    "ModelDependencyVersion",
    "StockResidualCorrection",
    "StratumCorrection",
    "ValidationMetrics",
    "ValidationReport",
    "build_feature_dataset",
    "select_mature_fold_training_rows",
    "select_mature_training_rows",
]

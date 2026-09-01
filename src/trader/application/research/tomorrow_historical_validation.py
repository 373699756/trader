"""Historical-only validation for Tomorrow portfolios and severe-loss probability."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPredictorPort
from trader.application.research.replay_models import canonical_hash
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalScreeningSpec
from trader.domain.research.shadow_calibration import (
    LinearModel,
    PlattCalibrator,
    fit_logistic_model,
    fit_platt_calibrator,
)

_MODEL_FEATURE_IDS = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
_PRIMARY_COST = 0.002
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,160}$")


@dataclass(frozen=True)
class HistoricalRiskValidationSpec:
    research_identity: str = "tomorrow_v2_historical_risk_probability_v1"
    training_trade_dates: int = 60
    calibration_trade_dates: int = 20
    test_trade_dates: int = 40
    embargo_trade_dates_per_boundary: int = 1
    severe_loss_mae_atr20: float = -1.5
    maximum_expected_calibration_error: float = 0.05
    calibration_bins: int = 10
    production_authority: bool = False
    schema_version: str = "tomorrow_v2_historical_risk_validation_spec_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.research_identity != "tomorrow_v2_historical_risk_probability_v1"
            or (self.training_trade_dates, self.calibration_trade_dates, self.test_trade_dates) != (60, 20, 40)
            or self.embargo_trade_dates_per_boundary != 1
            or self.severe_loss_mae_atr20 != -1.5
            or self.maximum_expected_calibration_error != 0.05
            or self.calibration_bins != 10
            or self.production_authority
            or self.schema_version != "tomorrow_v2_historical_risk_validation_spec_v1"
        ):
            raise ValueError("Tomorrow historical risk validation contract is fixed")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def required_trade_dates(self) -> int:
        return (
            self.training_trade_dates
            + self.calibration_trade_dates
            + self.test_trade_dates
            + 2 * self.embargo_trade_dates_per_boundary
        )


HISTORICAL_RISK_VALIDATION_SPEC = HistoricalRiskValidationSpec()


@dataclass(frozen=True, order=True)
class TomorrowHistoricalRiskRow:
    trade_date: date
    code: str
    board: Literal["main", "chinext", "star"]
    alpha_features: tuple[float, float, float, float, float, float]
    realized_volatility_20d: float
    downside_semivariance_20d: float
    drawdown_recovery_60d: float
    amihud_20d: float
    average_amount_20d: float
    baseline_score: float
    gross_return: float
    benchmark_return: float
    gross_excess_return: float
    atr20_pct: float
    mae_atr20: float

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or self.board not in {"main", "chinext", "star"}
            or len(self.alpha_features) != len(_MODEL_FEATURE_IDS)
        ):
            raise ValueError("Tomorrow historical risk row identity is invalid")
        values = (
            *self.alpha_features,
            self.realized_volatility_20d,
            self.downside_semivariance_20d,
            self.drawdown_recovery_60d,
            self.amihud_20d,
            self.average_amount_20d,
            self.baseline_score,
            self.gross_return,
            self.benchmark_return,
            self.gross_excess_return,
            self.atr20_pct,
            self.mae_atr20,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Tomorrow historical risk row values must be finite")
        if (
            min(
                self.realized_volatility_20d,
                self.downside_semivariance_20d,
                self.amihud_20d,
                self.average_amount_20d,
                self.atr20_pct,
            )
            < 0.0
        ):
            raise ValueError("Tomorrow historical risk row magnitudes cannot be negative")


@dataclass(frozen=True)
class HistoricalSelectedDay:
    trade_date: date
    status: Literal["invested", "cash"]
    selected_codes: tuple[str, ...]
    gross_return: float
    benchmark_return: float
    turnover: float
    net_excess_return_20bp: float

    def __post_init__(self) -> None:
        values = (self.gross_return, self.benchmark_return, self.turnover, self.net_excess_return_20bp)
        if any(not math.isfinite(value) for value in values) or not 0.0 <= self.turnover <= 1.0:
            raise ValueError("Historical selected-day metrics are invalid")
        if self.status not in {"invested", "cash"} or (self.status == "cash") != (not self.selected_codes):
            raise ValueError("Historical selected-day status must match its portfolio")


HistoricalRiskStatus = Literal["historical_validated", "historical_rejected", "historical_data_insufficient"]


@dataclass(frozen=True)
class HistoricalRiskModelArtifact:
    spec_hash: str
    source_spec_hash: str
    parent_model_id: str
    parent_model_hash: str
    feature_ids: tuple[str, str, str, str, str]
    logistic_intercept: float
    logistic_coefficients: tuple[float, ...]
    platt_intercept: float
    platt_slope: float
    platt_constant: float | None
    training_evidence_hash: str
    calibration_evidence_hash: str
    production_authority: bool = False
    schema_version: str = "tomorrow_v2_historical_risk_model_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.spec_hash,
            self.source_spec_hash,
            self.parent_model_hash,
            self.training_evidence_hash,
            self.calibration_evidence_hash,
        )
        values = (self.logistic_intercept, *self.logistic_coefficients, self.platt_intercept, self.platt_slope)
        if (
            self.spec_hash != HISTORICAL_RISK_VALIDATION_SPEC.content_hash
            or self.source_spec_hash != SCORE_H0_V1_SPEC.content_hash
            or any(_SHA256.fullmatch(value) is None for value in hashes)
            or not self.parent_model_id
            or self.feature_ids
            != (
                "net_predicted_excess_20bp",
                "model_disagreement",
                "signal_score",
                "atr20_pct",
                "estimated_cost",
            )
            or len(self.logistic_coefficients) != len(self.feature_ids)
            or any(not math.isfinite(value) for value in values)
            or (self.platt_constant is not None and not 0.0 < self.platt_constant < 1.0)
            or self.production_authority
            or self.schema_version != "tomorrow_v2_historical_risk_model_v1"
        ):
            raise ValueError("Historical risk model artifact is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    def predict(self, rows: tuple[tuple[float, float, float, float, float], ...]) -> tuple[float, ...]:
        model = LinearModel(self.logistic_intercept, self.logistic_coefficients, logistic=True)
        calibrator = PlattCalibrator(self.platt_intercept, self.platt_slope, self.platt_constant)
        return calibrator.predict(model.predict(rows))


@dataclass(frozen=True)
class HistoricalRiskValidationReport:
    spec_hash: str
    model_id: str
    model_hash: str
    evidence_hash: str
    training_trade_dates: int
    calibration_trade_dates: int
    test_trade_dates: int
    embargo_trade_dates: int
    training_rows: int
    calibration_rows: int
    test_rows: int
    brier_score: float | None
    baseline_brier_score: float | None
    expected_calibration_error: float | None
    model_artifact_hash: str | None
    status: HistoricalRiskStatus
    failure_reasons: tuple[str, ...]
    production_authority: bool = False
    schema_version: str = "tomorrow_v2_historical_risk_validation_report_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_risk_report_identity(self)
        _validate_risk_report_state(self)
        object.__setattr__(self, "failure_reasons", tuple(sorted(set(self.failure_reasons))))
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_risk_report_identity(report: HistoricalRiskValidationReport) -> None:
    if (
        report.spec_hash != HISTORICAL_RISK_VALIDATION_SPEC.content_hash
        or not report.model_id
        or _SHA256.fullmatch(report.model_hash) is None
        or _SHA256.fullmatch(report.evidence_hash) is None
    ):
        raise ValueError("Historical risk report identity is invalid")
    if report.model_artifact_hash is not None and _SHA256.fullmatch(report.model_artifact_hash) is None:
        raise ValueError("Historical risk report model hash is invalid")
    if report.production_authority:
        raise ValueError("Historical risk validation cannot authorize production")


def _validate_risk_report_state(report: HistoricalRiskValidationReport) -> None:
    counts = (
        report.training_trade_dates,
        report.calibration_trade_dates,
        report.test_trade_dates,
        report.embargo_trade_dates,
        report.training_rows,
        report.calibration_rows,
        report.test_rows,
    )
    if any(value < 0 for value in counts) or report.status not in {
        "historical_validated",
        "historical_rejected",
        "historical_data_insufficient",
    }:
        raise ValueError("Historical risk report state is invalid")
    metrics = (report.brier_score, report.baseline_brier_score, report.expected_calibration_error)
    if any(value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0) for value in metrics):
        raise ValueError("Historical risk probability metrics must be in [0, 1]")
    if any(_REASON.fullmatch(reason) is None for reason in report.failure_reasons):
        raise ValueError("Historical risk report failure reason is invalid")
    if report.status == "historical_validated" and report.failure_reasons:
        raise ValueError("Passing historical risk report cannot contain failures")
    if report.status != "historical_validated" and not report.failure_reasons:
        raise ValueError("Rejected historical risk report requires bounded failures")
    insufficient = report.status == "historical_data_insufficient"
    if (report.model_artifact_hash is None) != insufficient:
        raise ValueError("Historical risk report model binding is inconsistent")
    if insufficient and (any(counts) or any(value is not None for value in metrics)):
        raise ValueError("Insufficient historical risk report cannot contain fitted evidence")
    if not insufficient and (
        (
            report.training_trade_dates,
            report.calibration_trade_dates,
            report.test_trade_dates,
            report.embargo_trade_dates,
        )
        != (60, 20, 40, 2)
        or min(report.training_rows, report.calibration_rows, report.test_rows) < 1
        or any(value is None for value in metrics)
    ):
        raise ValueError("Terminal historical risk report requires the fixed complete split")


@dataclass(frozen=True)
class HistoricalRiskValidationOutcome:
    report: HistoricalRiskValidationReport
    model_artifact: HistoricalRiskModelArtifact | None

    def __post_init__(self) -> None:
        expected = self.model_artifact.content_hash if self.model_artifact is not None else None
        if self.report.model_artifact_hash != expected:
            raise ValueError("Historical risk outcome model binding is invalid")


class HistoricalRiskEvidence(Protocol):
    def tomorrow_historical_risk_rows(self, spec: HistoricalScreeningSpec) -> Sequence[TomorrowHistoricalRiskRow]: ...


class HistoricalRiskValidationService:
    def __init__(self, evidence: HistoricalRiskEvidence, predictor: TomorrowModelPredictorPort) -> None:
        self._evidence = evidence
        self._predictor = predictor

    def execute(self) -> HistoricalRiskValidationOutcome:
        rows = tuple(self._evidence.tomorrow_historical_risk_rows(SCORE_H0_V1_SPEC))
        return build_historical_risk_probability(rows, self._predictor)


def evaluate_historical_selected_days(
    rows: tuple[TomorrowHistoricalRiskRow, ...],
    utilities: tuple[float, ...],
) -> tuple[HistoricalSelectedDay, ...]:
    """Evaluate every valid historical day, treating a legal empty selection as cash."""

    if not rows or len(rows) != len(utilities) or any(not math.isfinite(value) for value in utilities):
        raise ValueError("Historical portfolio inputs must be finite and paired")
    if len({(row.trade_date, row.code) for row in rows}) != len(rows):
        raise ValueError("Historical portfolio rows must be unique by date and code")
    grouped: dict[date, list[tuple[TomorrowHistoricalRiskRow, float]]] = defaultdict(list)
    for row, utility in zip(rows, utilities, strict=True):
        grouped[row.trade_date].append((row, utility))
    previous: dict[str, float] = {"__cash__": 1.0}
    result: list[HistoricalSelectedDay] = []
    for trade_date in sorted(grouped):
        population = tuple(sorted(grouped[trade_date], key=lambda item: item[0].code))
        benchmarks = {row.benchmark_return for row, _utility in population}
        if len(benchmarks) != 1:
            raise ValueError("Historical rows must share one same-day benchmark")
        selected: list[tuple[TomorrowHistoricalRiskRow, float]] = []
        boards: Counter[str] = Counter()
        for row, utility in sorted(population, key=lambda item: (-item[1], item[0].code)):
            if utility <= 0.0 or boards[row.board] >= 3:
                continue
            selected.append((row, utility))
            boards[row.board] += 1
            if len(selected) == 6:
                break
        weight = 1.0 / len(selected) if selected else 0.0
        current = {row.code: weight for row, _utility in selected}
        if not current:
            current = {"__cash__": 1.0}
        turnover = 0.5 * math.fsum(
            abs(current.get(code, 0.0) - previous.get(code, 0.0)) for code in set(current) | set(previous)
        )
        gross = math.fsum(weight * row.gross_return for row, _utility in selected)
        benchmark = next(iter(benchmarks))
        result.append(
            HistoricalSelectedDay(
                trade_date=trade_date,
                status="invested" if selected else "cash",
                selected_codes=tuple(row.code for row, _utility in selected),
                gross_return=gross,
                benchmark_return=benchmark,
                turnover=turnover,
                net_excess_return_20bp=gross - benchmark - turnover * _PRIMARY_COST,
            )
        )
        previous = current
    return tuple(result)


def evaluate_historical_risk_probability(
    rows: tuple[TomorrowHistoricalRiskRow, ...],
    predictor: TomorrowModelPredictorPort,
    spec: HistoricalRiskValidationSpec = HISTORICAL_RISK_VALIDATION_SPEC,
) -> HistoricalRiskValidationReport:
    return build_historical_risk_probability(rows, predictor, spec).report


def build_historical_risk_probability(
    rows: tuple[TomorrowHistoricalRiskRow, ...],
    predictor: TomorrowModelPredictorPort,
    spec: HistoricalRiskValidationSpec = HISTORICAL_RISK_VALIDATION_SPEC,
) -> HistoricalRiskValidationOutcome:
    """Fit, calibrate, and test severe-loss probability on sealed historical dates only."""

    ordered = tuple(
        sorted(
            row
            for row in rows
            if SCORE_H0_V1_SPEC.validation_start <= row.trade_date <= SCORE_H0_V1_SPEC.validation_end
        )
    )
    if len({(row.trade_date, row.code) for row in ordered}) != len(ordered):
        raise ValueError("Historical risk rows must be unique by date and code")
    dates = tuple(sorted({row.trade_date for row in ordered}))
    if len(dates) < spec.required_trade_dates:
        return HistoricalRiskValidationOutcome(_insufficient_report(predictor, ordered, spec), None)
    selected_dates = dates[: spec.required_trade_dates]
    train_dates = frozenset(selected_dates[: spec.training_trade_dates])
    first_embargo = spec.training_trade_dates
    calibration_start = first_embargo + spec.embargo_trade_dates_per_boundary
    calibration_end = calibration_start + spec.calibration_trade_dates
    calibration_dates = frozenset(selected_dates[calibration_start:calibration_end])
    test_start = calibration_end + spec.embargo_trade_dates_per_boundary
    test_dates = frozenset(selected_dates[test_start : test_start + spec.test_trade_dates])
    included = tuple(row for row in ordered if row.trade_date in train_dates | calibration_dates | test_dates)
    feature_rows, targets = _risk_features(included, predictor, spec)
    training = tuple((row, target) for row, target in zip(feature_rows, targets, strict=True) if row[0] in train_dates)
    calibration = tuple(
        (row, target) for row, target in zip(feature_rows, targets, strict=True) if row[0] in calibration_dates
    )
    test = tuple((row, target) for row, target in zip(feature_rows, targets, strict=True) if row[0] in test_dates)
    if not training or not calibration or not test:
        return HistoricalRiskValidationOutcome(
            _insufficient_report(predictor, included, spec, "historical_split_rows_missing"),
            None,
        )
    train_matrix = tuple(item[0][1] for item in training)
    train_targets = tuple(item[1] for item in training)
    model = fit_logistic_model(train_matrix, train_targets, ridge=1e-3)
    calibration_raw = model.predict(tuple(item[0][1] for item in calibration))
    calibrator = fit_platt_calibrator(calibration_raw, tuple(item[1] for item in calibration))
    probabilities = calibrator.predict(model.predict(tuple(item[0][1] for item in test)))
    test_targets = tuple(item[1] for item in test)
    brier = _brier(probabilities, test_targets)
    prevalence = (math.fsum(train_targets) + 1.0) / (len(train_targets) + 2.0)
    baseline = _brier((prevalence,) * len(test_targets), test_targets)
    ece = _expected_calibration_error(probabilities, test_targets, spec.calibration_bins)
    reasons = tuple(
        reason
        for failed, reason in (
            (brier >= baseline, "brier_not_better_than_historical_baseline"),
            (ece > spec.maximum_expected_calibration_error, "expected_calibration_error_above_limit"),
        )
        if failed
    )
    artifact = HistoricalRiskModelArtifact(
        spec_hash=spec.content_hash,
        source_spec_hash=SCORE_H0_V1_SPEC.content_hash,
        parent_model_id=predictor.model_id,
        parent_model_hash=predictor.model_hash,
        feature_ids=(
            "net_predicted_excess_20bp",
            "model_disagreement",
            "signal_score",
            "atr20_pct",
            "estimated_cost",
        ),
        logistic_intercept=model.intercept,
        logistic_coefficients=model.coefficients,
        platt_intercept=calibrator.intercept,
        platt_slope=calibrator.slope,
        platt_constant=calibrator.constant,
        training_evidence_hash=canonical_hash(training),
        calibration_evidence_hash=canonical_hash(calibration),
    )
    report = HistoricalRiskValidationReport(
        spec_hash=spec.content_hash,
        model_id=predictor.model_id,
        model_hash=predictor.model_hash,
        evidence_hash=canonical_hash(included),
        training_trade_dates=len(train_dates),
        calibration_trade_dates=len(calibration_dates),
        test_trade_dates=len(test_dates),
        embargo_trade_dates=2 * spec.embargo_trade_dates_per_boundary,
        training_rows=len(training),
        calibration_rows=len(calibration),
        test_rows=len(test),
        brier_score=brier,
        baseline_brier_score=baseline,
        expected_calibration_error=ece,
        model_artifact_hash=artifact.content_hash,
        status="historical_rejected" if reasons else "historical_validated",
        failure_reasons=reasons,
    )
    return HistoricalRiskValidationOutcome(report, artifact)


def _risk_features(
    rows: tuple[TomorrowHistoricalRiskRow, ...],
    predictor: TomorrowModelPredictorPort,
    spec: HistoricalRiskValidationSpec,
) -> tuple[tuple[tuple[date, tuple[float, ...]], ...], tuple[float, ...]]:
    positions = tuple(_MODEL_FEATURE_IDS.index(item) for item in predictor.feature_ids)
    inputs = tuple(
        TomorrowModelInput(row.code, tuple(row.alpha_features[position] for position in positions)) for row in rows
    )
    predictions = predictor.predict(inputs)
    if tuple(item.code for item in predictions) != tuple(item.code for item in inputs):
        raise ValueError("Historical risk predictor returned mismatched rows")
    grouped: dict[date, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[row.trade_date].append(index)
    costs = [0.0] * len(rows)
    scores = [0.0] * len(rows)
    for indices in grouped.values():
        ranks = _percentile_ranks(tuple(rows[index].amihud_20d for index in indices))
        utilities = tuple(
            predictions[index].predicted_excess_return - _PRIMARY_COST * (1.0 + rank)
            for index, rank in zip(indices, ranks, strict=True)
        )
        utility_scores = _positive_utility_scores(utilities)
        for index, rank, score in zip(indices, ranks, utility_scores, strict=True):
            costs[index] = _PRIMARY_COST * (1.0 + rank)
            scores[index] = score
    features = tuple(
        (
            row.trade_date,
            (
                prediction.predicted_excess_return - costs[index],
                prediction.model_disagreement,
                scores[index] / 100.0,
                row.atr20_pct,
                costs[index],
            ),
        )
        for index, (row, prediction) in enumerate(zip(rows, predictions, strict=True))
    )
    targets = tuple(1.0 if row.mae_atr20 <= spec.severe_loss_mae_atr20 else 0.0 for row in rows)
    return features, targets


def _insufficient_report(
    predictor: TomorrowModelPredictorPort,
    rows: tuple[TomorrowHistoricalRiskRow, ...],
    spec: HistoricalRiskValidationSpec,
    reason: str | None = None,
) -> HistoricalRiskValidationReport:
    return HistoricalRiskValidationReport(
        spec_hash=spec.content_hash,
        model_id=predictor.model_id,
        model_hash=predictor.model_hash,
        evidence_hash=canonical_hash(rows),
        training_trade_dates=0,
        calibration_trade_dates=0,
        test_trade_dates=0,
        embargo_trade_dates=0,
        training_rows=0,
        calibration_rows=0,
        test_rows=0,
        brier_score=None,
        baseline_brier_score=None,
        expected_calibration_error=None,
        model_artifact_hash=None,
        status="historical_data_insufficient",
        failure_reasons=(reason or f"historical_trade_dates_below_{spec.required_trade_dates}",),
    )


def _percentile_ranks(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) <= 1:
        return (0.0,) * len(values)
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result = [0.0] * len(values)
    for position, index in enumerate(order):
        result[index] = position / (len(values) - 1)
    return tuple(result)


def _positive_utility_scores(values: tuple[float, ...]) -> tuple[float, ...]:
    positive = tuple(index for index, value in enumerate(values) if value > 0.0)
    result = [0.0] * len(values)
    if not positive:
        return tuple(result)
    order = sorted(positive, key=lambda index: (values[index], index))
    for position, index in enumerate(order):
        result[index] = 100.0 if len(order) == 1 else 100.0 * position / (len(order) - 1)
    return tuple(result)


def _brier(probabilities: tuple[float, ...], targets: tuple[float, ...]) -> float:
    return math.fsum(
        (probability - target) ** 2 for probability, target in zip(probabilities, targets, strict=True)
    ) / len(targets)


def _expected_calibration_error(probabilities: tuple[float, ...], targets: tuple[float, ...], bins: int) -> float:
    total = len(probabilities)
    error = 0.0
    for bucket in range(bins):
        lower = bucket / bins
        upper = (bucket + 1) / bins
        indices = tuple(
            index
            for index, value in enumerate(probabilities)
            if lower <= value < upper or (bucket == bins - 1 and value == 1.0)
        )
        if not indices:
            continue
        confidence = math.fsum(probabilities[index] for index in indices) / len(indices)
        observed = math.fsum(targets[index] for index in indices) / len(indices)
        error += len(indices) / total * abs(confidence - observed)
    return error


__all__ = [
    "HISTORICAL_RISK_VALIDATION_SPEC",
    "HistoricalRiskEvidence",
    "HistoricalRiskModelArtifact",
    "HistoricalRiskValidationOutcome",
    "HistoricalRiskValidationReport",
    "HistoricalRiskValidationSpec",
    "HistoricalRiskValidationService",
    "HistoricalSelectedDay",
    "TomorrowHistoricalRiskRow",
    "build_historical_risk_probability",
    "evaluate_historical_risk_probability",
    "evaluate_historical_selected_days",
]

"""Deterministic five-candidate C3 OOF development and freezing."""

from __future__ import annotations

import dataclasses
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.application.research.replay_models import canonical_hash
from trader.application.research.tomorrow_daily_close_training import (
    BaseModelKind,
    CandidateModelArtifact,
    DailyCloseFeatureRow,
    FeatureDataset,
    ModelDependencyVersion,
    StockResidualCorrection,
    StratumCorrection,
    select_mature_fold_training_rows,
    select_mature_training_rows,
)
from trader.domain.research.tomorrow_daily_close import (
    DailyCloseTemporalSplit,
    ExpandingWalkForwardFold,
    build_expanding_walk_forward,
)

_CANDIDATE_IDS = (
    "ridge_v1",
    "lightgbm_v1",
    "ridge_lightgbm_ensemble_v1",
    "ensemble_stratum_residual_v1",
    "ensemble_stratum_stock_residual_v1",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FittedBaseModels:
    preprocessing_means: tuple[float, ...]
    preprocessing_scales: tuple[float, ...]
    ridge_intercept: float
    ridge_coefficients: tuple[float, ...]
    lightgbm_model_text: str
    lightgbm_best_iteration: int
    dependency_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        width = len(self.preprocessing_means)
        if width < 1 or len(self.preprocessing_scales) != width or len(self.ridge_coefficients) != width:
            raise ValueError("C3 fitted model vector widths are inconsistent")
        numeric = (
            *self.preprocessing_means,
            *self.preprocessing_scales,
            self.ridge_intercept,
            *self.ridge_coefficients,
        )
        if not all(math.isfinite(value) for value in numeric) or any(
            value <= 0.0 for value in self.preprocessing_scales
        ):
            raise ValueError("C3 fitted model parameters are invalid")
        if not self.lightgbm_model_text.strip() or self.lightgbm_best_iteration < 1 or not self.dependency_versions:
            raise ValueError("C3 fitted model dependencies are incomplete")


class C3BaseModelFitPort(Protocol):
    def fit(self, training_rows: tuple[DailyCloseFeatureRow, ...], *, feature_count: int) -> FittedBaseModels: ...

    def predict(
        self,
        fitted: FittedBaseModels,
        rows: tuple[DailyCloseFeatureRow, ...],
    ) -> tuple[tuple[float, ...], tuple[float, ...]]: ...


@dataclass(frozen=True)
class C3OOFPrediction:
    candidate_id: str
    fold_index: int
    trade_date: date
    code: str
    board: str
    predicted_net_excess_return: float
    actual_net_excess_returns: tuple[float, float, float]
    schema_version: str = "tomorrow_c3_oof_prediction_v1"
    production_authority: bool = False

    def __post_init__(self) -> None:
        if self.candidate_id not in _CANDIDATE_IDS or not 1 <= self.fold_index <= 5:
            raise ValueError("C3 OOF identity is invalid")
        if not math.isfinite(self.predicted_net_excess_return) or not all(
            math.isfinite(value) for value in self.actual_net_excess_returns
        ):
            raise ValueError("C3 OOF values must be finite")
        if self.schema_version != "tomorrow_c3_oof_prediction_v1" or self.production_authority:
            raise ValueError("C3 OOF schema is invalid")


@dataclass(frozen=True)
class C3CandidateMetrics:
    evaluated_trade_dates: int
    evaluated_rows: int
    mean_portfolio_net_excess_20bp: float
    mean_portfolio_net_excess_50bp: float
    positive_fold_count: int
    severe_loss_rate: float
    rank_direction_rate: float

    def __post_init__(self) -> None:
        values = (
            self.mean_portfolio_net_excess_20bp,
            self.mean_portfolio_net_excess_50bp,
            self.severe_loss_rate,
            self.rank_direction_rate,
        )
        if min(self.evaluated_trade_dates, self.evaluated_rows, self.positive_fold_count) < 0:
            raise ValueError("C3 candidate metric counts are invalid")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("C3 candidate metrics must be finite")
        if not 0.0 <= self.severe_loss_rate <= 1.0 or not 0.0 <= self.rank_direction_rate <= 1.0:
            raise ValueError("C3 candidate rates are invalid")


@dataclass(frozen=True)
class C3CandidateOOF:
    candidate_id: str
    predictions: tuple[C3OOFPrediction, ...]
    metrics: C3CandidateMetrics
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.candidate_id not in _CANDIDATE_IDS or any(
            item.candidate_id != self.candidate_id for item in self.predictions
        ):
            raise ValueError("C3 candidate OOF identity is invalid")
        identities = tuple((item.fold_index, item.trade_date, item.code) for item in self.predictions)
        if len(identities) != len(set(identities)):
            raise ValueError("C3 candidate OOF identities must be unique")
        if self.production_authority:
            raise ValueError("C3 candidate OOF cannot authorize production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class C3DevelopmentResult:
    dataset_manifest_hash: str
    temporal_split_hash: str
    candidates: tuple[C3CandidateOOF, ...]
    selected_candidate_id: str
    schema_version: str = "tomorrow_c3_development_result_v1"
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.dataset_manifest_hash, "dataset manifest")
        _hash(self.temporal_split_hash, "temporal split")
        if tuple(item.candidate_id for item in self.candidates) != _CANDIDATE_IDS:
            raise ValueError("C3 development requires the fixed five-candidate family")
        if self.selected_candidate_id not in _CANDIDATE_IDS:
            raise ValueError("C3 selected candidate is invalid")
        if self.schema_version != "tomorrow_c3_development_result_v1":
            raise ValueError("C3 development schema is invalid")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("C3 development cannot open holdout or production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


class C3CandidateEvaluator:
    def evaluate(self, predictions: tuple[C3OOFPrediction, ...]) -> C3CandidateMetrics:
        if not predictions:
            raise ValueError("C3 evaluation requires OOF predictions")
        by_day: dict[date, list[C3OOFPrediction]] = defaultdict(list)
        for prediction in predictions:
            by_day[prediction.trade_date].append(prediction)
        portfolio_20: list[float] = []
        portfolio_50: list[float] = []
        severe: list[bool] = []
        direction: list[bool] = []
        fold_values: dict[int, list[float]] = defaultdict(list)
        for day in sorted(by_day):
            selected = sorted(by_day[day], key=lambda item: (-item.predicted_net_excess_return, item.code))[:6]
            daily_20 = math.fsum(item.actual_net_excess_returns[0] for item in selected) / len(selected)
            daily_50 = math.fsum(item.actual_net_excess_returns[1] for item in selected) / len(selected)
            portfolio_20.append(daily_20)
            portfolio_50.append(daily_50)
            fold_values[selected[0].fold_index].append(daily_20)
            severe.extend(item.actual_net_excess_returns[0] <= -0.05 for item in selected)
            direction.extend(
                item.predicted_net_excess_return * item.actual_net_excess_returns[0] > 0.0 for item in by_day[day]
            )
        return C3CandidateMetrics(
            evaluated_trade_dates=len(by_day),
            evaluated_rows=len(predictions),
            mean_portfolio_net_excess_20bp=math.fsum(portfolio_20) / len(portfolio_20),
            mean_portfolio_net_excess_50bp=math.fsum(portfolio_50) / len(portfolio_50),
            positive_fold_count=sum(math.fsum(values) / len(values) > 0.0 for values in fold_values.values()),
            severe_loss_rate=sum(severe) / len(severe),
            rank_direction_rate=sum(direction) / len(direction),
        )

    def select(self, candidates: tuple[C3CandidateOOF, ...]) -> str:
        if tuple(item.candidate_id for item in candidates) != _CANDIDATE_IDS:
            raise ValueError("C3 selection requires the preregistered family")
        eligible = tuple(item for item in candidates if item.metrics.positive_fold_count == 5)
        pool = eligible or candidates
        return max(
            pool,
            key=lambda item: (
                item.metrics.mean_portfolio_net_excess_20bp,
                item.metrics.mean_portfolio_net_excess_50bp,
                -item.metrics.severe_loss_rate,
                -_CANDIDATE_IDS.index(item.candidate_id),
            ),
        ).candidate_id


class C3CandidateTrainer:
    def __init__(self, fit_port: C3BaseModelFitPort, evaluator: C3CandidateEvaluator) -> None:
        self._fit_port = fit_port
        self._evaluator = evaluator

    def develop(self, dataset: FeatureDataset, split: DailyCloseTemporalSplit) -> C3DevelopmentResult:
        _validate_dataset_split(dataset, split)
        predictions: dict[str, list[C3OOFPrediction]] = {candidate_id: [] for candidate_id in _CANDIDATE_IDS}
        base_oof: list[C3OOFPrediction] = []
        stratum_oof: list[C3OOFPrediction] = []
        for fold in build_expanding_walk_forward(split.development_dates):
            training_rows = select_mature_fold_training_rows(dataset, fold)
            validation_rows = _rows_for_dates(dataset, fold.validation_dates)
            if not training_rows or not validation_rows:
                raise ValueError("C3 fold lacks mature training or validation rows")
            fitted = self._fit_port.fit(training_rows, feature_count=len(dataset.manifest.feature_names))
            ridge, lightgbm = self._fit_port.predict(fitted, validation_rows)
            if len(ridge) != len(validation_rows) or len(lightgbm) != len(validation_rows):
                raise ValueError("C3 fit port prediction width is invalid")
            base = tuple((left + right) / 2.0 for left, right in zip(ridge, lightgbm, strict=True))
            strata = _fit_oof_board_corrections(tuple(base_oof))
            stratum = _apply_board_corrections(validation_rows, base, strata)
            stocks = _fit_oof_stock_corrections(tuple(stratum_oof))
            stock = _apply_stock_corrections(validation_rows, stratum, stocks)
            values = (ridge, lightgbm, base, stratum, stock)
            for candidate_id, candidate_values in zip(_CANDIDATE_IDS, values, strict=True):
                predictions[candidate_id].extend(_oof_rows(candidate_id, fold, validation_rows, candidate_values))
            base_oof.extend(_oof_rows("ridge_lightgbm_ensemble_v1", fold, validation_rows, base))
            stratum_oof.extend(_oof_rows("ensemble_stratum_residual_v1", fold, validation_rows, stratum))
        candidates = tuple(
            C3CandidateOOF(
                candidate_id,
                tuple(predictions[candidate_id]),
                self._evaluator.evaluate(tuple(predictions[candidate_id])),
            )
            for candidate_id in _CANDIDATE_IDS
        )
        return C3DevelopmentResult(
            dataset.manifest.content_hash,
            split.content_hash,
            candidates,
            self._evaluator.select(candidates),
        )

    def freeze(
        self,
        dataset: FeatureDataset,
        split: DailyCloseTemporalSplit,
        development: C3DevelopmentResult,
        *,
        confirmation_report_hash: str,
    ) -> CandidateModelArtifact:
        _validate_dataset_split(dataset, split)
        _hash(confirmation_report_hash, "confirmation report")
        if (
            development.dataset_manifest_hash != dataset.manifest.content_hash
            or development.temporal_split_hash != split.content_hash
        ):
            raise ValueError("C3 development parents do not match freeze inputs")
        training_rows = select_mature_training_rows(
            dataset,
            split.development_dates,
            prediction_start=split.confirmation_dates[0],
        )
        fitted = self._fit_port.fit(training_rows, feature_count=len(dataset.manifest.feature_names))
        selected = development.selected_candidate_id
        candidate_by_id = {item.candidate_id: item for item in development.candidates}
        base_oof = candidate_by_id["ridge_lightgbm_ensemble_v1"].predictions
        stratum_oof = candidate_by_id["ensemble_stratum_residual_v1"].predictions
        strata = _fit_oof_board_corrections(base_oof) if "stratum" in selected else ()
        stocks = _fit_oof_stock_corrections(stratum_oof) if "stock" in selected else ()
        kind: BaseModelKind = (
            "ridge"
            if selected == "ridge_v1"
            else "lightgbm"
            if selected == "lightgbm_v1"
            else "ridge_lightgbm_ensemble"
        )
        return CandidateModelArtifact(
            model_id="score_tomorrow_daily_close_c3_v1",
            candidate_id=selected,
            base_model_kind=kind,
            manifest_hash=dataset.manifest.content_hash,
            filter_spec_hash=dataset.manifest.filter_spec_hash,
            confirmation_report_hash=confirmation_report_hash,
            feature_names=dataset.manifest.feature_names,
            feature_units=dataset.manifest.feature_units,
            preprocessing_means=fitted.preprocessing_means,
            preprocessing_scales=fitted.preprocessing_scales,
            ridge_intercept=None if kind == "lightgbm" else fitted.ridge_intercept,
            ridge_coefficients=None if kind == "lightgbm" else fitted.ridge_coefficients,
            lightgbm_model_text=None if kind == "ridge" else fitted.lightgbm_model_text,
            lightgbm_best_iteration=None if kind == "ridge" else fitted.lightgbm_best_iteration,
            stratum_corrections=strata,
            stock_residual_corrections=stocks,
            trained_from=min(row.trade_date for row in training_rows),
            trained_through=max(row.trade_date for row in training_rows),
            dependencies=tuple(ModelDependencyVersion(name, version) for name, version in fitted.dependency_versions),
        )


def _validate_dataset_split(dataset: FeatureDataset, split: DailyCloseTemporalSplit) -> None:
    if dataset.manifest.trading_dates != split.all_dates:
        raise ValueError("C3 split must bind the complete dataset date identity")
    if set(split.point_in_time_reserved_dates).intersection(split.daily_close_dates):
        raise ValueError("C3 daily-close dates cannot open point-in-time reserve")


def _rows_for_dates(dataset: FeatureDataset, dates: tuple[date, ...]) -> tuple[DailyCloseFeatureRow, ...]:
    allowed = set(dates)
    return tuple(row for row in dataset.rows if row.trade_date in allowed)


def _fit_oof_board_corrections(predictions: tuple[C3OOFPrediction, ...]) -> tuple[StratumCorrection, ...]:
    residuals: dict[str, list[float]] = defaultdict(list)
    for prediction in predictions:
        residuals[prediction.board].append(
            prediction.actual_net_excess_returns[0] - prediction.predicted_net_excess_return
        )
    return tuple(
        StratumCorrection("board", board, len(values), 50, 500.0, math.fsum(values) / (len(values) + 500.0))
        for board, values in sorted(residuals.items())
        if len(values) >= 50
    )


def _fit_oof_stock_corrections(
    predictions: tuple[C3OOFPrediction, ...],
) -> tuple[StockResidualCorrection, ...]:
    grouped: dict[str, list[tuple[date, float, float]]] = defaultdict(list)
    for prediction in predictions:
        grouped[prediction.code].append(
            (
                prediction.trade_date,
                prediction.actual_net_excess_returns[0] - prediction.predicted_net_excess_return,
                prediction.predicted_net_excess_return,
            )
        )
    result: list[StockResidualCorrection] = []
    cross_section_stddev = _stddev(tuple(item.predicted_net_excess_return for item in predictions))
    if cross_section_stddev <= 0.0:
        return ()
    for code, values in sorted(grouped.items()):
        if len(values) < 250 or len({item[0] for item in values}) < 120:
            continue
        correction = math.fsum(item[1] for item in values) / (len(values) + 1_000.0)
        cap = cross_section_stddev * 0.10
        result.append(
            StockResidualCorrection(
                code,
                len(values),
                len({item[0] for item in values}),
                1_000.0,
                cross_section_stddev,
                max(-cap, min(cap, correction)),
            )
        )
    return tuple(result)


def _apply_board_corrections(
    rows: tuple[DailyCloseFeatureRow, ...], predictions: tuple[float, ...], corrections: tuple[StratumCorrection, ...]
) -> tuple[float, ...]:
    by_board = {item.key: item.correction for item in corrections}
    return tuple(value + by_board.get(row.board, 0.0) for row, value in zip(rows, predictions, strict=True))


def _apply_stock_corrections(
    rows: tuple[DailyCloseFeatureRow, ...],
    predictions: tuple[float, ...],
    corrections: tuple[StockResidualCorrection, ...],
) -> tuple[float, ...]:
    by_code = {item.code: item.correction for item in corrections}
    return tuple(value + by_code.get(row.code, 0.0) for row, value in zip(rows, predictions, strict=True))


def _oof_rows(
    candidate_id: str,
    fold: ExpandingWalkForwardFold,
    rows: tuple[DailyCloseFeatureRow, ...],
    predictions: tuple[float, ...],
) -> tuple[C3OOFPrediction, ...]:
    return tuple(
        C3OOFPrediction(
            candidate_id,
            fold.index,
            row.trade_date,
            row.code,
            row.board,
            prediction,
            row.net_excess_returns,
        )
        for row, prediction in zip(rows, predictions, strict=True)
    )


def _stddev(values: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    mean = math.fsum(values) / len(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / len(values))


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"C3 {label} identity must be SHA-256")


__all__ = [
    "C3BaseModelFitPort",
    "C3CandidateEvaluator",
    "C3CandidateTrainer",
    "C3DevelopmentResult",
    "FittedBaseModels",
]

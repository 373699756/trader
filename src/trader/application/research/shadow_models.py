"""Walk-forward orchestration for offline Tomorrow shadow models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import fmean, pstdev

from trader.application.research.shadow_model_models import (
    SHADOW_CALIBRATION_DATES,
    SHADOW_COST_BPS,
    SHADOW_EMBARGO_DATES,
    SHADOW_MIN_TRAIN_DATES,
    SHADOW_RANDOM_SEED,
    SHADOW_ROLLING_DATES,
    SHADOW_VALIDATION_DATES,
    ShadowFoldRecord,
    ShadowHorizon,
    ShadowLabeledDay,
    ShadowModelReport,
    ShadowPrediction,
    ShadowWindowMode,
)
from trader.application.research.shadow_model_ports import ShadowFitRequest, ShadowModelFamily, ShadowModelTrainer
from trader.domain.research.historical import CostSettlementBasis
from trader.domain.research.shadow_calibration import fit_affine_calibrator, fit_platt_calibrator
from trader.domain.research.tomorrow_features import TOMORROW_FEATURE_NAMES, TomorrowStockFeatures

_COST_RATE = SHADOW_COST_BPS / 10_000.0
_SEVERE_THRESHOLD = -1.5


@dataclass(frozen=True)
class _Observation:
    trade_date: date
    feature_batch_hash: str
    row: TomorrowStockFeatures
    settlement: CostSettlementBasis


@dataclass(frozen=True)
class _Transformer:
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def transform(self, observations: tuple[_Observation, ...]) -> tuple[tuple[float, ...], ...]:
        result: list[tuple[float, ...]] = []
        for observation in observations:
            values = tuple(item.value for item in observation.row.values)
            standardized = tuple(
                0.0 if value is None else (value - self.means[index]) / self.scales[index]
                for index, value in enumerate(values)
            )
            missing = tuple(1.0 if value is None else 0.0 for value in values)
            result.append((*standardized, *missing))
        return tuple(result)


@dataclass(frozen=True)
class _FamilyOutput:
    net_model_hash: str
    severe_model_hash: str
    calibrated_net: tuple[float, ...]
    calibrated_severe: tuple[float, ...]


@dataclass(frozen=True)
class _FoldDays:
    horizon: ShadowHorizon
    window_mode: ShadowWindowMode
    train: tuple[ShadowLabeledDay, ...]
    validation: tuple[ShadowLabeledDay, ...]
    calibration: tuple[ShadowLabeledDay, ...]
    prediction: ShadowLabeledDay


class ScoreTomorrowShadowModels:
    """Fit fixed linear/LightGBM challengers on chronological research evidence."""

    def __init__(self, trainers: tuple[ShadowModelTrainer, ...]) -> None:
        ordered = tuple(sorted(trainers, key=lambda item: item.model_family))
        if tuple(item.model_family for item in ordered) != ("lightgbm", "linear"):
            raise ValueError("shadow models require exactly one linear and one LightGBM trainer")
        self._trainers = ordered

    def build(self, days: tuple[ShadowLabeledDay, ...]) -> ShadowModelReport:
        ordered = _validate_days(days)
        folds: list[ShadowFoldRecord] = []
        predictions: list[ShadowPrediction] = []
        for horizon in ("tomorrow", "d25"):
            horizon_days = tuple(item for item in ordered if item.horizon == horizon)
            for window_mode in ("expanding", "rolling"):
                for index, prediction_day in enumerate(horizon_days):
                    eligible = _eligible_days(horizon_days, index, horizon, prediction_day.features.trade_date)
                    split = _split_days(eligible, window_mode)
                    if split is None:
                        continue
                    train_days, validation_days, calibration_days = split
                    fold_records, fold_predictions = self._fit_fold(
                        _FoldDays(
                            horizon,
                            window_mode,
                            train_days,
                            validation_days,
                            calibration_days,
                            prediction_day,
                        )
                    )
                    folds.extend(fold_records)
                    predictions.extend(fold_predictions)
        return ShadowModelReport(
            training_window_start=min(item.features.trade_date for item in ordered),
            training_window_end=max(item.features.trade_date for item in ordered),
            folds=tuple(folds),
            predictions=tuple(predictions),
        )

    def _fit_fold(
        self,
        fold: _FoldDays,
    ) -> tuple[tuple[ShadowFoldRecord, ...], tuple[ShadowPrediction, ...]]:
        train = _observations(fold.train)
        validation = _observations(fold.validation)
        calibration = _observations(fold.calibration)
        prediction = _observations((fold.prediction,))
        transformer = _fit_transformer(train)
        transformed_names = (*TOMORROW_FEATURE_NAMES, *(f"{name}__missing" for name in TOMORROW_FEATURE_NAMES))
        train_x = transformer.transform(train)
        validation_x = transformer.transform(validation)
        calibration_x = transformer.transform(calibration)
        prediction_x = transformer.transform(prediction)
        net_train = _net_targets(train)
        net_validation = _net_targets(validation)
        net_calibration = _net_targets(calibration)
        severe_train = _severe_targets(train)
        severe_validation = _severe_targets(validation)
        severe_calibration = _severe_targets(calibration)
        outputs: dict[ShadowModelFamily, _FamilyOutput] = {}
        records: list[ShadowFoldRecord] = []
        for trainer in self._trainers:
            net_result = trainer.fit_predict(
                ShadowFitRequest(
                    objective="net_excess",
                    feature_names=transformed_names,
                    train_x=train_x,
                    train_y=net_train,
                    validation_x=validation_x,
                    validation_y=net_validation,
                    calibration_x=calibration_x,
                    prediction_x=prediction_x,
                    seed=SHADOW_RANDOM_SEED,
                )
            )
            severe_result = trainer.fit_predict(
                ShadowFitRequest(
                    objective="severe_loss",
                    feature_names=transformed_names,
                    train_x=train_x,
                    train_y=severe_train,
                    validation_x=validation_x,
                    validation_y=severe_validation,
                    calibration_x=calibration_x,
                    prediction_x=prediction_x,
                    seed=SHADOW_RANDOM_SEED,
                )
            )
            if net_result.model_family != trainer.model_family or severe_result.model_family != trainer.model_family:
                raise ValueError("shadow trainer returned the wrong model family")
            if len(net_result.calibration_predictions) != len(calibration) or len(
                net_result.prediction_predictions
            ) != len(prediction):
                raise ValueError("shadow net prediction coverage is incomplete")
            if len(severe_result.calibration_predictions) != len(calibration) or len(
                severe_result.prediction_predictions
            ) != len(prediction):
                raise ValueError("shadow severe prediction coverage is incomplete")
            calibrated_net = fit_affine_calibrator(net_result.calibration_predictions, net_calibration).predict(
                net_result.prediction_predictions
            )
            calibrated_severe = fit_platt_calibrator(
                severe_result.calibration_predictions,
                severe_calibration,
            ).predict(severe_result.prediction_predictions)
            outputs[trainer.model_family] = _FamilyOutput(
                net_result.model_hash,
                severe_result.model_hash,
                calibrated_net,
                calibrated_severe,
            )
            records.append(
                ShadowFoldRecord(
                    horizon=fold.horizon,
                    window_mode=fold.window_mode,
                    prediction_date=fold.prediction.features.trade_date,
                    model_family=trainer.model_family,
                    train_start=fold.train[0].features.trade_date,
                    train_end=fold.train[-1].features.trade_date,
                    validation_start=fold.validation[0].features.trade_date,
                    validation_end=fold.validation[-1].features.trade_date,
                    calibration_start=fold.calibration[0].features.trade_date,
                    calibration_end=fold.calibration[-1].features.trade_date,
                    train_date_count=len(fold.train),
                    net_model_hash=net_result.model_hash,
                    severe_model_hash=severe_result.model_hash,
                )
            )
        return tuple(records), _prediction_rows(fold.horizon, fold.window_mode, prediction, outputs)


def _validate_days(days: tuple[ShadowLabeledDay, ...]) -> tuple[ShadowLabeledDay, ...]:
    ordered = tuple(sorted(days, key=lambda item: (item.features.trade_date, item.horizon)))
    if not ordered or {item.horizon for item in ordered} != {"tomorrow", "d25"}:
        raise ValueError("shadow research requires Tomorrow and D25 labeled days")
    keys = tuple((item.horizon, item.features.trade_date) for item in ordered)
    if len(set(keys)) != len(keys):
        raise ValueError("shadow labeled dates must be unique per horizon")
    dates_by_horizon = {
        horizon: {item.features.trade_date for item in ordered if item.horizon == horizon}
        for horizon in ("tomorrow", "d25")
    }
    if dates_by_horizon["tomorrow"] != dates_by_horizon["d25"]:
        raise ValueError("Tomorrow and D25 must cover the same feature dates")
    hashes_by_date: dict[date, set[str]] = {}
    for item in ordered:
        hashes_by_date.setdefault(item.features.trade_date, set()).add(item.features.content_hash)
        for row in item.features.rows:
            if tuple(value.name for value in row.values) != TOMORROW_FEATURE_NAMES:
                raise ValueError("shadow feature schema does not match the frozen Tomorrow feature set")
    if any(len(hashes) != 1 for hashes in hashes_by_date.values()):
        raise ValueError("Tomorrow and D25 must share the same point-in-time feature batch")
    return ordered


def _eligible_days(
    days: tuple[ShadowLabeledDay, ...],
    index: int,
    horizon: ShadowHorizon,
    prediction_date: date,
) -> tuple[ShadowLabeledDay, ...]:
    embargo = dict(SHADOW_EMBARGO_DATES)[horizon]
    boundary = max(0, index - embargo)
    return tuple(
        item
        for item in days[:boundary]
        if all(settlement.basis.label_date < prediction_date for settlement in item.settlements)
    )


def _split_days(
    eligible: tuple[ShadowLabeledDay, ...],
    window_mode: ShadowWindowMode,
) -> tuple[tuple[ShadowLabeledDay, ...], tuple[ShadowLabeledDay, ...], tuple[ShadowLabeledDay, ...]] | None:
    held_out = SHADOW_VALIDATION_DATES + SHADOW_CALIBRATION_DATES
    if len(eligible) < SHADOW_MIN_TRAIN_DATES + held_out:
        return None
    calibration = eligible[-SHADOW_CALIBRATION_DATES:]
    validation = eligible[-held_out:-SHADOW_CALIBRATION_DATES]
    train = eligible[:-held_out]
    if window_mode == "rolling":
        train = train[-SHADOW_ROLLING_DATES:]
    return train, validation, calibration


def _observations(days: tuple[ShadowLabeledDay, ...]) -> tuple[_Observation, ...]:
    result: list[_Observation] = []
    for day in days:
        settlements = {item.basis.code: item.basis for item in day.settlements}
        result.extend(
            _Observation(day.features.trade_date, day.features.content_hash, row, settlements[row.code])
            for row in day.features.rows
        )
    return tuple(result)


def _fit_transformer(observations: tuple[_Observation, ...]) -> _Transformer:
    columns = tuple(
        tuple(item.row.values[index].value for item in observations) for index in range(len(TOMORROW_FEATURE_NAMES))
    )
    known = tuple(tuple(value for value in column if value is not None) for column in columns)
    means = tuple(fmean(column) if column else 0.0 for column in known)
    scales = tuple(pstdev(column) if len(column) > 1 and pstdev(column) > 0.0 else 1.0 for column in known)
    return _Transformer(means, scales)


def _net_targets(observations: tuple[_Observation, ...]) -> tuple[float, ...]:
    return tuple(item.settlement.gross_excess_return - item.settlement.turnover * _COST_RATE for item in observations)


def _severe_targets(observations: tuple[_Observation, ...]) -> tuple[float, ...]:
    return tuple(1.0 if item.settlement.mae_atr20 <= _SEVERE_THRESHOLD else 0.0 for item in observations)


def _prediction_rows(
    horizon: ShadowHorizon,
    window_mode: ShadowWindowMode,
    observations: tuple[_Observation, ...],
    outputs: dict[ShadowModelFamily, _FamilyOutput],
) -> tuple[ShadowPrediction, ...]:
    linear = outputs["linear"]
    lightgbm = outputs["lightgbm"]
    result: list[ShadowPrediction] = []
    for index, observation in enumerate(observations):
        actual_net = observation.settlement.gross_excess_return - observation.settlement.turnover * _COST_RATE
        uncertainty = abs(linear.calibrated_net[index] - lightgbm.calibrated_net[index])
        result.append(
            ShadowPrediction(
                prediction_date=observation.trade_date,
                code=observation.row.code,
                board=observation.row.board,
                industry=observation.row.industry,
                horizon=horizon,
                window_mode=window_mode,
                feature_batch_hash=observation.feature_batch_hash,
                estimated_cost=observation.settlement.turnover * _COST_RATE,
                actual_net_excess=actual_net,
                actual_severe_loss=observation.settlement.mae_atr20 <= _SEVERE_THRESHOLD,
                linear_net_excess=linear.calibrated_net[index],
                lightgbm_net_excess=lightgbm.calibrated_net[index],
                linear_severe_probability=linear.calibrated_severe[index],
                lightgbm_severe_probability=lightgbm.calibrated_severe[index],
                uncertainty=uncertainty,
            )
        )
    return tuple(result)


__all__ = ["ScoreTomorrowShadowModels"]

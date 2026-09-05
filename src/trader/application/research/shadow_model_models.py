"""Immutable identities and outputs for Tomorrow shadow model research."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.application.research.shadow_model_ports import ShadowModelFamily
from trader.application.research.tomorrow_feature_models import (
    TOMORROW_FEATURE_SCHEMA_VERSION,
    TomorrowPointInTimeFeatureBatch,
)
from trader.domain.research.historical import SUPPORTED_RESEARCH_BOARDS, CostSettlementBasis, ResearchBoard

ShadowHorizon = Literal["tomorrow", "d25"]
ShadowWindowMode = Literal["expanding", "rolling"]
SHADOW_REPORT_SCHEMA_VERSION = "score_tomorrow_shadow_report"
SHADOW_IMPLEMENTATION_VERSION = "score_tomorrow_shadow_models"
SHADOW_RANDOM_SEED = 20260828
SHADOW_COST_BPS = 20
SHADOW_MIN_TRAIN_DATES = 20
SHADOW_VALIDATION_DATES = 5
SHADOW_CALIBRATION_DATES = 10
SHADOW_ROLLING_DATES = 252
SHADOW_EMBARGO_DATES: tuple[tuple[ShadowHorizon, int], ...] = (("tomorrow", 1), ("d25", 25))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ShadowSettlementLabel:
    horizon: ShadowHorizon
    observation_lag: int
    basis: CostSettlementBasis

    def __post_init__(self) -> None:
        expected_lag = dict(SHADOW_EMBARGO_DATES).get(self.horizon)
        if expected_lag is None or self.observation_lag != expected_lag:
            raise ValueError("shadow settlement horizon and observation lag do not match")


@dataclass(frozen=True)
class ShadowLabeledDay:
    horizon: ShadowHorizon
    features: TomorrowPointInTimeFeatureBatch
    settlements: tuple[ShadowSettlementLabel, ...]

    def __post_init__(self) -> None:
        if self.horizon not in {"tomorrow", "d25"} or self.features.production_authority:
            raise ValueError("shadow labeled day identity is invalid")
        settlements = tuple(sorted(self.settlements, key=lambda item: item.basis.code))
        rows = {item.code: item for item in self.features.rows}
        if {item.basis.code for item in settlements} != set(rows) or len(settlements) != len(rows):
            raise ValueError("shadow settlements must exactly cover feature rows")
        if any(item.horizon != self.horizon for item in settlements):
            raise ValueError("shadow settlement horizon must match its labeled day")
        if any(item.basis.decision_date != self.features.trade_date for item in settlements):
            raise ValueError("shadow settlement decision date must match feature date")
        if any(item.basis.board != rows[item.basis.code].board for item in settlements):
            raise ValueError("shadow settlement board must match feature row")
        object.__setattr__(self, "settlements", settlements)


@dataclass(frozen=True)
class ShadowFoldRecord:
    horizon: ShadowHorizon
    window_mode: ShadowWindowMode
    prediction_date: date
    model_family: ShadowModelFamily
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    calibration_start: date
    calibration_end: date
    train_date_count: int
    net_model_hash: str
    severe_model_hash: str

    def __post_init__(self) -> None:
        if self.horizon not in {"tomorrow", "d25"} or self.window_mode not in {"expanding", "rolling"}:
            raise ValueError("shadow fold identity is invalid")
        if self.model_family not in {"linear", "lightgbm"}:
            raise ValueError("shadow fold model family is invalid")
        if not (
            self.train_start
            <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.calibration_start
            <= self.calibration_end
            < self.prediction_date
        ):
            raise ValueError("shadow fold windows must be strictly chronological")
        if self.train_date_count < SHADOW_MIN_TRAIN_DATES:
            raise ValueError("shadow fold training window is too short")
        if _SHA256.fullmatch(self.net_model_hash) is None or _SHA256.fullmatch(self.severe_model_hash) is None:
            raise ValueError("shadow fold model hashes must be SHA-256")


@dataclass(frozen=True)
class ShadowPrediction:
    prediction_date: date
    code: str
    board: ResearchBoard
    industry: str
    horizon: ShadowHorizon
    window_mode: ShadowWindowMode
    feature_batch_hash: str
    estimated_cost: float
    actual_net_excess: float
    actual_severe_loss: bool
    linear_net_excess: float
    lightgbm_net_excess: float
    linear_severe_probability: float
    lightgbm_severe_probability: float
    uncertainty: float

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or self.board not in SUPPORTED_RESEARCH_BOARDS
            or not self.industry.strip()
        ):
            raise ValueError("shadow prediction security identity is invalid")
        if self.horizon not in {"tomorrow", "d25"} or self.window_mode not in {"expanding", "rolling"}:
            raise ValueError("shadow prediction model identity is invalid")
        if _SHA256.fullmatch(self.feature_batch_hash) is None:
            raise ValueError("shadow prediction feature hash is invalid")
        numeric = (
            self.estimated_cost,
            self.actual_net_excess,
            self.linear_net_excess,
            self.lightgbm_net_excess,
            self.linear_severe_probability,
            self.lightgbm_severe_probability,
            self.uncertainty,
        )
        if any(not math.isfinite(value) for value in numeric) or self.estimated_cost < 0.0 or self.uncertainty < 0.0:
            raise ValueError("shadow prediction values are invalid")
        if not 0.0 <= self.linear_severe_probability <= 1.0 or not 0.0 <= self.lightgbm_severe_probability <= 1.0:
            raise ValueError("shadow severe probabilities must be in [0, 1]")
        object.__setattr__(self, "industry", self.industry.strip())


@dataclass(frozen=True)
class ShadowModelReport:
    training_window_start: date
    training_window_end: date
    folds: tuple[ShadowFoldRecord, ...]
    predictions: tuple[ShadowPrediction, ...]
    spec_hash: str = field(default_factory=lambda: shadow_spec_hash())
    feature_version: str = TOMORROW_FEATURE_SCHEMA_VERSION
    random_seed: int = SHADOW_RANDOM_SEED
    cost_bps: int = SHADOW_COST_BPS
    status: Literal["exploratory"] = "exploratory"
    production_authority: bool = False
    schema_version: str = SHADOW_REPORT_SCHEMA_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.training_window_start > self.training_window_end or self.spec_hash != shadow_spec_hash():
            raise ValueError("shadow report training identity is invalid")
        if (
            self.feature_version != TOMORROW_FEATURE_SCHEMA_VERSION
            or self.random_seed != SHADOW_RANDOM_SEED
            or self.cost_bps != SHADOW_COST_BPS
            or self.status != "exploratory"
            or self.production_authority
            or self.schema_version != SHADOW_REPORT_SCHEMA_VERSION
        ):
            raise ValueError("shadow report fixed contract is invalid")
        folds = tuple(sorted(self.folds, key=_fold_key))
        predictions = tuple(sorted(self.predictions, key=_prediction_key))
        if not folds or not predictions:
            raise ValueError("shadow report requires walk-forward folds and predictions")
        fold_keys = tuple(_fold_key(item) for item in folds)
        prediction_keys = tuple(_prediction_key(item) for item in predictions)
        if len(set(fold_keys)) != len(fold_keys) or len(set(prediction_keys)) != len(prediction_keys):
            raise ValueError("shadow report folds and predictions must be unique")
        family_by_fold: dict[tuple[date, str, str], set[str]] = {}
        for item in folds:
            key = item.prediction_date, item.horizon, item.window_mode
            family_by_fold.setdefault(key, set()).add(item.model_family)
        if any(families != {"linear", "lightgbm"} for families in family_by_fold.values()):
            raise ValueError("shadow report folds must contain both fixed model families")
        prediction_folds = {(item.prediction_date, item.horizon, item.window_mode) for item in predictions}
        if prediction_folds != set(family_by_fold):
            raise ValueError("shadow report predictions must exactly match model folds")
        if any(
            item.train_start < self.training_window_start or item.prediction_date > self.training_window_end
            for item in folds
        ):
            raise ValueError("shadow report fold is outside its training identity")
        if any(
            not self.training_window_start <= item.prediction_date <= self.training_window_end for item in predictions
        ):
            raise ValueError("shadow report prediction date is outside its training identity")
        object.__setattr__(self, "folds", folds)
        object.__setattr__(self, "predictions", predictions)
        object.__setattr__(self, "content_hash", _canonical_hash(_shadow_report_payload(self)))


def shadow_spec_hash() -> str:
    return _canonical_hash(
        {
            "implementation_version": SHADOW_IMPLEMENTATION_VERSION,
            "feature_version": TOMORROW_FEATURE_SCHEMA_VERSION,
            "random_seed": SHADOW_RANDOM_SEED,
            "cost_bps": SHADOW_COST_BPS,
            "window_modes": ("expanding", "rolling_252"),
            "embargo_dates": SHADOW_EMBARGO_DATES,
            "min_train_dates": SHADOW_MIN_TRAIN_DATES,
            "validation_dates": SHADOW_VALIDATION_DATES,
            "calibration_dates": SHADOW_CALIBRATION_DATES,
            "linear_ridge": 1e-3,
            "lightgbm": {
                "max_depth": 3,
                "num_leaves": 7,
                "min_data_in_leaf": 20,
                "learning_rate": 0.05,
                "num_boost_round": 200,
                "early_stopping_rounds": 20,
            },
            "net_calibration": "affine",
            "severe_calibration": "platt",
            "severe_threshold_mae_atr20": -1.5,
        }
    )


def _shadow_report_payload(report: ShadowModelReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "spec_hash": report.spec_hash,
        "feature_version": report.feature_version,
        "training_window_start": report.training_window_start.isoformat(),
        "training_window_end": report.training_window_end.isoformat(),
        "random_seed": report.random_seed,
        "cost_bps": report.cost_bps,
        "status": report.status,
        "production_authority": report.production_authority,
        "folds": tuple(_fold_payload(item) for item in report.folds),
        "predictions": tuple(_prediction_payload(item) for item in report.predictions),
    }


def _fold_payload(item: ShadowFoldRecord) -> dict[str, object]:
    return {
        "horizon": item.horizon,
        "window_mode": item.window_mode,
        "prediction_date": item.prediction_date.isoformat(),
        "model_family": item.model_family,
        "train_start": item.train_start.isoformat(),
        "train_end": item.train_end.isoformat(),
        "validation_start": item.validation_start.isoformat(),
        "validation_end": item.validation_end.isoformat(),
        "calibration_start": item.calibration_start.isoformat(),
        "calibration_end": item.calibration_end.isoformat(),
        "train_date_count": item.train_date_count,
        "net_model_hash": item.net_model_hash,
        "severe_model_hash": item.severe_model_hash,
    }


def _prediction_payload(item: ShadowPrediction) -> dict[str, object]:
    return {
        "prediction_date": item.prediction_date.isoformat(),
        "code": item.code,
        "board": item.board,
        "industry": item.industry,
        "horizon": item.horizon,
        "window_mode": item.window_mode,
        "feature_batch_hash": item.feature_batch_hash,
        "estimated_cost": item.estimated_cost,
        "actual_net_excess": item.actual_net_excess,
        "actual_severe_loss": item.actual_severe_loss,
        "linear_net_excess": item.linear_net_excess,
        "lightgbm_net_excess": item.lightgbm_net_excess,
        "linear_severe_probability": item.linear_severe_probability,
        "lightgbm_severe_probability": item.lightgbm_severe_probability,
        "uncertainty": item.uncertainty,
    }


def _fold_key(item: ShadowFoldRecord) -> tuple[date, str, str, str]:
    return item.prediction_date, item.horizon, item.window_mode, item.model_family


def _prediction_key(item: ShadowPrediction) -> tuple[date, str, str, str]:
    return item.prediction_date, item.horizon, item.window_mode, item.code


def _canonical_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "SHADOW_CALIBRATION_DATES",
    "SHADOW_COST_BPS",
    "SHADOW_EMBARGO_DATES",
    "SHADOW_IMPLEMENTATION_VERSION",
    "SHADOW_MIN_TRAIN_DATES",
    "SHADOW_RANDOM_SEED",
    "SHADOW_REPORT_SCHEMA_VERSION",
    "SHADOW_ROLLING_DATES",
    "SHADOW_VALIDATION_DATES",
    "ShadowFoldRecord",
    "ShadowHorizon",
    "ShadowLabeledDay",
    "ShadowModelReport",
    "ShadowPrediction",
    "ShadowSettlementLabel",
    "ShadowWindowMode",
    "shadow_spec_hash",
]

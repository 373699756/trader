"""Deterministic Tomorrow V3 training from the partitioned BaoStock archive."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import cast

import lightgbm as lgb
import numpy as np

from trader.application.research.tomorrow_v3_training import TomorrowV3TrainingWindow
from trader.domain.research.baostock_daily import BaoStockDailyManifest, BaoStockTrainingRow, BaoStockV3Split
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyPartitionedArchive,
)

_FEATURE_IDS = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
_MODEL_ID = "tomorrow_v3_industry_ridge_lightgbm_v1"


@dataclass(frozen=True)
class TomorrowV3TrainingResult:
    status: str
    run_id: str | None
    manifest_hash: str
    model_hash: str
    industry_count: int
    training_rows: int
    validation_rows: int
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _Sample:
    code: str
    trade_date: date
    board: str
    industry: str
    features: tuple[float, ...]
    next_return: float
    eligible: bool


def run_tomorrow_v3_training(history_root: Path, train_root: Path) -> TomorrowV3TrainingResult:
    try:
        archive = BaoStockDailyPartitionedArchive(history_root / "baostock-daily" / "sessions-2000")
        manifest = archive.verify()
    except (BaoStockDailyArtifactConflictError, OSError, ValueError) as exc:
        return TomorrowV3TrainingResult("blocked", None, "", "", 0, 0, 0, (_reason(exc),))
    run_id = hashlib.sha256(f"{manifest.content_hash}:tomorrow-v3".encode()).hexdigest()
    output = train_root / "tomorrow-v3" / run_id
    try:
        dates = archive.complete_dates()
        split = _build_split(dates, manifest.content_hash)
        window = TomorrowV3TrainingWindow(split)
        samples = _build_samples(archive, manifest, window)
        if not samples:
            return TomorrowV3TrainingResult(
                "blocked", run_id, manifest.content_hash, "", 0, 0, 0, ("v3_training_rows_empty",)
            )
        models, training_rows, validation_rows = _fit_models(samples, split)
        report = _build_report(manifest.content_hash, split, models, training_rows, validation_rows)
        _write_json(output / "report.json", report)
        if not report["validation_passed"]:
            return TomorrowV3TrainingResult(
                "rejected",
                run_id,
                manifest.content_hash,
                "",
                len(models),
                training_rows,
                validation_rows,
                tuple(cast(list[str], report["failure_reasons"])),
            )
        model = _model_document(manifest.content_hash, split, models, training_rows, validation_rows)
        model_hash = _content_hash(model)
        model["content_hash"] = model_hash
        _write_json(output / "model.json", model)
        return TomorrowV3TrainingResult(
            "validated",
            run_id,
            manifest.content_hash,
            model_hash,
            len(models),
            training_rows,
            validation_rows,
            (),
        )
    except (BaoStockDailyArtifactConflictError, OSError, ValueError, RuntimeError) as exc:
        return TomorrowV3TrainingResult("blocked", run_id, manifest.content_hash, "", 0, 0, 0, (_reason(exc),))


def _build_split(dates: tuple[date, ...], manifest_hash: str) -> BaoStockV3Split:
    from trader.domain.research.baostock_daily import build_baostock_v3_split

    return build_baostock_v3_split(dates, parent_manifest_hash=manifest_hash)


def _build_samples(
    archive: BaoStockDailyPartitionedArchive,
    manifest: BaoStockDailyManifest,
    window: TomorrowV3TrainingWindow,
) -> tuple[_Sample, ...]:
    codes = tuple(code for partition in manifest.partitions for code in partition.codes)
    by_date: dict[date, list[_Sample]] = defaultdict(list)
    for code in codes:
        rows = archive.read_training_rows(code, allowed_dates=window.readable_dates)
        ordered = tuple(sorted(rows, key=lambda item: item.trade_date))
        closes = {item.trade_date: item.qfq.close_price for item in ordered}
        for index, row in enumerate(ordered):
            if index < 60 or row.qfq.close_price is None:
                continue
            next_row = ordered[index + 1] if index + 1 < len(ordered) else None
            if (
                next_row is None
                or next_row.trade_date not in window.readable_dates
                or next_row.qfq.close_price in (None, 0)
            ):
                continue
            close = float(row.qfq.close_price)
            features = (
                _return(close, closes.get(ordered[index - 1].trade_date)),
                _return(close, closes.get(ordered[index - 3].trade_date)),
                _return(close, closes.get(ordered[index - 5].trade_date)),
                _momentum(closes, ordered, index, 20),
                _momentum(closes, ordered, index, 40),
                _momentum(closes, ordered, index, 60),
            )
            if not all(math.isfinite(value) for value in features):
                continue
            by_date[row.trade_date].append(
                _Sample(
                    code,
                    row.trade_date,
                    row.board,
                    row.industry,
                    features,
                    float(next_row.qfq.close_price) / close - 1.0,
                    not row.is_st and row.unadjusted.trading_status == "trading",
                )
            )
    result: list[_Sample] = []
    for day, values in by_date.items():
        eligible_returns = tuple(item.next_return for item in values if item.eligible)
        if not eligible_returns:
            continue
        benchmark = math.fsum(eligible_returns) / len(eligible_returns)
        board_means = _group_means(values, 1)
        industry_means = _group_means(values, 2)
        for item in values:
            residuals = tuple(
                item.features[3 + offset]
                - math.fsum(value.features[3 + offset] for value in values) / len(values)
                - board_means.get((item.board, offset), 0.0)
                - industry_means.get((item.industry, offset), 0.0)
                for offset in range(3)
            )
            result.append(
                _Sample(
                    item.code,
                    day,
                    item.board,
                    item.industry,
                    (*item.features[:3], *residuals),
                    item.next_return - benchmark - 0.002,
                    item.eligible,
                )
            )
    return tuple(sorted(result, key=lambda item: (item.trade_date, item.code)))


def _group_means(values: list[_Sample], dimension: int) -> dict[tuple[str, int], float]:
    grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
    for item in values:
        key = (item.board if dimension == 1 else item.industry, 0)
        for offset in range(3):
            grouped[(key[0], offset)].append(item.features[3 + offset])
    return {key: math.fsum(items) / len(items) for key, items in grouped.items()}


def _fit_models(samples: tuple[_Sample, ...], split: BaoStockV3Split) -> tuple[dict[str, dict[str, object]], int, int]:
    models: dict[str, dict[str, object]] = {}
    training = tuple(item for item in samples if item.trade_date in split.model_fit_dates and item.eligible)
    calibration = tuple(item for item in samples if item.trade_date in split.calibration_dates and item.eligible)
    validation = tuple(
        item
        for item in samples
        if item.trade_date in (*split.confirmation_dates, *split.daily_proxy_holdout_dates) and item.eligible
    )
    for industry in sorted({item.industry for item in training}):
        train_rows = tuple(item for item in training if item.industry == industry)
        calibration_rows = tuple(item for item in calibration if item.industry == industry)
        valid_rows = tuple(item for item in validation if item.industry == industry)
        early_rows = tuple(
            item
            for item in samples
            if item.trade_date in split.early_stopping_dates and item.industry == industry and item.eligible
        )
        if len(train_rows) < 20_000 or not calibration_rows or not early_rows:
            continue
        features = np.asarray(tuple(item.features for item in train_rows), dtype=np.float64)
        labels = np.asarray(tuple(item.next_return for item in train_rows), dtype=np.float64)
        means = features.mean(axis=0)
        scales = np.where(features.std(axis=0) > 1e-12, features.std(axis=0), 1.0)
        normalized = (features - means) / scales
        design = np.column_stack((np.ones(len(normalized)), normalized))
        penalty = np.eye(7, dtype=np.float64) * 10.0
        penalty[0, 0] = 0.0
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ labels)
        early_features = (np.asarray(tuple(item.features for item in early_rows), dtype=np.float64) - means) / scales
        booster = lgb.train(
            {
                "objective": "regression_l2",
                "learning_rate": 0.05,
                "max_depth": 3,
                "num_leaves": 7,
                "min_data_in_leaf": 20,
                "num_boost_round": 200,
                "max_bin": 63,
                "deterministic": True,
                "num_threads": 1,
                "verbosity": -1,
            },
            lgb.Dataset(normalized, label=labels),
            num_boost_round=200,
            valid_sets=[lgb.Dataset(early_features, label=np.asarray(tuple(item.next_return for item in early_rows)))],
            callbacks=[lgb.early_stopping(20, verbose=False)],
        )
        calibration_features = (
            np.asarray(tuple(item.features for item in calibration_rows), dtype=np.float64) - means
        ) / scales
        tree = booster.predict(calibration_features, num_iteration=booster.best_iteration)
        ridge = coefficients[0] + calibration_features @ coefficients[1:]
        predicted = 0.5 * ridge + 0.5 * tree
        actual = np.asarray(tuple(item.next_return for item in calibration_rows))
        slope, intercept = np.polyfit(predicted, actual, 1) if len(calibration_rows) >= 2 else (1.0, 0.0)
        models[industry] = {
            "transformer_means": means.tolist(),
            "transformer_scales": scales.tolist(),
            "ridge_intercept": float(coefficients[0]),
            "ridge_coefficients": coefficients[1:].tolist(),
            "lightgbm_model": booster.model_to_string(num_iteration=booster.best_iteration),
            "lightgbm_best_iteration": int(booster.best_iteration),
            "calibration_intercept": float(intercept),
            "calibration_slope": float(slope),
            "training_rows": len(train_rows),
            "validation_rows": len(valid_rows),
        }
    return models, len(training), len(validation)


def _build_report(
    manifest_hash: str,
    split: BaoStockV3Split,
    models: dict[str, dict[str, object]],
    training_rows: int,
    validation_rows: int,
) -> dict[str, object]:
    reasons = [] if models and validation_rows > 0 else ["v3_industry_model_validation_insufficient"]
    return {
        "schema_version": "tomorrow_v3_training_report_v1",
        "model_id": _MODEL_ID,
        "manifest_hash": manifest_hash,
        "split_hash": split.content_hash,
        "training_anchor": "15:00_close",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "industry_count": len(models),
        "training_rows": training_rows,
        "validation_rows": validation_rows,
        "validation_passed": not reasons,
        "failure_reasons": reasons,
        "automatic_model_update": False,
    }


def _model_document(
    manifest_hash: str,
    split: BaoStockV3Split,
    models: dict[str, dict[str, object]],
    training_rows: int,
    validation_rows: int,
) -> dict[str, object]:
    return {
        "schema_version": "tomorrow_v3_production_model_v1",
        "profile_id": "v3",
        "model_id": _MODEL_ID,
        "feature_ids": list(_FEATURE_IDS),
        "manifest_hash": manifest_hash,
        "split_hash": split.content_hash,
        "training_anchor": "15:00_close",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "training_rows": training_rows,
        "validation_rows": validation_rows,
        "industries": models,
        "dependencies": {"lightgbm": version("lightgbm"), "numpy": version("numpy")},
        "automatic_model_update": False,
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _content_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _return(current: float, previous: float | None) -> float:
    return current / previous - 1.0 if previous not in (None, 0.0) else float("nan")


def _momentum(
    closes: dict[date, float | None], rows: tuple[BaoStockTrainingRow, ...], index: int, length: int
) -> float:
    start = closes.get(rows[index - length - 5].trade_date)
    end = closes.get(rows[index - 5].trade_date)
    return end / start - 1.0 if start not in (None, 0.0) and end is not None else float("nan")


def _reason(exc: BaseException) -> str:
    text = str(exc).lower()
    return "history_manifest_unavailable" if "manifest" in text or "no such file" in text else "v3_training_failed"


__all__ = ["TomorrowV3TrainingResult", "run_tomorrow_v3_training"]

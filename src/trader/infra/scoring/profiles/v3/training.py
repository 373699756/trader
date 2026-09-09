"""Deterministic Tomorrow V3 training from the partitioned BaoStock archive."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast

import lightgbm as lgb
import numpy as np

from trader.application.research.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingWindow,
)
from trader.domain.market.feature_contracts import (
    TOMORROW_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    QfqPriceAnchors,
    calculate_tomorrow_qfq_alpha,
)
from trader.domain.recommendation.model_scoring import V3_EXPOSURE_CONTRACT, residualize_exposure
from trader.domain.research.baostock_daily import BaoStockTrainingRow, BaoStockTrainingSplit
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockTrainingTrainingInputArchive,
    BaoStockTrainingTrainingInputSnapshot,
)
from trader.infra.scoring.artifact_hashing import artifact_content_hash

_MODEL_ID = "industry_ridge_lightgbm"
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class TomorrowTrainingResult:
    status: str
    training_input_scope: Literal["unavailable", "complete_manifest", "partial_checkpoint"]
    run_id: str | None
    training_input_hash: str
    training_input_codes: int
    training_universe_codes: int
    report_hash: str
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
    average_amount_20d: float
    features: tuple[float, ...]
    next_return: float
    eligible: bool


@dataclass(frozen=True)
class _TrainingArtifactContext:
    training_input_scope: Literal["complete_manifest", "partial_checkpoint"]
    training_input_hash: str
    training_input_codes: int
    training_universe_codes: int
    split: BaoStockTrainingSplit
    source_commit: str
    training_contract_hash: str
    training_rows: int
    validation_rows: int


def run_tomorrow_training(
    history_root: Path,
    train_root: Path,
    *,
    allow_partial_history: bool = False,
    progress: TomorrowTrainingProgressPort | None = None,
    source_commit: str = "",
) -> TomorrowTrainingResult:
    try:
        archive = BaoStockTrainingTrainingInputArchive.open(
            history_root / "baostock-daily" / "sessions-2000",
            allow_partial_history=allow_partial_history,
        )
    except (BaoStockDailyArtifactConflictError, OSError, ValueError) as exc:
        return TomorrowTrainingResult("blocked", "unavailable", None, "", 0, 0, "", "", 0, 0, 0, (_reason(exc),))
    snapshot = archive.snapshot
    if _SOURCE_COMMIT.fullmatch(source_commit) is None:
        return TomorrowTrainingResult(
            "blocked",
            snapshot.input_scope,
            None,
            snapshot.input_hash,
            len(snapshot.training_codes),
            snapshot.universe_count,
            "",
            "",
            0,
            0,
            0,
            ("source_commit_unavailable",),
        )
    _publish_progress(progress, "input_snapshot", len(snapshot.training_codes), len(snapshot.training_codes))
    run_id = hashlib.sha256(f"{snapshot.input_hash}:tomorrow-v3".encode()).hexdigest()
    output = _training_output_directory(train_root)
    try:
        split = _build_split(snapshot.calendar.open_dates, snapshot.input_hash)
        training_contract_hash = _training_contract_hash(snapshot, split, source_commit)
        _write_training_input_evidence(output, snapshot, source_commit, training_contract_hash)
        window = TomorrowTrainingWindow(split)
        samples = _build_samples(archive, snapshot.training_codes, window, progress=progress)
        if not samples:
            return TomorrowTrainingResult(
                "blocked",
                snapshot.input_scope,
                run_id,
                snapshot.input_hash,
                len(snapshot.training_codes),
                snapshot.universe_count,
                "",
                "",
                0,
                0,
                0,
                ("v3_training_rows_empty",),
            )
        _publish_progress(progress, "model_fit", 0, len(snapshot.training_codes))
        models, training_rows, validation_rows = _fit_models(samples, split)
        context = _TrainingArtifactContext(
            snapshot.input_scope,
            snapshot.input_hash,
            len(snapshot.training_codes),
            snapshot.universe_count,
            split,
            source_commit,
            training_contract_hash,
            training_rows,
            validation_rows,
        )
        provisional_model = _model_document(context, "0" * 64, models)
        model_payload_hash = cast(str, provisional_model["model_payload_hash"])
        report = _build_report(context, models, model_payload_hash)
        report_hash = artifact_content_hash(report)
        report["content_hash"] = report_hash
        _write_json(output / "report.json", report)
        if not report["validation_passed"]:
            return TomorrowTrainingResult(
                "rejected",
                snapshot.input_scope,
                run_id,
                snapshot.input_hash,
                len(snapshot.training_codes),
                snapshot.universe_count,
                report_hash,
                "",
                len(models),
                training_rows,
                validation_rows,
                tuple(cast(list[str], report["failure_reasons"])),
            )
        model = _model_document(context, report_hash, models)
        model_hash = artifact_content_hash(model)
        model["content_hash"] = model_hash
        _write_json(output / "model.json", model)
        _publish_progress(progress, "completed", len(snapshot.training_codes), len(snapshot.training_codes))
        return TomorrowTrainingResult(
            "trial_ready" if snapshot.input_scope == "partial_checkpoint" else "engineering_ready",
            snapshot.input_scope,
            run_id,
            snapshot.input_hash,
            len(snapshot.training_codes),
            snapshot.universe_count,
            report_hash,
            model_hash,
            len(models),
            training_rows,
            validation_rows,
            (),
        )
    except (BaoStockDailyArtifactConflictError, OSError, ValueError, RuntimeError) as exc:
        return TomorrowTrainingResult(
            "blocked",
            snapshot.input_scope,
            run_id,
            snapshot.input_hash,
            len(snapshot.training_codes),
            snapshot.universe_count,
            "",
            "",
            0,
            0,
            0,
            (_reason(exc),),
        )


def _build_split(dates: tuple[date, ...], manifest_hash: str) -> BaoStockTrainingSplit:
    from trader.domain.research.baostock_daily import build_baostock_training_split

    return build_baostock_training_split(dates, parent_manifest_hash=manifest_hash)


def _build_samples(
    archive: BaoStockTrainingTrainingInputArchive,
    codes: tuple[str, ...],
    window: TomorrowTrainingWindow,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> tuple[_Sample, ...]:
    calendar = archive.snapshot.calendar.open_dates
    by_date: dict[date, list[_Sample]] = defaultdict(list)
    for position, code in enumerate(codes, start=1):
        rows = archive.read_training_rows(code, allowed_dates=window.readable_dates)
        rows_by_date = {item.trade_date: item for item in rows}
        closes = {item.trade_date: item.qfq.close_price for item in rows}
        for day, next_day, indices in _aligned_sample_dates(calendar, rows_by_date.keys(), window.readable_dates):
            _index, previous_1, previous_3, previous_5, momentum_20, momentum_40, momentum_60 = indices
            row = rows_by_date.get(day)
            next_row = rows_by_date.get(next_day)
            if (
                row is None
                or next_row is None
                or row.qfq.close_price in (None, 0)
                or next_row.qfq.close_price in (None, 0)
            ):
                continue
            close = float(row.qfq.close_price)
            average_amount_20d = _average_amount_20d(rows_by_date, calendar, _index)
            if average_amount_20d is None:
                continue
            raw_features = TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.bind(
                calculate_tomorrow_qfq_alpha(
                    QfqPriceAnchors(
                        close,
                        (
                            (1, closes.get(calendar[previous_1])),
                            (3, closes.get(calendar[previous_3])),
                            (5, closes.get(calendar[previous_5])),
                            (20, closes.get(calendar[momentum_20])),
                            (40, closes.get(calendar[momentum_40])),
                            (60, closes.get(calendar[momentum_60])),
                        ),
                    )
                )
            )
            if any(raw_features.missing_mask):
                continue
            features = raw_features.require_complete()
            by_date[row.trade_date].append(
                _Sample(
                    code,
                    row.trade_date,
                    row.board,
                    row.industry,
                    average_amount_20d,
                    features,
                    float(next_row.qfq.close_price) / close - 1.0,
                    not row.is_st and row.unadjusted.trading_status == "trading",
                )
            )
        if position == 1 or position % 50 == 0 or position == len(codes):
            _publish_progress(progress, "sample_build", position, len(codes))
    result: list[_Sample] = []
    for day, values in by_date.items():
        eligible = tuple(item for item in values if item.eligible)
        if not eligible:
            continue
        benchmark = math.fsum(item.next_return for item in eligible) / len(eligible)
        residuals = _residualize_sample_day(
            tuple(item.features[3:] for item in eligible),
            tuple(item.board for item in eligible),
            tuple(item.industry for item in eligible),
            tuple(item.average_amount_20d for item in eligible),
        )
        for index, item in enumerate(eligible):
            result.append(
                _Sample(
                    item.code,
                    day,
                    item.board,
                    item.industry,
                    item.average_amount_20d,
                    (*item.features[:3], *(values[index] for values in residuals)),
                    training_alpha_target(next_return=item.next_return, benchmark_return=benchmark),
                    item.eligible,
                )
            )
    return tuple(sorted(result, key=lambda item: (item.trade_date, item.code)))


def training_alpha_target(*, next_return: float, benchmark_return: float) -> float:
    """Return pre-cost alpha; validation and execution own round-trip costs."""

    if not math.isfinite(next_return) or not math.isfinite(benchmark_return):
        raise ValueError("V3 training alpha inputs must be finite")
    return next_return - benchmark_return


def _aligned_sample_dates(
    calendar: tuple[date, ...],
    available_dates: Collection[date],
    readable_dates: frozenset[date],
) -> tuple[tuple[date, date, tuple[int, ...]], ...]:
    available = set(available_dates)
    result: list[tuple[date, date, tuple[int, ...]]] = []
    for index, day in enumerate(calendar):
        next_index = index + 1
        indices = (index, index - 1, index - 3, index - 5, index - 20, index - 40, index - 60, next_index)
        if next_index >= len(calendar) or any(value < 0 for value in indices):
            continue
        amount_indices = tuple(range(index - 19, index + 1))
        required_dates = tuple(calendar[value] for value in (*indices, *amount_indices))
        if not set(required_dates).issubset(readable_dates) or not set(required_dates).issubset(available):
            continue
        result.append((day, calendar[next_index], indices[:-1]))
    return tuple(result)


def _average_amount_20d(
    rows_by_date: Mapping[date, BaoStockTrainingRow],
    calendar: tuple[date, ...],
    index: int,
) -> float | None:
    rows = tuple(rows_by_date.get(calendar[position]) for position in range(index - 19, index + 1))
    amounts = tuple(row.qfq.amount if row is not None else None for row in rows)
    if any(amount is None or not math.isfinite(amount) or amount <= 0.0 for amount in amounts):
        return None
    return math.fsum(cast(float, amount) for amount in amounts) / 20.0


def _residualize_sample_day(
    momenta: Sequence[Sequence[float]],
    boards: Sequence[str],
    industries: Sequence[str],
    average_amounts: Sequence[float],
) -> tuple[tuple[float, ...], ...]:
    if not momenta or not momenta[0] or any(len(row) != len(momenta[0]) for row in momenta):
        raise ValueError("V3 training momentum rows must have one consistent non-empty width")
    return tuple(
        residualize_exposure(
            tuple(row[offset] for row in momenta),
            boards,
            average_amounts,
            industries=industries,
            contract=V3_EXPOSURE_CONTRACT,
        )
        for offset in range(len(momenta[0]))
    )


def _fit_models(
    samples: tuple[_Sample, ...], split: BaoStockTrainingSplit
) -> tuple[dict[str, dict[str, object]], int, int]:
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
        if len(train_rows) < 20_000 or not calibration_rows or not early_rows or not valid_rows:
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
    context: _TrainingArtifactContext,
    models: dict[str, dict[str, object]],
    model_payload_hash: str,
) -> dict[str, object]:
    reasons = [] if models and context.validation_rows > 0 else ["v3_industry_model_validation_insufficient"]
    return {
        "schema_version": "tomorrow_training_report",
        "model_id": _MODEL_ID,
        "training_input_scope": context.training_input_scope,
        "training_input_hash": context.training_input_hash,
        "training_input_codes": context.training_input_codes,
        "training_universe_codes": context.training_universe_codes,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "split_hash": context.split.content_hash,
        "source_commit": context.source_commit,
        "training_contract_hash": context.training_contract_hash,
        "model_payload_hash": model_payload_hash,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "validation_scope": "daily_close_engineering_proxy",
        "industry_count": len(models),
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "validation_passed": not reasons,
        "failure_reasons": reasons,
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": list(_historical_failure_reasons(context.training_input_scope)),
        "automatic_model_update": False,
        "production_authority": False,
    }


def _model_document(
    context: _TrainingArtifactContext,
    report_hash: str,
    models: dict[str, dict[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "tomorrow_scoring_model",
        "profile_id": "v3",
        "model_id": _MODEL_ID,
        "strategy_head": "tomorrow",
        "feature_ids": list(TOMORROW_MODEL_FEATURE_MANIFEST.names),
        "feature_units": list(TOMORROW_MODEL_FEATURE_MANIFEST.units),
        "exposure_contract": {
            "market": True,
            "board": True,
            "industry": True,
            "log_average_amount_20d": True,
            "order": list(V3_EXPOSURE_CONTRACT.order),
        },
        "training_input_scope": context.training_input_scope,
        "training_input_hash": context.training_input_hash,
        "training_input_codes": context.training_input_codes,
        "training_universe_codes": context.training_universe_codes,
        "split_hash": context.split.content_hash,
        "report_hash": report_hash,
        "source_commit": context.source_commit,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "training_contract_hash": context.training_contract_hash,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": list(_historical_failure_reasons(context.training_input_scope)),
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "training_rows": context.training_rows,
        "validation_rows": context.validation_rows,
        "industry_count": len(models),
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
        "industries": models,
        "dependencies": {"lightgbm": version("lightgbm"), "numpy": version("numpy")},
        "automatic_model_update": False,
        "production_authority": False,
    }
    document["model_payload_hash"] = _model_payload_hash(document)
    return document


def _training_contract_hash(
    snapshot: BaoStockTrainingTrainingInputSnapshot,
    split: BaoStockTrainingSplit,
    source_commit: str,
) -> str:
    contract: dict[str, object] = {
        "schema_version": "tomorrow_training_contract",
        "source_commit": source_commit,
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.input_hash,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "feature_ids": list(TOMORROW_MODEL_FEATURE_MANIFEST.names),
        "feature_units": list(TOMORROW_MODEL_FEATURE_MANIFEST.units),
        "split_hash": split.content_hash,
        "label_target": "pre_cost_excess_return",
        "benchmark": "daily_close_equal_weight_engineering_proxy",
        "training_cost_bps": 0,
        "evaluation_cost_bps": [20, 50, 100],
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "validation_scope": "daily_close_engineering_proxy",
        "terminal_holdout_opened": False,
        "automatic_model_update": False,
        "production_authority": False,
    }
    return artifact_content_hash(contract)


def _write_training_input_evidence(
    output: Path,
    snapshot: BaoStockTrainingTrainingInputSnapshot,
    source_commit: str,
    training_contract_hash: str,
) -> None:
    document: dict[str, object] = {
        "schema_version": "tomorrow_training_input",
        "training_input_scope": snapshot.input_scope,
        "training_input_hash": snapshot.input_hash,
        "training_input_codes": len(snapshot.training_codes),
        "training_universe_codes": snapshot.universe_count,
        "completed_codes": snapshot.completed_code_count,
        "codes": list(snapshot.training_codes),
        "source_commit": source_commit,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "training_contract_hash": training_contract_hash,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "production_authority": False,
    }
    document["content_hash"] = artifact_content_hash(document)
    _write_json(output / "training-input.json", document)


def _model_payload_hash(document: dict[str, object]) -> str:
    excluded = {"content_hash", "report_hash", "model_payload_hash"}
    return artifact_content_hash({key: value for key, value in document.items() if key not in excluded})


def _historical_failure_reasons(
    training_input_scope: str,
) -> tuple[str, ...]:
    reasons = ["daily_close_proxy_not_point_in_time"]
    if training_input_scope == "partial_checkpoint":
        reasons.append("partial_history_pipeline_trial")
    return tuple(reasons)


def _training_output_directory(train_root: Path) -> Path:
    return train_root / "tomorrow-v3"


def _publish_progress(
    progress: TomorrowTrainingProgressPort | None,
    stage: Literal["input_snapshot", "sample_build", "model_fit", "completed"],
    processed_codes: int,
    total_codes: int,
) -> None:
    if progress is not None:
        progress.publish(TomorrowTrainingProgress(stage, processed_codes, total_codes))


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


def _reason(exc: BaseException) -> str:
    text = str(exc).lower()
    return "history_manifest_unavailable" if "manifest" in text or "no such file" in text else "v3_training_failed"


__all__ = ["TomorrowTrainingResult", "run_tomorrow_training"]

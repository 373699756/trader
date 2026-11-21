"""Fit and independently calibrate one pooled next-close hit model."""

from __future__ import annotations

import math
from collections.abc import Callable, Generator, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Protocol, TypeAlias

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from trader.download.domain.history_control import HistoryPublishedControlState
from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader, HistorySnapshotReadError
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.tomorrow_probability import BuildTomorrowFeatures, TOMORROW_FEATURE_NAMES
from trader.recommendation.domain.outcomes.tomorrow_target import EvaluateTomorrowCloseGain, TomorrowCloseGain
from trader.training.application.resources import TrainingMemoryAdmission
from trader.training.domain.tomorrow_probability import TomorrowProbabilitySelection, TomorrowProbabilityTrainingResult
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.tomorrow_probability import (
    LogisticTomorrowModel,
    PublishTomorrowProbabilityModel,
    TomorrowProbabilityArtifact,
)

_FIT_SESSIONS = 750
_FOLD_SESSIONS = 20
_FOLDS = 2
_CALIBRATION_SESSIONS = 20
_ASSESSMENT_SESSIONS = 20
_EMBARGO_SESSIONS = 5
_RESERVE_SESSIONS = _FOLD_SESSIONS * _FOLDS + _CALIBRATION_SESSIONS + _ASSESSMENT_SESSIONS + 3 * _EMBARGO_SESSIONS
_MIN_FITTING_SESSIONS = 60
_BOARD_SEATS = 4
_SEATS = 6
_BOARDS = ("main", "chinext", "star")
_FloatArray: TypeAlias = NDArray[np.float64]
_IntArray: TypeAlias = NDArray[np.int64]


@dataclass(frozen=True, slots=True)
class _Samples:
    features: _FloatArray
    labels: _FloatArray
    codes: _IntArray
    boards: _IntArray
    offsets: tuple[int, ...]
    pending: int
    missing_features: int

    def rows(self, start_day: int, end_day: int) -> slice:
        return slice(self.offsets[start_day], self.offsets[end_day])


@dataclass(frozen=True, slots=True)
class TomorrowHistoryInput:
    """A price-only input pinned by the selected training operation's history lock."""

    state: HistoryPublishedControlState
    reader: HistorySnapshotReader

    @property
    def snapshot_hash(self) -> str:
        return self.state.snapshot.content_hash

    @property
    def open_dates(self) -> tuple[date, ...]:
        return self.state.calendar.open_dates

    @property
    def universe_count(self) -> int:
        return len(self.state.universe.securities)

    def iter_training_rows(self, dates: tuple[date, ...]) -> Generator[HistoryTrainingRow, None, None]:
        yield from self.reader.iter_training_rows(
            self.state.snapshot, dates, tuple(item.code for item in self.state.universe.securities),
            industry_snapshot=None,
        )


def OpenTomorrowHistoryInput(root: Path, *, sqlite_policy: SqliteIoPolicy) -> TomorrowHistoryInput:
    control = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=sqlite_policy)
    try:
        state = control.load_published_state()
    except HistoryControlError as exc:
        raise RuntimeError("history_manifest_unavailable") from exc
    if state is None:
        raise RuntimeError("history_manifest_unavailable")
    return TomorrowHistoryInput(state, HistorySnapshotReader(root, sqlite_policy=sqlite_policy))


class SharedTomorrowTrainer(Protocol):
    def __call__(
        self,
        history: TomorrowHistoryInput,
        model_root: Path,
        *,
        cancelled: Callable[[], bool],
        cpu_budget: TrainingCpuBudget,
        memory: TrainingMemoryAdmission,
    ) -> TomorrowProbabilityTrainingResult: ...


def TrainSharedTomorrowProbability(
    history: TomorrowHistoryInput,
    model_root: Path,
    *,
    cancelled: Callable[[], bool],
    cpu_budget: TrainingCpuBudget,
    memory: TrainingMemoryAdmission,
) -> TomorrowProbabilityTrainingResult:
    calendar = history.open_dates[-(_FIT_SESSIONS + _RESERVE_SESSIONS + 1) :]
    cap = len(calendar) * history.universe_count
    if len(calendar) < _MIN_FITTING_SESSIONS + _RESERVE_SESSIONS + 1:
        return _failed(history.snapshot_hash, "tomorrow_insufficient_training_dates")

    def check() -> None:
        if cancelled():
            raise InterruptedError("tomorrow_probability_training_cancelled")

    # Allow for the packed samples, normalized design, IRLS scratch and the native tree dataset.
    required_bytes = max(1, cap * (5 * len(TOMORROW_FEATURE_NAMES) + 15) * 8)
    try:
        with (
            memory.reserve(required_bytes, check),
            cpu_budget.reserve(cpu_budget.native_threads, check),
            cpu_budget.native_runtime(),
            closing(history.iter_training_rows(calendar)) as rows,
        ):
            samples = _collect_samples(rows, calendar, cap, check)
            return _train(samples, calendar[:-1], history.snapshot_hash, model_root, check)
    except (OSError, ValueError, HistorySnapshotReadError, lgb.basic.LightGBMError, np.linalg.LinAlgError) as exc:
        if isinstance(exc, InterruptedError):
            raise
        detail = str(exc)
        if detail.startswith("training_memory_request_exceeds_budget:"):
            reason = "tomorrow_probability_memory_budget_insufficient"
        else:
            reason = detail if isinstance(exc, ValueError) and detail.startswith("tomorrow_") else type(exc).__name__
        return _failed(history.snapshot_hash, reason)


def _failed(snapshot_hash: str, reason: str) -> TomorrowProbabilityTrainingResult:
    return TomorrowProbabilityTrainingResult(snapshot_hash, None, None, failure=reason)


def _label(current: HistoryTrainingRow, following: HistoryTrainingRow | None) -> TomorrowCloseGain:
    left = current.point
    if following is None or left is None or left.raw_close_price is None:
        return TomorrowCloseGain.PENDING
    if following.cell_status == "supplier_marked_suspended":
        return EvaluateTomorrowCloseGain(Decimal(str(left.raw_close_price)), None, confirmed_suspended=True)
    right = following.point
    if (
        right is None or following.cell_status != "complete"
        or left.qfq_close_price is None or right.qfq_close_price is None
        or left.qfq_reconstruction_anchor is not None or right.qfq_reconstruction_anchor is not None
    ):
        return TomorrowCloseGain.PENDING
    return EvaluateTomorrowCloseGain(
        Decimal(str(left.qfq_close_price)), Decimal(str(right.qfq_close_price)),
        next_raw_reference=None if right.raw_preclose is None else Decimal(str(right.raw_preclose)),
        next_raw_close=None if right.raw_close_price is None else Decimal(str(right.raw_close_price)),
    )


def _collect_samples(
    rows: Iterable[HistoryTrainingRow],
    calendar: tuple[date, ...],
    cap: int,
    check: Callable[[], None],
) -> _Samples:
    features: _FloatArray = np.empty((cap, len(TOMORROW_FEATURE_NAMES)), dtype=np.float64)
    labels: _FloatArray = np.empty(cap, dtype=np.float64)
    codes: _IntArray = np.empty(cap, dtype=np.int64)
    boards: _IntArray = np.empty(cap, dtype=np.int64)
    grouped = iter(groupby(rows, key=lambda item: item.trade_date))
    group = next(grouped, None)
    previous: tuple[HistoryTrainingRow, ...] = ()
    offsets = [0]
    written = pending = missing_features = 0
    for day_index, day in enumerate(calendar):
        check()
        current: dict[str, HistoryTrainingRow] = {}
        if group is not None and group[0] <= day:
            if group[0] != day:
                raise ValueError("tomorrow_history_stream_out_of_order")
            for row in group[1]:
                if row.code in current:
                    raise ValueError("tomorrow_history_duplicate_stock_day")
                current[row.code] = row
            group = next(grouped, None)
        if day_index:
            for row in previous:
                point = row.point
                if point is None or row.cell_status != "complete":
                    continue
                vector = BuildTomorrowFeatures(
                    point.raw_open, point.raw_high, point.raw_low, point.raw_close_price,
                    point.raw_volume, Board(row.board),
                )
                if vector is None:
                    missing_features += 1
                    continue
                outcome = _label(row, current.get(row.code))
                if written >= cap:
                    raise ValueError("tomorrow_history_population_exceeds_manifest")
                features[written] = vector
                labels[written] = np.nan if outcome is TomorrowCloseGain.PENDING else float(outcome is TomorrowCloseGain.HIT)
                pending += int(outcome is TomorrowCloseGain.PENDING)
                codes[written] = int(row.code)
                boards[written] = _BOARDS.index(row.board)
                written += 1
            offsets.append(written)
        previous = tuple(current.values())
    if group is not None:
        raise ValueError("tomorrow_history_stream_out_of_range")
    return _Samples(
        features[:written], labels[:written], codes[:written], boards[:written], tuple(offsets), pending, missing_features,
    )


def _require_labels(labels: _FloatArray) -> None:
    if not len(labels) or not np.any(labels > 0) or not np.any(labels == 0):
        raise ValueError("tomorrow_partition_requires_both_labels")


def _observed_rows(samples: _Samples, section: slice) -> tuple[_FloatArray, _FloatArray]:
    labels = samples.labels[section]
    observed = np.isfinite(labels)
    return samples.features[section][observed], labels[observed]


def _observed_dates(samples: _Samples, start_day: int, end_day: int) -> tuple[int, ...]:
    return tuple(
        day for day in range(start_day, end_day)
        if np.any(np.isfinite(samples.labels[samples.rows(day, day + 1)]))
    )


def _sigmoid(scores: _FloatArray) -> _FloatArray:
    return np.asarray(np.exp(-np.logaddexp(0.0, -scores)), dtype=np.float64)


def _fit_logistic(
    features: _FloatArray, labels: _FloatArray, ridge: float, check: Callable[[], None],
) -> tuple[float, _FloatArray]:
    _require_labels(labels)
    params: _FloatArray = np.zeros(features.shape[1] + 1, dtype=np.float64)
    positive = float(labels.mean())
    params[0] = math.log(positive / (1 - positive))
    design: _FloatArray = np.empty((len(labels), len(params)), dtype=np.float64)
    design[:, 0] = 1
    design[:, 1:] = features
    penalty = np.eye(len(params)) * ridge
    penalty[0, 0] = 1e-8

    def loss(parameters: _FloatArray) -> float:
        scores = design @ parameters
        return float(np.sum(np.logaddexp(0, scores) - labels * scores) + parameters @ penalty @ parameters / 2)

    for _iteration in range(30):
        check()
        fitted = _sigmoid(design @ params)
        variance = fitted * (1 - fitted)
        hessian = design.T @ (design * variance[:, None]) + penalty
        gradient = design.T @ (fitted - labels) + penalty @ params
        step = np.linalg.solve(hessian, gradient)
        if np.max(np.abs(step)) < 1e-7:
            break
        baseline = loss(params)
        for exponent in range(12):
            proposal = params - step / (2 ** exponent)
            if loss(proposal) <= baseline:
                params = proposal
                break
        else:
            raise ValueError("tomorrow_logistic_step_unstable")
    return float(params[0]), params[1:]


def _fit_linear(features: _FloatArray, labels: _FloatArray, check: Callable[[], None]) -> LogisticTomorrowModel:
    means = features.mean(axis=0) if len(features) else np.zeros(features.shape[1])
    scales = features.std(axis=0) if len(features) else np.ones(features.shape[1])
    scales = np.where(scales > 1e-9, scales, 1.0)
    intercept, weights = _fit_logistic((features - means) / scales, labels, 5.0, check)
    return LogisticTomorrowModel(intercept, tuple(weights), tuple(means), tuple(scales))


def _linear_scores(model: LogisticTomorrowModel, features: _FloatArray) -> _FloatArray:
    normalized = (features - np.asarray(model.means)) / np.asarray(model.scales)
    return np.asarray(normalized @ np.asarray(model.weights) + model.intercept, dtype=np.float64)


def _fit_tree(features: _FloatArray, labels: _FloatArray, check: Callable[[], None]) -> lgb.Booster:
    _require_labels(labels)

    def check_iteration(_environment: lgb.callback.CallbackEnv) -> None:
        check()

    return lgb.train(
        {
            "objective": "binary", "learning_rate": 0.05, "max_depth": 3, "num_leaves": 7,
            "min_data_in_leaf": 20, "max_bin": 63, "num_threads": 1, "seed": 0,
            "deterministic": True, "force_col_wise": True, "verbosity": -1,
        },
        lgb.Dataset(features, label=labels, feature_name=list(TOMORROW_FEATURE_NAMES)),
        num_boost_round=100,
        callbacks=[check_iteration],
    )


def _tree_scores(model: lgb.Booster, values: _FloatArray) -> _FloatArray:
    return np.asarray(model.predict(values, raw_score=True, num_threads=1), dtype=np.float64).reshape(-1)


def _seat_hits(
    scores: _FloatArray,
    samples: _Samples,
    start_day: int,
    end_day: int,
) -> tuple[int, int, int]:
    hits = seats = unsettled = 0
    base = samples.offsets[start_day]
    for day in range(start_day, end_day):
        section = samples.rows(day, day + 1)
        assert isinstance(section.start, int) and isinstance(section.stop, int)
        day_scores = scores[section.start - base : section.stop - base]
        rank = np.lexsort((samples.codes[section], -day_scores))
        counts = [0] * len(_BOARDS)
        selected = 0
        for relative in rank:
            index = section.start + relative
            board = samples.boards[index]
            if counts[board] == _BOARD_SEATS:
                continue
            counts[board] += 1
            hits += int(samples.labels[index] == 1)
            unsettled += int(not math.isfinite(samples.labels[index]))
            selected += 1
            if selected == _SEATS:
                break
        seats += selected
    return hits, seats, unsettled


def _compare(
    samples: _Samples, selection_end: int, check: Callable[[], None],
) -> tuple[TomorrowProbabilitySelection, bool]:
    rule_hits = linear_hits = tree_hits = seats = 0
    rule_unsettled = linear_unsettled = tree_unsettled = 0
    tree_never_worse = True
    for offset in reversed(range(_FOLDS)):
        end_day = selection_end - offset * _FOLD_SESSIONS
        start_day = end_day - _FOLD_SESSIONS
        fitting_end = start_day - _EMBARGO_SESSIONS
        if len(_observed_dates(samples, 0, fitting_end)) < _MIN_FITTING_SESSIONS:
            raise ValueError("tomorrow_fold_training_dates_insufficient")
        if len(_observed_dates(samples, start_day, end_day)) != _FOLD_SESSIONS:
            raise ValueError("tomorrow_comparison_dates_incomplete")
        fitting = samples.rows(0, fitting_end)
        comparison = samples.rows(start_day, end_day)
        features, labels = _observed_rows(samples, fitting)
        values = samples.features[comparison]
        _require_labels(samples.labels[comparison])
        linear = _fit_linear(features, labels, check)
        tree = _fit_tree(features, labels, check)
        rh, count, ru = _seat_hits(values[:, 0], samples, start_day, end_day)
        lh, _, lu = _seat_hits(_linear_scores(linear, values), samples, start_day, end_day)
        th, _, tu = _seat_hits(_tree_scores(tree, values), samples, start_day, end_day)
        rule_hits += rh
        linear_hits += lh
        tree_hits += th
        seats += count
        rule_unsettled += ru
        linear_unsettled += lu
        tree_unsettled += tu
        tree_never_worse &= th >= lh + lu
    summary = TomorrowProbabilitySelection(
        rule_hits, linear_hits, tree_hits, seats, rule_unsettled, linear_unsettled, tree_unsettled,
    )
    return summary, tree_never_worse and tree_hits > linear_hits + linear_unsettled


def _train(
    samples: _Samples, signal_dates: tuple[date, ...], snapshot_hash: str, model_root: Path,
    check: Callable[[], None],
) -> TomorrowProbabilityTrainingResult:
    assessment_begin = len(signal_dates) - _ASSESSMENT_SESSIONS
    calibration_end = assessment_begin - _EMBARGO_SESSIONS
    calibration_begin = calibration_end - _CALIBRATION_SESSIONS
    fitting_end = calibration_begin - _EMBARGO_SESSIONS
    calibrated = _observed_dates(samples, calibration_begin, calibration_end)
    if len(calibrated) != _CALIBRATION_SESSIONS:
        raise ValueError("tomorrow_calibration_dates_incomplete")
    if len(_observed_dates(samples, assessment_begin, len(signal_dates))) != _ASSESSMENT_SESSIONS:
        raise ValueError("tomorrow_assessment_dates_incomplete")
    selection, select_tree = _compare(samples, fitting_end, check)
    fitting = samples.rows(0, fitting_end)
    calibration = samples.rows(calibration_begin, calibration_end)
    assessment = samples.rows(assessment_begin, len(signal_dates))
    features, labels = _observed_rows(samples, fitting)
    linear = None if select_tree else _fit_linear(features, labels, check)
    tree = _fit_tree(features, labels, check) if select_tree else None

    def scores(values: _FloatArray) -> _FloatArray:
        if linear is not None:
            return _linear_scores(linear, values)
        assert tree is not None
        return _tree_scores(tree, values)

    calibration_rows, calibration_labels = _observed_rows(samples, calibration)
    _require_labels(calibration_labels)
    calibration_scores = scores(calibration_rows)
    intercept, weights = _fit_logistic(calibration_scores[:, None], calibration_labels, 0.001, check)
    slope = float(weights[0])
    if not math.isfinite(slope) or slope <= 0:
        raise ValueError("tomorrow_calibration_not_monotone")
    assessment_labels_all = samples.labels[assessment]
    assessed = np.isfinite(assessment_labels_all)
    assessment_labels = assessment_labels_all[assessed]
    _require_labels(assessment_labels)
    probabilities = _sigmoid(intercept + slope * scores(samples.features[assessment]))
    hits, seats, unsettled = _seat_hits(probabilities, samples, assessment_begin, len(signal_dates))
    fitted_dates = tuple(signal_dates[index] for index in _observed_dates(samples, 0, fitting_end))
    calibrated_dates = tuple(signal_dates[index] for index in calibrated)
    artifact = TomorrowProbabilityArtifact(
        snapshot_hash, fitted_dates, calibrated_dates,
        intercept, slope, linear, None if tree is None else tree.model_to_string(),
    )
    check()
    identity = PublishTomorrowProbabilityModel(model_root, artifact)
    return TomorrowProbabilityTrainingResult(
        snapshot_hash, identity, artifact.algorithm,
        selection=selection, assessment_rows=len(assessment_labels),
        assessment_positive_rows=int(assessment_labels.sum()),
        assessment_brier=float(np.mean(np.square(probabilities[assessed] - assessment_labels))),
        assessment_hits=hits, assessment_seats=seats, assessment_unsettled=unsettled,
        pending_labels=samples.pending, missing_features=samples.missing_features,
    )


__all__ = [
    "OpenTomorrowHistoryInput", "SharedTomorrowTrainer", "TomorrowHistoryInput", "TrainSharedTomorrowProbability",
]

"""Fit the four fixed next-close event models with bounded sample sharing."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Generator, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import partial
from importlib.util import find_spec
from itertools import groupby
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from trader.download.domain.history_control import HistoryPublishedControlState
from trader.download.domain.history_revision import HistoryTrainingRow
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader, HistorySnapshotReadError
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.tomorrow_probability import (
    BuildTomorrowFeatures,
    BuildTomorrowSequenceFeatures,
    TOMORROW_FEATURE_NAMES,
)
from trader.recommendation.domain.market.v5.features import (
    PriceVolumePoint,
    SEQUENCE_STEPS,
)
from trader.recommendation.domain.outcomes.prediction_target import EvaluatePredictionTarget, PredictionTarget, PredictionTargets
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingMemoryAdmission
from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingProgressState,
    TomorrowTrainingStage,
    TrainingSampleProgress,
    TrainingValidationProgress,
)
from trader.training.domain.tomorrow_probability import TomorrowProbabilityProfileResult, TomorrowProbabilityTrainingResult
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.logistic_fitting import FitBinaryLogistic
from trader.training.infra.fitting_settings import LightGBMFittingParameters, RidgeFittingParameters
from trader.training.infra.ranking import DailyBinaryRankRows
from trader.training.infra.tcn.settings import TcnFittingParameters
from trader.training.infra.tomorrow.artifact import (
    LoadTomorrowProbabilityModel,
    PublishTomorrowProbabilityModel,
    TomorrowProbabilityArtifact,
    TomorrowFeatureNames,
)
from trader.training.infra.tomorrow.settings import LoadTomorrowFitSettings, TomorrowFitSettings
from trader.training.infra.tomorrow.tree import TomorrowTreeScores
from trader.training.infra.tomorrow.v1.model import FitTomorrowRanker
from trader.training.infra.tomorrow.v2.model import FitTomorrowClassifier
from trader.training.infra.tomorrow.v3.model import FitTomorrowLogistic, LogisticTomorrowModel, TomorrowLogisticScores
from trader.training.infra.tomorrow.v5.model import FitTomorrowTcn, TomorrowTcnModel
from trader.training.infra.tomorrow.v5.samples import AppendTomorrowSequencePoints, CompletedTomorrowSequence

_CALIBRATION_SESSIONS = 20
_ASSESSMENT_SESSIONS = 20
_EMBARGO_SESSIONS = 5
_MIN_FITTING_SESSIONS = 60
_BOARD_SEATS = 4
_SEATS = 6
_BOARDS = ("main", "chinext", "star")
_FloatArray: TypeAlias = NDArray[np.float64]
_IntArray: TypeAlias = NDArray[np.int64]
_ProfileProgress = Callable[[ScoringProfileId, TomorrowTrainingProgress], None]


@dataclass(slots=True)
class _Progress:
    notify: Callable[[TomorrowTrainingProgress], None] | None
    latest: TomorrowTrainingProgress | None = None

    def publish(self, event: TomorrowTrainingProgress) -> None:
        self.latest = event
        if self.notify is not None:
            self.notify(event)

    def stage(
        self, stage: TomorrowTrainingStage, state: TomorrowTrainingProgressState, *, validation_rows: int | None = None,
    ) -> None:
        self.publish(TomorrowTrainingProgress(
            stage, state, int(state == "completed"), 1, strategy=Strategy.TOMORROW,
            validation=(
                TrainingValidationProgress(validation_rows if state == "completed" else 0, validation_rows)
                if validation_rows is not None else None
            ),
        ))

    def fail(self) -> None:
        if self.latest is not None and self.latest.state in {"started", "running"}:
            self.publish(replace(self.latest, state="failed"))


@dataclass(frozen=True, slots=True)
class _Samples:
    features: np.ndarray
    labels: _FloatArray
    codes: _IntArray
    boards: _IntArray
    offsets: tuple[int, ...]
    pending: int
    missing_features: int

    def rows(self, start_day: int, end_day: int) -> slice:
        return slice(self.offsets[start_day], self.offsets[end_day])


@dataclass(frozen=True, slots=True)
class _FitDates:
    fitting: int
    early: int = 0
    calibration: int = _CALIBRATION_SESSIONS
    assessment: int = _ASSESSMENT_SESSIONS
    embargo: int = _EMBARGO_SESSIONS

    @property
    def reserve(self) -> int:
        return self.early + self.calibration + self.assessment + self.embargo * (3 if self.early else 2)


@dataclass(frozen=True, slots=True)
class _PendingFit:
    target: PredictionTarget
    settings: TomorrowFitSettings
    dates: _FitDates

    @property
    def profile(self) -> ScoringProfileId:
        return self.settings.profile


def _fit_dates(settings: TomorrowFitSettings) -> _FitDates:
    parameters = settings.parameters
    if isinstance(parameters, V1TrainingSettings):
        return _FitDates(
            settings.fit_dates, parameters.early_stopping_dates, parameters.calibration_dates,
            parameters.validation_dates, parameters.embargo_dates,
        )
    return _FitDates(settings.fit_dates)


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


class TomorrowTrainer(Protocol):
    def __call__(
        self,
        history: TomorrowHistoryInput,
        model_root: Path,
        *,
        profiles: tuple[ScoringProfileId, ...],
        prediction_targets: PredictionTargets,
        project_root: Path,
        cancelled: Callable[[], bool],
        cpu_budget: TrainingCpuBudget,
        memory: TrainingMemoryAdmission,
        progress: _ProfileProgress | None = None,
    ) -> tuple[TomorrowProbabilityProfileResult, ...]: ...


def TrainTomorrowProbability(
    history: TomorrowHistoryInput,
    model_root: Path,
    *,
    profiles: tuple[ScoringProfileId, ...],
    prediction_targets: PredictionTargets,
    project_root: Path,
    cancelled: Callable[[], bool],
    cpu_budget: TrainingCpuBudget,
    memory: TrainingMemoryAdmission,
    progress: _ProfileProgress | None = None,
) -> tuple[TomorrowProbabilityProfileResult, ...]:
    def check() -> None:
        if cancelled():
            raise InterruptedError("tomorrow_probability_training_cancelled")

    results: dict[ScoringProfileId, TomorrowProbabilityProfileResult] = {}
    pending: dict[tuple[PredictionTarget, bool], list[_PendingFit]] = {}
    reporters = {profile: _Progress(partial(progress, profile) if progress is not None else None) for profile in profiles}
    for profile in profiles:
        check()
        reporter = reporters[profile]
        reporter.stage("training_preparation", "started")
        target = prediction_targets.for_profile(profile, Strategy.TOMORROW)
        try:
            current = LoadTomorrowProbabilityModel(model_root, profile, target, project_root=project_root)
        except (OSError, RuntimeError, ValueError):
            current = None
        if current is not None and current.artifact.history_snapshot_hash == history.snapshot_hash:
            result = TomorrowProbabilityTrainingResult(history.snapshot_hash, current.identity, current.artifact.algorithm)
            results[profile] = TomorrowProbabilityProfileResult(target, profile, result)
            reporter.stage("training_preparation", "completed")
            reporter.stage("model_reuse", "completed")
        elif profile == "v5" and find_spec("torch") is None:
            results[profile] = TomorrowProbabilityProfileResult(
                target, profile, _failed(history.snapshot_hash, "tomorrow_v5_tensor_dependency_unavailable"),
            )
            reporter.fail()
        else:
            try:
                settings = LoadTomorrowFitSettings(project_root, profile)
                request = _PendingFit(target, settings, _fit_dates(settings))
            except (OSError, ValueError) as exc:
                result = _training_failure(history.snapshot_hash, exc)
                results[profile] = TomorrowProbabilityProfileResult(target, profile, result)
                reporter.fail()
            else:
                pending.setdefault((target, profile == "v5"), []).append(request)

    for requests in pending.values():
        group_reporters = tuple(reporters[item.profile] for item in requests)
        for head in _train_sample_group(history, model_root, tuple(requests), cpu_budget, memory, check, group_reporters):
            results[head.profile] = head
    return tuple(results[profile] for profile in profiles)


def _train_sample_group(
    history: TomorrowHistoryInput, model_root: Path, requests: tuple[_PendingFit, ...],
    cpu_budget: TrainingCpuBudget, memory: TrainingMemoryAdmission, check: Callable[[], None],
    reporters: tuple[_Progress, ...],
) -> tuple[TomorrowProbabilityProfileResult, ...]:
    def publish_samples(event: TomorrowTrainingProgress) -> None:
        for reporter in reporters:
            reporter.publish(event)

    target = requests[0].target
    sequence = requests[0].profile == "v5"
    warmup = SEQUENCE_STEPS + 1 if sequence else 0
    limit = max(item.dates.fitting + item.dates.reserve for item in requests)
    calendar = history.open_dates[-(limit + warmup + 1):]
    cap = max(0, len(calendar) - warmup - 1) * history.universe_count
    if sequence:
        width = len(TomorrowFeatureNames("v5"))
        # Float32 matrix plus one filtered fitting copy and a bounded universe history buffer.
        required_bytes = 2 * cap * (4 * width + 32) + warmup * history.universe_count * 160 + 64 * 1024**2
    else:
        # Packed samples, normalized design, IRLS scratch and the native tree dataset.
        required_bytes = max(1, cap * (5 * len(TOMORROW_FEATURE_NAMES) + 15) * 8)
    try:
        check()
        if not cap:
            raise ValueError("tomorrow_insufficient_training_dates")
        with (
            memory.reserve(max(1, required_bytes), check),
            cpu_budget.reserve(cpu_budget.native_threads, check),
            cpu_budget.native_runtime(),
        ):
            for reporter in reporters:
                reporter.stage("training_preparation", "completed")
            with closing(history.iter_training_rows(calendar)) as rows:
                samples = _collect_samples(
                    rows, calendar, cap, check, target, warmup=warmup, source_identity=history.snapshot_hash,
                    progress=publish_samples if any(reporter.notify is not None for reporter in reporters) else None,
                )
            return tuple(_fit_pending(
                item, samples, calendar[warmup:-1], history.snapshot_hash, model_root, check,
                cpu_budget.native_threads, reporter,
            ) for item, reporter in zip(requests, reporters, strict=True))
    except (OSError, ValueError, HistorySnapshotReadError, lgb.basic.LightGBMError, np.linalg.LinAlgError) as exc:
        result = _training_failure(history.snapshot_hash, exc)
        for reporter in reporters:
            reporter.fail()
        return tuple(TomorrowProbabilityProfileResult(item.target, item.profile, result) for item in requests)


def _training_failure(snapshot_hash: str, exc: Exception) -> TomorrowProbabilityTrainingResult:
    if isinstance(exc, InterruptedError):
        raise exc
    detail = str(exc)
    if detail.startswith("training_memory_request_exceeds_budget:"):
        reason = "tomorrow_probability_memory_budget_insufficient"
    else:
        reason = detail if isinstance(exc, ValueError) and detail.startswith("tomorrow_") else type(exc).__name__
    return _failed(snapshot_hash, reason)


def _failed(snapshot_hash: str, reason: str) -> TomorrowProbabilityTrainingResult:
    return TomorrowProbabilityTrainingResult(snapshot_hash, None, None, failure=reason)


def _label(
    current: HistoryTrainingRow, following: HistoryTrainingRow | None, target: PredictionTarget,
) -> TomorrowCloseGain:
    left = current.point
    if following is None or left is None or left.raw_close_price is None:
        return TomorrowCloseGain.PENDING
    if following.cell_status == "supplier_marked_suspended":
        return EvaluatePredictionTarget(target, Decimal(str(left.raw_close_price)), (None,), confirmed_suspended=True)
    right = following.point
    if (
        right is None or following.cell_status != "complete"
        or left.qfq_close_price is None or right.qfq_close_price is None
        or left.qfq_reconstruction_anchor is not None or right.qfq_reconstruction_anchor is not None
    ):
        return TomorrowCloseGain.PENDING
    return EvaluatePredictionTarget(
        target, Decimal(str(left.qfq_close_price)), (Decimal(str(right.qfq_close_price)),),
        next_raw_reference=None if right.raw_preclose is None else Decimal(str(right.raw_preclose)),
        next_raw_close=None if right.raw_close_price is None else Decimal(str(right.raw_close_price)),
    )


def _collect_samples(
    rows: Iterable[HistoryTrainingRow],
    calendar: tuple[date, ...],
    cap: int,
    check: Callable[[], None],
    target: PredictionTarget,
    *,
    warmup: int = 0,
    source_identity: str = "",
    progress: Callable[[TomorrowTrainingProgress], None] | None = None,
) -> _Samples:
    def report(state: TomorrowTrainingProgressState, phase: Literal["warmup", "generation"]) -> None:
        if progress is not None:
            sample_progress = TrainingSampleProgress(
                read_days, len(calendar), processed_rows, warmup_rows, phase, pending,
            )
            progress(TomorrowTrainingProgress(
                "history_conversion", state, read_days, len(calendar), written,
                Strategy.TOMORROW, sample_progress=sample_progress,
            ))

    read_days = processed_rows = warmup_rows = 0
    written = pending = missing_features = 0
    report("started", "warmup" if warmup else "generation")
    columns = TomorrowFeatureNames("v5" if warmup else "v3")
    features = np.empty((cap, len(columns)), dtype=np.float32 if warmup else np.float64)
    labels: _FloatArray = np.empty(cap, dtype=np.float64)
    codes: _IntArray = np.empty(cap, dtype=np.int64)
    boards: _IntArray = np.empty(cap, dtype=np.int64)
    grouped = iter(groupby(rows, key=lambda item: item.trade_date))
    group = next(grouped, None)
    previous: tuple[HistoryTrainingRow, ...] = ()
    rolling: dict[str, deque[PriceVolumePoint]] = {}
    offsets = [0]
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
        if day_index > warmup:
            sequence_dates = calendar[day_index - warmup - 1:day_index - 1] if warmup else ()
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
                if warmup:
                    sequence = CompletedTomorrowSequence(rolling.get(row.code), sequence_dates, row.code, source_identity)
                    vector = (
                        BuildTomorrowSequenceFeatures(vector, sequence, signal_date=row.trade_date)
                        if sequence is not None else None
                    )
                    if vector is None:
                        missing_features += 1
                        continue
                outcome = _label(row, current.get(row.code), target)
                if written >= cap:
                    raise ValueError("tomorrow_history_population_exceeds_manifest")
                features[written] = vector
                labels[written] = np.nan if outcome is TomorrowCloseGain.PENDING else float(outcome is TomorrowCloseGain.HIT)
                pending += int(outcome is TomorrowCloseGain.PENDING)
                codes[written] = int(row.code)
                boards[written] = _BOARDS.index(row.board)
                written += 1
            offsets.append(written)
        if warmup:
            AppendTomorrowSequencePoints(rolling, previous, warmup)
        previous = tuple(current.values())
        if progress is not None:
            read_days += bool(current)
            processed_rows += len(current)
            if day_index < warmup:
                warmup_rows += len(current)
            report("running", "warmup" if warmup and day_index <= warmup else "generation")
    if group is not None:
        raise ValueError("tomorrow_history_stream_out_of_range")
    report("completed", "warmup" if warmup and len(calendar) <= warmup + 1 else "generation")
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


@dataclass(frozen=True, slots=True)
class _FittedHead:
    scores: Callable[[np.ndarray], _FloatArray]
    logistic: LogisticTomorrowModel | None = None
    tree: str | None = None
    tcn: TomorrowTcnModel | None = None


def _rank_rows(samples: _Samples, start: int, end: int) -> DailyBinaryRankRows:
    features, labels = _observed_rows(samples, samples.rows(start, end))
    groups = tuple(
        count for day in range(start, end)
        if (count := int(np.isfinite(samples.labels[samples.rows(day, day + 1)]).sum())) > 0
    )
    return DailyBinaryRankRows(features, labels, groups)


def _fit_head(
    samples: _Samples, fitting: tuple[int, int], early: tuple[int, int],
    settings: TomorrowFitSettings, check: Callable[[], None], threads: int,
) -> _FittedHead:
    parameters = settings.parameters
    if isinstance(parameters, V1TrainingSettings):
        fit_rows = _rank_rows(samples, *fitting)
        early_rows = _rank_rows(samples, *early)
        tree = FitTomorrowRanker(fit_rows, early_rows, parameters, check, threads)
        return _FittedHead(lambda values: TomorrowTreeScores(tree, values), tree=tree.model_to_string())

    features, labels = _observed_rows(samples, samples.rows(*fitting))
    _require_labels(labels)
    if isinstance(parameters, LightGBMFittingParameters):
        tree = FitTomorrowClassifier(features, labels, parameters, threads, check)
        return _FittedHead(lambda values: TomorrowTreeScores(tree, values), tree=tree.model_to_string())
    if isinstance(parameters, RidgeFittingParameters):
        linear = FitTomorrowLogistic(features, labels, parameters.penalty, check)
        return _FittedHead(lambda values: TomorrowLogisticScores(linear, values), logistic=linear)

    model, fitted = FitTomorrowTcn(features, labels, parameters, check, threads)
    return _FittedHead(fitted.logits, tcn=model)


def _fit_pending(
    request: _PendingFit, samples: _Samples, signal_dates: tuple[date, ...], snapshot_hash: str,
    model_root: Path, check: Callable[[], None], threads: int, progress: _Progress,
) -> TomorrowProbabilityProfileResult:
    try:
        progress.stage("model_fit", "started")
        if isinstance(request.settings.parameters, TcnFittingParameters):
            from trader.training.infra.tcn.network import PreserveTcnRuntime

            with PreserveTcnRuntime():
                result = _train(request, samples, signal_dates, snapshot_hash, model_root, check, threads, progress)
        else:
            result = _train(request, samples, signal_dates, snapshot_hash, model_root, check, threads, progress)
    except (ImportError, OSError, ValueError, RuntimeError, lgb.basic.LightGBMError, np.linalg.LinAlgError) as exc:
        result = _training_failure(snapshot_hash, exc)
        progress.fail()
    return TomorrowProbabilityProfileResult(request.target, request.profile, result)


def _train(
    request: _PendingFit, samples: _Samples, signal_dates: tuple[date, ...], snapshot_hash: str,
    model_root: Path, check: Callable[[], None], threads: int, progress: _Progress,
) -> TomorrowProbabilityTrainingResult:
    dates = request.dates
    assessment_begin = len(signal_dates) - dates.assessment
    calibration_end = assessment_begin - dates.embargo
    calibration_begin = calibration_end - dates.calibration
    early_end = calibration_begin - dates.embargo
    early_begin = early_end - dates.early
    fitting_end = early_begin - (dates.embargo if dates.early else 0)
    fitting_begin = max(0, fitting_end - dates.fitting)
    minimum = min(dates.fitting, _MIN_FITTING_SESSIONS)
    if fitting_end - fitting_begin < minimum or len(_observed_dates(samples, fitting_begin, fitting_end)) < minimum:
        raise ValueError("tomorrow_insufficient_training_dates")
    if dates.early and len(_observed_dates(samples, early_begin, early_end)) != dates.early:
        raise ValueError("tomorrow_early_stopping_dates_incomplete")
    calibrated = _observed_dates(samples, calibration_begin, calibration_end)
    if len(calibrated) != dates.calibration:
        raise ValueError("tomorrow_calibration_dates_incomplete")
    if len(_observed_dates(samples, assessment_begin, len(signal_dates))) != dates.assessment:
        raise ValueError("tomorrow_assessment_dates_incomplete")
    calibration = samples.rows(calibration_begin, calibration_end)
    assessment = samples.rows(assessment_begin, len(signal_dates))
    fitted = _fit_head(samples, (fitting_begin, fitting_end), (early_begin, early_end), request.settings, check, threads)
    progress.stage("model_fit", "completed")
    progress.stage("model_calibration", "started")
    calibration_rows, calibration_labels = _observed_rows(samples, calibration)
    _require_labels(calibration_labels)
    calibration_scores = fitted.scores(calibration_rows)
    intercept, weights = FitBinaryLogistic(calibration_scores[:, None], calibration_labels, 0.001, check)
    slope = float(weights[0])
    if not math.isfinite(slope) or slope <= 0:
        raise ValueError("tomorrow_calibration_not_monotone")
    progress.stage("model_calibration", "completed")
    assessment_labels_all = samples.labels[assessment]
    progress.stage("model_evaluation", "started", validation_rows=len(assessment_labels_all))
    assessed = np.isfinite(assessment_labels_all)
    assessment_labels = assessment_labels_all[assessed]
    _require_labels(assessment_labels)
    probabilities = _sigmoid(intercept + slope * fitted.scores(samples.features[assessment]))
    hits, seats, unsettled = _seat_hits(probabilities, samples, assessment_begin, len(signal_dates))
    progress.stage("model_evaluation", "completed", validation_rows=len(assessment_labels_all))
    fitted_dates = tuple(signal_dates[index] for index in (
        *_observed_dates(samples, fitting_begin, fitting_end), *_observed_dates(samples, early_begin, early_end),
    ))
    calibrated_dates = tuple(signal_dates[index] for index in calibrated)
    artifact = TomorrowProbabilityArtifact(
        request.profile, request.target, snapshot_hash, request.settings.identity, fitted_dates, calibrated_dates,
        intercept, slope, fitted.logistic, fitted.tree, fitted.tcn,
    )
    check()
    progress.stage("artifact_publish", "started")
    identity = PublishTomorrowProbabilityModel(model_root, artifact)
    progress.stage("artifact_publish", "completed")
    return TomorrowProbabilityTrainingResult(
        snapshot_hash, identity, artifact.algorithm,
        assessment_rows=len(assessment_labels),
        assessment_positive_rows=int(assessment_labels.sum()),
        assessment_brier=float(np.mean(np.square(probabilities[assessed] - assessment_labels))),
        assessment_hits=hits, assessment_seats=seats, assessment_unsettled=unsettled,
        pending_labels=samples.pending, missing_features=samples.missing_features,
    )


__all__ = [
    "OpenTomorrowHistoryInput", "TomorrowTrainer", "TomorrowHistoryInput", "TrainTomorrowProbability",
]

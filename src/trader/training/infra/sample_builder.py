"""Stream one verified history scan into a profile-scoped multi-target sample database."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Protocol, cast

from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.market.feature_contracts import (
    CalculateProfileQfqAlpha,
    QfqPriceAnchors,
)
from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint
from trader.recommendation.domain.scoring.residualization import (
    TRAINED_HEAD_EXPOSURE_CONTRACT,
    CreateExposureContext,
    ResidualizeExposureWithContext,
)
from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingWindow,
    TrainingSampleDiagnostics,
)
from trader.training.domain.label_benchmark import ObservedTrainingBenchmark
from trader.training.infra.history.aligned_training_rows import AlignmentReadDates, AlignTrainingRows
from trader.training.infra.history.history_training_input import HistoryTrainingInputError, HistoryTrainingInputSnapshot
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.sample_index import (
    SQLiteTrainingSampleIndex,
    TrainingSample,
)


class TrainingHistorySource(Protocol):
    snapshot: HistoryTrainingInputSnapshot

    def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int: ...

    def has_industry_window(
        self, allowed_dates: frozenset[date], required_sessions: int, *, anchor_dates: frozenset[date] | None = None
    ) -> bool: ...

    def iter_training_rows(
        self,
        allowed_dates: frozenset[date],
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[HistoryTrainingRow]: ...


@dataclass(frozen=True)
class TrainingSampleBuildRequest:
    archive: TrainingHistorySource
    codes: tuple[str, ...]
    window: TomorrowTrainingWindow
    repository: SQLiteTrainingSampleIndex
    profile: TrainedProfileContract
    progress: TomorrowTrainingProgressPort | None = None


@dataclass
class _PendingSample:
    code: str
    trade_date: date
    calendar_position: int
    board: str
    industry: str
    average_amount_20d: float
    features: tuple[float, ...]
    close: float
    future_returns: list[float | None] = field(default_factory=lambda: [None] * 5)


@dataclass
class _PendingDay:
    trade_date: date
    calendar_position: int
    samples: dict[str, _PendingSample]


@dataclass
class _RollingCodeState:
    points: deque[HistoryTrainingPoint]
    previous_position: int | None = None


@dataclass
class _SampleBuildState:
    repository: SQLiteTrainingSampleIndex
    profile: TrainedProfileContract
    progress: TomorrowTrainingProgressPort | None
    total_rows: int
    calendar_positions: dict[date, int]
    readable_dates: frozenset[date]
    allowed_codes: frozenset[str]
    converted_samples: int = 0
    processed_rows: int = 0
    pending_days: list[_PendingDay] = field(default_factory=list)
    rolling_codes: dict[str, _RollingCodeState] = field(default_factory=dict)
    current_day: date | None = None
    current_position: int = -1
    missing_industry: int = 0
    missing_prices: int = 0
    short_window: int = 0
    invalid_features: int = 0
    missing_labels: int = 0
    suspended_sessions: int = 0
    estimated_sessions: int = 0
    missing_t1: int = 0
    missing_d25: int = 0

    def report_scanned(self, value: int) -> None:
        self.processed_rows = value
        _publish(
            self.progress,
            TomorrowTrainingProgress("history_conversion", "running", value, self.total_rows, self.converted_samples),
        )

    def accept(self, row: HistoryTrainingRow) -> None:
        code = row.code
        if code not in self.allowed_codes:
            raise HistoryTrainingInputError("history_code_outside_active_universe")
        position = self.calendar_positions[row.trade_date]
        self._advance_day(row.trade_date, position)
        rolling = self._rolling_for(code, position)
        if row.point is not None:
            self.suspended_sessions += row.cell_status == "supplier_marked_suspended"
            self.estimated_sessions += row.cell_status in {"unknown_missing", "tencent_inferred_suspended"}
        if row.point is not None and row.cell_status in {None, "complete", "supplier_marked_suspended"}:
            self._record_future_close(code, row.point, position)
        if row.point is None:
            rolling.points.clear()
            self.missing_prices += 1
        else:
            rolling.points.append(row.point)
            if row.cell_status not in {None, "complete"}:
                rolling.previous_position = position
                return
            if row.industry is None:
                self.missing_industry += 1
            elif len(rolling.points) == self.profile.history_sessions:
                candidate = _pending_sample(row, rolling.points, position, self.readable_dates, self.profile)
                if candidate is not None:
                    self.pending_days[-1].samples[code] = candidate
                else:
                    self.invalid_features += 1
            else:
                self.short_window += 1
        rolling.previous_position = position

    def _advance_day(self, trade_date: date, position: int) -> None:
        if self.current_day is not None and trade_date != self.current_day:
            self.flush_matured(self.current_position)
        if trade_date != self.current_day:
            self.current_day = trade_date
            self.current_position = position
            self.pending_days.append(_PendingDay(trade_date, position, {}))

    def _rolling_for(self, code: str, position: int) -> _RollingCodeState:
        rolling = self.rolling_codes.setdefault(
            code,
            _RollingCodeState(deque(maxlen=self.profile.history_sessions)),
        )
        if rolling.previous_position is not None and position != rolling.previous_position + 1:
            rolling.points.clear()
        return rolling

    def _record_future_close(self, code: str, point: HistoryTrainingPoint, position: int) -> None:
        close = _eligible_future_close(point)
        if close is None:
            return
        for pending_day in self.pending_days[:-1]:
            horizon = position - pending_day.calendar_position
            candidate = pending_day.samples.get(code)
            if candidate is not None and 1 <= horizon <= 5:
                candidate.future_returns[horizon - 1] = close / candidate.close - 1.0

    def flush_matured(self, last_position: int, *, all_days: bool = False) -> None:
        while self.pending_days and (all_days or last_position - self.pending_days[0].calendar_position >= 5):
            pending = self.pending_days.pop(0)
            self.repository.record_day(
                pending.trade_date,
                len(pending.samples),
                tuple(
                    sum(item.future_returns[horizon] is None for item in pending.samples.values())
                    for horizon in range(5)
                ),
            )
            finalized = _finalize_sample_day(tuple(pending.samples.values()), self.profile)
            self.missing_labels += len(pending.samples) - len(finalized)
            self.missing_t1 += len(pending.samples) - sum(item.targets[0] is not None for item in finalized)
            self.missing_d25 += len(pending.samples) - sum(item.targets[-1] is not None for item in finalized)
            self.repository.add_final(finalized)
            self.converted_samples += len(finalized)
            _publish(
                self.progress,
                TomorrowTrainingProgress(
                    "cross_section_conversion",
                    "running",
                    self.converted_samples,
                    self.total_rows,
                    self.converted_samples,
                ),
            )


def BuildTrainingSamples(
    request: TrainingSampleBuildRequest, *, aligned_rows: Iterator[HistoryTrainingRow] | None = None
) -> None:
    """Build T+1..T+5 targets with one scan and at most five pending sample days."""

    state = _new_sample_state(request)
    total_rows = state.total_rows
    _publish(request.progress, TomorrowTrainingProgress("history_conversion", "started", 0, total_rows))
    _publish(request.progress, TomorrowTrainingProgress("cross_section_conversion", "started", 0, total_rows))

    read_dates = AlignmentReadDates(request.archive.snapshot.calendar.open_dates, request.window.readable_dates)
    rows = (
        aligned_rows
        if aligned_rows is not None
        else AlignTrainingRows(
            request.archive.iter_training_rows(read_dates, state.report_scanned),
            tuple(sorted(read_dates)),
            request.archive.snapshot.listing_intervals,
        )
    )
    for row in rows:
        if row.trade_date in request.window.readable_dates:
            state.accept(row)
            if aligned_rows is not None:
                state.processed_rows += 1
                if state.processed_rows % 4096 == 0:
                    state.report_scanned(state.processed_rows)

    _complete_sample_state(state, request)


def _new_sample_state(request: TrainingSampleBuildRequest) -> _SampleBuildState:
    calendar = request.archive.snapshot.calendar.open_dates
    return _SampleBuildState(
        request.repository,
        request.profile,
        request.progress,
        request.archive.training_row_upper_bound(AlignmentReadDates(calendar, request.window.readable_dates)),
        {day: position for position, day in enumerate(calendar)},
        request.window.readable_dates,
        frozenset(request.codes),
    )


def _complete_sample_state(state: _SampleBuildState, request: TrainingSampleBuildRequest) -> None:
    state.flush_matured(state.current_position, all_days=True)
    _publish(
        request.progress,
        TomorrowTrainingProgress(
            "history_conversion",
            "completed",
            state.processed_rows,
            state.processed_rows,
            state.converted_samples,
            sample_diagnostics=TrainingSampleDiagnostics(
                state.missing_industry,
                state.missing_prices,
                state.short_window,
                state.invalid_features,
                state.missing_labels,
                state.suspended_sessions,
                state.estimated_sessions,
                state.missing_t1,
                state.missing_d25,
            ),
        ),
    )
    _publish(
        request.progress,
        TomorrowTrainingProgress(
            "cross_section_conversion",
            "completed",
            state.converted_samples,
            state.converted_samples,
            state.converted_samples,
        ),
    )
    _publish(request.progress, TomorrowTrainingProgress("sample_index", "started", 0, 1))
    request.repository.prepare_for_model_fitting(request.window.split)
    _publish(request.progress, TomorrowTrainingProgress("sample_index", "completed", 1, 1))


def _eligible_future_close(point: HistoryTrainingPoint) -> float | None:
    if point.qfq_close_price is None or not math.isfinite(point.qfq_close_price) or point.qfq_close_price <= 0.0:
        return None
    return float(point.qfq_close_price)


def _pending_sample(
    row: HistoryTrainingRow,
    points: deque[HistoryTrainingPoint],
    calendar_position: int,
    readable_dates: frozenset[date],
    profile: TrainedProfileContract,
) -> _PendingSample | None:
    if points[0].trade_date not in readable_dates or row.trade_date not in readable_dates:
        return None
    current = points[-1]
    close = _eligible_future_close(current)
    if close is None:
        return None
    amounts = tuple(points[index].qfq_amount for index in range(len(points) - 20, len(points)))
    if any(amount is None or not math.isfinite(amount) or amount < 0.0 for amount in amounts):
        return None
    if math.fsum(cast(float, amount) for amount in amounts) <= 0.0 or (current.qfq_amount or 0.0) <= 0.0:
        return None
    anchor_horizons = (1, 3, 5, *profile.momentum_horizons)
    raw_features = profile.raw_feature_manifest.bind(
        CalculateProfileQfqAlpha(
            QfqPriceAnchors(
                close,
                tuple((horizon, points[-horizon - 1].qfq_close_price) for horizon in anchor_horizons),
            ),
            profile.momentum_horizons,
        )
    )
    if any(raw_features.missing_mask):
        return None
    sequence: tuple[float, ...] = ()
    if profile.algorithm == "tcn":
        completed = tuple(points)[:-1]
        ordered_dates = _ordered_dates(readable_dates)
        end = bisect_left(ordered_dates, row.trade_date)
        expected = ordered_dates[max(0, end - 61) : end]
        try:
            sequence = CalculatePriceVolumeSequence(
                row.code,
                tuple(
                    PriceVolumePoint(
                        p.trade_date,
                        cast(float, p.qfq_open),
                        cast(float, p.qfq_high),
                        cast(float, p.qfq_low),
                        cast(float, p.qfq_close_price),
                        cast(float, p.raw_volume),
                        cast(float, p.qfq_amount),
                    )
                    for p in completed
                    if all(
                        v is not None
                        for v in (p.qfq_open, p.qfq_high, p.qfq_low, p.qfq_close_price, p.raw_volume, p.qfq_amount)
                    )
                ),
                expected_dates=expected,
                source_identity=profile.profile_id,
            ).values
        except ValueError:
            return None
    return _PendingSample(
        row.code,
        row.trade_date,
        calendar_position,
        row.board,
        cast(str, row.industry),
        math.fsum(cast(float, amount) for amount in amounts) / 20.0,
        (*raw_features.require_complete(), *sequence),
        close,
    )


@lru_cache(maxsize=4)
def _ordered_dates(dates: frozenset[date]) -> tuple[date, ...]:
    return tuple(sorted(dates))


def _finalize_sample_day(
    values: Sequence[_PendingSample],
    profile: TrainedProfileContract,
) -> tuple[TrainingSample, ...]:
    population = tuple(values)
    if not population:
        return ()
    residuals = ResidualizeSampleDay(
        tuple(item.features[3 : 3 + len(profile.momentum_horizons)] for item in population),
        tuple(item.board for item in population),
        tuple(item.industry for item in population),
        tuple(item.average_amount_20d for item in population),
    )
    benchmarks = tuple(
        ObservedTrainingBenchmark(item.future_returns[horizon] for item in population) for horizon in range(5)
    )
    market_state = tuple(
        math.fsum(item.features[3 + profile.momentum_horizons.index(horizon)] for item in population) / len(population)
        for horizon in profile.market_state_momentum_horizons
    )
    finalized: list[TrainingSample] = []
    for index, item in enumerate(population):
        excess = tuple(
            TrainingAlphaTarget(next_return=value, benchmark_return=cast(float, benchmarks[horizon]))
            if value is not None and benchmarks[horizon] is not None
            else None
            for horizon, value in enumerate(item.future_returns)
        )
        d25_values = excess[1:]
        aggregate = math.fsum(cast(float, value) for value in d25_values) / 4.0 if None not in d25_values else None
        if excess[0] is None and aggregate is None:
            continue
        finalized.append(
            TrainingSample(
                item.code,
                item.trade_date,
                item.board,
                item.industry,
                item.average_amount_20d,
                (
                    *item.features[:3],
                    *(residual[index] for residual in residuals),
                    *market_state,
                    *item.features[len(profile.raw_feature_manifest.names) :],
                ),
                (excess[0], excess[1], excess[2], excess[3], excess[4], aggregate),
            )
        )
    return tuple(finalized)


def TrainingAlphaTarget(*, next_return: float, benchmark_return: float) -> float:
    """Return pre-cost alpha; runtime execution owns the one round-trip cost."""

    if not math.isfinite(next_return) or not math.isfinite(benchmark_return):
        raise ValueError("training alpha inputs must be finite")
    return next_return - benchmark_return


def AlignedSampleDates(
    calendar: tuple[date, ...],
    available_dates: Collection[date],
    readable_dates: frozenset[date],
) -> tuple[tuple[date, date, tuple[int, ...]], ...]:
    """Expose the frozen T+1 alignment used by the unchanged Tomorrow head."""

    available = set(available_dates)
    result: list[tuple[date, date, tuple[int, ...]]] = []
    for index, day in enumerate(calendar):
        next_index = index + 1
        indices = (index, index - 1, index - 3, index - 5, index - 20, index - 40, index - 60, next_index)
        if next_index >= len(calendar) or any(value < 0 for value in indices):
            continue
        amount_indices = tuple(range(index - 19, index + 1))
        required_dates = tuple(calendar[value] for value in (*indices, *amount_indices))
        if set(required_dates).issubset(readable_dates) and set(required_dates).issubset(available):
            result.append((day, calendar[next_index], indices[:-1]))
    return tuple(result)


def ResidualizeSampleDay(
    momenta: Sequence[Sequence[float]],
    boards: Sequence[str],
    industries: Sequence[str],
    average_amounts: Sequence[float],
) -> tuple[tuple[float, ...], ...]:
    if not momenta or not momenta[0] or any(len(row) != len(momenta[0]) for row in momenta):
        raise ValueError("training momentum rows must have one consistent non-empty width")
    context = CreateExposureContext(
        boards,
        average_amounts,
        industries=industries,
        contract=TRAINED_HEAD_EXPOSURE_CONTRACT,
    )
    return tuple(
        ResidualizeExposureWithContext(tuple(row[offset] for row in momenta), context)
        for offset in range(len(momenta[0]))
    )


def _publish(progress: TomorrowTrainingProgressPort | None, update: TomorrowTrainingProgress) -> None:
    if progress is not None:
        progress.publish(update)


__all__ = [
    "TrainingHistorySource",
    "TrainingSampleBuildRequest",
    "AlignedSampleDates",
    "BuildTrainingSamples",
    "ResidualizeSampleDay",
    "TrainingAlphaTarget",
]

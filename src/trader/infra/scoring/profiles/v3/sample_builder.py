"""Stream one verified history scan into the shared V3 multi-target sample database."""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, cast

from trader.application.research.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingWindow,
)
from trader.domain.market.feature_contracts import (
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    QfqPriceAnchors,
    calculate_tomorrow_qfq_alpha,
)
from trader.domain.recommendation.model_scoring import (
    TRAINED_HEAD_EXPOSURE_CONTRACT,
    create_exposure_context,
    residualize_exposure_with_context,
)
from trader.domain.research.history_revision import HistoryTrainingWindow
from trader.infra.research.history_training_input import HistoryTrainingInputError, HistoryTrainingInputSnapshot
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteV3TrainingSampleRepository,
    V3TrainingSample,
)


class TrainingWindowArchive(Protocol):
    snapshot: HistoryTrainingInputSnapshot

    def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int: ...

    def iter_training_windows(
        self,
        allowed_dates: frozenset[date],
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[HistoryTrainingWindow]: ...


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
class _SampleBuildState:
    repository: SQLiteV3TrainingSampleRepository
    progress: TomorrowTrainingProgressPort | None
    total_rows: int
    calendar_positions: dict[date, int]
    readable_dates: frozenset[date]
    allowed_codes: frozenset[str]
    converted_samples: int = 0
    processed_rows: int = 0
    pending_days: list[_PendingDay] = field(default_factory=list)
    current_day: date | None = None
    current_position: int = -1

    def report_scanned(self, value: int) -> None:
        self.processed_rows = value
        _publish(
            self.progress,
            TomorrowTrainingProgress("history_conversion", "running", value, self.total_rows, self.converted_samples),
        )

    def accept(self, training_window: HistoryTrainingWindow) -> None:
        code = training_window.code
        if code not in self.allowed_codes:
            raise HistoryTrainingInputError("history_code_outside_active_universe")
        row = training_window.rows[-1]
        position = self.calendar_positions[row.trade_date]
        if self.current_day is not None and row.trade_date != self.current_day:
            self.flush_matured(self.current_position)
        if row.trade_date != self.current_day:
            self.current_day = row.trade_date
            self.current_position = position
            self.pending_days.append(_PendingDay(row.trade_date, position, {}))
        self._record_future_close(training_window, position)
        candidate = _pending_sample(training_window, position, self.readable_dates)
        if candidate is not None:
            self.pending_days[-1].samples[code] = candidate

    def _record_future_close(self, training_window: HistoryTrainingWindow, position: int) -> None:
        close = _eligible_future_close(training_window)
        if close is None:
            return
        for pending_day in self.pending_days[:-1]:
            horizon = position - pending_day.calendar_position
            candidate = pending_day.samples.get(training_window.code)
            if candidate is not None and 1 <= horizon <= 5:
                candidate.future_returns[horizon - 1] = close / candidate.close - 1.0

    def flush_matured(self, last_position: int, *, all_days: bool = False) -> None:
        while self.pending_days and (all_days or last_position - self.pending_days[0].calendar_position >= 5):
            pending = self.pending_days.pop(0)
            finalized = _finalize_sample_day(tuple(pending.samples.values()))
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


def build_training_samples(
    archive: TrainingWindowArchive,
    codes: tuple[str, ...],
    window: TomorrowTrainingWindow,
    repository: SQLiteV3TrainingSampleRepository,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> None:
    """Build T+1..T+5 targets with one scan and at most five pending sample days."""

    calendar = archive.snapshot.calendar.open_dates
    total_rows = archive.training_row_upper_bound(window.readable_dates)
    state = _SampleBuildState(
        repository,
        progress,
        total_rows,
        {day: position for position, day in enumerate(calendar)},
        window.readable_dates,
        frozenset(codes),
    )
    _publish(progress, TomorrowTrainingProgress("history_conversion", "started", 0, total_rows))
    _publish(progress, TomorrowTrainingProgress("cross_section_conversion", "started", 0, total_rows))

    for training_window in archive.iter_training_windows(window.readable_dates, state.report_scanned):
        state.accept(training_window)

    state.flush_matured(state.current_position, all_days=True)
    _publish(
        progress,
        TomorrowTrainingProgress(
            "history_conversion",
            "completed",
            state.processed_rows,
            state.processed_rows,
            state.converted_samples,
        ),
    )
    _publish(
        progress,
        TomorrowTrainingProgress(
            "cross_section_conversion",
            "completed",
            state.converted_samples,
            state.converted_samples,
            state.converted_samples,
        ),
    )
    repository.prepare_for_model_fitting(window.split)


def _eligible_future_close(training_window: HistoryTrainingWindow) -> float | None:
    current = training_window.rows[-1]
    if current.qfq.close_price is None or not math.isfinite(current.qfq.close_price) or current.qfq.close_price <= 0.0:
        return None
    return float(current.qfq.close_price)


def _pending_sample(
    training_window: HistoryTrainingWindow,
    calendar_position: int,
    readable_dates: frozenset[date],
) -> _PendingSample | None:
    rows = training_window.rows
    current = rows[-1]
    if any(row.trade_date not in readable_dates for row in rows):
        return None
    if current.is_st or current.unadjusted.trading_status != "trading":
        return None
    close = _eligible_future_close(training_window)
    if close is None:
        return None
    amounts = tuple(row.qfq.amount for row in rows[-20:])
    if any(amount is None or not math.isfinite(amount) or amount <= 0.0 for amount in amounts):
        return None
    raw_features = TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.bind(
        calculate_tomorrow_qfq_alpha(
            QfqPriceAnchors(
                close,
                (
                    (1, rows[-2].qfq.close_price),
                    (3, rows[-4].qfq.close_price),
                    (5, rows[-6].qfq.close_price),
                    (20, rows[-21].qfq.close_price),
                    (40, rows[-41].qfq.close_price),
                    (60, rows[-61].qfq.close_price),
                ),
            )
        )
    )
    if any(raw_features.missing_mask):
        return None
    return _PendingSample(
        current.code,
        current.trade_date,
        calendar_position,
        current.board,
        current.industry,
        math.fsum(cast(float, amount) for amount in amounts) / 20.0,
        raw_features.require_complete(),
        close,
    )


def _finalize_sample_day(values: Sequence[_PendingSample]) -> tuple[V3TrainingSample, ...]:
    eligible = tuple(item for item in values if item.future_returns[0] is not None)
    if not eligible:
        return ()
    residuals = residualize_sample_day(
        tuple(item.features[3:] for item in eligible),
        tuple(item.board for item in eligible),
        tuple(item.industry for item in eligible),
        tuple(item.average_amount_20d for item in eligible),
    )
    benchmarks = tuple(_benchmark(eligible, horizon) for horizon in range(5))
    finalized: list[V3TrainingSample] = []
    for index, item in enumerate(eligible):
        excess = tuple(
            training_alpha_target(next_return=value, benchmark_return=cast(float, benchmarks[horizon]))
            if value is not None and benchmarks[horizon] is not None
            else None
            for horizon, value in enumerate(item.future_returns)
        )
        d25_values = excess[1:]
        aggregate = math.fsum(cast(float, value) for value in d25_values) / 4.0 if None not in d25_values else None
        finalized.append(
            V3TrainingSample(
                item.code,
                item.trade_date,
                item.board,
                item.industry,
                item.average_amount_20d,
                (*item.features[:3], *(residual[index] for residual in residuals)),
                (excess[0], excess[1], excess[2], excess[3], excess[4], aggregate),
            )
        )
    return tuple(finalized)


def _benchmark(values: Sequence[_PendingSample], horizon: int) -> float | None:
    available = tuple(item.future_returns[horizon] for item in values if item.future_returns[horizon] is not None)
    return math.fsum(cast(float, item) for item in available) / len(available) if available else None


def training_alpha_target(*, next_return: float, benchmark_return: float) -> float:
    """Return pre-cost alpha; runtime execution owns the one round-trip cost."""

    if not math.isfinite(next_return) or not math.isfinite(benchmark_return):
        raise ValueError("V3 training alpha inputs must be finite")
    return next_return - benchmark_return


def aligned_sample_dates(
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


def residualize_sample_day(
    momenta: Sequence[Sequence[float]],
    boards: Sequence[str],
    industries: Sequence[str],
    average_amounts: Sequence[float],
) -> tuple[tuple[float, ...], ...]:
    if not momenta or not momenta[0] or any(len(row) != len(momenta[0]) for row in momenta):
        raise ValueError("V3 training momentum rows must have one consistent non-empty width")
    context = create_exposure_context(
        boards,
        average_amounts,
        industries=industries,
        contract=TRAINED_HEAD_EXPOSURE_CONTRACT,
    )
    return tuple(
        residualize_exposure_with_context(tuple(row[offset] for row in momenta), context)
        for offset in range(len(momenta[0]))
    )


def _publish(progress: TomorrowTrainingProgressPort | None, update: TomorrowTrainingProgress) -> None:
    if progress is not None:
        progress.publish(update)


__all__ = [
    "TrainingWindowArchive",
    "aligned_sample_dates",
    "build_training_samples",
    "residualize_sample_day",
    "training_alpha_target",
]

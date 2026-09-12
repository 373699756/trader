"""Stream the verified monthly archive into the disposable sample repository."""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
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
from trader.domain.recommendation.model_scoring import V3_EXPOSURE_CONTRACT, residualize_exposure
from trader.domain.research.history_monthly import HistoryTrainingWindow
from trader.infra.research.history_training_input import HistoryTrainingInputError, HistoryTrainingInputSnapshot
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteTomorrowTrainingSampleRepository,
    TomorrowTrainingSample,
)

_RAW_WRITE_BATCH_SIZE = 4_096


class TrainingWindowArchive(Protocol):
    snapshot: HistoryTrainingInputSnapshot

    def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int: ...

    def iter_training_windows(
        self,
        allowed_dates: frozenset[date],
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[HistoryTrainingWindow]: ...


@dataclass(frozen=True)
class _PendingSample:
    code: str
    trade_date: date
    next_date: date
    board: str
    industry: str
    average_amount_20d: float
    features: tuple[float, ...]
    close: float


def build_training_samples(
    archive: TrainingWindowArchive,
    codes: tuple[str, ...],
    window: TomorrowTrainingWindow,
    repository: SQLiteTomorrowTrainingSampleRepository,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> None:
    """Build samples with one chronological partition scan and bounded per-code state."""

    calendar = archive.snapshot.calendar.open_dates
    next_dates = dict(zip(calendar, calendar[1:], strict=False))
    allowed_codes = frozenset(codes)
    total_rows = archive.training_row_upper_bound(window.readable_dates)
    generated_samples = 0
    processed_rows = 0
    pending: dict[str, _PendingSample] = {}
    write_batch: list[TomorrowTrainingSample] = []
    _publish(progress, TomorrowTrainingProgress("history_conversion", "started", 0, total_rows))

    def report_scanned(value: int) -> None:
        nonlocal processed_rows
        processed_rows = value
        _publish(
            progress,
            TomorrowTrainingProgress("history_conversion", "running", value, total_rows, generated_samples),
        )

    for training_window in archive.iter_training_windows(window.readable_dates, report_scanned):
        code = training_window.code
        if code not in allowed_codes:
            raise HistoryTrainingInputError("history_code_outside_active_universe")
        current = training_window.rows[-1]
        previous = pending.pop(code, None)
        if (
            previous is not None
            and previous.next_date == current.trade_date
            and current.qfq.close_price not in (None, 0)
        ):
            write_batch.append(
                TomorrowTrainingSample(
                    previous.code,
                    previous.trade_date,
                    previous.board,
                    previous.industry,
                    previous.average_amount_20d,
                    previous.features,
                    float(current.qfq.close_price) / previous.close - 1.0,
                )
            )
            generated_samples += 1
            if len(write_batch) >= _RAW_WRITE_BATCH_SIZE:
                repository.add_raw(write_batch)
                write_batch.clear()
        candidate = _pending_sample(training_window, next_dates, window.readable_dates)
        if candidate is not None:
            pending[code] = candidate

    if write_batch:
        repository.add_raw(write_batch)
    _publish(
        progress,
        TomorrowTrainingProgress(
            "history_conversion",
            "completed",
            processed_rows,
            processed_rows,
            generated_samples,
        ),
    )
    _convert_cross_sections(repository, progress)


def _pending_sample(
    training_window: HistoryTrainingWindow,
    next_dates: dict[date, date],
    readable_dates: frozenset[date],
) -> _PendingSample | None:
    rows = training_window.rows
    current = rows[-1]
    next_date = next_dates.get(current.trade_date)
    if next_date is None:
        return None
    if next_date not in readable_dates or any(row.trade_date not in readable_dates for row in rows):
        return None
    if current.is_st or current.unadjusted.trading_status != "trading" or current.qfq.close_price in (None, 0):
        return None
    amounts = tuple(row.qfq.amount for row in rows[-20:])
    if any(amount is None or not math.isfinite(amount) or amount <= 0.0 for amount in amounts):
        return None
    close = float(current.qfq.close_price)
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
        next_date,
        current.board,
        current.industry,
        math.fsum(cast(float, amount) for amount in amounts) / 20.0,
        raw_features.require_complete(),
        close,
    )


def _convert_cross_sections(
    repository: SQLiteTomorrowTrainingSampleRepository,
    progress: TomorrowTrainingProgressPort | None,
) -> None:
    raw_total = repository.raw_count()
    converted_samples = 0
    _publish(progress, TomorrowTrainingProgress("cross_section_conversion", "started", 0, raw_total))
    for day in repository.raw_dates():
        values = repository.raw_for_date(day)
        if not values:
            continue
        benchmark = math.fsum(item.target for item in values) / len(values)
        residuals = residualize_sample_day(
            tuple(item.features[3:] for item in values),
            tuple(item.board for item in values),
            tuple(item.industry for item in values),
            tuple(item.average_amount_20d for item in values),
        )
        repository.add_final(
            TomorrowTrainingSample(
                item.code,
                day,
                item.board,
                item.industry,
                item.average_amount_20d,
                (*item.features[:3], *(residual[index] for residual in residuals)),
                training_alpha_target(next_return=item.target, benchmark_return=benchmark),
            )
            for index, item in enumerate(values)
        )
        repository.discard_raw_date(day)
        converted_samples += len(values)
        _publish(
            progress,
            TomorrowTrainingProgress(
                "cross_section_conversion",
                "completed" if converted_samples == raw_total else "running",
                converted_samples,
                raw_total,
                converted_samples,
            ),
        )
    if raw_total == 0:
        _publish(progress, TomorrowTrainingProgress("cross_section_conversion", "completed", 0, 0))
    repository.prepare_for_model_fitting()


def training_alpha_target(*, next_return: float, benchmark_return: float) -> float:
    """Return pre-cost alpha; validation and execution own round-trip costs."""

    if not math.isfinite(next_return) or not math.isfinite(benchmark_return):
        raise ValueError("V3 training alpha inputs must be finite")
    return next_return - benchmark_return


def aligned_sample_dates(
    calendar: tuple[date, ...],
    available_dates: Collection[date],
    readable_dates: frozenset[date],
) -> tuple[tuple[date, date, tuple[int, ...]], ...]:
    """Expose the frozen skip-five alignment for focused contract tests."""

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


def _publish(
    progress: TomorrowTrainingProgressPort | None,
    update: TomorrowTrainingProgress,
) -> None:
    if progress is not None:
        progress.publish(update)


__all__ = [
    "TrainingWindowArchive",
    "aligned_sample_dates",
    "build_training_samples",
    "residualize_sample_day",
    "training_alpha_target",
]

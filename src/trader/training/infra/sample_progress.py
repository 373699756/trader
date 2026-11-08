"""Observe completed input days without counting raw-row upper bounds as totals."""

from datetime import date

from trader.training.application.tomorrow_training import (
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
    TomorrowTrainingProgressState,
    TrainingSampleDiagnostics,
    TrainingSampleProgress,
)


class TrainingSampleProgressTracker:
    def __init__(
        self,
        dates: frozenset[date],
        first_sample_date: date,
        progress: TomorrowTrainingProgressPort | None,
        *,
        interval_rows: int = 4096,
    ) -> None:
        if type(interval_rows) is not int or interval_rows < 1:
            raise ValueError("training_sample_progress_interval_invalid")
        self._dates = dates
        self._first_sample_date = first_sample_date
        self._progress = progress
        self._interval_rows = interval_rows
        self._current: date | None = None
        self._completed_days = 0
        self._rows = 0
        self._warmup_rows = 0

    def observe(self, day: date) -> bool:
        if day not in self._dates or self._current is not None and day < self._current:
            raise ValueError("training_sample_progress_date_invalid")
        changed = self._current is not None and day != self._current
        if changed:
            self._completed_days += 1
        self._current = day
        self._rows += 1
        self._warmup_rows += day < self._first_sample_date
        return changed or self._rows % self._interval_rows == 0

    def snapshot(self, *, finished: bool = False, pending_samples: int = 0) -> TrainingSampleProgress:
        return TrainingSampleProgress(
            self._completed_days + int(finished and self._current is not None), len(self._dates),
            self._rows, self._warmup_rows,
            "generation" if self._current is not None and self._current >= self._first_sample_date else "warmup",
            pending_samples,
        )

    def publish(
        self,
        produced: int,
        diagnostics: TrainingSampleDiagnostics,
        *,
        state: TomorrowTrainingProgressState = "running",
        pending_samples: int = 0,
    ) -> None:
        if self._progress is not None:
            value = self.snapshot(finished=state == "completed", pending_samples=pending_samples)
            self._progress.publish(TomorrowTrainingProgress(
                "history_conversion", state, value.completed_days, value.total_days, produced,
                sample_diagnostics=diagnostics, sample_progress=value,
            ))


__all__ = ["TrainingSampleProgressTracker"]

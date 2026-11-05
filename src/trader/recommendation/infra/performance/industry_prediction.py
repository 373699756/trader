"""Bounded offline industry concurrency using an explicitly owned executor."""

from collections.abc import Callable
from concurrent.futures import Executor, Future, wait
from dataclasses import dataclass
import threading

from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction


@dataclass(frozen=True)
class IndustryPredictionCounts:
    completed: int
    failed: int
    cancelled: int
    in_flight: int
    peak_in_flight: int


class ParallelIndustryPrediction:
    def __init__(self, executor: Executor, *, parallelism: int = 2) -> None:
        if type(parallelism) is not int or not 1 <= parallelism <= 8:
            raise ValueError("industry_prediction_parallelism_invalid")
        self._executor = executor
        self._parallelism = parallelism
        self._lock = threading.Lock()
        self._completed = self._failed = self._cancelled = self._in_flight = self._peak = 0

    @property
    def counts(self) -> IndustryPredictionCounts:
        with self._lock:
            return IndustryPredictionCounts(self._completed, self._failed, self._cancelled, self._in_flight, self._peak)

    def predict(
        self,
        calculate: Callable[[tuple[ModelInput, ...]], tuple[ModelPrediction, ...]],
        groups: tuple[tuple[ModelInput, ...], ...],
    ) -> tuple[tuple[ModelPrediction, ...], ...]:
        industries = tuple(group[0].industry for group in groups if group)
        if (
            len(industries) != len(groups) or len(set(industries)) != len(industries)
            or any(any(item.industry != group[0].industry for item in group) for group in groups)
        ):
            raise ValueError("industry_prediction_groups_invalid")
        results: list[tuple[ModelPrediction, ...]] = []
        for start in range(0, len(groups), self._parallelism):
            futures: list[Future[tuple[ModelPrediction, ...]]] = []
            try:
                for group in groups[start : start + self._parallelism]:
                    futures.append(self._executor.submit(self._run, calculate, group))
                results.extend(future.result() for future in futures)
            finally:
                cancelled = sum(future.cancel() for future in futures if not future.done())
                wait(futures)
                with self._lock:
                    self._cancelled += cancelled
        return tuple(results)

    def _run(
        self,
        calculate: Callable[[tuple[ModelInput, ...]], tuple[ModelPrediction, ...]],
        group: tuple[ModelInput, ...],
    ) -> tuple[ModelPrediction, ...]:
        with self._lock:
            self._in_flight += 1
            self._peak = max(self._peak, self._in_flight)
        completed = False
        try:
            result = calculate(group)
            completed = True
            return result
        finally:
            with self._lock:
                self._in_flight -= 1
                self._completed += int(completed)
                self._failed += int(not completed)


__all__ = [
    "IndustryPredictionCounts",
    "ParallelIndustryPrediction",
]

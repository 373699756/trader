"""Measure components of sample construction in an isolated training worker."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from threading import Lock, current_thread, main_thread
from typing import TYPE_CHECKING, Literal, ParamSpec, TypeVar
from unittest.mock import patch

if TYPE_CHECKING:
    from datetime import date

    from trader.download.domain.history_revision import HistoryTrainingRow
    from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
    from trader.training.infra.sample_builder import TrainingSampleBuildRequest

_Component = Literal["window_features", "cross_section_and_labels", "sqlite_append", "index_and_commit"]
_COMPONENTS: tuple[_Component, ...] = (
    "window_features", "cross_section_and_labels", "sqlite_append", "index_and_commit",
)
_Args = ParamSpec("_Args")
_Result = TypeVar("_Result")


@dataclass
class _Duration:
    calls: int = 0
    wall_seconds: float = 0.0
    thread_cpu_seconds: float = 0.0

    def record(self, wall_start: float, cpu_start: float) -> None:
        self.wall_seconds += time.perf_counter() - wall_start
        self.thread_cpu_seconds += time.thread_time() - cpu_start
        self.calls += 1


@dataclass
class _SampleBuild:
    profile: ScoringProfileId
    total: _Duration = field(default_factory=_Duration)
    components: dict[_Component, _Duration] = field(
        default_factory=lambda: {component: _Duration() for component in _COMPONENTS},
    )
    completed: bool = False


def _measure_call(
    current: ContextVar[_SampleBuild | None], component: _Component, call: Callable[_Args, _Result],
) -> Callable[_Args, _Result]:
    def measure(*args: _Args.args, **kwargs: _Args.kwargs) -> _Result:
        build = current.get()
        if build is None:
            return call(*args, **kwargs)
        wall_start, cpu_start = time.perf_counter(), time.thread_time()
        try:
            return call(*args, **kwargs)
        finally:
            build.components[component].record(wall_start, cpu_start)

    return measure


@contextmanager
def MeasureSampleComponents() -> Iterator[Callable[[], dict[str, object]]]:
    """Measure per-profile component calls without changing sample production or fitting."""
    from trader.training.infra import engine, sample_builder, sample_index

    if current_thread() is not main_thread():
        raise ValueError("sample_component_measurement_requires_main_thread")
    current: ContextVar[_SampleBuild | None] = ContextVar("sample_component_measurement", default=None)
    results: list[_SampleBuild] = []
    results_lock = Lock()
    original = engine.BuildTrainingSamples
    index = sample_index.SQLiteTrainingSampleIndex

    def build_samples(
        request: TrainingSampleBuildRequest,
        *,
        aligned_rows: Iterator[HistoryTrainingRow] | None = None,
        aligned_dates: frozenset[date] | None = None,
    ) -> None:
        build = _SampleBuild(request.profile.profile_id)
        token = current.set(build)
        wall_start, cpu_start = time.perf_counter(), time.thread_time()
        try:
            original(request, aligned_rows=aligned_rows, aligned_dates=aligned_dates)
            build.completed = True
        finally:
            build.total.record(wall_start, cpu_start)
            current.reset(token)
            with results_lock:
                results.append(build)

    def project() -> dict[str, object]:
        with results_lock:
            profiles = [asdict(result) for result in sorted(results, key=lambda result: result.profile)]
        return {
            "profiles": profiles,
            "accounting": (
                "within_one_builder_components_are_subsets_of_total; profiles_overlap; "
                "source_and_queue_timings_are_separate_observations"
            ),
            "sqlite_append_scope": "sample_rows_and_day_diagnostics; includes_encoding_and_statistics",
            "sample_seal_scope": "outside_builder; profile_stage_seconds.sample_cache_seal",
            "instrumented": True,
            "performance_acceptance": False,
        }

    with (
        patch.object(engine, "BuildTrainingSamples", build_samples),
        patch.object(sample_builder, "_pending_sample", _measure_call(current, "window_features", sample_builder._pending_sample)),
        patch.object(
            sample_builder,
            "_finalize_sample_day",
            _measure_call(current, "cross_section_and_labels", sample_builder._finalize_sample_day),
        ),
        patch.object(index, "add_final", _measure_call(current, "sqlite_append", index.add_final)),
        patch.object(index, "record_day", _measure_call(current, "sqlite_append", index.record_day)),
        patch.object(index, "prepare_for_model_fitting", _measure_call(current, "index_and_commit", index.prepare_for_model_fitting)),
    ):
        yield project

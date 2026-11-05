"""Explicit, scoped execution experiments for the existing complete training worker."""

from __future__ import annotations

import hashlib
import os
from _thread import LockType
from collections.abc import Iterator
from concurrent.futures import Executor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock, current_thread, main_thread
from unittest.mock import patch

from scripts.training.resources import execution_selection as selection
from trader.infra.disk_types import DiskKind
from trader.infra.settings import training as training_configuration
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingProfileThreads, TrainingResourceSettings
from trader.training.application.selected_training import SelectedTrainingRun
from trader.training.entrypoints import selected_training as training_entrypoint
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.selected_training import SelectedProfileResult, SelectedTrainingRequest

_ENVIRONMENT = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


@dataclass(frozen=True)
class _RunResources:
    profiles: tuple[ScoringProfileId, ...]
    cpu_capacity: int
    native_threads: int
    profile_threads: TrainingProfileThreads
    memory_capacity_bytes: int | None
    batch_rows: int
    queue_batches: int


@dataclass
class TrainingExecutionExperiment:
    execution_candidate: selection.ExecutionCandidate
    cpu_candidate: selection.CpuCandidate
    options: TrainingExecutionOptions
    components: tuple[selection.ExecutionComponent, ...] = field(default=(), kw_only=True)
    fanout_candidate: selection.FanoutCandidate = field(default="baseline", kw_only=True)
    _runs: list[_RunResources] = field(default_factory=list)
    _lock: LockType = field(default_factory=Lock)

    def observe(self, request: SelectedTrainingRequest) -> None:
        with self._lock:
            self._runs.append(_RunResources(
                request.profiles,
                request.cpu_budget.capacity,
                request.cpu_budget.native_threads,
                request.cpu_budget.profile_threads,
                request.memory_capacity_bytes,
                request.fanout_settings.batch_rows,
                request.fanout_settings.queue_batches,
            ))

    def runs(self) -> tuple[_RunResources, ...]:
        with self._lock:
            return tuple(self._runs)


def _options(
    candidate: selection.ExecutionCandidate, components: tuple[selection.ExecutionComponent, ...],
) -> TrainingExecutionOptions:
    if candidate not in {"baseline", "resources", "ram_matrices", "shared_validation", "normalized_tcn", "combined", "selected"}:
        raise ValueError("training_execution_candidate_invalid")
    if not isinstance(components, tuple) or any(not isinstance(component, str) for component in components):
        raise ValueError("training_execution_components_invalid")
    if candidate == "selected":
        canonical = selection.ParseExecutionComponents(",".join(components))
        if components != canonical:
            raise ValueError("training_execution_components_not_canonical")
    elif components:
        raise ValueError("training_execution_components_require_selected")
    else:
        canonical = (
            () if candidate == "baseline" else selection.ParseExecutionComponents(
                "resources,ram_matrices,shared_validation,normalized_tcn" if candidate == "combined" else candidate
            )
        )
    return TrainingExecutionOptions(
        memory_first="resources" in canonical,
        release_cpu_before_publish="resources" in canonical,
        shared_matrix_memory="ram_matrices" in canonical,
        shared_validation="shared_validation" in canonical,
        normalized_tcn_batches="normalized_tcn" in canonical,
    )


@contextmanager
def UseTrainingExecutionCandidate(
    execution: selection.ExecutionCandidate,
    cpu: selection.CpuCandidate,
    *,
    components: tuple[selection.ExecutionComponent, ...] = (),
    fanout: selection.FanoutCandidate = "baseline",
) -> Iterator[TrainingExecutionExperiment]:
    if current_thread() is not main_thread():
        raise ValueError("training_execution_candidate_requires_main_thread")
    if cpu not in {"baseline", "two_plus_one", "three_single"}:
        raise ValueError("training_cpu_candidate_invalid")
    if fanout not in {"baseline", "batch_double", "queue_double"}:
        raise ValueError("training_fanout_candidate_invalid")
    experiment = TrainingExecutionExperiment(
        execution,
        cpu,
        _options(execution, components),
        components=components,
        fanout_candidate=fanout,
    )
    if execution == cpu == fanout == "baseline":
        yield experiment
        return
    original_train = training_entrypoint.TrainSelectedProfiles
    original_load = training_configuration.LoadTrainingResourceSettings

    def load(project_root: Path, medium: DiskKind) -> TrainingResourceSettings:
        resources = original_load(project_root, medium)
        cpu_settings = resources.cpu
        if cpu != "baseline":
            threads = TrainingProfileThreads(1, 1, 1) if cpu == "three_single" else resources.cpu.profile_threads
            cpu_settings = replace(cpu_settings, native_threads=1, profile_threads=threads)
        fanout_settings = resources.fanout
        if fanout != "baseline":
            fanout_settings = replace(
                fanout_settings,
                batch_rows=fanout_settings.batch_rows * (2 if fanout == "batch_double" else 1),
                queue_batches=fanout_settings.queue_batches * (2 if fanout == "queue_double" else 1),
            )
            if fanout_settings.batch_rows > 2048 or fanout_settings.queue_batches > 8:
                raise ValueError("training_fanout_candidate_exceeds_limit")
        return replace(resources, cpu=cpu_settings, fanout=fanout_settings)

    def train(
        request: SelectedTrainingRequest, executor: Executor, *, sqlite_policy: SqliteIoPolicy,
    ) -> SelectedTrainingRun[SelectedProfileResult]:
        request = replace(request, execution_options=experiment.options)
        experiment.observe(request)
        return original_train(request, executor, sqlite_policy=sqlite_policy)

    environment = tuple((name, os.environ.get(name)) for name in _ENVIRONMENT)
    try:
        with ExitStack() as scope:
            scope.enter_context(patch.object(training_entrypoint, "TrainSelectedProfiles", train))
            if cpu != "baseline" or fanout != "baseline":
                scope.enter_context(patch.object(training_configuration, "LoadTrainingResourceSettings", load))
            yield experiment
    finally:
        for name, value in environment:
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def ProjectTrainingExecutionExperiment(experiment: TrainingExecutionExperiment) -> dict[str, object]:
    options = experiment.options
    source_directory = Path(__file__).parent
    source_hash = hashlib.sha256(b"".join(
        hashlib.sha256((source_directory / name).read_bytes()).digest()
        for name in ("execution_candidates.py", "execution_selection.py")
    )).hexdigest()
    return {
        "execution": experiment.execution_candidate,
        "cpu": experiment.cpu_candidate,
        "components": experiment.components,
        "fanout": experiment.fanout_candidate,
        "production_enabled": False,
        "performance_evidence": False,
        "candidate_source_hash": source_hash,
        "options": {
            "memory_first": options.memory_first,
            "release_cpu_before_publish": options.release_cpu_before_publish,
            "shared_matrix_memory": options.shared_matrix_memory,
            "shared_validation": options.shared_validation,
            "normalized_tcn_batches": options.normalized_tcn_batches,
        },
        "runs": [
            {
                "profiles": run.profiles, "cpu_capacity": run.cpu_capacity,
                "native_threads": run.native_threads,
                "profile_threads": {"v1": run.profile_threads.v1, "v2": run.profile_threads.v2, "v3": run.profile_threads.v3},
                "memory_capacity_bytes": run.memory_capacity_bytes,
                "batch_rows": run.batch_rows,
                "queue_batches": run.queue_batches,
            }
            for run in experiment.runs()
        ],
    }


__all__ = [
    "ProjectTrainingExecutionExperiment",
    "TrainingExecutionExperiment",
    "UseTrainingExecutionCandidate",
]

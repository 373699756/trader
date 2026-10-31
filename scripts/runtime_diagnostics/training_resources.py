"""Bounded CPU admission smoke test; no history, models, configuration writes or network."""

from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

import numpy as np
from threadpoolctl import threadpool_info

from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.training.infra.cpu_admission import AvailableTrainingCpus, TrainingCpuBudget

from .reporting import emit_report


def _native_limits() -> tuple[tuple[str, str, int], ...]:
    return tuple(sorted(
        (str(item["user_api"]), str(item["internal_api"]), int(item["num_threads"]))
        for item in threadpool_info()
        if item["num_threads"] is not None
    ))


@dataclass(frozen=True)
class _ProfileProbe:
    profile: str
    requested_threads: int
    granted_threads: int
    native_limits_valid: bool
    calculation_valid: bool


def ProbeTrainingResources(profiles: tuple[str, ...]) -> dict[str, object]:
    if not profiles or len(set(profiles)) != len(profiles) or not set(profiles) <= set(SCORING_PROFILE_IDS):
        raise ValueError("training_resource_profiles_invalid")
    budget = TrainingCpuBudget(AvailableTrainingCpus())
    before = _native_limits()
    started = time.monotonic()
    entered, cancelled = Event(), Event()
    with budget.native_runtime() as controller, ThreadPoolExecutor(max_workers=len(profiles)) as workers:
        def calculate(profile: str) -> _ProfileProbe:
            controller.limit(limits=budget.native_threads)
            # Fixed probe requests exercise admission, independently of training configuration.
            requested = 1 if profile == "v5" else 2
            with budget.reserve(requested, lambda: None) as threads:
                limits = _native_limits()
                matrix = np.ones((64, 64), dtype=np.float64)
                valid = bool(np.array_equal(matrix @ matrix, np.full((64, 64), 64.0)))
                limited = bool(limits) and all(count <= budget.native_threads for _, _, count in limits)
                return _ProfileProbe(
                    profile,
                    requested,
                    threads,
                    limited,
                    valid,
                )

        results = tuple(workers.map(calculate, profiles))

        def heartbeat() -> None:
            entered.set()
            if cancelled.is_set():
                raise InterruptedError("diagnostic_cancelled")

        def wait_for_cpu() -> bool:
            try:
                with budget.reserve(1, heartbeat):
                    return False
            except InterruptedError:
                return True

        with budget.reserve(budget.capacity, lambda: None):
            waiting = workers.submit(wait_for_cpu)
            if not entered.wait(2):
                cancelled.set()
                raise TimeoutError("training_cpu_wait_not_observed")
            cancellation_started = time.monotonic()
            cancelled.set()
            wait_cancelled = waiting.result(timeout=2)
            cancellation_seconds = time.monotonic() - cancellation_started
        # A cancelled waiter must not leak a permit or prevent the next full reservation.
        with budget.reserve(budget.capacity, lambda: None):
            released = True
    restored = _native_limits() == before
    passed = all(item.native_limits_valid and item.calculation_valid for item in results)
    passed = passed and wait_cancelled and released and restored
    return {
        "schema_version": "training-resources-diagnostic",
        "status": "passed" if passed else "failed",
        "summary": {
            "logical_cpus": os.cpu_count(),
            "available_cpus": budget.available_cpus,
            "fitting_cpu_capacity": budget.capacity,
            "native_threads": budget.native_threads,
            "probe_requests": "fixed_synthetic",
            "profiles": [
                {
                    "profile": item.profile,
                    "requested_threads": item.requested_threads,
                    "granted_threads": item.granted_threads,
                    "native_limits_valid": item.native_limits_valid,
                    "calculation_valid": item.calculation_valid,
                }
                for item in results
            ],
            "peak_reserved_cpu_threads": budget.peak_reserved_threads,
            "peak_includes_full_capacity_cancel_probe": True,
            "blocked_wait_cancelled": wait_cancelled,
            "cancel_seconds": round(cancellation_seconds, 6),
            "permits_released": released,
            "native_limits_restored": restored,
            "seconds": round(time.monotonic() - started, 6),
            "models_trained": 0,
            "data_writes": 0,
            "network_calls": 0,
            "full_training_validation": False,
        },
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", required=True)
    args = parser.parse_args()
    try:
        report = ProbeTrainingResources(tuple(args.profiles.split(",")))
    except (OSError, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": "training-resources-diagnostic",
            "status": "failed",
            "summary": {"failure": type(exc).__name__},
        }
    emit_report(report)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(Main())

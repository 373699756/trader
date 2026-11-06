"""Measured priorities, bounded borrowing and cancellation on real CPU budgets."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from threading import Event

import pytest

from scripts.training.resources.scheduling_candidates import LoadSchedulingEvidence
from scripts.training.resources.scheduling_candidates import ProfileFitEstimate
from scripts.training.resources.scheduling_candidates import ProjectSchedulingExperiment
from scripts.training.resources.scheduling_candidates import SchedulingEvidence
from scripts.training.resources.scheduling_candidates import UseSchedulingCandidate
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_timing import TrainingExecutionMeasurements

PROFILES = ("v1", "v2", "v5")


def _budget(native=1, available=4):
    return TrainingCpuBudget(available, replace(TrainingResourceSettingsFixture().cpu, native_threads=native))


def _evidence(budget, profiles=PROFILES):
    return SchedulingEvidence(
        budget.available_cpus, budget.capacity, budget.native_threads,
        tuple(ProfileFitEstimate(profile, seconds) for profile, seconds in zip(profiles, (30.0, 10.0, 20.0), strict=False)),
        "f" * 64,
    )


def _ports(budget, profiles=PROFILES):
    measured = TrainingExecutionMeasurements(profiles, time.monotonic)
    return {profile: measured.cpu_admission(profile, budget) for profile in profiles}


def test_candidate_preserves_keyword_arguments_and_restores_the_method():
    budget = _budget()
    original = TrainingExecutionMeasurements.cpu_admission
    measured = TrainingExecutionMeasurements(PROFILES, time.monotonic)
    with UseSchedulingCandidate("fixed", _evidence(budget)):
        admission = measured.cpu_admission(profile="v1", admission=budget)
        with admission.reserve(1, lambda: None) as threads:
            assert threads == 1
    assert TrainingExecutionMeasurements.cpu_admission is original


@pytest.mark.parametrize("candidate,expected", (("fixed", ("v2", "v5")), ("long_task_first", ("v5", "v2")), ("borrowing", ("v5", "v2"))))
def test_ready_waiters_follow_measured_priority_or_fixed_fifo_and_return_all_slots(candidate, expected):
    budget = _budget()
    ready = {profile: Event() for profile in ("v2", "v5")}
    order = []
    original = TrainingExecutionMeasurements.cpu_admission
    with UseSchedulingCandidate(candidate, _evidence(budget)) as experiment, ThreadPoolExecutor(max_workers=2) as pool:
        ports = _ports(budget)

        def fit(profile):
            with ports[profile].reserve(3, ready[profile].set):
                order.append(profile)

        with ExitStack() as held:
            for _ in range(3 if candidate == "fixed" else 1):
                held.enter_context(ports["v1"].reserve(1 if candidate == "fixed" else 3, lambda: None))
            short = pool.submit(fit, "v2")
            assert ready["v2"].wait(2)
            long = pool.submit(fit, "v5")
            assert ready["v5"].wait(2)
        short.result(timeout=3)
        long.result(timeout=3)
        report = ProjectSchedulingExperiment(experiment)
        assert report["pending_requests"] == report["reserved_threads"] == 0
        assert all(0 < grant.granted_threads <= grant.requested_threads for grant in experiment.grants)
    # Fixed shares may run concurrently after FIFO admission; execution order is then free.
    assert (set(order) == set(expected)) if candidate == "fixed" else (tuple(order) == expected)
    assert TrainingExecutionMeasurements.cpu_admission is original
    with budget.reserve(3, lambda: None) as threads:
        assert threads == 3


@pytest.mark.parametrize("candidate,native,early,granted", (
    ("borrowing", 1, True, 1), ("long_task_first", 1, False, 3), ("borrowing", 2, False, 3),
))
def test_borrowing_uses_only_idle_capacity_and_respects_native_floor(candidate, native, early, granted):
    budget = _budget(native)
    waiting, entered = Event(), Event()
    profiles = ("v2", "v5")
    with UseSchedulingCandidate(candidate, _evidence(budget, profiles)) as experiment, ThreadPoolExecutor(max_workers=1) as pool:
        ports = _ports(budget, profiles)

        def fit():
            with ports["v5"].reserve(3, waiting.set) as threads:
                assert threads == granted
                entered.set()

        with ports["v2"].reserve(2 if native == 1 else 1, lambda: None):
            future = pool.submit(fit)
            assert waiting.wait(2)
            if early:
                assert entered.wait(2)
                future.result(timeout=2)
            else:
                assert not entered.is_set()
        future.result(timeout=3)
        assert budget.peak_reserved_threads <= budget.capacity
        assert all(grant.charged_threads >= native for grant in experiment.grants)
        assert ProjectSchedulingExperiment(experiment)["reserved_threads"] == 0


@pytest.mark.parametrize("failure", (RuntimeError, InterruptedError, KeyboardInterrupt))
def test_body_failure_restores_hook_and_releases_candidate_and_original_budget(failure):
    budget = _budget()
    original = TrainingExecutionMeasurements.cpu_admission
    with pytest.raises(failure), UseSchedulingCandidate("borrowing", _evidence(budget)) as experiment:
        with _ports(budget)["v1"].reserve(3, lambda: None):
            raise failure("fixture")
    assert TrainingExecutionMeasurements.cpu_admission is original
    assert ProjectSchedulingExperiment(experiment)["reserved_threads"] == 0
    with budget.reserve(3, lambda: None) as threads:
        assert threads == 3


def test_cancelled_waiter_is_removed_without_consuming_slots():
    budget = _budget()
    waiting, cancelled = Event(), Event()
    with UseSchedulingCandidate("long_task_first", _evidence(budget)) as experiment, ThreadPoolExecutor(max_workers=1) as pool:
        ports = _ports(budget)

        def heartbeat():
            waiting.set()
            if cancelled.is_set():
                raise InterruptedError("fixture")

        def fit():
            with ports["v5"].reserve(2, heartbeat):
                pytest.fail("cancelled waiter acquired CPU")

        with ports["v1"].reserve(3, lambda: None):
            future = pool.submit(fit)
            assert waiting.wait(2)
            cancelled.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=2)
        report = ProjectSchedulingExperiment(experiment)
        assert report["pending_requests"] == report["reserved_threads"] == 0


def _document():
    return {
        "schema_version": "training-scheduling-evidence", "source_hash": "a" * 64,
        "configuration_hash": "b" * 64, "implementation_hash": "c" * 64, "medium": "hdd",
        "available_cpus": 4, "cpu_capacity": 3, "native_threads": 1,
        "profiles": [{"profile": profile, "fit_seconds": value} for profile, value in zip(PROFILES, (30, 10, 20), strict=True)],
    }


@pytest.mark.parametrize("field,value", (
    ("source_hash", "d" * 64), ("configuration_hash", "d" * 64), ("implementation_hash", "d" * 64),
    ("medium", "ssd"), ("cpu_capacity", 5), ("native_threads", True),
    ("profiles", [{"profile": "v1", "fit_seconds": float("nan")}]),
))
def test_evidence_from_other_input_machine_or_invalid_measurement_is_rejected(tmp_path, field, value):
    raw = _document()
    raw[field] = value
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        LoadSchedulingEvidence(path, source_hash="a" * 64, configuration_hash="b" * 64,
                               implementation_hash="c" * 64, profiles=PROFILES, medium="hdd")


def test_valid_evidence_is_typed_and_machine_capacity_is_checked_again_at_admission(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_document()))
    evidence = LoadSchedulingEvidence(path, source_hash="a" * 64, configuration_hash="b" * 64,
                                      implementation_hash="c" * 64, profiles=PROFILES, medium="hdd")
    assert evidence.estimates[1] == ProfileFitEstimate("v2", 10.0)
    with UseSchedulingCandidate("fixed", evidence), pytest.raises(ValueError, match="machine_budget"):
        _ports(_budget(available=8))


def test_one_measurement_cannot_silently_switch_to_another_cpu_budget():
    budget = _budget()
    with UseSchedulingCandidate("fixed", _evidence(budget)), pytest.raises(ValueError, match="budget_changed"):
        measured = TrainingExecutionMeasurements(PROFILES, time.monotonic)
        measured.cpu_admission("v1", budget)
        measured.cpu_admission("v2", _budget())


@pytest.mark.parametrize("requested", (0, -1, True, 1.0))
def test_invalid_request_never_enters_the_candidate_queue(requested):
    budget = _budget()
    with UseSchedulingCandidate("borrowing", _evidence(budget)) as experiment:
        with pytest.raises(ValueError), _ports(budget)["v1"].reserve(requested, lambda: None):
            pass
        report = ProjectSchedulingExperiment(experiment)
        assert report["pending_requests"] == report["reserved_threads"] == 0


@pytest.mark.parametrize("candidate", ("fixed", "long_task_first", "borrowing"))
def test_candidate_runs_real_isolated_fit_and_zero_scan_model_and_sample_reuse(tmp_path, monkeypatch, candidate):
    import hashlib
    from pathlib import Path

    from scripts.training.jobs import training_alignment
    from scripts.training.samples.training_read_comparison import PrepareTrainingHistory
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.infra.settings import LoadRuntimeSettings
    from trader.infra.settings.data_paths import DataPaths
    from trader.training.infra import selected_training
    from trader.training.infra.cpu_admission import AvailableTrainingCpus
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader.training.infra.implementation_identity import TrainingImplementationIdentity
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    project = Path(__file__).resolve().parents[4]
    runtime = replace(LoadRuntimeSettings(project / "config/runtime.json"), data_paths=DataPaths(tmp_path / "source"), disk_medium="hdd")
    history = PrepareTrainingHistory(tmp_path / "source", size=4, sessions=2000, sqlite_policy=SqlitePolicyFixture())
    settings = LoadProfileTrainingSettings(project, "v3")
    settings = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=10))
    monkeypatch.setattr(selected_training, "LoadProfileTrainingSettings", lambda *_args: settings)
    monkeypatch.setattr("trader.infra.settings.LoadRuntimeSettings", lambda _path: runtime)
    source = SQLiteHistoryTrainingInputSnapshot.open(history, sqlite_policy=SqlitePolicyFixture())
    budget = _budget(native=2, available=AvailableTrainingCpus())
    raw = _document()
    raw.update(
        source_hash=source.snapshot.snapshot_hash,
        configuration_hash=hashlib.sha256(b"".join(hashlib.sha256((project / "config" / name).read_bytes()).digest()
                                                 for name in ("training.json", "features.json"))).hexdigest(),
        implementation_hash=TrainingImplementationIdentity(), available_cpus=budget.available_cpus,
        cpu_capacity=budget.capacity, native_threads=budget.native_threads,
        profiles=[{"profile": "v3", "fit_seconds": 1.0}],
    )
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(raw))
    report = training_alignment._probe(
        history, tmp_path / "output", ("v3",), sqlite_policy=SqlitePolicyFixture(),
        published_history_read_only=True, sample_cache_transfer="hardlink",
        scheduling_candidate=candidate, scheduling_evidence=path,
    )
    assert report["status"] == "passed", report
    summary = report["summary"]
    assert summary["source_files_unchanged"] and summary["source_data_writes"] == 0
    assert summary["repeat"]["execution"]["history_scan_passes"] == 0
    assert summary["sample_reuse"]["run"]["execution"]["history_scan_passes"] == 0
    schedule = summary["scheduling_candidate"]
    assert schedule["name"] == candidate and schedule["production_enabled"] is False
    assert schedule["pending_requests"] == schedule["reserved_threads"] == 0
    assert summary["repeat"]["execution"]["timing"]["profiles"][0]["cpu"]["acquired_count"] == 0
    assert summary["sample_reuse"]["preparation"]["fit_entry_counts"]["v3"] == 1
    # Initial fit, the existing controlled pre-fit failure, then the sample-cache refit.
    assert len(schedule["grants"]) == 3

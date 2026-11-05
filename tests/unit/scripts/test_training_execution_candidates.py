"""Explicit execution experiments restore process state and never alter default invocations."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import pytest

from scripts.training.jobs import training_alignment
from scripts.training.resources import execution_candidates as candidates
from tests.helpers.configuration import SqlitePolicyFixture
from tests.helpers.training_resources import TrainingResourceSettingsFixture
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.resources import TrainingProfileThreads
from trader.training.application.selected_training import SelectedTrainingRun, TrainingLocations
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingRequest

ROOT = Path(__file__).resolve().parents[3]
_ENVIRONMENT = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def _request(tmp_path, resources):
    return SelectedTrainingRequest(
        LoadRuntimeSettings(ROOT / "config/runtime.json"),
        ("v1", "v2", "v3", "v5"),
        (),
        lambda: False,
        TrainingMemoryBudget(1024),
        TrainingLocations(tmp_path / "history", tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(4, resources.cpu),
        fanout_settings=resources.fanout,
        sample_settings=resources.samples,
        memory_capacity_bytes=1024,
    )


@pytest.mark.parametrize("cpu", ("baseline", "two_plus_one", "three_single"))
@pytest.mark.parametrize("cancelled", (False, True))
def test_cpu_selection_and_cancelled_scope_restore_functions_and_environment(tmp_path, monkeypatch, cpu, cancelled):
    resources = TrainingResourceSettingsFixture()
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    policy = SqlitePolicyFixture()
    outcome = SelectedTrainingRun((), 0, 0)
    requests = []

    def train(request, executor, *, sqlite_policy):
        assert executor is pool and sqlite_policy is policy
        requests.append(request)
        return outcome

    monkeypatch.setattr(candidates.training_entrypoint, "TrainSelectedProfiles", train)
    monkeypatch.setenv(_ENVIRONMENT[0], "7")
    monkeypatch.delenv(_ENVIRONMENT[1], raising=False)
    monkeypatch.setenv(_ENVIRONMENT[2], "")
    monkeypatch.delenv(_ENVIRONMENT[3], raising=False)
    environment = tuple(os.environ.get(name) for name in _ENVIRONMENT)
    expected_threads = TrainingProfileThreads(1, 1, 1) if cpu == "three_single" else resources.cpu.profile_threads

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(InterruptedError, match="fixture cancellation") if cancelled else nullcontext():
            with candidates.UseTrainingExecutionCandidate("combined", cpu) as experiment:
                current = candidates.training_configuration.LoadTrainingResourceSettings(ROOT, "hdd")
                assert current.cpu.native_threads == (resources.cpu.native_threads if cpu == "baseline" else 1)
                assert current.cpu.profile_threads == expected_threads
                assert current.cpu.maximum_threads == resources.cpu.maximum_threads
                assert current.cpu.coordinator_cpus == resources.cpu.coordinator_cpus
                assert (current.memory, current.fanout, current.samples) == (resources.memory, resources.fanout, resources.samples)
                assert (candidates.training_configuration.LoadTrainingResourceSettings is loader) is (cpu == "baseline")
                request = _request(tmp_path, current)
                assert candidates.training_entrypoint.TrainSelectedProfiles(request, pool, sqlite_policy=policy) is outcome
                assert requests[-1] is not request
                assert request.execution_options == TrainingExecutionOptions()
                assert requests[-1].execution_options == TrainingExecutionOptions(True, True, True, True, True)
                projection = candidates.ProjectTrainingExecutionExperiment(experiment)
                assert projection["execution"] == "combined" and projection["cpu"] == cpu
                assert projection["production_enabled"] is projection["performance_evidence"] is False
                assert len(projection["candidate_source_hash"]) == 64
                assert projection["runs"] == [{
                    "profiles": request.profiles,
                    "cpu_capacity": 3,
                    "native_threads": current.cpu.native_threads,
                    "profile_threads": {"v1": expected_threads.v1, "v2": expected_threads.v2, "v3": expected_threads.v3},
                    "memory_capacity_bytes": 1024,
                    "batch_rows": current.fanout.batch_rows,
                    "queue_batches": current.fanout.queue_batches,
                }]
                for name in _ENVIRONMENT:
                    os.environ[name] = "1"
                if cancelled:
                    raise InterruptedError("fixture cancellation")
    assert candidates.training_entrypoint.TrainSelectedProfiles is train
    assert candidates.training_configuration.LoadTrainingResourceSettings is loader
    assert tuple(os.environ.get(name) for name in _ENVIRONMENT) == environment


@pytest.mark.parametrize(("execution", "expected"), (
    ("baseline", TrainingExecutionOptions()),
    ("resources", TrainingExecutionOptions(memory_first=True, release_cpu_before_publish=True)),
    ("ram_matrices", TrainingExecutionOptions(shared_matrix_memory=True)),
    ("shared_validation", TrainingExecutionOptions(shared_validation=True)),
    ("normalized_tcn", TrainingExecutionOptions(normalized_tcn_batches=True)),
    ("combined", TrainingExecutionOptions(True, True, True, True, True)),
))
def test_only_explicit_execution_candidate_options_are_injected(tmp_path, monkeypatch, execution, expected):
    request = _request(tmp_path, TrainingResourceSettingsFixture())
    requests = []
    outcome = SelectedTrainingRun((), 0, 0)

    def train(selected, _executor, *, sqlite_policy):
        requests.append(selected)
        return outcome

    monkeypatch.setattr(candidates.training_entrypoint, "TrainSelectedProfiles", train)
    with ThreadPoolExecutor(max_workers=1) as pool, candidates.UseTrainingExecutionCandidate(execution, "baseline") as experiment:
        assert candidates.training_entrypoint.TrainSelectedProfiles(request, pool, sqlite_policy=SqlitePolicyFixture()) is outcome
        assert requests[-1].execution_options == expected
        assert request.execution_options == TrainingExecutionOptions()
        if execution == "baseline":
            assert requests[-1] is request
            assert candidates.training_entrypoint.TrainSelectedProfiles is train
            assert experiment.runs() == ()
        else:
            assert requests[-1] is not request and len(experiment.runs()) == 1
        assert experiment.options == expected
    assert candidates.training_entrypoint.TrainSelectedProfiles is train


@pytest.mark.parametrize(("execution", "cpu", "failure"), (
    ("unknown", "baseline", "training_execution_candidate_invalid"),
    ("baseline", "unknown", "training_cpu_candidate_invalid"),
))
def test_unknown_candidate_is_rejected_without_patching(execution, cpu, failure):
    train = candidates.training_entrypoint.TrainSelectedProfiles
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    with pytest.raises(ValueError, match=failure), candidates.UseTrainingExecutionCandidate(execution, cpu):
        pytest.fail("invalid candidate entered")
    assert candidates.training_entrypoint.TrainSelectedProfiles is train
    assert candidates.training_configuration.LoadTrainingResourceSettings is loader


def test_candidate_cannot_patch_process_globals_from_worker_thread():
    def enter():
        with candidates.UseTrainingExecutionCandidate("combined", "three_single"):
            pytest.fail("worker entered process-wide experiment")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError, match="requires_main_thread"):
            pool.submit(enter).result(timeout=5)


@pytest.mark.parametrize("inputs_only", (False, True))
@pytest.mark.parametrize(("execution", "cpu"), (
    ("baseline", "baseline"),
    ("combined", "baseline"),
    ("baseline", "two_plus_one"),
    ("normalized_tcn", "three_single"),
))
def test_alignment_requires_training_mode_and_forwards_only_explicit_candidates(
    tmp_path, monkeypatch, inputs_only, execution, cpu,
):
    calls = []
    reports = []
    expected = {}
    if execution != "baseline":
        expected["execution_candidate"] = execution
    if cpu != "baseline":
        expected["cpu_candidate"] = cpu

    def select(_checkout):
        calls.append("selected")
        return tmp_path / "product"

    def inputs(*_args, **_kwargs):
        calls.append("inputs")
        return {"status": "passed"}

    def probe(_history, _work, _profiles, *, runtime_config, sqlite_policy, require_reuse, **options):
        assert options == expected
        assert runtime_config == tmp_path / "product/config/runtime.json"
        assert sqlite_policy is None and require_reuse is True
        calls.append("probed")
        return {"status": "passed"}

    argv = ["probe", "--history-root", str(tmp_path / "history")]
    argv += ["--inputs-only"] if inputs_only else ["--work-root", str(tmp_path / "output")]
    if execution != "baseline":
        argv += ["--execution-candidate", execution]
    if cpu != "baseline":
        argv += ["--cpu-candidate", cpu]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(training_alignment, "_select_product", select)
    monkeypatch.setattr(training_alignment, "_inputs", inputs)
    monkeypatch.setattr(training_alignment, "_probe", probe)
    monkeypatch.setattr(training_alignment, "emit_report", reports.append)
    rejected = inputs_only and bool(expected)
    assert training_alignment.Main() == int(rejected)
    assert calls == ([] if rejected else ["selected", "inputs" if inputs_only else "probed"])
    assert reports[0]["status"] == ("failed" if rejected else "passed")


@pytest.mark.parametrize("flag", ("--execution-candidate", "--cpu-candidate"))
def test_alignment_rejects_unknown_candidate_before_product_or_history_access(tmp_path, monkeypatch, flag):
    def forbidden(*_args):
        pytest.fail("invalid option reached checkout selection")

    monkeypatch.setattr(sys, "argv", ["probe", "--history-root", str(tmp_path / "history"), flag, "unknown"])
    monkeypatch.setattr(training_alignment, "_select_product", forbidden)
    with pytest.raises(SystemExit) as error:
        training_alignment.Main()
    assert error.value.code == 2

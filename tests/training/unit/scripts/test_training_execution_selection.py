"""Canonical winner subsets and bounded single-dimension fanout experiments."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.training.resources import execution_candidates as candidates
from scripts.training.resources import execution_selection as selection
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.resources import TrainingFanoutSettings, TrainingProfileThreads
from trader.training.application.selected_training import SelectedTrainingRun, TrainingLocations
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingRequest

ROOT = Path(__file__).resolve().parents[4]
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


def test_selection_import_is_product_free_and_the_runtime_does_not_reexport_its_names():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import scripts.training.resources.execution_selection; "
            "assert not any(name == 'trader' or name.startswith('trader.') for name in sys.modules)"
        )],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert all(not hasattr(candidates, name) for name in selection.__all__)


@pytest.mark.parametrize(("text", "expected"), (
    ("resources", ("resources",)),
    (" normalized_tcn , resources ", ("resources", "normalized_tcn")),
    ("shared_validation,ram_matrices", ("ram_matrices", "shared_validation")),
    (
        "normalized_tcn,shared_validation,ram_matrices,resources",
        ("resources", "ram_matrices", "shared_validation", "normalized_tcn"),
    ),
))
def test_component_parser_uses_one_fixed_order(text, expected):
    assert selection.ParseExecutionComponents(text) == expected


@pytest.mark.parametrize("value", (
    "", " ", "resources,", ",resources", "resources,,normalized_tcn", "unknown",
    "baseline", "combined", "Resources", "resources,resources", "resources, resources", None, 17,
))
def test_component_parser_rejects_empty_unknown_and_duplicate_inputs(value):
    with pytest.raises(ValueError, match="training_execution_components_invalid"):
        selection.ParseExecutionComponents(value)


@pytest.mark.parametrize(("components", "expected"), (
    (("resources",), TrainingExecutionOptions(memory_first=True, release_cpu_before_publish=True)),
    (("ram_matrices",), TrainingExecutionOptions(shared_matrix_memory=True)),
    (("shared_validation",), TrainingExecutionOptions(shared_validation=True)),
    (("normalized_tcn",), TrainingExecutionOptions(normalized_tcn_batches=True)),
    (
        ("resources", "normalized_tcn"),
        TrainingExecutionOptions(memory_first=True, release_cpu_before_publish=True, normalized_tcn_batches=True),
    ),
    (
        ("ram_matrices", "shared_validation"),
        TrainingExecutionOptions(shared_matrix_memory=True, shared_validation=True),
    ),
))
def test_selected_enables_only_the_requested_winners(tmp_path, monkeypatch, components, expected):
    request = _request(tmp_path, TrainingResourceSettingsFixture())
    received = []
    outcome = SelectedTrainingRun((), 0, 0)

    def train(current, executor, *, sqlite_policy):
        assert executor is pool and sqlite_policy is policy
        received.append(current)
        return outcome

    policy = SqlitePolicyFixture()
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    monkeypatch.setattr(candidates.training_entrypoint, "TrainSelectedProfiles", train)
    with ThreadPoolExecutor(max_workers=1) as pool, candidates.UseTrainingExecutionCandidate(
        "selected", "baseline", components=components,
    ) as experiment:
        assert candidates.training_configuration.LoadTrainingResourceSettings is loader
        assert candidates.training_entrypoint.TrainSelectedProfiles(request, pool, sqlite_policy=policy) is outcome
        assert received[-1].execution_options == expected
        assert received[-1].fanout_settings == request.fanout_settings
        assert received[-1].cpu_budget is request.cpu_budget
        assert request.execution_options == TrainingExecutionOptions()
        report = candidates.ProjectTrainingExecutionExperiment(experiment)
        assert report["components"] == components and report["fanout"] == "baseline"
        assert report["production_enabled"] is report["performance_evidence"] is False
        assert report["runs"][0]["batch_rows"] == request.fanout_settings.batch_rows
        assert report["runs"][0]["queue_batches"] == request.fanout_settings.queue_batches
    assert candidates.training_entrypoint.TrainSelectedProfiles is train


@pytest.mark.parametrize(("execution", "components", "fanout"), (
    ("selected", (), "baseline"),
    ("selected", ("resources", "resources"), "baseline"),
    ("selected", ("normalized_tcn", "resources"), "baseline"),
    ("selected", ("unknown",), "baseline"),
    ("selected", (" resources",), "baseline"),
    ("selected", ["resources"], "baseline"),
    ("selected", (17,), "baseline"),
    ("baseline", ("resources",), "baseline"),
    ("combined", ("resources",), "baseline"),
    ("resources", ("resources",), "baseline"),
    ("baseline", (), "both"),
))
def test_invalid_subset_and_fanout_are_rejected_before_hooks(execution, components, fanout):
    train = candidates.training_entrypoint.TrainSelectedProfiles
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    before = tuple(os.environ.get(name) for name in _ENVIRONMENT)
    with pytest.raises(ValueError), candidates.UseTrainingExecutionCandidate(
        execution, "baseline", components=components, fanout=fanout,
    ):
        pytest.fail("invalid experiment entered")
    assert candidates.training_entrypoint.TrainSelectedProfiles is train
    assert candidates.training_configuration.LoadTrainingResourceSettings is loader
    assert tuple(os.environ.get(name) for name in _ENVIRONMENT) == before


@pytest.mark.parametrize("medium", ("hdd", "ssd"))
@pytest.mark.parametrize("cpu", ("baseline", "two_plus_one", "three_single"))
@pytest.mark.parametrize("fanout", ("batch_double", "queue_double"))
def test_fanout_changes_one_configured_dimension_and_observes_actual_requests(
    tmp_path, monkeypatch, medium, cpu, fanout,
):
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    baseline = loader(ROOT, medium)
    source = (ROOT / "config/training.json").read_bytes()
    received = []
    outcome = SelectedTrainingRun((), 0, 0)

    def train(request, _executor, *, sqlite_policy):
        received.append(request)
        return outcome

    monkeypatch.setattr(candidates.training_entrypoint, "TrainSelectedProfiles", train)
    with ThreadPoolExecutor(max_workers=1) as pool, candidates.UseTrainingExecutionCandidate(
        "baseline", cpu, fanout=fanout,
    ) as experiment:
        resources = candidates.training_configuration.LoadTrainingResourceSettings(ROOT, medium)
        expected = (
            TrainingFanoutSettings(baseline.fanout.batch_rows * 2, baseline.fanout.queue_batches)
            if fanout == "batch_double" else
            TrainingFanoutSettings(baseline.fanout.batch_rows, baseline.fanout.queue_batches * 2)
        )
        assert resources.fanout == expected
        if cpu == "baseline":
            assert resources.cpu == baseline.cpu
        else:
            assert resources.cpu.native_threads == 1
            assert resources.cpu.profile_threads == (
                TrainingProfileThreads(1, 1, 1) if cpu == "three_single" else baseline.cpu.profile_threads
            )
        assert resources.memory == baseline.memory and resources.samples == baseline.samples
        request = _request(tmp_path, resources)
        assert candidates.training_entrypoint.TrainSelectedProfiles(request, pool, sqlite_policy=SqlitePolicyFixture()) is outcome
        assert received[-1].execution_options == TrainingExecutionOptions()
        report = candidates.ProjectTrainingExecutionExperiment(experiment)
        assert report["execution"] == "baseline" and report["components"] == ()
        assert report["fanout"] == fanout
        assert report["runs"][0]["batch_rows"] == expected.batch_rows
        assert report["runs"][0]["queue_batches"] == expected.queue_batches
        assert report["runs"][0]["native_threads"] == resources.cpu.native_threads
    assert candidates.training_configuration.LoadTrainingResourceSettings is loader
    assert candidates.training_entrypoint.TrainSelectedProfiles is train
    assert (ROOT / "config/training.json").read_bytes() == source


@pytest.mark.parametrize(("fanout", "batch_rows", "queue_batches", "allowed"), (
    ("batch_double", 1024, 4, True),
    ("queue_double", 2048, 4, True),
    ("batch_double", 1025, 1, False),
    ("queue_double", 256, 5, False),
    ("batch_double", 1024, 9, False),
    ("queue_double", 2049, 1, False),
))
def test_fanout_caps_and_loader_failure_restore_the_scope(monkeypatch, fanout, batch_rows, queue_batches, allowed):
    original = replace(TrainingResourceSettingsFixture(), fanout=TrainingFanoutSettings(batch_rows, queue_batches))

    def load(_root, _medium):
        return original

    train = candidates.training_entrypoint.TrainSelectedProfiles
    monkeypatch.setattr(candidates.training_configuration, "LoadTrainingResourceSettings", load)
    monkeypatch.setenv(_ENVIRONMENT[0], "7")
    before = tuple(os.environ.get(name) for name in _ENVIRONMENT)
    with nullcontext() if allowed else pytest.raises(ValueError, match="training_fanout_candidate_exceeds_limit"):
        with candidates.UseTrainingExecutionCandidate("baseline", "baseline", fanout=fanout):
            os.environ[_ENVIRONMENT[0]] = "1"
            resources = candidates.training_configuration.LoadTrainingResourceSettings(ROOT, "hdd")
            assert resources.fanout.batch_rows <= 2048 and resources.fanout.queue_batches <= 8
            assert resources.cpu == original.cpu
    assert original.fanout == TrainingFanoutSettings(batch_rows, queue_batches)
    assert candidates.training_configuration.LoadTrainingResourceSettings is load
    assert candidates.training_entrypoint.TrainSelectedProfiles is train
    assert tuple(os.environ.get(name) for name in _ENVIRONMENT) == before


@pytest.mark.parametrize("cancelled", (False, True))
def test_selected_and_fanout_restore_nested_patches_and_existing_or_missing_environment(monkeypatch, cancelled):
    train = candidates.training_entrypoint.TrainSelectedProfiles
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    monkeypatch.setenv(_ENVIRONMENT[0], "7")
    monkeypatch.delenv(_ENVIRONMENT[1], raising=False)
    monkeypatch.setenv(_ENVIRONMENT[2], "")
    monkeypatch.delenv(_ENVIRONMENT[3], raising=False)
    before = tuple(os.environ.get(name) for name in _ENVIRONMENT)
    with candidates.UseTrainingExecutionCandidate("selected", "baseline", components=("resources",)):
        outer = candidates.training_entrypoint.TrainSelectedProfiles
        os.environ[_ENVIRONMENT[0]] = "outer"
        with pytest.raises(InterruptedError, match="fixture cancellation") if cancelled else nullcontext():
            with candidates.UseTrainingExecutionCandidate("baseline", "baseline", fanout="batch_double"):
                assert candidates.training_entrypoint.TrainSelectedProfiles is not outer
                assert candidates.training_configuration.LoadTrainingResourceSettings is not loader
                for name in _ENVIRONMENT:
                    os.environ[name] = "inner"
                if cancelled:
                    raise InterruptedError("fixture cancellation")
        assert candidates.training_entrypoint.TrainSelectedProfiles is outer
        assert candidates.training_configuration.LoadTrainingResourceSettings is loader
        assert os.environ[_ENVIRONMENT[0]] == "outer"
    assert candidates.training_entrypoint.TrainSelectedProfiles is train
    assert candidates.training_configuration.LoadTrainingResourceSettings is loader
    assert tuple(os.environ.get(name) for name in _ENVIRONMENT) == before


def test_default_scope_does_not_install_hooks_or_inject_a_selection():
    train = candidates.training_entrypoint.TrainSelectedProfiles
    loader = candidates.training_configuration.LoadTrainingResourceSettings
    before = tuple(os.environ.get(name) for name in _ENVIRONMENT)
    with candidates.UseTrainingExecutionCandidate("baseline", "baseline") as experiment:
        assert candidates.training_entrypoint.TrainSelectedProfiles is train
        assert candidates.training_configuration.LoadTrainingResourceSettings is loader
        assert experiment.options == TrainingExecutionOptions() and experiment.runs() == ()
        report = candidates.ProjectTrainingExecutionExperiment(experiment)
        assert report["components"] == () and report["fanout"] == "baseline"
    assert tuple(os.environ.get(name) for name in _ENVIRONMENT) == before

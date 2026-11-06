"""Sample observation reaches real builder boundaries without fitting or publishing models."""

from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.profiles.v1.learned_rank_training import _History, _settings
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.infra.settings import RuntimeSettings
from trader.training.application.selected_training import TrainingLocations
from trader.training.infra import engine
from trader.training.infra import selected_training
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.execution_timing import TrainingExecutionMeasurements
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.profile.v1 import training as v1_training
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_builder import TrainingSampleBuildRequest
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE))
@pytest.mark.parametrize("state", ("built", "reused", "failed", "cancelled"))
def test_standard_builder_observation_distinguishes_reuse_and_preserves_failures(monkeypatch, profile, state):
    wall, cpu = _Clock(), _Clock()
    observed = TrainingExecutionMeasurements((profile.profile_id,), wall, thread_time=cpu)
    request = Mock(spec=TrainingSampleBuildRequest)
    request.profile = profile
    request.progress = None
    request.index = Mock(spec=SQLiteTrainingSampleIndex)
    request.index.count.return_value = 1
    request.index.validation_target_metrics.return_value = ()
    cache = Mock(spec=TrainingSampleCache)
    cache.reused = state == "reused"
    rows = iter(())
    failure = InterruptedError("sample_cancelled") if state == "cancelled" else ValueError("sample_failed")

    def build(actual, *, aligned_rows):
        assert actual is request and aligned_rows is rows
        assert observed.snapshot().profiles[0].sample_build.inflight_count == 1
        wall.value += 7
        cpu.value += 2
        if state in {"failed", "cancelled"}:
            raise failure

    builder = Mock(side_effect=build)
    monkeypatch.setattr(engine, "BuildTrainingSamples", builder)
    outcome = pytest.raises(type(failure)) if state in {"failed", "cancelled"} else nullcontext()
    with outcome as caught:
        assert engine._prepare_samples(request, cache, aligned_rows=rows, observer=observed) == ()
    if state in {"failed", "cancelled"}:
        assert caught.value is failure
    timing = observed.snapshot().profiles[0].sample_build
    assert timing.count == builder.call_count == int(state != "reused")
    assert timing.completed_count == int(state == "built")
    assert timing.inflight_count == 0
    assert timing.wall_seconds == (None if state == "reused" else 7)
    assert timing.thread_cpu_seconds == (None if state == "reused" else 2)
    assert cache.seal.call_count == int(state == "built")


@pytest.mark.parametrize("failure", (ValueError("sample_failed"), InterruptedError("sample_cancelled")))
def test_v1_builder_observation_survives_failure_without_entering_fitting(tmp_path, monkeypatch, failure):
    wall, cpu = _Clock(), _Clock()
    observed = TrainingExecutionMeasurements(("v1",), wall, thread_time=cpu)

    def build(*_args, **_kwargs):
        assert observed.snapshot().profiles[0].sample_build.inflight_count == 1
        wall.value += 5
        cpu.value += 1
        raise failure

    monkeypatch.setattr(v1_training, "BuildLearnedRankSamples", build)
    monkeypatch.setattr(v1_training, "FitLearnedRankHead", lambda *_args, **_kwargs: pytest.fail("fitter entered"))
    monkeypatch.setattr(v1_training, "PublishLearnedRankPair", lambda *_args, **_kwargs: pytest.fail("model published"))
    outcome = pytest.raises(InterruptedError) if isinstance(failure, InterruptedError) else nullcontext()
    with outcome:
        result = v1_training.RunV1Training(
            _History,
            tmp_path / "models",
            history_root=tmp_path / "history",
            sample_root=tmp_path / "samples",
            settings=_settings(),
            sample_settings=TrainingSampleSettingsFixture(),
            sqlite_policy=SqlitePolicyFixture(),
            aligned_rows=iter(()),
            execution_observer=observed,
        )
        assert result.status == "failed" and result.failure_reasons == ("ValueError",)
    timing = observed.snapshot().profiles[0].sample_build
    assert timing.count == 1 and timing.completed_count == timing.inflight_count == 0
    assert timing.wall_seconds == 5 and timing.thread_cpu_seconds == 1
    assert not list((tmp_path / "models").rglob("model.json"))


@pytest.mark.parametrize("profile", ("v1", "v3"))
def test_selected_factory_injects_the_same_job_observer_into_both_builder_owners(tmp_path, monkeypatch, profile):
    wall = _Clock()
    observed = TrainingExecutionMeasurements((profile,), wall)
    runtime = Mock(spec=RuntimeSettings)
    runtime.project_root = Path(__file__).resolve().parents[4]
    selection = selected_training.SelectedTrainingRequest(
        runtime,
        (profile,),
        (Mock(),),
        lambda: False,
        TrainingMemoryBudget(1024),
        TrainingLocations(tmp_path / "history", tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(3, settings=TrainingResourceSettingsFixture().cpu),
        fanout_settings=TrainingResourceSettingsFixture().fanout,
        sample_settings=TrainingSampleSettingsFixture(),
    )
    received = []
    result = v1_training.V1TrainingResult("failed", "fixture", "a" * 64, 0)

    def run_v1(*_args, **kwargs):
        received.append(kwargs["execution_observer"])
        return result

    def inspect(_history, request, *, sqlite_policy):
        return request, None, frozenset(), None

    def run_standard(_history, request, _cache, aligned_rows, *, sqlite_policy):
        received.append(request.execution_observer)
        return result

    monkeypatch.setattr(selected_training, "LoadV1TrainingSettings", lambda _root: _settings())
    monkeypatch.setattr(selected_training, "InspectV1Training", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(selected_training, "RunV1Training", run_v1)
    monkeypatch.setattr(selected_training, "InspectSelectedProfile", inspect)
    monkeypatch.setattr(selected_training, "FitSelectedProfile", run_standard)
    with ExitStack() as resources:
        factory = selected_training._TrainingTaskFactory(
            selection, _History(), resources, observed, sqlite_policy=SqlitePolicyFixture()
        )
        task = factory.prepare(profile, selection.progresses[0])
        assert task.execute(iter(())) is result
    assert received == [observed]

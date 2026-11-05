"""Four real profile workers share one synthetic history scan and retain independent pairs."""

from tests.helpers.training_resources import TrainingSampleSettingsFixture

from tests.helpers.training_resources import TrainingResourceSettingsFixture

from tests.helpers.configuration import SqlitePolicyFixture

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path
from threading import Event

import pytest

from tests.helpers.model_publication import CrashProfilePublication
from tests.unit.training.infra.profile.v1.test_learned_rank_training import _settings
from tests.unit.training.infra.profile.v5.test_v5_training import _history
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError
from trader.training.application.selected_training import SelectedTrainingTask, TrainingLocations
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.application.resources import TrainingProfileThreads
from trader.training.infra.execution_options import TrainingExecutionOptions
from trader.training.infra.selected_training import SelectedTrainingRequest, TrainSelectedProfiles
from trader.training.infra.training_settings import LoadProfileTrainingSettings
from tests.helpers.configuration import TcnFittingParametersFixture

ROOT = Path(__file__).resolve().parents[4]


class _Progress:
    def publish(self, _update):
        pass


@pytest.mark.parametrize("terminal", ("worker_failure", "source_failure", "cancelled", "success"))
def test_shared_reader_closes_nested_streams_even_when_callers_retain_them(tmp_path, monkeypatch, terminal):
    from trader.training.infra import selected_training

    history = _history(tmp_path / "history")
    original_read = history.iter_training_rows
    original_align = selected_training.AlignTrainingRows
    retained, worker_rows = [], []
    cancelled, source_closed = Event(), Event()

    def source(dates):
        from contextlib import closing

        try:
            with closing(original_read(dates)) as rows:
                retained.append(rows)
                for index, row in enumerate(rows):
                    if index == 300:
                        if terminal == "source_failure":
                            raise OSError("fixture_source_failure")
                        if terminal == "cancelled":
                            cancelled.set()
                    yield row
        finally:
            source_closed.set()

    def align(*args):
        rows = original_align(*args)
        retained.append(rows)
        return rows

    def fit(rows):
        worker_rows.append(rows)
        if terminal == "worker_failure":
            next(rows)
            raise ValueError("fixture_worker_failure")
        return len(tuple(rows))

    monkeypatch.setattr(history, "iter_training_rows", source)
    monkeypatch.setattr(selected_training, "AlignTrainingRows", align)
    monkeypatch.setattr(selected_training.SQLiteHistoryTrainingInputSnapshot, "open", lambda *_a, **_kw: history)
    monkeypatch.setattr(selected_training._TrainingTaskFactory, "prepare", lambda _self, profile, _progress:
                        SelectedTrainingTask(profile, frozenset(history.snapshot.calendar.open_dates), fit))
    request = SelectedTrainingRequest(
        LoadRuntimeSettings(ROOT / "config/runtime.json"),
        ("v1", "v5"),
        (_Progress(), _Progress()),
        cancelled.is_set,
        TrainingMemoryBudget(1024),
        TrainingLocations(history.history_root, tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(2, settings=TrainingResourceSettingsFixture().cpu),
        fanout_settings=TrainingResourceSettingsFixture().fanout,
        sample_settings=TrainingSampleSettingsFixture(),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        if terminal in ("source_failure", "cancelled"):
            with pytest.raises(OSError if terminal == "source_failure" else InterruptedError):
                TrainSelectedProfiles(request, executor, sqlite_policy=SqlitePolicyFixture())
        else:
            result = TrainSelectedProfiles(request, executor, sqlite_policy=SqlitePolicyFixture())
            assert all(item.failure == ("fixture_worker_failure" if terminal == "worker_failure" else None)
                       for item in result.outcomes)
    assert source_closed.is_set()
    assert len(retained) == 2 and len(worker_rows) == 2
    assert all(rows.gi_frame is None for rows in (*retained, *worker_rows))


@pytest.mark.parametrize("publication_exit", ("old_moved", "new_moved"))
@pytest.mark.parametrize("execution_case", ("baseline", "combined"))
def test_four_real_workers_one_scan_then_standard_model_or_sample_reuse(tmp_path, monkeypatch, publication_exit, execution_case):
    import torch
    from trader.training.infra.profile.v5 import network

    original_threads = torch.get_num_threads()
    original_deterministic = torch.are_deterministic_algorithms_enabled()
    tcn_threads = []
    configure = network.ConfigureTcnRuntime

    def configure_tcn(*, training, compute_threads):
        configure(training=training, compute_threads=compute_threads)
        tcn_threads.append(torch.get_num_threads())

    monkeypatch.setattr(network, "ConfigureTcnRuntime", configure_tcn)
    history = _history(tmp_path / "history")
    original = history.iter_training_rows
    scans = []
    missing_day = history.snapshot.calendar.open_dates[-10]

    def rows(dates, progress=None):
        scans.append(dates)
        for row in original(dates, progress):
            yield (
                replace(row, point=None, cell_status="unknown_missing")
                if row.trade_date == missing_day and row.code == "600005"
                else row
            )

    history.iter_training_rows = rows
    monkeypatch.setattr(
        "trader.training.infra.selected_training.SQLiteHistoryTrainingInputSnapshot.open", lambda _p, *, sqlite_policy: history
    )
    monkeypatch.setattr("trader.training.infra.selected_training.LoadV1TrainingSettings", lambda _p: _settings())

    def settings(project, profile):
        result = LoadProfileTrainingSettings(project, profile)
        if profile == "v5":
            return replace(
                result,
                fitting=TcnFittingParametersFixture(
                    minimum_industry_training_rows=10,
                    epochs=2,
                    early_stopping_rounds=2,
                ),
            )
        fitting = replace(result.fitting, minimum_industry_training_rows=10)
        if profile == "v2":
            fitting = replace(fitting, boosting_rounds=12, early_stopping_rounds=3, minimum_leaf_rows=5)
        return replace(result, fitting=fitting)

    monkeypatch.setattr("trader.training.infra.selected_training.LoadProfileTrainingSettings", settings)
    candidate = execution_case == "combined"
    cpu_settings = TrainingResourceSettingsFixture().cpu
    if candidate:
        cpu_settings = replace(cpu_settings, native_threads=1, profile_threads=TrainingProfileThreads(1, 1, 1))
    request = SelectedTrainingRequest(
        LoadRuntimeSettings(ROOT / "config/runtime.json"),
        ("v1", "v2", "v3", "v5"),
        tuple(_Progress() for _ in range(4)),
        lambda: False,
        TrainingMemoryBudget(3 * 1024**3),
        TrainingLocations(history.history_root, tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(4 if candidate else 2, settings=cpu_settings),
        fanout_settings=TrainingResourceSettingsFixture().fanout,
        sample_settings=TrainingSampleSettingsFixture(),
        memory_capacity_bytes=3 * 1024**3,
        execution_options=TrainingExecutionOptions(*(candidate for _ in range(5))),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        result = TrainSelectedProfiles(request, executor, sqlite_policy=SqlitePolicyFixture())
    assert len(scans) == result.history_scan_passes == 1
    assert tuple(item.profile for item in result.outcomes) == request.profiles
    assert all(item.failure is None and item.result.status == "engineering_ready" for item in result.outcomes), result
    assert len(tuple((tmp_path / "models").rglob("model.json"))) == 8
    assert 1 <= request.cpu_budget.peak_reserved_threads <= (3 if candidate else 1)
    assert all("[num_threads: 1]" in head.rank_model for head in result.outcomes[0].result.heads)
    assert tcn_threads == [1]
    assert torch.get_num_threads() == original_threads
    assert torch.are_deterministic_algorithms_enabled() == original_deterministic
    assert result.timing is not None
    assert all(item.cpu.acquired_count == item.memory.acquired_count == 1 for item in result.timing.profiles)
    assert all(item.cpu.inflight_count == item.memory.inflight_count == 0 for item in result.timing.profiles)
    assert all(item.finished_after_seconds >= item.started_after_seconds >= 0 for item in result.timing.profiles)
    before = {
        path.relative_to(request.paths.models): path.read_bytes()
        for profile in request.profiles
        for path in (request.paths.models / profile).rglob("*.json")
    }
    for profile in request.profiles:
        process = get_context("spawn").Process(
            target=CrashProfilePublication, args=(request.paths.models, profile, publication_exit)
        )
        process.start()
        try:
            process.join(45)
            assert process.exitcode == 86
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            process.close()
        root = request.paths.models / profile
        assert root.exists() == (publication_exit == "new_moved")
        assert len(tuple(root.parent.glob(f".{profile}.previous.*"))) == 1
        if publication_exit == "old_moved":
            with pytest.raises(ScoringModelsUnavailableError):
                LoadScoringProfile(profile, training_root=request.paths.models)
            assert not root.exists()  # The serving reader never repairs a model directory.
    second = request
    with ThreadPoolExecutor(max_workers=4) as executor:
        reused = TrainSelectedProfiles(second, executor, sqlite_policy=SqlitePolicyFixture())
    assert len(scans) == 1
    assert reused.history_scan_passes == reused.history_rows_read == 0
    assert all(item.result.reused for item in reused.outcomes)
    assert all((request.paths.models / relative).read_bytes() == content for relative, content in before.items())
    assert all(
        len(tuple(request.paths.models.glob(f".{profile}.previous.*"))) == (publication_exit == "new_moved")
        for profile in request.profiles
    )
    assert reused.timing is not None and reused.timing.source_read_seconds == 0
    assert all(item.cpu.acquired_count == item.memory.acquired_count == 0 for item in reused.timing.profiles)

    def missing_dependency():
        raise ImportError("fixture missing optional torch")

    monkeypatch.setattr(network, "PreserveTcnRuntime", missing_dependency)
    with ThreadPoolExecutor(max_workers=4) as executor:
        partial_failure = TrainSelectedProfiles(request, executor, sqlite_policy=SqlitePolicyFixture())
    assert all(item.result.reused for item in partial_failure.outcomes[:3])
    assert partial_failure.outcomes[3].failure == "ImportError"
    assert partial_failure.history_scan_passes == 0

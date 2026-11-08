from tests.helpers.training.training_resources import TrainingSampleSettingsFixture

from tests.helpers.training.training_resources import TrainingResourceSettingsFixture

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from multiprocessing import get_context
from threading import Event

import pytest

from tests.helpers.training.model_publication import CrashProfilePublication
from tests.helpers.training.profiles.v5.v5_training import _history
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError
from trader.training.application.selected_training import SelectedTrainingTask, TrainingLocations
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.selected_training import SelectedTrainingRequest, SelectedTrainingAdapter
from trader.training.application.selected_training import TrainSelectedProfiles
from tests.helpers.training.execution.selected_profiles import (
    ROOT,
    FourProfileSeed,
    _Progress,
    ReadModelFiles,
    BuildFourProfileRequest,
    four_profile_seed as four_profile_seed,
)


_CURRENT_INDUSTRY = CurrentIndustrySnapshotFixture(("000001", *(f"60000{number}" for number in range(6))))


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
                TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at), executor)
        else:
            result = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at), executor)
            assert all(item.failure == ("fixture_worker_failure" if terminal == "worker_failure" else None)
                       for item in result.outcomes)
    assert source_closed.is_set()
    assert len(retained) == 2 and len(worker_rows) == 2
    assert all(rows.gi_frame is None for rows in (*retained, *worker_rows))


@pytest.mark.slow_training
@pytest.mark.parametrize("publication_exit", ("old_moved", "new_moved"))
def test_four_real_workers_one_scan_then_standard_model_or_sample_reuse(
    tmp_path, monkeypatch, publication_exit, four_profile_seed: FourProfileSeed,
):
    import torch
    from trader.training.infra.profile.v5 import network

    original_threads = torch.get_num_threads()
    original_deterministic = torch.are_deterministic_algorithms_enabled()
    scans: list[frozenset[date]] = []
    tcn_threads: list[int] = []
    request = BuildFourProfileRequest(tmp_path, four_profile_seed.execution_case, monkeypatch, scans, tcn_threads)
    shutil.copytree(four_profile_seed.models, request.paths.models)
    assert ReadModelFiles(request.paths.models) == four_profile_seed.files
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
    with ThreadPoolExecutor(max_workers=4) as executor:
        reused = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at), executor)
    assert not scans
    assert reused.history_scan_passes == reused.history_rows_read == 0
    assert all(item.result.reused for item in reused.outcomes)
    assert all((request.paths.models / relative).read_bytes() == content for relative, content in before.items())
    assert all(
        len(tuple(request.paths.models.glob(f".{profile}.previous.*"))) == (publication_exit == "new_moved")
        for profile in request.profiles
    )
    assert reused.timing is not None and reused.timing.source_read_seconds == 0
    assert all(item.cpu.acquired_count == item.memory.acquired_count == 0 for item in reused.timing.profiles)
    assert not tcn_threads
    assert torch.get_num_threads() == original_threads
    assert torch.are_deterministic_algorithms_enabled() == original_deterministic

    def missing_dependency():
        raise ImportError("fixture missing optional torch")

    monkeypatch.setattr(network, "PreserveTcnRuntime", missing_dependency)
    with ThreadPoolExecutor(max_workers=4) as executor:
        partial_failure = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=SqlitePolicyFixture(), load_industry_snapshot=lambda _observed: _CURRENT_INDUSTRY, clock=lambda: _CURRENT_INDUSTRY.observed_at), executor)
    assert all(item.result.reused for item in partial_failure.outcomes[:3])
    assert partial_failure.outcomes[3].failure == "ImportError"
    assert partial_failure.history_scan_passes == 0
    assert not scans
    assert ReadModelFiles(four_profile_seed.models) == four_profile_seed.files

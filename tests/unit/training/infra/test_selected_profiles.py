"""Four real profile workers share one synthetic history scan and retain independent pairs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from tests.unit.training.infra.profile.v1.test_learned_rank_training import _settings
from tests.unit.training.infra.profile.v5.test_v5_training import _history
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.selected_training import TrainingLocations
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingRequest, TrainSelectedProfiles
from trader.training.infra.training_settings import LoadProfileTrainingSettings
from tests.helpers.configuration import TcnFittingParametersFixture

ROOT = Path(__file__).resolve().parents[4]


class _Progress:
    def publish(self, _update):
        pass


def test_four_real_workers_one_scan_then_standard_model_or_sample_reuse(tmp_path, monkeypatch):
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
        "trader.training.infra.selected_training.SQLiteHistoryTrainingInputSnapshot.open", lambda _p: history
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
    request = SelectedTrainingRequest(
        LoadRuntimeSettings(ROOT / "config/runtime.json"),
        ("v1", "v2", "v3", "v5"),
        tuple(_Progress() for _ in range(4)),
        lambda: False,
        TrainingMemoryBudget(3 * 1024**3),
        TrainingLocations(history.history_root, tmp_path / "models", tmp_path / "samples"),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        result = TrainSelectedProfiles(request, executor)
    assert len(scans) == result.history_scan_passes == 1
    assert tuple(item.profile for item in result.outcomes) == request.profiles
    assert all(item.failure is None and item.result.status == "engineering_ready" for item in result.outcomes), result
    assert len(tuple((tmp_path / "models").rglob("model.json"))) == 8
    second = request
    with ThreadPoolExecutor(max_workers=4) as executor:
        reused = TrainSelectedProfiles(second, executor)
    assert len(scans) == 1
    assert reused.history_scan_passes == reused.history_rows_read == 0
    assert all(item.result.reused for item in reused.outcomes)

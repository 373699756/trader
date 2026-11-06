"""Real published history must survive four-profile fitting, publication and reuse."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.training.samples.training_read_comparison import PrepareTrainingHistory
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.configuration import TcnFittingParametersFixture
from tests.helpers.training.execution.selected_profiles import ROOT, _Progress
from tests.helpers.training.profiles.v1.learned_rank_training import _settings
from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.selected_training import TrainingLocations
from trader.training.infra import selected_training
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.application.profile_training import TrainingRunResult
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.selected_training import SelectedTrainingRequest, SelectedTrainingAdapter
from trader.training.application.selected_training import TrainSelectedProfiles
from trader.training.infra.training_settings import LoadProfileTrainingSettings


@pytest.mark.slow_training
def test_four_profiles_publish_and_reuse_the_real_history_calendar(tmp_path: Path, monkeypatch) -> None:
    policy = SqlitePolicyFixture()
    history_root = PrepareTrainingHistory(tmp_path / "source", size=6, sessions=2000, sqlite_policy=policy)
    source = SQLiteHistoryTrainingInputSnapshot.open(history_root, sqlite_policy=policy)
    published = source.published_snapshot

    def settings(project: Path, profile: str):
        result = LoadProfileTrainingSettings(project, profile)
        if profile == "v5":
            return replace(
                result,
                fitting=TcnFittingParametersFixture(minimum_industry_training_rows=10, epochs=2, early_stopping_rounds=2),
            )
        fitting = replace(result.fitting, minimum_industry_training_rows=10)
        if profile == "v2":
            fitting = replace(fitting, boosting_rounds=3, early_stopping_rounds=2, minimum_leaf_rows=2)
        return replace(result, fitting=fitting)

    monkeypatch.setattr(selected_training, "LoadProfileTrainingSettings", settings)
    monkeypatch.setattr(selected_training, "LoadV1TrainingSettings", lambda _project: _settings())
    resources = TrainingResourceSettingsFixture()
    memory_capacity = 768 * 1024**2
    request = SelectedTrainingRequest(
        LoadRuntimeSettings(ROOT / "config/runtime.json"),
        ("v1", "v2", "v3", "v5"),
        tuple(_Progress() for _ in range(4)),
        lambda: False,
        TrainingMemoryBudget(memory_capacity),
        TrainingLocations(history_root, tmp_path / "models", tmp_path / "samples"),
        cpu_budget=TrainingCpuBudget(2, settings=resources.cpu),
        fanout_settings=resources.fanout,
        sample_settings=resources.samples,
        memory_capacity_bytes=memory_capacity,
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        initial = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=policy), executor)
        assert initial.history_scan_passes == 1
        for outcome in initial.outcomes:
            assert outcome.failure is None, (outcome.profile, outcome.failure)
            assert outcome.result is not None
            failures = (
                tuple(head.failure_reasons for head in outcome.result.heads)
                if isinstance(outcome.result, TrainingRunResult)
                else outcome.result.failure_reasons
            )
            assert outcome.result.status == "engineering_ready", (outcome.profile, failures)
        files = {path: path.read_bytes() for path in request.paths.models.glob("*/*/*.json")}
        assert len(files) == 16
        for path, content in files.items():
            if path.name == "metadata.json":
                inputs = json.loads(content)["training_input"]
                assert inputs["calendar_hash"] == source.snapshot.calendar.content_hash
                assert inputs["training_input_hash"] == published.content_hash
                assert inputs["source_identity_hash"] == published.source_identity_hash
        repeated = TrainSelectedProfiles(SelectedTrainingAdapter(request, sqlite_policy=policy), executor)

    assert repeated.history_scan_passes == repeated.history_rows_read == 0
    assert all(
        item.failure is None and item.result is not None and item.result.reused for item in repeated.outcomes
    )
    assert {path: path.read_bytes() for path in files} == files
    current = SQLiteHistoryTrainingInputSnapshot.open(history_root, sqlite_policy=policy)
    assert current.published_snapshot == published

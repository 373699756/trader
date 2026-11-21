from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.training.training_resources import TrainingResourceSettingsFixture

from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain
from trader.training.infra.tomorrow_probability import LoadTomorrowProbabilityModel, PublishTomorrowProbabilityModel
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.tomorrow_probability_training import (
    TrainSharedTomorrowProbability, _collect_samples, _fit_tree, _label, _train,
)


def _row(day: date, code: str, close: float, *, opening: float = 100, preclose: float = 100) -> HistoryTrainingRow:
    return HistoryTrainingRow(
        code,
        day,
        "main" if code.startswith("6") else "chinext",
        None,
        HistoryTrainingPoint(
            day,
            close,
            20_000_000,
            raw_open=opening,
            raw_high=max(close, opening) + 1,
            raw_low=min(close, opening) - 1,
            raw_close_price=close,
            raw_preclose=preclose,
            raw_volume=10_000,
        ),
        "complete",
    )


def test_label_preserves_exact_hit_and_unknown_or_suspension() -> None:
    day = date(2024, 5, 7)
    following_day = day + timedelta(days=1)
    current = _row(day, "600001", 100)
    threshold = _row(following_day, current.code, 105)
    assert _label(current, threshold) is TomorrowCloseGain.HIT
    assert _label(current, _row(following_day, current.code, 104.9999)) is TomorrowCloseGain.MISS
    assert _label(current, None) is TomorrowCloseGain.PENDING
    assert _label(current, replace(threshold, cell_status="supplier_marked_suspended")) is TomorrowCloseGain.MISS
    assert _label(current, replace(threshold, cell_status="tencent_inferred_suspended")) is TomorrowCloseGain.PENDING
    assert threshold.point is not None
    assert _label(current, replace(threshold, point=replace(
        threshold.point, qfq_reconstruction_anchor=following_day,
    ))) is TomorrowCloseGain.PENDING
    assert _label(current, replace(threshold, point=replace(
        threshold.point, raw_preclose=101,
    ))) is TomorrowCloseGain.PENDING


def test_pooled_model_trains_selects_calibrates_and_reloads(tmp_path: Path) -> None:
    first = date(2023, 1, 2)
    dates = tuple(first + timedelta(days=index) for index in range(180))
    codes = ("300001", "300002", "300003", "300004", "600001", "600002", "600003", "600004")

    def close(day: int, stock: int) -> float:
        return 106.0 if day % 4 == (1 if stock % 2 == 0 else 3) else 100.0

    rows = (
        _row(
            day, code, close(day_index, index),
            opening=close(day_index, index) * (0.94 if close(day_index + 1, index) == 106 else 1.02),
            preclose=close(day_index - 1, index),
        )
        for day_index, day in enumerate(dates) for index, code in enumerate(codes)
    )
    samples = _collect_samples(rows, dates, len(dates) * len(codes), lambda: None)
    result = _train(samples, dates[:-1], "a" * 64, tmp_path, lambda: None)
    assert result.ready
    assert result.selection is not None
    assert result.selection.comparable_seats == 6 * 40
    assert result.assessment_brier is not None and result.assessment_brier < 0.1
    model = LoadTomorrowProbabilityModel(tmp_path)
    assert model is not None and model.identity == result.model_identity
    assert model.artifact.fitting_dates[-1] < model.artifact.calibration_dates[0]
    vectors = tuple(tuple(vector) for vector in samples.features[:8])
    probabilities = model.predict_probabilities(vectors)
    assert probabilities == pytest.approx(tuple(model.predict_probability(vector) for vector in vectors))
    for actual, probability in zip(samples.labels[:8], probabilities, strict=True):
        assert (probability > 0.5) is bool(actual)
    tree = _fit_tree(samples.features[:60], samples.labels[:60], lambda: None)
    artifact = replace(
        model.artifact, logistic=None, lightgbm=tree.model_to_string(), calibration_intercept=0.0, calibration_slope=1.0,
    )
    tree_model_root = tmp_path / "tree"
    PublishTomorrowProbabilityModel(tree_model_root, artifact)
    tree_model = LoadTomorrowProbabilityModel(tree_model_root)
    assert tree_model is not None and tree_model.booster is not None
    batch = tree_model.predict_probabilities(vectors)
    assert all(0.0 <= value <= 1.0 for value in batch)
    assert batch == pytest.approx(tuple(tree_model.predict_probability(vector) for vector in vectors))
    with pytest.raises(ValueError, match="tomorrow_probability_artifact_tree_invalid"):
        PublishTomorrowProbabilityModel(tree_model_root, replace(artifact, lightgbm="invalid tree"))
    restored = LoadTomorrowProbabilityModel(tree_model_root)
    assert restored is not None and restored.identity == tree_model.identity


def test_shared_training_respects_memory_admission_and_cancellation(tmp_path: Path) -> None:
    first = date(2023, 1, 2)
    dates = tuple(first + timedelta(days=index) for index in range(180))
    # Reserve/check must run before touching the reader or creating the artifact.
    history = SimpleNamespace(open_dates=dates, universe_count=6, snapshot_hash="a" * 64)
    budget = TrainingCpuBudget(2, TrainingResourceSettingsFixture().cpu)
    blocked = TrainSharedTomorrowProbability(
        history, tmp_path, cancelled=lambda: False, cpu_budget=budget, memory=TrainingMemoryBudget(1),
    )
    assert blocked.failure == "tomorrow_probability_memory_budget_insufficient"
    with pytest.raises(InterruptedError, match="training_cancelled"):
        TrainSharedTomorrowProbability(
            history, tmp_path, cancelled=lambda: True, cpu_budget=budget, memory=TrainingMemoryBudget(2**20),
        )
    assert LoadTomorrowProbabilityModel(tmp_path) is None

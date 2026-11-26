import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.training.training_resources import TrainingResourceSettingsFixture
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.infra.settings.loading import LoadPredictionTargets
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.training.infra.tomorrow.artifact import (
    LoadTomorrowProbabilityModel,
    PublishTomorrowProbabilityModel,
    TomorrowFeatureNames,
)
from trader.training.infra.tomorrow.settings import LoadTomorrowFitSettings
from trader.training.infra.tomorrow.training import TrainTomorrowProbability, _collect_samples, _label

_ROOT = Path(__file__).resolve().parents[4]
_TARGET = PredictionTarget(Strategy.TOMORROW, 500)
_HIGHER_TARGET = PredictionTarget(Strategy.TOMORROW, 600)

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
            qfq_open=opening,
            qfq_high=max(close, opening) + 1,
            qfq_low=min(close, opening) - 1,
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
    assert _label(current, threshold, _TARGET) is TomorrowCloseGain.HIT
    assert _label(current, threshold, _HIGHER_TARGET) is TomorrowCloseGain.MISS
    assert _label(current, _row(following_day, current.code, 104.9999), _TARGET) is TomorrowCloseGain.MISS
    assert _label(current, None, _TARGET) is TomorrowCloseGain.PENDING
    assert _label(current, replace(threshold, cell_status="supplier_marked_suspended"), _TARGET) is TomorrowCloseGain.MISS
    assert _label(current, replace(threshold, cell_status="tencent_inferred_suspended"), _TARGET) is TomorrowCloseGain.PENDING
    assert threshold.point is not None
    assert _label(current, replace(threshold, point=replace(
        threshold.point, qfq_reconstruction_anchor=following_day,
    )), _TARGET) is TomorrowCloseGain.PENDING
    assert _label(current, replace(threshold, point=replace(
        threshold.point, raw_preclose=101,
    )), _TARGET) is TomorrowCloseGain.PENDING


def test_four_independent_algorithms_fit_and_reload_with_only_two_reads(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("torch")
    first = date(2023, 1, 2)
    dates = tuple(first + timedelta(days=index) for index in range(1051))
    codes = tuple(f"{prefix}{index:03d}" for prefix in ("300", "600") for index in range(1, 9))
    reads = []

    def close(day: int, stock: int) -> float:
        return 106.0 if day % 4 == (1 if stock % 2 == 0 else 3) else 100.0

    def rows(selected):
        reads.append(selected)
        for day in selected:
            day_index = (day - first).days
            for index, code in enumerate(codes):
                current = close(day_index, index)
                yield _row(
                    day, code, current,
                    opening=current * (0.94 if close(day_index + 1, index) == 106 else 1.02),
                    preclose=close(day_index - 1, index),
                )

    history = SimpleNamespace(open_dates=dates, universe_count=len(codes), snapshot_hash="a" * 64, iter_training_rows=rows)
    targets = LoadPredictionTargets(_ROOT / "config/strategy.json")
    config = json.loads((_ROOT / "config/training.json").read_text(encoding="utf-8"))
    next(profile for profile in config["profiles"] if profile["profile"] == "v1")["calibration_dates"] = 12
    (tmp_path / "config").mkdir()
    (tmp_path / "config/training.json").write_text(json.dumps(config), encoding="utf-8")
    budget = TrainingCpuBudget(2, TrainingResourceSettingsFixture().cpu)
    progress = {profile: [] for profile in SCORING_PROFILE_IDS}
    arguments = dict(
        profiles=SCORING_PROFILE_IDS, prediction_targets=targets, project_root=tmp_path,
        cancelled=lambda: False, cpu_budget=budget, memory=TrainingMemoryBudget(120 * 1024**2),
        progress=lambda profile, event: progress[profile].append(event),
    )
    fitted = TrainTomorrowProbability(history, tmp_path, **arguments)
    assert len(reads) == 2
    assert tuple(head.profile for head in fitted) == SCORING_PROFILE_IDS
    assert tuple(head.result.algorithm for head in fitted) == ("lambdarank", "lightgbm_binary", "l2_logistic", "tcn_bce"), fitted
    assert len({head.result.model_identity for head in fitted}) == 4
    for head in fitted:
        head_root = tmp_path / head.profile / "tomorrow"
        assert {path.name for path in head_root.iterdir()} == {"model.json", "metadata.json"}
        metadata = json.loads((head_root / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["profile"] == head.profile
        assert metadata["target"] == _TARGET.identity
        assert metadata["identity"] == head.result.model_identity
        assert head.result.ready
        assert head.result.assessment_seats == 6 * 20
        assert head.result.assessment_brier is not None and 0 <= head.result.assessment_brier <= 1
        model = LoadTomorrowProbabilityModel(tmp_path, head.profile, _TARGET, project_root=tmp_path)
        assert model is not None and model.identity == head.result.model_identity
        assert model.artifact.profile == head.profile
        assert model.artifact.fitting_dates[-1] < model.artifact.calibration_dates[0]
        assert len(model.artifact.fitting_dates) == LoadTomorrowFitSettings(_ROOT, head.profile).fit_dates
        assert model.artifact.calibration_dates == dates[-21:-1]
        diagnostic = model.artifact.diagnostic
        assert diagnostic.calibration_dates[-1] < diagnostic.assessment_dates[0]
        assert diagnostic.model_identity != head.result.model_identity
        assert diagnostic.fitting_dates != model.artifact.fitting_dates
        assert model.publication.label_cutoff == dates[-2]
        if head.profile == "v1":
            assert len(diagnostic.calibration_dates) == 12
            assert len(model.artifact.early_stopping_dates) == 20
            assert model.artifact.fitting_dates[-1] < model.artifact.early_stopping_dates[0]
        zero = (0.0,) * len(TomorrowFeatureNames(head.profile))
        batch = model.predict_probabilities((zero, zero))
        assert all(0 <= value <= 1 for value in batch)
        assert batch == pytest.approx((model.predict_probability(zero),) * 2)
        events = progress[head.profile]
        assert all(event.strategy is Strategy.TOMORROW for event in events)
        assert [(event.stage, event.state) for event in events if event.state != "running"] == [
            ("training_preparation", "started"), ("training_preparation", "completed"),
            ("history_conversion", "started"), ("history_conversion", "completed"),
            ("model_fit", "started"), ("model_fit", "completed"),
            ("model_calibration", "started"), ("model_calibration", "completed"),
            ("model_evaluation", "started"), ("model_evaluation", "completed"),
            ("final_model_refit", "started"), ("final_model_refit", "completed"),
            ("artifact_publish", "started"), ("artifact_publish", "completed"),
        ]
        scan = next(event for event in events if event.stage == "history_conversion" and event.state == "completed")
        calendar = reads[1] if head.profile == "v5" else reads[0]
        assert scan.sample_progress is not None
        assert scan.sample_progress.completed_days == scan.sample_progress.total_days == len(calendar)
        assert scan.sample_progress.processed_rows == len(calendar) * len(codes)
        assert scan.sample_progress.warmup_rows == (61 * len(codes) if head.profile == "v5" else 0)
        assert any(event.state == "running" and event.completed_units > 0 for event in events)
        events.clear()

    reused = TrainTomorrowProbability(history, tmp_path, **arguments)
    assert len(reads) == 2
    for cold, hot in zip(fitted, reused, strict=True):
        assert hot.result.assessment_rows == cold.result.assessment_rows > 0
        assert hot.result.assessment_positive_rows == cold.result.assessment_positive_rows
        assert hot.result.assessment_brier == cold.result.assessment_brier
        assert hot.result.assessment_hits == cold.result.assessment_hits
        assert hot.result.assessment_seats == cold.result.assessment_seats
        assert hot.result.assessment_unsettled == cold.result.assessment_unsettled
    assert all(head.result.reused for head in reused)
    assert tuple(head.result.diagnostic for head in reused) == tuple(head.result.diagnostic for head in fitted)
    assert tuple(head.result.model_identity for head in reused) == tuple(head.result.model_identity for head in fitted)
    assert all(
        [(event.stage, event.state) for event in events] == [
            ("training_preparation", "started"),
            ("training_preparation", "completed"),
            ("model_reuse", "completed"),
        ] for events in progress.values()
    )

    assert LoadTomorrowProbabilityModel(tmp_path, "v2", _HIGHER_TARGET, project_root=_ROOT) is None
    tree_model = LoadTomorrowProbabilityModel(tmp_path, "v2", _TARGET, project_root=_ROOT)
    assert tree_model is not None
    changed = PublishTomorrowProbabilityModel(tmp_path, replace(tree_model.artifact, target=_HIGHER_TARGET))
    assert changed != tree_model.identity
    assert LoadTomorrowProbabilityModel(tmp_path, "v2", _TARGET, project_root=_ROOT) is None
    with pytest.raises(ValueError, match="tomorrow_probability_artifact_tree_invalid"):
        PublishTomorrowProbabilityModel(tmp_path, replace(tree_model.artifact, lightgbm="invalid tree"))
    preserved = LoadTomorrowProbabilityModel(tmp_path, "v2", _HIGHER_TARGET, project_root=_ROOT)
    assert preserved is not None and preserved.identity == changed
    fitting = LoadTomorrowFitSettings(_ROOT, "v2")
    monkeypatch.setattr(
        "trader.training.infra.tomorrow.artifact.LoadTomorrowFitSettings",
        lambda *_args: replace(fitting, fit_dates=fitting.fit_dates - 1),
    )
    with pytest.raises(ValueError, match="tomorrow_probability_training_contract_mismatch"):
        LoadTomorrowProbabilityModel(tmp_path, "v2", _HIGHER_TARGET, project_root=_ROOT)

    metadata_path = tmp_path / "v2" / "tomorrow" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["history_snapshot_hash"] = "b" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="tomorrow_probability_artifact_contract_mismatch"):
        LoadTomorrowProbabilityModel(tmp_path, "v2", _HIGHER_TARGET, project_root=_ROOT)


def test_sequence_ends_on_the_previous_trading_day() -> None:
    first = date(2023, 1, 2)
    dates = tuple(first + timedelta(days=index) for index in range(63))
    closes = (*((100.0,) * 60), 120.0, 160.0, 168.0)
    rows = tuple(
        _row(day, "600001", close, preclose=closes[max(0, index - 1)])
        for index, (day, close) in enumerate(zip(dates, closes, strict=True))
    )
    samples = _collect_samples(rows, dates, 1, lambda: None, _TARGET, warmup=61, source_identity="a" * 64)
    assert samples.features.shape == (1, 366)
    last_close_return = TomorrowFeatureNames("v5").index("sequence_59_close_return")
    assert samples.features[0, last_close_return] == pytest.approx(0.2)
    assert samples.labels[0] == 1


def test_training_respects_memory_admission_and_cancellation(tmp_path: Path) -> None:
    first = date(2023, 1, 2)
    dates = tuple(first + timedelta(days=index) for index in range(801))
    # Reserve/check must run before touching the reader or creating the artifact.
    history = SimpleNamespace(open_dates=dates, universe_count=6, snapshot_hash="a" * 64)
    budget = TrainingCpuBudget(2, TrainingResourceSettingsFixture().cpu)
    targets = LoadPredictionTargets(_ROOT / "config/strategy.json")
    progress = []
    blocked, = TrainTomorrowProbability(
        history, tmp_path, profiles=("v3",), prediction_targets=targets, project_root=_ROOT,
        cancelled=lambda: False, cpu_budget=budget, memory=TrainingMemoryBudget(1),
        progress=lambda _profile, event: progress.append(event),
    )
    assert blocked.result.failure == "tomorrow_probability_memory_budget_insufficient"
    assert [(event.stage, event.state) for event in progress] == [
        ("training_preparation", "started"), ("training_preparation", "failed"),
    ]
    with pytest.raises(InterruptedError, match="training_cancelled"):
        TrainTomorrowProbability(
            history, tmp_path, profiles=("v3",), prediction_targets=targets, project_root=_ROOT,
            cancelled=lambda: True, cpu_budget=budget, memory=TrainingMemoryBudget(2**20),
        )
    assert LoadTomorrowProbabilityModel(tmp_path, "v3", _TARGET, project_root=_ROOT) is None

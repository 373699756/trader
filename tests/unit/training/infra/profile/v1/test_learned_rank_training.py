"""Offline learned-rank training and pair publication contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.infra.process_lock import ProcessLock
from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.recommendation.domain.market.v1.features import (
    CalculateLearnedRankFeatures,
    LearnedRankDailyPoint,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.v1.learned_rank import (
    BuildLearnedRankDateSplit,
    LearnedRankRelevance,
    V1TrainingSettings,
)
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.profile.v1.artifacts import (
    LearnedRankPredictor,
    LoadV1ScoringProfile,
    ReadLearnedRankPair,
)
from trader.training.infra.profile.v1.samples import BuildLearnedRankSamples, LearnedRankSampleIndex
from trader.training.infra.profile.v1.training import RunV1Training


class _History:
    def __init__(self) -> None:
        self.dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(340))
        self.codes = tuple(f"600{index:03d}" for index in range(12))
        self.snapshot = HistoryTrainingInputSnapshot(
            "complete_manifest",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            self.dates[-1],
            self.dates[-2],
            BaoStockCalendar(self.dates),
            self.codes,
            "d" * 64,
        )

    def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int:
        return len(allowed_dates) * len(self.codes)

    def iter_training_rows(self, allowed_dates: frozenset[date]) -> Iterator[HistoryTrainingRow]:
        for position, day in enumerate(self.dates):
            if day not in allowed_dates:
                continue
            for index, code in enumerate(self.codes):
                point = self.point(position, index)
                yield HistoryTrainingRow(
                    code,
                    day,
                    "main",
                    f"industry-{index % 2}",
                    HistoryTrainingPoint(day, point.close_price, point.amount),
                    "complete",
                )

    def point(self, position: int, index: int) -> LearnedRankDailyPoint:
        return LearnedRankDailyPoint(
            self.dates[position],
            10 * math.exp((index - 5.5) * 0.0006 * position + 0.01 * math.sin(position / 7)),
            1e8 * (index + 1),
        )


def _settings() -> V1TrainingSettings:
    return V1TrainingSettings(
        fit_dates=30,
        early_stopping_dates=5,
        calibration_dates=5,
        validation_dates=5,
        minimum_training_rows=100,
        minimum_leaf_rows=5,
        boosting_rounds=12,
        early_stopping_rounds=3,
    )


def test_recent_split_matures_labels_and_never_overlaps_partitions() -> None:
    history = _History()
    split = BuildLearnedRankDateSplit(history.dates, _settings(), maturity_sessions=5)
    assert split.validation[-1] == history.dates[-6]
    assert split.feature_cutoff == history.dates[-7]
    for before, after in zip(
        (split.fitting, split.early_stopping, split.calibration),
        (split.early_stopping, split.calibration, split.validation),
        strict=True,
    ):
        assert history.dates.index(after[0]) - history.dates.index(before[-1]) == 6
    with pytest.raises(ValueError, match="calendar_insufficient"):
        BuildLearnedRankDateSplit(history.dates[:270], _settings(), maturity_sessions=5)
    assert LearnedRankRelevance((2, 2, 2)) == (2, 2, 2)
    assert LearnedRankRelevance((2,)) == (2,)
    assert LearnedRankRelevance((1, 1, 3, 4, 5)) == (0, 0, 2, 3, 4)


def test_training_samples_use_prior_completed_sessions_and_explicit_missing_labels(tmp_path: Path) -> None:
    from contextlib import ExitStack, closing

    history = _History()
    splits = tuple(
        BuildLearnedRankDateSplit(history.dates, _settings(), maturity_sessions=maturity) for maturity in (1, 5)
    )
    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        counts = BuildLearnedRankSamples(history, (splits[0], splits[1]), index)
        with ExitStack() as resources:
            matrix = index.matrix(splits[0].fitting, Strategy.TOMORROW, tmp_path / "matrix", resources)
            position = history.dates.index(matrix.dates[0])
            expected = CalculateLearnedRankFeatures(
                tuple(history.point(day, 0) for day in range(position - 251, position))
            )
            assert tuple(matrix.features[0]) == pytest.approx(expected)
            assert counts.missing_d25 > 0  # Tomorrow's newest anchors lack mature D25 labels.


def test_real_lightgbm_ridge_pair_trains_reloads_and_refuses_partial_or_tampered_artifacts(tmp_path: Path) -> None:
    history = _History()
    model_root = tmp_path / "models"
    sample_root = tmp_path / "samples"
    result = RunV1Training(
        lambda: history, model_root, history_root=tmp_path / "history", sample_root=sample_root, settings=_settings()
    )
    assert result.status == "engineering_ready", result.failure_reasons
    assert tuple(head.strategy for head in result.heads) == (Strategy.TOMORROW, Strategy.D25)
    assert all(head.training_rows == 360 and head.validation.predicted_rows == 60 for head in result.heads)
    assert all(head.validation.mean_daily_rank_ic > 0.5 for head in result.heads)
    assert list((sample_root / "learned_rank").iterdir()) == []
    pair = ReadLearnedRankPair(model_root)
    profile = LoadV1ScoringProfile(model_root)
    inputs = tuple(
        ModelInput(code, CalculateLearnedRankFeatures(tuple(history.point(day, index) for day in range(88, 339))))
        for index, code in enumerate(history.codes)
    )
    for head in pair.heads:
        predicted = LearnedRankPredictor(head).predict(inputs)
        assert predicted == profile.heads[head.strategy].predictor.predict(inputs)
        assert tuple(item.rank_raw for item in predicted) != (None,) * len(inputs)
        assert predicted[0].predicted_excess_return < predicted[-1].predicted_excess_return
        assert profile.heads[head.strategy].evidence.production_authority is False
        assert LearnedRankPredictor(head).predict(()) == ()
    path = model_root / "learned_rank/model.json"
    before = path.read_bytes()
    # Failed and cancelled retraining leave the complete previously published pair intact.
    failed = RunV1Training(
        lambda: history,
        model_root,
        history_root=tmp_path / "history",
        sample_root=sample_root,
        settings=replace(_settings(), minimum_training_rows=99999),
    )
    assert failed.status == "failed" and path.read_bytes() == before
    with pytest.raises(InterruptedError):
        RunV1Training(
            lambda: history,
            model_root,
            history_root=tmp_path / "history",
            sample_root=sample_root,
            settings=_settings(),
            cancel_requested=lambda: True,
        )
    assert path.read_bytes() == before
    raw = json.loads(before)
    raw["pair"]["heads"].pop()
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="both heads"):
        LoadV1ScoringProfile(model_root)
    raw = json.loads(before)
    raw["pair"]["heads"][0]["ridge_intercept"] += 0.1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        LoadV1ScoringProfile(model_root)


def test_old_v1_directory_and_publication_lock_never_become_new_v1_models(tmp_path: Path) -> None:
    from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError

    (tmp_path / "v1").mkdir()
    (tmp_path / "v1/model.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ScoringModelsUnavailableError) as missing:
        LoadScoringProfile("v1", training_root=tmp_path)
    assert missing.value.reason == "missing"
    (tmp_path / "learned_rank").mkdir()
    with ProcessLock(tmp_path / ".learned_rank.publication.lock"):
        with pytest.raises(ScoringModelsUnavailableError) as busy:
            LoadScoringProfile("v1", training_root=tmp_path)
    assert busy.value.reason == "publication_busy"

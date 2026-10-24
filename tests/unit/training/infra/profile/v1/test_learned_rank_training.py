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
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.recommendation.domain.market.v1.features import (
    CalculateLearnedRankFeatures,
    LearnedRankDailyPoint,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError
from trader.training.domain.v1.learned_rank import (
    BuildLearnedRankDateSplit,
    LearnedRankRelevance,
    V1TrainingSettings,
)
from trader.training.entrypoints.profile_training import ProjectLearnedRankTrainingResult
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.profile.v1.models import (
    LearnedRankPredictor,
    LoadV1ScoringProfile,
    PublishLearnedRankPair,
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


def test_v1_missing_t1_keeps_d25_and_fixed_calendar_partitions(tmp_path: Path) -> None:
    from contextlib import ExitStack, closing

    history = _History()
    splits = tuple(
        BuildLearnedRankDateSplit(history.dates, _settings(), maturity_sessions=maturity) for maturity in (1, 5)
    )
    anchor = splits[0].fitting[5]
    missing_day = history.dates[history.dates.index(anchor) + 1]

    class MissingHistory(_History):
        def iter_training_rows(self, allowed_dates):
            for row in super().iter_training_rows(allowed_dates):
                if row.trade_date == missing_day and row.code == self.codes[0]:
                    row = replace(row, point=None, cell_status="unknown_missing")
                yield row

    with closing(LearnedRankSampleIndex(tmp_path / "samples.sqlite3")) as index:
        counts = BuildLearnedRankSamples(MissingHistory(), (splits[0], splits[1]), index)
        with ExitStack() as resources:
            tomorrow = index.matrix((anchor, missing_day), Strategy.TOMORROW, tmp_path / "t1", resources)
            d25 = index.matrix((anchor, missing_day), Strategy.D25, tmp_path / "d25", resources)
            assert tomorrow.dates.count(anchor) == len(history.codes) - 1 and missing_day in tomorrow.dates
            assert all(
                code != history.codes[0]
                for code, day in zip(tomorrow.codes, tomorrow.dates, strict=True)
                if day == anchor
            )
            assert d25.dates.count(anchor) == len(history.codes)
            assert counts.estimated_sessions == 1 and counts.missing_t1 >= 1


def test_missing_d25_partition_fails_before_either_head_fit_and_reports_coverage(tmp_path, monkeypatch):
    original = LearnedRankSampleIndex.add_day

    def missing_second_day(index, pending):
        for candidate in pending.candidates.values():
            candidate.future[1] = None
        return original(index, pending)

    monkeypatch.setattr(LearnedRankSampleIndex, "add_day", missing_second_day)
    monkeypatch.setattr(
        "trader.training.infra.profile.v1.training.FitLearnedRankHead",
        lambda *args, **kwargs: pytest.fail("both heads must pass coverage before any expensive fit"),
    )
    result = RunV1Training(
        _History,
        tmp_path / "models",
        history_root=tmp_path / "history",
        sample_root=tmp_path / "samples",
        settings=_settings(),
    )
    assert result.status == "failed"
    assert result.failure_reasons == tuple(
        f"learned_rank_sample_partition_empty:d25:{name}"
        for name in ("fitting", "early_stopping", "calibration", "validation")
    )
    assert len(result.coverage) == 8
    assert all(item.rows > 0 for item in result.coverage if item.strategy is Strategy.TOMORROW)
    projected = ProjectLearnedRankTrainingResult(result)
    assert projected["failure_reasons"] == list(result.failure_reasons)
    assert projected["coverage"][-1] == {
        "strategy": "d25",
        "partition": "validation",
        "expected_dates": 5,
        "effective_dates": 0,
        "rows": 0,
    }
    assert not (tmp_path / "models/v1").exists()


def test_real_lightgbm_ridge_pair_trains_reloads_and_refuses_partial_or_tampered_models(tmp_path: Path) -> None:
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
    assert not sample_root.exists()
    pair = ReadLearnedRankPair(model_root)
    profile_root = model_root / "v1"
    paths = tuple(
        profile_root / strategy.value / name
        for strategy in (Strategy.TOMORROW, Strategy.D25)
        for name in ("model.json", "metadata.json")
    )
    assert {path.relative_to(profile_root).as_posix() for path in paths} == {
        "tomorrow/model.json",
        "tomorrow/metadata.json",
        "d25/model.json",
        "d25/metadata.json",
    }
    assert all(path.is_file() for path in paths)
    before = {path.relative_to(profile_root): path.read_bytes() for path in paths}
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        metadata = json.loads((profile_root / strategy.value / "metadata.json").read_text(encoding="utf-8"))
        model = json.loads((profile_root / strategy.value / "model.json").read_text(encoding="utf-8"))
        assert metadata["profile_id"] == "v1" and metadata["strategy_head"] == strategy.value
        assert metadata["model_id"] == "learned_rank"
        assert metadata["validation_report"]["production_authority"] is False
        assert metadata["pair_content_hash"] == model["pair_content_hash"] == pair.content_hash
        assert metadata["head_content_hash"] == model["head_content_hash"]
        assert metadata["model_hash"] == model["content_hash"]
        assert set(model["components"]) != set(metadata["training_input"])
    profile = LoadV1ScoringProfile(model_root)
    public_profile = LoadScoringProfile("v1", training_root=model_root)
    assert tuple(public_profile.heads) == (Strategy.TOMORROW, Strategy.D25)
    inputs = tuple(
        ModelInput(code, CalculateLearnedRankFeatures(tuple(history.point(day, index) for day in range(88, 339))))
        for index, code in enumerate(history.codes)
    )
    for head in pair.heads:
        predicted = LearnedRankPredictor(head).predict(inputs)
        assert predicted == profile.heads[head.strategy].predictor.predict(inputs)
        assert predicted == public_profile.heads[head.strategy].predictor.predict(inputs)
        assert tuple(item.rank_raw for item in predicted) != (None,) * len(inputs)
        assert predicted[0].predicted_excess_return < predicted[-1].predicted_excess_return
        assert profile.heads[head.strategy].evidence.production_authority is False
        assert LearnedRankPredictor(head).predict(()) == ()
    # Failed and cancelled retraining leave the complete previously published pair intact.
    failed = RunV1Training(
        lambda: history,
        model_root,
        history_root=tmp_path / "history",
        sample_root=sample_root,
        settings=replace(_settings(), minimum_training_rows=99999),
    )
    assert failed.status == "failed" and {path.relative_to(profile_root): path.read_bytes() for path in paths} == before
    assert not sample_root.exists()
    with pytest.raises(InterruptedError):
        RunV1Training(
            lambda: history,
            model_root,
            history_root=tmp_path / "history",
            sample_root=sample_root,
            settings=_settings(),
            cancel_requested=lambda: True,
        )
    assert {path.relative_to(profile_root): path.read_bytes() for path in paths} == before
    assert not sample_root.exists()
    missing_path = profile_root / "d25/metadata.json"
    missing_path.unlink()
    with pytest.raises(FileNotFoundError, match="head files"):
        LoadV1ScoringProfile(model_root)
    with pytest.raises(ScoringModelsUnavailableError) as missing:
        LoadScoringProfile("v1", training_root=model_root)
    assert missing.value.reason == "missing"
    missing_path.write_bytes(before[missing_path.relative_to(profile_root)])
    path = profile_root / "tomorrow/model.json"
    raw = json.loads(before[path.relative_to(profile_root)])
    raw["components"]["ridge_intercept"] += 0.1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        LoadV1ScoringProfile(model_root)
    with pytest.raises(ScoringModelsUnavailableError) as integrity:
        LoadScoringProfile("v1", training_root=model_root)
    assert integrity.value.reason == "integrity_conflict"
    path.write_bytes(before[path.relative_to(profile_root)])
    metadata_path = profile_root / "d25/metadata.json"
    metadata = json.loads(before[metadata_path.relative_to(profile_root)])
    metadata["training_input"]["training_rows"] += 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        LoadV1ScoringProfile(model_root)
    metadata_path.write_bytes(before[metadata_path.relative_to(profile_root)])
    metadata_path.write_bytes(before[profile_root.joinpath("tomorrow/metadata.json").relative_to(profile_root)])
    with pytest.raises(ValueError, match="hash differ"):
        LoadV1ScoringProfile(model_root)
    metadata_path.write_bytes(before[metadata_path.relative_to(profile_root)])
    metadata = json.loads(before[metadata_path.relative_to(profile_root)])
    metadata["model_id"] = "wrong_model"
    metadata["content_hash"] = ContentHash({key: value for key, value in metadata.items() if key != "content_hash"})
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ScoringModelsUnavailableError) as contract:
        LoadScoringProfile("v1", training_root=model_root)
    assert contract.value.reason == "contract_conflict"
    metadata_path.write_bytes(before[metadata_path.relative_to(profile_root)])
    assert ReadLearnedRankPair(model_root) == pair


def test_v1_directory_publication_failure_preserves_previous_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.training.infra.profile.v1 import models

    history = _History()
    model_root = tmp_path / "models"
    result = RunV1Training(
        lambda: history,
        model_root,
        history_root=tmp_path / "history",
        sample_root=tmp_path / "samples",
        settings=_settings(),
    )
    assert result.status == "engineering_ready", result.failure_reasons
    pair = ReadLearnedRankPair(model_root)
    PublishLearnedRankPair(model_root, pair)
    assert ReadLearnedRankPair(model_root) == pair
    original_replace = models.os.replace

    def fail_publication(source: str | Path, destination: str | Path) -> None:
        if Path(source).name.startswith(".v1.publication.") and Path(destination) == model_root / "v1":
            raise OSError("simulated directory publication failure")
        original_replace(source, destination)

    monkeypatch.setattr(models.os, "replace", fail_publication)
    with pytest.raises(OSError, match="simulated"):
        PublishLearnedRankPair(model_root, pair)
    assert ReadLearnedRankPair(model_root) == pair
    retired_root = tmp_path / "retired_models"
    retired_path = retired_root / "v1/model.json"
    retired_path.parent.mkdir(parents=True)
    retired_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="retired V1"):
        PublishLearnedRankPair(retired_root, pair)
    assert retired_path.read_text(encoding="utf-8") == "{}"
    sample_root = tmp_path / "retired_samples"
    old_sample = sample_root / "v1/samples.sqlite3"
    old_sample.parent.mkdir(parents=True)
    old_sample.write_bytes(b"old")
    blocked = RunV1Training(
        lambda: history,
        tmp_path / "other_models",
        history_root=tmp_path / "history",
        sample_root=sample_root,
        settings=_settings(),
    )
    assert blocked.status == "failed" and old_sample.read_bytes() == b"old"


def test_retraining_replaces_obsolete_contract_and_reports_actual_fit_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.training.domain.v1 import learned_rank

    history = _History()
    model_root = tmp_path / "models"
    with monkeypatch.context() as old_contract:
        old_contract.setattr(learned_rank, "LEARNED_RANK_CONTRACT", "obsolete_fixture_contract")
        old = RunV1Training(
            lambda: history,
            model_root,
            history_root=tmp_path / "history",
            sample_root=tmp_path / "samples",
            settings=_settings(),
        )
        assert old.status == "engineering_ready", old.failure_reasons
    with pytest.raises(ValueError, match="provenance"):
        ReadLearnedRankPair(model_root)

    events = []

    class Progress:
        def publish(self, event):
            events.append(event)

    result = RunV1Training(
        lambda: history,
        model_root,
        history_root=tmp_path / "history",
        sample_root=tmp_path / "samples",
        settings=_settings(),
        progress=Progress(),
    )
    assert result.status == "engineering_ready", result.failure_reasons
    assert ReadLearnedRankPair(model_root).heads == result.heads
    fitted = [event for event in events if event.stage == "model_fit" and event.state == "completed"]
    assert [event.produced_units for event in fitted] == [1, 1]
    assert [(event.stage, event.state, event.strategy) for event in events[-2:]] == [
        ("artifact_publish", "started", None),
        ("artifact_publish", "completed", None),
    ]


def test_publication_recovers_damaged_owned_files_but_preserves_unknown_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.training.infra.profile.v1 import models

    model_root = tmp_path / "models"
    result = RunV1Training(
        _History,
        model_root,
        history_root=tmp_path / "history",
        sample_root=tmp_path / "samples",
        settings=_settings(),
    )
    assert result.status == "engineering_ready", result.failure_reasons
    pair = ReadLearnedRankPair(model_root)
    metadata = model_root / "v1/d25/metadata.json"
    metadata.write_text("damaged", encoding="utf-8")
    original_replace = models.os.replace

    def fail_publication(source, destination):
        if Path(source).name.startswith(".v1.publication.") and Path(destination) == model_root / "v1":
            raise OSError("simulated publication failure")
        original_replace(source, destination)

    with monkeypatch.context() as failed_swap:
        failed_swap.setattr(models.os, "replace", fail_publication)
        with pytest.raises(OSError, match="simulated"):
            PublishLearnedRankPair(model_root, pair)
    assert metadata.read_text(encoding="utf-8") == "damaged"
    PublishLearnedRankPair(model_root, pair)
    assert ReadLearnedRankPair(model_root) == pair
    metadata.unlink()
    PublishLearnedRankPair(model_root, pair)
    assert ReadLearnedRankPair(model_root) == pair
    for path in (model_root / "v1/notes", model_root / "v1/d25/notes"):
        path.write_text("unowned", encoding="utf-8")
        with pytest.raises(ValueError, match="unexpected entries"):
            PublishLearnedRankPair(model_root, pair)
        assert path.read_text(encoding="utf-8") == "unowned"
        path.unlink()
    metadata.unlink()
    target = tmp_path / "outside.json"
    target.write_text("unowned", encoding="utf-8")
    metadata.symlink_to(target)
    with pytest.raises(ValueError, match="unexpected entries"):
        PublishLearnedRankPair(model_root, pair)
    assert metadata.is_symlink() and target.read_text(encoding="utf-8") == "unowned"
    assert not any(path.is_dir() for path in model_root.glob(".v1.publication.*"))
    assert not tuple(model_root.glob(".v1.previous.*"))


def test_old_single_file_model_and_publication_lock_never_become_new_v1_models(tmp_path: Path) -> None:
    (tmp_path / "learned_rank").mkdir()
    (tmp_path / "learned_rank/model.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ScoringModelsUnavailableError) as missing:
        LoadScoringProfile("v1", training_root=tmp_path)
    assert missing.value.reason == "missing"
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1/model.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ScoringModelsUnavailableError) as corrupt:
        LoadScoringProfile("v1", training_root=tmp_path)
    assert corrupt.value.reason == "corrupt"
    with ProcessLock(tmp_path / ".v1.publication.lock"):
        with pytest.raises(ScoringModelsUnavailableError) as busy:
            LoadScoringProfile("v1", training_root=tmp_path)
    assert busy.value.reason == "publication_busy"

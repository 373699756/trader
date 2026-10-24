"""Actual V5 CPU fitting and publication on isolated synthetic history."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from trader.download.domain.baostock_daily import BAOSTOCK_RESEARCH_IDENTITY, BaoStockCalendar
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.domain.tomorrow_training_input import REQUIRED_DAILY_FIELDS, FrozenDailyInputDescriptor
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.profile.v5.tensors import DecodeTcnTensors, TensorShapes
from trader.training.infra.profile.v5.training import RunV5Training
from trader.training.infra.training_settings import LoadProfileTrainingSettings

ROOT = Path(__file__).resolve().parents[6]


def test_missing_future_label_reports_exact_partition_without_discarding_other_head(tmp_path, monkeypatch):
    from trader.training.domain.training_dates import BuildTrainingDateSplit

    archive = _archive(tmp_path / "history")
    settings = LoadProfileTrainingSettings(ROOT, "v5")
    split = BuildTrainingDateSplit(
        archive.snapshot.calendar.open_dates,
        parent_manifest_hash="a" * 64,
        fit_dates=settings.fit_dates,
        history_sessions=62,
    )
    signal = split.model_fit_dates[100]
    absent = archive.snapshot.calendar.open_dates[archive.snapshot.calendar.open_dates.index(signal) + 2]
    original = archive.iter_training_rows

    def rows(allowed, progress=None):
        for row in original(allowed, progress):
            yield (replace(row, point=None, cell_status="unknown_missing") if row.trade_date == absent else row)

    archive.iter_training_rows = rows
    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open", lambda _p: archive)
    result = RunV5Training(
        archive.archive_root, tmp_path / "models", sample_root=tmp_path / "samples", settings=settings
    )
    assert result.status == "blocked"
    d25 = next(head for head in result.heads if head.strategy is Strategy.D25)
    coverage = next(item for item in d25.coverage if item.partition == "training")
    gap = next(item for item in coverage.missing if item.trade_date == signal)
    assert gap.sample_rows == gap.candidates == 6
    assert gap.missing_future_returns == (0, 6, 0, 0, 0)
    assert coverage.sample_rows > coverage.target_rows > 0
    assert d25.training_rows == 0
    assert not tuple((tmp_path / "models").rglob("model.json"))


def _point(day: date, position: int, stock: int) -> HistoryTrainingPoint:
    close = 20 * math.exp(position * 0.0001 + 0.04 * math.sin(position / (5 + stock)))
    return HistoryTrainingPoint(
        day, close, 1e8 + position * 2000 + stock * 100, close * 0.998, close * 1.01, close * 0.99, 1e6 + position * 30
    )


def _archive(root: Path) -> SimpleNamespace:
    root.mkdir()
    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    codes = tuple(f"60000{i}" for i in range(6))
    snapshot = HistoryTrainingInputSnapshot(
        "complete_manifest",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        dates[-1],
        dates[-2],
        BaoStockCalendar(dates),
        codes,
        "d" * 64,
    )
    descriptor = FrozenDailyInputDescriptor(
        manifest_hash=snapshot.active_snapshot_hash,
        source_identity=BAOSTOCK_RESEARCH_IDENTITY,
        source_cutoff=dates[-1],
        requested_sessions=2000,
        primary_key=("code", "trade_date"),
        fields=REQUIRED_DAILY_FIELDS,
        raw_qfq_layout="same_row",
        row_hash_algorithm="sha256",
        frozen=True,
    )

    def rows(allowed, progress=None):
        for position, day in enumerate(dates):
            if day in allowed:
                for stock, code in enumerate(codes):
                    yield HistoryTrainingRow(code, day, "main", "bank", _point(day, position, stock), "complete")

    return SimpleNamespace(
        snapshot=snapshot,
        archive_root=root,
        active_snapshot=SimpleNamespace(partitions=(object(), object())),
        describe_frozen_daily_input=lambda: descriptor,
        training_row_upper_bound=lambda allowed: len(allowed) * len(codes),
        has_industry_window=lambda *_args, **_kwargs: True,
        industry_sample_upper_bounds=lambda *_args: (("bank", 100000),),
        iter_training_rows=rows,
    )


@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_v5_actual_dual_head_fit_publish_reload_and_failed_run_preserves_pair(tmp_path, monkeypatch):
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime

    ConfigureTcnRuntime(training=True)
    archive = _archive(tmp_path / "history")
    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open", lambda _p: archive)
    settings = replace(
        LoadProfileTrainingSettings(ROOT, "v5"), fitting=TcnFittingParameters(10, epochs=2, early_stopping_rounds=2)
    )
    models = tmp_path / "models"
    result = RunV5Training(archive.archive_root, models, sample_root=tmp_path / "samples", settings=settings)
    assert result.status == "engineering_ready", result.heads
    assert {h.strategy for h in result.heads} == {Strategy.TOMORROW, Strategy.D25}
    assert sorted(p.relative_to(models).as_posix() for p in models.rglob("*.json")) == [
        "v5/d25/metadata.json",
        "v5/d25/model.json",
        "v5/tomorrow/metadata.json",
        "v5/tomorrow/model.json",
    ]
    assert not (tmp_path / "samples").exists()
    loaded = LoadScoringProfile("v5", training_root=models)
    dates = archive.snapshot.calendar.open_dates[:61]
    points = tuple(_point(d, i, 0) for i, d in enumerate(dates))
    sequence = CalculatePriceVolumeSequence(
        "600000",
        tuple(
            PriceVolumePoint(
                p.trade_date, p.qfq_open, p.qfq_high, p.qfq_low, p.qfq_close_price, p.raw_volume, p.qfq_amount
            )
            for p in points
        ),
        expected_dates=dates,
        source_identity="fixture",
    )
    for strategy, width in ((Strategy.TOMORROW, 6), (Strategy.D25, 5)):
        inputs = (ModelInput("600000", (0.1,) * width, "bank", sequence),)
        first = loaded.heads[strategy].predictor.predict(inputs)[0].predicted_excess_return
        second = LoadScoringProfile("v5", training_root=models).heads[strategy].predictor.predict(inputs)[0]
        np.testing.assert_allclose(first, second.predicted_excess_return, rtol=1e-10, atol=1e-12)
        with pytest.raises(ValueError, match="sequence"):
            loaded.heads[strategy].predictor.predict((ModelInput("600000", (0.1,) * width, "bank"),))
    original = {p: p.read_bytes() for p in models.rglob("*.json")}
    rejected = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=1000000))
    failed = RunV5Training(archive.archive_root, models, sample_root=tmp_path / "samples", settings=rejected)
    assert failed.status != "engineering_ready"
    assert all(p.read_bytes() == before for p, before in original.items())

    class _CancelledFit:
        def publish(self, progress):
            if progress.stage == "model_fit" and progress.state == "running":
                raise InterruptedError("cancelled fixture")

    with pytest.raises(InterruptedError):
        RunV5Training(
            archive.archive_root,
            models,
            sample_root=tmp_path / "samples",
            settings=replace(settings, fitting=replace(settings.fitting, seed=1)),
            progress=_CancelledFit(),
        )
    assert all(p.read_bytes() == before for p, before in original.items())


def test_v5_tensor_codec_rejects_duplicate_wrong_shape_and_nonfinite():
    weights = [
        {"name": name, "shape": list(shape), "values": [0.0] * math.prod(shape)} for name, shape in TensorShapes(6)
    ]
    assert DecodeTcnTensors(weights, 6)
    with pytest.raises(ValueError):
        DecodeTcnTensors([weights[0]] * len(weights), 6)
    weights[0]["values"][0] = float("nan")
    with pytest.raises(ValueError):
        DecodeTcnTensors(weights, 6)

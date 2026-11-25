from __future__ import annotations

from tests.helpers.training.training_resources import TrainingSampleSettingsFixture

from tests.helpers.shared.configuration import SqlitePolicyFixture

import math
from pathlib import Path
from shutil import copytree
from types import SimpleNamespace

from dataclasses import replace


from importlib.util import find_spec



import numpy as np

import pytest



from trader.recommendation.application.ports.loaded_profile import ModelInput

from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint

from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget

from trader.training.application.profile_training import TrainingRunResult
from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.infra.d25.artifact import (
    D25ProbabilityArtifact, D25ProbabilityBinding, HasD25Probability, StageD25Probability,
)
from trader.training.infra.d25.calibration import D25ProbabilityCalibrationError
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.d25.model_document import ReadD25ModelDocument
from trader.training.infra.d25.v5.probability import D25TcnModel
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining
from trader.training.infra.model_bundles.bundle_codec import TrainedTcnModelSnapshot
from trader.training.infra.model_bundles.profile_publication import (
    InspectPublishedProfileBundles, PublishStrategyModelDirectory,
)
from trader.training.infra.research.close_return.scoring import LoadDiagnosticScoringProfile
from trader.training.infra.research.close_return.v5_contracts import V5_TRAINING_PROFILE



from trader.training.infra.tcn.tensors import DecodeTcnNetwork, DecodeTcnTensors, TensorShapes

from trader.training.infra.training_settings import LoadProfileTrainingSettings, ProfileTrainingSettings

from tests.helpers.training.configuration import TcnFittingParametersFixture

from tests.helpers.training.profiles.v5.v5_training import (
    ROOT,
    _history,
    _point,
)


def _run_v5_training(
    history: SimpleNamespace,
    models: Path,
    settings: ProfileTrainingSettings,
    progress: TomorrowTrainingProgressPort | None = None,
) -> TrainingRunResult:
    return RunProfileTraining(
        ProfileTrainingRequest(
            history.history_root,
            models,
            V5_TRAINING_PROFILE,
            V5_TRAINING_PROFILE.heads,
            settings,
            progress,
            sample_root=models.parent / "samples",
            sample_settings=TrainingSampleSettingsFixture(),
            industry_snapshot=history.snapshot.industry_snapshot,
        ),
        sqlite_policy=SqlitePolicyFixture(),
    )


def test_missing_future_label_reports_exact_partition_without_discarding_other_head(tmp_path, monkeypatch):
    from trader.training.domain.production_diagnostic_dates import BuildProductionDiagnosticDatePlan

    history = _history(tmp_path / "history")
    settings = LoadProfileTrainingSettings(ROOT, "v5")
    split = BuildProductionDiagnosticDatePlan(
        history.snapshot.calendar.open_dates,
        source_cutoff=history.snapshot.source_cutoff,
        fit_sessions=settings.fit_dates,
        early_stopping_sessions=20,
        history_sessions=61,
        history_excludes_signal=True,
    )
    signal = split.fitting_dates[30]
    absent = history.snapshot.calendar.open_dates[history.snapshot.calendar.open_dates.index(signal) + 2]
    original = history.iter_training_rows

    def rows(allowed, progress=None):
        for row in original(allowed, progress):
            yield (replace(row, point=None, cell_status="unknown_missing") if row.trade_date == absent else row)

    history.iter_training_rows = rows
    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open", lambda _p, *, industry_snapshot, sqlite_policy: history)
    result = _run_v5_training(history, tmp_path / "models", settings)
    assert result.status == "blocked"
    d25 = next(head for head in result.heads if head.strategy is Strategy.D25)
    coverage = next(item for item in d25.coverage if item.partition == "training")
    gap = next(item for item in coverage.missing if item.trade_date == signal)
    assert gap.sample_rows == gap.candidates == 6
    assert gap.missing_future_returns == (0, 6, 0, 0, 0)
    assert coverage.sample_rows > coverage.target_rows > 0
    assert d25.training_rows == 0
    assert not tuple((tmp_path / "models").rglob("model.json"))


@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_v5_d25_calibration_failure_writes_utility_and_keeps_existing_calibrated_model(tmp_path, monkeypatch):
    history = _history(tmp_path / "history")
    monkeypatch.setattr(
        "trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open",
        lambda _p, *, industry_snapshot, sqlite_policy: history,
    )

    def reject_calibration(*_args, **_kwargs):
        raise D25ProbabilityCalibrationError("d25_probability_calibration_not_monotone")

    monkeypatch.setattr("trader.training.infra.engine.FitIndustryD25Probability", reject_calibration)
    train_root = tmp_path / "train"
    contract = StandardD25Profile("v5")
    target = PredictionTarget(Strategy.D25, 100)
    settings = replace(
        LoadProfileTrainingSettings(ROOT, "v5"),
        fitting=TcnFittingParametersFixture(
            minimum_industry_training_rows=10, epochs=1, early_stopping_rounds=1,
            hidden_channels=8, compute_dtype="float32",
        ),
    )
    request = ProfileTrainingRequest(
        history.history_root, train_root, contract, contract.heads, settings,
        sample_root=tmp_path / "samples", sample_settings=TrainingSampleSettingsFixture(),
        industry_snapshot=history.snapshot.industry_snapshot, d25_target=target, compute_threads=1,
    )
    result = RunProfileTraining(request, sqlite_policy=SqlitePolicyFixture())
    assert result.status == "blocked"
    assert result.heads[0].diagnostic.causes[-1].code == "d25_probability_calibration_not_monotone"
    assert result.heads[0].utility_model_published and result.heads[0].probability_calibration_failed
    assert result.heads[0].model_hash and result.heads[0].probability_model_hash is None
    assert {path.relative_to(train_root).as_posix() for path in train_root.rglob("*.json")} == {
        "v5/d25/model.json", "v5/d25/metadata.json",
    }
    profile_root = train_root / "v5"
    directory = profile_root / "d25"
    assert ReadD25ModelDocument(directory / "model.json").probability is None
    utility, = InspectPublishedProfileBundles(profile_root, contract)
    assert utility.model.training_rows > 0
    binding = D25ProbabilityBinding(target, utility.model_hash, utility.training_input_hash, utility.metadata_hash)
    features = contract.heads[0].feature_manifest.names
    assert not HasD25Probability(profile_root, "v5", features, binding)

    industry, fixture_weights = utility.model.industries[0]
    assert isinstance(fixture_weights, TrainedTcnModelSnapshot)
    plan = utility.model.diagnostic_plan
    assert plan is not None
    staged = copytree(directory, tmp_path / "staged")
    StageD25Probability(staged, D25ProbabilityArtifact(
        "v5", binding, features, plan.fitting_dates, plan.calibration_dates, (0.0, 1.0),
        ((industry, D25TcnModel(
            fixture_weights.transformer_means, fixture_weights.transformer_scales,
            fixture_weights.dropout, fixture_weights.weights, fixture_weights.architecture,
        )),),
    ))
    PublishStrategyModelDirectory(profile_root, Strategy.D25, staged)
    assert HasD25Probability(profile_root, "v5", features, binding)
    previous = {path.name: path.read_bytes() for path in directory.iterdir()}
    changed = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=11))
    repeated = RunProfileTraining(replace(request, settings=changed), sqlite_policy=SqlitePolicyFixture())
    assert repeated.status == "blocked"
    assert repeated.heads[0].diagnostic.causes[-1].code == "d25_probability_calibration_not_monotone"
    assert not repeated.heads[0].utility_model_published and repeated.heads[0].probability_calibration_failed
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == previous


@pytest.mark.skipif(find_spec("torch") is None, reason="V5 optional CPU dependency")
def test_v5_actual_dual_head_fit_publish_reload_and_failed_run_preserves_pair(tmp_path, monkeypatch):
    from trader.training.infra.tcn.network import ConfigureTcnRuntime

    ConfigureTcnRuntime(training=True)
    history = _history(tmp_path / "history")
    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open", lambda _p, *, industry_snapshot, sqlite_policy: history)
    settings = replace(
        LoadProfileTrainingSettings(ROOT, "v5"),
        fit_dates=61,
        fitting=TcnFittingParametersFixture(
            minimum_industry_training_rows=10,
            epochs=2,
            early_stopping_rounds=2,
            hidden_channels=8,
            compute_dtype="float32",
            early_stopping_relative_delta=0.001,
        ),
    )
    models = tmp_path / "models"
    updates = []

    class Progress:
        def publish(self, update):
            updates.append(update)

    result = _run_v5_training(history, models, settings, Progress())
    assert result.status == "engineering_ready", result.heads
    assert {h.strategy for h in result.heads} == {Strategy.TOMORROW, Strategy.D25}
    for head in result.heads:
        diagnostic = [
            update.fit_detail for update in updates
            if update.stage == "model_fit" and update.strategy is head.strategy and update.fit_detail
        ]
        final = [
            update.fit_detail for update in updates
            if update.stage == "final_model_refit" and update.strategy is head.strategy and update.fit_detail
        ]
        assert {item.phase for item in diagnostic} == {
            "preparation",
            "batch_cache",
            "normalization",
            "training",
            "early_stopping",
            "calibration",
            "completed",
        }
        assert {item.phase for item in final} == {"preparation", "normalization", "training", "completed"}
        for details in (diagnostic, final):
            assert all(item.industry == "capco:J66货币金融服务" and item.training_rows == head.training_rows for item in details)
            for epoch in (1, 2):
                trained = [item for item in details if item.phase == "training" and item.epoch == epoch]
                assert trained[0].completed_batches == 0
                assert trained[-1].completed_batches == trained[-1].total_batches == math.ceil(head.training_rows / 256)
                assert all(item.total_epochs == 2 for item in trained)
                assert [item.completed_batches for item in trained] == sorted(item.completed_batches for item in trained)
            assert details[-1].phase == "completed"
    assert sorted(p.relative_to(models).as_posix() for p in models.rglob("*.json")) == [
        "v5/d25/metadata.json",
        "v5/d25/model.json",
        "v5/tomorrow/metadata.json",
        "v5/tomorrow/model.json",
    ]
    assert not (tmp_path / "samples").exists()
    loaded = LoadDiagnosticScoringProfile("v5", training_root=models)
    dates = history.snapshot.calendar.open_dates[:61]
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
        inputs = (ModelInput("600000", (0.1,) * width, "capco:J66货币金融服务", sequence),)
        first = loaded.heads[strategy].predictor.predict(inputs)[0].predicted_excess_return
        second = LoadDiagnosticScoringProfile("v5", training_root=models).heads[strategy].predictor.predict(inputs)[0]
        np.testing.assert_allclose(first, second.predicted_excess_return, rtol=1e-10, atol=1e-12)
        with pytest.raises(ValueError, match="sequence"):
            loaded.heads[strategy].predictor.predict((ModelInput("600000", (0.1,) * width, "capco:J66货币金融服务"),))
    original = {p: p.read_bytes() for p in models.rglob("*.json")}
    rejected = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=1000000))
    failed = _run_v5_training(history, models, rejected)
    assert failed.status != "engineering_ready"
    assert all(p.read_bytes() == before for p, before in original.items())

    class _CancelledFit:
        def publish(self, progress):
            detail = progress.fit_detail
            if detail is not None and detail.phase == "training" and detail.completed_batches == 1:
                raise InterruptedError("cancelled fixture")

    with pytest.raises(InterruptedError):
        _run_v5_training(
            history,
            models,
            replace(settings, fitting=replace(settings.fitting, seed=1)),
            _CancelledFit(),
        )
    assert all(p.read_bytes() == before for p, before in original.items())


def test_v5_tensor_codec_rejects_duplicate_wrong_shape_and_nonfinite():
    from trader.training.infra.tcn.settings import TcnArchitecture

    architecture = TcnArchitecture(8, "float32")
    weights = [
        {"name": name, "shape": list(shape), "values": [0.0] * math.prod(shape)}
        for name, shape in TensorShapes(6, architecture)
    ]
    assert DecodeTcnTensors(weights, 6, architecture)
    with pytest.raises(ValueError):
        DecodeTcnTensors([weights[0]] * len(weights), 6, architecture)
    weights[0]["values"][0] = float("nan")
    with pytest.raises(ValueError):
        DecodeTcnTensors(weights, 6, architecture)


@pytest.mark.parametrize("suffix", ("", "_v1", "_v999"))
def test_v5_network_accepts_only_the_current_architecture_description(suffix):
    from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
    from trader.training.infra.tcn.settings import TCN_NETWORK_CONTRACT, TcnArchitecture

    architecture = TcnArchitecture(8, "float32")
    network = {
        "contract": SEQUENCE_CONTRACT,
        "architecture": TCN_NETWORK_CONTRACT + suffix,
        "hidden_channels": 8,
        "compute_dtype": "float32",
        "dropout": 0.1,
        "weights": [
            {"name": name, "shape": list(shape), "values": [0.0] * math.prod(shape)}
            for name, shape in TensorShapes(6, architecture)
        ],
    }
    if suffix:
        with pytest.raises(ValueError, match="architecture"):
            DecodeTcnNetwork(network, 6)
    else:
        assert DecodeTcnNetwork(network, 6)[2] == architecture
    network["architecture"] = "different-network" + suffix
    with pytest.raises(ValueError, match="architecture"):
        DecodeTcnNetwork(network, 6)

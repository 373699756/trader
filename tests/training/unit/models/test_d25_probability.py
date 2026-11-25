import json
import shutil
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.configuration import TcnFittingParametersFixture
from tests.helpers.training.profiles.v1.learned_rank_training import _History, _settings
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.production_diagnostic_dates import BuildProductionDiagnosticDatePlan
from trader.training.domain.v1.learned_rank import BuildLearnedRankDateSplit
from trader.training.infra.d25.labels import D25HitLabel, VerifiedD25Close
from trader.training.infra.d25.artifact import (
    D25ProbabilityBinding,
    HasD25Probability,
    LoadD25ScoringProfile,
    StageD25Probability,
)
from trader.training.infra.d25.model_document import D25ModelDocument, EncodeD25MetadataDocument, EncodeD25ModelDocument
from trader.training.infra.d25.training import (
    D25IndustryFitRequest,
    FitIndustryD25Probability,
)
from trader.training.infra.d25.v1.probability import FitV1D25Probability
from trader.training.infra.d25.tree import D25TreeModel
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.fitting_settings import LightGBMFittingParameters, RidgeFittingParameters
from trader.training.infra.d25.v1.samples import LearnedRankCandidate, LearnedRankPendingDay, LearnedRankSampleIndex
from trader.training.infra.d25.v1.models import LoadV1ScoringProfile
from trader.training.infra.d25.v1.training import RunV1Training
from trader.training.infra.history.aligned_training_rows import AlignTrainingRows
from trader.training.infra.model_bundles.profile_publication import PublishStrategyModelDirectory
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample

_TARGET = PredictionTarget(Strategy.D25, 100)
_OTHER = PredictionTarget(Strategy.D25, 150)
_BINDING = D25ProbabilityBinding(_TARGET, "a" * 64, "b" * 64, "c" * 64)
_CALENDAR = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(320))


def test_d25_exact_binary_label_requires_four_prices() -> None:
    prices = (100, 101, 101, 102)
    assert D25HitLabel(_TARGET, 100, prices) == 1
    assert D25HitLabel(_TARGET, 100, (100, 101, 101, 101.999999)) == 0
    assert D25HitLabel(_OTHER, 100, prices) == 0
    assert D25HitLabel(_TARGET, 100, (100, 101, None, 102)) is None


@pytest.mark.parametrize(("condition", "expected_status", "expected_label"), (
    ("complete", "complete", 1),
    ("suspended", "supplier_marked_suspended", 1),
    ("suspended_raw_only", "supplier_marked_suspended", None),
    ("suspended_qfq_only", "supplier_marked_suspended", None),
    ("inferred", "unknown_missing", None),
    ("unknown", "unknown_missing", None),
    ("reconstructed", "complete", None),
))
def test_d25_close_event_after_production_history_alignment(condition, expected_status, expected_label) -> None:
    dates = _CALENDAR[:6]
    industry = "capco:J66货币金融服务"
    rows = []
    for day, close in zip(dates, (100.0, 103.0, 100.0, 101.0, 101.0, 102.0), strict=True):
        point = HistoryTrainingPoint(
            day, close, 2_000.0, close, close, close, 200.0,
            raw_open=close, raw_high=close, raw_low=close, raw_close_price=close,
            raw_preclose=99.0, raw_amount=2_100.0,
        )
        status = "complete"
        if day == dates[2]:
            if condition.startswith("suspended"):
                status = "supplier_marked_suspended"
                if condition == "suspended_raw_only":
                    point = replace(point, qfq_close_price=None)
                elif condition == "suspended_qfq_only":
                    point = replace(point, raw_close_price=None)
            elif condition in {"inferred", "unknown"}:
                status = "tencent_inferred_suspended" if condition == "inferred" else "unknown_missing"
            elif condition == "reconstructed":
                point = replace(point, qfq_reconstruction_anchor=dates[4])
        rows.append(HistoryTrainingRow("600001", day, "main", industry, point, status))

    aligned = tuple(AlignTrainingRows(iter(rows), dates))
    assert aligned[0].point is rows[0].point
    assert aligned[2].cell_status == expected_status
    second_horizon = aligned[2].point
    assert second_horizon is not None
    if condition in {"suspended_raw_only", "inferred", "unknown"}:
        assert second_horizon.qfq_close_price == 103.0
        assert second_horizon.raw_close_price is None and second_horizon.raw_preclose is None
    else:
        assert second_horizon.qfq_close_price == 100.0
        assert second_horizon.raw_close_price == (None if condition == "suspended_qfq_only" else 100.0)
    if condition.startswith("suspended") or condition in {"inferred", "unknown"}:
        assert second_horizon.raw_volume == second_horizon.qfq_amount == 0.0
    if condition == "reconstructed":
        assert second_horizon.qfq_reconstruction_anchor == dates[4]

    signal = VerifiedD25Close(aligned[0])
    future = tuple(VerifiedD25Close(row) for row in aligned[2:])
    assert D25HitLabel(_TARGET, signal, future) == expected_label


def test_v1_production_fit_builds_d25_probabilities_from_raw_history_rows(tmp_path: Path) -> None:
    class VerifiedHistory(_History):
        def iter_training_rows(self, allowed_dates: frozenset[date]) -> Iterator[HistoryTrainingRow]:
            for row in super().iter_training_rows(allowed_dates):
                assert row.point is not None
                yield replace(row, point=replace(row.point, raw_close_price=row.point.qfq_close_price))

    model_root = tmp_path / "models"
    target = PredictionTarget(Strategy.D25, 50)
    result = RunV1Training(
        VerifiedHistory, model_root, history_root=tmp_path / "history", sample_root=tmp_path / "samples",
        settings=_settings(), sample_settings=TrainingSampleSettingsFixture(), sqlite_policy=SqlitePolicyFixture(),
        strategies=(Strategy.D25,), d25_target=target,
    )
    assert result.status == "engineering_ready", (result.failure_reasons, result.diagnostic)
    assert {path.relative_to(model_root).as_posix() for path in model_root.rglob("*.json")} == {
        "v1/d25/model.json", "v1/d25/metadata.json",
    }
    utility = LoadV1ScoringProfile(model_root, strategies=(Strategy.D25,))
    loaded = LoadD25ScoringProfile(model_root, "v1", target)
    assert loaded.heads[Strategy.D25].predictor.model_hash != utility.heads[Strategy.D25].predictor.model_hash


def test_v1_binary_lambdarank_uses_mature_close_labels_and_reloads(tmp_path: Path) -> None:
    settings = _settings()
    model_root = tmp_path / "models"
    assert RunV1Training(
        _History, model_root, history_root=tmp_path / "history", sample_root=tmp_path / "samples",
        settings=settings, sample_settings=TrainingSampleSettingsFixture(), sqlite_policy=SqlitePolicyFixture(),
        strategies=(Strategy.D25,),
    ).status == "engineering_ready"
    utility = LoadV1ScoringProfile(model_root, strategies=(Strategy.D25,))
    published = utility.publications[0]
    binding = D25ProbabilityBinding(_TARGET, published.model_hash, published.training_history_hash, published.metadata_hash)
    split = BuildLearnedRankDateSplit(_CALENDAR, settings, maturity_sessions=5)
    with closing(LearnedRankSampleIndex(
        tmp_path / "samples.sqlite3", sample_settings=TrainingSampleSettingsFixture(), sqlite_policy=SqlitePolicyFixture(),
    )) as samples:
        for position, day in enumerate(split.sample_dates):
            candidates = {}
            for stock in range(12):
                price = 103.0 if stock >= 6 else 99.0
                code = f"{600000 + stock:06d}"
                candidates[code] = LearnedRankCandidate(
                    code, "capco:J66货币金融服务", (float(stock),) * 10, 100.0,
                    [None, *([price / 100.0 - 1.0] * 4)], 100.0, [price] * 4,
                )
            samples.add_day(LearnedRankPendingDay(day, position, candidates), include_t1=False, d25_target=_TARGET)
        model = FitV1D25Probability(
            samples, tmp_path / "matrices", split, settings, binding,
            compute_threads=1, cancel_requested=lambda: False,
        )
    assert model.calibration[1] > 0
    assert model.fitting_dates[-1] < model.calibration_dates[0]
    profile = model_root / "v1"
    staged = tmp_path / "staged"
    shutil.copytree(profile / "d25", staged)
    identity = StageD25Probability(staged, model)
    assert StageD25Probability(staged, model) == identity
    assert not HasD25Probability(profile, "v1", model.features, model.binding)
    with pytest.raises(ValueError, match="publication_identity_conflict"):
        StageD25Probability(staged, replace(model, calibration=(0.1, model.calibration[1])))
    with pytest.raises(ValueError, match="utility_hash_mismatch"):
        StageD25Probability(staged, replace(model, binding=replace(binding, utility_hash="f" * 64)))
    PublishStrategyModelDirectory(profile, Strategy.D25, staged)
    assert HasD25Probability(profile, "v1", model.features, model.binding)
    loaded = LoadD25ScoringProfile(model_root, "v1", _TARGET)
    current = loaded.heads[Strategy.D25].predictor
    previous = utility.heads[Strategy.D25].predictor
    inputs = (ModelInput("600000", (0.0,) * 10), ModelInput("600011", (11.0,) * 10))
    old = previous.predict(inputs)
    predictions = cast(tuple[ModelPrediction, ...], current.predict(inputs))
    probabilities = tuple(item.target_probability for item in predictions)
    assert probabilities[0] is not None and 0 < probabilities[0] < 0.5
    assert probabilities[1] is not None and 0.5 < probabilities[1] < 1
    tree = model.industry_models[0][1]
    assert isinstance(tree, D25TreeModel)
    from lightgbm import Booster

    ranks = Booster(model_str=tree.tree).predict(
        np.asarray([item.alpha_features for item in inputs]), num_threads=1, raw_score=True,
    )
    assert tuple(item.rank_raw for item in predictions) == pytest.approx(ranks)
    assert tuple(item.predicted_excess_return for item in predictions) == tuple(item.predicted_excess_return for item in old)
    assert current.model_hash != previous.model_hash
    with pytest.raises(ValueError, match="artifact_contract_mismatch"):
        LoadD25ScoringProfile(model_root, "v1", _OTHER)
    (profile / "d25" / "model.json").write_text("malformed", encoding="utf-8")
    with pytest.raises(ValueError):
        LoadD25ScoringProfile(model_root, "v1", _TARGET)


@pytest.mark.parametrize("profile_id", ("v2", "v3", "v5"))
def test_d25_industry_models_fit_the_new_event_with_separate_utility_identity(
    tmp_path: Path, profile_id: ScoringProfileId,
) -> None:
    contract = StandardD25Profile(profile_id).heads[0]
    width = len(contract.feature_positions)
    split = BuildProductionDiagnosticDatePlan(
        _CALENDAR, source_cutoff=_CALENDAR[-1], fit_sessions=10, early_stopping_sessions=0, history_sessions=61,
    )
    industry = "capco:J66货币金融服务"
    labels = (0,) * 8 + (1,) * 8

    parameters = {
        "v2": LightGBMFittingParameters(10, 0.1, 3, 7, 2, 8, 3, 63, 16, 7),
        "v3": RidgeFittingParameters(10, 0.001),
        "v5": TcnFittingParametersFixture(minimum_industry_training_rows=10, epochs=10),
    }[profile_id]
    request = D25IndustryFitRequest(profile_id, contract, split, parameters, (industry,), 1, _BINDING)
    if profile_id == "v5":
        pytest.importorskip("torch")
    from trader.training.infra.tcn.network import PreserveTcnRuntime

    updates = []

    def cancelled() -> None:
        raise InterruptedError("training_cancelled")

    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        contract.feature_manifest.names,
        sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(matrix_memory_bytes=1),
    ) as samples:
        samples.add_final(
            TrainingSample(
                f"{600000 + position:06d}", day, "main", industry, 1e7,
                (float(label * 2 - 1),) * width, (None,) * 6, label,
            )
            for day in (*split.fitting_dates, *split.calibration_dates)
            for position, label in enumerate(labels)
        )
        samples.prepare_for_model_fitting(split)
        with PreserveTcnRuntime():
            artifact = FitIndustryD25Probability(samples, request, lambda: None, progress=updates.append)
        assert not tuple(tmp_path.glob("sample-fit-*"))
        with pytest.raises(InterruptedError, match="training_cancelled"):
            FitIndustryD25Probability(samples, request, cancelled)
        assert not tuple(tmp_path.glob("sample-fit-*"))
    assert (updates[-1].stage, updates[-1].state) == ("model_calibration", "completed")
    assert any(event.stage == "event_model_fit" and event.completed_units == 1 for event in updates)
    if profile_id == "v5":
        assert {event.fit_detail.epoch for event in updates if event.fit_detail and event.fit_detail.phase == "training"} == set(range(1, 11))
    profile_root = tmp_path / "models" / profile_id
    directory = profile_root / "d25"
    directory.mkdir(parents=True)
    document = EncodeD25ModelDocument(D25ModelDocument({"content_hash": artifact.binding.utility_hash}))
    (directory / "model.json").write_text(json.dumps(document), encoding="utf-8")
    (directory / "metadata.json").write_text(json.dumps(EncodeD25MetadataDocument({})), encoding="utf-8")
    assert artifact.calibration[1] > 0
    assert len(StageD25Probability(directory, artifact)) == 64
    assert HasD25Probability(profile_root, profile_id, artifact.features, artifact.binding)
    assert not HasD25Probability(profile_root, profile_id, artifact.features, replace(artifact.binding, utility_hash="f" * 64))

    assert HasD25Probability(profile_root, profile_id, artifact.features, artifact.binding)

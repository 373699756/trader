from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.configuration import TcnFittingParametersFixture
from tests.helpers.training.profiles.v1.learned_rank_training import _History, _settings
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.production_diagnostic_dates import BuildProductionDiagnosticDatePlan
from trader.training.domain.v1.learned_rank import BuildLearnedRankDateSplit
from trader.training.infra.d25.labels import D25HitLabel
from trader.training.infra.d25.artifact import (
    D25ProbabilityBinding,
    HasD25Probability,
    LoadD25ScoringProfile,
    PublishD25Probability,
    _path,
)
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
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSampleBatch

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
    identity = PublishD25Probability(profile, model)
    assert PublishD25Probability(profile, model) == identity
    assert HasD25Probability(profile, "v1", model.features, model.binding)
    with pytest.raises(ValueError, match="publication_identity_conflict"):
        PublishD25Probability(profile, replace(model, calibration=(0.1, model.calibration[1])))
    # A staged event bound to an unselected utility document cannot switch the active model.
    staged_binding = replace(binding, utility_metadata_hash="f" * 64)
    PublishD25Probability(profile, replace(model, binding=staged_binding, calibration=(0.1, model.calibration[1])))
    loaded = LoadD25ScoringProfile(model_root, utility, _TARGET)
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
    with pytest.raises(FileNotFoundError):
        LoadD25ScoringProfile(model_root, utility, _OTHER)
    _path(profile, binding).write_text('malformed', encoding='utf-8')
    with pytest.raises(ValueError):
        LoadD25ScoringProfile(model_root, utility, _TARGET)


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
    labels = np.asarray([0.0] * 8 + [1.0] * 8)
    data = np.tile((labels * 2.0 - 1.0)[:, None], (1, width))

    def batches(industry_id, received, partition, *, d25_probability):
        assert industry_id == industry and received == contract and d25_probability
        dates = split.fitting_dates if partition == "training" else split.calibration_dates
        for day in dates:
            yield TrainingSampleBatch(data.copy(), labels.copy(), (day,) * len(labels),
                                      tuple(f"{600000 + index:06d}" for index in range(len(labels))),
                                      (industry,) * len(labels))

    parameters = {
        "v2": LightGBMFittingParameters(10, 0.1, 3, 7, 2, 8, 3, 63, 16, 7),
        "v3": RidgeFittingParameters(10, 0.001),
        "v5": TcnFittingParametersFixture(minimum_industry_training_rows=10, epochs=10),
    }[profile_id]
    samples = cast(SQLiteTrainingSampleIndex, SimpleNamespace(iter_batches=batches))
    request = D25IndustryFitRequest(profile_id, contract, split, parameters, (industry,), 1, _BINDING)
    if profile_id == "v5":
        pytest.importorskip("torch")
        from trader.training.infra.tcn.network import PreserveTcnRuntime

        with PreserveTcnRuntime():
            artifact = FitIndustryD25Probability(samples, request, lambda: None)
    else:
        artifact = FitIndustryD25Probability(samples, request, lambda: None)
    profile_root = tmp_path / "models" / profile_id
    assert artifact.calibration[1] > 0
    assert len(PublishD25Probability(profile_root, artifact)) == 64
    assert HasD25Probability(profile_root, profile_id, artifact.features, artifact.binding)
    assert not HasD25Probability(profile_root, profile_id, artifact.features, replace(artifact.binding, utility_hash="f" * 64))

    def cancelled() -> None:
        raise InterruptedError("training_cancelled")

    with pytest.raises(InterruptedError, match="training_cancelled"):
        FitIndustryD25Probability(samples, request, cancelled)
    assert HasD25Probability(profile_root, profile_id, artifact.features, artifact.binding)

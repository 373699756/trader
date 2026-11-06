"""Real fixture fits must publish mature recent weights and keep diagnostics distinct."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import lightgbm as lgb
import numpy as np
import pytest

from trader.infra.serialization.canonical import ContentHash
from trader.training.infra.engine import InspectSelectedProfile, ProfileTrainingRequest, RunProfileTraining
from trader.training.infra.model_bundles.bundle_codec import DecodeHeadBundle, LoadHeadBundle
from trader.training.infra.model_bundles.bundle_codec import TrainedLightGBMModelSnapshot
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.infra.model_evaluation import CalibrationResult
from trader.training.infra.model_fitting import _refit_industry
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.sample_index import FinalFitIndustryCounts, TrainingSampleMatrix
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.training_settings import LoadProfileTrainingSettings
from tests.helpers.shared.configuration import SqlitePolicyFixture, TcnFittingParametersFixture
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from tests.helpers.training.profiles.v5.v5_training import ROOT, _history


@pytest.fixture(params=(V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE), ids=lambda item: item.profile_id)
def trained(request, tmp_path: Path, monkeypatch):
    profile = request.param
    history = _history(tmp_path / "history")
    monkeypatch.setattr("trader.training.infra.engine.SQLiteHistoryTrainingInputSnapshot.open",
                        lambda _path, *, sqlite_policy: history)
    settings = LoadProfileTrainingSettings(ROOT, profile.profile_id)
    if profile.profile_id == "v2":
        settings = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=10,
                                                    boosting_rounds=3, early_stopping_rounds=2, minimum_leaf_rows=2))
    elif profile.profile_id == "v5":
        settings = replace(settings, fitting=TcnFittingParametersFixture(
            minimum_industry_training_rows=10, epochs=2, early_stopping_rounds=2,
            hidden_channels=8, compute_dtype="float32",
        ))
    else:
        settings = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=10))
    training = ProfileTrainingRequest(
        history.history_root, tmp_path / "models", profile, profile.heads, settings,
        sample_root=tmp_path / "samples", sample_settings=TrainingSampleSettingsFixture(),
    )
    result = RunProfileTraining(training, sqlite_policy=SqlitePolicyFixture())
    assert result.status == "engineering_ready", result
    return profile, history, training, result


def test_final_weights_end_at_each_heads_latest_mature_day_and_reuse(trained):
    profile, history, training, result = trained
    for contract, head in zip(profile.heads, result.heads, strict=True):
        path = training.train_root / profile.output_directory / head.strategy.value
        model = json.loads((path / "model.json").read_text())
        metadata = json.loads((path / "metadata.json").read_text())
        evidence = metadata["final_fit"]
        report = metadata["validation_report"]
        assert head.final_fit is not None
        assert head.final_fit.fit_dates[-1] == history.snapshot.calendar.open_dates[-1 - contract.maturity_sessions]
        assert evidence["fit_dates"][-1] == head.final_fit.fit_dates[-1].isoformat()
        assert len(evidence["fit_dates"]) == training.settings.fit_dates
        assert evidence["diagnostic_model"]["content_hash"] == report["model_hash"]
        assert report["model_hash"] != model["content_hash"]
        assert evidence["final_model_unseen_validation_passed"] is False
        assert evidence["calibration_policy"] == "frozen_diagnostic_model_transfer"
        assert sum(evidence["industry_training_rows"].values()) == head.training_rows
        loaded = LoadHeadBundle(path / "model.json", head.strategy, profile)
        assert loaded.final_fit == head.final_fit
        assert loaded.training_rows == head.training_rows
    _request, _cache, requested_dates, repeated = InspectSelectedProfile(history, training, sqlite_policy=SqlitePolicyFixture())
    assert repeated is not None
    assert not requested_dates
    assert repeated.status == "engineering_ready"
    assert repeated.reused
    assert tuple(head.model_hash for head in repeated.heads) == tuple(head.model_hash for head in result.heads)


def test_refit_evidence_rejects_stale_fit_or_relabelled_validation(trained):
    profile, _history_input, training, result = trained
    head = result.heads[0]
    path = training.train_root / profile.output_directory / head.strategy.value
    model = json.loads((path / "model.json").read_text())
    original = (path / "metadata.json").read_text()
    for field, value in (("final_model_unseen_validation_passed", True),
                         ("calibration_policy", "independent_final_model_calibration"),
                         ("model_hash", "0" * 64)):
        metadata = json.loads(original)
        metadata["final_fit"][field] = value
        metadata.pop("content_hash")
        metadata["content_hash"] = ContentHash(metadata)
        with pytest.raises(ValueError, match="final fit"):
            DecodeHeadBundle(model, metadata, head.strategy, profile)


def test_final_lightgbm_saves_actual_tree_count_when_recent_features_cannot_split():
    contract = V2_TRAINING_PROFILE.heads[0]
    width = len(contract.feature_positions)
    features = np.asarray([[float(row)] * width for row in range(20)])
    booster = lgb.train(
        {"objective": "regression", "verbosity": -1, "num_threads": 1, "min_data_in_leaf": 2, "num_leaves": 3},
        lgb.Dataset(features, label=np.sin(np.arange(20))), num_boost_round=3,
    )
    assert booster.current_iteration() == 3
    original = FittedIndustryModel(
        "bank", TrainedLightGBMModelSnapshot((0.0,) * width, (1.0,) * width, 0.0, 1.0,
                                            booster.model_to_string(), 3),
        20, CalibrationResult(2, 2, 1.0, 0.0, 1.0),
    )
    matrix = TrainingSampleMatrix(np.zeros((20, width)), np.linspace(0.0, 1.0, 20))
    samples = SimpleNamespace(final_fit_data=lambda *_args: nullcontext(matrix))
    fitted = _refit_industry(
        samples, FinalFitIndustryCounts("bank", 20, 2), contract,
        FinalFitDatePlan(date(2026, 11, 5), 1, (date(2026, 11, 3), date(2026, 11, 4))),
        LoadProfileTrainingSettings(ROOT, "v2").fitting, original, lambda: None, 1,
    )
    assert fitted.model.lightgbm_best_iteration == 1
    assert lgb.Booster(model_str=fitted.model.lightgbm_model).current_iteration() == 1

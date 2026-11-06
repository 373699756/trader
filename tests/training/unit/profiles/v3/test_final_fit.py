"""Real fixture fits must publish mature recent weights and keep diagnostics distinct."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import lightgbm as lgb
import numpy as np
import pytest

from trader.infra.serialization.canonical import ContentHash
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.training.infra.engine import InspectSelectedProfile, ProfileTrainingRequest, RunProfileTraining
from trader.training.entrypoints.profile_training import ProjectProfileTrainingResult
from trader.training.infra.model_bundles.bundle_codec import DecodeHeadBundle, LoadHeadBundle
from trader.training.infra.model_bundles.bundle_codec import TrainedLightGBMModelSnapshot
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.domain.production_diagnostic_dates import (
    BuildProductionDiagnosticDatePlan,
    PRODUCTION_DIAGNOSTIC_CONTRACT,
)
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.model_evaluation import PREDICTION_EVALUATION_CONTRACT, RECENT_PREDICTION_EVALUATION_CONTRACT
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
    projected = ProjectProfileTrainingResult(profile.profile_id, result)
    for contract, head, public in zip(profile.heads, result.heads, projected["heads"], strict=True):
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
        assert metadata["schema_version"] == f"{profile.profile_id}_head_recent_refit_metadata"
        assert evidence["calibration_policy"] == "frozen_recent_diagnostic_model_transfer"
        assert evidence["diagnostic_contract"] == PRODUCTION_DIAGNOSTIC_CONTRACT
        assert head.diagnostic_plan == BuildProductionDiagnosticDatePlan(
            history.snapshot.calendar.open_dates, source_cutoff=history.snapshot.source_cutoff,
            fit_sessions=training.settings.fit_dates,
            early_stopping_sessions=20 if profile.algorithm in {"lightgbm", "tcn"} else 0,
            history_sessions=profile.history_sessions, history_excludes_signal=profile.algorithm == "tcn",
        )
        assert head.diagnostic_plan.validation_dates == history.snapshot.calendar.open_dates[-25:-5]
        assert head.diagnostic_plan.calibration_dates == history.snapshot.calendar.open_dates[-50:-30]
        assert head.diagnostic_plan.calibration_dates[-1] == date(2023, 5, 24)
        assert head.diagnostic_plan.validation_dates[-1] == date(2023, 6, 18)
        assert head.diagnostic_plan.fitting_dates[-1] == history.snapshot.calendar.open_dates[-81 if profile.algorithm != "ridge" else -56]
        assert len(head.diagnostic_plan.warmup_dates) == profile.history_sessions - int(profile.algorithm != "tcn")
        assert history.snapshot.calendar.open_dates.index(head.diagnostic_plan.fitting_dates[0]) == (
            history.snapshot.calendar.open_dates.index(head.diagnostic_plan.warmup_dates[-1]) + 1
        )
        assert report["evaluation_contract"] == RECENT_PREDICTION_EVALUATION_CONTRACT
        assert report["evaluation_coverage"]["sealed_holdout_rows"] == 0
        assert report["evaluation_coverage"]["holdout_status"] == "not_applicable_recent_production_diagnostic"
        assert sum(evidence["industry_training_rows"].values()) == head.training_rows
        loaded = LoadHeadBundle(path / "model.json", head.strategy, profile)
        assert loaded.final_fit == head.final_fit
        assert loaded.diagnostic_plan == head.diagnostic_plan
        assert loaded.training_rows == head.training_rows
        assert public["diagnostic_fit_start"] == head.diagnostic_plan.fitting_dates[0].isoformat()
        assert public["diagnostic_fit_end"] == head.diagnostic_plan.fitting_dates[-1].isoformat()
        assert public["calibration_start"] == head.diagnostic_plan.calibration_dates[0].isoformat()
        assert public["calibration_end"] == "2023-05-24"
        assert public["diagnostic_validation_start"] == head.diagnostic_plan.validation_dates[0].isoformat()
        assert public["diagnostic_validation_end"] == "2023-06-18"
        assert public["diagnostic_maturity_sessions"] == 5
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
    for changed in ("fitting", "early_stopping", "calibration", "validation", "warmup", "embargo", "calendar", "contract", "maturity", "source"):
        metadata = json.loads(original)
        evidence = metadata["final_fit"]
        if changed in {"fitting", "calibration", "validation", "warmup"}:
            evidence["diagnostic_dates"][changed] = evidence["diagnostic_dates"][changed][1:]
        elif changed == "early_stopping":
            evidence["diagnostic_dates"][changed] = [] if profile.algorithm != "ridge" else ["2020-01-01"]
        elif changed == "embargo":
            evidence["diagnostic_dates"][changed][0].pop()
        elif changed == "calendar":
            evidence["calendar_dates"].pop(0)
        elif changed == "contract":
            evidence["diagnostic_contract"] = "sealed_research_diagnostic"
        elif changed == "maturity":
            evidence["diagnostic_maturity_sessions"] = 1
        else:
            evidence["diagnostic_source_cutoff"] = evidence["fit_dates"][-1]
        metadata["content_hash"] = ContentHash({key: value for key, value in metadata.items() if key != "content_hash"})
        with pytest.raises(ValueError, match="final fit"):
            DecodeHeadBundle(model, metadata, head.strategy, profile)
    for changed in ("evaluation_contract", "holdout_status", "holdout_rows", "calibration_coefficients", "calibration_dates", "validation_dates"):
        metadata = json.loads(original)
        report = metadata["validation_report"]
        if changed == "evaluation_contract":
            report[changed] = PREDICTION_EVALUATION_CONTRACT
        elif changed == "holdout_status":
            report["evaluation_coverage"][changed] = "sealed_rows_count_only"
        elif changed == "holdout_rows":
            report["evaluation_coverage"]["sealed_holdout_rows"] = 1
        elif changed == "calibration_coefficients":
            next(iter(report["calibration_metrics"].values()))["intercept"] += 0.1
        elif changed == "calibration_dates":
            next(iter(report["calibration_metrics"].values()))["dates"] = 21
        else:
            report["prediction_metrics"]["covered_dates"] = 21
            next(iter(report["industry_prediction_metrics"].values()))["covered_dates"] = 21
        report["content_hash"] = ContentHash({key: value for key, value in report.items() if key != "content_hash"})
        metadata["content_hash"] = ContentHash({key: value for key, value in metadata.items() if key != "content_hash"})
        reason = (
            "recent calibration report" if changed == "calibration_coefficients"
            else "coverage exceeds" if changed in {"calibration_dates", "validation_dates"}
            else "evaluation contract"
        )
        with pytest.raises(ValueError, match=reason):
            DecodeHeadBundle(model, metadata, head.strategy, profile)
    for changed in ("requested_sessions", "calendar_tail"):
        metadata = json.loads(original)
        source = metadata["training_input"]
        if changed == "requested_sessions":
            source["requested_sessions"] -= 1
        else:
            calendar = tuple(date.fromisoformat(day) for day in metadata["final_fit"]["calendar_dates"])
            calendar = (*calendar[:-1], calendar[-1] + timedelta(days=1))
            metadata["final_fit"]["calendar_dates"] = [day.isoformat() for day in calendar]
            source["calendar_hash"] = BaoStockCalendar(calendar).content_hash
        source["content_hash"] = ContentHash({key: value for key, value in source.items() if key != "content_hash"})
        report = metadata["validation_report"]
        report["training_input_document_hash"] = source["content_hash"]
        report["content_hash"] = ContentHash({key: value for key, value in report.items() if key != "content_hash"})
        metadata["content_hash"] = ContentHash({key: value for key, value in metadata.items() if key != "content_hash"})
        with pytest.raises(ValueError, match="recent source calendar extent"):
            DecodeHeadBundle(model, metadata, head.strategy, profile)


def test_legacy_research_refit_schema_keeps_its_identity_and_cannot_reuse_as_recent(trained):
    profile, history, training, result = trained
    research = BuildTrainingDateSplit(
        history.snapshot.calendar.open_dates, parent_manifest_hash=history.snapshot.snapshot_hash,
        fit_dates=training.settings.fit_dates, recent_fit=profile.profile_id == "v5",
        early_stopping_sessions=20 if profile.algorithm in {"lightgbm", "tcn"} else 0,
        history_sessions=profile.history_sessions, history_excludes_signal=profile.algorithm == "tcn",
    )
    # Serialization fixture only: no production writer may create the retired schema.
    for contract, head in zip(profile.heads, result.heads, strict=True):
        path = training.train_root / profile.output_directory / head.strategy.value
        model = json.loads((path / "model.json").read_text())
        metadata = json.loads((path / "metadata.json").read_text())
        metadata["schema_version"] = f"{profile.profile_id}_head_refit_metadata"
        evidence = metadata["final_fit"]
        for key in ("diagnostic_contract", "diagnostic_source_cutoff", "diagnostic_maturity_sessions"):
            evidence.pop(key)
        evidence["diagnostic_split_hash"] = research.content_hash
        evidence["diagnostic_dates"] = {
            "fitting": [day.isoformat() for day in research.model_fit_dates],
            "early_stopping": [day.isoformat() for day in research.early_stopping_dates],
            "calibration": [day.isoformat() for day in research.calibration_dates],
            "validation": [day.isoformat() for day in research.confirmation_dates],
            "count_only_holdout": [day.isoformat() for day in research.daily_proxy_holdout_dates],
        }
        evidence["calibration_policy"] = "frozen_diagnostic_model_transfer"
        evidence["research_holdout_status"] = "may_be_used_by_final_refit_not_unseen_for_final_model"
        split_hash = ContentHash({"calendar_split_hash": research.content_hash, "strategy_head": head.strategy.value,
                                  "maturity_sessions": contract.maturity_sessions, "label_target": contract.label_target})
        source = metadata["training_input"]
        source["split_hash"] = split_hash
        source["content_hash"] = ContentHash({key: value for key, value in source.items() if key != "content_hash"})
        report = metadata["validation_report"]
        report["split_hash"] = split_hash
        report["training_input_document_hash"] = source["content_hash"]
        report["evaluation_contract"] = PREDICTION_EVALUATION_CONTRACT
        report["evaluation_coverage"]["holdout_status"] = "sealed_rows_count_only"
        report["content_hash"] = ContentHash({key: value for key, value in report.items() if key != "content_hash"})
        metadata["content_hash"] = ContentHash({key: value for key, value in metadata.items() if key != "content_hash"})
        loaded = DecodeHeadBundle(model, metadata, head.strategy, profile)
        assert loaded.diagnostic_plan is None and loaded.final_fit == head.final_fit
        (path / "metadata.json").write_text(json.dumps(metadata))
    before = {path: path.read_bytes() for path in training.train_root.rglob("*.json")}
    _request, _cache, requested_dates, repeated = InspectSelectedProfile(history, training, sqlite_policy=SqlitePolicyFixture())
    assert repeated is None and requested_dates
    assert all(path.read_bytes() == value for path, value in before.items())


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

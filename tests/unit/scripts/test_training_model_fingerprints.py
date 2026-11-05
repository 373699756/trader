"""Real LightGBM semantics remain exact while fitting-only thread metadata is ignored."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pytest
from threadpoolctl import ThreadpoolController

from scripts.training.jobs import training_alignment as alignment


@dataclass(frozen=True)
class _FittedModels:
    profile: str
    two_threads: str
    single_thread: str
    changed_leaf: str
    fewer_trees: str


def _fit_models(profile):
    labels = np.tile(np.arange(4), 24)
    features = np.random.default_rng(65).normal(size=(96, 3))
    features[:, 0] += labels
    features[:, 2] = np.tile(np.arange(3), 32)
    regression = features[:, 0] * 0.3 - features[:, 1] * 0.7 + (features[:, 2] == 1) * 0.2
    dataset = lgb.Dataset(
        features,
        label=labels if profile == "v1" else regression,
        group=[16] * 6 if profile == "v1" else None,
        feature_name=["price", "volume", "industry"],
        categorical_feature=["industry"],
    )
    booster = lgb.train(
        {
            "objective": "lambdarank" if profile == "v1" else "regression",
            "num_threads": 2,
            "num_leaves": 5,
            "min_data_in_leaf": 2,
            "learning_rate": 0.2,
            "deterministic": True,
            "force_col_wise": True,
            "verbosity": -1,
            "seed": 65,
        },
        dataset,
        num_boost_round=4,
        keep_training_booster=True,
    )
    original = booster.model_to_string(num_iteration=-1)
    fewer_trees = booster.model_to_string(num_iteration=3)
    predictions = booster.predict(features, num_threads=1)
    booster.reset_parameter({"num_threads": 1})
    single_thread = booster.model_to_string(num_iteration=-1)
    assert "[num_threads: 2]" in original and "[num_threads: 1]" in single_thread
    assert original != single_thread
    np.testing.assert_array_equal(booster.predict(features, num_threads=1), predictions)
    changed = lgb.Booster(model_str=original)
    leaf = changed.get_leaf_output(0, 0)
    changed.set_leaf_output(0, 0, float(np.nextafter(leaf, np.inf)))
    changed_leaf = changed.model_to_string(num_iteration=-1)
    assert changed.dump_model(num_iteration=-1) != booster.dump_model(num_iteration=-1)
    return _FittedModels(profile, original, single_thread, changed_leaf, fewer_trees)


@pytest.fixture(scope="module")
def models_by_profile():
    with ThreadpoolController().limit(limits=1):
        yield {profile: _fit_models(profile) for profile in ("v1", "v2")}


@pytest.fixture(scope="module", params=("v1", "v2"))
def fitted_models(request, models_by_profile):
    return models_by_profile[request.param]


def _documents(profile, model_text):
    common = {
        "transformer_means": [0.0, 0.1, 0.2],
        "transformer_scales": [1.0, 2.0, 3.0],
        "calibration_intercept": 0.01,
        "calibration_slope": 1.1,
    }
    metrics = {
        "source_rows": 96,
        "predicted_rows": 24,
        "covered_codes": 6,
        "covered_dates": 4,
        "covered_industries": 1,
        "mean_absolute_error": 0.01,
        "root_mean_squared_error": 0.02,
        "mean_daily_rank_ic": 0.3,
        "mean_daily_ndcg_at_6": 0.7,
    }
    if profile == "v1":
        return {"components": {
            **common,
            "ridge_intercept": 0.02,
            "ridge_coefficients": [0.1, -0.2, 0.3],
            "rank_model": model_text,
            "rank_iterations": 4,
        }}, {"validation_report": {"validation": metrics}}
    report = {
        "prediction_metrics": metrics,
        "industry_prediction_metrics": {"main": metrics},
        "calibration_metrics": {"main": {"intercept": 0.01, "slope": 1.1}},
        "rejected_industries": [],
        "evaluation_coverage": {"candidate_training_rows": 96, "predicted_rows": 24},
        "target_metrics": {"net_utility": {"count": 96, "mean": 0.01, "standard_deviation": 0.03}},
        "training_rows": 96,
        "validation_rows": 24,
    }
    if profile == "v2":
        component = {**common, "lightgbm_model": model_text, "lightgbm_best_iteration": 4}
    elif profile == "v3":
        component = {**common, "ridge_intercept": 0.02, "ridge_coefficients": [0.1, -0.2, 0.3]}
    else:
        component = {**common, "tcn_network": {
            "contract": "fixture-sequence",
            "architecture": "fixture-tcn",
            "hidden_channels": 1,
            "compute_dtype": "float32",
            "dropout": 0.1,
            "weights": [{"name": "output.weight", "shape": [1, 1], "values": [0.2]}],
        }}
    return {"industries": {"main": component, "other": dict(component)}}, {"validation_report": report}


def _write_documents(root, profile, model, metadata, *, strategies=("tomorrow", "d25")):
    for strategy in strategies:
        directory = root / profile / strategy
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "model.json").write_text(json.dumps(model, allow_nan=False), encoding="utf-8")
        (directory / "metadata.json").write_text(json.dumps(metadata, allow_nan=False), encoding="utf-8")


def _component(profile, model):
    return model["components"] if profile == "v1" else model["industries"]["main"]


def test_thread_metadata_keeps_real_rank_and_regression_fingerprints_equal_without_source_writes(tmp_path, fitted_models):
    profile = fitted_models.profile
    model, metadata = _documents(profile, fitted_models.two_threads)
    _write_documents(tmp_path, profile, model, metadata)
    source = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    original = alignment._numeric_fingerprints(tmp_path, (profile,))
    assert {path: path.read_bytes() for path in source} == source
    model, metadata = _documents(profile, fitted_models.single_thread)
    _write_documents(tmp_path, profile, model, metadata)
    source = {path: path.read_bytes() for path in source}
    assert alignment._numeric_fingerprints(tmp_path, (profile,)) == original
    assert {path: path.read_bytes() for path in source} == source


def test_semantic_projection_preserves_every_sdk_dump_field_and_all_iterations(fitted_models):
    booster = lgb.Booster(model_str=fitted_models.two_threads)
    expected = booster.dump_model(num_iteration=-1)
    projected = alignment._lightgbm_fingerprint_model(fitted_models.two_threads)
    assert projected == expected
    assert len(projected["tree_info"]) == 4
    assert projected["feature_names"] == ["price", "volume", "industry"]
    assert projected["feature_infos"]["industry"]["values"]
    assert projected["objective"].startswith("lambdarank" if fitted_models.profile == "v1" else "regression")


@pytest.mark.parametrize("mutation", (
    "leaf",
    "trees",
    "iteration",
    "transformer_means",
    "transformer_scales",
    "calibration_intercept",
    "calibration_slope",
    "metric",
))
def test_every_real_tree_and_other_numeric_change_remains_exact(tmp_path, fitted_models, mutation):
    profile = fitted_models.profile
    model, metadata = _documents(profile, fitted_models.two_threads)
    _write_documents(tmp_path, profile, model, metadata)
    original = alignment._numeric_fingerprints(tmp_path, (profile,))
    component = _component(profile, model)
    if mutation in {"leaf", "trees"}:
        component["rank_model" if profile == "v1" else "lightgbm_model"] = (
            fitted_models.changed_leaf if mutation == "leaf" else fitted_models.fewer_trees
        )
    elif mutation == "iteration":
        component["rank_iterations" if profile == "v1" else "lightgbm_best_iteration"] = 3
    elif mutation in {"transformer_means", "transformer_scales"}:
        component[mutation][0] = float(np.nextafter(component[mutation][0], np.inf))
    elif mutation == "metric":
        report = metadata["validation_report"]
        metrics = report["validation"] if profile == "v1" else report["prediction_metrics"]
        metrics["mean_absolute_error"] = float(np.nextafter(metrics["mean_absolute_error"], np.inf))
    else:
        component[mutation] = float(np.nextafter(component[mutation], np.inf))
    _write_documents(tmp_path, profile, model, metadata, strategies=("tomorrow",))
    changed = alignment._numeric_fingerprints(tmp_path, (profile,))
    assert changed[0] != original[0]
    assert changed[1] == original[1]


@pytest.mark.parametrize("field", ("ridge_intercept", "ridge_coefficients"))
def test_v1_ridge_component_is_not_lost_by_rank_projection(tmp_path, models_by_profile, field):
    model, metadata = _documents("v1", models_by_profile["v1"].two_threads)
    _write_documents(tmp_path, "v1", model, metadata)
    original = alignment._numeric_fingerprints(tmp_path, ("v1",))
    if field == "ridge_coefficients":
        model["components"][field][1] = float(np.nextafter(model["components"][field][1], np.inf))
    else:
        model["components"][field] = float(np.nextafter(model["components"][field], np.inf))
    _write_documents(tmp_path, "v1", model, metadata)
    assert alignment._numeric_fingerprints(tmp_path, ("v1",)) != original


@pytest.mark.parametrize("mutation", ("thread_metadata", "leaf", "calibration"))
def test_v2_projects_every_industry_independently(tmp_path, models_by_profile, mutation):
    models = models_by_profile["v2"]
    model, metadata = _documents("v2", models.two_threads)
    _write_documents(tmp_path, "v2", model, metadata)
    original = alignment._numeric_fingerprints(tmp_path, ("v2",))
    other = model["industries"]["other"]
    if mutation == "calibration":
        other["calibration_slope"] = float(np.nextafter(other["calibration_slope"], np.inf))
    else:
        other["lightgbm_model"] = models.single_thread if mutation == "thread_metadata" else models.changed_leaf
    _write_documents(tmp_path, "v2", model, metadata)
    assert (alignment._numeric_fingerprints(tmp_path, ("v2",)) == original) is (mutation == "thread_metadata")


@pytest.mark.parametrize("invalid", (None, 17, "", "not a LightGBM model"))
def test_invalid_lightgbm_is_an_explicit_error_without_raw_text_fallback(tmp_path, fitted_models, invalid):
    profile = fitted_models.profile
    model, metadata = _documents(profile, fitted_models.two_threads)
    component = model["components"] if profile == "v1" else model["industries"]["other"]
    component["rank_model" if profile == "v1" else "lightgbm_model"] = invalid
    _write_documents(tmp_path, profile, model, metadata)
    with pytest.raises(ValueError, match="training_alignment_lightgbm_model_invalid"):
        alignment._numeric_fingerprints(tmp_path, (profile,))


@pytest.mark.parametrize("profile", ("v3", "v5"))
def test_ridge_and_tcn_keep_their_entire_existing_model_and_metric_values(tmp_path, profile):
    model, metadata = _documents(profile, None)
    _write_documents(tmp_path, profile, model, metadata)
    report = metadata["validation_report"]
    numeric = json.dumps((model["industries"], report), ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
    expected = hashlib.sha256(numeric).hexdigest()
    original = alignment._numeric_fingerprints(tmp_path, (profile,))
    assert all(item["model_values_and_metrics_sha256"] == expected for item in original)
    component = model["industries"]["other"]
    values = component["ridge_coefficients"] if profile == "v3" else component["tcn_network"]["weights"][0]["values"]
    values[0] = float(np.nextafter(values[0], np.inf))
    _write_documents(tmp_path, profile, model, metadata)
    assert alignment._numeric_fingerprints(tmp_path, (profile,)) != original

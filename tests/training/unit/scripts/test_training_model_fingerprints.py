from __future__ import annotations

import hashlib

import json


import numpy as np

import pytest


from scripts.training.jobs import training_alignment as alignment

from tests.helpers.training.scripts.training_model_fingerprints import (
    _component,
    _documents,
    _write_documents,
    fitted_models as fitted_models,
    models_by_profile as models_by_profile,
)


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
    import lightgbm as lgb

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

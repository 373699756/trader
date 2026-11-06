from __future__ import annotations

import hashlib
from copy import deepcopy

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


def _final_documents(profile, model_text):
    model, metadata = _documents(profile, model_text)
    diagnostic = deepcopy(model)
    if profile == "v1":
        model["schema_version"] = "learned_rank_final_head_model"
        metadata["schema_version"] = "learned_rank_final_head_metadata"
        metadata["training_input"] = {"final_fit": {"fit_dates": ["2026-11-04"]}}
        metadata["validation_report"]["diagnostic_head"] = {
            **diagnostic["components"], "split": {"label_cutoff": "2026-01-06"},
        }
    else:
        metadata["schema_version"] = f"{profile}_head_refit_metadata"
        metadata["final_fit"] = {
            "diagnostic_model": diagnostic, "fit_dates": ["2026-11-04"],
            "calibration_policy": "frozen_diagnostic_model_transfer",
        }
    return model, metadata


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
@pytest.mark.parametrize("recent_diagnostic", (False, True))
def test_final_diagnostic_weights_change_fingerprint_and_reject_benchmark_with_final_and_metrics_unchanged(
    tmp_path, models_by_profile, profile, recent_diagnostic,
):
    from scripts.training.jobs.training_performance import SummarizeTrainingBenchmark
    from tests.helpers.training.scripts.training_execution_performance import _results, _summary

    text = models_by_profile[profile].two_threads if profile in {"v1", "v2"} else None
    model, metadata = _final_documents(profile, text)
    if recent_diagnostic and profile != "v1":
        metadata["schema_version"] = f"{profile}_head_recent_refit_metadata"
    final_before = deepcopy(model)
    report_before = deepcopy(metadata["validation_report"])
    _write_documents(tmp_path, profile, model, metadata)
    original = alignment._numeric_fingerprints(tmp_path, (profile,))
    diagnostic = (
        metadata["validation_report"]["diagnostic_head"] if profile == "v1"
        else metadata["final_fit"]["diagnostic_model"]["industries"]["main"]
    )
    if profile in {"v1", "v2"}:
        diagnostic["rank_model" if profile == "v1" else "lightgbm_model"] = models_by_profile[profile].changed_leaf
    else:
        weights = diagnostic["ridge_coefficients"] if profile == "v3" else diagnostic["tcn_network"]["weights"][0]["values"]
        weights[0] = float(np.nextafter(weights[0], np.inf))
    assert model == final_before
    if profile == "v1":
        assert metadata["validation_report"]["validation"] == report_before["validation"]
    else:
        assert metadata["validation_report"] == report_before
    _write_documents(tmp_path, profile, model, metadata, strategies=("tomorrow",))
    changed = alignment._numeric_fingerprints(tmp_path, (profile,))
    assert changed[0] != original[0] and changed[1] == original[1]

    results = _results()
    for result in results:
        summary = _summary(result)
        runs = [summary["initial"], summary["repeat"], summary["sample_reuse"]["run"]]
        for run in runs:
            if not any(item["profile"] == profile for item in run["numeric_fingerprints"]):
                continue
            run["numeric_fingerprints"] = [
                *[item for item in run["numeric_fingerprints"] if item["profile"] != profile],
                *original,
            ]
    assert SummarizeTrainingBenchmark(results)["comparable"] is True
    for result in results:
        if ":candidate:" not in result.name:
            continue
        summary = _summary(result)
        for run in (summary["initial"], summary["repeat"], summary["sample_reuse"]["run"]):
            if not any(item["profile"] == profile for item in run["numeric_fingerprints"]):
                continue
            run["numeric_fingerprints"] = [
                *[item for item in run["numeric_fingerprints"] if item["profile"] != profile],
                *changed,
            ]
    comparison = SummarizeTrainingBenchmark(results)
    assert comparison["comparable"] is False
    assert comparison["timing_acceptance"] is False


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_final_numeric_projection_keeps_thread_metadata_and_date_policy_identity_separate(
    tmp_path, models_by_profile, profile,
):
    text = models_by_profile[profile].two_threads if profile in {"v1", "v2"} else None
    model, metadata = _final_documents(profile, text)
    _write_documents(tmp_path, profile, model, metadata)
    original = alignment._numeric_fingerprints(tmp_path, (profile,))
    if profile == "v1":
        diagnostic = metadata["validation_report"]["diagnostic_head"]
        diagnostic["rank_model"] = models_by_profile[profile].single_thread
        diagnostic["split"] = {"label_cutoff": "2026-01-05"}
        metadata["training_input"]["final_fit"]["fit_dates"] = ["2026-11-03"]
        metadata["validation_report"]["evaluation_scope"] = "identity_checked_by_model_codec"
    else:
        evidence = metadata["final_fit"]
        diagnostic = evidence["diagnostic_model"]
        evidence["fit_dates"] = ["2026-11-03"]
        evidence["calibration_policy"] = "identity_checked_by_model_codec"
        evidence["plan_hash"] = "changed-identity"
        if profile == "v2":
            diagnostic["industries"]["main"]["lightgbm_model"] = models_by_profile[profile].single_thread
    diagnostic["content_hash"] = "changed-identity"
    _write_documents(tmp_path, profile, model, metadata)
    assert alignment._numeric_fingerprints(tmp_path, (profile,)) == original


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
@pytest.mark.parametrize("diagnostic", (None, {}))
def test_final_schema_rejects_missing_diagnostic_payload_without_using_final_model(
    tmp_path, models_by_profile, profile, diagnostic,
):
    text = models_by_profile[profile].two_threads if profile in {"v1", "v2"} else None
    model, metadata = _final_documents(profile, text)
    if profile == "v1":
        metadata["validation_report"]["diagnostic_head"] = diagnostic
    else:
        metadata["final_fit"]["diagnostic_model"] = diagnostic
    _write_documents(tmp_path, profile, model, metadata)
    with pytest.raises(ValueError, match="training_alignment_.*model_document_invalid"):
        alignment._numeric_fingerprints(tmp_path, (profile,))


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_historical_schema_keeps_its_original_numeric_fingerprint(tmp_path, models_by_profile, profile):
    text = models_by_profile[profile].two_threads if profile in {"v1", "v2"} else None
    model, metadata = _documents(profile, text)
    _write_documents(tmp_path, profile, model, metadata)
    original = alignment._numeric_fingerprints(tmp_path, (profile,))
    metadata["schema_version"] = "learned_rank_head_metadata" if profile == "v1" else f"{profile}_head_metadata"
    _write_documents(tmp_path, profile, model, metadata)
    assert alignment._numeric_fingerprints(tmp_path, (profile,)) == original

from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path
import numpy as np
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.model_bundles.profile_publication import PublishProfileBundles
from trader.training.infra.model_evaluation import PREDICTION_EVALUATION_CONTRACT
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


@lru_cache(maxsize=2)
def _lightgbm_model(width: int) -> str:
    import lightgbm as lgb

    features = np.asarray(
        tuple(tuple(float((row + column) % 3) for column in range(width)) for row in range(12)),
        dtype=np.float64,
    )
    target = np.asarray(tuple(float(row % 2) for row in range(12)), dtype=np.float64)
    booster = lgb.train(
        {"objective": "regression", "verbosity": -1, "num_threads": 1, "min_data_in_leaf": 2, "num_leaves": 3},
        lgb.Dataset(features, label=target),
        num_boost_round=1,
    )
    return booster.model_to_string(num_iteration=1)


def _hash(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = ContentHash(document)
    return document


def _prediction_report() -> dict[str, object]:
    metric = {
        "input_rows": 400,
        "predicted_rows": 400,
        "covered_codes": 2,
        "covered_dates": 200,
        "covered_industries": 1,
        "mean_absolute_error": 0.01,
        "root_mean_squared_error": 0.02,
        "mean_daily_rank_ic": 0.0,
        "rank_ic_dates": 200,
    }
    return {
        "evaluation_contract": PREDICTION_EVALUATION_CONTRACT,
        "prediction_evaluation_mode": "prediction_diagnostic_only",
        "prediction_metrics": dict(metric),
        "industry_prediction_metrics": {"银行": dict(metric)},
        "calibration_metrics": {
            "银行": {"rows": 40, "dates": 20, "prediction_variance": 0.0001, "intercept": 0.0, "slope": 1.0}
        },
        "rejected_industries": [],
        "evaluation_coverage": {
            "candidate_training_rows": 20000,
            "trained_rows": 20000,
            "candidate_validation_rows": 400,
            "predicted_rows": 400,
            "sealed_holdout_rows": 400,
            "holdout_status": "sealed_rows_count_only",
        },
    }


def _documents(
    contract: TrainedHeadContract,
    profile: TrainedProfileContract = V3_TRAINING_PROFILE,
) -> tuple[dict[str, object], dict[str, object]]:
    codes = ["600000", "600001"]
    input_document = _hash(
        {
            "schema_version": f"{profile.profile_id}_head_training_input_manifest",
            "profile_id": profile.profile_id,
            "strategy_head": contract.strategy.value,
            "training_input_scope": "complete_manifest",
            "training_input_hash": "a" * 64,
            "training_run_hash": "9" * 64,
            "label_cutoff": "2026-09-08",
            "source_identity_hash": "1" * 64,
            "calendar_hash": "4" * 64,
            "source_cutoff": "2026-09-09",
            "requested_sessions": 2000,
            "input_descriptor_hash": "5" * 64,
            "training_input_codes": len(codes),
            "training_universe_codes": len(codes),
            "codes": codes,
            "feature_manifest_hash": contract.feature_manifest.content_hash,
            "split_hash": "b" * 64,
            "training_contract_hash": "e" * 64,
            "label_target": contract.label_target,
            "training_cost_bps": 0,
            "validation_scope": "daily_close_engineering_proxy",
            "production_authority": False,
        }
    )
    width = len(contract.feature_manifest.names)
    parameters: dict[str, object] = {
        "transformer_means": [0.0] * width,
        "transformer_scales": [1.0] * width,
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
    }
    if profile.algorithm == "lightgbm":
        parameters.update({"lightgbm_model": _lightgbm_model(width), "lightgbm_best_iteration": 1})
        dependencies = {"lightgbm": "4.7.0", "numpy": "2.0.0"}
    else:
        parameters.update({"ridge_intercept": 0.0, "ridge_coefficients": [0.1] * width})
        dependencies = {"numpy": "2.0.0"}
    model = _hash({"industries": {"银行": parameters}})
    targets = (
        ("target_t2", "target_t3", "target_t4", "target_t5", "target_d25_aggregate")
        if contract.strategy is Strategy.D25
        else ("target_t1",)
    )
    report = _hash(
        {
            "schema_version": f"{profile.profile_id}_head_validation_report",
            "profile_id": profile.profile_id,
            "model_id": contract.model_id,
            "strategy_head": contract.strategy.value,
            "training_input_scope": "complete_manifest",
            "training_input_hash": "a" * 64,
            "training_run_hash": "9" * 64,
            "label_cutoff": "2026-09-08",
            "source_identity_hash": "1" * 64,
            "training_input_document_hash": input_document["content_hash"],
            "training_input_codes": len(codes),
            "training_universe_codes": len(codes),
            "feature_manifest_hash": contract.feature_manifest.content_hash,
            "split_hash": "b" * 64,
            "training_contract_hash": "e" * 64,
            "model_hash": model["content_hash"],
            "label_target": contract.label_target,
            "training_cost_bps": 0,
            "training_anchor": "15:00_close_proxy",
            "runtime_anchor": contract.runtime_anchor,
            "point_in_time_parity": False,
            "validation_scope": "daily_close_engineering_proxy",
            "historical_status": "historical_data_insufficient",
            "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
            "industry_count": 1,
            "training_rows": 20_000,
            "validation_rows": 400,
            "target_metrics": {target: {"count": 400, "mean": 0.01, "standard_deviation": 0.02} for target in targets},
            "artifact_structure_valid": True,
            "artifact_structure_failure_reasons": [],
            "prediction_return_validation_passed": False,
            "prediction_return_failure_reasons": ["daily_close_proxy_not_point_in_time"],
            "automatic_model_update": False,
            "production_authority": False,
            **_prediction_report(),
        }
    )
    metadata = _hash(
        {
            "schema_version": f"{profile.profile_id}_head_metadata",
            "profile_id": profile.profile_id,
            "model_id": contract.model_id,
            "strategy_head": contract.strategy.value,
            "feature_ids": list(contract.feature_manifest.names),
            "feature_units": list(contract.feature_manifest.units),
            "exposure_contract": {
                "market": True,
                "board": True,
                "industry": True,
                "log_average_amount_20d": True,
                "order": ["market", "board", "industry", "log_average_amount_20d"],
            },
            "algorithm": profile.algorithm,
            "dependencies": dependencies,
            "training_input": input_document,
            "validation_report": report,
            "industry_sample_counts": {"银行": {"training_rows": 20_000, "validation_rows": 400}},
            "model_hash": model["content_hash"],
        }
    )
    return model, metadata


def _write_bundle(
    staging: Path,
    contract: TrainedHeadContract,
    profile: TrainedProfileContract = V3_TRAINING_PROFILE,
) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    model, metadata = _documents(contract, profile)
    (staging / "model.json").write_text(json.dumps(model), encoding="utf-8")
    (staging / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _publish_profile(training_root: Path, profile: TrainedProfileContract) -> None:
    staged = []
    for contract in profile.heads:
        staging = training_root / f"staging-{profile.profile_id}-{contract.strategy.value}"
        _write_bundle(staging, contract, profile)
        staged.append((contract.strategy, staging))
    PublishProfileBundles(training_root / profile.output_directory, profile, tuple(staged))

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from trader.application.research.tomorrow_daily_close_training import (
    CandidateModelArtifact,
    DailyCloseSourceSample,
    ModelDependencyVersion,
    StockResidualCorrection,
    StratumCorrection,
    ValidationMetrics,
    ValidationReport,
    build_feature_dataset,
)
from trader.infra.research.tomorrow_daily_close_artifacts import (
    TomorrowDailyCloseArtifactCodec,
    TomorrowDailyCloseArtifactError,
)


def _artifact() -> CandidateModelArtifact:
    return CandidateModelArtifact(
        model_id="tomorrow_daily_close_candidate_v1",
        candidate_id="ridge_lightgbm_ensemble",
        base_model_kind="ridge_lightgbm_ensemble",
        manifest_hash="a" * 64,
        filter_spec_hash="a" * 64,
        confirmation_report_hash="b" * 64,
        feature_names=("qfq_return_1d",),
        feature_units=("ratio",),
        preprocessing_means=(0.1,),
        preprocessing_scales=(1.0,),
        ridge_intercept=0.0,
        ridge_coefficients=(0.3,),
        lightgbm_model_text="tree\nend",
        lightgbm_best_iteration=3,
        stratum_corrections=(),
        stock_residual_corrections=(),
        trained_from=date(2020, 1, 2),
        trained_through=date(2024, 12, 31),
        dependencies=(ModelDependencyVersion("python", "3.10"),),
    )


def test_json_codec_round_trips_candidate_model_without_pickle() -> None:
    artifact = _artifact()

    encoded = TomorrowDailyCloseArtifactCodec.encode(artifact)
    decoded = TomorrowDailyCloseArtifactCodec.decode_candidate_model(encoded)

    assert decoded == artifact
    assert decoded.content_hash == artifact.content_hash
    assert "pickle" not in encoded.lower()


@pytest.mark.parametrize("base_model_kind", ("ridge", "lightgbm", "ridge_lightgbm_ensemble"))
def test_json_codec_round_trips_each_base_model_kind_and_shrinkage_contract(base_model_kind: str) -> None:
    artifact = replace(
        _artifact(),
        base_model_kind=base_model_kind,
        ridge_intercept=None if base_model_kind == "lightgbm" else 0.0,
        ridge_coefficients=None if base_model_kind == "lightgbm" else (0.3,),
        lightgbm_model_text=None if base_model_kind == "ridge" else "tree\nend",
        lightgbm_best_iteration=None if base_model_kind == "ridge" else 3,
        stratum_corrections=(StratumCorrection("board", "main", 100, 50, 500.0, 0.001),),
        stock_residual_corrections=(StockResidualCorrection("600001", 250, 120, 1_000.0, 0.02, 0.002),),
    )

    decoded = TomorrowDailyCloseArtifactCodec.decode_candidate_model(TomorrowDailyCloseArtifactCodec.encode(artifact))

    assert decoded == artifact


def test_json_codec_round_trips_manifest_feature_dataset_and_validation_report() -> None:
    day = date(2024, 1, 2)
    dataset = build_feature_dataset(
        (
            DailyCloseSourceSample(
                trade_date=day,
                label_maturity_date=date(2024, 1, 3),
                code="600001",
                board="main",
                feature_values=(0.01,),
                net_excess_returns=(0.01, 0.007, 0.002),
                hard_filter_passed=True,
                hard_filter_evidence_complete=True,
                filter_evidence_hash="a" * 64,
                source_row_hash="b" * 64,
            ),
        ),
        feature_names=("qfq_return_1d",),
        feature_units=("ratio",),
        source_archive_hash="a" * 64,
        filter_spec_hash="b" * 64,
    )
    report = ValidationReport(
        status="historical_data_insufficient",
        manifest_hash=dataset.manifest.content_hash,
        candidate_model_artifact_hash=None,
        metrics=ValidationMetrics.empty(),
        failure_reasons=("terminal_trade_dates_below_200",),
    )

    assert (
        TomorrowDailyCloseArtifactCodec.decode_manifest(TomorrowDailyCloseArtifactCodec.encode(dataset.manifest))
        == dataset.manifest
    )
    assert (
        TomorrowDailyCloseArtifactCodec.decode_feature_dataset(TomorrowDailyCloseArtifactCodec.encode(dataset))
        == dataset
    )
    assert (
        TomorrowDailyCloseArtifactCodec.decode_validation_report(TomorrowDailyCloseArtifactCodec.encode(report))
        == report
    )


def test_json_codec_rejects_content_tampering_and_unknown_fields() -> None:
    payload = json.loads(TomorrowDailyCloseArtifactCodec.encode(_artifact()))
    payload["ridge_intercept"] = 99.0

    with pytest.raises(TomorrowDailyCloseArtifactError, match="hash"):
        TomorrowDailyCloseArtifactCodec.decode_candidate_model(json.dumps(payload))

    payload = json.loads(TomorrowDailyCloseArtifactCodec.encode(_artifact()))
    payload["unexpected"] = True
    payload_without_hash = {key: value for key, value in payload.items() if key != "content_hash"}
    from trader.application.research.replay_models import canonical_hash

    payload["content_hash"] = canonical_hash(payload_without_hash)
    with pytest.raises(TomorrowDailyCloseArtifactError, match="schema"):
        TomorrowDailyCloseArtifactCodec.decode_candidate_model(json.dumps(payload))

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from trader.application.research.tomorrow_historical_p2_models import (
    TomorrowHistoricalP2GateMetrics,
    TomorrowHistoricalP2Report,
)
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2ModelArtifact
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.tomorrow_historical_p2 import (
    TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
    TOMORROW_HISTORICAL_P2_SPEC,
)
from trader.infra.research.tomorrow_historical_p2_artifacts import (
    TomorrowHistoricalP2ArtifactConflictError,
    TomorrowHistoricalP2ArtifactStore,
)


def _artifact() -> TomorrowHistoricalP2ModelArtifact:
    return TomorrowHistoricalP2ModelArtifact(
        candidate_id=TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
        feature_ids=("a", "b"),
        transformer_means=(0.0, 0.0),
        transformer_scales=(1.0, 1.0),
        linear_intercept=0.0,
        linear_coefficients=(1.0, 0.0),
        lightgbm_model="test-model",
        lightgbm_best_iteration=1,
        training_rows=10,
        internal_validation_rows=2,
    )


def _report(artifact: TomorrowHistoricalP2ModelArtifact) -> TomorrowHistoricalP2Report:
    metrics = TomorrowHistoricalP2GateMetrics(
        archive_coverage=0.98,
        training_trade_dates=10,
        validation_trade_dates=10,
        validation_pairs=10,
        mean_net_increment_20bp=-0.01,
        mean_net_increment_50bp=-0.01,
        mean_net_increment_100bp=-0.01,
        bootstrap_lower_bound_20bp=-0.01,
        baseline_severe_loss_rate=0.0,
        candidate_severe_loss_rate=0.1,
        turnover_increase=0.0,
        mean_rank_ic=-0.1,
        top_bottom_quintile_spread=-0.1,
        maximum_stock_positive_fraction=1.0,
        top_five_positive_fraction=1.0,
        maximum_board_fraction=0.5,
    )
    return TomorrowHistoricalP2Report(
        research_spec_hash=TOMORROW_HISTORICAL_P2_SPEC.content_hash,
        source_spec_hash=SCORE_H0_V1_SPEC.content_hash,
        source_manifest_hash="1" * 64,
        source_universe_hash="2" * 64,
        source_histories_hash="3" * 64,
        candidate_id=TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
        status="historical_rejected",
        metrics=metrics,
        training_evidence_hash="4" * 64,
        validation_evidence_hash="5" * 64,
        model_artifact_hash=artifact.content_hash,
        failure_reasons=("mean_increment_not_positive",),
    )


def test_p2_report_and_model_are_idempotent_tamper_evident_and_inspectable(tmp_path) -> None:  # noqa: ANN001
    artifact = _artifact()
    report = _report(artifact)
    store = TomorrowHistoricalP2ArtifactStore(tmp_path)

    assert store.seal(report, artifact) == report.content_hash
    assert store.seal(report, artifact) == report.content_hash
    assert store.inspect() == {
        "report_hash": report.content_hash,
        "status": "historical_rejected",
        "candidate_id": TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
        "failure_reasons": ["mean_increment_not_positive"],
        "forward_preregistration_eligible": False,
        "production_authority": False,
    }

    path = tmp_path / TOMORROW_HISTORICAL_P2_SPEC.research_identity / "historical-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "historical_passed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TomorrowHistoricalP2ArtifactConflictError, match="hash"):
        store.inspect()


def test_p2_store_rejects_a_report_bound_to_another_model(tmp_path) -> None:  # noqa: ANN001
    artifact = _artifact()
    report = _report(artifact)

    with pytest.raises(ValueError, match="model binding"):
        TomorrowHistoricalP2ArtifactStore(tmp_path).seal(
            replace(report, model_artifact_hash="f" * 64),
            artifact,
        )


def test_p2_store_rejects_a_tampered_bound_model_on_direct_report_replay(tmp_path) -> None:  # noqa: ANN001
    artifact = _artifact()
    report = _report(artifact)
    store = TomorrowHistoricalP2ArtifactStore(tmp_path)
    store.seal(report, artifact)
    path = tmp_path / TOMORROW_HISTORICAL_P2_SPEC.research_identity / "model-artifact.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lightgbm_model"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TomorrowHistoricalP2ArtifactConflictError, match="hash"):
        store.read_report_payload()

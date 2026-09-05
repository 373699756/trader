from __future__ import annotations

import json

import pytest

from trader.application.research.tomorrow_historical_validation import (
    HISTORICAL_RISK_VALIDATION_SPEC,
    HistoricalRiskModelArtifact,
    HistoricalRiskValidationOutcome,
    HistoricalRiskValidationReport,
)
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.infra.research.tomorrow_historical_risk_artifacts import (
    TomorrowHistoricalRiskArtifactConflictError,
    TomorrowHistoricalRiskArtifactStore,
)


def _outcome() -> HistoricalRiskValidationOutcome:
    model = HistoricalRiskModelArtifact(
        spec_hash=HISTORICAL_RISK_VALIDATION_SPEC.content_hash,
        source_spec_hash=SCORE_H0_V1_SPEC.content_hash,
        parent_model_id="test-risk-model",
        parent_model_hash="1" * 64,
        feature_ids=(
            "net_predicted_excess_20bp",
            "model_disagreement",
            "signal_score",
            "atr20_pct",
            "estimated_cost",
        ),
        logistic_intercept=-1.0,
        logistic_coefficients=(0.1, 0.2, 0.3, 0.4, 0.5),
        platt_intercept=0.0,
        platt_slope=1.0,
        platt_constant=None,
        training_evidence_hash="2" * 64,
        calibration_evidence_hash="3" * 64,
    )
    report = HistoricalRiskValidationReport(
        spec_hash=HISTORICAL_RISK_VALIDATION_SPEC.content_hash,
        model_id="test-risk-model",
        model_hash="1" * 64,
        evidence_hash="4" * 64,
        training_trade_dates=60,
        calibration_trade_dates=20,
        test_trade_dates=40,
        embargo_trade_dates=2,
        training_rows=600,
        calibration_rows=200,
        test_rows=400,
        brier_score=0.10,
        baseline_brier_score=0.12,
        expected_calibration_error=0.04,
        model_artifact_hash=model.content_hash,
        status="historical_validated",
        failure_reasons=(),
    )
    return HistoricalRiskValidationOutcome(report, model)


def test_historical_risk_model_and_report_are_bound_idempotent_artifacts(tmp_path) -> None:  # noqa: ANN001
    store = TomorrowHistoricalRiskArtifactStore(tmp_path)
    outcome = _outcome()
    assert outcome.model_artifact is not None

    assert store.seal(outcome) == outcome.report.content_hash
    assert store.seal(outcome) == outcome.report.content_hash
    assert store.inspect() == {
        "status": "historical_validated",
        "report_hash": outcome.report.content_hash,
        "model_artifact_hash": outcome.model_artifact.content_hash,
        "brier_score": 0.10,
        "baseline_brier_score": 0.12,
        "expected_calibration_error": 0.04,
        "production_authority": False,
    }


def test_historical_risk_artifact_tampering_fails_closed(tmp_path) -> None:  # noqa: ANN001
    store = TomorrowHistoricalRiskArtifactStore(tmp_path)
    store.seal(_outcome())
    model_path = (
        tmp_path
        / "tomorrow-v2-historical-risk"
        / HISTORICAL_RISK_VALIDATION_SPEC.research_identity
        / "model-artifact.json"
    )
    payload = json.loads(model_path.read_text(encoding="utf-8"))
    payload["logistic_intercept"] = 9.0
    model_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TomorrowHistoricalRiskArtifactConflictError, match="invalid"):
        store.inspect()

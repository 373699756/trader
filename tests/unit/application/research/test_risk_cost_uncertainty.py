from __future__ import annotations

from datetime import date

from trader.recommendation.domain.risk.decision import RiskDecision, UncertaintyAssessment
from trader.recommendation.domain.scoring.alpha import AlphaScore
from trader.recommendation.domain.selection.execution_cost import ExecutionCost, ExecutionCostScenario
from trader.training.application.risk_cost_uncertainty import (
    RiskCostUncertaintyPrerequisite,
    RiskCostUncertaintyResearchBuilder,
)
from trader.training.domain.evaluation.risk_cost_uncertainty import DeepSeekResearchReview, RiskCostUncertaintySample


class _Evidence:
    def __init__(self) -> None:
        self.calls = 0

    def load_samples(self, prerequisite):
        del prerequisite
        self.calls += 1
        return None


class _InvalidEvidence:
    def __init__(self) -> None:
        self.calls = 0

    def load_samples(self, prerequisite):
        del prerequisite
        self.calls += 1
        return ()


def test_parent_historical_insufficiency_returns_typed_report_without_reading_evidence() -> None:
    evidence = _Evidence()
    prerequisite = RiskCostUncertaintyPrerequisite(
        "historical_data_insufficient",
        "a" * 64,
        None,
        False,
        ("daily_close_proxy_not_point_in_time",),
    )

    report = RiskCostUncertaintyResearchBuilder(evidence).build(prerequisite)

    assert evidence.calls == 0
    assert report.status == "historical_data_insufficient"
    assert report.failure_reasons == (
        "daily_close_proxy_not_point_in_time",
        "risk_cost_parent_evidence_unavailable",
    )
    assert report.calibration is None
    assert report.ablation == ()
    assert report.evidence_hash is None
    assert report.production_authority is False
    assert report.terminal_holdout_opened is False


def test_validated_parent_reads_evidence_once_and_rejects_an_empty_population() -> None:
    evidence = _InvalidEvidence()
    prerequisite = RiskCostUncertaintyPrerequisite(
        "historical_validated",
        "a" * 64,
        "b" * 64,
        True,
        (),
    )

    report = RiskCostUncertaintyResearchBuilder(evidence).build(prerequisite)

    assert evidence.calls == 1
    assert report.status == "historical_data_insufficient"
    assert report.failure_reasons == ("risk_cost_evidence_invalid",)
    assert report.sample_count == 0


class _MissingStressEvidence:
    def __init__(self) -> None:
        self.calls = 0

    def load_samples(self, prerequisite: RiskCostUncertaintyPrerequisite) -> tuple[RiskCostUncertaintySample, ...]:
        self.calls += 1
        assert prerequisite.status == "historical_validated"
        code = "600001"
        sample = RiskCostUncertaintySample(
            date(2026, 8, 31),
            "main",
            "verified_industry",
            AlphaScore(code, "v3", "industry_ridge_lightgbm", "a" * 64, "b" * 64, 90, 0.01),
            RiskDecision(code, 0, False, (), UncertaintyAssessment(None, None, None, None, None, None)),
            ExecutionCost(code, 0.002, None, None, (ExecutionCostScenario("cost_20bp", 0.002),)),
            DeepSeekResearchReview("failed", None, 0, False, (), "ignored"),
            0.012,
            0.01,
            False,
        )
        return (sample,)


def test_missing_pressure_scenarios_discards_all_calibration_and_ablation_results() -> None:
    source = _MissingStressEvidence()
    prerequisite = RiskCostUncertaintyPrerequisite("historical_validated", "c" * 64, "d" * 64, True, ())

    report = RiskCostUncertaintyResearchBuilder(source).build(prerequisite)

    assert source.calls == 1
    assert report.status == "historical_data_insufficient"
    assert report.failure_reasons == ("risk_cost_evidence_invalid",)
    assert report.calibration is None
    assert report.ablation == report.decisions == ()
    assert report.evidence_hash is None
    assert report.production_authority is report.terminal_holdout_opened is False

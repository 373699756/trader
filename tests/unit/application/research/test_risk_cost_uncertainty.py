from __future__ import annotations

from trader.application.research.risk_cost_uncertainty import (
    RiskCostUncertaintyPrerequisite,
    RiskCostUncertaintyResearchBuilder,
)


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

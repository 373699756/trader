from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company.company_startup import _AT, _snapshot
from tests.helpers.recommendation.company_replay_fixture import BuildCompanyReplayCase
from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, _native_input, _verified_feature
from tests.helpers.recommendation.runtime.input_runtime import _policy
from trader.download.domain.company_source_records import (
    CompanyFinancialScoringObservation, CompanyFinancialSourceObservation, CompanyFinancialSourceRecordHash,
)
from trader.recommendation.application.pipeline.final_selection.company_replay import (
    CompanyReplayInputs,
    CompanyReplayPublication,
    ProjectCompanyReplayDecision,
    ReplayCompanyDecisions,
)
from trader.recommendation.application.pipeline.final_selection.freeze_projection import BuildScoredLocal, ScoredProjectionInputs
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.tomorrow_probability import TomorrowProbabilityCapability
from trader.recommendation.application.tomorrow.v2.plan import V2_TOMORROW_PLAN
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import ScoredDisposition, Strategy
from trader.recommendation.domain.scoring.company import D25FinancialScores


@pytest.fixture
def company_replay_case(application_feature_factory):
    return BuildCompanyReplayCase(application_feature_factory)


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_company_replay_compares_fixed_seats_and_rebuilds_risk_denominator(application_feature_factory, profile):
    inputs, policy, runtime, observed = BuildCompanyReplayCase(application_feature_factory, profile=profile)
    report = ReplayCompanyDecisions(inputs, policy, runtime=runtime)
    baseline, risk, financial, combined = report.arms
    assert report.profile == profile
    assert report.strategy is Strategy.D25
    assert report.prediction_target_bps == 100
    assert baseline.decision == inputs.expected_baseline
    assert len(observed.stages.entries) == 6
    assert tuple((row.code, row.rank, row.action, row.action_reason) for row in baseline.seats) == tuple(
        (entry.code, entry.rank, entry.action, entry.action_reason) for entry in observed.stages.entries
    )
    risk_rows = {row.code: row for row in risk.decision.rows}
    assert risk_rows["600000"].disposition is ScoredDisposition.REJECT
    assert risk_rows["600000"].filter_reasons == ("latest_disclosed_report_loss",)
    assert risk_rows["600000"].rank is None
    assert risk.decision.cost_reference != baseline.decision.cost_reference
    assert len(risk.seats) == 6 and "688000" in {row.code for row in risk.seats}
    assert risk_rows["600001"].company_coverage.announcements == "unknown"
    assert risk_rows["688002"].disposition is ScoredDisposition.PASS
    assert risk_rows["688002"].selection_reason == "candidate_input_unavailable"
    assert risk_rows["688002"].rank is None and risk_rows["688002"].action is None

    assert financial.seats[0].code == "600001"
    assert financial.seats[0].company_coverage.announcements == "known_clear"
    assert financial.seats[0].local_score > risk_rows["600001"].local_score
    assert "600000" in {row.code for row in financial.seats}
    assert financial.decision.cost_reference == baseline.decision.cost_reference
    assert combined.decision.cost_reference == risk.decision.cost_reference
    assert {row.code for row in combined.seats} == {row.code for row in risk.seats}
    assert combined.seats[0].local_score == financial.seats[0].local_score
    assert tuple(row.code for row in combined.decision.rows) == tuple(row.code for row in baseline.decision.rows)
    assert inputs.baseline.market_features[0].values.get("latest_disclosed_report_loss") != 1.0


def test_only_financial_replay_arms_receive_supplier_scores(application_feature_factory):
    inputs, policy, runtime, _ = BuildCompanyReplayCase(application_feature_factory, evaluated_at=_AT + timedelta(days=1))
    report = _snapshot().financials[0]
    issuer = f"{report.code}.SH"
    source = CompanyFinancialSourceObservation(
        report, issuer, _AT, None, _AT, "CNY", CompanyFinancialSourceRecordHash(report, issuer, _AT, None, "CNY"), (),
    )
    scores = D25FinancialScores(CompanyFinancialScoringObservation(source, _AT), 10.0, 10.0, 10.0, _AT)
    baseline = inputs.baseline

    def overlay(rows):
        return tuple(replace(row, d25_financial_scores=scores) if row.quote.code == report.code else row for row in rows)

    candidate = replace(
        baseline, market_features=overlay(baseline.market_features), candidate_features=overlay(baseline.candidate_features),
    )
    original, risk, financial, combined = ReplayCompanyDecisions(
        replace(inputs, candidate_company_features=candidate), policy, runtime=runtime,
    ).arms
    assert original.decision == risk.decision
    assert financial.decision == combined.decision
    assert report.code in {row.code for row in original.seats}
    assert report.code not in {row.code for row in financial.seats}


@pytest.mark.parametrize(("sealed_offset", "adopted_offset", "used"), [
    (-2, -1, True), (0, 0, True), (-2, 3, False), (3, 3, False),
])
def test_d25_close_fallback_uses_financial_scores_available_by_close(
    application_feature_factory, sealed_offset, adopted_offset, used,
):
    close = (_AT + timedelta(days=1)).replace(hour=15)
    inputs, policy, runtime, _ = BuildCompanyReplayCase(
        application_feature_factory, evaluated_at=close + timedelta(seconds=15),
    )
    report = _snapshot().financials[0]
    issuer = f"{report.code}.SH"
    observation = CompanyFinancialSourceObservation(
        report, issuer, _AT, None, _AT, "CNY", CompanyFinancialSourceRecordHash(report, issuer, _AT, None, "CNY"), (),
    )
    scores = D25FinancialScores(
        CompanyFinancialScoringObservation(observation, close + timedelta(seconds=sealed_offset)),
        10.0, 10.0, 10.0, close + timedelta(seconds=adopted_offset),
    )

    def with_scores(rows):
        return tuple(replace(row, d25_financial_scores=scores) if row.quote.code == report.code else row for row in rows)

    baseline = replace(inputs.baseline, phase="close_fallback")
    native = replace(baseline, market_features=with_scores(baseline.market_features),
                     candidate_features=with_scores(baseline.candidate_features))
    runtime = replace(runtime, population_qualifications=None)
    control = BuildScoredLocal(baseline, policy, sequence=1, runtime=runtime)
    scored = BuildScoredLocal(native, policy, sequence=1, runtime=runtime)
    assert dict(scored.local.input_versions)["d25_close_evidence"] == f"official-close:{native.input_version}"
    row = next(item for item in scored.selection.evaluations if item.code == report.code)
    assert row.score_ledger is not None
    assert any(item.code == "supplier_financial_scoring" for item in row.score_ledger.items) is used
    base_seats = tuple(item.code for item in control.local.items)
    seats = tuple(item.code for item in scored.local.items)
    assert report.code in base_seats
    assert (report.code not in seats) is used
    if not used:
        assert scored.local.items == control.local.items


def test_company_replay_rejects_decision_drift_foreknowledge_and_quote_change(company_replay_case):
    inputs, policy, runtime, _observed = company_replay_case
    altered = replace(inputs.expected_baseline, freeze_content_hash="different-content")
    with pytest.raises(ValueError, match="baseline differs"):
        ReplayCompanyDecisions(replace(inputs, expected_baseline=altered), policy, runtime=runtime)
    future = replace(inputs.candidate_company, published_at=EVALUATED_AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="publication is unavailable"):
        ReplayCompanyDecisions(replace(inputs, candidate_company=future), policy, runtime=runtime)
    candidate = inputs.candidate_company_features
    first, *remaining = candidate.market_features
    changed_quote = replace(first, quote=replace(first.quote, price=13.0))
    altered = replace(candidate, market_features=(changed_quote, *remaining))
    with pytest.raises(ValueError, match="matching quote rows"):
        ReplayCompanyDecisions(replace(inputs, candidate_company_features=altered), policy, runtime=runtime)


def test_company_replay_same_publication_requires_same_facts(company_replay_case):
    inputs, policy, runtime, _observed = company_replay_case
    with pytest.raises(ValueError, match="facts changed without a new publication identity"):
        ReplayCompanyDecisions(replace(inputs, candidate_company=inputs.current_company), policy, runtime=runtime)
    report = ReplayCompanyDecisions(
        replace(inputs, candidate_company=inputs.current_company, candidate_company_features=inputs.baseline),
        policy,
        runtime=runtime,
    )
    assert all(arm.decision == inputs.expected_baseline for arm in report.arms)


def test_tomorrow_financial_arm_keeps_probability_decisions(application_feature_factory):
    class Predictor:
        identity = "a" * 64

        def predict_probabilities(self, rows):
            return tuple(0.60 for _ in rows)

    observed_at = EVALUATED_AT - timedelta(seconds=10)
    feature = _verified_feature(application_feature_factory("600000", observed_at))
    feature = replace(feature, quote=replace(feature.quote, volume=1_000_000.0))
    baseline = _native_input((feature,))
    changed = replace(feature, values={**feature.values, "quality_score": 100.0})
    candidate = _native_input((changed,))
    capability = TomorrowProbabilityCapability(V2_TOMORROW_PLAN, Predictor(), PredictionTarget(Strategy.TOMORROW, 500))
    runtime = ScoredProjectionInputs(model_scoring=ModelScoringRouter(
        "v2", {Strategy.TOMORROW: capability},
    ))
    policy = _policy()
    expected = ProjectCompanyReplayDecision(BuildScoredLocal(baseline, policy, sequence=1, runtime=runtime))
    inputs = CompanyReplayInputs(
        baseline,
        candidate,
        CompanyReplayPublication("company-current", observed_at - timedelta(days=2)),
        CompanyReplayPublication("company-candidate", observed_at - timedelta(days=1)),
        expected,
    )
    report = ReplayCompanyDecisions(inputs, policy, runtime=runtime)
    assert report.prediction_target_bps == 500
    seat, = report.arms[0].seats
    assert seat.ranking_score == 60.0
    assert report.arms[2].decision == report.arms[0].decision
    assert report.arms[3].decision == report.arms[1].decision

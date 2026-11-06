from __future__ import annotations

from dataclasses import replace

from datetime import date, timedelta


import pytest

from tests.helpers.download.company.company_score_ledger import _document, _facts

from tests.helpers.recommendation.epoch_helpers import candidate_field_values, daily_field_values, research_field_values

from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    AssembleScoredFeatures,
    ScoredSelectionNotReadyError,
    ScoredSelectionUseCase,
)

from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot

from trader.recommendation.domain.market.epochs import ResearchEpoch

from trader.recommendation.domain.market.models import Evidence

from trader.recommendation.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    FinancialReport,
    ResearchObservation,
)


from trader.recommendation.domain.scoring.company import (
    AssessCompanyAdjustments,
    BuildCompanyScoringContext,
    CompanyScoringCalendar,
    CompanyFinancialScores,
)

from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture as CompanyScoringPolicy

from tests.helpers.recommendation.selection.application.tomorrow_selection import (
    NOW,
    _Reader,
    _data_snapshot,
    _policy,
)


def test_use_case_assembles_one_coherent_epoch_and_applies_candidate_quote(
    recommendation_policy,
) -> None:
    snapshot = _data_snapshot()
    use_case = ScoredSelectionUseCase(_Reader(snapshot), _policy(recommendation_policy))
    assembled = AssembleScoredFeatures(snapshot)

    result = use_case.execute(evaluated_at=NOW, max_age_seconds=60.0)
    evaluation = result.evaluations[0]

    assert evaluation.features.quote.price == 10.2
    assert evaluation.features.quote.high == 10.2
    assert evaluation.features.quote.source == "tencent"
    assert evaluation.features.quote.cross_source_deviation_pct == 0.2
    assert evaluation.features.quote.cross_source_verified is True
    assert evaluation.features.values["tail_return_30m"] == 91.0
    assert evaluation.features.values["entry_quality"] == 84.0
    assert evaluation.features.market_regime == "risk_on"
    assert evaluation.features.merge_epoch.startswith("market:")
    assert snapshot.candidate_quotes is not None
    assert snapshot.candidate_quotes.version in assembled[0].merge_epoch
    assert snapshot.candidate_quotes.version in evaluation.features.merge_epoch
    assert evaluation.disposition.value == "observe_only"
    assert tuple(flag.code for flag in evaluation.optional_flags) == ("board_data_reliability_below_threshold",)


def test_use_case_refuses_to_score_without_a_coherent_market_epoch(recommendation_policy) -> None:
    snapshot = _data_snapshot()
    reader = _Reader(MarketDataPlaneSnapshot(snapshot.daily_features, None, None, None))
    use_case = ScoredSelectionUseCase(reader, _policy(recommendation_policy))

    with pytest.raises(ScoredSelectionNotReadyError, match="coherent_market_epoch_unavailable"):
        use_case.execute(evaluated_at=NOW, max_age_seconds=60.0)

    current_use_case = ScoredSelectionUseCase(_Reader(snapshot), _policy(recommendation_policy))
    with pytest.raises(ScoredSelectionNotReadyError, match="market_epoch_from_future"):
        current_use_case.execute(evaluated_at=NOW - timedelta(seconds=1), max_age_seconds=60.0)

    assert snapshot.market is not None
    future_received_market = replace(snapshot.market, received_at=NOW + timedelta(seconds=1))
    future_received = replace(snapshot, market=future_received_market, candidate_quotes=None)
    with pytest.raises(ScoredSelectionNotReadyError, match="market_epoch_from_future"):
        ScoredSelectionUseCase(_Reader(future_received), _policy(recommendation_policy)).execute(
            evaluated_at=NOW,
            max_age_seconds=60.0,
        )


def test_feature_assembly_does_not_let_late_candidate_price_replace_newer_market_price() -> None:
    snapshot = _data_snapshot()
    assert snapshot.candidate_quotes is not None
    late_quote = replace(
        snapshot.candidate_quotes.quotes[0],
        source_time=NOW - timedelta(seconds=1),
    )
    candidate_epoch = replace(
        snapshot.candidate_quotes,
        quotes=(late_quote,),
        field_values={late_quote.code: candidate_field_values(late_quote)},
    )

    assembled = AssembleScoredFeatures(replace(snapshot, candidate_quotes=candidate_epoch))

    assert assembled[0].quote.price == 10.0
    assert assembled[0].values["tail_return_30m"] == 70.0
    assert snapshot.market is not None
    assert assembled[0].merge_epoch == snapshot.market.version


def test_feature_assembly_keeps_company_context_and_does_not_label_soft_risk_clear() -> None:
    facts = _facts(
        (_document("warning", "关于收到警示函的公告", "本公司收到证监会出具警示函的决定。", NOW - timedelta(hours=1)),),
        at=NOW,
    )
    policy = CompanyScoringPolicy(enabled=True)
    context = BuildCompanyScoringContext(facts, CompanyScoringCalendar(()), policy)
    research = ResearchEpoch(
        NOW.date(),
        1,
        NOW,
        NOW,
        "runtime-current",
        {"600001": ResearchObservation(company_context=context, announcements_available=True, loaded_at=NOW)},
        {"company": "company-fixture"},
        field_values={
            "600001": research_field_values(source_time=NOW, received_time=NOW, data_version="company-fixture")
        },
    )
    feature = AssembleScoredFeatures(replace(_data_snapshot(), research=research))[0]
    assert feature.company_context is context
    assert feature.research_components.announcements == "known_risk"
    result = AssessCompanyAdjustments(
        feature.company_context, policy, "tomorrow", NOW, financial_scores=CompanyFinancialScores(100, 100, 100)
    )
    assert result.risks[0].penalty == 3
    assert not any(item.reason == "eligible" for item in result.bonuses)


def test_feature_assembly_applies_coherent_research_evidence_and_current_corporate_risk() -> None:
    snapshot = _data_snapshot()
    evidence = Evidence(
        evidence_id="official-risk",
        evidence_type="regulatory_filing",
        title="监管立案公告",
        source="exchange",
        published_at=NOW - timedelta(hours=1),
        received_at=NOW - timedelta(minutes=30),
        data_version="research-1",
    )
    research = ResearchEpoch(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-current",
        observations={
            "600001": ResearchObservation(
                financial=FinancialReport(
                    report_date=date(2026, 6, 30),
                    published_at=NOW - timedelta(days=1),
                    parent_net_profit=-3.0,
                    core_net_profit=2.0,
                ),
                corporate_risk_facts=(
                    CorporateRiskFact(
                        category=CorporateRiskCategory.OFFICIAL_INVESTIGATION,
                        announced_at=NOW - timedelta(hours=1),
                        evidence_id=evidence.evidence_id,
                        source="exchange_disclosure",
                    ),
                ),
                corporate_risk_window_complete=True,
                corporate_risk_evidence_version="risk-1",
                evidence=(evidence,),
            )
        },
        source_versions={"exchange": "research-1"},
        field_values={
            "600001": research_field_values(
                source_time=NOW - timedelta(hours=1),
                received_time=NOW,
                data_version="research-1",
            )
        },
    )

    assembled = AssembleScoredFeatures(replace(snapshot, research=research))

    assert assembled[0].values["official_investigation_history"] == 1.0
    assert assembled[0].values["corporate_risk_window_unavailable"] == 0.0
    assert assembled[0].values["latest_disclosed_report_loss"] == 1.0
    assert evidence in assembled[0].evidence
    assert {item.evidence_type for item in assembled[0].evidence} == {
        "structured_point_in_time",
        "intraday_tail",
        "regulatory_filing",
    }


def test_latest_report_with_missing_profit_clears_older_loss_value() -> None:
    snapshot = _data_snapshot()
    assert snapshot.daily_features is not None
    daily_row = snapshot.daily_features.rows[0]
    stale_values = {**daily_row.values, "latest_disclosed_report_loss": 1.0}
    stale_daily = replace(
        snapshot.daily_features,
        rows=(
            replace(
                daily_row,
                values=stale_values,
                field_values=daily_field_values(
                    stale_values,
                    source_time=NOW - timedelta(days=1),
                    received_time=NOW,
                ),
            ),
        ),
    )
    assert snapshot.market is not None
    market = replace(snapshot.market, daily_feature_pack_version=stale_daily.version)
    research = ResearchEpoch(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-current",
        observations={
            "600001": ResearchObservation(
                financial=FinancialReport(
                    report_date=date(2026, 6, 30),
                    published_at=NOW - timedelta(days=1),
                    parent_net_profit=3.0,
                )
            )
        },
        source_versions={"financial": "research-1"},
        field_values={
            "600001": research_field_values(
                source_time=NOW - timedelta(days=1),
                received_time=NOW,
                data_version="research-1",
            )
        },
    )

    assembled = AssembleScoredFeatures(MarketDataPlaneSnapshot(stale_daily, market, None, research))

    assert assembled[0].values["latest_disclosed_report_loss"] is None

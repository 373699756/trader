from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_events import CompanyRiskType
from tests.helpers.download.company.company_startup import _snapshot
from tests.helpers.download.configuration import CompanyEventPolicyFixture as CompanyEventPolicy
from tests.helpers.recommendation.configuration import StrategySettingsFixture
from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, Disclosure
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField,
    CompanyFinancialScoringObservation,
    CompanyFinancialSourceObservation,
    CompanyFinancialSourceRecordHash,
)
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    DeriveCorporateRiskFeatures,
    DeriveD25FinancialScores,
    DeriveLatestDisclosedReportLoss,
    DeriveLongResearchFeatures,
    DeriveMarketRegime,
    FeatureComponentWeightPolicy,
    FinancialReport,
    FinancialReportIsFresh,
    LongResearchInputs,
    LongResearchPolicy,
    MarketRegimePolicy,
    ResearchAnnouncement,
    ResearchComponentsFromObservation,
    ResearchObservation,
)
from trader.recommendation.domain.publication.models import BoardStrategyPolicy, Strategy
from trader.recommendation.domain.scoring.company import CompanyScoringContext
from trader.recommendation.domain.scoring.scoring import ScoreBoardStrategy

NOW = datetime.fromisoformat("2026-07-16T14:50:00+08:00")


def _extract(announcements):
    documents = tuple(
        CompanyDocument(
            Disclosure(item.source, item.announcement_id, ("600001",), item.title, item.published_at, "", "fixture"),
            item.body,
            item.body_checked,
        )
        for item in announcements
    )
    return ExtractCompanyEvents(documents, NOW, CompanyEventPolicy())


def test_market_regime_uses_exact_configured_boundaries() -> None:
    policy = MarketRegimePolicy(risk_on_breadth_min=60.0, risk_off_breadth_max=40.0)

    assert DeriveMarketRegime(60.0, policy) == "risk_on"
    assert DeriveMarketRegime(40.0, policy) == "risk_off"
    assert DeriveMarketRegime(50.0, policy) == "neutral"
    assert DeriveMarketRegime(None, policy) == "neutral"


def test_latest_disclosed_report_supersedes_older_loss_regardless_of_period_type() -> None:
    observation = ResearchObservation(
        financial_history=(
            FinancialReport(
                date(2025, 12, 31),
                NOW - timedelta(days=100),
                parent_net_profit=-10.0,
                core_net_profit=-8.0,
            ),
            FinancialReport(
                date(2026, 3, 31),
                NOW - timedelta(days=20),
                parent_net_profit=3.0,
                core_net_profit=2.0,
            ),
        )
    )

    assert DeriveLatestDisclosedReportLoss(observation, NOW) == 0.0


@pytest.mark.parametrize("negative_field", ("parent_net_profit", "core_net_profit"))
def test_latest_disclosed_report_loss_uses_either_profit_field(negative_field: str) -> None:
    profits = {"parent_net_profit": 3.0, "core_net_profit": 2.0, negative_field: -1.0}
    report = FinancialReport(date(2026, 6, 30), NOW - timedelta(days=1), **profits)

    assert DeriveLatestDisclosedReportLoss(ResearchObservation(financial=report), NOW) == 1.0


def test_latest_disclosed_report_loss_is_point_in_time_and_requires_both_profit_fields() -> None:
    known = FinancialReport(
        date(2026, 3, 31),
        NOW - timedelta(days=20),
        parent_net_profit=3.0,
        core_net_profit=2.0,
    )
    future = FinancialReport(
        date(2026, 6, 30),
        NOW + timedelta(days=1),
        parent_net_profit=-3.0,
        core_net_profit=-2.0,
    )
    missing = FinancialReport(
        date(2026, 6, 30),
        NOW - timedelta(days=1),
        parent_net_profit=3.0,
    )
    known_loss_with_missing_peer = FinancialReport(
        date(2026, 6, 30),
        NOW - timedelta(days=1),
        parent_net_profit=-3.0,
    )

    assert DeriveLatestDisclosedReportLoss(ResearchObservation(financial_history=(known, future)), NOW) == 0.0
    assert DeriveLatestDisclosedReportLoss(ResearchObservation(financial=missing), NOW) is None
    assert DeriveLatestDisclosedReportLoss(ResearchObservation(financial=known_loss_with_missing_peer), NOW) == 1.0


def test_supplier_financial_observation_scores_only_d25_after_actual_availability(application_feature_factory):
    disclosed_at = NOW - timedelta(days=2)
    financial = CompanyFinancial("600001", date(2026, 6, 30), disclosed_at, 1.0, 5.0, -10.0, -22.0, None, 12.0, -3.0, -2.0)
    record_hash = CompanyFinancialSourceRecordHash(financial, "600001.SH", disclosed_at, None, "CNY")
    supplier = CompanyFinancialSourceObservation(
        financial, "600001.SH", disclosed_at, None, NOW - timedelta(hours=2), "CNY", record_hash,
        (CompanyFinancialMissingField("core_profit_growth_pct"),),
    )
    source = CompanyFinancialScoringObservation(supplier, NOW)
    observation = ResearchObservation(financial_scoring_observation=source)
    assert DeriveD25FinancialScores(observation, 20.0, _long_policy(), observed_at=NOW - timedelta(seconds=1)) is None
    preload_at = NOW + timedelta(seconds=1)
    adopted = replace(observation, loaded_at=preload_at)
    assert DeriveD25FinancialScores(adopted, 20.0, _long_policy(), observed_at=NOW) is None
    assert DeriveD25FinancialScores(adopted, 20.0, _long_policy(), observed_at=preload_at) is not None
    scores = DeriveD25FinancialScores(observation, 20.0, _long_policy(), observed_at=NOW)
    assert scores is not None and scores.source == source
    assert scores.growth == 18.0
    assert scores.quality == 85.0
    assert scores.value is not None
    assert DeriveLatestDisclosedReportLoss(observation, NOW) is None
    assert ResearchComponentsFromObservation(observation, NOW).financial == "unknown"
    features = DeriveLongResearchFeatures(
        observation, LongResearchInputs(20, 80, 70, 80), _long_policy(), _feature_component_weights(), observed_at=NOW,
    )
    assert features["financial_deterioration"] is None
    assert features["quality_score"] is None

    verified = FinancialReport(date(2026, 3, 31), NOW - timedelta(days=20), basic_eps=2.0, parent_net_profit=-3.0)
    confirmed = replace(observation, financial=verified, financial_history=(verified,))
    assert DeriveD25FinancialScores(confirmed, 20.0, _long_policy(), observed_at=NOW) is None
    assert DeriveLatestDisclosedReportLoss(confirmed, NOW) == 1.0

    snapshot = application_feature_factory("600001", NOW)
    missing_fields = ("quality_score", "value_score", "growth_score")
    original = replace(
        snapshot,
        quote=replace(snapshot.quote, board=Board.MAIN),
        values={**snapshot.values, **dict.fromkeys(missing_fields)},
        missing_fields=missing_fields,
    )
    company = CompanyScoringContext(CompileCompanyFacts(_snapshot(), CompanyEventPolicy()), (), False, scores)
    supplied = replace(original, company_context=company)
    settings = StrategySettingsFixture()
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        policy = BoardStrategyPolicy(
            policy_id=f"test:{strategy.value}",
            version=settings.board_policy_version,
            board=Board.MAIN,
            strategy=strategy,
            local_weights=settings.board_local_strategy_weights[strategy.value][Board.MAIN.value],
            local_component_weights=settings.local_component_weights[strategy.value],
        )
        before, after = (ScoreBoardStrategy(item, policy) for item in (original, supplied))
        if strategy is Strategy.TOMORROW:
            assert after == before
        else:
            assert after.components["quality_penalty"] < before.components["quality_penalty"]
            assert after.components["financial_contribution"] > 0.0
    assert supplied.values == original.values
    assert supplied.missing_fields == original.missing_fields


@pytest.mark.parametrize("age", (550, 551, 900))
def test_financial_scoring_uses_original_disclosure_age_and_keeps_latest_loss_independent(age):
    observed_at = datetime.fromisoformat("2024-12-31T14:50:00+08:00") + timedelta(days=age)
    report = FinancialReport(
        date(2024, 12, 31), observed_at - timedelta(days=age), 1, 5, 20, 10, 10, 10, -3, 2,
    )
    observation = ResearchObservation(
        financial=report, financial_history=(report,), snapshot_at=observed_at, loaded_at=observed_at,
        source_errors=("financial_scoring:report_stale",) if age > 550 else (),
        corporate_risk_facts=(CorporateRiskFact(
            CorporateRiskCategory.FINANCIAL_FRAUD, observed_at - timedelta(days=900), "fraud", "issuer_disclosure", persistent=True,
        ),),
    )
    features = DeriveLongResearchFeatures(
        observation, LongResearchInputs(20, 80, 70, 80), _long_policy(), _feature_component_weights(), observed_at=observed_at,
    )
    for field in ("value_score", "growth_score", "quality_score", "financial_deterioration"):
        assert (features[field] is not None) is (age <= 550)
    assert DeriveLatestDisclosedReportLoss(observation, observed_at) == 1.0
    positive = replace(report, parent_net_profit=3)
    assert DeriveLatestDisclosedReportLoss(replace(observation, financial_history=(positive,), financial=positive), observed_at) == 0.0
    assert ResearchComponentsFromObservation(observation, observed_at).financial == ("stale" if age > 550 else "known_clear")
    assert DeriveCorporateRiskFeatures(observation.corporate_risk_facts, observed_at, window_complete=True)["financial_fraud_history"] == 1


@pytest.mark.parametrize(("maximum_age_days", "report_age", "fresh"), (
    (550, 550, True), (550, 551, False), (30, 30, True), (30, 31, False),
))
def test_recent_restatement_does_not_reset_financial_report_period_age(maximum_age_days, report_age, fresh):
    observed_at = datetime.fromisoformat("2024-12-31T14:50:00+08:00") + timedelta(days=report_age)
    report = FinancialReport(
        date(2024, 12, 31), observed_at - timedelta(days=1), 1, 5, 20, 10, 10, 10, -3, 2,
    )
    assert FinancialReportIsFresh(report, observed_at, maximum_age_days=maximum_age_days) is fresh
    observation = ResearchObservation(financial=report, financial_history=(report,))
    features = DeriveLongResearchFeatures(
        observation, LongResearchInputs(20, 80, 70, 80),
        replace(_long_policy(), financial_max_age_days=maximum_age_days), _feature_component_weights(), observed_at=observed_at,
    )
    for field in ("value_score", "growth_score", "quality_score", "financial_deterioration"):
        assert (features[field] is not None) is fresh
    assert DeriveLatestDisclosedReportLoss(observation, observed_at) == 1.0


def test_financial_freshness_uses_shanghai_dates_and_preserves_missing_and_future_guards():
    observed = datetime.fromisoformat("2026-07-16T00:30:00+08:00")
    report = FinancialReport(observed.date() - timedelta(days=550), observed - timedelta(days=1))
    utc = datetime.fromisoformat("2026-07-15T16:30:00+00:00")
    assert FinancialReportIsFresh(report, observed, maximum_age_days=550)
    assert FinancialReportIsFresh(replace(report, published_at=report.published_at.astimezone(utc.tzinfo)), utc,
                                  maximum_age_days=550)
    assert not FinancialReportIsFresh(replace(report, report_date=report.report_date - timedelta(days=1)), utc,
                                      maximum_age_days=550)
    assert not FinancialReportIsFresh(None, observed, maximum_age_days=550)
    assert not FinancialReportIsFresh(replace(report, published_at=observed + timedelta(seconds=1)), observed,
                                      maximum_age_days=550)
    assert not FinancialReportIsFresh(replace(report, report_date=observed.date() + timedelta(days=1)), observed,
                                      maximum_age_days=550)
    with pytest.raises(ValueError, match="financial_freshness_context_invalid"):
        FinancialReportIsFresh(report, observed.replace(tzinfo=None), maximum_age_days=550)


def test_long_research_features_use_point_in_time_financial_and_event_inputs() -> None:
    observation = ResearchObservation(
        financial=FinancialReport(
            report_date=date(2026, 3, 31),
            published_at=datetime.fromisoformat("2026-04-30T23:59:59+08:00"),
            basic_eps=1.0,
            book_value_per_share=10.0,
            revenue_growth_pct=20.0,
            net_profit_growth_pct=10.0,
            core_profit_growth_pct=0.0,
            roe_pct=3.0,
            parent_net_profit=100.0,
            core_net_profit=80.0,
        ),
        announcements=(
            ResearchAnnouncement("公司获得政策支持并获批新项目", NOW),
            ResearchAnnouncement("控股股东减持并收到监管函", NOW),
        ),
        announcements_available=True,
        pledge_ratio_pct=15.0,
        unlock_ratio_pct=6.0,
    )

    features = _derive_long_research_features(
        observation,
        price=20.0,
        industry_strength=80.0,
        low_volatility_score=70.0,
        low_drawdown_score=80.0,
        policy=_long_policy(),
    )

    assert features["value_score"] == pytest.approx(92.8571428571)
    assert features["growth_score"] == pytest.approx(70.0)
    assert features["quality_score"] == pytest.approx(67.5)
    assert features["industry_policy_score"] == pytest.approx(68.0)
    assert features["risk_protection_score"] == pytest.approx(75.0)
    assert features["financial_deterioration"] == 0.0
    assert features["negative_announcement_level"] == 0.0
    assert features["pledge_risk"] == 1.0
    assert features["reduction_or_unlock"] == 2.0


def test_long_research_missing_sources_stay_missing_instead_of_becoming_zero() -> None:
    features = _derive_long_research_features(
        ResearchObservation(),
        price=20.0,
        industry_strength=80.0,
        low_volatility_score=70.0,
        low_drawdown_score=80.0,
        policy=_long_policy(),
    )

    assert features["value_score"] is None
    assert features["growth_score"] is None
    assert features["quality_score"] is None
    assert features["industry_policy_score"] is None
    assert features["financial_deterioration"] is None
    assert features["negative_announcement_level"] is None
    assert features["pledge_risk"] is None
    assert features["reduction_or_unlock"] is None
    assert features["risk_protection_score"] == pytest.approx(75.0)


def test_long_research_composite_scores_consume_explicit_weight_policy() -> None:
    observation = ResearchObservation(
        announcements=(ResearchAnnouncement("公司获得政策支持", NOW),),
        announcements_available=True,
    )
    inputs = LongResearchInputs(20.0, 80.0, 60.0, 100.0)
    baseline = DeriveLongResearchFeatures(observation, inputs, _long_policy(), _feature_component_weights(), observed_at=NOW)
    changed = DeriveLongResearchFeatures(
        observation,
        inputs,
        _long_policy(),
        FeatureComponentWeightPolicy(
            trend_score={"ma20_60_position": 0.7, "ma_slope": 0.3},
            industry_policy_score={"industry_strength": 0.75, "evidence_score": 0.25},
            risk_protection_score={"low_volatility_score": 0.25, "low_drawdown_score": 0.75},
        ),
        observed_at=NOW,
    )

    assert baseline["industry_policy_score"] == 68.0
    assert changed["industry_policy_score"] == 72.5
    assert baseline["risk_protection_score"] == 80.0
    assert changed["risk_protection_score"] == 90.0


def test_successful_empty_event_sources_are_auditable_real_zeroes() -> None:
    features = _derive_long_research_features(
        ResearchObservation(
            announcements_available=True,
            pledge_ratio_pct=0.0,
            unlock_ratio_pct=0.0,
        ),
        price=20.0,
        industry_strength=80.0,
        low_volatility_score=70.0,
        low_drawdown_score=80.0,
        policy=_long_policy(),
    )

    assert features["negative_announcement_level"] == 0.0
    assert features["pledge_risk"] == 0.0
    assert features["reduction_or_unlock"] == 0.0
    assert features["industry_policy_score"] == pytest.approx(68.0)


@pytest.mark.parametrize(
    ("report_month", "expected_value_score"),
    ((3, 100.0), (6, 75.0), (9, 50.0), (12, 25.0)),
)
def test_long_value_score_uses_configured_quarter_annualizers(
    report_month: int,
    expected_value_score: float,
) -> None:
    features = _derive_long_research_features(
        ResearchObservation(
            financial=FinancialReport(
                report_date=date(2025, report_month, 1),
                published_at=NOW,
                basic_eps=1.0,
            )
        ),
        price=40.0,
        industry_strength=None,
        low_volatility_score=None,
        low_drawdown_score=None,
        policy=_long_policy(),
    )

    assert features["value_score"] == pytest.approx(expected_value_score)


@pytest.mark.parametrize(
    ("pledge_ratio", "unlock_ratio", "expected_pledge", "expected_unlock"),
    (
        (9.999, 0.999, 0.0, 0.0),
        (10.0, 1.0, 1.0, 1.0),
        (20.0, 5.0, 2.0, 2.0),
        (35.0, 10.0, 3.0, 3.0),
    ),
)
def test_long_event_risk_levels_use_exact_configured_boundaries(
    pledge_ratio: float,
    unlock_ratio: float,
    expected_pledge: float,
    expected_unlock: float,
) -> None:
    features = _derive_long_research_features(
        ResearchObservation(
            announcements_available=True,
            pledge_ratio_pct=pledge_ratio,
            unlock_ratio_pct=unlock_ratio,
        ),
        price=None,
        industry_strength=None,
        low_volatility_score=None,
        low_drawdown_score=None,
        policy=_long_policy(),
    )

    assert features["pledge_risk"] == expected_pledge
    assert features["reduction_or_unlock"] == expected_unlock


def test_corporate_risk_window_does_not_keep_old_permanent_or_three_year_rules() -> None:
    facts = (
        CorporateRiskFact(
            category=CorporateRiskCategory.FINANCIAL_FRAUD,
            announced_at=NOW - timedelta(days=4000),
            evidence_id="official:fraud",
            source="exchange_disclosure",
        ),
        CorporateRiskFact(
            category=CorporateRiskCategory.OFFICIAL_INVESTIGATION,
            announced_at=NOW - timedelta(days=1500),
            resolved_at=NOW.replace(year=2023),
            evidence_id="official:investigation",
            source="regulator_disclosure",
        ),
        CorporateRiskFact(
            category=CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION,
            announced_at=NOW - timedelta(days=100),
            resolved_at=NOW - timedelta(days=90),
            evidence_id="official:reduction",
            source="exchange_disclosure",
        ),
    )

    features = DeriveCorporateRiskFeatures(facts, NOW, window_complete=False)

    assert features["financial_fraud_history"] == 0.0
    assert features["official_investigation_history"] == 0.0
    assert features["major_shareholder_reduction"] == 0.0
    assert features["corporate_risk_window_unavailable"] == 1.0


def test_current_announcement_coverage_does_not_claim_regulatory_history() -> None:
    observation = ResearchObservation(announcements_available=True, corporate_risk_window_complete=False)

    assert ResearchComponentsFromObservation(observation).announcements == "known_clear"
    assert DeriveCorporateRiskFeatures((), NOW, window_complete=False)["corporate_risk_window_unavailable"] == 1.0


def test_effective_reduction_and_unresolved_investigation_are_hard_risk_facts() -> None:
    facts = (
        CorporateRiskFact(
            category=CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION,
            announced_at=NOW,
            evidence_id="official:reduction",
            source="exchange_disclosure",
        ),
        CorporateRiskFact(
            category=CorporateRiskCategory.OFFICIAL_INVESTIGATION,
            announced_at=NOW,
            evidence_id="official:investigation",
            source="regulator_disclosure",
        ),
    )

    features = DeriveCorporateRiskFeatures(facts, NOW, window_complete=True)

    assert features["major_shareholder_reduction"] == 1.0
    assert features["official_investigation_history"] == 1.0
    assert features["corporate_risk_window_unavailable"] == 0.0


def test_titles_alone_never_create_hard_risks_and_official_bodies_are_required() -> None:
    announcements = (
        ResearchAnnouncement("关于收到问询函及普通诉讼的公告", NOW, "official:generic"),
        ResearchAnnouncement(
            "控股股东拟减持股份的计划公告",
            NOW,
            "official:reduction",
            body="本公司控股股东拟减持股份，详见减持计划。",
            body_checked=True,
        ),
        ResearchAnnouncement(
            "关于收到行政处罚决定书确认财务造假的公告",
            NOW,
            "official:fraud",
            "exchange_disclosure",
            "本公司收到证监会行政处罚决定书，确认财务造假。",
            True,
        ),
        ResearchAnnouncement("公司不存在资金占用及违规担保的说明", NOW, "official:negative"),
    )

    facts = _extract(announcements).risks

    assert {fact.risk_type for fact in facts} == {
        CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION,
        CompanyRiskType.FINANCIAL_FRAUD,
    }
    title_only = ResearchAnnouncement("关于收到立案告知书的公告", NOW, "unverified")
    assert _extract((title_only,)).reviews
    assert not _extract((title_only,)).risks


def test_resolution_title_does_not_create_and_resolve_a_duplicate_risk_cycle() -> None:
    announcements = (
        ResearchAnnouncement(
            "控股股东拟减持股份的计划公告",
            NOW - timedelta(days=10),
            "official:reduction-plan",
            body="本公司控股股东拟减持股份，减持计划〔2026〕减持1号。",
            body_checked=True,
        ),
        ResearchAnnouncement(
            "关于控股股东减持计划实施进展的公告",
            NOW - timedelta(days=5),
            "official:reduction-progress",
        ),
        ResearchAnnouncement(
            "关于控股股东提前终止减持计划的公告",
            NOW,
            "official:reduction-end",
            body="本公司控股股东提前终止减持计划，涉及减持计划〔2026〕减持1号。",
            body_checked=True,
        ),
        ResearchAnnouncement(
            "关于收到立案调查告知书的公告",
            NOW - timedelta(days=20),
            "official:investigation-start",
            body="本公司收到证监会立案告知书〔2026〕调查1号。",
            body_checked=True,
        ),
        ResearchAnnouncement(
            "关于立案调查进展的公告",
            NOW - timedelta(days=5),
            "official:investigation-progress",
        ),
        ResearchAnnouncement(
            "关于立案调查终结的公告",
            NOW,
            "official:investigation-end",
            body="本公司收到证监会〔2026〕调查1号通知，立案调查终结。",
            body_checked=True,
        ),
    )

    facts = _extract(announcements).risks

    assert len(facts) == 2
    assert all(fact.resolved_at == NOW for fact in facts)


def test_closure_cannot_resolve_an_unrelated_case_and_negation_cannot_create_risk() -> None:
    opened = ResearchAnnouncement(
        "收到立案告知书",
        NOW - timedelta(days=1),
        "case-a",
        body="本公司收到证监会立案告知书〔2026〕调查1号。",
        body_checked=True,
    )
    unrelated = ResearchAnnouncement(
        "立案调查终结", NOW, "case-b", body="本公司收到证监会〔2026〕调查2号，立案调查终结。", body_checked=True
    )
    facts = _extract((opened, unrelated)).risks
    assert len(facts) == 1 and facts[0].resolved_at is None
    negated = ResearchAnnouncement(
        "公司不存在财务造假的说明", NOW, "negated", body="本公司不存在财务造假。", body_checked=True
    )
    assert not _extract((negated,)).risks


def test_investigation_ends_without_three_year_cooldown_and_window_has_exact_boundary() -> None:
    fact = CorporateRiskFact(
        CorporateRiskCategory.OFFICIAL_INVESTIGATION, NOW - timedelta(days=365), "case", "issuer_disclosure"
    )
    assert DeriveCorporateRiskFeatures((fact,), NOW, window_complete=True)["official_investigation_history"] == 1
    assert (
        DeriveCorporateRiskFeatures((fact,), NOW + timedelta(microseconds=1), window_complete=True)[
            "official_investigation_history"
        ]
        == 0
    )
    from dataclasses import replace

    resolved = replace(fact, resolved_at=NOW)
    assert DeriveCorporateRiskFeatures((resolved,), NOW, window_complete=True)["official_investigation_history"] == 0


def _derive_long_research_features(
    observation: ResearchObservation,
    *,
    price: float | None,
    industry_strength: float | None,
    low_volatility_score: float | None,
    low_drawdown_score: float | None,
    policy: LongResearchPolicy,
) -> dict[str, float | None]:
    return DeriveLongResearchFeatures(
        observation,
        LongResearchInputs(
            price=price,
            industry_strength=industry_strength,
            low_volatility_score=low_volatility_score,
            low_drawdown_score=low_drawdown_score,
        ),
        policy,
        _feature_component_weights(),
        observed_at=NOW,
    )


def _feature_component_weights() -> FeatureComponentWeightPolicy:
    return FeatureComponentWeightPolicy(
        trend_score={"ma20_60_position": 0.6, "ma_slope": 0.4},
        industry_policy_score={"industry_strength": 0.6, "evidence_score": 0.4},
        risk_protection_score={"low_volatility_score": 0.5, "low_drawdown_score": 0.5},
    )


def _long_policy() -> LongResearchPolicy:
    return LongResearchPolicy(
        financial_max_age_days=550,
        pe_full_score_max=10.0,
        pe_zero_score_min=50.0,
        pb_full_score_max=1.0,
        pb_zero_score_min=8.0,
        growth_points_per_pct=2.0,
        quality_roe_neutral_pct=10.0,
        quality_roe_points_per_pct=2.5,
        financial_revenue_deterioration_pct=-10.0,
        financial_profit_deterioration_pct=-20.0,
        financial_core_profit_deterioration_pct=-20.0,
        pledge_thresholds=(10.0, 20.0, 35.0),
        unlock_thresholds=(1.0, 5.0, 10.0),
    )

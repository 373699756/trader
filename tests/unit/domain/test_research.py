from __future__ import annotations

from datetime import date, datetime

import pytest

from trader.domain.market.research import (
    D25SignalPolicy,
    FinancialReport,
    LongResearchInputs,
    LongResearchPolicy,
    ResearchAnnouncement,
    ResearchObservation,
    derive_d25_signals,
    derive_long_research_features,
)

NOW = datetime.fromisoformat("2026-07-16T14:50:00+08:00")


def test_d25_signals_apply_exact_configured_boundaries() -> None:
    policy = _d25_policy()

    at_full = derive_d25_signals(15.0, 60.0, policy)
    at_linear_end = derive_d25_signals(30.0, 40.0, policy)
    above_linear_end = derive_d25_signals(30.01, None, policy)

    assert at_full.market_regime == "risk_on"
    assert at_full.market_regime_factor == pytest.approx(1.03)
    assert at_full.overheat_factor == pytest.approx(1.0)
    assert at_full.not_overheated_score == pytest.approx(100.0)
    assert at_linear_end.market_regime == "risk_off"
    assert at_linear_end.market_regime_factor == pytest.approx(0.92)
    assert at_linear_end.overheat_factor == pytest.approx(0.85)
    assert at_linear_end.not_overheated_score == pytest.approx(0.0)
    assert above_linear_end.market_regime == "neutral"
    assert above_linear_end.market_regime_factor == pytest.approx(1.0)
    assert above_linear_end.overheat_factor == pytest.approx(0.75)


def test_d25_signals_interpolate_between_boundaries_and_preserve_missing() -> None:
    policy = _d25_policy()

    midpoint = derive_d25_signals(22.5, 50.0, policy)
    missing = derive_d25_signals(None, None, policy)

    assert midpoint.market_regime == "neutral"
    assert midpoint.overheat_factor == pytest.approx(0.925)
    assert midpoint.not_overheated_score == pytest.approx(50.0)
    assert missing.market_regime_factor == pytest.approx(1.0)
    assert missing.overheat_factor == pytest.approx(1.0)
    assert missing.not_overheated_score is None


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
    assert features["industry_policy_score"] == pytest.approx(76.0)
    assert features["risk_protection_score"] == pytest.approx(75.0)
    assert features["financial_deterioration"] == 0.0
    assert features["negative_announcement_level"] == 2.0
    assert features["pledge_risk"] == 1.0
    assert features["reduction_or_unlock"] == 3.0


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
                report_date=date(2026, report_month, 1),
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


def _derive_long_research_features(
    observation: ResearchObservation,
    *,
    price: float | None,
    industry_strength: float | None,
    low_volatility_score: float | None,
    low_drawdown_score: float | None,
    policy: LongResearchPolicy,
) -> dict[str, float | None]:
    return derive_long_research_features(
        observation,
        LongResearchInputs(
            price=price,
            industry_strength=industry_strength,
            low_volatility_score=low_volatility_score,
            low_drawdown_score=low_drawdown_score,
        ),
        policy,
    )


def _d25_policy() -> D25SignalPolicy:
    return D25SignalPolicy(
        overheat_full_return_max=15.0,
        overheat_linear_return_max=30.0,
        overheat_linear_end_factor=0.85,
        overheat_above_factor=0.75,
        risk_on_breadth_min=60.0,
        risk_off_breadth_max=40.0,
        risk_on_factor=1.03,
        neutral_factor=1.0,
        risk_off_factor=0.92,
    )


def _long_policy() -> LongResearchPolicy:
    return LongResearchPolicy(
        financial_max_age_days=550,
        announcement_lookback_days=180,
        announcement_limit=100,
        unlock_forward_days=90,
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
        policy_keyword_score_step=10.0,
        negative_high_keywords=("立案", "行政处罚", "重大违法", "强制退市", "终止上市", "债务违约"),
        negative_medium_keywords=("问询函", "监管函", "诉讼", "仲裁", "业绩预亏", "预亏", "资金占用", "股份冻结"),
        negative_low_keywords=("风险提示", "业绩下滑", "终止", "延期回复"),
        reduction_high_keywords=("控股股东减持", "实际控制人减持"),
        reduction_medium_keywords=("减持计划", "拟减持", "集中竞价减持", "大宗交易减持"),
        reduction_low_keywords=("减持完成", "减持结果", "减持进展"),
        policy_positive_keywords=(
            "政策支持",
            "专项资金",
            "政府补助",
            "产业扶持",
            "获批",
            "中标",
            "战略合作",
            "技术突破",
            "回购",
            "增持",
        ),
        policy_negative_keywords=("行业监管", "限产", "禁售", "取消补贴", "产能过剩"),
    )

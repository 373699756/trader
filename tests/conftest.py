from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.domain.fusion import DIMENSION_NAMES, FusionPolicy
from trader.domain.models import FeatureSnapshot, MarketQuote, RiskRule, Strategy


@pytest.fixture
def recommendation_policy() -> RecommendationPolicy:
    risk_rules = {
        "near_limit_crowding": RiskRule("near_limit_crowding", "medium", 5.0, 0.7, "market_crowding"),
        "price_volume_divergence": RiskRule("price_volume_divergence", "medium", 4.0, 0.7, "market_structure"),
        "high_volatility": RiskRule("high_volatility", "low", 3.0, 0.7, "market_structure"),
        "financial_deterioration": RiskRule("financial_deterioration", "high", 6.0, 0.7, "financial"),
    }
    return RecommendationPolicy(
        strategy_version="strategy-v6",
        fusion_version="fusion-v2",
        fusion=FusionPolicy(),
        selection=SelectionPolicy(
            default_top_k=10,
            maximum_top_k=18,
            maximum_per_industry=3,
            observation_margin=5.0,
            thresholds={"today_main": 70.0, "today_late": 76.0, "tomorrow": 72.0, "d25": 70.0},
        ),
        candidate_weights={
            "liquidity": 0.35,
            "short_momentum": 0.25,
            "trend": 0.20,
            "industry_strength": 0.10,
            "data_completeness": 0.10,
        },
        dimension_weights={strategy: {name: 0.2 for name in DIMENSION_NAMES} for strategy in Strategy},
        local_strategy_weights={
            Strategy.TODAY: {
                "momentum": 0.35,
                "liquidity": 0.25,
                "industry": 0.10,
                "sentiment": 0.20,
                "protection": 0.10,
            },
            Strategy.TOMORROW: {
                "liquidity": 0.25,
                "momentum": 0.15,
                "trend": 0.20,
                "historical_edge": 0.15,
                "execution": 0.10,
                "tail_structure": 0.15,
            },
            Strategy.D25: {
                "momentum": 0.30,
                "trend": 0.25,
                "liquidity": 0.20,
                "execution": 0.15,
                "not_overheated": 0.10,
            },
            Strategy.LONG: {
                "value": 0.30,
                "growth": 0.25,
                "quality": 0.20,
                "industry_policy": 0.15,
                "protection": 0.10,
            },
        },
        risk_rules=risk_rules,
    )


@pytest.fixture
def application_feature_factory():
    def build(code: str, observed_at: datetime, *, industry: str = "工业") -> FeatureSnapshot:
        quote = MarketQuote(
            code=code,
            name=f"测试{code}",
            price=12.0,
            previous_close=11.65,
            open_price=11.8,
            high=12.2,
            low=11.7,
            pct_change=3.0,
            change_5m=1.0,
            speed=0.8,
            volume_ratio=2.0,
            turnover_rate=3.0,
            amount=300_000_000.0,
            amplitude=4.0,
            market_cap=30_000_000_000.0,
            industry=industry,
            source="fixture",
            source_time=observed_at,
            received_time=observed_at,
            data_version=f"fixture:{observed_at.isoformat()}",
        )
        values = {
            "amount_median_20d": 200_000_000.0,
            "amount_percentile_20d": 75.0,
            "speed_percentile": 75.0,
            "relative_strength_3d": 75.0,
            "relative_strength_5d": 75.0,
            "relative_strength_10d": 70.0,
            "relative_strength_20d": 70.0,
            "industry_strength": 70.0,
            "industry_breadth": 70.0,
            "industry_trend": 70.0,
            "news_sentiment": 60.0,
            "evidence_freshness": 70.0,
            "market_breadth": 60.0,
            "low_volatility_score": 70.0,
            "low_drawdown_score": 70.0,
            "low_crowding_score": 70.0,
            "volatility_20d": 2.0,
            "max_drawdown_20d": -8.0,
            "price_volume_confirmation": 70.0,
            "moderate_daily_return": 75.0,
            "ma20_60_position": 75.0,
            "ma20_60_structure": 75.0,
            "ma_slope": 70.0,
            "breakout_20d": 65.0,
            "risk_adjusted_return_20d": 70.0,
            "upward_consistency": 70.0,
            "capacity_score": 80.0,
            "moderate_amplitude": 75.0,
            "limit_distance_safety": 75.0,
            "tail_return_30m_pct": 0.8,
            "tail_return_30m": 70.0,
            "tail_volume_ratio_raw": 1.4,
            "tail_volume_ratio": 70.0,
            "close_location": 75.0,
            "price_executability": 75.0,
            "ma20_deviation_inverse": 70.0,
            "return_20d_not_overheated": 70.0,
            "return_20d": 10.0,
            "trend_score": 70.0,
            "value_score": 70.0,
            "growth_score": 70.0,
            "quality_score": 70.0,
            "industry_policy_score": 70.0,
            "risk_protection_score": 70.0,
            "limit_proximity": 0.3,
            "price_volume_divergence": 0.0,
            "financial_deterioration": 0.0,
            "reduction_or_unlock": 0.0,
            "shareholder_reduction_level": 0.0,
            "unlock_risk": 0.0,
            "pledge_risk": 0.0,
            "negative_announcement_level": 0.0,
        }
        return FeatureSnapshot(quote=quote, values=values, observed_at=observed_at, history_days=60)

    return build


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)

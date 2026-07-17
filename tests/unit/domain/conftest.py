from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.models import FeatureSnapshot, MarketQuote


@pytest.fixture
def observed_at() -> datetime:
    return datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def feature_factory(observed_at):
    def build(
        *,
        code: str = "600001",
        pct_change: float = 3.0,
        industry: str = "工业",
        values: dict[str, float | None] | None = None,
        **quote_overrides,
    ) -> FeatureSnapshot:
        quote_values = {
            "code": code,
            "name": "测试股份",
            "price": 12.0,
            "previous_close": 11.65,
            "open_price": 11.8,
            "high": 12.2,
            "low": 11.7,
            "pct_change": pct_change,
            "change_5m": 1.0,
            "speed": 0.8,
            "volume_ratio": 2.0,
            "turnover_rate": 3.0,
            "amount": 300_000_000.0,
            "amplitude": 4.0,
            "market_cap": 30_000_000_000.0,
            "industry": industry,
            "source": "fixture",
            "source_time": observed_at,
            "received_time": observed_at,
            "data_version": "fixture-v1",
        }
        quote_values.update(quote_overrides)
        base_values = {
            "amount_median_20d": 200_000_000.0,
            "amount_percentile_20d": 70.0,
            "speed_percentile": 70.0,
            "relative_strength_3d": 65.0,
            "relative_strength_5d": 65.0,
            "relative_strength_10d": 60.0,
            "relative_strength_20d": 60.0,
            "industry_strength": 60.0,
            "industry_breadth": 60.0,
            "industry_trend": 60.0,
            "news_sentiment": 55.0,
            "evidence_freshness": 70.0,
            "market_breadth": 55.0,
            "low_volatility_score": 65.0,
            "low_drawdown_score": 70.0,
            "low_crowding_score": 65.0,
            "volatility_20d": 2.0,
            "max_drawdown_20d": -8.0,
            "price_volume_confirmation": 65.0,
            "moderate_daily_return": 70.0,
            "ma20_60_position": 65.0,
            "ma20_60_structure": 65.0,
            "ma_slope": 60.0,
            "breakout_20d": 55.0,
            "risk_adjusted_return_20d": 60.0,
            "upward_consistency": 65.0,
            "capacity_score": 75.0,
            "moderate_amplitude": 70.0,
            "limit_distance_safety": 70.0,
            "tail_return_30m_pct": 0.4,
            "tail_return_30m": 60.0,
            "tail_volume_ratio_raw": 1.2,
            "tail_volume_ratio": 60.0,
            "close_location": 70.0,
            "price_executability": 70.0,
            "ma20_deviation_inverse": 60.0,
            "return_20d_not_overheated": 65.0,
            "return_20d": 10.0,
            "d25_overheat_factor": 1.0,
            "market_regime_factor": 1.0,
            "trend_score": 60.0,
            "value_score": 60.0,
            "growth_score": 65.0,
            "quality_score": 70.0,
            "industry_policy_score": 55.0,
            "risk_protection_score": 70.0,
        }
        base_values.update(values or {})
        return FeatureSnapshot(
            quote=MarketQuote(**quote_values),
            values=base_values,
            observed_at=observed_at,
            history_days=60,
        )

    return build

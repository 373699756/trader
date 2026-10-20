from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.infra.market_data.history.history import (
    BuildHistoryContext,
    DailyBar,
    PriceAdjustment,
    SummarizeHistoryMetrics,
)
from trader.infra.settings import LoadStrategySettings
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.risk.downside import DeriveEntrySetup
from trader.recommendation.infra.normalization.features import FeatureBuilder

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 16, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_entry_inputs_exclude_same_day_history_bar() -> None:
    settings = LoadStrategySettings(ROOT / "config/strategy.json")
    builder = FeatureBuilder(
        settings.news_signal,
        settings.tomorrow_tail_signal,
        settings.market_regime,
        settings.long_research,
        settings.feature_component_weights,
    )
    prior = tuple(
        DailyBar(
            trade_date=f"2026-06-{index:02d}",
            open_price=9.0 + index / 10,
            close=9.1 + index / 10,
            high=9.2 + index / 10,
            low=8.9 + index / 10,
            volume=1_000.0,
            amount=10_000.0,
            pct_change=1.0,
            turnover_rate=2.0,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for index in range(1, 26)
    )
    same_day = DailyBar(
        "2026-07-16",
        12.0,
        12.0,
        99.0,
        1.0,
        50_000.0,
        600_000.0,
        0.0,
        2.0,
        adjustment=PriceAdjustment.QFQ,
        source="fixture",
    )
    bars = (*prior, same_day)
    quote = MarketQuote(
        code="600001",
        name="测试股份",
        price=12.0,
        previous_close=11.9,
        open_price=11.9,
        high=12.1,
        low=11.8,
        pct_change=0.84,
        change_5m=0.1,
        speed=0.1,
        volume_ratio=0.5,
        turnover_rate=2.0,
        amount=100_000_000.0,
        volume=500.0,
        amplitude=2.5,
        market_cap=10_000_000_000.0,
        industry="工业",
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version="fixture-current",
    )

    feature = builder.build(
        (quote,),
        {quote.code: bars},
        NOW,
        history_summaries={quote.code: BuildHistoryContext(bars)},
    )[0]

    assert feature.values["prior_high_20d"] == max(bar.high for bar in prior[-20:])
    assert feature.values["atr20_pct"] != SummarizeHistoryMetrics(bars).atr20_pct
    assert feature.values["volume_to_5d_average"] == 0.5
    uncached = builder.build((quote,), {quote.code: bars}, NOW)[0]
    assert uncached.values == feature.values


@pytest.mark.parametrize("volume", [1_000.0, None, -1.0, float("nan"), float("inf")])
@pytest.mark.parametrize("vendor_ratio", [None, 0.5])
def test_entry_quality_requires_actual_volume_without_amount_or_vendor_ratio_substitution(
    volume: float | None, vendor_ratio: float | None
) -> None:
    settings = LoadStrategySettings(ROOT / "config/strategy.json")
    builder = FeatureBuilder(
        settings.news_signal,
        settings.tomorrow_tail_signal,
        settings.market_regime,
        settings.long_research,
        settings.feature_component_weights,
    )
    bars = tuple(
        DailyBar(
            trade_date=f"2026-06-{index:02d}",
            open_price=9.0 + index / 100,
            close=9.0 + index / 100,
            high=9.1 + index / 100,
            low=8.9 + index / 100,
            volume=1_000.0,
            amount=10_000.0,
            pct_change=0.5,
            turnover_rate=2.0,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for index in range(1, 26)
    )
    quote = MarketQuote(
        code="600001",
        name="测试股份",
        price=9.25,
        previous_close=9.24,
        open_price=9.20,
        high=9.30,
        low=9.10,
        pct_change=0.11,
        change_5m=0.1,
        speed=0.1,
        volume_ratio=vendor_ratio,
        turnover_rate=2.0,
        amount=10_000.0,
        volume=volume,
        amplitude=2.1,
        market_cap=10_000_000_000.0,
        industry="工业",
        source="sina",
        source_time=NOW,
        received_time=NOW,
        data_version="sina-current",
    )

    feature = builder.build((quote,), {quote.code: bars}, NOW)[0]

    if volume == 1_000.0:
        assert feature.values["volume_to_5d_average"] == 1.0
        assert feature.values["entry_quality"] == 50.0
        assert "entry_quality" not in feature.missing_fields
        repriced = replace(quote, price=18.5, amount=20_000.0, volume_ratio=3.0)
        changed = builder.build((repriced,), {quote.code: bars}, NOW)[0]
        assert changed.values["volume_to_5d_average"] == 1.0
        for unavailable_history in (bars[:4], tuple(replace(bar, volume=0.0) for bar in bars)):
            unavailable = builder.build((quote,), {quote.code: unavailable_history}, NOW)[0]
            assert unavailable.values["volume_to_5d_average"] is None
            assert unavailable.values["entry_quality"] is None
    else:
        assert feature.values["volume_to_5d_average"] is None
        assert feature.values["entry_quality"] is None
        assert "entry_quality" in feature.missing_fields
        assert DeriveEntrySetup(feature).setup_type == "none"


@pytest.mark.parametrize(
    ("volumes", "expected"),
    [
        ((1_000.0,) * 5, 1_000.0),
        ((1_000.0,) * 4 + (0.0,), 800.0),
        ((0.0,) * 5, 0.0),
        ((1_000.0,) * 4, None),
        ((1_000.0,) * 4 + (-1.0,), None),
        ((1_000.0,) * 4 + (float("nan"),), None),
        ((1_000.0,) * 4 + (float("inf"),), None),
    ],
)
def test_five_session_volume_mean_keeps_real_zero_and_rejects_incomplete_or_invalid_window(
    volumes: tuple[float, ...], expected: float | None
) -> None:
    bars = tuple(
        DailyBar(
            f"2026-07-{index + 1:02d}",
            10.0,
            10.0,
            11.0,
            9.0,
            volume,
            10_000.0,
            0.0,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for index, volume in enumerate(volumes)
    )
    assert SummarizeHistoryMetrics(bars).average_volume_5d == expected

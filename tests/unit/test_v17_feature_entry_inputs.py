from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.domain.models import MarketQuote
from trader.infra.market_data.features import FeatureBuilder
from trader.infra.market_data.history import DailyBar, summarize_history_metrics
from trader.infra.settings import load_strategy_settings

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 16, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_entry_inputs_exclude_same_day_history_bar() -> None:
    settings = load_strategy_settings(ROOT / "config/v2/strategy.json")
    builder = FeatureBuilder(
        settings.today_news_signal,
        settings.tomorrow_tail_signal,
        settings.d25_signal,
        settings.long_research,
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
        )
        for index in range(1, 26)
    )
    same_day = DailyBar("2026-07-16", 12.0, 12.0, 99.0, 1.0, 50_000.0, 600_000.0, 0.0, 2.0)
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
        amplitude=2.5,
        market_cap=10_000_000_000.0,
        industry="工业",
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version="fixture-v17",
    )

    feature = builder.build(
        (quote,),
        {quote.code: bars},
        NOW,
        history_summaries={quote.code: summarize_history_metrics(bars)},
    )[0]

    assert feature.values["prior_high_20d"] == max(bar.high for bar in prior[-20:])
    assert feature.values["atr20_pct"] != summarize_history_metrics(bars).atr20_pct

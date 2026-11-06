from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from trader.recommendation.domain.market.tail import MinuteBar, TailSignalPolicy

SHANGHAI = ZoneInfo("Asia/Shanghai")

POLICY = TailSignalPolicy(
    lookback_minutes=30,
    minimum_baseline_minutes=30,
    return_score_points_per_pct=25.0,
    volume_score_points_per_ratio=50.0,
)


def _minute_bars(*, latest_price: float = 10.2, tail_volume: float = 150.0) -> tuple[MinuteBar, ...]:
    start = datetime(2026, 7, 16, 9, 31, tzinfo=SHANGHAI)
    return tuple(
        MinuteBar(
            source_time=start + timedelta(minutes=index),
            close=latest_price if index == 60 else 10.0,
            volume=tail_volume if index >= 31 else 100.0,
            source="eastmoney_intraday",
            received_time=start + timedelta(minutes=60),
            data_version="intraday",
        )
        for index in range(61)
    )

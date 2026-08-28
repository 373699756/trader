from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.tomorrow_features import (
    DailyFeaturePoint,
    IntradayFeaturePoint,
    PointInTimePublishedFact,
    TomorrowFeatureStockInput,
    build_tomorrow_stock_features,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 28)
AS_OF = datetime.combine(TRADE_DATE, time(14, 50), tzinfo=SHANGHAI)


def _stock(index: int, *, history_days: int = 70) -> TomorrowFeatureStockInput:
    closes = tuple(
        DailyFeaturePoint(
            session_date=TRADE_DATE - timedelta(days=history_days - offset),
            close=10.0 + index * 0.2 + offset * (0.01 + index * 0.002),
            amount=10_000_000.0 + index * 1_000_000.0 + offset * 10_000.0,
        )
        for offset in range(history_days)
    )
    intraday = tuple(
        IntradayFeaturePoint(
            observed_at=datetime.combine(TRADE_DATE, minute, tzinfo=SHANGHAI),
            close=11.0 + index * 0.1 + position * 0.02,
            amount=1_000_000.0 + position * 100_000.0,
        )
        for position, minute in enumerate((time(9, 30), time(11, 30), time(13, 0), time(14, 20), time(14, 50)))
    )
    facts = (
        PointInTimePublishedFact(
            kind="financial",
            name="earnings_revision",
            value=0.1 * index,
            report_period=date(2026, 6, 30),
            published_at=AS_OF - timedelta(days=10),
            received_at=AS_OF - timedelta(days=9),
            source="cninfo",
            evidence_hash=f"{index:x}" * 64,
        ),
        PointInTimePublishedFact(
            kind="announcement",
            name="official_event_count_20d",
            value=float(index),
            report_period=None,
            published_at=AS_OF - timedelta(days=2),
            received_at=AS_OF - timedelta(days=2),
            source="cninfo",
            evidence_hash=f"{index + 4:x}" * 64,
        ),
    )
    return TomorrowFeatureStockInput(
        code=f"60000{index}",
        board="main",
        industry="equipment",
        industry_effective_at=AS_OF - timedelta(days=100),
        industry_received_at=AS_OF - timedelta(days=90),
        as_of=AS_OF,
        daily_points=closes,
        intraday_points=intraday,
        current_open=11.0 + index * 0.1,
        current_high=11.4 + index * 0.1,
        current_low=10.8 + index * 0.1,
        current_last=11.08 + index * 0.1,
        market_cap=1_000_000_000.0 * index,
        liquidity=20_000_000.0 * index,
        published_facts=facts,
    )


def test_five_tomorrow_feature_families_are_point_in_time_and_deterministic() -> None:
    rows = build_tomorrow_stock_features(tuple(_stock(index) for index in range(1, 5)))

    assert tuple(row.code for row in rows) == ("600001", "600002", "600003", "600004")
    assert {value.family for value in rows[0].values} == {
        "residual_reversal",
        "residual_momentum",
        "overnight",
        "intraday",
        "tail",
    }
    values = {value.name: value.value for value in rows[0].values}
    assert values["residual_reversal_1d"] is not None
    assert values["residual_momentum_20_5"] is not None
    assert values["overnight_gap"] is not None
    assert values["intraday_return"] is not None
    assert values["tail_return_30m"] is not None
    assert rows == build_tomorrow_stock_features(tuple(reversed(tuple(_stock(index) for index in range(1, 5)))))


def test_future_disclosure_is_rejected_even_when_report_period_is_historical() -> None:
    stock = _stock(1)
    fact = replace(
        stock.published_facts[0],
        report_period=date(2025, 12, 31),
        published_at=AS_OF + timedelta(seconds=1),
        received_at=AS_OF + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="published after feature cutoff"):
        replace(stock, published_facts=(fact, *stock.published_facts[1:]))


def test_insufficient_history_remains_missing_instead_of_becoming_zero() -> None:
    stocks = tuple(_stock(index, history_days=3) for index in range(1, 5))

    rows = build_tomorrow_stock_features(stocks)

    values = {value.name: value.value for value in rows[0].values}
    assert values["residual_reversal_5d"] is None
    assert values["residual_momentum_20_5"] is None
    assert "residual_reversal_5d" in rows[0].missing_fields
    assert "residual_momentum_20_5" in rows[0].missing_fields


def test_tail_return_requires_the_exact_thirty_minute_anchor() -> None:
    first = _stock(1)
    without_anchor = replace(
        first,
        intraday_points=tuple(
            point for point in first.intraday_points if point.observed_at != AS_OF - timedelta(minutes=30)
        ),
    )

    rows = build_tomorrow_stock_features((without_anchor, *tuple(_stock(index) for index in range(2, 5))))

    values = {value.name: value.value for value in rows[0].values}
    assert values["tail_return_30m"] is None
    assert "tail_return_30m" in rows[0].missing_fields

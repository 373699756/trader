from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.tail import MinuteBar, TailSignalPolicy, derive_tail_signals

SHANGHAI = ZoneInfo("Asia/Shanghai")
POLICY = TailSignalPolicy(
    lookback_minutes=30,
    minimum_baseline_minutes=30,
    return_score_points_per_pct=25.0,
    volume_score_points_per_ratio=50.0,
)


def test_tail_signals_use_exact_return_and_volume_formulas() -> None:
    bars = _minute_bars()

    signals = derive_tail_signals(bars, observed_at=bars[-1].source_time, policy=POLICY)

    assert signals.return_pct == pytest.approx(2.0)
    assert signals.return_score == pytest.approx(100.0)
    assert signals.volume_ratio == pytest.approx(1.5)
    assert signals.volume_score == pytest.approx(75.0)
    assert signals.reference_price == pytest.approx(10.0)
    assert signals.latest_price == pytest.approx(10.2)
    assert signals.baseline_mean_volume == pytest.approx(100.0)
    assert signals.tail_mean_volume == pytest.approx(150.0)
    assert signals.latest_at == bars[-1].source_time
    assert signals.received_at == bars[-1].received_time
    assert signals.source == "eastmoney_intraday"
    assert signals.data_versions == ("intraday-v1",)


@pytest.mark.parametrize(
    ("latest_price", "expected_return", "expected_score"),
    ((9.8, -2.0, 0.0), (10.0, 0.0, 50.0), (10.2, 2.0, 100.0)),
)
def test_tail_return_score_has_exact_clamp_boundaries(
    latest_price: float,
    expected_return: float,
    expected_score: float,
) -> None:
    bars = _minute_bars(latest_price=latest_price)

    signals = derive_tail_signals(bars, observed_at=bars[-1].source_time, policy=POLICY)

    assert signals.return_pct == pytest.approx(expected_return)
    assert signals.return_score == pytest.approx(expected_score)


@pytest.mark.parametrize(
    ("tail_volume", "expected_ratio", "expected_score"),
    ((0.0, 0.0, 0.0), (100.0, 1.0, 50.0), (200.0, 2.0, 100.0)),
)
def test_tail_volume_score_has_exact_clamp_boundaries(
    tail_volume: float,
    expected_ratio: float,
    expected_score: float,
) -> None:
    bars = _minute_bars(tail_volume=tail_volume)

    signals = derive_tail_signals(bars, observed_at=bars[-1].source_time, policy=POLICY)

    assert signals.volume_ratio == pytest.approx(expected_ratio)
    assert signals.volume_score == pytest.approx(expected_score)


def test_tail_signals_ignore_future_and_other_trade_date_bars() -> None:
    bars = _minute_bars()
    observed_at = bars[-1].source_time
    unrelated = (
        MinuteBar(
            observed_at - timedelta(days=1),
            99.0,
            9_999.0,
            "eastmoney_intraday",
            observed_at,
            "intraday-v1",
        ),
        MinuteBar(
            observed_at + timedelta(minutes=1),
            99.0,
            9_999.0,
            "eastmoney_intraday",
            observed_at,
            "intraday-v1",
        ),
    )

    signals = derive_tail_signals((*bars, *unrelated), observed_at=observed_at, policy=POLICY)

    assert signals.return_pct == pytest.approx(2.0)
    assert signals.volume_ratio == pytest.approx(1.5)
    assert signals.latest_at == observed_at


def test_tail_signals_ignore_bars_received_after_the_observation_time() -> None:
    bars = _minute_bars()
    observed_at = bars[-1].source_time
    received_late = replace(
        bars[-1],
        close=99.0,
        received_time=observed_at + timedelta(seconds=1),
    )

    signals = derive_tail_signals(
        (*bars[:-1], received_late),
        observed_at=observed_at,
        policy=POLICY,
    )

    assert signals.latest_at == bars[-2].source_time
    assert signals.latest_price == pytest.approx(10.0)


def test_tail_signals_ignore_records_outside_trading_minutes() -> None:
    bars = _minute_bars()
    observed_at = bars[-1].source_time
    out_of_session = replace(
        bars[-1],
        source_time=observed_at.replace(hour=12, minute=30),
        close=99.0,
        volume=9_999.0,
    )

    signals = derive_tail_signals((*bars, out_of_session), observed_at=observed_at, policy=POLICY)

    assert signals.return_pct == pytest.approx(2.0)
    assert signals.volume_ratio == pytest.approx(1.5)


@pytest.mark.parametrize("invalid_close", (0.0, -1.0, float("nan"), float("inf")))
def test_tail_signals_exclude_nonpositive_or_nonfinite_close(invalid_close: float) -> None:
    bars = _minute_bars()

    signals = derive_tail_signals(
        (*bars[:-1], replace(bars[-1], close=invalid_close)),
        observed_at=bars[-1].source_time,
        policy=POLICY,
    )

    assert signals.latest_at == bars[-2].source_time
    assert signals.latest_price == 10.0


@pytest.mark.parametrize("invalid_volume", (-1.0, float("nan"), float("inf")))
def test_tail_signals_do_not_convert_invalid_volume_to_zero(invalid_volume: float) -> None:
    bars = _minute_bars()

    signals = derive_tail_signals(
        (*bars[:-1], replace(bars[-1], volume=invalid_volume)),
        observed_at=bars[-1].source_time,
        policy=POLICY,
    )

    assert signals.return_pct == pytest.approx(2.0)
    assert signals.volume_ratio is None
    assert signals.volume_score is None


def test_tail_signals_reject_a_gap_in_the_latest_trading_minute_window() -> None:
    bars = _minute_bars()
    with_gap = (*bars[:45], *bars[46:])

    signals = derive_tail_signals(with_gap, observed_at=bars[-1].source_time, policy=POLICY)

    assert signals.return_pct is None
    assert signals.return_score is None
    assert signals.volume_ratio is None
    assert signals.volume_score is None


def test_tail_signals_exclude_duplicate_minute_without_using_ambiguous_value() -> None:
    bars = _minute_bars()
    duplicate = replace(bars[-1], close=99.0)

    signals = derive_tail_signals((*bars, duplicate), observed_at=bars[-1].source_time, policy=POLICY)

    assert signals.latest_at == bars[-2].source_time
    assert signals.latest_price == 10.0
    assert signals.return_pct == pytest.approx(0.0)


def test_tail_signals_treat_lunch_break_as_adjacent_trading_minutes() -> None:
    morning = tuple(datetime(2026, 7, 16, 9, 31, tzinfo=SHANGHAI) + timedelta(minutes=index) for index in range(120))
    afternoon = tuple(datetime(2026, 7, 16, 13, 1, tzinfo=SHANGHAI) + timedelta(minutes=index) for index in range(10))
    times = (*morning, *afternoon)
    bars = tuple(
        MinuteBar(
            source_time=source_time,
            close=10.2 if index == len(times) - 1 else 10.0,
            volume=150.0 if index >= len(times) - 30 else 100.0,
            source="eastmoney_intraday",
            received_time=times[-1],
            data_version="intraday-v1",
        )
        for index, source_time in enumerate(times)
    )

    signals = derive_tail_signals(bars, observed_at=times[-1], policy=POLICY)

    assert signals.return_pct == pytest.approx(2.0)
    assert signals.volume_ratio == pytest.approx(1.5)


def test_tail_signals_require_timezone_aware_observation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        derive_tail_signals((), observed_at=datetime(2026, 7, 16, 10, 0), policy=POLICY)


def _minute_bars(*, latest_price: float = 10.2, tail_volume: float = 150.0) -> tuple[MinuteBar, ...]:
    start = datetime(2026, 7, 16, 9, 31, tzinfo=SHANGHAI)
    return tuple(
        MinuteBar(
            source_time=start + timedelta(minutes=index),
            close=latest_price if index == 60 else 10.0,
            volume=tail_volume if index >= 31 else 100.0,
            source="eastmoney_intraday",
            received_time=start + timedelta(minutes=60),
            data_version="intraday-v1",
        )
        for index in range(61)
    )

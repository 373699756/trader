from __future__ import annotations

from datetime import date, timedelta

from trader.application.research.cross_strategy_conclusion import CrossStrategyConclusionService
from trader.application.research.d25_terminal_holdout import D25TerminalHoldoutService, D25TerminalRow
from trader.application.research.today_terminal_holdout import TodayTerminalHoldoutService, TodayTerminalRow
from trader.application.research.tomorrow_point_in_time_holdout import (
    TomorrowPointInTimeHoldoutService,
    TomorrowPointInTimeRow,
)


def _rows(row_type):
    return tuple(
        row_type(
            trade_date=date(2025, 1, 1) + timedelta(days=index),
            code="600001",
            board="main",
            industry="bank",
            market_state="up",
            volatility_state="low",
            liquidity_state="high",
            predicted_net_excess_return=0.01,
            actual_net_excess_returns=(0.01, 0.007, 0.002),
            baseline_net_excess_returns=(0.0, -0.003, -0.008),
            selected=True,
            baseline_selected=True,
            severe_loss=False,
            baseline_severe_loss=False,
            mae_atr20=-0.3,
            baseline_mae_atr20=-0.3,
            point_in_time_parity=True,
            horizon_net_excess_returns=(0.01, 0.01, 0.01, 0.01) if row_type is D25TerminalRow else (),
            baseline_horizon_net_excess_returns=(0.0, 0.0, 0.0, 0.0) if row_type is D25TerminalRow else (),
        )
        for index in range(200)
    )


def test_strategy_adapters_bind_their_fixed_anchor_and_identity() -> None:
    today = TodayTerminalHoldoutService(_rows(TodayTerminalRow)).execute()
    tomorrow = TomorrowPointInTimeHoldoutService(_rows(TomorrowPointInTimeRow)).execute()
    d25 = D25TerminalHoldoutService(_rows(D25TerminalRow)).execute()

    assert today.anchor == "11:20_unadjusted_point_in_time"
    assert tomorrow.anchor == "14:50_unadjusted_point_in_time"
    assert d25.anchor == "14:50_unadjusted_point_in_time"
    assert {today.strategy, tomorrow.strategy, d25.strategy} == {"today", "tomorrow", "d25"}

    conclusion = CrossStrategyConclusionService().execute(today, tomorrow, d25)
    assert conclusion.strategy_statuses == (
        ("today", today.status),
        ("tomorrow", tomorrow.status),
        ("d25", d25.status),
    )
    assert conclusion.production_authority is False
    assert conclusion.strategy_metrics[0][0] == "today"
    assert conclusion.strategy_metrics[1][0] == "tomorrow"
    assert conclusion.strategy_metrics[2][0] == "d25"

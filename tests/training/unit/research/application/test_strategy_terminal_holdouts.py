from __future__ import annotations

from datetime import date, timedelta

from trader.training.application.cross_strategy_conclusion import CrossStrategyConclusionService
from trader.training.application.d25_terminal_holdout import D25TerminalHoldoutService
from trader.training.application.tomorrow_point_in_time_holdout import TomorrowPointInTimeHoldoutService
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState, TerminalHoldoutRow


def _rows(*, d25: bool = False) -> tuple[TerminalHoldoutRow, ...]:
    return tuple(
        TerminalHoldoutRow(
            trade_date=date(2025, 1, 1) + timedelta(days=index),
            code="600001",
            board="main",
            industry="bank",
            market_state="up",
            volatility_state="low",
            liquidity_state="high",
            predicted_net_excess_return=0.01,
            ranking_score=80.0, local_score=80.0,
            actual_net_excess_returns=(0.01, 0.007, 0.002),
            baseline_net_excess_returns=(0.0, -0.003, -0.008),
            selected=True,
            baseline_selected=True,
            severe_loss=False,
            baseline_severe_loss=False,
            mae_atr20=-0.3,
            baseline_mae_atr20=-0.3,
            exit_untradable=False,
            baseline_exit_untradable=False,
            capacity=1_000_000.0,
            baseline_capacity=1_000_000.0,
            point_in_time_parity=True,
            horizon_net_excess_returns=(0.01, 0.01, 0.01, 0.01) if d25 else (),
            baseline_horizon_net_excess_returns=(0.0, 0.0, 0.0, 0.0) if d25 else (),
        )
        for index in range(200)
    )


def test_strategy_adapters_bind_their_fixed_anchor_and_identity() -> None:
    parent = TerminalHoldoutParentState(profile_id="v2")
    tomorrow = TomorrowPointInTimeHoldoutService(_rows(), parent).execute()
    d25 = D25TerminalHoldoutService(_rows(d25=True), parent).execute()

    assert tomorrow.anchor == "15:00_unadjusted_point_in_time"
    assert d25.anchor == "15:00_unadjusted_point_in_time"
    assert {tomorrow.strategy, d25.strategy} == {"tomorrow", "d25"}

    conclusion = CrossStrategyConclusionService().execute(tomorrow, d25)
    assert conclusion.strategy_statuses == (
        ("tomorrow", tomorrow.status),
        ("d25", d25.status),
    )
    assert conclusion.production_authority is False
    assert conclusion.strategy_metrics[0][0] == "tomorrow"
    assert conclusion.strategy_metrics[1][0] == "d25"

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

from stock_analyzer import config
from stock_analyzer.app_support import strategy_validation_gate_decision
from stock_analyzer.execution_policy import build_execution_policy
from stock_analyzer.risk_rules import simulate_exit
from stock_analyzer.strategy_validation import StrategyValidationStore
from stock_analyzer.validation_repository import SignalFreezeDeadlineExceeded
from stock_analyzer.validation_statistics import block_bootstrap_mean_confidence_interval


def _raw_history(rows):
    frame = pd.DataFrame(rows)
    frame.attrs["price_adjustment_mode"] = "raw"
    return frame


class RawProvider:
    def __init__(self, rows):
        self.rows = rows

    def get_execution_history(self, code, days=180):
        return _raw_history(self.rows)

    def get_history(self, code, days=180):
        return _raw_history(self.rows)


def _save_signal(store):
    store.save_signals(
        "tomorrow_picks",
        config.TOMORROW_STRATEGY_VERSION,
        "2024-01-02T14:49:00",
        [{"rank": 1, "code": "600001", "name": "隔夜样本", "price": 10.0, "score": 90}],
        execution_policy=build_execution_policy("tomorrow_picks"),
    )


def test_post_1430_policy_freezes_at_1450_and_keeps_top5_weight():
    policy = build_execution_policy("tomorrow_picks")

    assert policy["entry"]["timing"] == "same_trade_day_after_1430"
    assert policy["entry"]["signal_cutoff"] == "14:50"
    assert policy["entry"]["order_window"] == "14:30-14:50"
    assert policy["entry"]["price_field"] == "signal_time_raw_quote_with_slippage"
    assert policy["exit"]["primary_timing"] == "next_trade_day_dynamic_exit"
    assert policy["portfolio"]["default_target_weight_pct"] == 20.0


def test_tail_auction_switch_changes_frozen_execution_policy():
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False):
        baseline = build_execution_policy("tomorrow_picks")
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True):
        research = build_execution_policy("tomorrow_picks")

    assert not baseline["cost"]["tail_auction"]["enabled"]
    assert research["cost"]["tail_auction"]["enabled"]
    assert baseline["policy_version"] != research["policy_version"]


def test_signal_batch_rolls_back_if_database_freeze_reaches_cutoff(tmp_path):
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    with patch("stock_analyzer.validation_repository.datetime") as clock:
        clock.now.return_value = datetime(2024, 1, 2, 14, 50, 0)
        with pytest.raises(SignalFreezeDeadlineExceeded):
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-02T14:50:00",
                [{"rank": 1, "code": "600001", "name": "超时样本", "price": 10.0, "score": 90}],
                batch_metadata={"freeze_deadline": "2024-01-02T14:50:00"},
                execution_policy=build_execution_policy("tomorrow_picks"),
            )

    assert store.list_signal_dates("tomorrow_picks") == []


def test_signal_exit_return_uses_signal_reference_to_t1_exit_raw_prices(tmp_path):
    provider = RawProvider(
        [
            {"trade_date": "20240101", "open": 9.8, "high": 10.1, "low": 9.7, "price": 10.0},
            {"trade_date": "20240102", "open": 10.1, "high": 10.3, "low": 9.9, "price": 10.0},
            {"trade_date": "20240103", "open": 10.2, "high": 11.2, "low": 10.1, "price": 11.0},
        ]
    )
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    _save_signal(store)

    result = store.update_outcomes(provider, signal_date="2024-01-02", strategy_name="tomorrow_picks")
    row = store.signals_for_date("2024-01-02", "tomorrow_picks")[0]

    assert result["updated"] == 1
    assert row["stored_primary_return_field"] == "signal_exit_return"
    assert row["signal_exit_return"] == row["exit_return"]
    assert row["entry_price"] == 10.0
    assert row["exit_price"] > 0
    assert row["entry_trade_date"] == "20240102"
    assert row["exit_trade_date"] == "20240103"
    assert row["price_adjustment_mode"] == "raw"


def test_signal_reference_is_not_reclassified_from_later_close_limit_state(tmp_path):
    provider = RawProvider(
        [
            {"trade_date": "20240101", "open": 10.0, "high": 10.0, "low": 10.0, "price": 10.0},
            {"trade_date": "20240102", "open": 11.0, "high": 11.0, "low": 11.0, "price": 11.0},
            {"trade_date": "20240103", "open": 11.1, "high": 11.3, "low": 10.9, "price": 11.2},
        ]
    )
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    _save_signal(store)

    result = store.update_outcomes(provider, signal_date="2024-01-02", strategy_name="tomorrow_picks")
    row = store.signals_for_date("2024-01-02", "tomorrow_picks")[0]

    assert result["execution_skipped"] == 0
    assert row["label_status"] == "settled"
    assert row["entry_status"] == "filled"
    assert row["position_status"] == "closed"


def test_t1_limit_down_remains_open_then_retries_until_tradable(tmp_path):
    rows = [
        {"trade_date": "20240101", "open": 10.0, "high": 10.1, "low": 9.9, "price": 10.0},
        {"trade_date": "20240102", "open": 10.0, "high": 10.1, "low": 9.9, "price": 10.0},
        {"trade_date": "20240103", "open": 9.0, "high": 9.0, "low": 9.0, "price": 9.0},
    ]
    provider = RawProvider(rows)
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    _save_signal(store)

    first = store.update_outcomes(provider, signal_date="2024-01-02", strategy_name="tomorrow_picks")
    pending = store.signals_for_date("2024-01-02", "tomorrow_picks")[0]
    assert first["execution_skipped"] == 0
    assert pending["label_status"] == "pending"
    assert pending["entry_status"] == "filled"
    assert pending["exit_status"] == "pending"
    assert pending["position_status"] == "exit_pending"
    assert pending["unfilled_exit_quantity"] == 0

    provider.rows.append(
        {"trade_date": "20240104", "open": 9.2, "high": 9.5, "low": 9.1, "price": 9.4}
    )
    second = store.update_outcomes(provider, signal_date="2024-01-02", strategy_name="tomorrow_picks")
    settled = store.signals_for_date("2024-01-02", "tomorrow_picks")[0]
    assert second["updated"] == 1
    assert settled["label_status"] == "settled"
    assert settled["exit_reason"] == "stop_loss_limit_down_delayed"
    assert settled["exit_price"] == 9.2


def test_swing_entry_day_risk_event_cannot_exit():
    future = pd.DataFrame(
        [
            {"trade_date": "20240102", "open": 10.0, "high": 10.2, "low": 9.0, "price": 9.2},
            {"trade_date": "20240103", "open": 9.3, "high": 9.5, "low": 9.1, "price": 9.4},
        ]
    )
    result = simulate_exit(
        future,
        10.0,
        holding_days=2,
        policy={
            "holding_days": 2,
            "stop_loss_pct": 5.0,
            "take_profit_pct": 0.0,
            "trailing_stop_pct": 0.0,
            "earliest_exit_offset_days": 1,
        },
    )

    assert result["exit_date"] == "20240103"
    assert result["exit_reason"] == "stop_loss"


def test_tomorrow_gate_requires_60_complete_positive_portfolio_days():
    metrics = {
        "strategy_name": "tomorrow_picks",
        "outcome_sample_count": 300,
        "real_sample_count": 300,
        "real_day_count": 60,
        "real_avg_primary_return_net": 0.4,
        "real_win_rate_primary_net": 60.0,
        "real_portfolio_max_drawdown_pct": -2.0,
        "real_avg_primary_return_net_ci95_low": 0.1,
        "portfolio_day_count": 59,
        "portfolio_total_return_pct": 8.0,
        "portfolio_avg_daily_net_return_ci95_low": 0.1,
    }
    with patch("stock_analyzer.app_support.strategy_status", return_value={"state": "active", "label": ""}):
        blocked = strategy_validation_gate_decision(metrics, "tomorrow_picks")
        metrics["portfolio_day_count"] = 60
        passed = strategy_validation_gate_decision(metrics, "tomorrow_picks")

    assert blocked["blocked"]
    assert not passed["blocked"]
    assert passed["validated"]


def test_tomorrow_gate_blocks_missing_portfolio_confidence_interval():
    metrics = {
        "strategy_name": "tomorrow_picks",
        "outcome_sample_count": 300,
        "real_sample_count": 300,
        "real_day_count": 60,
        "real_avg_primary_return_net": 0.4,
        "real_win_rate_primary_net": 60.0,
        "real_portfolio_max_drawdown_pct": -2.0,
        "real_avg_primary_return_net_ci95_low": 0.1,
        "portfolio_day_count": 60,
        "portfolio_total_return_pct": 8.0,
    }
    with patch("stock_analyzer.app_support.strategy_status", return_value={"state": "active", "label": ""}):
        decision = strategy_validation_gate_decision(metrics, "tomorrow_picks")

    assert decision["blocked"]
    assert decision["position_scale"] == 0.0


def test_block_bootstrap_is_deterministic():
    first = block_bootstrap_mean_confidence_interval([0.4, 0.2, -0.1, 0.5, 0.3])
    second = block_bootstrap_mean_confidence_interval([0.4, 0.2, -0.1, 0.5, 0.3])
    assert first == second
    assert first[0] is not None and first[1] is not None

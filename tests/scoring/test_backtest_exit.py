import unittest
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.backtest import run_alphalite_backtest, run_rolling_alphalite_backtest
from stock_analyzer.risk_rules import simulate_exit


class BacktestExitTest(unittest.TestCase):
    def test_backtest_returns_metrics_for_history_pool(self):
        history_by_code = {
            "600001": pd.DataFrame(
                {
                    "price": [10 + i * 0.1 for i in range(60)],
                    "high": [10 + i * 0.1 for i in range(60)],
                    "turnover": [10000000 + i * 50000 for i in range(60)],
                }
            )
        }

        result = run_alphalite_backtest(history_by_code, top_k=1, holding_days=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["selected_count"], 1)
        self.assertIn("avg_net_return", result["metrics"])

    def test_backtest_endpoint_marks_alphalite_as_research_only(self):
        history = pd.DataFrame({"price": [10 + i * 0.1 for i in range(60)]})
        with patch("stock_analyzer.app.list_market_data_codes", return_value=["600001"]), patch(
            "stock_analyzer.app.load_local_history_frames",
            return_value={"600001": history},
        ), patch(
            "stock_analyzer.app.run_rolling_alphalite_backtest",
            return_value={"ok": True, "metrics": {"period_count": 1}},
        ):
            response = create_app().test_client().get("/api/backtest")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["scope"], "alphalite_research")
        self.assertFalse(payload["production_strategy_validation"])

    def test_backtest_trade_cost_reuses_validation_cost_model(self):
        from stock_analyzer.backtest import _backtest_trade_cost_pct
        from stock_analyzer.strategy_validation import _execution_cost_pct

        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
            config, "ENABLE_MARKET_IMPACT", False
        ):
            self.assertEqual(_backtest_trade_cost_pct(100_000_000), _execution_cost_pct({"turnover": 100_000_000}))

    def test_simulate_exit_handles_stop_take_profit_and_trailing(self):
        stop = simulate_exit(
            pd.DataFrame([{"trade_date": "20240102", "high": 10.2, "low": 9.4, "price": 9.8}]),
            entry_price=10,
            holding_days=3,
        )
        take = simulate_exit(
            pd.DataFrame([{"trade_date": "20240102", "high": 10.9, "low": 10.1, "price": 10.5}]),
            entry_price=10,
            holding_days=3,
        )
        trailing = simulate_exit(
            pd.DataFrame(
                [
                    {"trade_date": "20240102", "high": 10.7, "low": 10.2, "price": 10.6},
                    {"trade_date": "20240103", "high": 10.8, "low": 10.2, "price": 10.4},
                ]
            ),
            entry_price=10,
            holding_days=3,
            policy={"stop_loss_pct": 0, "take_profit_pct": 0, "trailing_stop_pct": 4},
        )

        self.assertEqual(stop["exit_reason"], "stop_loss")
        self.assertAlmostEqual(stop["exit_return"], -5.0)
        self.assertEqual(take["exit_reason"], "take_profit")
        self.assertAlmostEqual(take["exit_return"], 8.0)
        self.assertEqual(trailing["exit_reason"], "trailing_stop")
        self.assertGreater(trailing["exit_return"], 0)

    def test_simulate_exit_delays_stop_on_sealed_limit_down(self):
        result = simulate_exit(
            pd.DataFrame(
                [
                    {"trade_date": "20240102", "prev_close": 10.0, "open": 9.0, "high": 9.05, "low": 9.0, "price": 9.0},
                    {"trade_date": "20240103", "prev_close": 9.0, "open": 8.8, "high": 9.0, "low": 8.6, "price": 8.9},
                ]
            ),
            entry_price=10,
            holding_days=1,
            policy={"limit_down_pct": 10},
        )

        self.assertEqual(result["exit_reason"], "stop_loss_limit_down_delayed")
        self.assertEqual(result["exit_days"], 2)
        self.assertAlmostEqual(result["exit_price"], 8.8)

    def test_simulate_exit_uses_open_when_price_gaps_through_stop(self):
        result = simulate_exit(
            pd.DataFrame(
                [
                    {
                        "trade_date": "20240102",
                        "prev_close": 10.0,
                        "open": 9.2,
                        "high": 9.4,
                        "low": 9.0,
                        "price": 9.3,
                    }
                ]
            ),
            entry_price=10,
            holding_days=1,
        )

        self.assertEqual(result["exit_reason"], "stop_loss")
        self.assertAlmostEqual(result["exit_price"], 9.2)
        self.assertAlmostEqual(result["exit_return"], -8.0)

    def test_simulate_exit_waits_through_consecutive_sealed_limit_down_days(self):
        result = simulate_exit(
            pd.DataFrame(
                [
                    {"trade_date": "20240102", "prev_close": 10.0, "open": 9.0, "high": 9.05, "low": 9.0, "price": 9.0},
                    {"trade_date": "20240103", "prev_close": 9.0, "open": 8.1, "high": 8.15, "low": 8.1, "price": 8.1},
                    {"trade_date": "20240104", "prev_close": 8.1, "open": 8.0, "high": 8.3, "low": 7.9, "price": 8.2},
                ]
            ),
            entry_price=10,
            holding_days=1,
            policy={"limit_down_pct": 10},
        )

        self.assertEqual(result["exit_reason"], "stop_loss_limit_down_delayed")
        self.assertEqual(result["exit_days"], 3)
        self.assertAlmostEqual(result["exit_price"], 8.0)

    def test_backtest_uses_exit_rule_before_fixed_holding_period(self):
        prices = [10 + i * 0.1 for i in range(60)]
        lows = [price * 0.99 for price in prices]
        lows[57] = prices[56] * 0.94
        history_by_code = {
            "600001": pd.DataFrame(
                {
                    "price": prices,
                    "high": [price * 1.02 for price in prices],
                    "low": lows,
                    "turnover": [10000000 + i * 50000 for i in range(60)],
                }
            )
        }

        result = run_alphalite_backtest(history_by_code, top_k=1, holding_days=3)

        self.assertTrue(result["ok"])
        selected = result["selected"][0]
        self.assertEqual(selected["exit_reason"], "stop_loss")
        self.assertAlmostEqual(selected["gross_return"], -5.0)
        self.assertGreater(selected["fixed_gross_return"], 0)

    def test_rolling_backtest_returns_drawdown_metrics(self):
        history_by_code = {
            "600001": pd.DataFrame(
                {
                    "trade_date": ["202401{:02d}".format(i + 1) for i in range(80)],
                    "price": [10 + i * 0.05 for i in range(80)],
                    "high": [10 + i * 0.05 for i in range(80)],
                    "turnover": [10000000 + i * 50000 for i in range(80)],
                }
            ),
            "600002": pd.DataFrame(
                {
                    "trade_date": ["202401{:02d}".format(i + 1) for i in range(80)],
                    "price": [12 + i * 0.03 for i in range(80)],
                    "high": [12 + i * 0.03 for i in range(80)],
                    "turnover": [12000000 + i * 30000 for i in range(80)],
                }
            ),
        }

        result = run_rolling_alphalite_backtest(
            history_by_code,
            top_k=1,
            holding_days=3,
            lookback_days=30,
            rebalance_step=5,
        )

        self.assertTrue(result["ok"])
        self.assertIn("max_drawdown", result["metrics"])
        self.assertGreater(result["metrics"]["period_count"], 0)


if __name__ == "__main__":
    unittest.main()

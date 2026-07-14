import unittest
from unittest.mock import patch

from stock_analyzer import config


class ValidationGatesTest(unittest.TestCase):
    def test_frozen_production_baseline_has_no_runtime_drift(self):
        from stock_analyzer.production_baseline import production_baseline_status

        status = production_baseline_status()

        self.assertTrue(status["freeze_enabled"])
        self.assertEqual(status["status"], "frozen")
        self.assertEqual(status["drift"], [])

    def test_strategy_status_does_not_fallback_when_real_metric_is_zero(self):
        from stock_analyzer.strategy_health import strategy_status

        status = strategy_status(
            {
                "sample_count": 80,
                "real_sample_count": 60,
                "real_win_rate_primary_net": 0.0,
                "real_avg_primary_return_net": 0.0,
                "win_rate_primary_net": 80.0,
                "avg_primary_return_net": 2.0,
            }
        )

        self.assertEqual(status["state"], "retired")

    def test_strategy_status_uses_real_trading_days_instead_of_row_count(self):
        from stock_analyzer.strategy_health import strategy_status

        status = strategy_status(
            {
                "strategy_name": "tomorrow_picks",
                "sample_count": 80,
                "day_count": 2,
                "real_sample_count": 70,
                "real_day_count": 2,
                "real_win_rate_primary_net": 0.0,
                "real_avg_primary_return_net": -1.0,
            }
        )

        self.assertEqual(status["state"], "pending")

    def test_tomorrow_validation_gate_waits_for_enough_real_days(self):
        from stock_analyzer.app_support import tomorrow_validation_gate_decision

        decision = tomorrow_validation_gate_decision(
            {
                "sample_count": 2,
                "outcome_sample_count": 2,
                "real_sample_count": 2,
                "real_day_count": 2,
                "avg_primary_return_net": -1.0,
                "win_rate_primary_net": 0.0,
                "real_avg_primary_return_net": -1.0,
                "real_win_rate_primary_net": 0.0,
            }
        )

        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["state"], "pending")
        self.assertIn("真实验证不足", decision["reason"])

    def test_tomorrow_validation_gate_ignores_bad_replay_metrics_when_real_is_good(self):
        from stock_analyzer.app_support import tomorrow_validation_gate_decision

        decision = tomorrow_validation_gate_decision(
            {
                "strategy_name": "tomorrow_picks",
                "sample_count": 80,
                "day_count": 40,
                "real_sample_count": 40,
                "real_day_count": 60,
                "avg_primary_return_net": -2.0,
                "win_rate_primary_net": 10.0,
                "real_avg_primary_return_net": 0.6,
                "real_win_rate_primary_net": 55.0,
                "avg_max_drawdown_3d": -1.0,
            }
        )

        self.assertFalse(decision["blocked"])
        self.assertEqual(decision["state"], "active")
        self.assertEqual(decision["position_scale"], 1.0)

    def test_strategy_validation_gate_scales_position_for_probation_metrics(self):
        from stock_analyzer.app_support import apply_strategy_validation_gate

        rows = [
            {
                "code": "600001",
                "tier": "primary_watch",
                "tier_label": "primary",
                "reasons": [],
                "trade_action": {"action": "buy_confirmed", "position_size": 1.0},
            },
            {
                "code": "600002",
                "tier": "primary_watch",
                "tier_label": "primary",
                "reasons": [],
                "trade_action": {"action": "buy_small", "position_size": 0.35},
            },
        ]
        meta = {}

        with patch.object(config, "ENABLE_DYNAMIC_POSITION_SCALING", True), patch.object(
            config, "STRATEGY_POSITION_SCALE_PROBATION", 0.6
        ):
            decision = apply_strategy_validation_gate(
                "tomorrow_picks",
                rows,
                meta,
                {
                    "strategy_name": "tomorrow_picks",
                    "sample_count": 80,
                    "day_count": 80,
                    "real_sample_count": 80,
                    "real_day_count": 80,
                    "real_avg_primary_return_net": 0.2,
                    "real_avg_primary_return_net_ci95_low": 0.1,
                    "real_win_rate_primary_net": 51.0,
                    "real_portfolio_max_drawdown_pct": -5.0,
                },
            )

        self.assertFalse(decision["blocked"])
        self.assertEqual(decision["state"], "probation")
        self.assertLess(decision["position_scale"], 1.0)
        self.assertAlmostEqual(rows[0]["trade_action"]["base_position_size"], 1.0)
        self.assertAlmostEqual(rows[0]["trade_action"]["position_size"], decision["position_scale"], places=4)
        self.assertLess(rows[1]["trade_action"]["position_size"], 0.35)
        self.assertIn("position_scale", meta["validation_gate"])

    def test_tomorrow_validation_gate_demotes_primary_when_retired(self):
        from stock_analyzer.app_support import apply_tomorrow_validation_gate as _apply_tomorrow_validation_gate

        rows = [
            {"code": "600001", "tier": "primary_watch", "tier_label": "primary", "reasons": []},
            {"code": "600002", "tier": "backup_pool", "tier_label": "backup", "reasons": []},
        ]
        meta = {"primary_watch_count": 1, "primary_gate_count": 1, "gate_reason": "base gate"}

        decision = _apply_tomorrow_validation_gate(
            rows,
            meta,
            {
                "sample_count": 3,
                "outcome_sample_count": 3,
                "total_outcome_sample_count": 30,
                "real_sample_count": 60,
                "real_day_count": 60,
                "avg_primary_return_net": -0.8,
                "win_rate_primary_net": 30.0,
                "real_avg_primary_return_net": -0.4,
                "real_win_rate_primary_net": 40.0,
            },
        )

        self.assertTrue(decision["blocked"])
        self.assertTrue(decision["allows_backup"])
        self.assertEqual(meta["primary_watch_count"], 0)
        self.assertEqual(meta["backup_watch_count"], 2)
        self.assertEqual({row["tier"] for row in rows}, {"backup_pool"})
        self.assertTrue(all(row["trade_action"]["position_size"] == 0 for row in rows))
        self.assertTrue(all(not row["execution_allowed"] for row in rows))

    def test_strategy_validation_gate_blocks_excessive_primary_drawdown(self):
        from stock_analyzer.app_support import strategy_validation_gate_decision

        decision = strategy_validation_gate_decision(
            {
                "strategy_name": "swing_picks",
                "day_count": 60,
                "real_day_count": 60,
                "real_avg_primary_return_net": 0.8,
                "real_win_rate_primary_net": 60.0,
                "real_avg_max_drawdown_primary": -9.0,
            }
        )

        self.assertTrue(decision["blocked"])
        self.assertIn("回撤", decision["reason"])

    def test_strategy_validation_gate_requires_positive_return_confidence_bound(self):
        from stock_analyzer.app_support import strategy_validation_gate_decision

        decision = strategy_validation_gate_decision(
            {
                "strategy_name": "tomorrow_picks",
                "day_count": 60,
                "real_day_count": 60,
                "real_avg_primary_return_net": 0.4,
                "real_avg_primary_return_net_ci95_low": -0.1,
                "real_win_rate_primary_net": 55.0,
                "real_portfolio_max_drawdown_pct": -3.0,
            }
        )

        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["real_avg_primary_return_net_ci95_low"], -0.1)

    def test_validation_statistics_report_confidence_and_compounded_drawdown(self):
        from stock_analyzer.strategy_validation import _mean_confidence_interval, _portfolio_max_drawdown

        low, high = _mean_confidence_interval([0.5, 1.0, 1.5, 2.0])
        drawdown = _portfolio_max_drawdown(
            [
                {"signal_date": "2024-01-01", "avg_primary_return_net": 10.0},
                {"signal_date": "2024-01-02", "avg_primary_return_net": -20.0},
            ]
        )

        self.assertLess(low, 1.25)
        self.assertGreater(high, 1.25)
        self.assertAlmostEqual(drawdown, -20.0)


if __name__ == "__main__":
    unittest.main()

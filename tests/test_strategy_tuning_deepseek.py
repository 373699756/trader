import unittest

from stock_analyzer.strategy_tuning import build_strategy_tuning_plan


class StrategyTuningDeepSeekTest(unittest.TestCase):
    def test_oos_passed_local_plan_remains_shadow_only(self):
        plan = build_strategy_tuning_plan(
            "tomorrow_picks",
            metrics={
                "day_count": 60,
                "real_day_count": 60,
                "replay_day_count": 0,
                "pending_outcome_count": 0,
                "real_win_rate_primary_net": 55.0,
                "real_avg_primary_return_net": 0.6,
                "real_avg_primary_return_net_ci95_low": 0.1,
                "real_avg_max_drawdown_primary": -2.0,
            },
            dates=[{"signal_date": "2026-01-02", "count": 5}],
            days=20,
        )

        self.assertEqual(plan["status"], "shadow_only")
        self.assertFalse(plan["can_apply"])
        self.assertTrue(plan["shadow_mode"])


if __name__ == "__main__":
    unittest.main()

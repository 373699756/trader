import unittest

from stock_analyzer.strategy_tuning import build_strategy_tuning_plan


class StrategyTuningDeepSeekTest(unittest.TestCase):
    def test_oos_passed_deepseek_rule_can_enter_manual_confirmation(self):
        plan = build_strategy_tuning_plan(
            "tomorrow_picks",
            metrics={
                "sample_count": 30,
                "real_sample_count": 10,
                "replay_sample_count": 0,
                "pending_outcome_count": 0,
                "real_win_rate_primary_net": 55.0,
                "real_avg_primary_return_net": 0.6,
            },
            dates=[{"signal_date": "2026-01-02", "count": 5}],
            deepseek_review={
                "enabled": True,
                "status": "ok",
                "rule_candidates": [
                    {
                        "field": "pct_chg",
                        "operator": ">",
                        "threshold": 5,
                        "penalty": 20,
                        "can_apply": True,
                        "oos_evaluation": {
                            "oos_improvement": 0.3,
                            "positive_folds": 3,
                            "fold_count": 4,
                        },
                    }
                ],
            },
            days=20,
        )

        self.assertEqual(plan["status"], "ready_for_confirmation")
        self.assertTrue(plan["can_apply"])
        self.assertFalse(plan["shadow_mode"])


if __name__ == "__main__":
    unittest.main()

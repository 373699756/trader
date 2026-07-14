import unittest
from unittest.mock import patch

from stock_analyzer.strategy_tuning import build_strategy_tuning_plan
from stock_analyzer.scoring_core.weights import WEIGHTS


class StrategyTuningDeepSeekTest(unittest.TestCase):
    def test_fingerprint_changes_with_samples_metrics_and_weights(self):
        metrics = {
            "day_count": 60,
            "sample_count": 100,
            "outcome_sample_count": 90,
            "real_day_count": 60,
            "replay_day_count": 0,
            "pending_outcome_count": 0,
            "unknown_outcome_count": 0,
            "real_win_rate_primary_net": 55.0,
            "real_avg_primary_return_net": 0.6,
            "real_avg_primary_return_net_ci95_low": 0.1,
            "real_avg_max_drawdown_primary": -2.0,
        }
        dates = [{"signal_date": "2026-07-14", "count": 5}]
        baseline = build_strategy_tuning_plan("tomorrow_picks", metrics, dates, days=60)

        sample_metrics = {**metrics, "sample_count": 101}
        sample_changed = build_strategy_tuning_plan("tomorrow_picks", sample_metrics, dates, days=60)
        metric_changed = build_strategy_tuning_plan(
            "tomorrow_picks",
            {**sample_metrics, "real_avg_primary_return_net": 0.7},
            dates,
            days=60,
        )
        with patch.dict(WEIGHTS, {"tomorrow_picks": {"quality": 0.11}}):
            first_weights = build_strategy_tuning_plan("tomorrow_picks", metrics, dates, days=60)
        with patch.dict(WEIGHTS, {"tomorrow_picks": {"quality": 0.12}}):
            second_weights = build_strategy_tuning_plan("tomorrow_picks", metrics, dates, days=60)

        self.assertNotEqual(baseline["input_fingerprint"], sample_changed["input_fingerprint"])
        self.assertNotEqual(sample_changed["input_fingerprint"], metric_changed["input_fingerprint"])
        self.assertNotEqual(first_weights["input_fingerprint"], second_weights["input_fingerprint"])

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

import unittest

import pandas as pd

from stock_analyzer import config
from stock_analyzer.prediction import build_stock_prediction


class StockPredictionTest(unittest.TestCase):
    def test_tomorrow_backup_is_visible_but_does_not_create_bullish_consensus(self):
        candidates = pd.DataFrame(
            [
                {
                    "code": "600001",
                    "name": "观察样本",
                    "price": 10.0,
                    "pct_chg": 2.0,
                    "turnover": 500000000,
                    "volume_ratio": 1.5,
                }
            ]
        )
        strategy_rows = {
            "short_term": [],
            "tomorrow_picks": [
                {
                    "code": "600001",
                    "score": 92.0,
                    "tier": "backup_pool",
                    "tier_label": "备选观察",
                    "execution_allowed": False,
                    "trade_action": {"action": "watch_only", "position_size": 0.0},
                    "serenity_profile": {
                        "quality_score": 90.0,
                        "rule_consistency_score": 90.0,
                        "risk_score": 20.0,
                    },
                }
            ],
            "swing_picks": [],
        }

        payload = build_stock_prediction(
            "600001",
            candidates,
            strategy_rows,
            strategy_metas={"tomorrow_picks": {"strategy_version": config.TOMORROW_STRATEGY_VERSION}},
            market_regime={"score": 80.0},
        )

        hit = payload["strategy_hits"][0]
        self.assertTrue(hit["observation_only"])
        self.assertEqual(hit["tier_label"], "备选观察")
        self.assertEqual(payload["consensus"]["appearances"], 0)
        self.assertEqual(payload["consensus"]["observation_appearances"], 1)
        self.assertEqual(payload["prediction"]["direction"], "neutral")
        self.assertEqual(payload["prediction"]["label"], "备选观察/不构成推荐")
        self.assertEqual(payload["prediction"]["rule_consistency"], 20.0)
        self.assertNotIn("confidence", payload["prediction"])
        self.assertNotIn("signal_coverage", payload["prediction"])
        self.assertEqual(hit["rule_consistency_score"], 90.0)
        self.assertNotIn("confidence_score", hit)
        self.assertNotIn("signal_coverage_score", hit)


if __name__ == "__main__":
    unittest.main()

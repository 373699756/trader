import os
import sqlite3
import tempfile
import unittest

import pandas as pd

from stock_analyzer.strategy_validation import StrategyValidationStore


class StanceTrackingTest(unittest.TestCase):
    def test_stock_prediction_stance_snapshot_updates_outcome_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "validation.sqlite3")
            store = StrategyValidationStore(db_path)
            saved = store.save_stock_prediction_snapshot(
                {
                    "code": "000001",
                    "name": "样本A",
                    "price": 10.0,
                    "optimization": {
                        "stance": "buy_trial",
                        "bias": "bullish",
                        "timing": "now",
                        "stop_loss_pct": 5.0,
                        "take_profit_pct": 8.0,
                        "trailing_stop_pct": 4.0,
                    },
                }
            )
            self.assertEqual(saved["saved"], 1)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE stock_prediction_snapshots SET prediction_date = ?, prediction_time = ? WHERE code = ?",
                    ("2026-01-01", "2026-01-01T15:00:00", "000001"),
                )

            update = store.update_stock_prediction_outcomes(_FakeProvider(), days=10)
            metrics = store.stance_metrics(days=10)

        self.assertEqual(update["updated"], 1)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["by_stance"]["buy_trial"]["sample_count"], 1)
        self.assertGreater(metrics["by_stance"]["buy_trial"]["avg_exit_return"], 0)


class _FakeProvider:
    def get_history(self, code, days=180):
        return pd.DataFrame(
            [
                {"trade_date": "2026-01-01", "open": 10.0, "high": 10.0, "low": 10.0, "price": 10.0},
                {"trade_date": "2026-01-02", "open": 10.0, "high": 10.9, "low": 9.9, "price": 10.8},
                {"trade_date": "2026-01-03", "open": 10.8, "high": 11.0, "low": 10.6, "price": 10.7},
            ]
        )


if __name__ == "__main__":
    unittest.main()

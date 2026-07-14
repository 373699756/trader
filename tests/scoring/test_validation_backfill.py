import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.strategy_validation import StrategyValidationStore
from stock_analyzer.validation_replay import backfill_strategy_validation_samples


def _validation_history(start_date: str, future_days: int, final_price: float) -> pd.DataFrame:
    dates = pd.date_range(start_date, periods=future_days + 1, freq="D").strftime("%Y%m%d").tolist()
    future_prices = [
        10 + (final_price - 10) * (idx + 1) / max(1, future_days)
        for idx in range(future_days)
    ]
    prices = [10] + future_prices
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": prices,
            "high": [price * 1.01 for price in prices],
            "low": [price * 0.99 for price in prices],
            "price": prices,
        }
    )


class ValidationBackfillTest(unittest.TestCase):
    def test_backfill_strategy_validation_samples_creates_replay_outcomes(self):
        dates = pd.date_range("2024-01-01", periods=70, freq="D").strftime("%Y%m%d").tolist()
        prices_a = [10 * (1.008 ** i) for i in range(70)]
        prices_b = [12 * (1.0075 ** i) for i in range(70)]
        histories = {
            "600001": pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": [value * 0.994 for value in prices_a],
                    "high": [value * 1.004 for value in prices_a],
                    "low": [value * 0.992 for value in prices_a],
                    "price": prices_a,
                    "turnover": [150000000 + i * 1000000 for i in range(70)],
                }
            ),
            "600002": pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": [value * 0.994 for value in prices_b],
                    "high": [value * 1.004 for value in prices_b],
                    "low": [value * 0.992 for value in prices_b],
                    "price": prices_b,
                    "turnover": [140000000 + i * 900000 for i in range(70)],
                }
            ),
        }

        class FakeProvider:
            def get_history(self, code, days=180):
                return histories[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            result = backfill_strategy_validation_samples(
                FakeProvider(),
                store,
                "tomorrow_picks",
                ["600001", "600002"],
                code_names={"600001": "样本A", "600002": "样本B"},
                days=90,
                replay_days=5,
                top_n=2,
                holding_days=3,
            )
            metrics = store.metrics("tomorrow_picks", days=20)
            dates_saved = store.list_signal_dates("tomorrow_picks")
            rows = store.signals_for_date(dates_saved[0]["signal_date"], "tomorrow_picks")

        self.assertTrue(result["ok"])
        self.assertEqual(result["date_count"], 5)
        self.assertEqual(result["saved"], 10)
        self.assertEqual(result["outcome"]["updated"], 10)
        self.assertEqual(metrics["sample_count"], 10)
        self.assertEqual(metrics["total_sample_count"], 10)
        self.assertEqual(metrics["backup_sample_count"], 0)
        self.assertEqual(
            rows[0]["strategy_version"],
            "tomorrow_picks_{}".format(config.VALIDATION_REPLAY_VERSION_SUFFIX),
        )
        self.assertTrue(rows[0]["raw"]["replay"])
        self.assertEqual(rows[0]["raw"]["replay_source"], "production_scorer")

    def test_backfill_rejects_intraday_observation_strategy(self):
        result = backfill_strategy_validation_samples(
            provider=None,
            validation_store=None,
            strategy_name="short_term",
            codes=[],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsupported_strategy")

    def test_backfill_samples_endpoint_grows_validation_sample_count(self):
        dates = pd.date_range("2024-01-01", periods=75, freq="D").strftime("%Y%m%d").tolist()
        prices = [10 * (1.008 ** i) for i in range(75)]
        history = pd.DataFrame(
            {
                "trade_date": dates,
                "open": [value * 0.994 for value in prices],
                "high": [value * 1.004 for value in prices],
                "low": [value * 0.992 for value in prices],
                "price": prices,
                "turnover": [150000000 + i * 900000 for i in range(75)],
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            StrategyValidationStore(validation_path).save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "样本", "price": 10, "score": 90}],
            )
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.prefetch_history",
                return_value={"requested": 1, "unique_codes": 1, "downloaded": 1, "cached": 0, "failed": 0, "errors": []},
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.get_history",
                return_value=history,
            ):
                response = create_app().test_client().post(
                    "/api/strategy-validation/backfill-samples?strategy=tomorrow_picks&days=120&replay_days=4&top_n=1"
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["replay"]["saved"], 4)
        self.assertEqual(payload["replay"]["outcome"]["updated"], 5)
        self.assertGreaterEqual(payload["metrics"]["sample_count"], 4)
        self.assertEqual(
            payload["replay"]["version"],
            "tomorrow_picks_{}".format(config.VALIDATION_REPLAY_VERSION_SUFFIX),
        )

    def test_strategy_validation_runtime_config_reports_baseline_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            StrategyValidationStore(validation_path).save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "待回填", "price": 10, "score": 90}],
            )
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ):
                response = create_app().test_client().get(
                    "/api/strategy-validation/runtime-config?strategy=tomorrow_picks&days=20"
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["validation_baseline_id"], payload["validation_baseline"]["baseline_id"])
        self.assertEqual(payload["baseline_status"]["status"], "needs_backfill")
        self.assertEqual(payload["baseline_status"]["signal_count"], 1)
        self.assertEqual(payload["baseline_status"]["pending_current_baseline_count"], 1)

    def test_backfill_current_baseline_endpoint_updates_mismatched_outcome(self):
        history = _validation_history("2024-01-01", future_days=3, final_price=10.6)

        class FakeProvider:
            def get_history(self, code, days=180):
                return history

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(validation_path)
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "旧口径", "price": 10, "score": 90}],
            )
            with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
                config, "ENABLE_MARKET_IMPACT", False
            ), patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", False):
                store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ), patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
                config, "ENABLE_MARKET_IMPACT", False
            ), patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", False), patch(
                "stock_analyzer.providers.MarketDataProvider.prefetch_history",
                return_value={"requested": 1, "unique_codes": 1, "downloaded": 0, "cached": 1, "failed": 0, "errors": []},
            ), patch(
                "stock_analyzer.providers.MarketDataProvider.get_history",
                return_value=history,
            ):
                response = create_app().test_client().post(
                    "/api/strategy-validation/backfill-current-baseline?strategy=tomorrow_picks&days=20&execute=1"
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["execute"])
        self.assertEqual(payload["before"]["status"], "needs_backfill")
        self.assertEqual(payload["candidates"]["candidate_count"], 1)
        self.assertEqual(payload["outcome"]["updated"], 1)
        self.assertFalse(payload["after"]["needs_backfill"])
        self.assertEqual(payload["after"]["current_baseline_outcome_count"], 1)


if __name__ == "__main__":
    unittest.main()

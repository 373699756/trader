import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer.execution_policy import build_execution_policy
from stock_analyzer.strategy_validation import StrategyValidationStore, _primary_return_config, validation_baseline_config
from stock_analyzer.validation_audit_cli import build_validation_readiness_report


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


class ValidationRepositoryRuntimeTest(unittest.TestCase):
    def test_validation_readiness_report_blocks_zero_oos_samples(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = "{}/validation.sqlite3".format(tmpdir)
            report = build_validation_readiness_report(path)

        self.assertFalse(report["ok"])
        self.assertEqual(report["table_counts"]["strategy_outcomes"], 0)
        self.assertEqual(report["table_counts"]["strategy_fold_predictions"], 0)
        self.assertEqual(report["readiness"]["real_oos_day_count"], 0)
        self.assertEqual(report["readiness"]["portfolio_day_count"], 0)
        self.assertEqual(report["readiness"]["deepseek_event_day_count"], 0)
        self.assertEqual(
            report["current_version_chains"]["tomorrow_picks"]["strategy_version"],
            config.TOMORROW_STRATEGY_VERSION,
        )
        self.assertEqual(
            report["current_version_chains"]["tomorrow_picks"]["signal_days"],
            0,
        )
        blocker_tasks = {item["task"] for item in report["blockers"]}
        self.assertIn("P0-CURRENT-VERSION-SAMPLE-CHAIN", blocker_tasks)
        self.assertIn("P3-REAL-OOS-SAMPLE-GATE", blocker_tasks)
        self.assertIn("P4-REBUILDABLE-RETURN-ARTIFACT", blocker_tasks)
        self.assertIn("P5-PORTFOLIO-ABLATION-EVIDENCE", blocker_tasks)
        self.assertIn("P6-DEEPSEEK-EVENT-COUNTERFACTUAL", blocker_tasks)
        self.assertIn("P7-GRAY-ROLLBACK", blocker_tasks)

    def test_validation_readiness_report_tracks_only_current_version_chain(self):
        class FakeProvider:
            def get_history(self, code, days=180):
                return _validation_history("2026-07-10", future_days=3, final_price=10.6)

            def get_execution_bars_raw(self, code, days=180):
                frame = self.get_history(code, days=days)
                frame.attrs["price_adjustment_mode"] = "raw"
                return frame

        with tempfile.TemporaryDirectory() as tmpdir:
            path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(path)
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2026-07-10T14:30:00",
                [{"rank": 1, "code": "600001", "name": "当前版本", "price": 10, "score": 80}],
                candidate_rows=[
                    {
                        "code": "600001",
                        "name": "当前版本",
                        "eligible": True,
                        "selected": True,
                        "rank": 1,
                        "score": 80,
                        "point_in_time_valid": True,
                    }
                ],
                execution_policy=build_execution_policy("tomorrow_picks"),
            )
            update = store.update_outcomes(
                FakeProvider(),
                signal_date="2026-07-10",
                strategy_name="tomorrow_picks",
            )

            report = build_validation_readiness_report(path)

        chain = report["current_version_chains"]["tomorrow_picks"]
        self.assertEqual(chain["batch_count"], 1)
        self.assertEqual(chain["nonempty_batch_count"], 1)
        self.assertEqual(chain["signal_days"], 1)
        self.assertEqual(chain["signal_count"], 1)
        self.assertEqual(chain["candidate_count"], 1)
        self.assertEqual(chain["selected_candidate_count"], 1)
        self.assertEqual(chain["production_baseline_match_count"], 1)
        self.assertEqual(update["updated"], 1)
        self.assertEqual(chain["execution_record_count"], 1)
        self.assertEqual(chain["outcome_count"], 1)
        self.assertEqual(chain["validation_baseline_match_count"], 1)
        self.assertEqual(chain["unknown_count"], 0)
        self.assertEqual(chain["promotion_eligible_count"], 0)
        self.assertEqual(chain["settled_promotion_days"], 0)
        self.assertEqual(chain["status"], "collecting")

    def test_strategy_validation_persists_fold_predictions_for_oos_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            first = store.save_fold_predictions(
                "EXP-1",
                "fold-1",
                "tomorrow_picks",
                [
                    {
                        "test_date": "2024-01-02",
                        "code": "600001",
                        "baseline_score": 88.0,
                        "predicted_net_return": 1.2,
                        "predicted_probability": 0.63,
                        "selected": True,
                        "actual_net_return": 0.9,
                        "extra": {"rank": 1},
                    },
                    {
                        "signal_date": "2024-01-02",
                        "code": "600002.SH",
                        "baseline_score": 70.0,
                        "predicted_net_return": -0.2,
                        "predicted_probability": 0.41,
                        "selected": False,
                        "actual_net_return": -0.8,
                    },
                ],
                baseline_id="baseline-v1",
                model_id="linear-net-v1",
                model_version="2024-01",
                train_end_date="2024-01-01",
                feature_schema_hash="features-v1",
            )
            second = store.save_fold_predictions(
                "EXP-1",
                "fold-1",
                "tomorrow_picks",
                [
                    {
                        "test_date": "2024-01-02",
                        "code": "600001",
                        "baseline_score": 88.0,
                        "predicted_net_return": 1.5,
                        "predicted_probability": 0.67,
                        "selected": True,
                        "actual_net_return": 1.1,
                    }
                ],
                baseline_id="baseline-v1",
                model_id="linear-net-v1",
                model_version="2024-01",
                train_end_date="2024-01-01",
                feature_schema_hash="features-v1",
            )
            rows = store.list_fold_predictions("EXP-1", strategy_name="tomorrow_picks", fold_id="fold-1")

        self.assertEqual(first["saved"], 2)
        self.assertEqual(second["saved"], 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertTrue(rows[0]["selected"])
        self.assertEqual(rows[0]["predicted_net_return"], 1.5)
        self.assertEqual(rows[0]["predicted_probability"], 0.67)
        self.assertEqual(rows[0]["baseline_id"], "baseline-v1")
        self.assertEqual(rows[0]["model_id"], "linear-net-v1")
        self.assertEqual(rows[0]["feature_schema_hash"], "features-v1")
        self.assertEqual(rows[0]["prediction"]["actual_net_return"], 1.1)
        self.assertEqual(rows[1]["code"], "600002")
        self.assertFalse(rows[1]["selected"])

    def test_strategy_validation_replaces_same_day_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            first = [
                {"rank": 1, "code": "600001", "name": "old", "price": 10, "score": 80},
                {"rank": 2, "code": "600002", "name": "replace", "price": 12, "score": 70},
            ]
            second = [{"rank": 1, "code": "600001", "name": "new", "price": 11, "score": 90}]

            store.save_signals("tomorrow_picks", "tomorrow_picks_v2", "2024-01-01T14:30:00", first)
            result = store.save_signals("tomorrow_picks", "tomorrow_picks_v3", "2024-01-01T14:31:00", second)
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
            with store.repository.connect() as conn:
                batches = conn.execute(
                    "SELECT strategy_version FROM strategy_signal_batches WHERE strategy_name = ? AND signal_date = ?",
                    ("tomorrow_picks", "2024-01-01"),
                ).fetchall()

        self.assertEqual(result["replaced"], 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertEqual(rows[0]["name"], "new")
        self.assertEqual(batches, [("tomorrow_picks_v3",)])

    def test_strategy_validation_reports_pending_outcomes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [
                    {
                        "rank": 1,
                        "code": "600001",
                        "name": "pending",
                        "market": "main",
                        "price": 10,
                        "pct_chg": 2,
                        "turnover": 200000000,
                        "volume_ratio": 1.5,
                        "turnover_rate": 3,
                        "sixty_day_pct": 8,
                        "ytd_pct": 10,
                        "score": 70,
                        "tier": "primary_watch",
                        "reasons": ["test"],
                    }
                ],
            )
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(metrics["signal_sample_count"], 1)
        self.assertEqual(metrics["pending_outcome_count"], 1)
        self.assertEqual(metrics["outcome_coverage_pct"], 0.0)

    def test_strategy_validation_uses_next_open_returns(self):
        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103", "20240104"],
                        "open": [10, 12, 12.5, 13],
                        "high": [10.5, 13, 13.2, 13.6],
                        "low": [9.8, 11.8, 12.0, 12.7],
                        "price": [10, 12.5, 13.0, 13.5],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 50.0):
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            execution_policy = build_execution_policy("tomorrow_picks")
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "sample", "price": 10, "score": 90}],
                execution_policy=execution_policy,
            )
            update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 1)
        self.assertAlmostEqual(rows[0]["signal_next_close_return"], 25.0)
        self.assertAlmostEqual(rows[0]["next_close_return"], 4.1667)
        self.assertAlmostEqual(metrics["avg_next_close_return"], 25.0)
        self.assertEqual(metrics["primary_return_field"], "signal_exit_return")
        self.assertEqual(metrics["validation_baseline_id"], metrics["validation_baseline"]["baseline_id"])

    def test_strategy_validation_persists_outcome_validation_baseline(self):
        class FakeProvider:
            def get_history(self, code, days=180):
                return _validation_history("2024-01-01", future_days=3, final_price=10.6)

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False
        ), patch.object(config, "ENABLE_MARKET_IMPACT", False), patch.object(
            config, "ENABLE_SURVIVORSHIP_CORRECTION", False
        ):
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "sample", "price": 10, "score": 90}],
            )
            store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
            metrics = store.metrics("tomorrow_picks", days=20)
            samples = store.live_weight_samples("tomorrow_picks", days=20)
            expected_baseline_id = validation_baseline_config("tomorrow_picks")["baseline_id"]

        self.assertEqual(rows[0]["validation_baseline_id"], expected_baseline_id)
        self.assertIn(expected_baseline_id, rows[0]["validation_baseline_json"])
        self.assertEqual(metrics["current_baseline_outcome_count"], 1)
        self.assertEqual(metrics["raw_outcome_sample_count"], 1)
        self.assertEqual(samples[0]["validation_baseline_id"], expected_baseline_id)
        self.assertEqual(rows[0]["stored_primary_return_field"], "signal_exit_return")

    def test_strategy_validation_excludes_mismatched_validation_baseline_until_backfilled(self):
        class FakeProvider:
            def get_history(self, code, days=180):
                return _validation_history("2024-01-01", future_days=3, final_price=10.6)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "legacy", "price": 10, "score": 90}],
            )
            with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
                config, "ENABLE_MARKET_IMPACT", False
            ), patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", False):
                legacy_update = store.update_outcomes(
                    FakeProvider(),
                    signal_date="2024-01-01",
                    strategy_name="tomorrow_picks",
                )
            with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
                config, "ENABLE_MARKET_IMPACT", False
            ), patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", False), patch.object(
                config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60
            ):
                metrics_before = store.metrics("tomorrow_picks", days=20)
                status_before = store.validation_baseline_status("tomorrow_picks", days=20)
                samples_before = store.live_weight_samples("tomorrow_picks", days=20)
                backfill_update = store.update_outcomes(
                    FakeProvider(),
                    strategy_name="tomorrow_picks",
                    only_incomplete=True,
                )
                metrics_after = store.metrics("tomorrow_picks", days=20)
                status_after = store.validation_baseline_status("tomorrow_picks", days=20)
                rows_after = store.signals_for_date("2024-01-01", "tomorrow_picks")

        self.assertEqual(legacy_update["updated"], 1)
        self.assertEqual(metrics_before["sample_count"], 0)
        self.assertEqual(metrics_before["excluded_baseline_mismatch_count"], 1)
        self.assertEqual(status_before["status"], "needs_backfill")
        self.assertEqual(len(samples_before), 0)
        self.assertEqual(backfill_update["updated"], 1)
        self.assertEqual(metrics_after["sample_count"], 1)
        self.assertFalse(status_after["needs_backfill"])
        self.assertIn("__tail__", rows_after[0]["validation_baseline_id"])

    def test_cost_policy_baseline_change_requires_new_compatible_outcome(self):
        class FakeProvider:
            def __init__(self):
                self.calls = []

            def get_history(self, code, days=180):
                self.calls.append(code)
                return _validation_history("2024-01-01", future_days=6, final_price=10.6)

            def get_execution_history(self, code, days=180):
                frame = self.get_history(code, days=days)
                frame.attrs["price_adjustment_mode"] = "raw"
                return frame

        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 30.0
        ):
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            frozen_policy = build_execution_policy("tomorrow_picks")
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "sample", "price": 10, "score": 90}],
                execution_policy=frozen_policy,
            )
            store.update_outcomes(
                provider,
                signal_date="2024-01-01",
                strategy_name="tomorrow_picks",
            )
            stored_baseline_id = store.signals_for_date("2024-01-01", "tomorrow_picks")[0][
                "validation_baseline_id"
            ]
            provider.calls.clear()
            with patch.object(config, "VALIDATION_TRADE_COST_PCT", 0.35):
                current_baseline_id = validation_baseline_config("tomorrow_picks")["baseline_id"]
                metrics = store.metrics("tomorrow_picks", days=20)
                status = store.validation_baseline_status("tomorrow_picks", days=20)
                candidates = store.validation_baseline_backfill_candidates("tomorrow_picks", days=20)
                samples = store.live_weight_samples("tomorrow_picks", days=20)
                update = store.update_outcomes(
                    provider,
                    strategy_name="tomorrow_picks",
                    codes=["600001"],
                    only_incomplete=True,
                )

        self.assertNotEqual(stored_baseline_id, current_baseline_id)
        self.assertEqual(metrics["sample_count"], 0)
        self.assertEqual(metrics["excluded_baseline_mismatch_count"], 1)
        self.assertEqual(status["current_baseline_outcome_count"], 0)
        self.assertTrue(status["needs_backfill"])
        self.assertEqual(candidates["candidate_count"], 1)
        self.assertEqual(len(samples), 0)
        self.assertEqual(update["updated"], 1)
        self.assertTrue(provider.calls)

    def test_strategy_validation_stale_no_future_sample_is_unknown_without_delisting_evidence(self):
        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101"],
                        "open": [10.0],
                        "high": [10.2],
                        "low": [9.8],
                        "price": [10.0],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T15:00:00",
                [{"rank": 1, "code": "600001", "name": "stale", "price": 10, "score": 90}],
            )
            with patch.object(config, "ENABLE_SURVIVORSHIP_CORRECTION", True), patch.object(
                config, "SURVIVORSHIP_CORRECTION_STALE_DAYS", 0
            ):
                update = store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
                rows = store.signals_for_date("2024-01-01", "tomorrow_picks")
                metrics = store.metrics("tomorrow_picks", days=20)

        self.assertEqual(update["updated"], 0)
        self.assertEqual(update["unknown"], 1)
        self.assertEqual(rows[0]["label_status"], "unknown")
        self.assertIsNone(rows[0]["gross_return_pct"])
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertEqual(metrics["sample_count"], 0)

    def test_validation_execution_cost_uses_liquidity_tail_auction_and_market_impact(self):
        from stock_analyzer.strategy_validation import _execution_cost_pct, market_impact_cost_pct, tail_auction_slippage_pct

        liquid = _execution_cost_pct({"strategy_name": "tomorrow_picks", "turnover": 1_500_000_000})
        illiquid = _execution_cost_pct({"strategy_name": "tomorrow_picks", "turnover": 50_000_000})

        self.assertGreater(illiquid, liquid)
        self.assertGreater(illiquid, config.VALIDATION_TRADE_COST_PCT)

        row = {"strategy_name": "tomorrow_picks", "turnover": 100_000_000, "adv_20d": 100_000_000, "suggested_weight": 10}
        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
            config, "ENABLE_MARKET_IMPACT", False
        ):
            baseline = _execution_cost_pct(row)
        with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", True), patch.object(
            config, "TAIL_AUCTION_LIQUIDITY_RATIO", 0.05
        ), patch.object(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.8), patch.object(
            config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000
        ), patch.object(
            config, "ENABLE_MARKET_IMPACT", False
        ):
            adjusted = _execution_cost_pct(row)
            tail_with_base = tail_auction_slippage_pct(row, base_slippage=0.2)
        with patch.object(config, "ENABLE_MARKET_IMPACT", True), patch.object(
            config, "VALIDATION_PORTFOLIO_CAPITAL", 10_000_000
        ), patch.object(config, "MARKET_IMPACT_COEFFICIENT", 0.1), patch.object(
            config, "MARKET_IMPACT_MAX_COST_PCT", 5.0
        ), patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False):
            impact = market_impact_cost_pct(row)
            impact_adjusted = _execution_cost_pct(row)

        self.assertGreater(adjusted, baseline)
        self.assertGreater(tail_with_base, 0.2)
        self.assertGreater(impact, 0)
        self.assertAlmostEqual(impact_adjusted, baseline + impact)

    def test_tomorrow_primary_return_config_uses_close_auction_overnight(self):
        column, days, horizon = _primary_return_config("tomorrow_picks")
        self.assertEqual(column, "signal_exit_return")
        self.assertEqual(days, 1)
        self.assertEqual(horizon, "T日14:30后参考入场至T+1规则退出")
        with patch.object(config, "VALIDATION_PRIMARY_ENTRY_MODE", "signal"):
            column_signal, days_signal, horizon_signal = _primary_return_config("tomorrow_picks")
            self.assertEqual(column_signal, "signal_exit_return")
            self.assertEqual(days_signal, 1)
            self.assertEqual(horizon_signal, "T日14:30后参考入场至T+1规则退出")

if __name__ == "__main__":
    unittest.main()

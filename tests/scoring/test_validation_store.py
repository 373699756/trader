import tempfile
import unittest

from stock_analyzer import config
from stock_analyzer.snapshot_phase import CLOSE_FALLBACK, PRECLOSE_TRADEABLE
from stock_analyzer.strategy_validation import StrategyValidationStore


class ValidationStoreTest(unittest.TestCase):
    def test_strategy_validation_crud_lists_and_reads_saved_signals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            result = store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2026-07-08T15:00:00",
                [
                    {
                        "rank": 2,
                        "code": "600001",
                        "name": "sample A",
                        "market": "main",
                        "theme": "semiconductor",
                        "price": 10,
                        "pct_chg": 2.0,
                        "turnover": 200000000,
                        "volume_ratio": 1.5,
                        "turnover_rate": 3.0,
                        "sixty_day_pct": 8.0,
                        "ytd_pct": 10.0,
                        "score": 80,
                        "reasons": ["reason A"],
                    },
                    {
                        "rank": 1,
                        "code": "600002",
                        "name": "sample B",
                        "market": "main",
                        "theme": "compute",
                        "price": 12,
                        "score": 90,
                        "reasons": ["reason B"],
                    },
                ],
            )
            dates = store.list_signal_dates("tomorrow_picks")
            rows = store.signals_for_date("2026-07-08", "tomorrow_picks")
            latest_rows = store.latest_signal_rows("tomorrow_picks")
            existing_dates = store.existing_validation_dates("tomorrow_picks")

        self.assertEqual(result["saved"], 2)
        self.assertEqual(result["replaced"], 0)
        self.assertEqual(dates[0]["signal_date"], "2026-07-08")
        self.assertEqual(dates[0]["count"], 2)
        self.assertEqual(dates[0]["sample_type"], "real")
        self.assertEqual(existing_dates, ["2026-07-08"])
        self.assertEqual([row["code"] for row in rows], ["600002", "600001"])
        self.assertEqual([row["code"] for row in latest_rows], ["600002", "600001"])
        self.assertEqual(rows[0]["strategy_version"], config.TOMORROW_STRATEGY_VERSION)
        self.assertEqual(rows[0]["raw"]["theme"], "compute")

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

        self.assertEqual(result["replaced"], 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "600001")
        self.assertEqual(rows[0]["name"], "new")

    def test_strategy_validation_keeps_same_day_phase_snapshots_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            version = "tomorrow_phase_v1"
            store.save_signals(
                "tomorrow_picks",
                version,
                "2026-07-08T14:30:00",
                [{"rank": 1, "code": "600001", "name": "盘中", "price": 10, "score": 80}],
                batch_metadata={"snapshot_phase": PRECLOSE_TRADEABLE},
            )
            store.save_signals(
                "tomorrow_picks",
                version,
                "2026-07-08T15:01:00",
                [{"rank": 1, "code": "600001", "name": "收盘", "price": 11, "score": 85}],
                batch_metadata={"snapshot_phase": CLOSE_FALLBACK},
            )

            preclose = store.latest_signal_rows(
                "tomorrow_picks",
                signal_date="2026-07-08",
                snapshot_phase=PRECLOSE_TRADEABLE,
            )
            close = store.latest_signal_rows(
                "tomorrow_picks",
                signal_date="2026-07-08",
                snapshot_phase=CLOSE_FALLBACK,
            )
            dates = store.list_signal_dates("tomorrow_picks")
            preferred = store.saved_signal_batch("tomorrow_picks", "2026-07-08")

        self.assertEqual(preclose[0]["name"], "盘中")
        self.assertEqual(close[0]["name"], "收盘")
        self.assertEqual([row["snapshot_phase"] for row in dates], [CLOSE_FALLBACK, PRECLOSE_TRADEABLE])
        self.assertEqual(preferred["snapshot_phase"], PRECLOSE_TRADEABLE)

    def test_strategy_validation_records_empty_saved_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            result = store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-08T15:00:00",
                [],
            )
            dates = store.list_signal_dates("tomorrow_picks")
            rows = store.signals_for_date("2026-07-08", "tomorrow_picks")
            latest_rows = store.latest_signal_rows("tomorrow_picks")

        self.assertEqual(result["saved"], 0)
        self.assertEqual(dates[0]["signal_date"], "2026-07-08")
        self.assertEqual(dates[0]["count"], 0)
        self.assertEqual(dates[0]["sample_type"], "empty")
        self.assertEqual(rows, [])
        self.assertEqual(latest_rows, [])

    def test_latest_signal_rows_ignores_newer_replay_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2026-07-08T15:00:00",
                [{"rank": 1, "code": "600001", "name": "formal", "price": 10, "score": 80}],
            )
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_{}".format(config.VALIDATION_REPLAY_VERSION_SUFFIX),
                "2026-07-09T15:00:00",
                [{"rank": 1, "code": "600002", "name": "replay", "price": 10, "score": 80}],
            )

            latest_rows = store.latest_signal_rows("tomorrow_picks")
            dates = store.list_signal_dates("tomorrow_picks")

        self.assertEqual([row["code"] for row in latest_rows], ["600001"])
        self.assertEqual(latest_rows[0]["strategy_version"], config.TOMORROW_STRATEGY_VERSION)
        self.assertEqual(dates[0]["sample_type"], "replay")
        self.assertEqual(dates[1]["sample_type"], "real")

    def test_strategy_validation_prunes_inactive_strategies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v5",
                "2026-07-08T15:00:00",
                [{"rank": 1, "code": "600001", "name": "keep", "price": 10, "score": 80}],
            )
            store.save_signals(
                "position_picks",
                "position_1_3m_v1",
                "2026-07-08T15:00:00",
                [{"rank": 1, "code": "600002", "name": "delete", "price": 11, "score": 70}],
            )
            result = store.prune_strategies(("today_term", "tomorrow_picks", "swing_picks"))
            active_dates = store.list_signal_dates("tomorrow_picks")
            inactive_dates = store.list_signal_dates("position_picks")

        self.assertEqual(result["deleted_signals"], 1)
        self.assertGreaterEqual(result["deleted_batches"], 1)
        self.assertEqual(active_dates[0]["count"], 1)
        self.assertEqual(inactive_dates, [])

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

    def test_strategy_validation_signal_codes_groups_saved_predictions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StrategyValidationStore("{}/validation.sqlite3".format(tmpdir))
            store.save_signals(
                "tomorrow_picks",
                "tomorrow_picks_v2",
                "2024-01-01T14:30:00",
                [
                    {"rank": 2, "code": "600001", "name": "sample A", "price": 10, "score": 80},
                    {"rank": 1, "code": "600002", "name": "sample B", "price": 12, "score": 90},
                ],
            )
            store.save_signals(
                "swing_picks",
                "swing_5_10d_v1",
                "2024-01-02T14:30:00",
                [{"rank": 1, "code": "600001", "name": "sample A", "price": 11, "score": 85}],
            )
            rows = store.signal_codes(strategy_name="tomorrow_picks")
            all_rows = store.signal_codes()

        self.assertEqual([row["code"] for row in rows], ["600002", "600001"])
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(all_rows[0]["code"], "600001")
        self.assertEqual(all_rows[0]["signal_count"], 2)


if __name__ == "__main__":
    unittest.main()

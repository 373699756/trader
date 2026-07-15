import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_analyzer import config
from stock_analyzer import validation_runtime_support as support
from stock_analyzer.app import create_app
from stock_analyzer.oos_report import build_strategy_oos_report
from stock_analyzer.strategy_validation import StrategyValidationStore


class ValidationOosTest(unittest.TestCase):
    def test_strategy_validation_oos_report_passes_ready_current_baseline(self):
        class FakeProvider:
            def get_history(self, code, days=180):
                return pd.DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103"],
                        "open": [10.0, 10.0, 10.8],
                        "high": [10.2, 11.2, 11.3],
                        "low": [9.8, 9.9, 10.7],
                        "price": [10.0, 11.0, 11.2],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(validation_path)
            store.save_signals(
                "tomorrow_picks",
                config.TOMORROW_STRATEGY_VERSION,
                "2024-01-01T14:30:00",
                [{"rank": 1, "code": "600001", "name": "OOS样本", "price": 10, "score": 90}],
            )
            store.update_outcomes(FakeProvider(), signal_date="2024-01-01", strategy_name="tomorrow_picks")
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ), patch.object(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 1), patch.object(
                config, "STRATEGY_DECAY_MIN_REAL_DAYS", 1
            ), patch.object(config, "STRATEGY_VALIDATION_REQUIRE_POSITIVE_CI", False):
                response = create_app().test_client().get(
                    "/api/strategy-validation/oos-report?strategy=tomorrow_picks&days=20"
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["oos_status"], "gate_blocked")
        self.assertFalse(payload["can_promote"])
        self.assertTrue(payload["baseline_status"]["oos_ready"])
        self.assertFalse(payload["validation_gate"]["validated"])
        self.assertGreater(payload["summary"]["avg_primary_return_net"], 0)

    def test_strategy_oos_report_builder_covers_empty_backfill_passed_and_blocked(self):
        base_metrics = {
            "validation_baseline": {"baseline_id": "baseline-v1"},
            "validation_baseline_id": "baseline-v1",
            "sample_count": 8,
            "outcome_sample_count": 8,
            "real_day_count": 8,
            "avg_primary_return_net": 1.2,
            "real_avg_primary_return_net": 1.1,
            "real_portfolio_max_drawdown_pct": -2.0,
        }
        ready = {
            "validation_baseline_id": "baseline-v1",
            "needs_backfill": False,
            "oos_ready": True,
            "min_oos_days": 1,
        }

        empty = build_strategy_oos_report("tomorrow_picks", 20, {}, {"needs_backfill": False}, {"blocked": True})
        backfill = build_strategy_oos_report(
            "tomorrow_picks",
            20,
            base_metrics,
            {**ready, "needs_backfill": True},
            {"blocked": False},
        )
        passed = build_strategy_oos_report(
            "tomorrow_picks",
            20,
            base_metrics,
            ready,
            {"blocked": False},
            portfolio_baseline={
                "day_count": 8,
                "groups": {
                    "frozen_rule_top_k": {
                        "total_return_pct": 1.5,
                        "avg_daily_net_return_ci95_low": 0.1,
                    }
                },
            },
        )
        blocked = build_strategy_oos_report("tomorrow_picks", 20, base_metrics, ready, {"blocked": True})
        portfolio_blocked = build_strategy_oos_report(
            "tomorrow_picks",
            20,
            base_metrics,
            ready,
            {"blocked": False},
            portfolio_baseline={
                "day_count": 3,
                "groups": {"frozen_rule_top_k": {"total_return_pct": -0.5}},
            },
        )
        portfolio_ci_blocked = build_strategy_oos_report(
            "tomorrow_picks",
            20,
            base_metrics,
            ready,
            {"blocked": False},
            portfolio_baseline={
                "day_count": 8,
                "groups": {
                    "frozen_rule_top_k": {
                        "total_return_pct": 1.5,
                        "avg_daily_net_return_ci95_low": -0.1,
                    }
                },
            },
        )

        self.assertEqual(empty["oos_status"], "empty")
        self.assertFalse(empty["can_promote"])
        self.assertEqual(empty["readiness"]["ready_oos_day_count"], 0)
        self.assertTrue(empty["readiness"]["blocked_by_real_oos_days"])
        self.assertEqual(empty["blockers"][0]["code"], "real_oos_days_insufficient")
        self.assertEqual(backfill["oos_status"], "needs_backfill")
        self.assertEqual(backfill["blockers"][0]["code"], "current_baseline_backfill_required")
        self.assertEqual(passed["oos_status"], "oos_passed")
        self.assertTrue(passed["can_promote"])
        self.assertFalse(passed["production_eligible"])
        self.assertEqual(passed["promotion_stage"], "shadow_eligible")
        self.assertEqual(passed["blockers"], [])
        self.assertEqual(blocked["oos_status"], "gate_blocked")
        self.assertFalse(blocked["can_promote"])
        self.assertEqual(blocked["blockers"][0]["code"], "validation_gate_blocked")
        self.assertEqual(portfolio_blocked["oos_status"], "portfolio_blocked")
        self.assertFalse(portfolio_blocked["can_promote"])
        self.assertEqual(portfolio_blocked["blockers"][0]["code"], "portfolio_baseline_blocked")
        self.assertEqual(portfolio_ci_blocked["oos_status"], "portfolio_blocked")
        self.assertFalse(portfolio_ci_blocked["can_promote"])

    def test_strategy_oos_report_history_persists_snapshots(self):
        report = build_strategy_oos_report(
            "tomorrow_picks",
            20,
            {
                "validation_baseline": {"baseline_id": "baseline-v1"},
                "validation_baseline_id": "baseline-v1",
                "sample_count": 3,
                "outcome_sample_count": 3,
                "real_day_count": 3,
                "avg_primary_return_net": 0.8,
                "real_avg_primary_return_net": 0.8,
                "real_avg_primary_return_net_ci95_low": 0.1,
                "real_avg_primary_return_net_ci95_high": 1.5,
                "real_portfolio_max_drawdown_pct": -1.0,
            },
            {
                "validation_baseline_id": "baseline-v1",
                "needs_backfill": False,
                "oos_ready": True,
                "min_oos_days": 1,
            },
            {"blocked": False, "validated": True},
            generated_at="2024-01-10T15:30:00",
            portfolio_baseline={
                "day_count": 3,
                "groups": {
                    "frozen_rule_top_k": {
                        "total_return_pct": 1.0,
                        "avg_daily_net_return_ci95_low": 0.1,
                    }
                },
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = "{}/validation.sqlite3".format(tmpdir)
            store = StrategyValidationStore(validation_path)
            saved = store.save_oos_report(report, trigger="auto_update")
            history = store.list_oos_reports("tomorrow_picks")
            with patch.object(config, "VALIDATION_DB_PATH", validation_path), patch.object(
                config, "STATE_PATH", "{}/state.json".format(tmpdir)
            ):
                response = create_app().test_client().get(
                    "/api/strategy-validation/oos-report/history?strategy=tomorrow_picks&limit=5"
                )

        payload = response.get_json()
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(history[0]["oos_status"], "oos_passed")
        self.assertEqual(history[0]["baseline_id"], "baseline-v1")
        self.assertEqual(history[0]["report"]["summary"]["sample_count"], 3)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reports"][0]["oos_status"], "oos_passed")

    def test_run_validation_auto_update_once_resets_statuses_on_failure(self):
        auto_update_status = {"running": False, "last_started_at": "", "last_error": "", "last_result": {}}
        updates = []

        def set_auto_update_status(**values):
            auto_update_status.update(values)
            updates.append(values)

        result = support.run_validation_auto_update_once(
            auto_update_lock=type("L", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False})(),
            auto_update_status=auto_update_status,
            set_auto_update_status=set_auto_update_status,
            run_validation_outcome_update_once_fn=lambda: {"ok": False, "status": "boom"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(auto_update_status["running"], False)
        self.assertEqual(auto_update_status["last_error"], "boom")
        self.assertEqual(auto_update_status["last_result"]["error"], "boom")
        self.assertTrue(updates)

    def test_run_validation_auto_update_once_writes_result_and_oos_status_alerts(self):
        auto_update_status = {"running": False}

        result = support.run_validation_auto_update_once(
            auto_update_lock=type("L", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False})(),
            auto_update_status=auto_update_status,
            set_auto_update_status=lambda **values: auto_update_status.update(values),
            run_validation_outcome_update_once_fn=lambda: {
                "ok": True,
                "updates": [{"strategy": "tomorrow_picks", "result": {"updated": 2}}],
            },
            run_oos_reports_once_fn=lambda: {
                "ok": True,
                "reports": [
                    {"strategy": "empty_picks", "report": {"oos_status": "empty"}},
                    {"strategy": "young_picks", "report": {"oos_status": "insufficient_oos_days"}},
                    {"strategy": "tomorrow_picks", "report": {"oos_status": "needs_backfill"}},
                    {"strategy": "swing_picks", "report": {"oos_status": "gate_blocked"}},
                    {"strategy": "today_term", "report": {"oos_status": "oos_passed"}},
                ],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "outcome_update")
        self.assertEqual(result["status"], "oos_attention_required")
        self.assertEqual(result["summary"]["updated"], 2)
        self.assertEqual(result["oos_summary"]["needs_backfill_count"], 1)
        self.assertEqual(result["oos_summary"]["empty_count"], 1)
        self.assertEqual(result["oos_summary"]["insufficient_oos_days_count"], 1)
        self.assertEqual(result["oos_summary"]["gate_blocked_count"], 1)
        self.assertEqual(result["oos_summary"]["attention_count"], 4)
        self.assertEqual(len(result["alerts"]), 4)
        self.assertEqual(auto_update_status["last_result"]["status"], "oos_attention_required")
        self.assertEqual(auto_update_status["last_oos_summary"]["gate_blocked_count"], 1)
        self.assertEqual(len(auto_update_status["last_oos_alerts"]), 4)


if __name__ == "__main__":
    unittest.main()

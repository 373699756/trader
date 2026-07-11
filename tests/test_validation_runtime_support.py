import unittest
from unittest.mock import patch

from stock_analyzer import validation_runtime_support as support


class ValidationRuntimeSupportTest(unittest.TestCase):
    def test_configured_auto_snapshot_strategies_no_longer_falls_back_to_auto_update_strategies(self):
        with patch.object(support.config, "VALIDATION_AUTO_SNAPSHOT_STRATEGIES", ""), patch.object(
            support.config,
            "VALIDATION_AUTO_UPDATE_STRATEGIES",
            "tomorrow_picks",
            create=True,
        ):
            strategies = support.configured_auto_snapshot_strategies(
                default_snapshot_strategies=("short_term", "tomorrow_picks"),
                snapshot_strategies=("short_term", "tomorrow_picks"),
            )

        self.assertEqual(strategies, ["short_term", "tomorrow_picks"])

    def test_default_auto_snapshot_strategies_include_intraday_observation(self):
        with patch.object(support.config, "VALIDATION_AUTO_SNAPSHOT_STRATEGIES", ""):
            strategies = support.configured_auto_snapshot_strategies(
                default_snapshot_strategies=("short_term", "tomorrow_picks", "swing_picks"),
                snapshot_strategies=("short_term", "tomorrow_picks", "swing_picks"),
            )

        self.assertEqual(strategies, ["short_term", "tomorrow_picks", "swing_picks"])

    def test_explicit_auto_snapshot_strategy_override_is_respected(self):
        with patch.object(support.config, "VALIDATION_AUTO_SNAPSHOT_STRATEGIES", "tomorrow_picks"):
            strategies = support.configured_auto_snapshot_strategies(
                default_snapshot_strategies=("short_term", "tomorrow_picks", "swing_picks"),
                snapshot_strategies=("short_term", "tomorrow_picks", "swing_picks"),
            )

        self.assertEqual(strategies, ["tomorrow_picks"])

    def test_run_validation_auto_update_once_resets_statuses_on_failure(self):
        auto_update_status = {"running": False, "last_started_at": "", "last_error": "", "last_result": {}}
        update_calls = []

        def set_auto_update_status(**values):
            auto_update_status.update(values)
            update_calls.append(values)

        result = support.run_validation_auto_update_once(
            auto_update_lock=type("L", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False})(),
            auto_update_status=auto_update_status,
            set_auto_update_status=set_auto_update_status,
            run_validation_outcome_update_once_fn=lambda: {"ok": False, "status": "boom"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(auto_update_status["running"], False)
        self.assertTrue(update_calls)

    def test_run_validation_auto_update_once_runs_outcome_update_not_snapshot(self):
        auto_update_status = {"running": False}

        result = support.run_validation_auto_update_once(
            auto_update_lock=type("L", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False})(),
            auto_update_status=auto_update_status,
            set_auto_update_status=lambda **values: auto_update_status.update(values),
            run_validation_outcome_update_once_fn=lambda: {
                "ok": True,
                "updates": [{"strategy": "tomorrow_picks", "result": {"updated": 2}}],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "outcome_update")
        self.assertEqual(result["updates"][0]["result"]["updated"], 2)

    def test_run_validation_auto_update_once_summarizes_oos_alerts(self):
        auto_update_status = {"running": False}

        result = support.run_validation_auto_update_once(
            auto_update_lock=type("L", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False})(),
            auto_update_status=auto_update_status,
            set_auto_update_status=lambda **values: auto_update_status.update(values),
            run_validation_outcome_update_once_fn=lambda: {"ok": True, "updates": []},
            run_oos_reports_once_fn=lambda: {
                "ok": True,
                "reports": [
                    {"strategy": "tomorrow_picks", "report": {"oos_status": "needs_backfill"}},
                    {"strategy": "swing_picks", "report": {"oos_status": "gate_blocked"}},
                    {"strategy": "short_term", "report": {"oos_status": "oos_passed"}},
                ],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "oos_attention_required")
        self.assertEqual(result["oos_summary"]["needs_backfill_count"], 1)
        self.assertEqual(result["oos_summary"]["gate_blocked_count"], 1)
        self.assertEqual(len(result["alerts"]), 2)
        self.assertEqual(auto_update_status["last_oos_summary"]["gate_blocked_count"], 1)
        self.assertEqual(len(auto_update_status["last_oos_alerts"]), 2)

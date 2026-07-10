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
        auto_snapshot_status = {"running": True, "last_error": "", "last_result": {}}
        update_calls = []
        snapshot_calls = []

        def set_auto_update_status(**values):
            auto_update_status.update(values)
            update_calls.append(values)

        def set_auto_snapshot_status(**values):
            auto_snapshot_status.update(values)
            snapshot_calls.append(values)

        result = support.run_validation_auto_update_once(
            auto_update_lock=type("L", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False})(),
            auto_update_status=auto_update_status,
            auto_snapshot_status=auto_snapshot_status,
            set_auto_update_status=set_auto_update_status,
            set_auto_snapshot_status=set_auto_snapshot_status,
            run_validation_auto_snapshot_once_fn=lambda: {"ok": False, "status": "boom"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(auto_update_status["running"], False)
        self.assertEqual(auto_snapshot_status["running"], False)
        self.assertTrue(update_calls)
        self.assertTrue(snapshot_calls)

import unittest
from datetime import datetime
from unittest.mock import patch

from stock_analyzer import validation_runtime_support as support


class ValidationRuntimeSupportTest(unittest.TestCase):
    def test_tuning_run_is_reused_until_semantic_inputs_change(self):
        class TuningStore:
            def __init__(self):
                self.latest = {}
                self.saved_plans = []

            def list_signal_dates(self, strategy):
                return [{"signal_date": "2026-07-14", "count": 5}]

            def latest_tuning_run(self, strategy):
                return self.latest

            def save_tuning_run(self, strategy, days, plan, metrics):
                saved = {"id": len(self.saved_plans) + 1, "run_time": plan["generated_at"]}
                self.saved_plans.append(plan)
                self.latest = {**saved, "strategy_name": strategy, "plan": plan}
                return saved

            def save_or_reuse_tuning_run(self, strategy, days, plan, metrics):
                latest_plan = self.latest.get("plan") or {}
                reused = bool(
                    plan.get("input_fingerprint")
                    and plan.get("input_fingerprint") == latest_plan.get("input_fingerprint")
                )
                if reused:
                    saved = {"id": self.latest["id"], "run_time": self.latest["run_time"]}
                else:
                    saved = self.save_tuning_run(strategy, days, plan, metrics)
                return {"reused": reused, "saved": saved, "run": self.latest}

        metrics = {
            "day_count": 60,
            "sample_count": 100,
            "outcome_sample_count": 100,
            "real_day_count": 60,
            "pending_outcome_count": 0,
            "unknown_outcome_count": 0,
            "real_win_rate_primary_net": 55.0,
            "real_avg_primary_return_net": 0.6,
            "real_avg_primary_return_net_ci95_low": 0.1,
            "real_avg_max_drawdown_primary": -2.0,
        }
        store = TuningStore()
        load_metrics = lambda strategy, days: dict(metrics)

        first = support.run_validation_tuning_once(store, load_metrics, ["tomorrow_picks"], days=60)
        second = support.run_validation_tuning_once(store, load_metrics, ["tomorrow_picks"], days=60)
        metrics["sample_count"] = 101
        sample_changed = support.run_validation_tuning_once(store, load_metrics, ["tomorrow_picks"], days=60)
        metrics["real_avg_primary_return_net_ci95_low"] = 0.2
        metric_changed = support.run_validation_tuning_once(store, load_metrics, ["tomorrow_picks"], days=60)

        self.assertFalse(first["runs"][0]["reused"])
        self.assertTrue(second["runs"][0]["reused"])
        self.assertEqual(first["runs"][0]["saved"]["id"], second["runs"][0]["saved"]["id"])
        self.assertFalse(sample_changed["runs"][0]["reused"])
        self.assertNotEqual(
            first["runs"][0]["input_fingerprint"],
            sample_changed["runs"][0]["input_fingerprint"],
        )
        self.assertFalse(metric_changed["runs"][0]["reused"])
        self.assertNotEqual(
            sample_changed["runs"][0]["input_fingerprint"],
            metric_changed["runs"][0]["input_fingerprint"],
        )
        self.assertEqual(len(store.saved_plans), 3)

    def test_auto_snapshot_retry_is_clamped_to_freeze_cutoff(self):
        regular = support.auto_snapshot_retry_schedule(
            datetime(2026, 7, 14, 14, 45, 0),
            "14:50",
            60,
        )
        final = support.auto_snapshot_retry_schedule(
            datetime(2026, 7, 14, 14, 49, 30),
            "14:50",
            60,
        )
        missed = support.auto_snapshot_retry_schedule(
            datetime(2026, 7, 14, 14, 50, 0),
            "14:50",
            60,
        )

        self.assertTrue(regular["retry"])
        self.assertEqual(regular["wait_seconds"], 60)
        self.assertEqual(final["next_run_at"], datetime(2026, 7, 14, 14, 50, 0))
        self.assertEqual(final["wait_seconds"], 30)
        self.assertFalse(missed["retry"])
        self.assertTrue(missed["deadline_missed"])

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

        self.assertEqual(strategies, ["tomorrow_picks", "short_term"])

    def test_default_auto_snapshot_strategies_include_intraday_observation(self):
        with patch.object(support.config, "VALIDATION_AUTO_SNAPSHOT_STRATEGIES", ""):
            strategies = support.configured_auto_snapshot_strategies(
                default_snapshot_strategies=("short_term", "tomorrow_picks", "swing_picks"),
                snapshot_strategies=("short_term", "tomorrow_picks", "swing_picks"),
            )

        self.assertEqual(strategies, ["tomorrow_picks", "short_term", "swing_picks"])

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
                    {
                        "strategy": "empty_picks",
                        "report": {
                            "oos_status": "empty",
                            "blockers": [{"code": "real_oos_days_insufficient"}],
                            "readiness": {"missing_oos_day_count": 60},
                        },
                    },
                    {"strategy": "young_picks", "report": {"oos_status": "insufficient_oos_days"}},
                    {"strategy": "tomorrow_picks", "report": {"oos_status": "needs_backfill"}},
                    {"strategy": "swing_picks", "report": {"oos_status": "gate_blocked"}},
                    {"strategy": "short_term", "report": {"oos_status": "oos_passed"}},
                ],
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "oos_attention_required")
        self.assertEqual(result["oos_summary"]["empty_count"], 1)
        self.assertEqual(result["oos_summary"]["insufficient_oos_days_count"], 1)
        self.assertEqual(result["oos_summary"]["needs_backfill_count"], 1)
        self.assertEqual(result["oos_summary"]["gate_blocked_count"], 1)
        self.assertEqual(result["oos_summary"]["attention_count"], 4)
        self.assertEqual(result["oos_summary"]["statuses"][0]["blockers"][0]["code"], "real_oos_days_insufficient")
        self.assertEqual(result["oos_summary"]["statuses"][0]["readiness"]["missing_oos_day_count"], 60)
        self.assertEqual(len(result["alerts"]), 4)
        self.assertEqual(auto_update_status["last_oos_summary"]["gate_blocked_count"], 1)
        self.assertEqual(len(auto_update_status["last_oos_alerts"]), 4)

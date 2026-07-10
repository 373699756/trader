import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from stock_analyzer import config
from stock_analyzer.deepseek_scheduler import (
    reuse_scheduled_deepseek_result,
    save_scheduled_deepseek_result,
    scheduled_deepseek_decision,
)


class DeepSeekSchedulerTest(unittest.TestCase):
    def _rows(self, first_score=80):
        return [
            {"code": "600001", "score": first_score, "tier": "primary_watch", "price": 10.0},
            {"code": "600002", "score": 75, "tier": "primary_watch", "price": 11.0},
            {"code": "600003", "score": 70, "tier": "backup_pool", "price": 12.0},
        ]

    def test_early_session_allows_at_most_one_call_per_half_hour(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "DEEPSEEK_SCHEDULE_STATE_PATH", "{}/schedule.json".format(tmpdir)
        ), patch.object(config, "DEEPSEEK_SCHEDULE_ENABLED", True):
            first = scheduled_deepseek_decision(
                "tomorrow_picks", self._rows(), datetime(2026, 7, 10, 10, 2)
            )
            second = scheduled_deepseek_decision(
                "tomorrow_picks", self._rows(), datetime(2026, 7, 10, 10, 20)
            )
            third = scheduled_deepseek_decision(
                "tomorrow_picks", self._rows(), datetime(2026, 7, 10, 10, 31)
            )

        self.assertTrue(first["allow_call"])
        self.assertEqual(first["slot"], "10:00")
        self.assertFalse(second["allow_call"])
        self.assertEqual(second["status"], "slot_reused")
        self.assertTrue(third["allow_call"])
        self.assertEqual(third["slot"], "10:30")

    def test_late_session_is_on_demand_and_reuses_unchanged_candidates(self):
        start = datetime(2026, 7, 10, 14, 32)
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "DEEPSEEK_SCHEDULE_STATE_PATH", "{}/schedule.json".format(tmpdir)
        ), patch.object(config, "DEEPSEEK_SCHEDULE_ENABLED", True):
            first = scheduled_deepseek_decision("tomorrow_picks", self._rows(), start)
            reviewed = [{**row, "deepseek_action": "watch", "deepseek_rank_score": row["score"] + 1} for row in self._rows()]
            save_scheduled_deepseek_result(
                "tomorrow_picks",
                reviewed,
                {"status": "ok", "usage": {"total_tokens": 100}},
                first,
                start,
            )
            unchanged = scheduled_deepseek_decision(
                "tomorrow_picks", self._rows(), start + timedelta(minutes=8)
            )
            reused_rows, reused_meta = reuse_scheduled_deepseek_result(
                "tomorrow_picks", [{**row, "price": 99.0} for row in self._rows()], unchanged, start
            )

        self.assertTrue(first["allow_call"])
        self.assertEqual(first["model_tier"], "pro")
        self.assertTrue(first["slot"].startswith("late:"))
        self.assertFalse(unchanged["allow_call"])
        self.assertEqual(unchanged["status"], "no_material_change")
        self.assertEqual(reused_meta["status"], "schedule_cache_hit")
        self.assertEqual(reused_rows[0]["price"], 99.0)
        self.assertEqual(reused_rows[0]["deepseek_action"], "watch")

    def test_late_changes_are_debounced_then_use_flash_after_pro_cap(self):
        start = datetime(2026, 7, 10, 14, 31)
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            config, "DEEPSEEK_SCHEDULE_STATE_PATH", "{}/schedule.json".format(tmpdir)
        ), patch.object(config, "DEEPSEEK_SCHEDULE_ENABLED", True), patch.object(
            config, "DEEPSEEK_LATE_MIN_INTERVAL_SECONDS", 300
        ), patch.object(config, "DEEPSEEK_DAILY_PRO_CALL_CAP", 1):
            first = scheduled_deepseek_decision("tomorrow_picks", self._rows(), start)
            save_scheduled_deepseek_result(
                "tomorrow_picks", self._rows(), {"status": "ok"}, first, start
            )
            debounced = scheduled_deepseek_decision(
                "tomorrow_picks", self._rows(first_score=84), start + timedelta(minutes=2)
            )
            later = scheduled_deepseek_decision(
                "tomorrow_picks", self._rows(first_score=84), start + timedelta(minutes=6)
            )

        self.assertFalse(debounced["allow_call"])
        self.assertEqual(debounced["status"], "late_debounced")
        self.assertTrue(later["allow_call"])
        self.assertEqual(later["model_tier"], "base")


if __name__ == "__main__":
    unittest.main()

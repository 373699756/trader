from datetime import datetime
from unittest.mock import patch

from stock_analyzer import config
from stock_analyzer.app import create_app
from stock_analyzer.deepseek_scheduler import DeepSeekPrecomputeScheduler


def test_scheduler_claims_each_pre_1430_slot_once_across_rechecks(tmp_path):
    scheduler = DeepSeekPrecomputeScheduler()
    with patch.object(config, "DEEPSEEK_SCHEDULER_DB_PATH", str(tmp_path / "scheduler.sqlite3")), patch.object(
        config,
        "DEEPSEEK_PRECOMPUTE_TIMES",
        ("09:30",),
    ), patch.object(scheduler, "_execute") as execute:
        scheduler._run_due_slot(datetime(2026, 7, 14, 9, 30, 10))
        scheduler._run_due_slot(datetime(2026, 7, 14, 9, 30, 20))

    execute.assert_called_once_with("2026-07-14T09:30")


def test_scheduler_never_auto_runs_at_or_after_on_demand_window(tmp_path):
    scheduler = DeepSeekPrecomputeScheduler()
    with patch.object(config, "DEEPSEEK_SCHEDULER_DB_PATH", str(tmp_path / "scheduler.sqlite3")), patch.object(
        config,
        "DEEPSEEK_PRECOMPUTE_TIMES",
        ("14:30",),
    ), patch.object(config, "DEEPSEEK_ON_DEMAND_START", "14:30"), patch.object(
        scheduler,
        "_execute",
    ) as execute:
        scheduler._run_due_slot(datetime(2026, 7, 14, 14, 30, 5))

    execute.assert_not_called()


def test_scheduler_status_reports_internal_sqlite_mode(tmp_path):
    scheduler = DeepSeekPrecomputeScheduler()
    with patch.object(config, "DEEPSEEK_SCHEDULER_DB_PATH", str(tmp_path / "scheduler.sqlite3")):
        status = scheduler.status(datetime(2026, 7, 14, 10, 0))

    assert status["mode"] == "in_process_sqlite_lease"
    assert status["daily_call_limit"] == 50


def test_app_factory_starts_only_the_internal_deepseek_scheduler():
    with patch.object(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", True), patch(
        "stock_analyzer.deepseek_scheduler.start_deepseek_scheduler"
    ) as start_scheduler:
        create_app()

    start_scheduler.assert_called_once_with()


def test_app_factory_respects_internal_scheduler_switch():
    with patch.object(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", False), patch(
        "stock_analyzer.deepseek_scheduler.start_deepseek_scheduler"
    ) as start_scheduler:
        create_app()

    start_scheduler.assert_not_called()

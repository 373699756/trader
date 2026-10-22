import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.recommendation.application.runtime.cadence import (
    CadencePlanner,
    CadencePolicy,
    PipelineTask,
    SchedulePointResult,
    SchedulePointStatus,
    TaskExecutionBudgetSeconds,
)
from trader.recommendation.application.runtime.schedule import SHANGHAI, SchedulePoint


def _policy():
    raw = json.loads(Path("config/scheduler.json").read_text())
    return CadencePolicy.from_seconds(raw["pipeline"]["cadence_seconds"])


@pytest.mark.parametrize(
    "clock",
    ["09:15:00", "10:00:00", "11:20:00", "12:15:00", "13:00:00", "14:46:00", "14:48:00", "14:59:20", "14:59:50"],
)
def test_uniform_ten_second_market_and_scoring_cycle(clock):
    at = datetime.fromisoformat("2026-07-16T" + clock).replace(tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=at)
    first = planner.plan(at, is_trading_day=True)
    main = {PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES, PipelineTask.TOPK_QUOTES, PipelineTask.LONG_QUOTES}
    assert main <= {t.task for t in first.tasks}
    assert not any(t.schedule_point for t in first.tasks)
    assert planner.plan_score_after_input(at, is_trading_day=True) is not None
    assert planner.plan_score_after_input(at + timedelta(seconds=9), is_trading_day=True) is None
    assert not main.intersection(t.task for t in planner.plan(at + timedelta(seconds=9), is_trading_day=True).tasks)
    due = planner.plan(at + timedelta(seconds=10), is_trading_day=True)
    if (at + timedelta(seconds=10)).hour >= 15:
        assert PipelineTask.FREEZE in {t.task for t in due.tasks}
        assert PipelineTask.SCORE not in {t.task for t in due.tasks}
        return
    assert main <= {t.task for t in due.tasks}
    assert PipelineTask.SCORE in {t.task for t in due.tasks}


@pytest.mark.parametrize("boundary", ("11:30:00", "12:45:00", "13:00:00", "14:46:00", "14:48:00", "14:59:20"))
def test_input_changes_across_former_time_bands_keep_one_pending_score(boundary):
    crossed = datetime.fromisoformat("2026-07-16T" + boundary).replace(tzinfo=SHANGHAI)
    at = crossed - timedelta(seconds=5)
    planner = CadencePlanner(_policy(), started_at=at)
    planner.plan(at, is_trading_day=True)
    assert planner.plan_score_after_input(at, is_trading_day=True) is not None
    assert planner.plan_score_after_input(crossed, is_trading_day=True) is None
    assert PipelineTask.SCORE not in {item.task for item in planner.plan(crossed, is_trading_day=True).tasks}
    assert planner.plan_score_after_input(crossed + timedelta(seconds=4), is_trading_day=True) is None
    due_at = at + timedelta(seconds=10)
    due = planner.plan(due_at, is_trading_day=True)
    assert sum(item.task is PipelineTask.SCORE for item in due.tasks) == 1
    assert PipelineTask.SCORE not in {item.task for item in planner.plan(due_at, is_trading_day=True).tasks}
    assert planner.plan_score_after_input(due_at, is_trading_day=True) is None


@pytest.mark.parametrize(
    "clock,minute,industry",
    [
        ("09:20:00", None, 120),
        ("10:00:00", None, 60),
        ("12:15:00", None, None),
        ("13:00:00", 5, 60),
        ("14:20:00", 3, 60),
        ("14:48:00", None, None),
    ],
)
def test_independent_input_windows_preserve_original_frequencies(clock, minute, industry):
    at = datetime.fromisoformat("2026-07-16T" + clock).replace(tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=at)
    assert planner.interval_for(PipelineTask.INTRADAY_TAIL, at, is_trading_day=True) == minute
    assert planner.interval_for(PipelineTask.INDUSTRY_HEAT, at, is_trading_day=True) == industry


def test_no_duplicate_or_catchup_after_slow_round_and_no_old_special_points():
    at = datetime(2026, 7, 16, 14, 45, 59, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=at)
    planner.plan(at, is_trading_day=True)
    late = planner.plan(at + timedelta(seconds=121), is_trading_day=True)
    assert sum(t.task is PipelineTask.FULL_MARKET for t in late.tasks) == 1
    assert planner.plan(at + timedelta(seconds=122), is_trading_day=True).tasks == ()
    assert {key.schedule_point for key in planner.status().schedule_points} == {
        SchedulePoint.AFTERNOON_FREEZE,
        SchedulePoint.CLOSE_QUOTES,
    }


def test_freeze_interrupts_regular_cycle_and_blocks_recovery_until_settled():
    at = datetime(2026, 7, 16, 14, 59, 59, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=at)
    planner.plan(at, is_trading_day=True)
    assert planner.plan_score_after_input(at, is_trading_day=True) is not None
    assert planner.plan(at, is_trading_day=True).next_delay_seconds == 1
    close = at + timedelta(seconds=1)
    batch = planner.plan(close, is_trading_day=True)
    freeze = next(t for t in batch.tasks if t.task is PipelineTask.FREEZE)
    assert not any(
        t.task in {PipelineTask.SCORE, PipelineTask.FULL_MARKET, PipelineTask.CLOSE_QUOTES} for t in batch.tasks
    )
    planner.record_submission(freeze, accepted=True, at=close)
    planner.record_results(
        freeze, {"tomorrow": SchedulePointResult.COMPLETED, "d25": SchedulePointResult.COMPLETED}, at=close
    )
    assert planner.plan_score_after_input(close, is_trading_day=True) is None
    assert PipelineTask.CLOSE_QUOTES in {t.task for t in planner.plan(close, is_trading_day=True).tasks}


def test_cold_close_recovery_retries_without_intraday_freeze():
    at = datetime(2026, 7, 16, 22, 30, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=at)
    batch = planner.plan(at, is_trading_day=True)
    assert PipelineTask.FREEZE not in {t.task for t in batch.tasks}
    recovery = next(t for t in batch.tasks if t.task is PipelineTask.CLOSE_QUOTES)
    planner.record_submission(recovery, accepted=True, at=at)
    planner.record_results(recovery, {"-": SchedulePointResult.RETRY}, at=at)
    assert PipelineTask.CLOSE_QUOTES not in {
        t.task for t in planner.plan(at + timedelta(milliseconds=999), is_trading_day=True).tasks
    }
    retry_tasks = {t.task for t in planner.plan(at + timedelta(seconds=1), is_trading_day=True).tasks}
    assert PipelineTask.CLOSE_QUOTES in retry_tasks
    assert PipelineTask.LONG_QUOTES not in retry_tasks
    assert PipelineTask.LONG_QUOTES in {
        t.task for t in planner.plan(at + timedelta(seconds=10), is_trading_day=True).tasks
    }
    assert (
        planner.schedule_point_status(at.date().isoformat(), SchedulePoint.AFTERNOON_FREEZE, "tomorrow")
        is SchedulePointStatus.MISSED
    )


def test_nontrading_day_has_no_work():
    at = datetime(2026, 7, 18, 10, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=at)
    assert planner.plan(at, is_trading_day=False).tasks == ()
    assert planner.plan_score_after_input(at, is_trading_day=False) is None


def test_execution_budgets_remain_bounded_and_freeze_has_independent_lane():
    assert TaskExecutionBudgetSeconds(PipelineTask.FREEZE) is None
    assert all(TaskExecutionBudgetSeconds(t) > 0 for t in PipelineTask if t is not PipelineTask.FREEZE)

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.application.cadence import (
    PERIODIC_TASKS,
    CadencePlanner,
    CadencePolicy,
    PipelineTask,
    SchedulePointKey,
    SchedulePointLifecycle,
    SchedulePointResult,
    freshness_level,
    task_execution_budget_seconds,
)
from trader.application.schedule import SHANGHAI, SchedulePoint


def test_delayed_scheduler_tick_catches_up_mandatory_freeze_point_once() -> None:
    planner = CadencePlanner(
        _policy(),
        started_at=datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI),
    )
    delayed = datetime(2026, 7, 16, 11, 20, 1, tzinfo=SHANGHAI)

    first = planner.plan(delayed, is_trading_day=True)
    freeze = next(task for task in first.tasks if task.task is PipelineTask.FREEZE)
    planner.record_submission(freeze, accepted=True, at=delayed)
    planner.record_results(
        freeze,
        {"today": SchedulePointResult.COMPLETED},
        at=delayed,
    )
    second = planner.plan(delayed + timedelta(seconds=1), is_trading_day=True)

    freezes = [task for task in first.tasks if task.task is PipelineTask.FREEZE]
    assert len(freezes) == 1
    assert freezes[0].freeze_strategies == ("today",)
    assert not [task for task in second.tasks if task.task is PipelineTask.FREEZE]


def test_restart_after_afternoon_cutoff_only_attempts_checkpoint_eligible_strategies() -> None:
    restarted = datetime(2026, 7, 16, 14, 50, 1, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=restarted)

    batch = planner.plan(restarted, is_trading_day=True)

    freezes = [task for task in batch.tasks if task.task is PipelineTask.FREEZE]
    assert len(freezes) == 1
    assert freezes[0].freeze_strategies == ("tomorrow", "d25")
    assert PipelineTask.DEEPSEEK_CUTOFF not in {task.task for task in batch.tasks}
    assert PipelineTask.FINAL_CANDIDATE_QUOTES not in {task.task for task in batch.tasks}
    status = planner.status()
    assert status.schedule_points[SchedulePointKey("2026-07-16", SchedulePoint.TODAY_FREEZE, "today")].lifecycle is (
        SchedulePointLifecycle.MISSED
    )
    assert status.schedule_points[SchedulePointKey("2026-07-16", SchedulePoint.DEEPSEEK_CUTOFF, "-")].lifecycle is (
        SchedulePointLifecycle.MISSED
    )


def test_cold_start_at_today_boundary_marks_today_missed() -> None:
    started_at = datetime(2026, 7, 16, 11, 20, 0, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)

    batch = planner.plan(started_at, is_trading_day=True)

    assert not [task for task in batch.tasks if task.task is PipelineTask.FREEZE]
    assert (
        planner.schedule_point_lifecycle("2026-07-16", SchedulePoint.TODAY_FREEZE, "today")
        is SchedulePointLifecycle.MISSED
    )


def test_rejected_freeze_submission_returns_to_pending_and_can_be_submitted_again() -> None:
    started_at = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    boundary = datetime(2026, 7, 16, 11, 20, 0, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)

    first = next(task for task in planner.plan(boundary, is_trading_day=True).tasks if task.task is PipelineTask.FREEZE)
    planner.record_submission(first, accepted=False, at=boundary)
    second = next(
        task
        for task in planner.plan(boundary + timedelta(milliseconds=50), is_trading_day=True).tasks
        if task.task is PipelineTask.FREEZE
    )

    assert second.freeze_strategies == ("today",)


def test_retry_wait_uses_fixed_backoff_and_keeps_point_incomplete() -> None:
    started_at = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    boundary = datetime(2026, 7, 16, 11, 20, 0, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)
    freeze = next(
        task for task in planner.plan(boundary, is_trading_day=True).tasks if task.task is PipelineTask.FREEZE
    )
    planner.record_submission(freeze, accepted=True, at=boundary)
    planner.record_results(freeze, {"today": SchedulePointResult.RETRY}, at=boundary)

    early = planner.plan(boundary + timedelta(milliseconds=999), is_trading_day=True)
    due = planner.plan(boundary + timedelta(seconds=1), is_trading_day=True)

    assert not [task for task in early.tasks if task.task is PipelineTask.FREEZE]
    assert next(task for task in due.tasks if task.task is PipelineTask.FREEZE).freeze_strategies == ("today",)


def test_periodic_candidate_task_cannot_supersede_an_inflight_fixed_quote_checkpoint() -> None:
    started_at = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    checkpoint_at = datetime(2026, 7, 16, 11, 19, 50, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=started_at)
    first = planner.plan(checkpoint_at, is_trading_day=True)
    checkpoint = next(
        task
        for task in first.tasks
        if task.task is PipelineTask.FINAL_CANDIDATE_QUOTES and task.schedule_point is SchedulePoint.TODAY_CHECKPOINT
    )
    planner.record_submission(checkpoint, accepted=True, at=checkpoint_at)

    while_inflight = planner.plan(checkpoint_at + timedelta(seconds=1), is_trading_day=True)

    assert PipelineTask.CANDIDATE_QUOTES not in {task.task for task in while_inflight.tasks}


@pytest.mark.parametrize(
    ("restarted", "expects_current_quotes"),
    (
        (datetime(2026, 7, 16, 14, 55, tzinfo=SHANGHAI), True),
        (datetime(2026, 7, 16, 15, 5, tzinfo=SHANGHAI), False),
    ),
)
def test_restart_uses_current_recovery_only_before_close(restarted, expects_current_quotes) -> None:
    planner = CadencePlanner(_policy(), started_at=restarted)

    first = planner.plan(restarted, is_trading_day=True)
    second = planner.plan(restarted + timedelta(seconds=1), is_trading_day=True)

    assert (PipelineTask.CURRENT_QUOTES in {task.task for task in first.tasks}) is expects_current_quotes
    assert (PipelineTask.CLOSE_QUOTES in {task.task for task in first.tasks}) is (not expects_current_quotes)
    assert PipelineTask.CURRENT_QUOTES not in {task.task for task in second.tasks}


def test_missed_final_candidate_refresh_is_not_replayed_after_freeze_boundary() -> None:
    planner = CadencePlanner(
        _policy(),
        started_at=datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI),
    )

    before_freeze = planner.plan(datetime(2026, 7, 16, 14, 49, 51, tzinfo=SHANGHAI), is_trading_day=True)
    after_freeze = planner.plan(datetime(2026, 7, 16, 14, 50, 1, tzinfo=SHANGHAI), is_trading_day=True)

    assert PipelineTask.FINAL_CANDIDATE_QUOTES in {task.task for task in before_freeze.tasks}
    assert PipelineTask.FINAL_CANDIDATE_QUOTES not in {task.task for task in after_freeze.tasks}


def test_frozen_and_after_close_only_plan_mutable_quote_projections() -> None:
    planner = CadencePlanner(
        _policy(),
        started_at=datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI),
    )

    frozen = planner.plan(datetime(2026, 7, 16, 14, 55, tzinfo=SHANGHAI), is_trading_day=True)
    freeze = next(
        task
        for task in frozen.tasks
        if task.task is PipelineTask.FREEZE and task.schedule_point is SchedulePoint.AFTERNOON_FREEZE
    )
    planner.record_submission(freeze, accepted=True, at=freeze.scheduled_at)
    planner.record_results(
        freeze,
        {"tomorrow": SchedulePointResult.COMPLETED, "d25": SchedulePointResult.COMPLETED},
        at=freeze.scheduled_at,
    )
    after_close = planner.plan(datetime(2026, 7, 16, 15, 5, tzinfo=SHANGHAI), is_trading_day=True)

    assert {task.task for task in frozen.tasks if task.schedule_point is None} == {
        PipelineTask.REFERENCE_DATA,
        PipelineTask.CURRENT_QUOTES,
        PipelineTask.TOPK_QUOTES,
        PipelineTask.LONG_QUOTES,
    }
    assert {task.task for task in after_close.tasks} == {
        PipelineTask.CLOSE_QUOTES,
        PipelineTask.LONG_QUOTES,
        PipelineTask.REFERENCE_DATA,
        PipelineTask.TOPK_QUOTES,
    }


def test_afternoon_tail_has_an_independent_five_second_deadline() -> None:
    current = datetime(2026, 7, 16, 13, 0, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=current)

    first = planner.plan(current, is_trading_day=True)
    second = planner.plan(current + timedelta(seconds=1), is_trading_day=True)
    due = planner.plan(current + timedelta(seconds=5), is_trading_day=True)

    assert PipelineTask.INTRADAY_TAIL in {task.task for task in first.tasks}
    assert first.next_delay_seconds == 5.0
    assert PipelineTask.INTRADAY_TAIL not in {task.task for task in second.tasks}
    assert PipelineTask.INTRADAY_TAIL in {task.task for task in due.tasks}


def test_freshness_level_uses_strict_two_and_three_cycle_boundaries() -> None:
    assert freshness_level(None, 10.0) == "unavailable"
    assert freshness_level(20.0, 10.0) == "fresh"
    assert freshness_level(20.001, 10.0) == "stale"
    assert freshness_level(30.0, 10.0) == "stale"
    assert freshness_level(30.001, 10.0) == "degraded"


def test_periodic_tasks_skip_missed_cycles_instead_of_bursting_catchup_work() -> None:
    first_tick = datetime(2026, 7, 16, 9, 30, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=first_tick)

    planner.plan(first_tick, is_trading_day=True)
    delayed = planner.plan(first_tick + timedelta(minutes=1), is_trading_day=True)

    counts = Counter(item.task for item in delayed.tasks)
    assert all(count <= 1 for count in counts.values())
    assert counts[PipelineTask.CANDIDATE_QUOTES] == 1
    assert counts[PipelineTask.TOPK_QUOTES] == 1
    assert counts[PipelineTask.LONG_QUOTES] == 1


def test_scoring_is_triggered_by_completed_inputs_and_uses_the_configured_minimum_interval() -> None:
    first_input = datetime(2026, 7, 16, 9, 30, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=first_input)

    warmup = planner.plan_score_after_input(first_input.replace(hour=9, minute=29), is_trading_day=True)
    periodic = planner.plan(first_input, is_trading_day=True)
    first_score = planner.plan_score_after_input(first_input, is_trading_day=True)
    throttled = planner.plan_score_after_input(first_input + timedelta(seconds=9), is_trading_day=True)
    next_score = planner.plan_score_after_input(first_input + timedelta(seconds=10), is_trading_day=True)

    assert warmup is None
    assert PipelineTask.SCORE not in PERIODIC_TASKS
    assert PipelineTask.SCORE not in {task.task for task in periodic.tasks}
    assert first_score is not None and first_score.task is PipelineTask.SCORE
    assert throttled is None
    assert next_score is not None and next_score.task is PipelineTask.SCORE


def test_input_arriving_inside_score_throttle_is_retained_as_one_latest_pending_score() -> None:
    first_input = datetime(2026, 7, 16, 9, 30, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=first_input)

    assert planner.plan_score_after_input(first_input, is_trading_day=True) is not None
    assert planner.plan_score_after_input(first_input + timedelta(seconds=4), is_trading_day=True) is None
    assert planner.plan_score_after_input(first_input + timedelta(seconds=9), is_trading_day=True) is None

    before_due = planner.plan(first_input + timedelta(seconds=9, milliseconds=999), is_trading_day=True)
    at_due = planner.plan(first_input + timedelta(seconds=10), is_trading_day=True)
    repeated = planner.plan(first_input + timedelta(seconds=10, milliseconds=1), is_trading_day=True)

    assert PipelineTask.SCORE not in {task.task for task in before_due.tasks}
    scores = tuple(task for task in at_due.tasks if task.task is PipelineTask.SCORE)
    assert len(scores) == 1
    assert scores[0].scheduled_at == first_input + timedelta(seconds=10)
    assert PipelineTask.SCORE not in {task.task for task in repeated.tasks}


def test_final_window_keeps_local_input_driven_scoring_until_the_freeze_boundary() -> None:
    final_input = datetime(2026, 7, 16, 14, 49, 20, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=final_input.replace(hour=9, minute=15))

    before_freeze = planner.plan_score_after_input(final_input, is_trading_day=True)
    at_freeze = planner.plan_score_after_input(final_input.replace(minute=50, second=0), is_trading_day=True)

    assert before_freeze is not None and before_freeze.phase.value == "deepseek_cutoff"
    assert at_freeze is None


def test_afternoon_checkpoint_is_a_retryable_strategy_scoped_schedule_point() -> None:
    checkpoint_at = datetime(2026, 7, 16, 14, 49, 20, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=checkpoint_at.replace(hour=9, minute=15))

    first = planner.plan(checkpoint_at, is_trading_day=True)
    checkpoint = next(task for task in first.tasks if task.task is PipelineTask.CHECKPOINT)
    planner.record_submission(checkpoint, accepted=True, at=checkpoint_at)
    planner.record_results(
        checkpoint,
        {"tomorrow": SchedulePointResult.RETRY, "d25": SchedulePointResult.COMPLETED},
        at=checkpoint_at,
    )
    retry = planner.plan(checkpoint_at + timedelta(seconds=1), is_trading_day=True)

    assert checkpoint.schedule_point is SchedulePoint.AFTERNOON_CHECKPOINT
    assert checkpoint.freeze_strategies == ("tomorrow", "d25")
    retried = next(task for task in retry.tasks if task.task is PipelineTask.CHECKPOINT)
    assert retried.freeze_strategies == ("tomorrow",)


def test_first_tick_after_warmup_still_initializes_reference_data_once() -> None:
    late_start = datetime(2026, 7, 16, 9, 45, tzinfo=SHANGHAI)
    planner = CadencePlanner(_policy(), started_at=late_start)

    first = planner.plan(late_start, is_trading_day=True)
    second = planner.plan(late_start + timedelta(seconds=1), is_trading_day=True)

    assert [task.task for task in first.tasks].count(PipelineTask.REFERENCE_DATA) == 1
    assert PipelineTask.REFERENCE_DATA not in {task.task for task in second.tasks}


def test_close_quotes_budget_allows_slow_close_source_and_local_rebuild() -> None:
    assert task_execution_budget_seconds(PipelineTask.CLOSE_QUOTES) >= 180.0


def test_production_policy_plans_exact_full_trading_day_task_counts() -> None:
    raw = json.loads((Path(__file__).parents[3] / "config" / "v2" / "runtime.json").read_text(encoding="utf-8"))
    current = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    planner = CadencePlanner(
        CadencePolicy.from_seconds(raw["pipeline"]["cadence_seconds"]),
        started_at=current,
    )
    closing = current.replace(hour=15, minute=0)
    counts: Counter[PipelineTask] = Counter()

    while current <= closing:
        batch = planner.plan(current, is_trading_day=True)
        counts.update(item.task for item in batch.tasks)
        for task in batch.tasks:
            if task.schedule_point is None:
                continue
            planner.record_submission(task, accepted=True, at=current)
            results = {strategy: SchedulePointResult.COMPLETED for strategy in (task.freeze_strategies or ("-",))}
            if task.schedule_point is SchedulePoint.TODAY_CHECKPOINT:
                results = {"today": SchedulePointResult.COMPLETED}
            planner.record_results(task, results, at=current)
        current += timedelta(seconds=1)

    assert counts == Counter(
        {
            PipelineTask.FULL_MARKET: 1998,
            PipelineTask.CANDIDATE_QUOTES: 10340,
            PipelineTask.TOPK_QUOTES: 15301,
            PipelineTask.INTRADAY_TAIL: 1520,
            PipelineTask.LONG_QUOTES: 15301,
            PipelineTask.INDUSTRY_HEAT: 226,
            PipelineTask.MARKET_NEWS: 226,
            PipelineTask.STOCK_RISK: 81,
            PipelineTask.REFERENCE_DATA: 2,
            PipelineTask.DEEPSEEK_CUTOFF: 1,
            PipelineTask.CHECKPOINT: 1,
            PipelineTask.FINAL_CANDIDATE_QUOTES: 2,
            PipelineTask.FREEZE: 2,
            PipelineTask.CLOSE_QUOTES: 1,
        }
    )


def _policy() -> CadencePolicy:
    return CadencePolicy.from_seconds(
        {
            "full_market": {"today_main": 30, "midday": 60, "final_window": 30},
            "candidate_quotes": {"today_main": 5, "midday": 60, "final_window": 2},
            "topk_quotes": {"today_main": 3, "midday": 60, "final_window": 3, "after_close": 10},
            "intraday_tail": {"afternoon": 5, "final_review": 3},
            "long_quotes": {"today_main": 3, "midday": 60, "final_window": 3},
            "score": {"today_main": 10, "final_window": 1},
            "industry_heat": {"today_main": 60},
            "market_news": {"today_main": 60},
            "stock_risk": {"today_main": 180},
        }
    )

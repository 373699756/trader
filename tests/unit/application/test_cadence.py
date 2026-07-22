from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.application.cadence import CadencePlanner, CadencePolicy, PipelineTask, freshness_level
from trader.application.schedule import SHANGHAI


def test_delayed_scheduler_tick_catches_up_mandatory_freeze_point_once() -> None:
    planner = CadencePlanner(_policy())
    delayed = datetime(2026, 7, 16, 11, 20, 1, tzinfo=SHANGHAI)

    first = planner.plan(delayed, is_trading_day=True)
    second = planner.plan(delayed + timedelta(seconds=1), is_trading_day=True)

    freezes = [task for task in first.tasks if task.task is PipelineTask.FREEZE]
    assert len(freezes) == 1
    assert freezes[0].freeze_strategies == ("today",)
    assert not [task for task in second.tasks if task.task is PipelineTask.FREEZE]


def test_restart_after_afternoon_cutoff_combines_due_freezes_without_losing_strategy() -> None:
    planner = CadencePlanner(_policy())
    restarted = datetime(2026, 7, 16, 14, 50, 1, tzinfo=SHANGHAI)

    batch = planner.plan(restarted, is_trading_day=True)

    freezes = [task for task in batch.tasks if task.task is PipelineTask.FREEZE]
    assert len(freezes) == 1
    assert freezes[0].freeze_strategies == ("today", "tomorrow", "d25")
    assert PipelineTask.DEEPSEEK_CUTOFF in {task.task for task in batch.tasks}
    assert PipelineTask.FINAL_CANDIDATE_QUOTES not in {task.task for task in batch.tasks}


@pytest.mark.parametrize(
    "restarted",
    (
        datetime(2026, 7, 16, 14, 55, tzinfo=SHANGHAI),
        datetime(2026, 7, 16, 15, 5, tzinfo=SHANGHAI),
    ),
)
def test_restart_after_freeze_recovers_current_quote_index_once(restarted) -> None:
    planner = CadencePlanner(_policy())

    first = planner.plan(restarted, is_trading_day=True)
    second = planner.plan(restarted + timedelta(seconds=1), is_trading_day=True)

    assert [task.task for task in first.tasks].count(PipelineTask.CURRENT_QUOTES) == 1
    assert PipelineTask.CURRENT_QUOTES not in {task.task for task in second.tasks}


def test_missed_final_candidate_refresh_is_not_replayed_after_freeze_boundary() -> None:
    planner = CadencePlanner(_policy())

    before_freeze = planner.plan(datetime(2026, 7, 16, 14, 49, 51, tzinfo=SHANGHAI), is_trading_day=True)
    after_freeze = planner.plan(datetime(2026, 7, 16, 14, 50, 1, tzinfo=SHANGHAI), is_trading_day=True)

    assert PipelineTask.FINAL_CANDIDATE_QUOTES in {task.task for task in before_freeze.tasks}
    assert PipelineTask.FINAL_CANDIDATE_QUOTES not in {task.task for task in after_freeze.tasks}


def test_freshness_level_uses_strict_two_and_three_cycle_boundaries() -> None:
    assert freshness_level(None, 10.0) == "unavailable"
    assert freshness_level(20.0, 10.0) == "fresh"
    assert freshness_level(20.001, 10.0) == "stale"
    assert freshness_level(30.0, 10.0) == "stale"
    assert freshness_level(30.001, 10.0) == "degraded"


def test_periodic_tasks_skip_missed_cycles_instead_of_bursting_catchup_work() -> None:
    planner = CadencePlanner(_policy())
    first_tick = datetime(2026, 7, 16, 9, 30, tzinfo=SHANGHAI)

    planner.plan(first_tick, is_trading_day=True)
    delayed = planner.plan(first_tick + timedelta(minutes=1), is_trading_day=True)

    counts = Counter(item.task for item in delayed.tasks)
    assert all(count <= 1 for count in counts.values())
    assert counts[PipelineTask.CANDIDATE_QUOTES] == 1
    assert counts[PipelineTask.TOPK_QUOTES] == 1


def test_first_tick_after_warmup_still_initializes_reference_data_once() -> None:
    planner = CadencePlanner(_policy())
    late_start = datetime(2026, 7, 16, 9, 45, tzinfo=SHANGHAI)

    first = planner.plan(late_start, is_trading_day=True)
    second = planner.plan(late_start + timedelta(seconds=1), is_trading_day=True)

    assert [task.task for task in first.tasks].count(PipelineTask.REFERENCE_DATA) == 1
    assert PipelineTask.REFERENCE_DATA not in {task.task for task in second.tasks}


def test_production_policy_plans_exact_full_trading_day_task_counts() -> None:
    raw = json.loads((Path(__file__).parents[3] / "config" / "v2" / "runtime.json").read_text(encoding="utf-8"))
    planner = CadencePlanner(CadencePolicy.from_seconds(raw["pipeline"]["cadence_seconds"]))
    current = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    closing = current.replace(hour=15, minute=0)
    counts: Counter[PipelineTask] = Counter()

    while current <= closing:
        counts.update(item.task for item in planner.plan(current, is_trading_day=True).tasks)
        current += timedelta(seconds=1)

    assert counts == Counter(
        {
            PipelineTask.FULL_MARKET: 3530,
            PipelineTask.CANDIDATE_QUOTES: 10940,
            PipelineTask.TOPK_QUOTES: 15300,
            PipelineTask.SCORE: 3410,
            PipelineTask.INDUSTRY_HEAT: 226,
            PipelineTask.MARKET_NEWS: 226,
            PipelineTask.STOCK_RISK: 81,
            PipelineTask.REFERENCE_DATA: 2,
            PipelineTask.DEEPSEEK_CUTOFF: 1,
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
            "topk_quotes": {"today_main": 3, "midday": 60, "final_window": 3},
            "score": {"today_main": 10},
            "industry_heat": {"today_main": 60},
            "market_news": {"today_main": 60},
            "stock_risk": {"today_main": 180},
        }
    )

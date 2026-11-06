"""The durable gap index distinguishes retryable gaps from source-unusable qfq dates."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace
from datetime import date, timedelta
import sqlite3
import sys

import pytest

from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.domain.history_download import HistoryDownloadBatch, HistoryDownloadCompletion, HistoryDownloadTask
from trader.download.infra.history_download_index import HistoryDownloadIndex


def test_nonpositive_qfq_gap_is_deferred_for_20_market_sessions_and_kept_unresolved(tmp_path):
    calendar = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(41))
    task = HistoryDownloadTask("600001", calendar[:2])
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).initialize()
    index.begin(HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, calendar[20]), (task,))
    index.finish((HistoryDownloadCompletion(task, task.dates, "tencent_qfq_invalid_price", (task.dates[0],)),))

    assert index.gaps_by_code().get(task.code, ()) == task.dates
    assert index.gaps_by_code(calendar[:21]).get(task.code, ()) == (task.dates[1],)
    assert index.gaps_by_code(calendar[:40]).get(task.code, ()) == (task.dates[1],)
    assert index.gaps_by_code(calendar).get(task.code, ()) == task.dates
    assert index.gap_summary((task.code,), calendar_dates=calendar[:21]).cells == 2
    assert index.gaps_by_code(calendar[:21], retryable_only=False) == {task.code: task.dates}
    assert index.gaps_by_code(calendar[:21]) == {task.code: (task.dates[1],)}
    assert index.gaps_by_code(calendar) == {task.code: task.dates}
    summary = index.gap_summary((task.code,), calendar_dates=calendar)
    assert (summary.stocks, summary.cells, summary.first_date, summary.last_date) == (1, 2, *task.dates)
    assert {item.reason: item.cells for item in summary.reasons} == {
        "tencent_qfq_invalid_price": 1,
        "history_price_pending": 1,
    }

    index.begin(HistoryDownloadBatch(2, "a" * 64, "b" * 64, "c" * 64, calendar[-1]), (task,))
    index.finish((HistoryDownloadCompletion(task, (), None),))

    assert index.gaps_by_code().get(task.code, ()) == ()
    assert index.gaps_by_code(calendar).get(task.code, ()) == ()
    assert index.gap_summary((task.code,), calendar_dates=calendar).cells == 0


def test_calendar_membership_limits_summary_and_repairs_without_erasing_old_gaps(tmp_path):
    days = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(5))
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    task = HistoryDownloadTask("600001", days)
    index.begin(HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, days[-1]), (task,))
    index.finish((HistoryDownloadCompletion(task, days, "tencent_qfq_invalid_price", days),))
    calendar = (days[1], days[-1])

    assert index.gaps_by_code(calendar) == {}
    assert index.gaps_by_code(calendar, retryable_only=False) == {task.code: calendar}
    summary = index.gap_summary((task.code,), calendar_dates=calendar)
    assert (summary.cells, summary.first_date, summary.last_date) == (2, *calendar)
    assert [(reason.reason, reason.cells) for reason in summary.reasons] == [("tencent_qfq_invalid_price", 2)]
    assert index.gap_summary(("600002",), calendar_dates=calendar).cells == 0
    assert index.gaps_by_code() == {task.code: days}
    for invalid in ((), (days[1], days[0]), (days[1], days[1])):
        with pytest.raises(ValueError, match="history_gap_calendar_invalid"):
            index.gap_summary((task.code,), calendar_dates=invalid)
        with pytest.raises(ValueError, match="history_retry_calendar_invalid"):
            index.gaps_by_code(invalid, retryable_only=False)


@pytest.mark.parametrize("change_population", [False, True])
def test_rebinding_preserves_tasks_attempts_gaps_and_recheck_dates(tmp_path, change_population):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    days = (date(2026, 10, 12), date(2026, 10, 13))
    previous = HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, days[-1])
    tasks = (HistoryDownloadTask("600001", days), HistoryDownloadTask("600002", days))
    index.begin(previous, tasks)
    index.begin_attempt("600001")
    index.finish((HistoryDownloadCompletion(tasks[0], days, "tencent_qfq_invalid_price", days[:1]),))
    before = index.tasks()
    current = replace(previous, source_hash="d" * 64, calendar_hash="e" * 64, universe_hash="f" * 64)
    population = (tasks[0], HistoryDownloadTask("600003", days)) if change_population else None
    index.rebind_context(previous, current, tasks=population)
    expected = (*before, population[1]) if population else before
    assert index.pending(0) == current
    assert index.tasks() == expected

    if population:
        assert index.tasks(codes=frozenset(task.code for task in population)) == (before[0], population[1])
    assert index.tasks(codes=frozenset()) == ()
    assert index.gaps_by_code().get("600001", ()) == days
    assert index.gaps_by_code(days).get("600001", ()) == days[1:]
    with pytest.raises(RuntimeError, match="history_download_identity_conflict"):
        index.rebind_context(previous, current, tasks=(HistoryDownloadTask("600004", days),))
    for invalid in (replace(current, sequence=2), replace(current, cutoff=days[0])):
        with pytest.raises(ValueError, match="history_download_identity_conflict"):
            index.rebind_context(current, invalid)
    assert index.pending(0) == current
    assert index.tasks() == expected


def test_advancing_pending_batch_atomically_replaces_dates_and_keeps_gap_receipts(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    days = tuple(date(2026, 10, 12) + timedelta(days=offset) for offset in range(3))
    previous = HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, days[1])
    task = HistoryDownloadTask("600001", days[:2])
    index.begin(previous, (task,))
    index.begin_attempt(task.code)
    index.finish((HistoryDownloadCompletion(task, days[:1], "tencent_qfq_invalid_price", days[:1]),))
    before = index.tasks()
    current = replace(previous, cutoff=days[-1], source_hash="d" * 64, calendar_hash="e" * 64)
    expanded = replace(before[0], dates=days[1:], tencent_done=False)
    with pytest.raises(ValueError, match="history_download_identity_conflict"):
        index.rebind_context(previous, current)
    with pytest.raises(sqlite3.IntegrityError):
        index.rebind_context(previous, current, tasks=(expanded, expanded))
    assert index.pending(0) == previous
    assert index.tasks() == before
    index.rebind_context(previous, current, tasks=(expanded,))
    assert index.pending(0) == current
    assert index.tasks() == (expanded,)
    assert index.gaps_by_code() == {task.code: days[:1]}
    assert index.gaps_by_code(days) == {}
    with pytest.raises(RuntimeError, match="history_download_identity_conflict"):
        index.rebind_context(previous, current, tasks=(expanded,))


def test_advancing_and_starting_batches_keep_ineligible_recovery_evidence(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    days = tuple(date(2026, 10, 12) + timedelta(days=i) for i in range(3))
    previous = HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, days[1])
    tasks = tuple(HistoryDownloadTask(code, days[:2]) for code in ("600001", "600002"))
    index.begin(previous, tasks)
    index.begin_attempt("600002")
    index.finish((HistoryDownloadCompletion(tasks[1], days[:1], "tencent_qfq_invalid_price", days[:1]),))
    retained = index.tasks(codes=frozenset({"600002"}))

    current = replace(previous, cutoff=days[-1], source_hash="d" * 64, calendar_hash="e" * 64)
    index.rebind_context(previous, current, tasks=(replace(tasks[0], dates=days),))
    assert index.tasks(codes=frozenset({"600002"})) == retained
    index.begin(replace(current, sequence=2), (HistoryDownloadTask("600003", days),))
    assert index.tasks(codes=frozenset({"600002"})) == retained
    assert index.gaps_by_code() == {"600002": days[:1]}
    assert index.gap_summary(("600003",), calendar_dates=days).cells == 0


def test_reopening_a_retained_task_replans_dates_without_losing_failure_evidence(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    days = tuple(date(2026, 10, 12) + timedelta(days=i) for i in range(3))
    previous = HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, days[-1])
    task = HistoryDownloadTask("600002", days[:1])
    index.begin(previous, (task,))
    index.begin_attempt(task.code)
    index.finish((HistoryDownloadCompletion(task, task.dates, "supplier_query_failed"),))
    retained = index.tasks()[0]
    current = replace(previous, universe_hash="d" * 64)
    index.rebind_context(
        previous, current, tasks=(replace(task, dates=days),), reopened_codes=frozenset({task.code}),
    )
    assert index.tasks() == (replace(retained, dates=days, tencent_done=False),)
    assert index.gaps_by_code() == {task.code: task.dates}


@pytest.mark.skipif(sys.version_info < (3, 11), reason="SQLite runtime limit API requires Python 3.11")
def test_selected_population_loads_with_999_sqlite_variable_limit(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    day = date(2026, 10, 12)
    tasks = tuple(HistoryDownloadTask(f"{600000 + number:06d}", (day,)) for number in range(1200))
    index.begin(HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, day), tasks)
    connect = sqlite3.connect

    def bounded(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return connection

    monkeypatch.setattr(sqlite3, "connect", bounded)
    assert index.tasks(codes=frozenset(task.code for task in tasks)) == tasks

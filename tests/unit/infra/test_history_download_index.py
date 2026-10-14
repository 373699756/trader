"""The durable gap index distinguishes retryable gaps from source-unusable qfq dates."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_download_index import (
    HistoryDownloadBatch,
    HistoryDownloadCompletion,
    HistoryDownloadIndex,
    HistoryDownloadTask,
)


def test_nonpositive_qfq_gap_is_deferred_for_20_market_sessions_and_kept_unresolved(tmp_path):
    calendar = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(41))
    task = HistoryDownloadTask("600001", calendar[:2])
    index = HistoryDownloadIndex(tmp_path / "control.sqlite3")
    index.initialize()
    index.begin(HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, calendar[20]), (task,))
    index.finish((HistoryDownloadCompletion(task, task.dates, "tencent_qfq_invalid_price", (task.dates[0],)),))

    assert index.gaps(task.code) == task.dates
    assert index.retryable_gaps(task.code, calendar[:21]) == (task.dates[1],)
    assert index.retryable_gaps(task.code, calendar[:40]) == (task.dates[1],)
    assert index.retryable_gaps(task.code, calendar) == task.dates
    assert index.unresolved((task.code,)) == 2

    index.begin(HistoryDownloadBatch(2, "a" * 64, "b" * 64, "c" * 64, calendar[-1]), (task,))
    index.finish((HistoryDownloadCompletion(task, (), None),))

    assert index.gaps(task.code) == ()
    assert index.retryable_gaps(task.code, calendar) == ()
    assert index.unresolved((task.code,)) == 0


def test_rebinding_preserves_tasks_attempts_gaps_and_recheck_dates(tmp_path):
    path = tmp_path / "control.sqlite3"
    HistoryControlIndex(path).initialize()
    index = HistoryDownloadIndex(path)
    index.initialize()
    days = (date(2026, 10, 12), date(2026, 10, 13))
    previous = HistoryDownloadBatch(1, "a" * 64, "b" * 64, "c" * 64, days[-1])
    tasks = (HistoryDownloadTask("600001", days), HistoryDownloadTask("600002", days))
    index.begin(previous, tasks)
    index.begin_attempt("600001")
    index.finish((HistoryDownloadCompletion(tasks[0], days, "tencent_qfq_invalid_price", days[:1]),))
    before = index.tasks()
    current = replace(previous, source_hash="d" * 64, calendar_hash="e" * 64, universe_hash="f" * 64)
    index.rebind_context(previous, current)
    assert index.pending(0) == current
    assert index.tasks() == before
    assert index.gaps("600001") == days
    assert index.retryable_gaps("600001", days) == days[1:]
    with pytest.raises(RuntimeError, match="history_download_identity_conflict"):
        index.rebind_context(previous, current)
    for invalid in (replace(current, sequence=2), replace(current, cutoff=days[0])):
        with pytest.raises(ValueError, match="history_download_identity_conflict"):
            index.rebind_context(current, invalid)
    assert index.pending(0) == current
    assert index.tasks() == before

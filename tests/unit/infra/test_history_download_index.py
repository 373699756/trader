"""The durable gap index distinguishes retryable gaps from source-unusable qfq dates."""

from datetime import date, timedelta

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

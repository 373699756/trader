from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.infra.reference_index import SQLiteReferenceIndex


def test_concurrent_writers_preserve_permanent_exclusion_and_other_codes(tmp_path: Path) -> None:
    path = tmp_path / "company_st.sqlite3"
    day = date(2026, 10, 9)
    gate = Barrier(2)

    def publish(status, code):
        gate.wait(timeout=5)
        return SQLiteReferenceIndex(path).merge_st(
            (HistoryStEvidence("600001", day, status), HistoryStEvidence(code, day, "clear"))
        )

    with ThreadPoolExecutor(2) as executor:
        a = executor.submit(publish, "ever_st", "600002")
        b = executor.submit(publish, "clear", "600003")
        a.result(timeout=10)
        b.result(timeout=10)
    result = {item.code: item.status for item in SQLiteReferenceIndex(path).read_st()}
    assert result == {"600001": "ever_st", "600002": "clear", "600003": "clear"}


def test_same_day_unknown_can_be_rechecked_but_stale_clear_cannot_overwrite_conflict(tmp_path: Path) -> None:
    path = tmp_path / "company_st.sqlite3"
    day = date(2026, 10, 9)
    unknown = HistoryStEvidence("600001", day, "unknown")
    clear = HistoryStEvidence("600001", day, "clear")
    SQLiteReferenceIndex(path).merge_st((unknown,))
    assert SQLiteReferenceIndex(path).merge_st((clear,), (unknown,)) == (clear,)
    assert SQLiteReferenceIndex(path).merge_st((unknown,), ()) == (unknown,)
    assert SQLiteReferenceIndex(path).merge_st((clear,), (clear,)) == (unknown,)


def test_stale_process_cannot_regress_observation_day(tmp_path: Path) -> None:
    path = tmp_path / "company_st.sqlite3"
    day = date(2026, 10, 9)
    newer = HistoryStEvidence("600001", day, "clear")
    older = HistoryStEvidence("600001", day - timedelta(days=1), "unknown")
    SQLiteReferenceIndex(path).merge_st((newer,))
    assert SQLiteReferenceIndex(path).merge_st((older,), ()) == (newer,)

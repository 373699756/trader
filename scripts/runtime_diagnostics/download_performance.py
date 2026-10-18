"""Offline download persistence measurements on disposable, synthetic databases."""

from __future__ import annotations

import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import Disclosure, DisclosurePage
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex, SQLiteCompanyEvidenceReader
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.infra.sqlite_connection import OpenSqliteConnection

from .reporting import emit_report


def _measure(root: Path) -> dict[str, float | int]:
    now = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    codes = tuple(str(600000 + offset) for offset in range(4402))
    index = SQLiteCompanyEvidenceIndex(root / "company.sqlite3")
    index.initialize()
    partition = index.partitions(("sse",), now)[0]
    rows = tuple(
        Disclosure("fixture", f"{code}:{offset}", (code,), "普通公告", now, "", str(offset))
        for code in codes
        for offset in range(3)
    )
    started = time.monotonic()
    index.commit_page(partition, DisclosurePage(rows, len(rows), False), now)
    write = time.monotonic() - started
    started = time.monotonic()
    index.publish(codes, now)
    publish = time.monotonic() - started
    reader = SQLiteCompanyEvidenceReader(index.path)
    started = time.monotonic()
    cold = reader.read(codes)
    cold_read = time.monotonic() - started
    started = time.monotonic()
    assert reader.read(codes) == cold
    warm_read = time.monotonic() - started
    started = time.monotonic()
    index.publish(codes, now + timedelta(minutes=1))
    repeat_publish = time.monotonic() - started
    assert reader.read(codes) == cold
    gaps = HistoryDownloadIndex(root / "control.sqlite3")
    gaps.initialize()
    with closing(OpenSqliteConnection(root / "control.sqlite3")) as connection, connection:
        connection.executemany("INSERT INTO download_gaps VALUES(?,?)", ((code, "2026-10-16") for code in codes))
    started = time.monotonic()
    # Reproduce the retired per-stock connection/query workload, only on this fixture.
    all_gaps = 0
    for code in codes:
        with closing(OpenSqliteConnection(root / "control.sqlite3")) as connection:
            all_gaps += len(
                connection.execute(
                    "SELECT gap.trade_date,unusable.checked_cutoff FROM download_gaps gap "
                    "LEFT JOIN download_qfq_unusable unusable ON unusable.code=gap.code AND unusable.trade_date=gap.trade_date "
                    "WHERE gap.code=? ORDER BY gap.trade_date",
                    (code,),
                ).fetchall()
            )
    individual = time.monotonic() - started
    started = time.monotonic()
    batched = gaps.gaps_by_code((now.date(),))
    batch = time.monotonic() - started
    assert all_gaps == sum(len(days) for days in batched.values()) == len(codes)
    return {
        "company_write_seconds": write,
        "company_publish_seconds": publish,
        "company_read_cold_seconds": cold_read,
        "company_read_warm_seconds": warm_read,
        "company_reuse_publish_seconds": repeat_publish,
        "gap_individual_seconds": individual,
        "gap_batch_seconds": batch,
        "stocks": len(codes),
        "physical_http_requests": 0,
    }


def Main() -> int:
    rounds = []
    for _ in range(3):
        with TemporaryDirectory(prefix="trader-download-perf-") as directory:
            rounds.append(_measure(Path(directory)))
    summary = {key: round(median(float(result[key]) for result in rounds), 6) for key in rounds[0]}
    emit_report(
        {
            "schema_version": "download-performance",
            "status": "passed",
            "summary": {"fixture": True, "rounds": 3, "medians": summary},
            "findings": [],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())

"""Published-history fixtures and end-to-end SQLite/Parquet preparation probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import struct
import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.history_revision import HistoryRevision, HistoryTrainingRow
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_inspection import LoadPublishedHistorySnapshot
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.infra.settings.data_paths import DataPaths
from trader.training.infra.history.aligned_training_rows import AlignTrainingRows

from .history_parquet import ParquetHistoryProjection
from .history_write_benchmark import HistoryFixtureSupplier


def PrepareTrainingHistory(root: Path, *, size: int, sessions: int) -> Path:
    days = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(sessions))
    supplier = HistoryFixtureSupplier(days, size, varying_prices=True)
    project = Path(__file__).resolve().parents[2]
    config = replace(
        LoadHistorySyncConfiguration(project, DataPaths(root)), sessions=sessions,
        reread_sessions=min(5, sessions), minimum_free_bytes=0,
    )
    supplier.publish_reference(config.reference_root / "company_st.sqlite3", days[-1], sessions)
    now = datetime.combine(days[-1], datetime.min.time(), ZoneInfo("Asia/Shanghai")).replace(hour=20, minute=30)
    status = RunHistorySync(config, supplier, clock=lambda: now)
    if status.state != "completed":
        raise RuntimeError(f"training_probe_history_incomplete:{status.reason}")
    history = LoadPublishedHistorySnapshot(config.history_root)
    reference = history.snapshot.partitions[-1]
    partition = HistoryPricePartition(
        config.history_root / reference.relative_path, reference.calendar_year, reference.calendar_month
    )
    source = supplier.fetch_baostock_prices(supplier.load_context(days[-1], sessions).universe[0], (days[-1],)).batch.cells[0]
    assert source.qfq is not None and source.qfq.amount is not None
    cell = replace(source, qfq=replace(source.qfq, amount=source.qfq.amount + 1234))
    partition.save_revisions((HistoryRevision(2, "main", cell, False, "fixture", "fixture"),))
    second = replace(history.snapshot, sequence=2, partitions=(*history.snapshot.partitions[:-1], partition.seal(2)))
    HistoryControlIndex(config.history_root / "catalog.sqlite3").publish_snapshot(second)
    # A third committed revision is deliberately not published and must be invisible.
    hidden = replace(source, qfq=replace(source.qfq, amount=source.qfq.amount + 9876))
    partition.save_revisions((HistoryRevision(3, "main", hidden, False, "fixture", "fixture"),))
    return config.history_root


def TrainingRowsHash(rows: Iterable[HistoryTrainingRow]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(str((row.code, row.trade_date, row.board, row.industry, row.cell_status)).encode())
        point = row.point
        digest.update(b"0" if point is None else b"1")
        if point is not None:
            for value in (point.qfq_close_price, point.qfq_amount, point.qfq_open,
                          point.qfq_high, point.qfq_low, point.raw_volume):
                digest.update(b"N" if value is None else b"F" + struct.pack("<d", value))
        count += 1
    return digest.hexdigest(), count


def MeasurePreparation(history_root: Path, output: Path, mode: str) -> dict[str, object]:
    initial = LoadPublishedHistorySnapshot(history_root, sequence=1)
    updated = LoadPublishedHistorySnapshot(history_root, sequence=2)
    projection = ParquetHistoryProjection(output)
    cases = {}
    first_files: set[str] = set()
    reused = 0
    for name, history in (("initial", initial), ("repeat", initial), ("incremental", updated)):
        started = time.perf_counter()
        export = 0.0
        if mode == "parquet" and name != "repeat":
            publication = projection.publish(history)
            export = time.perf_counter() - started
            files = {item.filename for item in publication.months}
            if name == "initial":
                first_files = files
            else:
                reused = len(first_files & files)
                if reused != len(first_files) - 1:
                    raise RuntimeError("parquet_incremental_month_reuse_failed")
        dates = history.calendar.open_dates
        codes = tuple(item.code for item in history.universe.securities)
        rows = (
            projection.iter_rows(history, dates) if mode == "parquet"
            else HistorySnapshotReader(history.root).iter_training_rows(history.snapshot, dates, codes)
        )
        fingerprint, count = TrainingRowsHash(AlignTrainingRows(rows, dates))
        cases[name] = {
            "seconds": time.perf_counter() - started, "export_seconds": export,
            "numeric_hash": fingerprint, "rows": count,
        }
    return {
        "cases": cases, "reused_months": reused,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("sqlite", "parquet"), required=True)
    args = parser.parse_args()
    print(json.dumps(MeasurePreparation(args.history, args.output, args.mode)))

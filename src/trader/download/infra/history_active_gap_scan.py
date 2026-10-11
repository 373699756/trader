"""Read active monthly partitions for unresolved raw/qfq price pairs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import date
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal

from trader.download.domain.history_control import HistoryActiveSnapshot
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity

GapScanProgress = Callable[[Literal["started", "waiting", "completed"], int, int, str], None]


@contextmanager
def _gap_index(root: Path) -> Iterator[sqlite3.Connection]:
    try:
        with closing(sqlite3.connect(root / "control.sqlite3", timeout=5.0)) as connection:
            yield connection
    except sqlite3.Error as exc:
        raise RuntimeError("history_gap_index_unavailable") from exc


def scan_active_price_gaps(
    root: Path,
    active: HistoryActiveSnapshot | None,
    context: HistorySupplierContext,
    cancel_requested: Callable[[], bool],
    progress: GapScanProgress,
) -> dict[str, tuple[date, ...]]:
    """Scan each active partition while exposing its otherwise long read boundary."""

    if active is None:
        return {}
    gaps: dict[str, list[date]] = defaultdict(list)
    codes = frozenset(item.code for item in context.universe)
    dates = frozenset(context.calendar.open_dates)
    total = len(active.partitions)
    with _gap_index(root) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS active_price_gaps ("
            "path TEXT PRIMARY KEY, identity TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL)"
        )
        paths = tuple(reference.relative_path for reference in active.partitions)
        connection.execute(
            "DELETE FROM active_price_gaps WHERE path NOT IN (" + ",".join("?" for _ in paths) + ")", paths
        )
        connection.commit()
    for index, reference in enumerate(active.partitions, start=1):
        if cancel_requested():
            raise KeyboardInterrupt
        relative_path = PurePosixPath(reference.relative_path)
        year, month = int(relative_path.parts[1]), int(relative_path.stem)
        current_item = f"{year:04d}-{month:02d}"
        progress("started", index - 1, total, current_item)
        activity = HistorySqliteActivity(partial(progress, "waiting", index - 1, total, current_item), cancel_requested)
        partition = SQLiteHistoryMonthPartitionRepository(
            root / reference.relative_path, year, month, activity=activity
        )
        path = root / reference.relative_path
        identity = _identity(path, reference.sha256, active.sequence)
        values = _cached_gaps(root, reference.relative_path, identity)
        if values is None:
            try:
                values = partition.missing_price_dates(active.sequence)
            except sqlite3.Error as exc:
                raise RuntimeError("history_gap_scan_failed") from exc
            if _identity(path, reference.sha256, active.sequence) != identity:
                raise RuntimeError("history_gap_scan_input_changed")
            payload = json.dumps([(code, day.isoformat()) for code, day in values], separators=(",", ":"))
            digest = hashlib.sha256((identity + payload).encode()).hexdigest()
            with _gap_index(root) as connection, connection:
                connection.execute(
                    "INSERT OR REPLACE INTO active_price_gaps VALUES (?,?,?,?)",
                    (reference.relative_path, identity, payload, digest),
                )
        for code, day in values:
            if code in codes and day in dates:
                gaps[code].append(day)
        progress("completed", index, total, current_item)
    return {code: tuple(sorted(days)) for code, days in gaps.items()}


def _identity(path: Path, sha256: str, sequence: int) -> str:
    stat = path.stat()
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("history_gap_scan_pending_wal")
    return json.dumps((sha256, sequence, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def _cached_gaps(root: Path, relative_path: str, identity: str) -> tuple[tuple[str, date], ...] | None:
    with _gap_index(root) as connection:
        row = connection.execute(
            "SELECT payload,digest FROM active_price_gaps WHERE path=? AND identity=?", (relative_path, identity)
        ).fetchone()
    if row is None or not isinstance(row[0], str) or hashlib.sha256((identity + row[0]).encode()).hexdigest() != row[1]:
        return None
    try:
        values = json.loads(row[0])
        if not isinstance(values, list) or any(
            not isinstance(item, list) or len(item) != 2 or not all(isinstance(value, str) for value in item)
            for item in values
        ):
            return None
        return tuple((code, date.fromisoformat(day)) for code, day in values)
    except (ValueError, TypeError):
        return None


__all__ = ["scan_active_price_gaps"]

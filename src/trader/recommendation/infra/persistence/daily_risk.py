"""Trade-date partitioned persistence for recommendation research evidence."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.recommendation.infra.persistence.data_plane import SQLiteDataPlane


class SQLiteDailyRiskIndex:
    """Route risk evidence to one self-contained SQLite database per trade date."""

    def __init__(self, root: Path, *, target_date: Callable[[], date]) -> None:
        self._root = root
        self._target_date = target_date
        self._lock = threading.RLock()
        self._prepared: set[date] = set()
        self._planes: dict[date, SQLiteDataPlane] = {}

    def initialize(self) -> None:
        self.prepare(self._target_date())

    def prepare(self, trade_date: date) -> Path:
        with self._lock:
            database = self.database_for(trade_date)
            if trade_date in self._prepared and database.is_file():
                return database
            self._root.mkdir(parents=True, exist_ok=True)
            if not database.exists():
                self._publish_seeded_partition(trade_date, database)
            plane = self._plane(trade_date)
            plane.initialize()
            _require_quick_check(database)
            self._prepared.add(trade_date)
            return database

    def database_for(self, trade_date: date) -> Path:
        return self._root / f"{trade_date.isoformat()}.sqlite3"

    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None:
        trade_date = self._target_date()
        self.prepare(trade_date)
        self._plane(trade_date).save_risk_evidence_recent(record)

    def save_risk_evidence_recent_records(self, records: Sequence[RiskEvidenceRecord]) -> None:
        trade_date = self._target_date()
        self.prepare(trade_date)
        self._plane(trade_date).save_risk_evidence_recent_records(records)

    def load_risk_evidence_recent(self, code: str, evidence_id: str) -> RiskEvidenceRecord | None:
        trade_date = self._target_date()
        self.prepare(trade_date)
        return self._plane(trade_date).load_risk_evidence_recent(code, evidence_id)

    def load_risk_evidence_recent_records(self, codes: Sequence[str] | None = None) -> tuple[RiskEvidenceRecord, ...]:
        trade_date = self._target_date()
        self.prepare(trade_date)
        return self._plane(trade_date).load_risk_evidence_recent_records(codes)

    def scan_risk_evidence_recent_records(
        self,
        consume: Callable[[RiskEvidenceRecord], None],
        codes: Sequence[str] | None = None,
    ) -> None:
        trade_date = self._target_date()
        self.prepare(trade_date)
        self._plane(trade_date).scan_risk_evidence_recent_records(consume, codes)

    def save_source_cursor_recent(self, record: SourceCursorRecord) -> None:
        trade_date = self._target_date()
        self.prepare(trade_date)
        self._plane(trade_date).save_source_cursor_recent(record)

    def load_source_cursor_recent(self, cursor_name: str) -> SourceCursorRecord | None:
        trade_date = self._target_date()
        self.prepare(trade_date)
        return self._plane(trade_date).load_source_cursor_recent(cursor_name)

    def load_source_cursor_recent_records(
        self, cursor_names: Sequence[str] | None = None
    ) -> tuple[SourceCursorRecord, ...]:
        trade_date = self._target_date()
        self.prepare(trade_date)
        return self._plane(trade_date).load_source_cursor_recent_records(cursor_names)

    def _plane(self, trade_date: date) -> SQLiteDataPlane:
        with self._lock:
            plane = self._planes.get(trade_date)
            if plane is None:
                plane = SQLiteDataPlane(self._root, database_path=self.database_for(trade_date))
                self._planes[trade_date] = plane
            return plane

    def _publish_seeded_partition(self, trade_date: date, database: Path) -> None:
        previous = self._latest_partition_before(trade_date)
        temporary = database.with_name(f".{database.name}.{os.getpid()}.building")
        try:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            if previous is None:
                SQLiteDataPlane(self._root, database_path=temporary).initialize()
            else:
                _require_quick_check(previous)
                source_connection = sqlite3.connect(f"file:{previous}?mode=ro", uri=True)
                target_connection = sqlite3.connect(temporary)
                try:
                    source_connection.backup(target_connection)
                finally:
                    target_connection.close()
                    source_connection.close()
            _checkpoint_partition(temporary)
            _require_quick_check(temporary)
            os.replace(temporary, database)
            _remove_sidecars(temporary)
        except (OSError, sqlite3.DatabaseError) as exc:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            _remove_sidecars(temporary)
            raise DataPlaneUnavailableError("daily risk partition preparation failed") from exc

    def _latest_partition_before(self, trade_date: date) -> Path | None:
        candidates: list[tuple[date, Path]] = []
        for path in self._root.glob("????-??-??.sqlite3"):
            try:
                partition_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if partition_date < trade_date:
                candidates.append((partition_date, path))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]


def _require_quick_check(database: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise DataPlaneUnavailableError("daily risk partition cannot be verified") from exc
    if row is None or str(row[0]).lower() != "ok":
        raise DataPlaneUnavailableError("daily risk partition quick_check failed")


def _checkpoint_partition(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        connection.close()


def _remove_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            database.with_name(f"{database.name}{suffix}").unlink()
        except FileNotFoundError:
            pass


__all__ = ["SQLiteDailyRiskIndex"]

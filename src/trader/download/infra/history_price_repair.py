"""Durable whole-stock replacement journal; replay completes before snapshot publication."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast
from typing import TYPE_CHECKING

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.download.domain.history_price_qualification import QualifyHistoryPairs, QualifyReplacementWindow
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
if TYPE_CHECKING:
    from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.price_repair_attempts import PRICE_REPAIR_ATTEMPT_SQL, PRICE_SOURCE_CHECK_SQL, PriceRepairAttempts, RepairSource, TencentRecheckDue, RecordTencentCheck
from trader.download.infra.published_history_codec import DecodePublishedHistoryCellPayload, EncodePublishedHistoryCell
from trader.infra.sqlite_codec import DecodeDate, EncodeDate
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction

HISTORY_REPAIR_SQL = (
    PRICE_SOURCE_CHECK_SQL,
    PRICE_REPAIR_ATTEMPT_SQL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1),
    "CREATE TABLE IF NOT EXISTS price_source(code TEXT NOT NULL, sync_sequence INTEGER NOT NULL, source TEXT NOT NULL, "
    "PRIMARY KEY(code,sync_sequence)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS price_replacement(code TEXT NOT NULL, trade_date INTEGER NOT NULL, "
    "sync_sequence INTEGER NOT NULL, board TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
)


class HistoryPriceRepair:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "catalog.sqlite3"

    def initialize(self) -> None:
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            for statement in HISTORY_REPAIR_SQL:
                connection.run(statement)

    def source(self, code: str, sequence: int) -> RepairSource:
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
            row = connection.run(
                "SELECT source FROM price_source WHERE code=? AND sync_sequence<=? ORDER BY sync_sequence DESC LIMIT 1",
                (code, sequence),
            ).fetchone()
        return cast(RepairSource, row[0]) if row else "tencent"

    def baostock_codes(self) -> frozenset[str]:
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
            return frozenset(row[0] for row in connection.run("SELECT p.code FROM price_source p WHERE p.source='baostock' AND p.sync_sequence=(SELECT MAX(s.sync_sequence) FROM price_source s WHERE s.code=p.code)"))

    def recheck_due(self, codes: frozenset[str], calendar: tuple[date, ...]) -> frozenset[str]:
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
            return frozenset(code for code in codes if TencentRecheckDue(connection, code, calendar))

    def record_tencent_check(self, code: str, day: date) -> None:
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            RecordTencentCheck(connection, code, day)

    def reserve(self, code: str, dates: tuple[date, ...], batch: str, *, explicit: bool) -> tuple[date, ...]:
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            return PriceRepairAttempts(connection).reserve(code, dates, batch, explicit=explicit)

    def fail(self, code: str, batch: str, reason: str) -> None:
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            PriceRepairAttempts(connection).fail(code, batch, reason)

    def replace(
        self, security: BaoStockSecurity, dates: tuple[date, ...], incoming: PublishedHistoryWindow,
        prices: HistoryDirectPrices, sequence: int, *, source: RepairSource = "baostock",
    ) -> None:
        previous = PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(c) for c in prices.read(security.code, dates)))
        candidate = QualifyReplacementWindow(dates, previous, incoming)
        with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
            self._require_unpublished(connection.run(
                "SELECT MAX(sync_sequence) FROM published_snapshot"
            ).require_one()[0], sequence)
            connection.run_many(
                "INSERT INTO price_replacement VALUES(?,?,?,?,?,?)",
                ((security.code, EncodeDate(c.trade_date), sequence, security.board, source, EncodePublishedHistoryCell(c))
                 for c in candidate.cells),
            )
        self.replay(sequence)

    @staticmethod
    def _require_unpublished(published: int | None, sequence: int) -> None:
        if published is not None and published >= sequence:
            raise RuntimeError("history_replacement_already_published")

    def replay(self, sequence: int) -> None:
        # The complete validated candidate is durable before touching annual files.
        # A crash may leave some annual writes; replay needs no second supplier call.
        with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
            self._require_unpublished(connection.run("SELECT MAX(sync_sequence) FROM published_snapshot").require_one()[0], sequence)
            codes = tuple(row[0] for row in connection.run("SELECT DISTINCT code FROM price_replacement ORDER BY code"))
        for code in codes:
            with SqliteSession(self._path, SqliteConnectionOptions(read_only=True)) as connection:
                rows = connection.run(
                    "SELECT trade_date,sync_sequence,board,source,payload FROM price_replacement WHERE code=? ORDER BY trade_date", (code,)
                ).fetchall()
            if any(row[1] != sequence for row in rows):
                raise RuntimeError("history_replacement_sequence_conflict")
            dates = tuple(DecodeDate(row[0]) for row in rows)
            window = PublishedHistoryWindow(code, tuple(DecodePublishedHistoryCellPayload(code, day.isoformat(), row[4]) for day, row in zip(dates, rows, strict=True)))
            QualifyReplacementWindow(dates, PublishedHistoryWindow(code, ()), window)
            if any(row[2:4] != rows[0][2:4] for row in rows) or rows[0][3] not in ("tencent", "baostock"):
                raise RuntimeError("history_replacement_source_conflict")
            cells = QualifyHistoryPairs(code, dates, window)
            board = cast(BaoStockBoard, rows[0][2])
            source = cast(RepairSource, rows[0][3])
            for year in sorted({day.year for day in dates}):
                path = self._root / f"prices_{year:04d}.sqlite3"
                partition = HistoryPricePartition(path, year, 1)
                if not path.exists():
                    partition.initialize()
                partition.replace_unpublished_code(
                    tuple(HistoryRevision(sequence, board, cell, None, None, None) for cell in cells if cell.trade_date.year == year), sequence
                )
            with SqliteSession(self._path) as connection, SqliteTransaction(connection, immediate=True):
                connection.run("INSERT OR REPLACE INTO price_source VALUES(?,?,?)", (code, sequence, source))
                RecordTencentCheck(connection, code, dates[-1], initial=source == "baostock")
                for table in ("download_gaps", "download_qfq_unusable"):
                    connection.run_many(f"DELETE FROM {table} WHERE code=? AND trade_date=?", ((code, EncodeDate(day)) for day in dates))
                PriceRepairAttempts(connection).resolve(code, dates, source)
                connection.run("UPDATE download_tasks SET tencent_done=1,failure=NULL WHERE code=?", (code,))
                connection.run("DELETE FROM price_replacement WHERE code=?", (code,))

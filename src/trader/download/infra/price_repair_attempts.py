"""Repair receipts owned by the caller's price database and transaction.

Reserve before supplier I/O. Resolve only in the transaction that confirms the
price publication. This adapter never opens a connection or commits a transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast

from trader.infra.sqlite_codec import DecodeDate, EncodeDate
from trader.infra.sqlite_connection import SqliteConnection

RepairState = Literal["requested", "failed", "resolved"]
RepairSource = Literal["tencent", "baostock"]

PRICE_REPAIR_ATTEMPT_SQL = """
CREATE TABLE price_repair_attempt (
    code TEXT NOT NULL, trade_date INTEGER NOT NULL,
    batch_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('requested','failed','resolved')),
    reason TEXT NOT NULL, resolved_source TEXT CHECK(resolved_source IN ('tencent','baostock')),
    CHECK((state='resolved')=(resolved_source IS NOT NULL)),
    PRIMARY KEY(code,trade_date)
) WITHOUT ROWID
"""
PRICE_SOURCE_CHECK_SQL = "CREATE TABLE IF NOT EXISTS price_tencent_check(code TEXT PRIMARY KEY, trade_date INTEGER NOT NULL) WITHOUT ROWID"


def TencentRecheckDue(connection: SqliteConnection, code: str, calendar: tuple[date, ...]) -> bool:
    row = connection.run("SELECT trade_date FROM price_tencent_check WHERE code=?", (code,)).fetchone()
    return row is None or sum(day > DecodeDate(row[0]) for day in calendar) >= 20


def RecordTencentCheck(connection: SqliteConnection, code: str, day: date, *, initial: bool = False) -> None:
    if not connection.in_transaction:
        raise RuntimeError("price_repair_requires_transaction")
    statement = "INSERT OR IGNORE" if initial else "INSERT OR REPLACE"
    connection.run(f"{statement} INTO price_tencent_check VALUES(?,?)", (code, EncodeDate(day)))


@dataclass(frozen=True)
class PriceRepairAttempt:
    code: str
    trade_date: date
    batch_id: str
    state: RepairState
    reason: str
    resolved_source: RepairSource | None


class PriceRepairAttempts:
    """Use the history or market writer's connection, never a third database."""

    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def read(self, code: str) -> tuple[PriceRepairAttempt, ...]:
        _require_code(code)
        return tuple(
            PriceRepairAttempt(
                code,
                DecodeDate(day),
                batch,
                cast(RepairState, state),
                reason,
                cast(RepairSource | None, source),
            )
            for day, batch, state, reason, source in self._connection.run(
                "SELECT trade_date,batch_id,state,reason,resolved_source FROM price_repair_attempt "
                "WHERE code=? ORDER BY trade_date", (code,)
            )
        )

    def reserve(
        self,
        code: str,
        dates: tuple[date, ...],
        batch_id: str,
        *,
        explicit: bool = False,
    ) -> tuple[date, ...]:
        """Consume automatic eligibility once; explicit retries remain batch-idempotent."""
        self._require_transaction()
        _require_code(code)
        _require_dates(dates)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch_id) is None:
            raise ValueError("price_repair_batch_invalid")
        reserved: list[date] = []
        for day in dates:
            cursor = self._connection.run(
                "INSERT INTO price_repair_attempt VALUES(?,?,?,'requested','supplier_result_unknown',NULL) "
                "ON CONFLICT(code,trade_date) DO UPDATE SET batch_id=excluded.batch_id,state='requested',"
                "reason='supplier_result_unknown',resolved_source=NULL "
                "WHERE ? AND price_repair_attempt.state<>'resolved' AND price_repair_attempt.batch_id<>excluded.batch_id",
                (code, EncodeDate(day), batch_id, int(explicit)),
            )
            if cursor.rowcount:
                reserved.append(day)
        return tuple(reserved)

    def fail(self, code: str, batch_id: str, reason: str) -> None:
        self._require_transaction()
        _require_code(code)
        if re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None:
            raise ValueError("price_repair_reason_invalid")
        self._connection.run(
            "UPDATE price_repair_attempt SET state='failed',reason=? "
            "WHERE code=? AND batch_id=? AND state='requested'",
            (reason, code, batch_id),
        )

    def resolve(self, code: str, dates: tuple[date, ...], source: RepairSource) -> None:
        """The owning publisher calls this only after confirming these price cells."""
        self._require_transaction()
        _require_code(code)
        _require_dates(dates)
        if source not in ("tencent", "baostock"):
            raise ValueError("price_repair_source_invalid")
        self._connection.run_many(
            "UPDATE price_repair_attempt SET state='resolved',reason='repaired',resolved_source=? "
            "WHERE code=? AND trade_date=? AND state<>'resolved'",
            ((source, code, EncodeDate(day)) for day in dates),
        )

    def _require_transaction(self) -> None:
        if not self._connection.in_transaction:
            raise RuntimeError("price_repair_requires_transaction")


def _require_code(code: str) -> None:
    if re.fullmatch(r"[0-9]{6}", code) is None:
        raise ValueError("price_repair_code_invalid")


def _require_dates(dates: tuple[date, ...]) -> None:
    if any(type(day) is not date for day in dates) or dates != tuple(sorted(set(dates))):
        raise ValueError("price_repair_dates_invalid")

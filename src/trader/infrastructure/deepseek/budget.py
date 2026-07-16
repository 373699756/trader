"""Atomic physical-request reservation for the 188-call daily budget."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.domain.models import Strategy


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reservation_id: str
    bucket: str
    reason: str


class DeepSeekBudgetStore:
    def __init__(
        self,
        database_path: Path,
        *,
        daily_hard_limit: int,
        strategy_limits: Mapping[str, int],
    ) -> None:
        if not 0 <= daily_hard_limit <= 188:
            raise ValueError("daily hard limit must be between 0 and 188")
        if sum(strategy_limits.values()) != daily_hard_limit:
            raise ValueError("strategy limits must sum to daily hard limit")
        self._path = database_path
        self._daily_hard_limit = daily_hard_limit
        self._limits = dict(strategy_limits)
        self._initialized = False

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deepseek_call_reservations(
                    reservation_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    http_status INTEGER,
                    latency_ms REAL,
                    token_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deepseek_budget_day ON deepseek_call_reservations(trade_date, strategy, bucket)"
            )
        self._initialized = True

    def reserve(
        self,
        strategy: Strategy,
        *,
        phase: str,
        requested_at: datetime,
        bucket: str | None = None,
        emergency: bool = False,
    ) -> BudgetReservation:
        trade_date = requested_at.date().isoformat()
        requested_bucket = "emergency" if emergency else (bucket or strategy.value)
        if requested_bucket not in self._limits:
            return BudgetReservation(False, "", requested_bucket, "unknown_bucket")
        reservation_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM deepseek_call_reservations WHERE trade_date = ?",
                    (trade_date,),
                ).fetchone()[0]
            )
            if total >= self._daily_hard_limit:
                connection.rollback()
                return BudgetReservation(False, "", requested_bucket, "daily_hard_limit")
            bucket_limit = self._limits[requested_bucket]
            bucket_used = int(
                connection.execute(
                    "SELECT COUNT(*) FROM deepseek_call_reservations WHERE trade_date = ? AND bucket = ?",
                    (trade_date, requested_bucket),
                ).fetchone()[0]
            )
            if bucket_used >= bucket_limit:
                connection.rollback()
                return BudgetReservation(False, "", requested_bucket, "bucket_limit")
            connection.execute(
                """
                INSERT INTO deepseek_call_reservations(
                    reservation_id, trade_date, strategy, bucket, phase, requested_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved')
                """,
                (
                    reservation_id,
                    trade_date,
                    strategy.value,
                    requested_bucket,
                    phase,
                    requested_at.isoformat(),
                ),
            )
            connection.commit()
        return BudgetReservation(True, reservation_id, requested_bucket, "reserved")

    def finish(
        self,
        reservation_id: str,
        *,
        status: str,
        error: str = "",
        http_status: int | None = None,
        latency_ms: float | None = None,
        token_count: int = 0,
    ) -> None:
        terminal = {"success", "failed", "abandoned"}
        if status not in terminal:
            raise ValueError(f"invalid physical call terminal status: {status}")
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE deepseek_call_reservations
                SET status = ?, error = ?, http_status = ?, latency_ms = ?, token_count = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (status, error[:1000], http_status, latency_ms, max(0, token_count), reservation_id),
            ).rowcount
            if changed != 1:
                raise KeyError(f"unknown or completed reservation: {reservation_id}")

    def abandon_reserved(self) -> int:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE deepseek_call_reservations SET status = 'abandoned', error = 'process_restart' WHERE status = 'reserved'"
            ).rowcount
        return int(changed)

    def summary(self, day: str) -> dict[str, object]:
        if not self._initialized:
            return {
                "used": 0,
                "remaining": self._daily_hard_limit,
                "by_bucket": {},
                "by_status": {},
            }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bucket, status, COUNT(*)
                FROM deepseek_call_reservations
                WHERE trade_date = ?
                GROUP BY bucket, status
                """,
                (day,),
            ).fetchall()
        by_bucket: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for bucket, status, count in rows:
            by_bucket[str(bucket)] = by_bucket.get(str(bucket), 0) + int(count)
            by_status[str(status)] = by_status.get(str(status), 0) + int(count)
        used = sum(by_bucket.values())
        return {
            "used": used,
            "remaining": max(0, self._daily_hard_limit - used),
            "by_bucket": by_bucket,
            "by_status": by_status,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


__all__ = ["BudgetReservation", "DeepSeekBudgetStore"]

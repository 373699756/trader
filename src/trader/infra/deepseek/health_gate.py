"""Persisted DeepSeek transport/schema/application health gate."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.infra.deepseek.budget_batch_ledger import BudgetBatchCompletion

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MORNING_HALF_OPEN_CUTOFF = time(11, 15)
_AFTERNOON_START = time(13, 0)
_AFTERNOON_HALF_OPEN_CUTOFF = time(14, 43)


@dataclass(frozen=True)
class DeepSeekHealthPolicy:
    consecutive_failure_limit: int
    rolling_window: int
    minimum_application_ratio: float
    healthy_application_ratio: float
    healthy_batch_count: int
    cooldown_seconds: int


@dataclass(frozen=True)
class _HealthOutcome:
    completion: BudgetBatchCompletion
    trade_date: str
    transport_failed: bool
    schema_failed: bool
    accepted_count: int


@dataclass(frozen=True)
class _HealthStateUpdate:
    mode: str
    completed_at: datetime
    recovery_successes: int
    reason: str
    open_until: datetime | None = None


class DeepSeekHealthGate:
    def __init__(
        self,
        connect: Callable[[], AbstractContextManager[sqlite3.Connection]],
        policy: DeepSeekHealthPolicy,
    ) -> None:
        self._connect = connect
        self._policy = policy

    @staticmethod
    def initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS deepseek_health_events(
                batch_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                transport_failed INTEGER NOT NULL,
                schema_failed INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL,
                accepted_count INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_deepseek_health_events_day
            ON deepseek_health_events(trade_date, completed_at);

            CREATE TABLE IF NOT EXISTS deepseek_health_state(
                trade_date TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                open_until TEXT,
                probe_batch_id TEXT NOT NULL DEFAULT '',
                recovery_successes INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )

    def reservation_rejection(
        self,
        connection: sqlite3.Connection,
        *,
        trade_date: str,
        requested_at: datetime,
        batch_id: str,
        candidate_count: int,
    ) -> str:
        row = connection.execute(
            """
            SELECT mode, open_until, probe_batch_id
            FROM deepseek_health_state WHERE trade_date = ?
            """,
            (trade_date,),
        ).fetchone()
        if row is None or str(row[0]) in {"closed", "recovering"}:
            return ""
        mode = str(row[0])
        open_until = datetime.fromisoformat(str(row[1])) if row[1] else None
        probe_batch_id = str(row[2] or "")
        if mode == "half_open":
            return "" if probe_batch_id == batch_id else "circuit_open"
        if mode != "open" or open_until is None or requested_at < open_until:
            return "circuit_open"
        if not _half_open_allowed(requested_at) or not 1 <= candidate_count <= 2 or not batch_id:
            return "circuit_open"
        connection.execute(
            """
            UPDATE deepseek_health_state
            SET mode = 'half_open', probe_batch_id = ?, updated_at = ?
            WHERE trade_date = ? AND mode = 'open'
            """,
            (batch_id, requested_at.isoformat(), trade_date),
        )
        return ""

    def record_completion(self, completion: BudgetBatchCompletion) -> None:
        if completion.physical_attempts <= 0:
            return
        trade_date = completion.completed_at.date().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            success_calls, failed_calls = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                FROM deepseek_call_reservations WHERE batch_id = ?
                """,
                (completion.batch_id,),
            ).fetchone()
            candidate_count = int(
                connection.execute(
                    "SELECT candidate_count FROM deepseek_review_batches WHERE batch_id = ?",
                    (completion.batch_id,),
                ).fetchone()[0]
            )
            accepted_count = sum(
                review.outcome.value in {"applied", "abstain"} for review in completion.reviews.values()
            )
            successful = int(success_calls or 0)
            failed = int(failed_calls or 0)
            transport_failed = failed > 0 and successful == 0
            schema_failed = completion.status == "failed" and successful > 0 and accepted_count == 0
            connection.execute(
                """
                INSERT OR REPLACE INTO deepseek_health_events(
                    batch_id, trade_date, completed_at, transport_failed, schema_failed,
                    candidate_count, accepted_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    completion.batch_id,
                    trade_date,
                    completion.completed_at.isoformat(),
                    int(transport_failed),
                    int(schema_failed),
                    candidate_count,
                    accepted_count,
                ),
            )
            self._advance_state(
                connection,
                _HealthOutcome(
                    completion=completion,
                    trade_date=trade_date,
                    transport_failed=transport_failed,
                    schema_failed=schema_failed,
                    accepted_count=accepted_count,
                ),
            )
            connection.commit()

    def summary(self, trade_date: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT mode, open_until, recovery_successes, reason
                FROM deepseek_health_state WHERE trade_date = ?
                """,
                (trade_date,),
            ).fetchone()
        if row is None:
            return {"mode": "closed", "open_until": None, "recovery_successes": 0, "reason": ""}
        return {
            "mode": str(row[0]),
            "open_until": str(row[1]) if row[1] else None,
            "recovery_successes": int(row[2]),
            "reason": str(row[3]),
        }

    def _advance_state(
        self,
        connection: sqlite3.Connection,
        outcome: _HealthOutcome,
    ) -> None:
        completion = outcome.completion
        trade_date = outcome.trade_date
        state = connection.execute(
            """
            SELECT mode, probe_batch_id, recovery_successes
            FROM deepseek_health_state WHERE trade_date = ?
            """,
            (trade_date,),
        ).fetchone()
        mode = str(state[0]) if state is not None else "closed"
        probe_batch_id = str(state[1]) if state is not None else ""
        recovery_successes = int(state[2]) if state is not None else 0
        valid = not outcome.transport_failed and not outcome.schema_failed and outcome.accepted_count > 0
        if mode == "half_open" and probe_batch_id == completion.batch_id:
            if valid:
                self._write_state(
                    connection,
                    trade_date,
                    _HealthStateUpdate(
                        mode="recovering",
                        completed_at=completion.completed_at,
                        recovery_successes=1,
                        reason="",
                    ),
                )
            else:
                self._open(connection, trade_date, completion.completed_at, "half_open_failed")
            return

        reason = self._trip_reason(connection, trade_date)
        if reason:
            self._open(connection, trade_date, completion.completed_at, reason)
            return
        if mode != "recovering":
            return
        recovery_successes = recovery_successes + 1 if valid else 0
        ratio = self._rolling_application_ratio(connection, trade_date)
        healthy = (
            recovery_successes >= self._policy.healthy_batch_count
            and ratio is not None
            and ratio >= self._policy.healthy_application_ratio
        )
        self._write_state(
            connection,
            trade_date,
            _HealthStateUpdate(
                mode="closed" if healthy else "recovering",
                completed_at=completion.completed_at,
                recovery_successes=0 if healthy else recovery_successes,
                reason="",
            ),
        )

    def _trip_reason(self, connection: sqlite3.Connection, trade_date: str) -> str:
        rows = connection.execute(
            """
            SELECT transport_failed, schema_failed
            FROM deepseek_health_events
            WHERE trade_date = ?
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (trade_date, self._policy.consecutive_failure_limit),
        ).fetchall()
        if len(rows) == self._policy.consecutive_failure_limit:
            if all(bool(row[0]) for row in rows):
                return "consecutive_transport_failures"
            if all(bool(row[1]) for row in rows):
                return "consecutive_schema_failures"
        ratio = self._rolling_application_ratio(connection, trade_date)
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM deepseek_health_events WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()[0]
        )
        if (
            count >= self._policy.rolling_window
            and ratio is not None
            and ratio < self._policy.minimum_application_ratio
        ):
            return "low_application_ratio"
        return ""

    def _rolling_application_ratio(
        self,
        connection: sqlite3.Connection,
        trade_date: str,
    ) -> float | None:
        rows = connection.execute(
            """
            SELECT candidate_count, accepted_count
            FROM deepseek_health_events
            WHERE trade_date = ?
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            (trade_date, self._policy.rolling_window),
        ).fetchall()
        candidates = sum(int(row[0]) for row in rows)
        return None if candidates <= 0 else sum(int(row[1]) for row in rows) / candidates

    def _open(
        self,
        connection: sqlite3.Connection,
        trade_date: str,
        completed_at: datetime,
        reason: str,
    ) -> None:
        self._write_state(
            connection,
            trade_date,
            _HealthStateUpdate(
                mode="open",
                completed_at=completed_at,
                open_until=completed_at + timedelta(seconds=self._policy.cooldown_seconds),
                recovery_successes=0,
                reason=reason,
            ),
        )

    @staticmethod
    def _write_state(
        connection: sqlite3.Connection,
        trade_date: str,
        update: _HealthStateUpdate,
    ) -> None:
        connection.execute(
            """
            INSERT INTO deepseek_health_state(
                trade_date, mode, open_until, probe_batch_id, recovery_successes, reason, updated_at
            ) VALUES (?, ?, ?, '', ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                mode = excluded.mode,
                open_until = excluded.open_until,
                probe_batch_id = '',
                recovery_successes = excluded.recovery_successes,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                trade_date,
                update.mode,
                update.open_until.isoformat() if update.open_until is not None else None,
                update.recovery_successes,
                update.reason,
                update.completed_at.isoformat(),
            ),
        )


def _half_open_allowed(value: datetime) -> bool:
    local = value.astimezone(_SHANGHAI).time().replace(tzinfo=None)
    return local < _MORNING_HALF_OPEN_CUTOFF or _AFTERNOON_START <= local < _AFTERNOON_HALF_OPEN_CUTOFF


__all__ = ["DeepSeekHealthGate", "DeepSeekHealthPolicy"]

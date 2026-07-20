"""Atomic DeepSeek request budgets and persisted review terminal states."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.domain.models import DeepSeekReview, Strategy

_BATCH_TERMINALS = frozenset({"success", "partial", "failed", "skipped", "abandoned"})
_CALL_TERMINALS = frozenset({"success", "failed", "abandoned"})
_CANDIDATE_TERMINALS = frozenset({"applied", "abstain", "rejected", "late"})
_EMERGENCY_REASONS = frozenset({"new_high_risk", "freeze_boundary_change"})

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reservation_id: str
    bucket: str
    reason: str
    stage: str = ""


class DeepSeekBudgetStore:
    def __init__(
        self,
        database_path: Path,
        *,
        daily_hard_limit: int,
        strategy_limits: Mapping[str, int],
        stage_targets: Mapping[str, int],
        stage_limits: Mapping[str, int],
    ) -> None:
        if not 0 <= daily_hard_limit <= 188:
            raise ValueError("daily hard limit must be between 0 and 188")
        if sum(strategy_limits.values()) != daily_hard_limit:
            raise ValueError("strategy limits must sum to daily hard limit")
        if set(stage_targets) != set(stage_limits):
            raise ValueError("stage targets and limits must contain the same stages")
        if any(stage_targets[name] > stage_limits[name] for name in stage_targets):
            raise ValueError("stage targets cannot exceed stage limits")
        if sum(stage_targets.values()) > daily_hard_limit or sum(stage_limits.values()) != daily_hard_limit:
            raise ValueError("stage targets must fit and stage limits must equal the daily hard limit")
        self._path = database_path
        self._daily_hard_limit = daily_hard_limit
        self._limits = dict(strategy_limits)
        self._stage_targets = dict(stage_targets)
        self._stage_limits = dict(stage_limits)
        self._daily_target = sum(stage_targets.values())
        self._initialized = False

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deepseek_call_reservations(
                    reservation_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    stage_key TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    emergency_reason TEXT NOT NULL DEFAULT '',
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    http_status INTEGER,
                    latency_ms REAL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    timed_out INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS deepseek_calls(
                    call_id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    model TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    http_status INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    latency_ms REAL,
                    outcome TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_deepseek_budget_day
                ON deepseek_call_reservations(trade_date, strategy, bucket, stage_key);

                CREATE TABLE IF NOT EXISTS deepseek_review_batches(
                    batch_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    cache_hit_count INTEGER NOT NULL DEFAULT 0,
                    physical_attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_deepseek_batches_day
                ON deepseek_review_batches(trade_date, strategy, phase, status);

                CREATE TABLE IF NOT EXISTS deepseek_candidate_results(
                    batch_id TEXT NOT NULL REFERENCES deepseek_review_batches(batch_id),
                    stock_code TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(batch_id, stock_code)
                );
                """
            )
            _ensure_column(connection, "deepseek_call_reservations", "stage_key", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "deepseek_call_reservations", "batch_id", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "deepseek_call_reservations", "completed_at", "TEXT")
            _ensure_column(connection, "deepseek_call_reservations", "timed_out", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(
                connection,
                "deepseek_call_reservations",
                "emergency_reason",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_schema_version(connection)
        self._initialized = True

    def _ensure_schema_version(self, connection: sqlite3.Connection) -> None:
        if _current_schema_version(connection) < SCHEMA_VERSION:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def begin_batch(
        self,
        strategy: Strategy,
        *,
        phase: str,
        bucket: str,
        model: str,
        requested_at: datetime,
        deadline: datetime,
        candidate_codes: Sequence[str],
        cache_hit_count: int = 0,
    ) -> str:
        _require_aware(requested_at, "batch requested_at")
        _require_aware(deadline, "batch deadline")
        codes = tuple(candidate_codes)
        if len(codes) != len(set(codes)):
            raise ValueError("DeepSeek batch candidate codes must be unique")
        if cache_hit_count < 0 or cache_hit_count > len(codes):
            raise ValueError("DeepSeek batch cache hits must be within candidate count")
        batch_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO deepseek_review_batches(
                    batch_id, trade_date, strategy, phase, bucket, model, requested_at,
                    deadline, status, candidate_count, cache_hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    batch_id,
                    requested_at.date().isoformat(),
                    strategy.value,
                    phase,
                    bucket,
                    model,
                    requested_at.isoformat(),
                    deadline.isoformat(),
                    len(codes),
                    cache_hit_count,
                ),
            )
            connection.executemany(
                """
                INSERT INTO deepseek_candidate_results(batch_id, stock_code, outcome)
                VALUES (?, ?, 'pending')
                """,
                ((batch_id, code) for code in codes),
            )
            connection.commit()
        return batch_id

    def finish_batch(
        self,
        batch_id: str,
        *,
        status: str,
        completed_at: datetime,
        reviews: Mapping[str, DeepSeekReview],
        physical_attempts: int,
        error: str = "",
    ) -> None:
        if status not in _BATCH_TERMINALS - {"abandoned"}:
            raise ValueError(f"invalid review batch terminal status: {status}")
        _require_aware(completed_at, "batch completed_at")
        if physical_attempts < 0:
            raise ValueError("physical attempts cannot be negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET completed_at = ?, status = ?, physical_attempts = ?, error = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (completed_at.isoformat(), status, physical_attempts, error[:1000], batch_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise KeyError(f"unknown or completed DeepSeek batch: {batch_id}")
            for code, review in reviews.items():
                outcome = review.outcome.value
                if code != review.code or outcome not in _CANDIDATE_TERMINALS:
                    connection.rollback()
                    raise ValueError("DeepSeek candidate result does not match its batch code")
                updated = connection.execute(
                    """
                    UPDATE deepseek_candidate_results
                    SET outcome = ?, completed_at = ?, error = ?
                    WHERE batch_id = ? AND stock_code = ? AND outcome = 'pending'
                    """,
                    (outcome, review.completed_at.isoformat(), review.error[:500], batch_id, code),
                ).rowcount
                if updated != 1:
                    connection.rollback()
                    raise ValueError(f"candidate {code} is not pending in batch {batch_id}")
            connection.execute(
                """
                UPDATE deepseek_candidate_results
                SET outcome = 'rejected', completed_at = ?, error = ?
                WHERE batch_id = ? AND outcome = 'pending'
                """,
                (completed_at.isoformat(), f"batch_{status}", batch_id),
            )
            connection.commit()

    def set_batch_cache_hits(self, batch_id: str, count: int) -> None:
        if count < 0:
            raise ValueError("batch cache hits cannot be negative")
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET cache_hit_count = ?
                WHERE batch_id = ? AND status = 'running' AND candidate_count >= ?
                """,
                (count, batch_id, count),
            ).rowcount
            if changed != 1:
                raise KeyError(f"unknown batch or invalid cache hit count: {batch_id}")

    def fail_running_batch(self, batch_id: str, *, completed_at: datetime, error: str) -> bool:
        _require_aware(completed_at, "batch failure time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET completed_at = ?, status = 'failed', error = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (completed_at.isoformat(), error[:1000], batch_id),
            ).rowcount
            if changed:
                reservation_ids = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT reservation_id FROM deepseek_call_reservations WHERE batch_id = ? AND status = 'reserved'",
                        (batch_id,),
                    ).fetchall()
                )
                connection.execute(
                    """
                    UPDATE deepseek_candidate_results
                    SET outcome = 'rejected', completed_at = ?, error = 'batch_failed'
                    WHERE batch_id = ? AND outcome = 'pending'
                    """,
                    (completed_at.isoformat(), batch_id),
                )
                connection.execute(
                    """
                    UPDATE deepseek_call_reservations
                    SET status = 'abandoned', completed_at = ?, error = ?
                    WHERE batch_id = ? AND status = 'reserved'
                    """,
                    (completed_at.isoformat(), error[:1000], batch_id),
                )
                for reservation_id in reservation_ids:
                    _sync_call_audit(connection, reservation_id)
            connection.commit()
        return bool(changed)

    def reserve(
        self,
        strategy: Strategy,
        *,
        phase: str,
        requested_at: datetime,
        bucket: str | None = None,
        emergency: bool = False,
        emergency_reason: str = "",
        batch_id: str = "",
    ) -> BudgetReservation:
        _require_aware(requested_at, "budget requested_at")
        trade_date = requested_at.date().isoformat()
        requested_bucket = "emergency" if emergency else (bucket or strategy.value)
        if requested_bucket not in self._limits:
            return BudgetReservation(False, "", requested_bucket, "unknown_bucket")
        stage = _stage_key(strategy, phase, requested_bucket)
        if stage not in self._stage_limits:
            return BudgetReservation(False, "", requested_bucket, "unknown_stage", stage)
        if requested_bucket == "emergency" and emergency_reason not in _EMERGENCY_REASONS:
            return BudgetReservation(False, "", requested_bucket, "invalid_emergency_reason", stage)

        reservation_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            total = _count(connection, "trade_date = ?", (trade_date,))
            if total >= self._daily_hard_limit:
                connection.rollback()
                return BudgetReservation(False, "", requested_bucket, "daily_hard_limit", stage)
            bucket_used = _count(
                connection,
                "trade_date = ? AND bucket = ?",
                (trade_date, requested_bucket),
            )
            if bucket_used >= self._limits[requested_bucket]:
                connection.rollback()
                return BudgetReservation(False, "", requested_bucket, "bucket_limit", stage)
            if requested_bucket == "emergency":
                normal_used = _count(
                    connection,
                    "trade_date = ? AND bucket = ?",
                    (trade_date, strategy.value),
                )
                if normal_used < self._limits[strategy.value]:
                    connection.rollback()
                    return BudgetReservation(False, "", requested_bucket, "normal_budget_available", stage)
            else:
                stage_used = _count(
                    connection,
                    "trade_date = ? AND stage_key = ?",
                    (trade_date, stage),
                )
                if stage_used >= self._stage_limits[stage]:
                    connection.rollback()
                    return BudgetReservation(False, "", requested_bucket, "stage_limit", stage)
            connection.execute(
                """
                INSERT INTO deepseek_call_reservations(
                    reservation_id, trade_date, strategy, bucket, phase, stage_key,
                    batch_id, emergency_reason, requested_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')
                """,
                (
                    reservation_id,
                    trade_date,
                    strategy.value,
                    requested_bucket,
                    phase,
                    stage,
                    batch_id,
                    emergency_reason,
                    requested_at.isoformat(),
                ),
            )
            connection.commit()
        return BudgetReservation(True, reservation_id, requested_bucket, "reserved", stage)

    def finish(
        self,
        reservation_id: str,
        *,
        status: str,
        error: str = "",
        http_status: int | None = None,
        latency_ms: float | None = None,
        token_count: int = 0,
        timed_out: bool = False,
        completed_at: datetime | None = None,
    ) -> None:
        if status not in _CALL_TERMINALS:
            raise ValueError(f"invalid physical call terminal status: {status}")
        with self._connect() as connection:
            completed = completed_at or datetime.now().astimezone()
            _require_aware(completed, "physical call completion time")
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE deepseek_call_reservations
                SET status = ?, completed_at = ?, error = ?, http_status = ?, latency_ms = ?, token_count = ?, timed_out = ?
                WHERE reservation_id = ? AND status = 'reserved'
                """,
                (
                    status,
                    completed.isoformat(),
                    error[:1000],
                    http_status,
                    latency_ms,
                    max(0, token_count),
                    int(timed_out),
                    reservation_id,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise KeyError(f"unknown or completed reservation: {reservation_id}")
            _sync_call_audit(connection, reservation_id)
            connection.commit()

    def recover_incomplete(self, recovered_at: datetime) -> int:
        _require_aware(recovered_at, "recovery time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT reservation_id FROM deepseek_call_reservations WHERE status = 'reserved'"
                ).fetchall()
            )
            calls = connection.execute(
                """
                UPDATE deepseek_call_reservations
                SET status = 'abandoned', completed_at = ?, error = 'process_restart'
                WHERE status = 'reserved'
                """,
                (recovered_at.isoformat(),),
            ).rowcount
            for reservation_id in reservation_ids:
                _sync_call_audit(connection, reservation_id)
            batches = connection.execute(
                """
                UPDATE deepseek_review_batches
                SET status = 'abandoned', completed_at = ?, error = 'process_restart'
                WHERE status = 'running'
                """,
                (recovered_at.isoformat(),),
            ).rowcount
            connection.execute(
                """
                UPDATE deepseek_candidate_results
                SET outcome = 'rejected', completed_at = ?, error = 'batch_abandoned'
                WHERE outcome = 'pending'
                  AND batch_id IN (SELECT batch_id FROM deepseek_review_batches WHERE status = 'abandoned')
                """,
                (recovered_at.isoformat(),),
            )
            connection.commit()
        return int(calls) + int(batches)

    def abandon_reserved(self) -> int:
        with self._connect() as connection:
            completed_at = datetime.now().astimezone().isoformat()
            reservation_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT reservation_id FROM deepseek_call_reservations WHERE status = 'reserved'"
                ).fetchall()
            )
            changed = connection.execute(
                """
                UPDATE deepseek_call_reservations
                SET status = 'abandoned', completed_at = ?, error = 'process_restart'
                WHERE status = 'reserved'
                """,
                (completed_at,),
            ).rowcount
            for reservation_id in reservation_ids:
                _sync_call_audit(connection, reservation_id)
        return int(changed)

    def summary(self, day: str) -> dict[str, object]:
        if not self._initialized:
            return {
                "used": 0,
                "remaining": self._daily_hard_limit,
                "target": self._daily_target,
                "target_met": False,
                "by_bucket": {},
                "by_strategy": {},
                "by_stage": {},
                "by_status": {},
                "call_status": {name: 0 for name in ("reserved", *sorted(_CALL_TERMINALS))},
                "batch_status": {name: 0 for name in sorted(_BATCH_TERMINALS)},
                "candidate_outcomes": {name: 0 for name in sorted(_CANDIDATE_TERMINALS)},
                "http_429_count": 0,
                "timeout_count": 0,
                "token_count": 0,
            }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bucket, strategy, stage_key, status, COUNT(*)
                FROM deepseek_call_reservations
                WHERE trade_date = ?
                GROUP BY bucket, strategy, stage_key, status
                """,
                (day,),
            ).fetchall()
            batch_rows = connection.execute(
                """
                SELECT status, COUNT(*) FROM deepseek_review_batches
                WHERE trade_date = ? GROUP BY status
                """,
                (day,),
            ).fetchall()
            candidate_rows = connection.execute(
                """
                SELECT r.outcome, COUNT(*)
                FROM deepseek_candidate_results AS r
                JOIN deepseek_review_batches AS b ON b.batch_id = r.batch_id
                WHERE b.trade_date = ? GROUP BY r.outcome
                """,
                (day,),
            ).fetchall()
            acceptance_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN http_status = 429 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN timed_out = 1 THEN 1 ELSE 0 END),
                    SUM(token_count)
                FROM deepseek_call_reservations WHERE trade_date = ?
                """,
                (day,),
            ).fetchone()
        by_bucket: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        by_stage_count: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for bucket, strategy, stage, status, count in rows:
            amount = int(count)
            by_bucket[str(bucket)] = by_bucket.get(str(bucket), 0) + amount
            by_stage_count[str(stage)] = by_stage_count.get(str(stage), 0) + amount
            by_status[str(status)] = by_status.get(str(status), 0) + amount
            if str(bucket) in {item.value for item in Strategy} or str(bucket) == "emergency":
                by_strategy[str(strategy)] = by_strategy.get(str(strategy), 0) + amount
        used = sum(by_bucket.values())
        by_stage = {
            stage: {
                "used": by_stage_count.get(stage, 0),
                "target": self._stage_targets[stage],
                "limit": self._stage_limits[stage],
                "remaining": max(0, self._stage_limits[stage] - by_stage_count.get(stage, 0)),
                "target_met": by_stage_count.get(stage, 0) >= self._stage_targets[stage],
            }
            for stage in self._stage_limits
        }
        target_met = all(
            by_stage_count.get(stage, 0) >= target
            for stage, target in self._stage_targets.items()
            if stage != "emergency"
        )
        return {
            "used": used,
            "remaining": max(0, self._daily_hard_limit - used),
            "target": self._daily_target,
            "target_met": target_met,
            "by_bucket": by_bucket,
            "by_strategy": by_strategy,
            "by_stage": by_stage,
            "by_status": by_status,
            "call_status": {name: by_status.get(name, 0) for name in ("reserved", *sorted(_CALL_TERMINALS))},
            "batch_status": {
                name: dict((str(status), int(count)) for status, count in batch_rows).get(name, 0)
                for name in sorted(_BATCH_TERMINALS)
            },
            "candidate_outcomes": {
                name: dict((str(outcome), int(count)) for outcome, count in candidate_rows).get(name, 0)
                for name in sorted(_CANDIDATE_TERMINALS)
            },
            "http_429_count": int(acceptance_row[0] or 0),
            "timeout_count": int(acceptance_row[1] or 0),
            "token_count": int(acceptance_row[2] or 0),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


def _stage_key(strategy: Strategy, phase: str, bucket: str) -> str:
    if bucket == "shared_preheat":
        return "shared_preheat"
    if bucket == "emergency":
        return "emergency"
    if strategy is Strategy.TODAY and phase in {"today_observe", "today_main", "today_late"}:
        return phase
    if strategy is Strategy.TOMORROW and phase in {"afternoon", "final_review"}:
        return "tomorrow_final" if phase == "final_review" else "tomorrow_afternoon"
    if strategy is Strategy.D25 and phase in {"afternoon", "final_review"}:
        return "d25_final" if phase == "final_review" else "d25_afternoon"
    if strategy is Strategy.LONG and phase in {"afternoon", "final_review"}:
        return "long_afternoon"
    return f"{strategy.value}_{phase}"


def _count(connection: sqlite3.Connection, where: str, parameters: tuple[object, ...]) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM deepseek_call_reservations WHERE {where}",
            parameters,
        ).fetchone()[0]
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(str(row[0]))
    except (TypeError, ValueError):
        return 0


def _sync_call_audit(connection: sqlite3.Connection, reservation_id: str) -> None:
    connection.execute(
        """
        INSERT INTO deepseek_calls(
            call_id, strategy, phase, model, batch_id, requested_at, completed_at,
            http_status, completion_tokens, latency_ms, outcome, error_code
        )
        SELECT
            r.reservation_id, r.strategy, r.phase, COALESCE(b.model, ''), r.batch_id,
            r.requested_at, r.completed_at, r.http_status, r.token_count, r.latency_ms,
            r.status,
            CASE
                WHEN r.timed_out = 1 THEN 'timeout'
                WHEN r.status = 'abandoned' THEN 'abandoned'
                WHEN r.http_status IS NOT NULL AND r.status = 'failed' THEN 'http_' || r.http_status
                WHEN r.status = 'failed' THEN 'request_failed'
                ELSE ''
            END
        FROM deepseek_call_reservations AS r
        LEFT JOIN deepseek_review_batches AS b ON b.batch_id = r.batch_id
        WHERE r.reservation_id = ?
        ON CONFLICT(call_id) DO UPDATE SET
            completed_at = excluded.completed_at,
            http_status = excluded.http_status,
            completion_tokens = excluded.completion_tokens,
            latency_ms = excluded.latency_ms,
            outcome = excluded.outcome,
            error_code = excluded.error_code
        """,
        (reservation_id,),
    )
    connection.execute(
        """
        DELETE FROM deepseek_calls
        WHERE call_id IN (
            SELECT call_id FROM deepseek_calls
            ORDER BY requested_at DESC
            LIMIT -1 OFFSET 10000
        )
        """
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = ["SCHEMA_VERSION", "BudgetReservation", "DeepSeekBudgetStore"]

"""Atomic DeepSeek request budgets and persisted review terminal states."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.domain.recommendation.models import Strategy
from trader.infra.deepseek.budget_batches import BudgetBatchMixin
from trader.infra.deepseek.budget_summary import BudgetSummaryMixin
from trader.infra.deepseek.budget_support import (
    _count,
    _current_schema_version,
    _ensure_column,
    _require_aware,
    _stage_key,
    _sync_call_audit,
)

_BATCH_TERMINALS = frozenset({"success", "partial", "failed", "skipped", "abandoned"})
_CALL_TERMINALS = frozenset({"success", "failed", "abandoned"})
_CANDIDATE_TERMINALS = frozenset({"applied", "abstain", "rejected", "late"})
_EMERGENCY_REASONS = frozenset({"new_high_risk", "freeze_boundary_change"})

SCHEMA_VERSION = 3


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reservation_id: str
    bucket: str
    reason: str
    stage: str = ""


class DeepSeekBudgetStore(BudgetBatchMixin, BudgetSummaryMixin):
    def __init__(
        self,
        database_path: Path,
        *,
        daily_hard_limit: int,
        strategy_limits: Mapping[str, int],
        stage_targets: Mapping[str, int],
        stage_limits: Mapping[str, int],
        challenger_limits: Mapping[str, int] | None = None,
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
        self._challenger_limits = dict(challenger_limits or {strategy.value: 0 for strategy in Strategy})
        if set(self._challenger_limits) != {strategy.value for strategy in Strategy}:
            raise ValueError("challenger limits must define all strategies")
        if any(value < 0 for value in self._challenger_limits.values()):
            raise ValueError("challenger limits cannot be negative")
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
                    timed_out INTEGER NOT NULL DEFAULT 0,
                    model_role TEXT NOT NULL DEFAULT 'primary',
                    requested_model TEXT,
                    actual_model TEXT,
                    reasoning_effort TEXT,
                    system_fingerprint TEXT,
                    finish_reason TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0
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
                    error_code TEXT NOT NULL DEFAULT '',
                    model_role TEXT NOT NULL DEFAULT 'primary',
                    requested_model TEXT,
                    actual_model TEXT,
                    reasoning_effort TEXT,
                    system_fingerprint TEXT,
                    finish_reason TEXT,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0
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
            for column, declaration in (
                ("model_role", "TEXT NOT NULL DEFAULT 'primary'"),
                ("requested_model", "TEXT"),
                ("actual_model", "TEXT"),
                ("reasoning_effort", "TEXT"),
                ("system_fingerprint", "TEXT"),
                ("finish_reason", "TEXT"),
                ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("prompt_cache_hit_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("prompt_cache_miss_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ):
                _ensure_column(connection, "deepseek_call_reservations", column, declaration)
                _ensure_column(connection, "deepseek_calls", column, declaration)
            _ensure_column(connection, "deepseek_calls", "total_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_schema_version(connection)
        self._initialized = True

    def _ensure_schema_version(self, connection: sqlite3.Connection) -> None:
        if _current_schema_version(connection) < SCHEMA_VERSION:
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

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
        model_role: str = "primary",
        requested_model: str = "",
        reasoning_effort: str = "",
    ) -> BudgetReservation:
        _require_aware(requested_at, "budget requested_at")
        trade_date = requested_at.date().isoformat()
        requested_bucket = "emergency" if emergency else (bucket or strategy.value)
        if requested_bucket not in self._limits:
            return BudgetReservation(False, "", requested_bucket, "unknown_bucket")
        if model_role not in {"primary", "challenger"}:
            return BudgetReservation(False, "", requested_bucket, "invalid_model_role")
        if model_role == "challenger" and strategy is Strategy.LONG:
            return BudgetReservation(False, "", requested_bucket, "challenger_not_allowed")
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
            if model_role == "challenger":
                challenger_used = _count(
                    connection,
                    "trade_date = ? AND strategy = ? AND model_role = 'challenger'",
                    (trade_date, strategy.value),
                )
                if challenger_used >= self._challenger_limits[strategy.value]:
                    connection.rollback()
                    return BudgetReservation(False, "", requested_bucket, "challenger_limit", stage)
            connection.execute(
                """
                INSERT INTO deepseek_call_reservations(
                    reservation_id, trade_date, strategy, bucket, phase, stage_key,
                    batch_id, emergency_reason, requested_at, status,
                    model_role, requested_model, reasoning_effort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
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
                    model_role,
                    requested_model or None,
                    reasoning_effort or None,
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
        actual_model: str | None = None,
        system_fingerprint: str | None = None,
        finish_reason: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        prompt_cache_hit_tokens: int = 0,
        prompt_cache_miss_tokens: int = 0,
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
                SET status = ?, completed_at = ?, error = ?, http_status = ?, latency_ms = ?, token_count = ?,
                    timed_out = ?, actual_model = ?, system_fingerprint = ?, finish_reason = ?, prompt_tokens = ?,
                    completion_tokens = ?, prompt_cache_hit_tokens = ?, prompt_cache_miss_tokens = ?
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
                    actual_model,
                    system_fingerprint,
                    finish_reason,
                    max(0, prompt_tokens),
                    max(0, completion_tokens),
                    max(0, prompt_cache_hit_tokens),
                    max(0, prompt_cache_miss_tokens),
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=10.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()


__all__ = ["SCHEMA_VERSION", "BudgetReservation", "DeepSeekBudgetStore"]

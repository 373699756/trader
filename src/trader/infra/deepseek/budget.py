"""Atomic DeepSeek request budgets and persisted review terminal states."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.domain.recommendation.models import Strategy
from trader.infra.deepseek.budget_batch_store import (
    BudgetBatchCompletion,
    BudgetBatchRequest,
    BudgetBatchStore,
)
from trader.infra.deepseek.budget_reporting import BudgetReportingConfig, BudgetSummaryReader
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
_SOFT_BUCKET_LIMITS = {
    "today": 22,
    "tomorrow": 14,
    "d25": 12,
    "long": 0,
    "shared_preheat": 10,
    "emergency": 5,
}
_CHALLENGER_SOFT_LIMIT = 8
_BEGIN_IMMEDIATE_TIMEOUT_SECONDS = 15.0
_BEGIN_IMMEDIATE_RETRY_SECONDS = 0.02

SCHEMA_VERSION = 3


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reservation_id: str
    bucket: str
    reason: str
    stage: str = ""


class _BudgetStoreRequiredOptions(TypedDict):
    daily_hard_limit: int
    strategy_limits: Mapping[str, int]
    stage_targets: Mapping[str, int]
    stage_limits: Mapping[str, int]


class _BudgetStoreOptionalOptions(TypedDict, total=False):
    challenger_limits: Mapping[str, int] | None
    write_lock: AbstractContextManager[object] | None


class BudgetStoreOptions(_BudgetStoreRequiredOptions, _BudgetStoreOptionalOptions):
    pass


class _ReserveRequiredOptions(TypedDict):
    phase: str
    requested_at: datetime


class _ReserveOptionalOptions(TypedDict, total=False):
    bucket: str | None
    emergency: bool
    emergency_reason: str
    batch_id: str
    model_role: str
    requested_model: str
    reasoning_effort: str


class ReserveOptions(_ReserveRequiredOptions, _ReserveOptionalOptions):
    pass


class _FinishRequiredOptions(TypedDict):
    status: str


class _FinishOptionalOptions(TypedDict, total=False):
    error: str
    http_status: int | None
    latency_ms: float | None
    token_count: int
    timed_out: bool
    completed_at: datetime | None
    actual_model: str | None
    system_fingerprint: str | None
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int


class FinishOptions(_FinishRequiredOptions, _FinishOptionalOptions):
    pass


@dataclass(frozen=True)
class _ReservationContext:
    strategy: Strategy
    phase: str
    requested_at: datetime
    trade_date: str
    bucket: str
    emergency_reason: str
    batch_id: str
    model_role: str
    requested_model: str
    reasoning_effort: str
    stage: str


class DeepSeekBudgetStore:
    def __init__(
        self,
        database_path: Path,
        **options: Unpack[BudgetStoreOptions],
    ) -> None:
        daily_hard_limit = options["daily_hard_limit"]
        strategy_limits = options["strategy_limits"]
        stage_targets = options["stage_targets"]
        stage_limits = options["stage_limits"]
        challenger_limits = options.get("challenger_limits")
        write_lock = options.get("write_lock")
        if not 0 <= daily_hard_limit <= 168:
            raise ValueError("daily hard limit must be between 0 and 168")
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
        self._write_lock = write_lock or threading.Lock()
        self._initialized = False
        self._batches = BudgetBatchStore(self._connect)
        self._reporting = BudgetSummaryReader(
            self._connect,
            lambda: self._initialized,
            BudgetReportingConfig(
                daily_hard_limit=self._daily_hard_limit,
                daily_target=self._daily_target,
                stage_targets=self._stage_targets,
                stage_limits=self._stage_limits,
            ),
        )

    def begin_batch(self, request: BudgetBatchRequest) -> str:
        with self._write_lock:
            return self._batches.begin_batch(request)

    def finish_batch(self, completion: BudgetBatchCompletion) -> None:
        with self._write_lock:
            self._batches.finish_batch(completion)

    def set_batch_cache_hits(self, batch_id: str, count: int) -> None:
        with self._write_lock:
            self._batches.set_batch_cache_hits(batch_id, count)

    def fail_running_batch(self, batch_id: str, *, completed_at: datetime, error: str) -> bool:
        with self._write_lock:
            return self._batches.fail_running_batch(batch_id, completed_at=completed_at, error=error)

    def summary(self, day: str) -> dict[str, object]:
        return self._reporting.summary(day)

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
        **options: Unpack[ReserveOptions],
    ) -> BudgetReservation:
        context, rejected = self._reservation_context(strategy, options)
        if rejected is not None:
            return rejected
        assert context is not None
        reservation_id = uuid.uuid4().hex
        with self._write_lock:
            with self._connect() as connection:
                _begin_immediate(connection)
                rejection = self._limit_rejection(connection, context)
                if rejection:
                    connection.rollback()
                    return BudgetReservation(False, "", context.bucket, rejection, context.stage)
                self._insert_reservation(connection, reservation_id, context)
                connection.commit()
        return BudgetReservation(True, reservation_id, context.bucket, "reserved", context.stage)

    def _reservation_context(
        self,
        strategy: Strategy,
        options: ReserveOptions,
    ) -> tuple[_ReservationContext | None, BudgetReservation | None]:
        requested_at = options["requested_at"]
        _require_aware(requested_at, "budget requested_at")
        bucket = "emergency" if options.get("emergency", False) else (options.get("bucket") or strategy.value)
        model_role = options.get("model_role", "primary")
        emergency_reason = options.get("emergency_reason", "")
        stage = _stage_key(strategy, options["phase"], bucket)
        checks = (
            (strategy is Strategy.LONG or bucket == "long", "long_not_allowed"),
            (bucket not in self._limits, "unknown_bucket"),
            (model_role not in {"primary", "challenger"}, "invalid_model_role"),
            (model_role == "challenger" and strategy is Strategy.LONG, "challenger_not_allowed"),
            (stage not in self._stage_limits, "unknown_stage"),
            (bucket == "emergency" and emergency_reason not in _EMERGENCY_REASONS, "invalid_emergency_reason"),
        )
        reason = next((value for invalid, value in checks if invalid), "")
        if reason:
            rejection_stage = stage if reason in {"unknown_stage", "invalid_emergency_reason"} else ""
            return None, BudgetReservation(False, "", bucket, reason, rejection_stage)
        return (
            _ReservationContext(
                strategy,
                options["phase"],
                requested_at,
                requested_at.date().isoformat(),
                bucket,
                emergency_reason,
                options.get("batch_id", ""),
                model_role,
                options.get("requested_model", ""),
                options.get("reasoning_effort", ""),
                stage,
            ),
            None,
        )

    def _limit_rejection(self, connection: sqlite3.Connection, context: _ReservationContext) -> str:
        rejection = self._primary_limit_rejection(connection, context)
        if rejection or context.model_role != "challenger":
            return rejection
        return self._challenger_limit_rejection(connection, context)

    def _primary_limit_rejection(self, connection: sqlite3.Connection, context: _ReservationContext) -> str:
        trade_date = context.trade_date
        bucket = context.bucket
        if _count(connection, "trade_date = ?", (trade_date,)) >= self._daily_hard_limit:
            return "daily_hard_limit"
        if _count(connection, "trade_date = ? AND bucket = ?", (trade_date, bucket)) >= self._limits[bucket]:
            return "bucket_limit"
        soft_limit = _SOFT_BUCKET_LIMITS.get(bucket)
        if context.model_role == "primary" and soft_limit is not None:
            used = _count(
                connection,
                "trade_date = ? AND bucket = ? AND model_role = 'primary'",
                (trade_date, bucket),
            )
            if used >= soft_limit:
                return "soft_bucket_limit"
        return self._stage_limit_rejection(connection, context)

    def _stage_limit_rejection(self, connection: sqlite3.Connection, context: _ReservationContext) -> str:
        if context.bucket == "emergency":
            normal_used = _count(
                connection,
                "trade_date = ? AND bucket = ? AND model_role = 'primary'",
                (context.trade_date, context.strategy.value),
            )
            normal_limit = min(self._limits[context.strategy.value], _SOFT_BUCKET_LIMITS[context.strategy.value])
            return "normal_budget_available" if normal_used < normal_limit else ""
        stage_used = _count(
            connection,
            "trade_date = ? AND stage_key = ?",
            (context.trade_date, context.stage),
        )
        return "stage_limit" if stage_used >= self._stage_limits[context.stage] else ""

    def _challenger_limit_rejection(self, connection: sqlite3.Connection, context: _ReservationContext) -> str:
        challenger_total = _count(
            connection,
            "trade_date = ? AND model_role = 'challenger'",
            (context.trade_date,),
        )
        if challenger_total >= _CHALLENGER_SOFT_LIMIT:
            return "challenger_soft_limit"
        challenger_used = _count(
            connection,
            "trade_date = ? AND strategy = ? AND model_role = 'challenger'",
            (context.trade_date, context.strategy.value),
        )
        return "challenger_limit" if challenger_used >= self._challenger_limits[context.strategy.value] else ""

    @staticmethod
    def _insert_reservation(
        connection: sqlite3.Connection,
        reservation_id: str,
        context: _ReservationContext,
    ) -> None:
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
                context.trade_date,
                context.strategy.value,
                context.bucket,
                context.phase,
                context.stage,
                context.batch_id,
                context.emergency_reason,
                context.requested_at.isoformat(),
                context.model_role,
                context.requested_model or None,
                context.reasoning_effort or None,
            ),
        )

    def finish(
        self,
        reservation_id: str,
        **options: Unpack[FinishOptions],
    ) -> None:
        status = options["status"]
        if status not in _CALL_TERMINALS:
            raise ValueError(f"invalid physical call terminal status: {status}")
        with self._write_lock:
            with self._connect() as connection:
                completed = options.get("completed_at") or datetime.now().astimezone()
                _require_aware(completed, "physical call completion time")
                _begin_immediate(connection)
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
                        options.get("error", "")[:1000],
                        options.get("http_status"),
                        options.get("latency_ms"),
                        max(0, options.get("token_count", 0)),
                        int(options.get("timed_out", False)),
                        options.get("actual_model"),
                        options.get("system_fingerprint"),
                        options.get("finish_reason"),
                        max(0, options.get("prompt_tokens", 0)),
                        max(0, options.get("completion_tokens", 0)),
                        max(0, options.get("prompt_cache_hit_tokens", 0)),
                        max(0, options.get("prompt_cache_miss_tokens", 0)),
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
        with self._write_lock:
            with self._connect() as connection:
                _begin_immediate(connection)
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
        with self._write_lock:
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
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()


def _begin_immediate(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _BEGIN_IMMEDIATE_TIMEOUT_SECONDS
    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(_BEGIN_IMMEDIATE_RETRY_SECONDS)


__all__ = ["SCHEMA_VERSION", "BudgetReservation", "DeepSeekBudgetStore"]

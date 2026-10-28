"""Recommendation-owned outcome settlement and immutable market benchmarks."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from trader.infra.sqlite_codec import DecodeDate, EncodeDate, EncodeTime
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import OutcomeTierSummary
from trader.recommendation.application.ports.freeze_records import FreezeRecordPort
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.domain.outcomes.models import (
    BenchmarkReturn,
    OutcomeHorizons,
    OutcomeTarget,
    RecommendationOutcome,
)


class OutcomeEvidenceConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutcomeEvidenceStatus:
    initialized: bool
    benchmark_returns: int
    recommendation_outcomes: int
    complete_outcomes: int
    latest_benchmark_date: str | None
    error_code: str


class SQLiteOutcomeEvidenceIndex:
    def __init__(
        self,
        runtime_root: Path,
        freezes: FreezeRecordPort,
    ) -> None:
        self._database = runtime_root / "outcomes.sqlite3"
        self._freezes = freezes
        self._lock = threading.RLock()
        self._initialized = False

    @classmethod
    def inspect_status(cls, runtime_root: Path) -> OutcomeEvidenceStatus:
        database = runtime_root / "outcomes.sqlite3"
        if not database.is_file():
            return OutcomeEvidenceStatus(False, 0, 0, 0, None, "")
        try:
            with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
                EnsureSqliteSchema(connection, _SCHEMA)
                benchmark = connection.run("SELECT COUNT(*), MAX(trade_date) FROM benchmark_returns").require_one()
                outcomes = connection.run(
                    "SELECT COUNT(*), COALESCE(SUM(status = 'complete'), 0) FROM recommendation_outcomes"
                ).require_one()
        except sqlite3.Error as exc:
            return OutcomeEvidenceStatus(True, 0, 0, 0, None, type(exc).__name__)
        return OutcomeEvidenceStatus(
            True,
            int(benchmark[0]),
            int(outcomes[0]),
            int(outcomes[1]),
            DecodeDate(benchmark[1]).isoformat() if benchmark[1] is not None else None,
            "",
        )

    def summaries(self, strategy: Strategy, *, limit: int = 31) -> tuple[OutcomeTierSummary, ...]:
        """Read a bounded, tier-separated forward outcome summary without writing state."""

        if limit < 1:
            raise ValueError("summary limit must be positive")
        identities, sample_counts = _summary_samples(self._freezes, strategy, limit)
        completed = _summary_returns(self._database, strategy, identities)
        return tuple(
            OutcomeTierSummary(
                tier,
                horizon,
                sample_counts.get((tier, horizon), 0),
                len(values := completed.get((tier, horizon), [])),
                sum(value > 0 for value in values),
                sum(values) / len(values) if values else None,
            )
            for tier in ("recommend", "watch")
            for horizon in OutcomeHorizons(strategy)
        )

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._database.parent.mkdir(parents=True, exist_ok=True)
            with SqliteSession(
                self._database, SqliteConnectionOptions(journal_mode="WAL", timeout_seconds=10)
            ) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
            self._initialized = True

    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]:
        if limit < 1:
            raise ValueError("outcome target limit must be positive")
        self.initialize()
        targets: list[OutcomeTarget] = []
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            for trade_date in reversed(self._freezes.list_dates(strategy, limit=31)):
                record = self._freezes.load(strategy, trade_date)
                if record is None:
                    continue
                for item in sorted(record.freeze.items, key=lambda value: value.code):
                    if not item.selected or item.quote is None:
                        continue
                    pending_horizons = self._pending_horizons(record.version, strategy, item.code)
                    if not pending_horizons:
                        continue
                    targets.append(
                        OutcomeTarget(
                            record.version,
                            strategy,
                            trade_date.isoformat(),
                            item.code,
                            item.quote.price,
                            item.downside.atr20_pct
                            if item.downside is not None and item.downside.atr20_pct is not None
                            else None,
                            pending_horizons,
                            entry_at=item.quote.source_time,
                        )
                    )
                    if len(targets) >= limit:
                        return tuple(targets)
        return tuple(targets)

    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None:
        payload = _benchmark_bytes(benchmark)
        self.initialize()
        with (
            self._lock,
            SqliteSession(self._database, _CONNECTION_OPTIONS) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            existing = connection.run(
                "SELECT record_hash, record_bytes FROM benchmark_returns WHERE trade_date = ?",
                (EncodeDate(date.fromisoformat(benchmark.trade_date)),),
            ).fetchone()
            _raise_on_conflict(existing, payload, "benchmark return")
            if existing is None:
                connection.run(
                    "INSERT INTO benchmark_returns(trade_date, return_pct, observed_at, record_hash, record_bytes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        EncodeDate(date.fromisoformat(benchmark.trade_date)),
                        benchmark.return_pct,
                        EncodeTime(observed_at),
                        _sha256(payload),
                        payload,
                    ),
                )

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]:
        if limit < 1:
            raise ValueError("benchmark return limit must be positive")
        if not self._database.is_file():
            return ()
        with self._lock, SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection:
            EnsureSqliteSchema(connection, _SCHEMA)
            rows = connection.run(
                """
                SELECT trade_date, return_pct, record_hash, record_bytes
                FROM benchmark_returns
                WHERE trade_date > ?
                ORDER BY trade_date
                LIMIT ?
                """,
                (EncodeDate(date.fromisoformat(recommend_date)), limit),
            ).fetchall()
        benchmarks = tuple(
            BenchmarkReturn(DecodeDate(row["trade_date"]).isoformat(), float(row["return_pct"])) for row in rows
        )
        for row, benchmark in zip(rows, benchmarks, strict=True):
            _raise_on_conflict(row, _benchmark_bytes(benchmark), "benchmark return")
        return benchmarks

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None:
        if any(outcome.is_retryable for outcome in outcomes):
            raise ValueError("retryable recommendation outcomes cannot be persisted")
        self.initialize()
        with (
            self._lock,
            SqliteSession(self._database, _CONNECTION_OPTIONS) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            for outcome in outcomes:
                payload = _outcome_bytes(outcome)
                key = (outcome.snapshot_id, outcome.strategy.value, outcome.stock_code, outcome.horizon)
                existing = connection.run(
                    "SELECT record_hash, record_bytes FROM recommendation_outcomes "
                    "WHERE snapshot_id = ? AND strategy = ? AND code = ? AND horizon = ?",
                    key,
                ).fetchone()
                _raise_on_conflict(existing, payload, "recommendation outcome")
                if existing is None:
                    connection.run(
                        """
                        INSERT INTO recommendation_outcomes(
                            snapshot_id, strategy, freeze_date, code, horizon,
                            status, settled_at, record_hash, record_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            *key[:2],
                            EncodeDate(date.fromisoformat(outcome.recommend_date)),
                            key[2],
                            key[3],
                            outcome.status,
                            EncodeTime(outcome.settled_at),
                            _sha256(payload),
                            payload,
                        ),
                    )

    def _pending_horizons(self, snapshot_id: str, strategy: Strategy, code: str) -> tuple[int, ...]:
        horizons = OutcomeHorizons(strategy)
        with self._lock, SqliteSession(self._database, _CONNECTION_OPTIONS) as connection:
            rows = connection.run(
                "SELECT horizon FROM recommendation_outcomes WHERE snapshot_id = ? AND strategy = ? AND code = ?",
                (snapshot_id, strategy.value, code),
            ).fetchall()
        settled = {int(row["horizon"]) for row in rows}
        return tuple(horizon for horizon in horizons if horizon not in settled)


_CONNECTION_OPTIONS = SqliteConnectionOptions(timeout_seconds=10.0)
_SummaryTier = Literal["recommend", "watch"]


def _summary_samples(
    freezes: FreezeRecordPort, strategy: Strategy, limit: int
) -> tuple[dict[tuple[str, str], _SummaryTier], dict[tuple[_SummaryTier, int], int]]:
    identities: dict[tuple[str, str], _SummaryTier] = {}
    sample_counts: dict[tuple[_SummaryTier, int], int] = {}
    for trade_date in freezes.list_dates(strategy, limit=limit):
        record = freezes.load(strategy, trade_date)
        if record is None:
            continue
        for item in record.freeze.items:
            if not item.selected or item.quote is None:
                continue
            tier: _SummaryTier = "recommend" if item.action is RecommendationAction.EXECUTABLE else "watch"
            identities[(record.version, item.code)] = tier
            for horizon in OutcomeHorizons(strategy):
                key = (tier, horizon)
                sample_counts[key] = sample_counts.get(key, 0) + 1
    return identities, sample_counts


def _summary_returns(
    database: Path, strategy: Strategy, identities: dict[tuple[str, str], _SummaryTier]
) -> dict[tuple[_SummaryTier, int], list[float]]:
    completed: dict[tuple[_SummaryTier, int], list[float]] = {}
    if not database.is_file() or not identities:
        return completed
    for snapshot_id, code, horizon, record_hash, record_bytes in _summary_rows(database, strategy, identities):
        tier = identities.get((snapshot_id, code))
        if tier is None:
            continue
        value = _summary_net_return(record_bytes, record_hash)
        if value is not None:
            completed.setdefault((tier, int(horizon)), []).append(value)
    return completed


def _summary_rows(
    database: Path, strategy: Strategy, identities: dict[tuple[str, str], _SummaryTier]
) -> list[tuple[str, str, int, bytes, bytes]]:
    snapshot_ids = tuple(sorted({snapshot_id for snapshot_id, _code in identities}))
    placeholders = ",".join("?" for _snapshot_id in snapshot_ids)
    try:
        with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
            rows = connection.run(
                "SELECT snapshot_id, code, horizon, record_hash, record_bytes "
                f"FROM recommendation_outcomes WHERE strategy = ? AND snapshot_id IN ({placeholders})",
                (strategy.value, *snapshot_ids),
            ).fetchall()
            return [(str(row[0]), str(row[1]), int(row[2]), bytes(row[3]), bytes(row[4])) for row in rows]
    except sqlite3.Error as exc:
        raise OutcomeEvidenceConflictError("outcome summary read failed") from exc


def _summary_net_return(payload: bytes, record_hash: bytes) -> float | None:
    if _sha256(payload) != record_hash:
        raise OutcomeEvidenceConflictError("outcome summary payload hash mismatch")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutcomeEvidenceConflictError("outcome summary payload is invalid") from exc
    net = value.get("net_excess_return_pct")
    if value.get("status") != "complete" or not isinstance(net, (int, float)):
        return None
    if not math.isfinite(net):
        raise OutcomeEvidenceConflictError("outcome summary net excess is invalid")
    return float(net)


def _outcome_bytes(outcome: RecommendationOutcome) -> bytes:
    return _canonical_bytes(
        {
            "schema_version": outcome.schema_identity,
            "snapshot_id": outcome.snapshot_id,
            "strategy": outcome.strategy.value,
            "recommend_date": outcome.recommend_date,
            "stock_code": outcome.stock_code,
            "horizon": outcome.horizon,
            "status": outcome.status,
            "anchor_raw_price": outcome.anchor_raw_price,
            "anchor_qfq_price": outcome.anchor_qfq_price,
            "atr20_pct": outcome.atr20_pct,
            "minimum_qfq_low": outcome.minimum_qfq_low,
            "end_qfq_close": outcome.end_qfq_close,
            "exit_status": outcome.exit_status.value if outcome.exit_status is not None else None,
            "exit_untradable": outcome.exit_untradable,
            "untradable_dates": list(outcome.untradable_dates),
            "gross_return_pct": outcome.gross_return_pct,
            "benchmark_return_pct": outcome.benchmark_return_pct,
            "net_excess_return_pct": outcome.net_excess_return_pct,
            "mae_pct": outcome.mae_pct,
            "mae_atr": outcome.mae_atr,
            "severe_drawdown": outcome.severe_drawdown,
            "quality_reason": outcome.quality_reason,
        }
    )


def _benchmark_bytes(benchmark: BenchmarkReturn) -> bytes:
    return _canonical_bytes(
        {
            "schema_version": "benchmark_return",
            "trade_date": benchmark.trade_date,
            "return_pct": benchmark.return_pct,
        }
    )


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def _raise_on_conflict(existing: sqlite3.Row | None, payload: bytes, label: str) -> None:
    if existing is None:
        return
    if bytes(existing["record_hash"]) != _sha256(payload) or bytes(existing["record_bytes"]) != payload:
        raise OutcomeEvidenceConflictError(f"immutable {label} conflict")


_SCHEMA = SqliteSchema(
    1,
    (
        """
        CREATE TABLE benchmark_returns(
            trade_date INTEGER NOT NULL PRIMARY KEY,
            return_pct REAL NOT NULL,
            observed_at INTEGER NOT NULL,
            record_hash BLOB NOT NULL CHECK(length(record_hash)=32),
            record_bytes BLOB NOT NULL
        )
        """,
        """
        CREATE TABLE recommendation_outcomes(
            snapshot_id TEXT NOT NULL,
            strategy TEXT NOT NULL,
            freeze_date INTEGER NOT NULL,
            code TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            status TEXT NOT NULL,
            settled_at INTEGER NOT NULL,
            record_hash BLOB NOT NULL CHECK(length(record_hash)=32),
            record_bytes BLOB NOT NULL,
            PRIMARY KEY(snapshot_id, strategy, code, horizon)
        )
        """,
        """
        CREATE INDEX recommendation_outcomes_date_idx
        ON recommendation_outcomes(freeze_date, strategy)
        """,
    ),
)


__all__ = [
    "OutcomeEvidenceConflictError",
    "OutcomeEvidenceStatus",
    "SQLiteOutcomeEvidenceIndex",
]

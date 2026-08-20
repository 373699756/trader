"""Immutable SQLite evidence for formal-decision outcomes and market benchmarks."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from trader.application.ports.data_plane import HistoricalFeatureRecord
from trader.application.ports.decision_records import DecisionRecordRepositoryPort
from trader.domain.outcome.models import BenchmarkReturn, OutcomeTarget, RecommendationOutcome
from trader.domain.recommendation.models import Strategy


class HistoricalFeatureReader(Protocol):
    def load_historical_feature_recent(self, code: str, trade_date: str) -> HistoricalFeatureRecord | None: ...


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


class SQLiteOutcomeEvidenceRepository:
    def __init__(
        self,
        runtime_root: Path,
        decisions: DecisionRecordRepositoryPort,
        historical: HistoricalFeatureReader,
    ) -> None:
        self._database = runtime_root / "research" / "outcomes.sqlite3"
        self._decisions = decisions
        self._historical = historical
        self._lock = threading.RLock()
        self._initialized = False

    @classmethod
    def inspect_status(cls, runtime_root: Path) -> OutcomeEvidenceStatus:
        database = runtime_root / "research" / "outcomes.sqlite3"
        if not database.is_file():
            return OutcomeEvidenceStatus(False, 0, 0, 0, None, "")
        try:
            target = f"file:{database.resolve()}?mode=ro"
            with sqlite3.connect(target, timeout=5.0, uri=True) as connection:
                benchmark = connection.execute("SELECT COUNT(*), MAX(trade_date) FROM benchmark_returns").fetchone()
                outcomes = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(status = 'complete'), 0) FROM recommendation_outcomes"
                ).fetchone()
        except sqlite3.Error as exc:
            return OutcomeEvidenceStatus(True, 0, 0, 0, None, type(exc).__name__)
        return OutcomeEvidenceStatus(
            True,
            int(benchmark[0]),
            int(outcomes[0]),
            int(outcomes[1]),
            str(benchmark[1]) if benchmark[1] is not None else None,
            "",
        )

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._database.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript(_SCHEMA)
            self._initialized = True

    def pending_outcome_targets(self, *, limit: int) -> Sequence[OutcomeTarget]:
        if limit < 1:
            raise ValueError("outcome target limit must be positive")
        self.initialize()
        targets: list[OutcomeTarget] = []
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            for trade_date in reversed(self._decisions.list_dates(strategy, limit=31)):
                record = self._decisions.load(strategy, trade_date)
                if record is None:
                    continue
                for item in sorted(record.decision.items, key=lambda value: value.code):
                    if (
                        not item.selected
                        or item.quote is None
                        or self._is_fully_settled(record.version, strategy, item.code)
                    ):
                        continue
                    targets.append(
                        OutcomeTarget(
                            record.version,
                            strategy,
                            trade_date.isoformat(),
                            item.code,
                            item.quote.price,
                            self._atr20_pct(item.code, trade_date.isoformat()),
                        )
                    )
                    if len(targets) >= limit:
                        return tuple(targets)
        return tuple(targets)

    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None:
        payload = _benchmark_bytes(benchmark)
        self.initialize()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT payload_hash, payload FROM benchmark_returns WHERE trade_date = ?",
                (benchmark.trade_date,),
            ).fetchone()
            _raise_on_conflict(existing, payload, "benchmark return")
            if existing is None:
                connection.execute(
                    "INSERT INTO benchmark_returns(trade_date, return_pct, observed_at, payload_hash, payload) VALUES (?, ?, ?, ?, ?)",
                    (benchmark.trade_date, benchmark.return_pct, observed_at.isoformat(), _sha256(payload), payload),
                )

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]:
        if limit < 1:
            raise ValueError("benchmark return limit must be positive")
        self.initialize()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, return_pct, payload_hash, payload
                FROM benchmark_returns
                WHERE trade_date > ?
                ORDER BY trade_date
                LIMIT ?
                """,
                (recommend_date, limit),
            ).fetchall()
        benchmarks = tuple(BenchmarkReturn(str(row["trade_date"]), float(row["return_pct"])) for row in rows)
        for row, benchmark in zip(rows, benchmarks, strict=True):
            _raise_on_conflict(row, _benchmark_bytes(benchmark), "benchmark return")
        return benchmarks

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None:
        self.initialize()
        with self._lock, self._connection() as connection:
            for outcome in outcomes:
                payload = _outcome_bytes(outcome)
                key = (outcome.snapshot_id, outcome.strategy.value, outcome.stock_code, outcome.horizon)
                existing = connection.execute(
                    "SELECT payload_hash, payload FROM recommendation_outcomes WHERE snapshot_id = ? AND strategy = ? AND stock_code = ? AND horizon = ?",
                    key,
                ).fetchone()
                _raise_on_conflict(existing, payload, "recommendation outcome")
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO recommendation_outcomes(
                            snapshot_id, strategy, recommend_date, stock_code, horizon,
                            status, settled_at, payload_hash, payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            *key[:2],
                            outcome.recommend_date,
                            key[2],
                            key[3],
                            outcome.status,
                            outcome.settled_at.isoformat(),
                            _sha256(payload),
                            payload,
                        ),
                    )

    def _is_fully_settled(self, snapshot_id: str, strategy: Strategy, code: str) -> bool:
        horizons = (2, 3, 5) if strategy is Strategy.D25 else (1,)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT horizon FROM recommendation_outcomes WHERE snapshot_id = ? AND strategy = ? AND stock_code = ?",
                (snapshot_id, strategy.value, code),
            ).fetchall()
        return {int(row["horizon"]) for row in rows} == set(horizons)

    def _atr20_pct(self, code: str, trade_date: str) -> float:
        record = self._historical.load_historical_feature_recent(code, trade_date)
        if record is None:
            return 0.0
        summary = record.payload.get("history_summary")
        profile = summary.get("profile") if isinstance(summary, Mapping) else None
        value = profile.get("atr20_pct") if isinstance(profile, Mapping) else None
        numeric = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        return numeric if math.isfinite(numeric) and numeric > 0.0 else 0.0

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _outcome_bytes(outcome: RecommendationOutcome) -> bytes:
    return _canonical_bytes(
        {
            "schema_version": outcome.version,
            "snapshot_id": outcome.snapshot_id,
            "strategy": outcome.strategy.value,
            "recommend_date": outcome.recommend_date,
            "stock_code": outcome.stock_code,
            "horizon": outcome.horizon,
            "status": outcome.status,
            "anchor_price": outcome.anchor_price,
            "atr20_pct": outcome.atr20_pct,
            "minimum_low": outcome.minimum_low,
            "end_close": outcome.end_close,
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
            "schema_version": "v2_benchmark_return_v1",
            "trade_date": benchmark.trade_date,
            "return_pct": benchmark.return_pct,
        }
    )


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _raise_on_conflict(existing: sqlite3.Row | None, payload: bytes, label: str) -> None:
    if existing is None:
        return
    if str(existing["payload_hash"]) != _sha256(payload) or bytes(existing["payload"]) != payload:
        raise OutcomeEvidenceConflictError(f"immutable {label} conflict")


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS benchmark_returns(
    trade_date TEXT PRIMARY KEY,
    return_pct REAL NOT NULL,
    observed_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendation_outcomes(
    snapshot_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    recommend_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    status TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY(snapshot_id, strategy, stock_code, horizon)
);
CREATE INDEX IF NOT EXISTS recommendation_outcomes_date_idx
ON recommendation_outcomes(recommend_date, strategy);
"""


__all__ = [
    "OutcomeEvidenceConflictError",
    "OutcomeEvidenceStatus",
    "SQLiteOutcomeEvidenceRepository",
]

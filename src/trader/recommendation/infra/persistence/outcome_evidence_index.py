"""Recommendation-owned outcome settlement and immutable market benchmarks."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

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
from trader.recommendation.application.ports.outcomes import OutcomeEvidenceConflictError
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.domain.outcomes.models import (
    BenchmarkReturn,
    OutcomeHorizons,
    OutcomeTarget,
    RecommendationOutcome,
    TomorrowRecommendationOutcome,
)
from trader.recommendation.domain.outcomes.tomorrow_target import TOMORROW_CLOSE_GAIN_TARGET, TomorrowCloseGain


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
        freezes: FreezeRecordPort, *, sqlite_policy: SqliteIoPolicy, tomorrow_database: Path | None = None,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._database = runtime_root / "outcomes.sqlite3"
        self._tomorrow_database = tomorrow_database if tomorrow_database is not None else self._database
        self._freezes = freezes
        self._lock = threading.RLock()
        self._initialized = False
        self._tomorrow_initialized = False
        self._tomorrow_scan_after: date | None = None

    @classmethod
    def inspect_status(cls, runtime_root: Path, *, sqlite_policy: SqliteIoPolicy) -> OutcomeEvidenceStatus:
        database = runtime_root / "outcomes.sqlite3"
        if not database.is_file():
            return OutcomeEvidenceStatus(False, 0, 0, 0, None, "")
        try:
            with SqliteSession(database, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
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
        identities, sample_counts, targets = _summary_samples(self._freezes, strategy, limit)
        completed = {
            **_summary_returns(
                self._database, strategy,
                {key: value for key, value in identities.items() if value[1] is None},
                sqlite_policy=self._sqlite_policy,
            ),
            **_summary_returns(
                self._tomorrow_database, strategy,
                {key: value for key, value in identities.items() if value[1] is not None},
                sqlite_policy=self._sqlite_policy,
            ),
        }
        summaries: list[OutcomeTierSummary] = []
        for target in targets:
            for tier in _TOMORROW_TIERS if target is not None else _SUMMARY_TIERS:
                for horizon in OutcomeHorizons(strategy):
                    key = (tier, horizon, target)
                    values = completed.get(key, [])
                    sample_count = sample_counts.get(key, 0)
                    if target is not None:
                        summaries.append(
                            OutcomeTierSummary(
                                tier, horizon, sample_count, len(values), 0, None,
                                target, sum(value is TomorrowCloseGain.HIT for value in values),
                            )
                        )
                        continue
                    returns = tuple(value for value in values if isinstance(value, float))
                    summaries.append(
                        OutcomeTierSummary(
                            tier, horizon, sample_count, len(returns), sum(value > 0 for value in returns),
                            sum(returns) / len(returns) if returns else None,
                        )
                    )
        return tuple(summaries)

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._database.parent.mkdir(parents=True, exist_ok=True)
            with SqliteSession(
                self._database, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=self._sqlite_policy
            ) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
            self._initialized = True

    def tomorrow_hits(self, record_version: str) -> tuple[tuple[str, TomorrowCloseGain], ...]:
        if not self._tomorrow_database.is_file():
            return ()
        try:
            with SqliteSession(
                self._tomorrow_database, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy
            ) as connection:
                rows = connection.run(
                    "SELECT code, record_hash, record_bytes FROM recommendation_outcomes "
                    "WHERE snapshot_id=? AND strategy=? AND horizon=1 ORDER BY code",
                    (record_version, Strategy.TOMORROW.value),
                ).fetchall()
        except sqlite3.Error as exc:
            raise OutcomeEvidenceConflictError("Tomorrow outcome read failed") from exc
        return tuple(
            (str(row["code"]), label)
            for row in rows
            if isinstance(
                label := _summary_value(bytes(row["record_bytes"]), bytes(row["record_hash"]), TOMORROW_CLOSE_GAIN_TARGET),
                TomorrowCloseGain,
            )
        )

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
                if dict(record.freeze.input_versions).get("prediction_target") == TOMORROW_CLOSE_GAIN_TARGET:
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

    def pending_tomorrow_targets(self, *, limit: int) -> Sequence[OutcomeTarget]:
        if limit < 1:
            raise ValueError("outcome target limit must be positive")
        targets: list[OutcomeTarget] = []
        with self._lock:
            days = self._freezes.list_settlement_dates(
                Strategy.TOMORROW, after=self._tomorrow_scan_after, limit=64
            )
            if not days:
                self._tomorrow_scan_after = None
                return ()
            for trade_date in days:
                self._tomorrow_scan_after = trade_date
                record = self._freezes.load(Strategy.TOMORROW, trade_date)
                if (
                    record is None
                    or dict(record.freeze.input_versions).get("prediction_target") != TOMORROW_CLOSE_GAIN_TARGET
                ):
                    continue
                for item in record.freeze.items:
                    if item.selected and item.quote is not None and self._pending_horizons(
                        record.version, Strategy.TOMORROW, item.code, database=self._tomorrow_database
                    ):
                        targets.append(
                            OutcomeTarget(
                                record.version, Strategy.TOMORROW, trade_date.isoformat(), item.code,
                                item.quote.price, None, (1,), entry_at=item.quote.source_time,
                                prediction_target=TOMORROW_CLOSE_GAIN_TARGET,
                            )
                        )
                if len(targets) >= limit:
                    return tuple(targets[:limit])
            if len(days) < 64:
                self._tomorrow_scan_after = None
        return tuple(targets)

    def record_benchmark_return(self, benchmark: BenchmarkReturn, *, observed_at: datetime) -> None:
        payload = _benchmark_bytes(benchmark)
        self.initialize()
        with (
            self._lock,
            SqliteSession(self._database, _CONNECTION_OPTIONS, sqlite_policy=self._sqlite_policy) as connection,
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

    def benchmark_return(self, trade_date: str) -> BenchmarkReturn | None:
        if not self._database.is_file():
            return None
        with self._lock, SqliteSession(self._database, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            EnsureSqliteSchema(connection, _SCHEMA)
            row = connection.run(
                "SELECT trade_date, return_pct, record_hash, record_bytes FROM benchmark_returns WHERE trade_date=?",
                (EncodeDate(date.fromisoformat(trade_date)),),
            ).fetchone()
        if row is None:
            return None
        return _read_benchmark(row)

    def benchmark_returns_after(self, recommend_date: str, *, limit: int) -> Sequence[BenchmarkReturn]:
        if limit < 1:
            raise ValueError("benchmark return limit must be positive")
        if not self._database.is_file():
            return ()
        with self._lock, SqliteSession(self._database, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
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
        return tuple(_read_benchmark(row) for row in rows)

    def save_recommendation_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None:
        if any(outcome.is_retryable for outcome in outcomes):
            raise ValueError("retryable recommendation outcomes cannot be persisted")
        self._save_outcomes(outcomes)

    def save_tomorrow_outcomes(self, outcomes: Sequence[TomorrowRecommendationOutcome]) -> None:
        self._save_outcomes(outcomes, database=self._tomorrow_database)

    def _save_outcomes(
        self, outcomes: Sequence[RecommendationOutcome | TomorrowRecommendationOutcome], *, database: Path | None = None,
    ) -> None:
        target_database = self._database if database is None else database
        if target_database == self._database:
            self.initialize()
        else:
            with self._lock:
                if not self._tomorrow_initialized:
                    target_database.parent.mkdir(parents=True, exist_ok=True)
                    with SqliteSession(
                        target_database, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=self._sqlite_policy
                    ) as connection:
                        EnsureSqliteSchema(connection, _TOMORROW_SCHEMA, allow_create=True, exact=True)
                    self._tomorrow_initialized = True
        with (
            self._lock,
            SqliteSession(target_database, _CONNECTION_OPTIONS, sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            for outcome in outcomes:
                if isinstance(outcome, TomorrowRecommendationOutcome):
                    target = outcome.target
                    payload = _tomorrow_outcome_bytes(outcome)
                    key = (target.snapshot_id, target.strategy.value, target.stock_code, 1)
                    recommend_date, status = target.recommend_date, "complete"
                else:
                    payload = _outcome_bytes(outcome)
                    key = (outcome.snapshot_id, outcome.strategy.value, outcome.stock_code, outcome.horizon)
                    recommend_date, status = outcome.recommend_date, outcome.status
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
                            EncodeDate(date.fromisoformat(recommend_date)),
                            key[2],
                            key[3],
                            status,
                            EncodeTime(outcome.settled_at),
                            _sha256(payload),
                            payload,
                        ),
                    )

    def _pending_horizons(
        self, snapshot_id: str, strategy: Strategy, code: str, *, database: Path | None = None,
    ) -> tuple[int, ...]:
        horizons = OutcomeHorizons(strategy)
        target_database = self._database if database is None else database
        if not target_database.is_file():
            return horizons
        with self._lock, SqliteSession(
            target_database, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy
        ) as connection:
            rows = connection.run(
                "SELECT horizon FROM recommendation_outcomes WHERE snapshot_id = ? AND strategy = ? AND code = ?",
                (snapshot_id, strategy.value, code),
            ).fetchall()
        settled = {int(row["horizon"]) for row in rows}
        return tuple(horizon for horizon in horizons if horizon not in settled)


_CONNECTION_OPTIONS = SqliteConnectionOptions()
_SummaryTier = Literal["recommend", "watch", "selected"]
_SummaryKey = tuple[_SummaryTier, int, str | None]
_SUMMARY_TIERS: tuple[_SummaryTier, ...] = ("recommend", "watch")
_TOMORROW_TIERS: tuple[_SummaryTier, ...] = ("recommend", "selected")
_SummaryIdentities = dict[tuple[str, str], tuple[tuple[_SummaryTier, ...], str | None]]


def _summary_samples(
    freezes: FreezeRecordPort, strategy: Strategy, limit: int
) -> tuple[_SummaryIdentities, dict[_SummaryKey, int], tuple[str | None, ...]]:
    identities: _SummaryIdentities = {}
    sample_counts: dict[_SummaryKey, int] = {}
    targets: list[str | None] = []
    for trade_date in freezes.list_dates(strategy, limit=limit):
        record = freezes.load(strategy, trade_date)
        if record is None:
            continue
        target = dict(record.freeze.input_versions).get("prediction_target")
        if target not in targets:
            targets.append(target)
        for item in record.freeze.items:
            if not item.selected or item.quote is None:
                continue
            recommended = item.action is RecommendationAction.EXECUTABLE
            tiers: tuple[_SummaryTier, ...]
            if target is None:
                tiers = ("recommend",) if recommended else ("watch",)
            else:
                tiers = ("recommend", "selected") if recommended else ("selected",)
            identities[(record.version, item.code)] = tiers, target
            for tier in tiers:
                for horizon in OutcomeHorizons(strategy):
                    key = (tier, horizon, target)
                    sample_counts[key] = sample_counts.get(key, 0) + 1
    return identities, sample_counts, tuple(targets) if targets else (None,)


def _summary_returns(
    database: Path, strategy: Strategy,
    identities: _SummaryIdentities, *,
    sqlite_policy: SqliteIoPolicy,
) -> dict[_SummaryKey, list[float | TomorrowCloseGain]]:
    completed: dict[_SummaryKey, list[float | TomorrowCloseGain]] = {}
    if not database.is_file() or not identities:
        return completed
    for snapshot_id, code, horizon, record_hash, record_bytes in _summary_rows(database, strategy, identities, sqlite_policy=sqlite_policy):
        identity = identities.get((snapshot_id, code))
        if identity is None:
            continue
        tiers, target = identity
        value = _summary_value(record_bytes, record_hash, target)
        if value is not None:
            for tier in tiers:
                completed.setdefault((tier, int(horizon), target), []).append(value)
    return completed


def _summary_rows(
    database: Path, strategy: Strategy, identities: _SummaryIdentities, *,
    sqlite_policy: SqliteIoPolicy,
) -> list[tuple[str, str, int, bytes, bytes]]:
    snapshot_ids = tuple(sorted({snapshot_id for snapshot_id, _code in identities}))
    placeholders = ",".join("?" for _snapshot_id in snapshot_ids)
    try:
        with SqliteSession(database, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
            rows = connection.run(
                "SELECT snapshot_id, code, horizon, record_hash, record_bytes "
                f"FROM recommendation_outcomes WHERE strategy = ? AND snapshot_id IN ({placeholders})",
                (strategy.value, *snapshot_ids),
            ).fetchall()
            return [(str(row[0]), str(row[1]), int(row[2]), bytes(row[3]), bytes(row[4])) for row in rows]
    except sqlite3.Error as exc:
        raise OutcomeEvidenceConflictError("outcome summary read failed") from exc


def _summary_value(payload: bytes, record_hash: bytes, target: str | None) -> float | TomorrowCloseGain | None:
    if _sha256(payload) != record_hash:
        raise OutcomeEvidenceConflictError("outcome summary payload hash mismatch")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutcomeEvidenceConflictError("outcome summary payload is invalid") from exc
    if not isinstance(value, dict) or value.get("prediction_target") != target:
        raise OutcomeEvidenceConflictError("outcome summary target is invalid")
    if target == TOMORROW_CLOSE_GAIN_TARGET:
        try:
            label = TomorrowCloseGain(value["label"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OutcomeEvidenceConflictError("outcome summary label is invalid") from exc
        if label is TomorrowCloseGain.PENDING:
            raise OutcomeEvidenceConflictError("outcome summary must contain mature evidence")
        return label
    net = value.get("net_excess_return_pct")
    if value.get("status") != "complete" or not isinstance(net, (int, float)):
        return None
    if not math.isfinite(net):
        raise OutcomeEvidenceConflictError("outcome summary net excess is invalid")
    return float(net)


def _tomorrow_outcome_bytes(outcome: TomorrowRecommendationOutcome) -> bytes:
    evidence = outcome.evidence
    return _canonical_bytes({
        "snapshot_id": outcome.target.snapshot_id,
        "strategy": Strategy.TOMORROW.value,
        "recommend_date": outcome.target.recommend_date,
        "stock_code": outcome.target.stock_code,
        "horizon": 1,
        "status": "complete",
        "prediction_target": TOMORROW_CLOSE_GAIN_TARGET,
        "label": outcome.label.value,
        "publication_hash": evidence.publication_hash,
        "next_date": evidence.next_date.isoformat(),
        "signal_raw_close": str(evidence.signal_raw_close) if evidence.signal_raw_close is not None else None,
        "signal_close": str(evidence.signal_close) if evidence.signal_close is not None else None,
        "next_close": str(evidence.next_close) if evidence.next_close is not None else None,
        "next_raw_reference": str(evidence.next_raw_reference) if evidence.next_raw_reference is not None else None,
        "next_raw_close": str(evidence.next_raw_close) if evidence.next_raw_close is not None else None,
        "confirmed_suspended": evidence.confirmed_suspended,
    })


def _outcome_bytes(outcome: RecommendationOutcome) -> bytes:
    return _canonical_bytes(
        {

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

            "trade_date": benchmark.trade_date,
            "return_pct": benchmark.return_pct,
        }
    )


def _read_benchmark(row: sqlite3.Row) -> BenchmarkReturn:
    if not isinstance(row["record_hash"], bytes) or not isinstance(row["record_bytes"], bytes):
        raise OutcomeEvidenceConflictError("invalid immutable benchmark return")
    try:
        benchmark = BenchmarkReturn(DecodeDate(row["trade_date"]).isoformat(), float(row["return_pct"]))
        _raise_on_conflict(row, _benchmark_bytes(benchmark), "benchmark return")
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutcomeEvidenceConflictError("invalid immutable benchmark return") from exc
    return benchmark


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
_TOMORROW_SCHEMA = SqliteSchema(_SCHEMA.statements[1:])


__all__ = [
    "OutcomeEvidenceStatus",
    "SQLiteOutcomeEvidenceIndex",
]

"""Immutable SQLite prices for independent historical screening."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import cast

from trader.infra.sqlite_codec import DecodeDate, DecodeHash, EncodeDate, EncodeHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)
from trader.training.application.historical_backtest import HistoricalScreeningDay
from trader.training.application.historical_screening import (
    HistoricalPriceHistoryIdentity,
    HistoricalScreeningSnapshotManifest,
    HistoricalScreeningSnapshotStatus,
    HistoricalSecurity,
    ResearchBoard,
)
from trader.training.application.tomorrow_historical_screening import (
    HistoricalBoard,
    TomorrowHistoricalRow,
)
from trader.training.application.tomorrow_historical_validation import TomorrowHistoricalRiskRow
from trader.training.domain.evaluation.historical_screening import (
    HISTORICAL_SCREENING_SPEC,
    HistoricalPriceBar,
    HistoricalScreeningSpec,
)


class HistoricalScreeningSnapshotConflictError(RuntimeError):
    pass


class SQLiteHistoricalScreeningSnapshot:
    def __init__(self, runtime_dir: Path) -> None:
        self._root = runtime_dir / "experiments" / HISTORICAL_SCREENING_SPEC.research_identity
        self._database = self._root / "samples.sqlite3"
        self._lock = threading.RLock()

    def register_universe(
        self,
        spec: HistoricalScreeningSpec,
        universe: Sequence[HistoricalSecurity],
    ) -> None:
        self._initialize()
        ordered = tuple(sorted(universe, key=lambda item: item.code))
        if len({item.code for item in ordered}) != len(ordered):
            raise ValueError("historical screening universe contains duplicate codes")
        with (
            SqliteSession(
                self._database, SqliteConnectionOptions(timeout_seconds=30.0, foreign_keys=True)
            ) as connection,
            SqliteTransaction(connection),
        ):
            self._register_spec(connection, spec)
            existing_codes = tuple(
                str(row[0])
                for row in connection.run(
                    "SELECT code FROM screening_universe WHERE research_id = ? ORDER BY code",
                    (spec.research_identity,),
                ).fetchall()
            )
            requested_codes = tuple(item.code for item in ordered)
            if existing_codes and existing_codes != requested_codes:
                raise HistoricalScreeningSnapshotConflictError("historical screening universe set conflict")
            for item in ordered:
                payload = _canonical(asdict(item))
                existing = connection.run(
                    "SELECT content_hash FROM screening_universe WHERE research_id = ? AND code = ?",
                    (spec.research_identity, item.code),
                ).fetchone()
                if existing is not None and DecodeHash(existing[0]) != _sha256(payload):
                    raise HistoricalScreeningSnapshotConflictError("historical screening universe identity conflict")
                connection.run(
                    """
                    INSERT OR IGNORE INTO screening_universe(
                        research_id, code, board, name, is_st, is_suspended, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.research_identity,
                        item.code,
                        item.board,
                        item.name,
                        int(item.is_st),
                        int(item.is_suspended),
                        EncodeHash(_sha256(payload)),
                    ),
                )

    def completed_codes(self, research_identity: str) -> frozenset[str]:
        if not self._database.is_file():
            return frozenset()
        with (
            SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            EnsureSqliteSchema(connection, _SCHEMA)
            rows = connection.run(
                "SELECT code FROM screening_download WHERE research_id = ? AND status = 'complete'",
                (research_identity,),
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def registered_universe(self, research_identity: str) -> tuple[HistoricalSecurity, ...]:
        if not self._database.is_file():
            return ()
        with (
            SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            EnsureSqliteSchema(connection, _SCHEMA)
            rows = connection.run(
                """
                SELECT code, board, name, is_st, is_suspended
                FROM screening_universe WHERE research_id = ? ORDER BY code
                """,
                (research_identity,),
            ).fetchall()
        return tuple(
            HistoricalSecurity(str(code), cast(ResearchBoard, str(board)), str(name), bool(is_st), bool(is_suspended))
            for code, board, name, is_st, is_suspended in rows
        )

    def save_history(
        self,
        spec: HistoricalScreeningSpec,
        code: str,
        bars: Sequence[HistoricalPriceBar],
    ) -> None:
        self._initialize()
        ordered = tuple(bars)
        with (
            SqliteSession(
                self._database, SqliteConnectionOptions(timeout_seconds=30.0, foreign_keys=True)
            ) as connection,
            SqliteTransaction(connection),
        ):
            self._register_spec(connection, spec)
            if (
                connection.run(
                    "SELECT 1 FROM screening_universe WHERE research_id = ? AND code = ?",
                    (spec.research_identity, code),
                ).fetchone()
                is None
            ):
                raise ValueError("historical screening code is outside the registered universe")
            hashes: list[str] = []
            for bar in ordered:
                payload = _canonical(_bar_payload(bar))
                payload_hash = _sha256(payload)
                hashes.append(payload_hash)
                existing = connection.run(
                    """
                    SELECT content_hash FROM screening_price
                    WHERE research_id = ? AND code = ? AND trade_date = ?
                    """,
                    (spec.research_identity, code, EncodeDate(bar.trade_date)),
                ).fetchone()
                if existing is not None and DecodeHash(existing[0]) != payload_hash:
                    raise HistoricalScreeningSnapshotConflictError("historical screening bar identity conflict")
                connection.run(
                    """
                    INSERT OR IGNORE INTO screening_price(
                        research_id, code, trade_date, open, close, high, low,
                        volume, amount, change_pct, turnover_pct, adjustment, source, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.research_identity,
                        code,
                        EncodeDate(bar.trade_date),
                        bar.open_price,
                        bar.close,
                        bar.high,
                        bar.low,
                        bar.volume,
                        bar.amount,
                        bar.pct_change,
                        bar.turnover_rate,
                        bar.adjustment,
                        bar.source,
                        EncodeHash(payload_hash),
                    ),
                )
            content_hash = _sha256(_canonical({"code": code, "bars": hashes}))
            existing_download = connection.run(
                """
                SELECT content_hash FROM screening_download
                WHERE research_id = ? AND code = ? AND status = 'complete'
                """,
                (spec.research_identity, code),
            ).fetchone()
            if existing_download is not None and DecodeHash(existing_download[0]) != content_hash:
                raise HistoricalScreeningSnapshotConflictError("historical screening download identity conflict")
            connection.run(
                """
                INSERT INTO screening_download(research_id, code, status, bar_count, content_hash, failure_code)
                VALUES (?, ?, 'complete', ?, ?, NULL)
                ON CONFLICT(research_id, code) DO UPDATE SET
                    status = excluded.status,
                    bar_count = excluded.bar_count,
                    content_hash = excluded.content_hash,
                    failure_code = excluded.failure_code
                """,
                (spec.research_identity, code, len(ordered), EncodeHash(content_hash)),
            )

    def record_failure(self, spec: HistoricalScreeningSpec, code: str, error_code: str) -> None:
        if not error_code or len(error_code) > 64:
            raise ValueError("historical screening failure code is invalid")
        self._initialize()
        with (
            SqliteSession(
                self._database, SqliteConnectionOptions(timeout_seconds=30.0, foreign_keys=True)
            ) as connection,
            SqliteTransaction(connection),
        ):
            self._register_spec(connection, spec)
            connection.run(
                """
                INSERT INTO screening_download(research_id, code, status, bar_count, content_hash, failure_code)
                VALUES (?, ?, 'failed', 0, NULL, ?)
                ON CONFLICT(research_id, code) DO UPDATE SET
                    status = CASE WHEN screening_download.status = 'complete' THEN screening_download.status ELSE excluded.status END,
                    failure_code = CASE
                        WHEN screening_download.status = 'complete' THEN screening_download.failure_code ELSE excluded.failure_code
                    END
                """,
                (spec.research_identity, code, error_code),
            )

    def inspect(self, research_identity: str) -> HistoricalScreeningSnapshotStatus:
        if not self._database.is_file():
            return HistoricalScreeningSnapshotStatus(research_identity=research_identity)
        try:
            with (
                SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
                SqliteTransaction(connection),
            ):
                EnsureSqliteSchema(connection, _SCHEMA)
                spec = connection.run(
                    "SELECT spec_hash FROM screening_spec WHERE research_id = ?", (research_identity,)
                ).fetchone()
                universe_count = int(
                    connection.run(
                        "SELECT COUNT(*) FROM screening_universe WHERE research_id = ?", (research_identity,)
                    ).require_one()[0]
                )
                completed, failed = connection.run(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                    FROM screening_download WHERE research_id = ?
                    """,
                    (research_identity,),
                ).require_one()
                bar_count, first_date, last_date = connection.run(
                    """
                    SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
                    FROM screening_price WHERE research_id = ?
                    """,
                    (research_identity,),
                ).require_one()
        except sqlite3.DatabaseError:
            return HistoricalScreeningSnapshotStatus(initialized=True, research_identity=research_identity)
        return HistoricalScreeningSnapshotStatus(
            initialized=True,
            research_identity=research_identity,
            universe_count=universe_count,
            completed_codes=int(completed or 0),
            failed_codes=int(failed or 0),
            bar_count=int(bar_count or 0),
            first_trade_date=DecodeDate(first_date).isoformat() if first_date is not None else None,
            last_trade_date=DecodeDate(last_date).isoformat() if last_date is not None else None,
            spec_hash=DecodeHash(spec[0]) if spec is not None else "",
        )

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalScreeningSnapshotManifest:
        empty_hash = _sha256(_canonical(()))
        if not self._database.is_file():
            return HistoricalScreeningSnapshotManifest(spec.research_identity, "", empty_hash, empty_hash, ())
        with (
            SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            EnsureSqliteSchema(connection, _SCHEMA)
            persisted_spec = connection.run(
                "SELECT spec_hash FROM screening_spec WHERE research_id = ?", (spec.research_identity,)
            ).fetchone()
            if persisted_spec is None or DecodeHash(persisted_spec[0]) != spec.content_hash:
                raise HistoricalScreeningSnapshotConflictError("historical screening spec manifest conflict")
            universe_rows = connection.run(
                """
                SELECT code, board, name, is_st, is_suspended, content_hash
                FROM screening_universe WHERE research_id = ? ORDER BY code
                """,
                (spec.research_identity,),
            ).fetchall()
            universe_identities: list[tuple[str, str]] = []
            for code, board, name, is_st, is_suspended, persisted_hash in universe_rows:
                security = HistoricalSecurity(
                    str(code),
                    cast(ResearchBoard, str(board)),
                    str(name),
                    bool(is_st),
                    bool(is_suspended),
                )
                payload_hash = _sha256(_canonical(asdict(security)))
                if payload_hash != DecodeHash(persisted_hash):
                    raise HistoricalScreeningSnapshotConflictError("historical screening universe payload conflict")
                universe_identities.append((security.code, payload_hash))
            downloads = {
                str(code): (int(bar_count), DecodeHash(content_hash))
                for code, bar_count, content_hash in connection.run(
                    """
                    SELECT code, bar_count, content_hash FROM screening_download
                    WHERE research_id = ? AND status = 'complete' ORDER BY code
                    """,
                    (spec.research_identity,),
                ).fetchall()
            }
            bar_rows = connection.run(
                """
                SELECT code, trade_date, open, close, high, low, volume, amount,
                       change_pct, turnover_pct, adjustment, source, content_hash
                FROM screening_price WHERE research_id = ? ORDER BY code, trade_date
                """,
                (spec.research_identity,),
            )
            bar_hashes: dict[str, list[str]] = {}
            for row in bar_rows:
                code = str(row[0])
                payload = {
                    "trade_date": DecodeDate(row[1]).isoformat(),
                    "open_price": float(row[2]),
                    "close": float(row[3]),
                    "high": float(row[4]),
                    "low": float(row[5]),
                    "volume": float(row[6]),
                    "amount": float(row[7]),
                    "pct_change": float(row[8]),
                    "turnover_rate": float(row[9]) if row[9] is not None else None,
                    "adjustment": str(row[10]),
                    "source": str(row[11]),
                }
                payload_hash = _sha256(_canonical(payload))
                if payload_hash != DecodeHash(row[12]):
                    raise HistoricalScreeningSnapshotConflictError("historical screening bar payload conflict")
                bar_hashes.setdefault(code, []).append(payload_hash)
        if set(bar_hashes) != set(downloads):
            raise HistoricalScreeningSnapshotConflictError("historical screening completed history set conflict")
        histories: list[HistoricalPriceHistoryIdentity] = []
        for code in sorted(downloads):
            persisted_count, persisted_hash = downloads[code]
            hashes = bar_hashes[code]
            content_hash = _sha256(_canonical({"code": code, "bars": hashes}))
            if persisted_count != len(hashes) or persisted_hash != content_hash:
                raise HistoricalScreeningSnapshotConflictError("historical screening history content conflict")
            histories.append(HistoricalPriceHistoryIdentity(code, len(hashes), content_hash))
        universe_hash = _sha256(_canonical({"securities": universe_identities}))
        histories_hash = _sha256(_canonical({"histories": [asdict(item) for item in histories]}))
        return HistoricalScreeningSnapshotManifest(
            spec.research_identity,
            spec.content_hash,
            universe_hash,
            histories_hash,
            tuple(histories),
        )

    def screening_days(self, spec: HistoricalScreeningSpec) -> tuple[HistoricalScreeningDay, ...]:
        """Return fixed OHLCV-only daily diagnostics without mutating the snapshot."""

        if not self._database.is_file():
            return ()
        with (
            SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            EnsureSqliteSchema(connection, _SCHEMA)
            rows = connection.run(
                _SCREENING_QUERY,
                {
                    "identity": spec.research_identity,
                    "start": EncodeDate(spec.training_start),
                    "end": EncodeDate(spec.validation_end),
                    "cost_pct": spec.round_trip_cost_bps / 100.0,
                },
            ).fetchall()
        return tuple(
            HistoricalScreeningDay(
                trade_date=DecodeDate(row[0]),
                population=int(row[1]),
                selected=int(row[2]),
                selected_return_1d_pct=float(row[3]),
                selected_return_5d_pct=float(row[4]),
                benchmark_return_1d_pct=float(row[5]),
                benchmark_return_5d_pct=float(row[6]),
                severe_loss_rate=float(row[7]),
            )
            for row in rows
        )

    def tomorrow_historical_rows(self, spec: HistoricalScreeningSpec) -> tuple[TomorrowHistoricalRow, ...]:
        """Return freeze-day qfq features and next-day labels bound to the H0 snapshot."""

        return tuple(self.iter_tomorrow_historical_rows(spec))

    def iter_tomorrow_historical_rows(
        self,
        spec: HistoricalScreeningSpec,
    ) -> Iterator[TomorrowHistoricalRow]:
        """Stream H0 model rows one cross-section at a time for bounded offline fitting."""

        if not self._database.is_file():
            return
        with (
            SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            EnsureSqliteSchema(connection, _SCHEMA)
            rows = connection.run(
                _TOMORROW_HISTORICAL_QUERY,
                {
                    "identity": spec.research_identity,
                    "start": EncodeDate(spec.training_start),
                    "end": EncodeDate(spec.validation_end),
                },
            )
            daily: list[tuple[object, ...]] = []
            trade_date_value: str | None = None
            for raw in rows:
                row = tuple(raw)
                current = str(row[0])
                if trade_date_value is not None and current != trade_date_value:
                    yield from _tomorrow_historical_day_rows(daily)
                    daily = []
                trade_date_value = current
                daily.append(row)
            if daily:
                yield from _tomorrow_historical_day_rows(daily)

    def tomorrow_historical_risk_rows(
        self,
        spec: HistoricalScreeningSpec,
    ) -> tuple[TomorrowHistoricalRiskRow, ...]:
        """Return a typed historical risk dataset without mutating the historical row identity."""

        if not self._database.is_file():
            return ()
        with (
            SqliteSession(self._database, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            EnsureSqliteSchema(connection, _SCHEMA)
            rows = connection.run(
                _TOMORROW_HISTORICAL_QUERY,
                {
                    "identity": spec.research_identity,
                    "start": EncodeDate(spec.training_start),
                    "end": EncodeDate(spec.validation_end),
                },
            ).fetchall()
        historical_rows = _tomorrow_historical_rows(tuple(tuple(row) for row in rows))
        return tuple(
            TomorrowHistoricalRiskRow(
                trade_date=historical.trade_date,
                code=historical.code,
                board=historical.board,
                alpha_features=historical.alpha_features,
                realized_volatility_20d=historical.realized_volatility_20d,
                downside_semivariance_20d=historical.downside_semivariance_20d,
                drawdown_recovery_60d=historical.drawdown_recovery_60d,
                amihud_20d=historical.amihud_20d,
                average_amount_20d=historical.average_amount_20d,
                baseline_score=historical.baseline_score,
                gross_return=_number(raw[17]),
                benchmark_return=_number(raw[18]),
                gross_excess_return=historical.gross_excess_return,
                atr20_pct=_number(raw[19]),
                mae_atr20=historical.mae_atr20,
            )
            for historical, raw in zip(historical_rows, rows, strict=True)
        )

    def _initialize(self) -> None:
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            with SqliteSession(self._database, SqliteConnectionOptions(journal_mode="WAL")) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)

    def _register_spec(self, connection: SqliteConnection, spec: HistoricalScreeningSpec) -> None:
        existing = connection.run(
            "SELECT spec_hash FROM screening_spec WHERE research_id = ?", (spec.research_identity,)
        ).fetchone()
        if existing is not None and DecodeHash(existing[0]) != spec.content_hash:
            raise HistoricalScreeningSnapshotConflictError("historical screening spec identity conflict")
        connection.run(
            "INSERT OR IGNORE INTO screening_spec(research_id, spec_hash) VALUES (?, ?)",
            (spec.research_identity, EncodeHash(spec.content_hash)),
        )


def _bar_payload(bar: HistoricalPriceBar) -> dict[str, object]:
    return {
        "trade_date": bar.trade_date.isoformat(),
        "open_price": bar.open_price,
        "close": bar.close,
        "high": bar.high,
        "low": bar.low,
        "volume": bar.volume,
        "amount": bar.amount,
        "pct_change": bar.pct_change,
        "turnover_rate": bar.turnover_rate,
        "adjustment": bar.adjustment,
        "source": bar.source,
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_SCHEMA = SqliteSchema(
    1,
    (
        """
        CREATE TABLE screening_spec(
            research_id TEXT NOT NULL PRIMARY KEY,
            spec_hash BLOB NOT NULL CHECK(length(spec_hash)=32)
        )
        """,
        """
        CREATE TABLE screening_universe(
            research_id TEXT NOT NULL,
            code TEXT NOT NULL,
            board TEXT NOT NULL,
            name TEXT NOT NULL,
            is_st INTEGER NOT NULL,
            is_suspended INTEGER NOT NULL,
            content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
            PRIMARY KEY(research_id, code),
            FOREIGN KEY(research_id) REFERENCES screening_spec(research_id)
        )
        """,
        """
        CREATE TABLE screening_price(
            research_id TEXT NOT NULL,
            code TEXT NOT NULL,
            trade_date INTEGER NOT NULL,
            open REAL NOT NULL,
            close REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            volume REAL NOT NULL,
            amount REAL NOT NULL,
            change_pct REAL NOT NULL,
            turnover_pct REAL,
            adjustment TEXT NOT NULL,
            source TEXT NOT NULL,
            content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
            PRIMARY KEY(research_id, code, trade_date),
            FOREIGN KEY(research_id, code) REFERENCES screening_universe(research_id, code)
        )
        """,
        """
        CREATE TABLE screening_download(
            research_id TEXT NOT NULL,
            code TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('complete', 'failed')),
            bar_count INTEGER NOT NULL,
            content_hash BLOB CHECK(content_hash IS NULL OR length(content_hash)=32),
            failure_code TEXT,
            PRIMARY KEY(research_id, code)
        )
        """,
        "CREATE INDEX screening_price_date ON screening_price(research_id, trade_date)",
    ),
)

_SCREENING_QUERY = """
WITH ordered AS (
    SELECT
        code,
        trade_date,
        close,
        amount,
        change_pct,
        LAG(close, 20) OVER (PARTITION BY code ORDER BY trade_date) AS close_20,
        LEAD(close, 1) OVER (PARTITION BY code ORDER BY trade_date) AS close_f1,
        LEAD(close, 5) OVER (PARTITION BY code ORDER BY trade_date) AS close_f5,
        COUNT(*) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 60 PRECEDING AND CURRENT ROW
        ) AS history_count,
        AVG(change_pct) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_return_20,
        AVG(change_pct * change_pct) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_square_return_20,
        AVG(amount) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_amount_20
    FROM screening_price
    WHERE research_id = :identity
), metrics AS (
    SELECT
        code,
        trade_date,
        100.0 * (close / close_20 - 1.0) AS momentum_20,
        MAX(0.0, mean_square_return_20 - mean_return_20 * mean_return_20) AS variance_20,
        mean_amount_20,
        100.0 * (close_f1 / close - 1.0) AS return_1d,
        100.0 * (close_f5 / close - 1.0) AS return_5d
    FROM ordered
    WHERE history_count >= 61
      AND close_20 > 0.0
      AND close_f1 > 0.0
      AND close_f5 > 0.0
      AND trade_date BETWEEN :start AND :end
), component_ranks AS (
    SELECT
        *,
        PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY momentum_20) AS momentum_rank,
        1.0 - PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY variance_20) AS stability_rank,
        PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY mean_amount_20) AS liquidity_rank
    FROM metrics
), scored AS (
    SELECT
        *,
        0.50 * momentum_rank + 0.30 * stability_rank + 0.20 * liquidity_rank AS bar_score
    FROM component_ranks
), ranked AS (
    SELECT
        *,
        PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY bar_score) AS score_rank
    FROM scored
)
SELECT
    trade_date,
    COUNT(*) AS population,
    SUM(CASE WHEN score_rank >= 0.90 THEN 1 ELSE 0 END) AS selected,
    AVG(CASE WHEN score_rank >= 0.90 THEN return_1d - :cost_pct END) AS selected_return_1d,
    AVG(CASE WHEN score_rank >= 0.90 THEN return_5d - :cost_pct END) AS selected_return_5d,
    AVG(return_1d) AS benchmark_return_1d,
    AVG(return_5d) AS benchmark_return_5d,
    AVG(CASE WHEN score_rank >= 0.90 THEN CASE WHEN return_5d <= -8.0 THEN 1.0 ELSE 0.0 END END) AS severe_loss_rate
FROM ranked
GROUP BY trade_date
HAVING COUNT(*) >= 30 AND selected >= 3
ORDER BY trade_date
"""

_TOMORROW_HISTORICAL_QUERY = """
WITH lagged AS (
    SELECT
        screening_price.code,
        screening_price.trade_date,
        screening_universe.board,
        screening_price.close,
        screening_price.high,
        screening_price.low,
        screening_price.amount,
        screening_price.change_pct / 100.0 AS daily_return,
        LAG(screening_price.close, 1) OVER code_dates AS close_1,
        LAG(screening_price.close, 3) OVER code_dates AS close_3,
        LAG(screening_price.close, 5) OVER code_dates AS close_5,
        LAG(screening_price.close, 20) OVER code_dates AS close_20,
        LAG(screening_price.close, 40) OVER code_dates AS close_40,
        LAG(screening_price.close, 60) OVER code_dates AS close_60,
        LEAD(screening_price.close, 1) OVER code_dates AS next_close,
        LEAD(screening_price.low, 1) OVER code_dates AS next_low,
        COUNT(*) OVER (PARTITION BY screening_price.code ORDER BY screening_price.trade_date ROWS BETWEEN 60 PRECEDING AND CURRENT ROW)
            AS history_count
    FROM screening_price
    JOIN screening_universe
      ON screening_universe.research_id = screening_price.research_id AND screening_universe.code = screening_price.code
    WHERE screening_price.research_id = :identity
    WINDOW code_dates AS (PARTITION BY screening_price.code ORDER BY screening_price.trade_date)
), rolling AS (
    SELECT
        *,
        AVG(daily_return) OVER recent_20 AS mean_return_20,
        AVG(daily_return * daily_return) OVER recent_20 AS mean_square_return_20,
        AVG(CASE WHEN daily_return < 0.0 THEN daily_return * daily_return ELSE 0.0 END)
            OVER recent_20 AS downside_semivariance_20,
        AVG(amount) OVER recent_20 AS average_amount_20,
        AVG(ABS(daily_return) / MAX(amount / 1000000.0, 0.000000001)) OVER recent_20 AS amihud_20,
        MAX(high) OVER recent_60 AS maximum_high_60,
        AVG(MAX(high - low, ABS(high - close_1), ABS(low - close_1)))
            OVER recent_20 AS atr_20
    FROM lagged
    WINDOW
        recent_20 AS (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
        recent_60 AS (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
), metrics AS (
    SELECT
        trade_date,
        code,
        board,
        close / close_1 - 1.0 AS return_1,
        close / close_3 - 1.0 AS return_3,
        close / close_5 - 1.0 AS return_5,
        close_5 / close_20 - 1.0 AS momentum_20_skip5,
        close_5 / close_40 - 1.0 AS momentum_40_skip5,
        close_5 / close_60 - 1.0 AS momentum_60_skip5,
        MAX(0.0, mean_square_return_20 - mean_return_20 * mean_return_20) AS variance_20,
        downside_semivariance_20,
        close / maximum_high_60 AS drawdown_recovery_60,
        amihud_20,
        average_amount_20,
        close / close_20 - 1.0 AS baseline_momentum_20,
        next_close / close - 1.0 AS next_return,
        atr_20 / close AS atr20_pct,
        (next_low - close) / atr_20 AS mae_atr20
    FROM rolling
    WHERE history_count >= 61
      AND close_1 > 0.0 AND close_3 > 0.0 AND close_5 > 0.0
      AND close_20 > 0.0 AND close_40 > 0.0 AND close_60 > 0.0
      AND next_close > 0.0 AND next_low > 0.0 AND maximum_high_60 > 0.0 AND atr_20 > 0.0
      AND average_amount_20 > 0.0
      AND board IN ('main', 'chinext', 'star')
      AND trade_date BETWEEN :start AND :end
), ranked AS (
    SELECT
        *,
        AVG(next_return) OVER (PARTITION BY trade_date) AS market_next_return,
        PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY baseline_momentum_20) AS momentum_rank,
        1.0 - PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY variance_20) AS stability_rank,
        PERCENT_RANK() OVER (PARTITION BY trade_date ORDER BY average_amount_20) AS liquidity_rank
    FROM metrics
)
SELECT
    trade_date,
    code,
    board,
    return_1,
    return_3,
    return_5,
    momentum_20_skip5,
    momentum_40_skip5,
    momentum_60_skip5,
    variance_20,
    downside_semivariance_20,
    drawdown_recovery_60,
    amihud_20,
    average_amount_20,
    100.0 * (0.50 * momentum_rank + 0.30 * stability_rank + 0.20 * liquidity_rank) AS baseline_score,
    next_return - market_next_return AS gross_excess_return,
    mae_atr20,
    next_return AS gross_return,
    market_next_return AS benchmark_return,
    atr20_pct
FROM ranked
ORDER BY trade_date, code
"""


def _tomorrow_historical_rows(rows: Sequence[tuple[object, ...]]) -> tuple[TomorrowHistoricalRow, ...]:
    grouped: dict[date, list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault(DecodeDate(row[0]), []).append(row)
    result: list[TomorrowHistoricalRow] = []
    for trade_date in sorted(grouped):
        daily = grouped[trade_date]
        residuals = tuple(_residualize(daily, column) for column in (6, 7, 8))
        for index, row in enumerate(daily):
            result.append(
                TomorrowHistoricalRow(
                    trade_date=trade_date,
                    code=str(row[1]),
                    board=cast(HistoricalBoard, str(row[2])),
                    alpha_features=(
                        _number(row[3]),
                        _number(row[4]),
                        _number(row[5]),
                        residuals[0][index],
                        residuals[1][index],
                        residuals[2][index],
                    ),
                    realized_volatility_20d=math.sqrt(_number(row[9])),
                    downside_semivariance_20d=_number(row[10]),
                    drawdown_recovery_60d=_number(row[11]),
                    amihud_20d=_number(row[12]),
                    average_amount_20d=_number(row[13]),
                    baseline_score=_number(row[14]),
                    gross_excess_return=_number(row[15]),
                    mae_atr20=_number(row[16]),
                )
            )
    return tuple(result)


def _tomorrow_historical_day_rows(rows: Sequence[tuple[object, ...]]) -> tuple[TomorrowHistoricalRow, ...]:
    if not rows or len({str(row[0]) for row in rows}) != 1:
        raise ValueError("Tomorrow historical streaming rows must contain exactly one trade date")
    return _tomorrow_historical_rows(tuple(tuple(row) for row in rows))


def _residualize(rows: Sequence[tuple[object, ...]], momentum_column: int) -> tuple[float, ...]:
    market_mean = math.fsum(_number(row[momentum_column]) for row in rows) / len(rows)
    boards: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        boards.setdefault(str(row[2]), []).append(index)
    centered = [0.0] * len(rows)
    amount_centered = [0.0] * len(rows)
    for indices in boards.values():
        board_mean = math.fsum(_number(rows[index][momentum_column]) - market_mean for index in indices) / len(indices)
        amounts = tuple(math.log(_number(rows[index][13])) for index in indices)
        amount_mean = math.fsum(amounts) / len(amounts)
        for index, amount in zip(indices, amounts, strict=True):
            centered[index] = _number(rows[index][momentum_column]) - market_mean - board_mean
            amount_centered[index] = amount - amount_mean
    denominator = math.fsum(value * value for value in amount_centered)
    slope = (
        math.fsum(value * amount for value, amount in zip(centered, amount_centered, strict=True)) / denominator
        if denominator > 0.0
        else 0.0
    )
    return tuple(value - slope * amount for value, amount in zip(centered, amount_centered, strict=True))


def _number(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise TypeError("historical snapshot numeric column is invalid")
    return float(value)


__all__ = [
    "HistoricalScreeningSnapshotConflictError",
    "HistoricalScreeningSnapshotStatus",
    "SQLiteHistoricalScreeningSnapshot",
]

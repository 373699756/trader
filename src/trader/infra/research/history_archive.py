"""Immutable SQLite archive for downloadable historical screening bars."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import cast

from trader.application.research.historical_backtest import HistoricalScreeningDay
from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
    HistoricalHistoryIdentity,
    HistoricalSecurity,
    ResearchBoard,
)
from trader.application.research.score_r6_daily_models import ScoreR6DailyRow
from trader.application.research.score_r6_models import ScoreR6Board, ScoreR6HistoricalRow
from trader.domain.research.historical_screening import HistoricalPriceBar, HistoricalScreeningSpec


class HistoricalArchiveConflictError(RuntimeError):
    pass


class SQLiteHistoricalArchive:
    def __init__(self, runtime_dir: Path) -> None:
        self._root = runtime_dir / "score-history"
        self._database = self._root / "score-history.sqlite3"
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
        with self._write_connection() as connection:
            self._register_spec(connection, spec)
            existing_codes = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT code FROM universe WHERE research_identity = ? ORDER BY code",
                    (spec.research_identity,),
                ).fetchall()
            )
            requested_codes = tuple(item.code for item in ordered)
            if existing_codes and existing_codes != requested_codes:
                raise HistoricalArchiveConflictError("historical screening universe set conflict")
            for item in ordered:
                payload = _canonical(asdict(item))
                existing = connection.execute(
                    "SELECT payload_hash FROM universe WHERE research_identity = ? AND code = ?",
                    (spec.research_identity, item.code),
                ).fetchone()
                if existing is not None and str(existing[0]) != _sha256(payload):
                    raise HistoricalArchiveConflictError("historical screening universe identity conflict")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO universe(
                        research_identity, code, board, name, is_st, is_suspended, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.research_identity,
                        item.code,
                        item.board,
                        item.name,
                        int(item.is_st),
                        int(item.is_suspended),
                        _sha256(payload),
                    ),
                )

    def completed_codes(self, research_identity: str) -> frozenset[str]:
        if not self._database.is_file():
            return frozenset()
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT code FROM downloads WHERE research_identity = ? AND status = 'complete'",
                (research_identity,),
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def registered_universe(self, research_identity: str) -> tuple[HistoricalSecurity, ...]:
        if not self._database.is_file():
            return ()
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT code, board, name, is_st, is_suspended
                FROM universe WHERE research_identity = ? ORDER BY code
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
        with self._write_connection() as connection:
            self._register_spec(connection, spec)
            if (
                connection.execute(
                    "SELECT 1 FROM universe WHERE research_identity = ? AND code = ?",
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
                existing = connection.execute(
                    """
                    SELECT payload_hash FROM bars
                    WHERE research_identity = ? AND code = ? AND trade_date = ?
                    """,
                    (spec.research_identity, code, bar.trade_date.isoformat()),
                ).fetchone()
                if existing is not None and str(existing[0]) != payload_hash:
                    raise HistoricalArchiveConflictError("historical screening bar identity conflict")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO bars(
                        research_identity, code, trade_date, open_price, close_price, high_price, low_price,
                        volume, amount, pct_change, turnover_rate, adjustment, source, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.research_identity,
                        code,
                        bar.trade_date.isoformat(),
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
                        payload_hash,
                    ),
                )
            content_hash = _sha256(_canonical({"code": code, "bars": hashes}))
            existing_download = connection.execute(
                """
                SELECT content_hash FROM downloads
                WHERE research_identity = ? AND code = ? AND status = 'complete'
                """,
                (spec.research_identity, code),
            ).fetchone()
            if existing_download is not None and str(existing_download[0]) != content_hash:
                raise HistoricalArchiveConflictError("historical screening download identity conflict")
            connection.execute(
                """
                INSERT INTO downloads(research_identity, code, status, bar_count, content_hash, error_code)
                VALUES (?, ?, 'complete', ?, ?, '')
                ON CONFLICT(research_identity, code) DO UPDATE SET
                    status = excluded.status,
                    bar_count = excluded.bar_count,
                    content_hash = excluded.content_hash,
                    error_code = excluded.error_code
                """,
                (spec.research_identity, code, len(ordered), content_hash),
            )

    def record_failure(self, spec: HistoricalScreeningSpec, code: str, error_code: str) -> None:
        if not error_code or len(error_code) > 64:
            raise ValueError("historical screening failure code is invalid")
        self._initialize()
        with self._write_connection() as connection:
            self._register_spec(connection, spec)
            connection.execute(
                """
                INSERT INTO downloads(research_identity, code, status, bar_count, content_hash, error_code)
                VALUES (?, ?, 'failed', 0, '', ?)
                ON CONFLICT(research_identity, code) DO UPDATE SET
                    status = CASE WHEN downloads.status = 'complete' THEN downloads.status ELSE excluded.status END,
                    error_code = CASE
                        WHEN downloads.status = 'complete' THEN downloads.error_code ELSE excluded.error_code
                    END
                """,
                (spec.research_identity, code, error_code),
            )

    def inspect(self, research_identity: str) -> HistoricalArchiveStatus:
        if not self._database.is_file():
            return HistoricalArchiveStatus(research_identity=research_identity)
        try:
            with self._read_connection() as connection:
                spec = connection.execute(
                    "SELECT spec_hash FROM specs WHERE research_identity = ?", (research_identity,)
                ).fetchone()
                universe_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM universe WHERE research_identity = ?", (research_identity,)
                    ).fetchone()[0]
                )
                completed, failed = connection.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                    FROM downloads WHERE research_identity = ?
                    """,
                    (research_identity,),
                ).fetchone()
                bar_count, first_date, last_date = connection.execute(
                    """
                    SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
                    FROM bars WHERE research_identity = ?
                    """,
                    (research_identity,),
                ).fetchone()
        except sqlite3.DatabaseError:
            return HistoricalArchiveStatus(initialized=True, research_identity=research_identity)
        return HistoricalArchiveStatus(
            initialized=True,
            research_identity=research_identity,
            universe_count=universe_count,
            completed_codes=int(completed or 0),
            failed_codes=int(failed or 0),
            bar_count=int(bar_count or 0),
            first_trade_date=str(first_date) if first_date is not None else None,
            last_trade_date=str(last_date) if last_date is not None else None,
            spec_hash=str(spec[0]) if spec is not None else "",
        )

    def manifest(self, spec: HistoricalScreeningSpec) -> HistoricalArchiveManifest:
        empty_hash = _sha256(_canonical(()))
        if not self._database.is_file():
            return HistoricalArchiveManifest(spec.research_identity, "", empty_hash, empty_hash, ())
        with self._read_connection() as connection:
            stored_spec = connection.execute(
                "SELECT spec_hash FROM specs WHERE research_identity = ?", (spec.research_identity,)
            ).fetchone()
            if stored_spec is None or str(stored_spec[0]) != spec.content_hash:
                raise HistoricalArchiveConflictError("historical screening spec manifest conflict")
            universe_rows = connection.execute(
                """
                SELECT code, board, name, is_st, is_suspended, payload_hash
                FROM universe WHERE research_identity = ? ORDER BY code
                """,
                (spec.research_identity,),
            ).fetchall()
            universe_identities: list[tuple[str, str]] = []
            for code, board, name, is_st, is_suspended, stored_hash in universe_rows:
                security = HistoricalSecurity(
                    str(code),
                    cast(ResearchBoard, str(board)),
                    str(name),
                    bool(is_st),
                    bool(is_suspended),
                )
                payload_hash = _sha256(_canonical(asdict(security)))
                if payload_hash != str(stored_hash):
                    raise HistoricalArchiveConflictError("historical screening universe payload conflict")
                universe_identities.append((security.code, payload_hash))
            downloads = {
                str(code): (int(bar_count), str(content_hash))
                for code, bar_count, content_hash in connection.execute(
                    """
                    SELECT code, bar_count, content_hash FROM downloads
                    WHERE research_identity = ? AND status = 'complete' ORDER BY code
                    """,
                    (spec.research_identity,),
                ).fetchall()
            }
            bar_rows = connection.execute(
                """
                SELECT code, trade_date, open_price, close_price, high_price, low_price, volume, amount,
                       pct_change, turnover_rate, adjustment, source, payload_hash
                FROM bars WHERE research_identity = ? ORDER BY code, trade_date
                """,
                (spec.research_identity,),
            )
            bar_hashes: dict[str, list[str]] = {}
            for row in bar_rows:
                code = str(row[0])
                payload = {
                    "trade_date": str(row[1]),
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
                if payload_hash != str(row[12]):
                    raise HistoricalArchiveConflictError("historical screening bar payload conflict")
                bar_hashes.setdefault(code, []).append(payload_hash)
        if set(bar_hashes) != set(downloads):
            raise HistoricalArchiveConflictError("historical screening completed history set conflict")
        histories: list[HistoricalHistoryIdentity] = []
        for code in sorted(downloads):
            stored_count, stored_hash = downloads[code]
            hashes = bar_hashes[code]
            content_hash = _sha256(_canonical({"code": code, "bars": hashes}))
            if stored_count != len(hashes) or stored_hash != content_hash:
                raise HistoricalArchiveConflictError("historical screening history content conflict")
            histories.append(HistoricalHistoryIdentity(code, len(hashes), content_hash))
        universe_hash = _sha256(_canonical({"securities": universe_identities}))
        histories_hash = _sha256(_canonical({"histories": [asdict(item) for item in histories]}))
        return HistoricalArchiveManifest(
            spec.research_identity,
            spec.content_hash,
            universe_hash,
            histories_hash,
            tuple(histories),
        )

    def screening_days(self, spec: HistoricalScreeningSpec) -> tuple[HistoricalScreeningDay, ...]:
        """Return fixed OHLCV-only daily diagnostics without mutating the archive."""

        if not self._database.is_file():
            return ()
        with self._read_connection() as connection:
            rows = connection.execute(
                _SCREENING_QUERY,
                {
                    "identity": spec.research_identity,
                    "start": spec.training_start.isoformat(),
                    "end": spec.validation_end.isoformat(),
                    "cost_pct": spec.round_trip_cost_bps / 100.0,
                },
            ).fetchall()
        return tuple(
            HistoricalScreeningDay(
                trade_date=date.fromisoformat(str(row[0])),
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

    def score_r6_rows(self, spec: HistoricalScreeningSpec) -> tuple[ScoreR6HistoricalRow, ...]:
        """Return point-in-time OHLCV proxy factors and future labels for Score-R6."""

        if not self._database.is_file():
            return ()
        with self._read_connection() as connection:
            rows = connection.execute(
                _SCORE_R6_QUERY,
                {
                    "identity": spec.research_identity,
                    "start": spec.training_start.isoformat(),
                    "end": spec.validation_end.isoformat(),
                },
            ).fetchall()
        return tuple(
            ScoreR6HistoricalRow(
                trade_date=date.fromisoformat(str(row[0])),
                code=str(row[1]),
                board=cast(ScoreR6Board, str(row[2])),
                momentum_score=float(row[3]),
                stability_score=float(row[4]),
                liquidity_score=float(row[5]),
                volatility_20d_pct=math.sqrt(float(row[6])),
                return_5d_pct=float(row[7]),
            )
            for row in rows
        )

    def score_r6_daily_rows(self, spec: HistoricalScreeningSpec) -> tuple[ScoreR6DailyRow, ...]:
        """Return point-in-time daily trend factors and fixed five-day labels."""

        if not self._database.is_file():
            return ()
        with self._read_connection() as connection:
            rows = connection.execute(
                _SCORE_R6_DAILY_QUERY,
                {
                    "identity": spec.research_identity,
                    "start": spec.training_start.isoformat(),
                    "end": spec.validation_end.isoformat(),
                },
            ).fetchall()
        return tuple(
            ScoreR6DailyRow(
                trade_date=date.fromisoformat(str(row[0])),
                code=str(row[1]),
                board=cast(ScoreR6Board, str(row[2])),
                momentum_20_score=float(row[3]),
                residual_momentum_score=float(row[4]),
                trend_efficiency_score=float(row[5]),
                downside_stability_score=float(row[6]),
                drawdown_recovery_score=float(row[7]),
                liquidity_score=float(row[8]),
                residual_return_60_5_pct=float(row[9]),
                recent_return_5d_pct=float(row[10]),
                close_ma20_spread_pct=float(row[11]),
                drawdown_60d_pct=float(row[12]),
                downside_volatility_20d_pct=math.sqrt(float(row[13])),
                volatility_20d_pct=math.sqrt(float(row[14])),
                return_5d_pct=float(row[15]),
            )
            for row in rows
        )

    def _initialize(self) -> None:
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._database) as connection:
                connection.executescript(_SCHEMA)

    def _register_spec(self, connection: sqlite3.Connection, spec: HistoricalScreeningSpec) -> None:
        existing = connection.execute(
            "SELECT spec_hash FROM specs WHERE research_identity = ?", (spec.research_identity,)
        ).fetchone()
        if existing is not None and str(existing[0]) != spec.content_hash:
            raise HistoricalArchiveConflictError("historical screening spec identity conflict")
        connection.execute(
            "INSERT OR IGNORE INTO specs(research_identity, spec_hash) VALUES (?, ?)",
            (spec.research_identity, spec.content_hash),
        )

    def _write_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _read_connection(self) -> sqlite3.Connection:
        uri = f"file:{self._database}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0)


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


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS specs(
    research_identity TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe(
    research_identity TEXT NOT NULL,
    code TEXT NOT NULL,
    board TEXT NOT NULL,
    name TEXT NOT NULL,
    is_st INTEGER NOT NULL,
    is_suspended INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(research_identity, code),
    FOREIGN KEY(research_identity) REFERENCES specs(research_identity)
);
CREATE TABLE IF NOT EXISTS bars(
    research_identity TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open_price REAL NOT NULL,
    close_price REAL NOT NULL,
    high_price REAL NOT NULL,
    low_price REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    pct_change REAL NOT NULL,
    turnover_rate REAL,
    adjustment TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(research_identity, code, trade_date),
    FOREIGN KEY(research_identity, code) REFERENCES universe(research_identity, code)
);
CREATE TABLE IF NOT EXISTS downloads(
    research_identity TEXT NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete', 'failed')),
    bar_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    error_code TEXT NOT NULL,
    PRIMARY KEY(research_identity, code)
);
CREATE INDEX IF NOT EXISTS bars_identity_date ON bars(research_identity, trade_date);
"""

_SCREENING_QUERY = """
WITH ordered AS (
    SELECT
        code,
        trade_date,
        close_price,
        amount,
        pct_change,
        LAG(close_price, 20) OVER (PARTITION BY code ORDER BY trade_date) AS close_20,
        LEAD(close_price, 1) OVER (PARTITION BY code ORDER BY trade_date) AS close_f1,
        LEAD(close_price, 5) OVER (PARTITION BY code ORDER BY trade_date) AS close_f5,
        COUNT(*) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 60 PRECEDING AND CURRENT ROW
        ) AS history_count,
        AVG(pct_change) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_return_20,
        AVG(pct_change * pct_change) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_square_return_20,
        AVG(amount) OVER (
            PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_amount_20
    FROM bars
    WHERE research_identity = :identity
), metrics AS (
    SELECT
        code,
        trade_date,
        100.0 * (close_price / close_20 - 1.0) AS momentum_20,
        MAX(0.0, mean_square_return_20 - mean_return_20 * mean_return_20) AS variance_20,
        mean_amount_20,
        100.0 * (close_f1 / close_price - 1.0) AS return_1d,
        100.0 * (close_f5 / close_price - 1.0) AS return_5d
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

_SCORE_R6_QUERY = """
WITH ordered AS (
    SELECT
        bars.code,
        bars.trade_date,
        universe.board,
        bars.close_price,
        LAG(bars.close_price, 20) OVER (PARTITION BY bars.code ORDER BY bars.trade_date) AS close_20,
        LEAD(bars.close_price, 5) OVER (PARTITION BY bars.code ORDER BY bars.trade_date) AS close_f5,
        COUNT(*) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 60 PRECEDING AND CURRENT ROW
        ) AS history_count,
        AVG(bars.pct_change) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_return_20,
        AVG(bars.pct_change * bars.pct_change) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_square_return_20,
        AVG(bars.amount) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_amount_20
    FROM bars
    JOIN universe
      ON universe.research_identity = bars.research_identity AND universe.code = bars.code
    WHERE bars.research_identity = :identity
), metrics AS (
    SELECT
        code,
        trade_date,
        board,
        100.0 * (close_price / close_20 - 1.0) AS momentum_20,
        MAX(0.0, mean_square_return_20 - mean_return_20 * mean_return_20) AS variance_20,
        mean_amount_20,
        100.0 * (close_f5 / close_price - 1.0) AS return_5d
    FROM ordered
    WHERE history_count >= 61
      AND close_20 > 0.0
      AND close_f5 > 0.0
      AND trade_date BETWEEN :start AND :end
), ranked AS (
    SELECT
        *,
        100.0 * PERCENT_RANK() OVER (PARTITION BY trade_date, board ORDER BY momentum_20) AS momentum_score,
        100.0 * (1.0 - PERCENT_RANK() OVER (PARTITION BY trade_date, board ORDER BY variance_20)) AS stability_score,
        100.0 * PERCENT_RANK() OVER (PARTITION BY trade_date, board ORDER BY mean_amount_20) AS liquidity_score
    FROM metrics
)
SELECT
    trade_date,
    code,
    board,
    momentum_score,
    stability_score,
    liquidity_score,
    variance_20,
    return_5d
FROM ranked
ORDER BY trade_date, code
"""

_SCORE_R6_DAILY_QUERY = """
WITH ordered AS (
    SELECT
        bars.code,
        bars.trade_date,
        universe.board,
        bars.close_price,
        LAG(bars.close_price, 5) OVER (PARTITION BY bars.code ORDER BY bars.trade_date) AS close_5,
        LAG(bars.close_price, 20) OVER (PARTITION BY bars.code ORDER BY bars.trade_date) AS close_20,
        LAG(bars.close_price, 60) OVER (PARTITION BY bars.code ORDER BY bars.trade_date) AS close_60,
        LEAD(bars.close_price, 5) OVER (PARTITION BY bars.code ORDER BY bars.trade_date) AS close_f5,
        COUNT(*) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 60 PRECEDING AND CURRENT ROW
        ) AS history_count,
        AVG(bars.close_price) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_close_20,
        MAX(bars.high_price) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
        ) AS high_60,
        SUM(ABS(bars.pct_change)) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 59 PRECEDING AND 5 PRECEDING
        ) AS path_60_5,
        AVG(bars.pct_change) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_return_20,
        AVG(bars.pct_change * bars.pct_change) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_square_return_20,
        AVG(CASE WHEN bars.pct_change < 0.0 THEN bars.pct_change * bars.pct_change ELSE 0.0 END) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS downside_square_return_20,
        AVG(bars.amount) OVER (
            PARTITION BY bars.code ORDER BY bars.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS mean_amount_20
    FROM bars
    JOIN universe
      ON universe.research_identity = bars.research_identity AND universe.code = bars.code
    WHERE bars.research_identity = :identity
), metrics AS (
    SELECT
        code,
        trade_date,
        board,
        100.0 * (close_price / close_20 - 1.0) AS momentum_20,
        100.0 * (close_5 / close_60 - 1.0) AS residual_return_60_5,
        (100.0 * (close_5 / close_60 - 1.0)) / path_60_5 AS trend_efficiency,
        MAX(0.0, downside_square_return_20) AS downside_variance_20,
        MAX(0.0, mean_square_return_20 - mean_return_20 * mean_return_20) AS variance_20,
        mean_amount_20,
        100.0 * (close_price / close_5 - 1.0) AS recent_return_5,
        100.0 * (close_price / mean_close_20 - 1.0) AS close_ma20_spread,
        100.0 * (close_price / high_60 - 1.0) AS drawdown_60,
        100.0 * (close_f5 / close_price - 1.0) AS return_5d
    FROM ordered
    WHERE history_count >= 61
      AND close_5 > 0.0
      AND close_20 > 0.0
      AND close_60 > 0.0
      AND close_f5 > 0.0
      AND mean_close_20 > 0.0
      AND high_60 > 0.0
      AND path_60_5 > 0.0
      AND trade_date BETWEEN :start AND :end
), ranked AS (
    SELECT
        *,
        100.0 * PERCENT_RANK() OVER (PARTITION BY trade_date, board ORDER BY momentum_20) AS momentum_score,
        100.0 * PERCENT_RANK() OVER (
            PARTITION BY trade_date, board ORDER BY residual_return_60_5
        ) AS residual_momentum_score,
        100.0 * PERCENT_RANK() OVER (
            PARTITION BY trade_date, board ORDER BY trend_efficiency
        ) AS trend_efficiency_score,
        100.0 * (1.0 - PERCENT_RANK() OVER (
            PARTITION BY trade_date, board ORDER BY downside_variance_20
        )) AS downside_stability_score,
        100.0 * PERCENT_RANK() OVER (PARTITION BY trade_date, board ORDER BY drawdown_60) AS drawdown_recovery_score,
        100.0 * PERCENT_RANK() OVER (PARTITION BY trade_date, board ORDER BY mean_amount_20) AS liquidity_score
    FROM metrics
)
SELECT
    trade_date,
    code,
    board,
    momentum_score,
    residual_momentum_score,
    trend_efficiency_score,
    downside_stability_score,
    drawdown_recovery_score,
    liquidity_score,
    residual_return_60_5,
    recent_return_5,
    close_ma20_spread,
    drawdown_60,
    downside_variance_20,
    variance_20,
    return_5d
FROM ranked
ORDER BY trade_date, code
"""


__all__ = [
    "HistoricalArchiveConflictError",
    "HistoricalArchiveStatus",
    "SQLiteHistoricalArchive",
]

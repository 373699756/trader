"""Read-only legacy history seeds and a bounded restart-safe runtime cache."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from trader.infra.market_data.history import DailyBar, PriceAdjustment


class DailyHistoryClient(Protocol):
    def fetch_history(self, code: str, *, days: int = 90) -> Sequence[DailyBar]: ...


class FallbackHistoryClient:
    """Prefer the complete Tencent daily feed and retain Eastmoney as fallback."""

    def __init__(
        self,
        primary: DailyHistoryClient,
        fallback: DailyHistoryClient,
        *,
        minimum_rows: int = 20,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._minimum_rows = max(1, minimum_rows)

    def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
        primary = _safe_history(self._primary, code, days)
        if len(primary) >= self._minimum_rows:
            return primary
        fallback = _safe_history(self._fallback, code, days)
        return fallback if len(fallback) >= len(primary) else primary


class LocalHistorySeedClient:
    """Serve sufficient qfq bars from the legacy runtime DB without mutating it."""

    def __init__(self, path: Path, remote: DailyHistoryClient, *, minimum_rows: int = 20) -> None:
        self._path = path
        self._remote = remote
        self._minimum_rows = max(1, minimum_rows)

    def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
        seeded = self._read_seed(code, days=days)
        if len(seeded) >= self._minimum_rows:
            return seeded
        try:
            remote = tuple(self._remote.fetch_history(code, days=days))
        except Exception:
            remote = ()
        return remote if len(remote) >= len(seeded) else seeded

    def available_codes(self, codes: Sequence[str]) -> tuple[str, ...]:
        """Return requested codes with enough locally seeded rows, preserving order."""
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized or not self._path.is_file():
            return ()
        placeholders = ",".join("?" for _ in normalized)
        uri = self._path.resolve().as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                rows = connection.execute(
                    f"""
                    SELECT code
                    FROM daily_bars
                    WHERE code IN ({placeholders})
                    GROUP BY code
                    HAVING COUNT(*) >= ?
                    """,  # noqa: S608 - placeholders are generated, values stay parameterized.
                    (*normalized, self._minimum_rows),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return ()
        available = {str(row[0]) for row in rows}
        return tuple(code for code in normalized if code in available)

    def _read_seed(self, code: str, *, days: int) -> tuple[DailyBar, ...]:
        if len(code) != 6 or not code.isdigit() or not self._path.is_file():
            return ()
        uri = self._path.resolve().as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                rows = connection.execute(
                    """
                    SELECT trade_date, qfq_open, qfq_close, qfq_high, qfq_low,
                           volume, turnover, pct_chg
                    FROM daily_bars
                    WHERE code = ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    (code, max(1, days)),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return ()
        bars: list[DailyBar] = []
        for row in reversed(rows):
            try:
                trade_date, open_price, close, high, low, volume, amount, pct_change = row
                trade_date_text = str(trade_date)
                if len(trade_date_text) == 8 and trade_date_text.isdigit():
                    trade_date_text = datetime.strptime(trade_date_text, "%Y%m%d").date().isoformat()
                bars.append(
                    DailyBar(
                        trade_date_text,
                        float(open_price),
                        float(close),
                        float(high),
                        float(low),
                        float(volume) * 100.0,
                        float(amount),
                        float(pct_change),
                        adjustment=PriceAdjustment.QFQ,
                        source="local_seed",
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(bars)


@dataclass(frozen=True)
class RuntimeHistoryCachePolicy:
    minimum_rows: int = 20
    capacity: int = 360
    freshness_seconds: float = 21_600


class RuntimeHistoryCacheClient:
    """Persist fresh qfq warmup rows without mutating the legacy seed."""

    def __init__(
        self,
        path: Path,
        remote: DailyHistoryClient,
        *,
        policy: RuntimeHistoryCachePolicy | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        effective_policy = policy or RuntimeHistoryCachePolicy()
        self._path = path
        self._remote = remote
        self._minimum_rows = max(1, effective_policy.minimum_rows)
        self._capacity = max(1, effective_policy.capacity)
        self._freshness_seconds = max(60.0, effective_policy.freshness_seconds)
        self._wall_clock = wall_clock
        self._write_lock = threading.Lock()

    def fetch_history(self, code: str, *, days: int = 90) -> tuple[DailyBar, ...]:
        cached, updated_at = self._read(code, days=days)
        if len(cached) >= self._minimum_rows and self._is_fresh(updated_at):
            return cached
        remote = _safe_history(self._remote, code, days)
        if any(bar.adjustment is not PriceAdjustment.QFQ for bar in remote):
            remote = ()
        if len(remote) >= self._minimum_rows:
            self._persist(code, remote)
        return remote if len(remote) >= len(cached) else cached

    def available_codes(self, codes: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized or not self._path.is_file():
            return ()
        placeholders = ",".join("?" for _ in normalized)
        try:
            with sqlite3.connect(self._read_uri(), uri=True, timeout=1.0) as connection:
                rows = connection.execute(
                    f"""
                    SELECT code
                    FROM daily_history
                    WHERE code IN ({placeholders})
                    GROUP BY code
                    HAVING COUNT(*) >= ?
                    """,  # noqa: S608 - placeholders are generated, values stay parameterized.
                    (*normalized, self._minimum_rows),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return ()
        available = {str(row[0]) for row in rows}
        return tuple(code for code in normalized if code in available)

    def _read(self, code: str, *, days: int) -> tuple[tuple[DailyBar, ...], datetime | None]:
        if len(code) != 6 or not code.isdigit() or not self._path.is_file():
            return (), None
        try:
            with sqlite3.connect(self._read_uri(), uri=True, timeout=1.0) as connection:
                metadata = connection.execute(
                    "SELECT updated_at FROM history_cache_codes WHERE code = ?",
                    (code,),
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT trade_date, open_price, close_price, high_price, low_price,
                           volume, amount, pct_change, turnover_rate, source
                    FROM daily_history
                    WHERE code = ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    (code, max(1, days)),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return (), None
        bars: list[DailyBar] = []
        for row in reversed(rows):
            try:
                (
                    trade_date,
                    open_price,
                    close,
                    high,
                    low,
                    volume,
                    amount,
                    pct_change,
                    turnover_rate,
                    source,
                ) = row
                bars.append(
                    DailyBar(
                        str(trade_date),
                        float(open_price),
                        float(close),
                        float(high),
                        float(low),
                        float(volume),
                        float(amount),
                        float(pct_change),
                        float(turnover_rate) if turnover_rate is not None else None,
                        adjustment=PriceAdjustment.QFQ,
                        source=str(source),
                    )
                )
            except (TypeError, ValueError):
                continue
        try:
            updated_at = datetime.fromisoformat(str(metadata[0])) if metadata is not None else None
        except ValueError:
            updated_at = None
        return tuple(bars), updated_at

    def _is_fresh(self, updated_at: datetime | None) -> bool:
        if updated_at is None or updated_at.tzinfo is None:
            return False
        age = (self._wall_clock().astimezone(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
        return 0.0 <= age <= self._freshness_seconds

    def _persist(self, code: str, bars: Sequence[DailyBar]) -> None:
        try:
            with self._write_lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(self._path, timeout=5.0) as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA busy_timeout=5000")
                    _initialize_runtime_history_cache(connection)
                    connection.execute("DELETE FROM daily_history WHERE code = ?", (code,))
                    connection.executemany(
                        """
                        INSERT INTO daily_history (
                            code, trade_date, open_price, close_price, high_price, low_price,
                            volume, amount, pct_change, turnover_rate, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                code,
                                bar.trade_date,
                                bar.open_price,
                                bar.close,
                                bar.high,
                                bar.low,
                                bar.volume,
                                bar.amount,
                                bar.pct_change,
                                bar.turnover_rate,
                                bar.source,
                            )
                            for bar in bars
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO history_cache_codes (code, updated_at)
                        VALUES (?, ?)
                        ON CONFLICT(code) DO UPDATE SET updated_at = excluded.updated_at
                        """,
                        (code, self._wall_clock().isoformat()),
                    )
                    overflow = connection.execute(
                        """
                        SELECT code
                        FROM history_cache_codes
                        ORDER BY updated_at DESC, code
                        LIMIT -1 OFFSET ?
                        """,
                        (self._capacity,),
                    ).fetchall()
                    for (stale_code,) in overflow:
                        connection.execute("DELETE FROM daily_history WHERE code = ?", (stale_code,))
                        connection.execute("DELETE FROM history_cache_codes WHERE code = ?", (stale_code,))
        except (OSError, sqlite3.Error):
            return

    def _read_uri(self) -> str:
        return self._path.resolve().as_uri() + "?mode=ro"


def _initialize_runtime_history_cache(connection: sqlite3.Connection) -> None:
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version not in {0, 1}:
        raise sqlite3.OperationalError(f"unsupported history cache schema version: {schema_version}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS history_cache_codes (
            code TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("PRAGMA user_version=1")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_history (
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
            source TEXT NOT NULL,
            PRIMARY KEY (code, trade_date)
        )
        """
    )


def _safe_history(client: DailyHistoryClient, code: str, days: int) -> tuple[DailyBar, ...]:
    try:
        return tuple(client.fetch_history(code, days=days))
    except Exception:
        return ()

"""Read-only last-valid history seed with a remote refresh fallback."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from trader.infrastructure.market_data.history import DailyBar


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
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(bars)


def _safe_history(client: DailyHistoryClient, code: str, days: int) -> tuple[DailyBar, ...]:
    try:
        return tuple(client.fetch_history(code, days=days))
    except Exception:
        return ()

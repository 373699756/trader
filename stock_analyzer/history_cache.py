import os
from datetime import datetime, timedelta
import pandas as pd

from .normalization import coerce_number, normalize_code, rename_known_columns
from .sqlite_support import sqlite_transaction


_connect_history_db = sqlite_transaction


class HistoryCache:
    def __init__(self, db_path: str, freshness_hours: int = 18) -> None:
        raw_path = os.path.expandvars(os.path.expanduser(str(db_path or "").strip()))
        if not raw_path:
            raise ValueError("history cache database path is empty")
        # sqlite resolves relative paths on every connect; keep the path stable if cwd changes later.
        self.db_path = os.path.abspath(raw_path)
        self.freshness_hours = freshness_hours
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def get(self, code: str, days: int) -> pd.DataFrame:
        code = normalize_code(code)
        with _connect_history_db(self.db_path) as conn:
            df = pd.read_sql_query(
                """
                SELECT trade_date, code, open, high, low, price, turnover, volume
                FROM daily_history
                WHERE code = ?
                ORDER BY trade_date ASC
                """,
                conn,
                params=(code,),
            )
        if df.empty:
            return df
        return df.tail(days).reset_index(drop=True)

    def is_fresh(self, code: str) -> bool:
        code = normalize_code(code)
        with _connect_history_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM daily_history WHERE code = ?",
                (code,),
            ).fetchone()
        if not row or not row[0]:
            return False
        try:
            updated_at = datetime.fromisoformat(row[0])
        except ValueError:
            return False
        return datetime.now() - updated_at <= timedelta(hours=self.freshness_hours)

    def set(self, code: str, history: pd.DataFrame) -> None:
        if history is None or history.empty:
            return
        df = _normalize_history_frame(code, history)
        if df.empty:
            return
        updated_at = datetime.now().isoformat(timespec="seconds")
        rows = [
            (
                row["trade_date"],
                row["code"],
                row["open"],
                row["high"],
                row["low"],
                row["price"],
                row["turnover"],
                row["volume"],
                updated_at,
            )
            for _, row in df.iterrows()
        ]
        with _connect_history_db(self.db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO daily_history
                (trade_date, code, open, high, low, price, turnover, volume, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _init_db(self) -> None:
        with _connect_history_db(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_history (
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    open REAL NOT NULL DEFAULT 0,
                    high REAL NOT NULL DEFAULT 0,
                    low REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    turnover REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, code)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_history_code ON daily_history(code)")


def _normalize_history_frame(code: str, history: pd.DataFrame) -> pd.DataFrame:
    df = rename_known_columns(history.copy())
    if "trade_date" not in df.columns:
        for candidate in ("日期", "date", "Date", "交易日期"):
            if candidate in df.columns:
                df["trade_date"] = df[candidate]
                break
    if "trade_date" not in df.columns:
        return pd.DataFrame()
    if "code" not in df.columns:
        df["code"] = code
    if "price" not in df.columns:
        df["price"] = 0.0
    for column in ("open", "high", "low", "price", "turnover", "volume"):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    df["code"] = df["code"].map(normalize_code)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    return df[["trade_date", "code", "open", "high", "low", "price", "turnover", "volume"]]

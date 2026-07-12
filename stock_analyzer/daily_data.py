import math
import os
import sqlite3
from datetime import datetime
from glob import glob
from typing import Dict, Iterable, List, Optional

import pandas as pd

from .normalization import coerce_number, market_type, normalize_code, rename_known_columns
from .sqlite_support import sqlite_transaction


_connect_market_db = sqlite_transaction


DAILY_BAR_COLUMNS = (
    "trade_date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "adj_factor",
    "qfq_open",
    "qfq_high",
    "qfq_low",
    "qfq_close",
    "pct_chg",
)


class DailyMarketDataStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.sharded = not _is_sqlite_file(db_path)
        if self.sharded:
            os.makedirs(db_path, exist_ok=True)
            self.meta_db_path = os.path.join(db_path, "market_data_meta.sqlite3")
        else:
            directory = os.path.dirname(db_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self.meta_db_path = db_path
        self._init_db()

    def latest_trade_date(self, code: str) -> str:
        code = normalize_code(code)
        db_path = self._bar_db_path_for_code(code)
        if not os.path.exists(db_path):
            return ""
        with _connect_market_db(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM daily_bars WHERE code = ?",
                (code,),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def upsert_stock_meta(self, rows: Iterable[Dict[str, object]]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        payload = []
        for row in rows:
            code = normalize_code(row.get("code"))
            if not code:
                continue
            payload.append(
                (
                    code,
                    str(row.get("name", "")),
                    str(row.get("market") or market_type(code)),
                    int(bool(row.get("is_active", True))),
                    now,
                )
            )
        if not payload:
            return 0
        with _connect_market_db(self.meta_db_path) as conn:
            conn.executemany(
                """
                INSERT INTO stock_meta (code, name, market, is_active, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    market = excluded.market,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
        return len(payload)

    def upsert_bars(
        self,
        code: str,
        raw_history: pd.DataFrame,
        qfq_history: pd.DataFrame,
    ) -> int:
        bars = build_daily_bars(code, raw_history, qfq_history)
        if bars.empty:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for _, row in bars.iterrows():
            rows.append(tuple(row[column] for column in DAILY_BAR_COLUMNS) + (now,))
        db_path = self._bar_db_path_for_code(code)
        self._init_bar_db(db_path)
        with _connect_market_db(db_path) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO daily_bars
                (trade_date, code, open, high, low, close, volume, turnover, adj_factor,
                 qfq_open, qfq_high, qfq_low, qfq_close, pct_chg, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def record_status(
        self,
        code: str,
        name: str,
        status: str,
        last_trade_date: str = "",
        row_count: int = 0,
        error: str = "",
    ) -> None:
        code = normalize_code(code)
        now = datetime.now().isoformat(timespec="seconds")
        with _connect_market_db(self.meta_db_path) as conn:
            existing = conn.execute(
                "SELECT attempts FROM download_status WHERE code = ?",
                (code,),
            ).fetchone()
            attempts = int(existing[0]) + 1 if existing else 1
            conn.execute(
                """
                INSERT INTO download_status
                (code, name, market, last_trade_date, row_count, status, error, attempts, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    market = excluded.market,
                    last_trade_date = excluded.last_trade_date,
                    row_count = excluded.row_count,
                    status = excluded.status,
                    error = excluded.error,
                    attempts = excluded.attempts,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    name,
                    market_type(code),
                    last_trade_date,
                    int(row_count),
                    status,
                    error[-1000:],
                    attempts,
                    now,
                ),
            )

    def summary(self) -> Dict[str, object]:
        bar_count = 0
        codes = set()
        date_start = ""
        date_end = ""
        shard_count = 0
        for db_path in self._bar_db_paths():
            with _connect_market_db(db_path) as conn:
                shard_bars = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
                if shard_bars <= 0:
                    continue
                shard_count += 1
                bar_count += int(shard_bars)
                codes.update(
                    row[0]
                    for row in conn.execute("SELECT DISTINCT code FROM daily_bars").fetchall()
                    if row and row[0]
                )
                date_range = conn.execute(
                    "SELECT MIN(trade_date), MAX(trade_date) FROM daily_bars"
                ).fetchone()
                if date_range:
                    if date_range[0] and (not date_start or date_range[0] < date_start):
                        date_start = date_range[0]
                    if date_range[1] and (not date_end or date_range[1] > date_end):
                        date_end = date_range[1]
        with _connect_market_db(self.meta_db_path) as conn:
            status_rows = conn.execute(
                """
                SELECT status, COUNT(*)
                FROM download_status
                GROUP BY status
                """
            ).fetchall()
        return {
            "db_path": self.db_path,
            "sharded": self.sharded,
            "shard_count": shard_count,
            "bar_count": int(bar_count),
            "stock_count": len(codes),
            "date_start": date_start,
            "date_end": date_end,
            "status": {row[0]: int(row[1]) for row in status_rows},
        }

    def _init_db(self) -> None:
        if not self.sharded:
            self._init_bar_db(self.db_path)
        with _connect_market_db(self.meta_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_meta (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_status (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    last_trade_date TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _bar_db_path_for_code(self, code: str) -> str:
        if not self.sharded:
            return self.db_path
        return os.path.join(
            self.db_path,
            "market_data_bars_{}.sqlite3".format(_market_data_bucket(code)),
        )

    def _bar_db_paths(self) -> List[str]:
        return _bar_db_paths(self.db_path)

    def _init_bar_db(self, db_path: str) -> None:
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _connect_market_db(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    open REAL NOT NULL DEFAULT 0,
                    high REAL NOT NULL DEFAULT 0,
                    low REAL NOT NULL DEFAULT 0,
                    close REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    turnover REAL NOT NULL DEFAULT 0,
                    adj_factor REAL NOT NULL DEFAULT 1,
                    qfq_open REAL NOT NULL DEFAULT 0,
                    qfq_high REAL NOT NULL DEFAULT 0,
                    qfq_low REAL NOT NULL DEFAULT 0,
                    qfq_close REAL NOT NULL DEFAULT 0,
                    pct_chg REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, code)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_bars_code_date ON daily_bars(code, trade_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trade_date)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_status (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    last_trade_date TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )


def load_history_frames(
    db_path: str,
    codes: Iterable[str],
    days: int = 90,
) -> Dict[str, pd.DataFrame]:
    db_path = resolve_market_data_path(db_path)
    normalized_codes = list(dict.fromkeys(normalize_code(code) for code in codes if str(code).strip()))
    if not normalized_codes or not os.path.exists(db_path):
        return {}
    history = _read_daily_bars_for_codes(
        db_path,
        normalized_codes,
        "trade_date, code, open, high, low, close, volume, turnover, qfq_open, qfq_high, qfq_low, qfq_close, pct_chg",
    )
    if history.empty:
        return {}

    result: Dict[str, pd.DataFrame] = {}
    for code, group in history.groupby("code"):
        df = group.tail(max(days, 6)).copy()
        for column in ("open", "high", "low", "close"):
            raw_value = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
            qfq_value = pd.to_numeric(df["qfq_{}".format(column)], errors="coerce").fillna(0.0)
            df[column] = qfq_value.where(qfq_value > 0, raw_value)
        df["price"] = df["close"]
        result[normalize_code(code)] = df[
            ["trade_date", "code", "open", "high", "low", "price", "volume", "turnover", "pct_chg"]
        ].reset_index(drop=True)
    return result


def load_tomorrow_history_factors(
    db_path: str,
    codes: Iterable[str],
    days: int = 90,
) -> pd.DataFrame:
    db_path = resolve_market_data_path(db_path)
    normalized_codes = list(dict.fromkeys(normalize_code(code) for code in codes if str(code).strip()))
    if not normalized_codes or not os.path.exists(db_path):
        return pd.DataFrame()
    history = _read_daily_bars_for_codes(
        db_path,
        normalized_codes,
        "trade_date, code, close, qfq_close, turnover",
    )
    if history.empty:
        return pd.DataFrame()

    rows = []
    for code, group in history.groupby("code"):
        factors = _tomorrow_factors_for_history(code, group.tail(max(days, 90)))
        if factors:
            rows.append(factors)
    return pd.DataFrame(rows)


def list_market_data_codes(db_path: str) -> List[str]:
    db_path = resolve_market_data_path(db_path)
    if not db_path or not os.path.exists(db_path):
        return []
    codes = []
    for path in _bar_db_paths(db_path):
        with _connect_market_db(path) as conn:
            try:
                rows = conn.execute("SELECT DISTINCT code FROM daily_bars").fetchall()
            except sqlite3.OperationalError:
                continue
        codes.extend(str(row[0]) for row in rows if row and row[0])
    return sorted(set(codes))


def resolve_market_data_path(db_path: str) -> str:
    path = str(db_path or "").strip() or ".runtime/market_data.sqlite3"
    if _market_data_path_has_bars(path):
        return path
    if _is_sqlite_file(path):
        sibling_dir = os.path.splitext(path)[0]
        if _market_data_path_has_bars(sibling_dir):
            return sibling_dir
    return path


def _tomorrow_factors_for_history(code: str, history: pd.DataFrame) -> Dict[str, object]:
    df = history.sort_values("trade_date").copy()
    close = pd.to_numeric(df["qfq_close"], errors="coerce").fillna(0.0)
    raw_close = pd.to_numeric(df["close"], errors="coerce").fillna(0.0)
    close = close.where(close > 0, raw_close)
    turnover = pd.to_numeric(df["turnover"], errors="coerce").fillna(0.0)
    if len(close) < 25 or close.iloc[-1] <= 0:
        return {}

    latest = close.iloc[-1]
    ma20 = close.tail(20).mean()
    ma60 = close.tail(60).mean() if len(close) >= 60 else 0.0
    returns = close.pct_change().dropna()
    volatility_20 = returns.tail(20).std() if len(returns) else 0.0
    high_60 = close.tail(60).max() if len(close) >= 60 else close.max()
    drawdown_60 = latest / high_60 - 1 if high_60 > 0 else 0.0
    ret_5 = _history_return(close, 5)
    ret_10 = _history_return(close, 10)
    ret_20 = _history_return(close, 20)
    ret_60 = _history_return(close, 60)
    ma20_gap = latest / ma20 - 1 if ma20 > 0 else 0.0
    ma60_gap = latest / ma60 - 1 if ma60 > 0 else 0.0
    avg_turnover_20 = turnover.tail(20).mean()
    avg_turnover_5 = turnover.tail(5).mean()
    prev_avg_turnover_5 = turnover.tail(10).head(5).mean() if len(turnover) >= 10 else avg_turnover_5
    vol_amount_5d = avg_turnover_5 / prev_avg_turnover_5 if prev_avg_turnover_5 > 0 else 0.0
    risk_adjusted_mom = (ret_20 + 0.7 * ret_10 + 0.4 * ret_5) / (volatility_20 + 0.015)
    downtrend_flag = (
        ret_20 < -0.12
        or ma20_gap < -0.02
        or drawdown_60 < -0.25
        or (ma60 > 0 and ma20 < ma60 and ret_60 < 0)
    )
    history_trend_ok = (
        ret_20 > -0.12
        and ma20_gap > -0.02
        and drawdown_60 > -0.25
        and volatility_20 < 0.08
    )

    return {
        "code": normalize_code(code),
        "history_trade_date": str(df["trade_date"].iloc[-1]),
        "ret_5d": round(ret_5 * 100, 4),
        "ret_10d": round(ret_10 * 100, 4),
        "ret_20d": round(ret_20 * 100, 4),
        "ret_60d": round(ret_60 * 100, 4),
        "ma20_gap": round(ma20_gap * 100, 4),
        "ma60_gap": round(ma60_gap * 100, 4),
        "vol_amount_5d": round(vol_amount_5d, 4),
        "volatility_20d": round(volatility_20 * 100, 4) if math.isfinite(volatility_20) else 0.0,
        "drawdown_60d": round(drawdown_60 * 100, 4),
        "avg_turnover_20d": round(avg_turnover_20, 4),
        "risk_adjusted_mom": round(risk_adjusted_mom, 4) if math.isfinite(risk_adjusted_mom) else 0.0,
        "history_trend_ok": bool(history_trend_ok),
        "downtrend_flag": bool(downtrend_flag),
    }


def _history_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return 0.0
    base = close.iloc[-days - 1]
    latest = close.iloc[-1]
    if base <= 0 or latest <= 0:
        return 0.0
    return latest / base - 1


def _read_daily_bars_for_codes(db_path: str, codes: List[str], columns: str) -> pd.DataFrame:
    frames = []
    for path, path_codes in _bar_db_paths_for_codes(db_path, codes).items():
        if not os.path.exists(path):
            continue
        placeholders = ",".join("?" for _ in path_codes)
        with _connect_market_db(path) as conn:
            query = """
                SELECT {}
                FROM daily_bars
                WHERE code IN ({})
                ORDER BY code ASC, trade_date ASC
            """.format(columns, placeholders)
            frames.append(pd.read_sql_query(query, conn, params=path_codes))
    frames = [frame for frame in frames if not frame.empty and frame.notna().any().any()]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _bar_db_paths_for_codes(db_path: str, codes: List[str]) -> Dict[str, List[str]]:
    if _is_sqlite_file(db_path):
        return {db_path: codes}
    grouped: Dict[str, List[str]] = {}
    for code in codes:
        path = os.path.join(
            db_path,
            "market_data_bars_{}.sqlite3".format(_market_data_bucket(code)),
        )
        grouped.setdefault(path, []).append(code)
    return grouped


def _bar_db_paths(db_path: str) -> List[str]:
    if _is_sqlite_file(db_path):
        return [db_path] if os.path.exists(db_path) else []
    return sorted(
        path
        for path in glob(os.path.join(db_path, "market_data_bars_*.sqlite3"))
        if os.path.isfile(path) and _is_current_bar_shard(path)
    )


def _is_sqlite_file(db_path: str) -> bool:
    return os.path.splitext(db_path)[1].lower() in (".db", ".sqlite", ".sqlite3")


def _market_data_path_has_bars(db_path: str) -> bool:
    if not db_path or not os.path.exists(db_path):
        return False
    for path in _bar_db_paths(db_path):
        try:
            with _connect_market_db(path) as conn:
                row = conn.execute("SELECT 1 FROM daily_bars LIMIT 1").fetchone()
            if row:
                return True
        except sqlite3.Error:
            continue
    return False


def _market_data_bucket(code: str) -> str:
    normalized = normalize_code(code)
    market = market_type(normalized) or "other"
    prefix = normalized[:5] if len(normalized) >= 5 else "misc"
    return "{}_{}".format(market, prefix)


def _is_current_bar_shard(path: str) -> bool:
    basename = os.path.basename(path)
    if not basename.startswith("market_data_bars_") or not basename.endswith(".sqlite3"):
        return False
    bucket = basename[len("market_data_bars_") : -len(".sqlite3")]
    parts = bucket.rsplit("_", 1)
    return len(parts) == 2 and (len(parts[1]) == 5 or parts[1] == "misc")


def build_daily_bars(code: str, raw_history: pd.DataFrame, qfq_history: pd.DataFrame) -> pd.DataFrame:
    raw = _normalize_history_frame(code, raw_history, prefix="")
    qfq = _normalize_history_frame(code, qfq_history, prefix="qfq_")
    if raw.empty and qfq.empty:
        return pd.DataFrame(columns=DAILY_BAR_COLUMNS)
    if raw.empty:
        raw = _raw_from_qfq(code, qfq)
    if qfq.empty:
        qfq = _qfq_from_raw(raw)
    merged = raw.merge(
        qfq[["trade_date", "code", "qfq_open", "qfq_high", "qfq_low", "qfq_close"]],
        on=["trade_date", "code"],
        how="left",
    )
    for column in ("qfq_open", "qfq_high", "qfq_low", "qfq_close"):
        source = column.replace("qfq_", "")
        if column not in merged.columns:
            merged[column] = merged[source]
        merged[column] = pd.to_numeric(merged[column], errors="coerce").fillna(merged[source])
    merged["adj_factor"] = merged.apply(_row_adj_factor, axis=1)
    merged = merged.sort_values("trade_date").drop_duplicates(["trade_date", "code"], keep="last")
    return merged[list(DAILY_BAR_COLUMNS)].reset_index(drop=True)


def _normalize_history_frame(code: str, history: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    df = rename_known_columns(history.copy())
    df = df.loc[:, ~df.columns.duplicated(keep="last")]
    if "trade_date" not in df.columns:
        return pd.DataFrame()
    if "code" not in df.columns:
        df["code"] = code
    if "close" not in df.columns and "price" in df.columns:
        df["close"] = df["price"]
    for column in ("open", "high", "low", "close", "volume", "turnover", "pct_chg"):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = df[column].map(coerce_number)
    df["code"] = df["code"].map(normalize_code)
    df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    columns = ["trade_date", "code"]
    values = ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]
    result = df[columns + values].copy()
    if prefix:
        result = result.rename(
            columns={column: "{}{}".format(prefix, column) for column in ("open", "high", "low", "close")}
        )
    return result


def _raw_from_qfq(code: str, qfq: pd.DataFrame) -> pd.DataFrame:
    df = qfq.copy()
    df["code"] = code
    for column in ("open", "high", "low", "close"):
        df[column] = df["qfq_{}".format(column)]
    for column in ("volume", "turnover", "pct_chg"):
        if column not in df.columns:
            df[column] = 0.0
    return df[["trade_date", "code", "open", "high", "low", "close", "volume", "turnover", "pct_chg"]]


def _qfq_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[["trade_date", "code", "open", "high", "low", "close"]].copy()
    return df.rename(
        columns={
            "open": "qfq_open",
            "high": "qfq_high",
            "low": "qfq_low",
            "close": "qfq_close",
        }
    )


def _row_adj_factor(row: pd.Series) -> float:
    close = coerce_number(row.get("close"))
    qfq_close = coerce_number(row.get("qfq_close"))
    if close <= 0 or qfq_close <= 0:
        return 1.0
    return round(qfq_close / close, 8)


def supported_stock_rows(rows: Iterable[Dict[str, object]], include_st: bool = False) -> List[Dict[str, object]]:
    from .normalization import is_supported_code

    result = []
    for row in rows:
        code = normalize_code(row.get("code"))
        name = str(row.get("name", "") or "")
        if not is_supported_code(code):
            continue
        if not include_st and ("ST" in name.upper() or "退" in name):
            continue
        result.append({"code": code, "name": name, "market": market_type(code), "is_active": True})
    return result

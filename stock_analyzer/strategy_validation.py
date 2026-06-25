import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from .normalization import coerce_number, normalize_code


class StrategyValidationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def save_signals(
        self,
        strategy_name: str,
        strategy_version: str,
        signal_time: str,
        rows: Iterable[Dict[str, object]],
    ) -> Dict[str, object]:
        signal_date = signal_time[:10]
        rows = list(rows)
        saved = 0
        with sqlite3.connect(self.db_path) as conn:
            old_ids = conn.execute(
                """
                SELECT id
                FROM strategy_signals
                WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                """,
                (strategy_name, strategy_version, signal_date),
            ).fetchall()
            if old_ids:
                conn.executemany(
                    "DELETE FROM strategy_outcomes WHERE signal_id = ?",
                    [(row[0],) for row in old_ids],
                )
                conn.execute(
                    """
                    DELETE FROM strategy_signals
                    WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                    """,
                    (strategy_name, strategy_version, signal_date),
                )
            for row in rows:
                code = normalize_code(row.get("code"))
                rank = int(row.get("rank") or 0)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategy_signals
                    (strategy_name, strategy_version, signal_date, signal_time, rank, code, name,
                     market, theme, price_at_signal, pct_chg_at_signal, turnover, volume_ratio,
                     turnover_rate, sixty_day_pct, ytd_pct, score, reasons_json, raw_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_name,
                        strategy_version,
                        signal_date,
                        signal_time,
                        rank,
                        code,
                        str(row.get("name", "")),
                        str(row.get("market_label") or row.get("market") or ""),
                        str(row.get("theme", "")),
                        coerce_number(row.get("price")),
                        coerce_number(row.get("pct_chg")),
                        coerce_number(row.get("turnover")),
                        coerce_number(row.get("volume_ratio")),
                        coerce_number(row.get("turnover_rate")),
                        coerce_number(row.get("sixty_day_pct")),
                        coerce_number(row.get("ytd_pct")),
                        coerce_number(row.get("score")),
                        json.dumps(row.get("reasons", []), ensure_ascii=False),
                        json.dumps(row, ensure_ascii=False),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                saved += 1
        return {"signal_date": signal_date, "saved": saved, "replaced": len(old_ids)}

    def list_signal_dates(self, strategy_name: str = "") -> List[Dict[str, object]]:
        where = ""
        params = []
        if strategy_name:
            where = "WHERE strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT signal_date, strategy_name, COUNT(*) AS count, MAX(signal_time) AS signal_time
                FROM strategy_signals
                {}
                GROUP BY signal_date, strategy_name
                ORDER BY signal_date DESC, strategy_name ASC
                LIMIT 120
                """.format(where),
                params,
            ).fetchall()
        return [
            {
                "signal_date": row[0],
                "strategy_name": row[1],
                "count": row[2],
                "signal_time": row[3],
            }
            for row in rows
        ]

    def signals_for_date(self, signal_date: str, strategy_name: str = "") -> List[Dict[str, object]]:
        where = "WHERE s.signal_date = ?"
        params = [signal_date]
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.*, o.next_trade_date, o.next_open_return, o.next_close_return,
                       o.intraday_high_return, o.hold_3d_return, o.max_gain_3d,
                       o.max_drawdown_3d, o.hit_3pct, o.hit_5pct,
                       o.signal_next_close_return, o.signal_intraday_high_return,
                       o.signal_hold_3d_return, o.signal_max_gain_3d,
                       o.signal_max_drawdown_3d, o.signal_hit_3pct, o.signal_hit_5pct,
                       o.updated_at AS outcome_updated_at
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                {}
                ORDER BY s.strategy_name ASC, s.rank ASC
                """.format(where),
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def latest_signal_rows(self, strategy_name: str) -> List[Dict[str, object]]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT signal_date
                FROM strategy_signals
                WHERE strategy_name = ?
                ORDER BY signal_date DESC, signal_time DESC
                LIMIT 1
                """,
                (strategy_name,),
            ).fetchone()
        if not row:
            return []
        signals = self.signals_for_date(row[0], strategy_name)
        rows = []
        for signal in signals:
            raw = signal.get("raw") or {}
            if isinstance(raw, dict):
                item = raw.copy()
                item["rank"] = signal.get("rank")
                rows.append(item)
        rows.sort(key=lambda item: int(item.get("rank") or 9999))
        return rows

    def update_outcomes(self, provider, signal_date: str = "", strategy_name: str = "") -> Dict[str, object]:
        where = "WHERE 1=1"
        params = []
        if signal_date:
            where += " AND signal_date = ?"
            params.append(signal_date)
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            signals = conn.execute(
                "SELECT * FROM strategy_signals {} ORDER BY signal_date DESC, rank ASC".format(where),
                params,
            ).fetchall()

        updated = 0
        skipped = 0
        for signal in signals:
            outcome = _compute_outcome(provider, signal)
            if not outcome:
                skipped += 1
                continue
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategy_outcomes
                    (signal_id, code, next_trade_date, next_open, next_high, next_low, next_close,
                     next_open_return, next_close_return, intraday_high_return, hold_3d_return,
                     max_gain_3d, max_drawdown_3d, hit_3pct, hit_5pct,
                     signal_next_close_return, signal_intraday_high_return, signal_hold_3d_return,
                     signal_max_gain_3d, signal_max_drawdown_3d, signal_hit_3pct, signal_hit_5pct,
                     updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal["id"],
                        signal["code"],
                        outcome["next_trade_date"],
                        outcome["next_open"],
                        outcome["next_high"],
                        outcome["next_low"],
                        outcome["next_close"],
                        outcome["next_open_return"],
                        outcome["next_close_return"],
                        outcome["intraday_high_return"],
                        outcome["hold_3d_return"],
                        outcome["max_gain_3d"],
                        outcome["max_drawdown_3d"],
                        int(outcome["hit_3pct"]),
                        int(outcome["hit_5pct"]),
                        outcome["signal_next_close_return"],
                        outcome["signal_intraday_high_return"],
                        outcome["signal_hold_3d_return"],
                        outcome["signal_max_gain_3d"],
                        outcome["signal_max_drawdown_3d"],
                        int(outcome["signal_hit_3pct"]),
                        int(outcome["signal_hit_5pct"]),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            updated += 1
        return {"updated": updated, "skipped": skipped}

    def metrics(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        where = "WHERE o.signal_id IS NOT NULL"
        params = []
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.rank,
                       COALESCE(o.signal_next_close_return, o.next_close_return) AS signal_next_close_return,
                       o.next_close_return,
                       COALESCE(o.signal_intraday_high_return, o.intraday_high_return) AS signal_intraday_high_return,
                       COALESCE(o.signal_hold_3d_return, o.hold_3d_return) AS signal_hold_3d_return,
                       COALESCE(o.signal_max_drawdown_3d, o.max_drawdown_3d) AS signal_max_drawdown_3d,
                       COALESCE(o.signal_hit_3pct, o.hit_3pct) AS signal_hit_3pct,
                       COALESCE(o.signal_hit_5pct, o.hit_5pct) AS signal_hit_5pct
                FROM strategy_signals s
                JOIN strategy_outcomes o ON o.signal_id = s.id
                {}
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(where),
                params,
            ).fetchall()
        if not rows:
            return {"sample_count": 0, "daily": []}
        dates = []
        for row in rows:
            if row["signal_date"] not in dates:
                dates.append(row["signal_date"])
            if len(dates) >= days:
                break
        selected = [row for row in rows if row["signal_date"] in dates]
        return {
            "sample_count": len(selected),
            "day_count": len(dates),
            "avg_next_close_return": _avg(row["signal_next_close_return"] for row in selected),
            "win_rate_next_close": _rate(row["signal_next_close_return"] > 0 for row in selected),
            "hit_3pct_rate": _rate(bool(row["signal_hit_3pct"]) for row in selected),
            "hit_5pct_rate": _rate(bool(row["signal_hit_5pct"]) for row in selected),
            "avg_intraday_high_return": _avg(row["signal_intraday_high_return"] for row in selected),
            "avg_hold_3d_return": _avg(row["signal_hold_3d_return"] for row in selected),
            "avg_max_drawdown_3d": _avg(row["signal_max_drawdown_3d"] for row in selected),
            "top10_avg_next_close_return": _avg(
                row["signal_next_close_return"] for row in selected if row["rank"] <= 10
            ),
            "avg_open_to_close_return": _avg(row["next_close_return"] for row in selected),
            "daily": _daily_metrics(selected),
        }

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    theme TEXT NOT NULL DEFAULT '',
                    price_at_signal REAL NOT NULL DEFAULT 0,
                    pct_chg_at_signal REAL NOT NULL DEFAULT 0,
                    turnover REAL NOT NULL DEFAULT 0,
                    volume_ratio REAL NOT NULL DEFAULT 0,
                    turnover_rate REAL NOT NULL DEFAULT 0,
                    sixty_day_pct REAL NOT NULL DEFAULT 0,
                    ytd_pct REAL NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    reasons_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_name, strategy_version, signal_date, code)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_outcomes (
                    signal_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    next_trade_date TEXT NOT NULL,
                    next_open REAL NOT NULL DEFAULT 0,
                    next_high REAL NOT NULL DEFAULT 0,
                    next_low REAL NOT NULL DEFAULT 0,
                    next_close REAL NOT NULL DEFAULT 0,
                    next_open_return REAL NOT NULL DEFAULT 0,
                    next_close_return REAL NOT NULL DEFAULT 0,
                    intraday_high_return REAL NOT NULL DEFAULT 0,
                    hold_3d_return REAL NOT NULL DEFAULT 0,
                    max_gain_3d REAL NOT NULL DEFAULT 0,
                    max_drawdown_3d REAL NOT NULL DEFAULT 0,
                    hit_3pct INTEGER NOT NULL DEFAULT 0,
                    hit_5pct INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES strategy_signals(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_signals_date ON strategy_signals(signal_date)")
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(strategy_outcomes)").fetchall()
            }
            outcome_columns = {
                "signal_next_close_return": "REAL",
                "signal_intraday_high_return": "REAL",
                "signal_hold_3d_return": "REAL",
                "signal_max_gain_3d": "REAL",
                "signal_max_drawdown_3d": "REAL",
                "signal_hit_3pct": "INTEGER",
                "signal_hit_5pct": "INTEGER",
            }
            for column, column_type in outcome_columns.items():
                if column not in existing_columns:
                    conn.execute("ALTER TABLE strategy_outcomes ADD COLUMN {} {}".format(column, column_type))


def _compute_outcome(provider, signal: sqlite3.Row) -> Optional[Dict[str, object]]:
    history = provider.get_history(signal["code"], days=180)
    if history is None or history.empty or "trade_date" not in history.columns:
        return None
    df = history.sort_values("trade_date").reset_index(drop=True)
    signal_date = str(signal["signal_date"]).replace("-", "")
    future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(drop=True)
    if future.empty:
        return None
    first = future.iloc[0]
    open_entry = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    signal_entry = coerce_number(signal["price_at_signal"])
    close = coerce_number(first.get("price"))
    high = coerce_number(first.get("high"))
    low = coerce_number(first.get("low"))
    if open_entry <= 0:
        return None
    if signal_entry <= 0:
        signal_entry = open_entry
    window = future.head(3)
    last = window.iloc[-1]
    hold_3d_close = coerce_number(last.get("price"))
    max_high = max(coerce_number(value) for value in window.get("high", pd.Series([high])).tolist())
    min_low = min(coerce_number(value) for value in window.get("low", pd.Series([low])).tolist())
    return {
        "next_trade_date": str(first.get("trade_date")),
        "next_open": round(open_entry, 4),
        "next_high": round(high, 4),
        "next_low": round(low, 4),
        "next_close": round(close, 4),
        "next_open_return": round((open_entry / signal_entry - 1) * 100, 4),
        "next_close_return": round((close / open_entry - 1) * 100, 4) if close > 0 else 0.0,
        "intraday_high_return": round((high / open_entry - 1) * 100, 4) if high > 0 else 0.0,
        "hold_3d_return": round((hold_3d_close / open_entry - 1) * 100, 4) if hold_3d_close > 0 else 0.0,
        "max_gain_3d": round((max_high / open_entry - 1) * 100, 4) if max_high > 0 else 0.0,
        "max_drawdown_3d": round((min_low / open_entry - 1) * 100, 4) if min_low > 0 else 0.0,
        "hit_3pct": high / open_entry - 1 >= 0.03 if high > 0 else False,
        "hit_5pct": high / open_entry - 1 >= 0.05 if high > 0 else False,
        "signal_next_close_return": round((close / signal_entry - 1) * 100, 4) if close > 0 else 0.0,
        "signal_intraday_high_return": round((high / signal_entry - 1) * 100, 4) if high > 0 else 0.0,
        "signal_hold_3d_return": round((hold_3d_close / signal_entry - 1) * 100, 4) if hold_3d_close > 0 else 0.0,
        "signal_max_gain_3d": round((max_high / signal_entry - 1) * 100, 4) if max_high > 0 else 0.0,
        "signal_max_drawdown_3d": round((min_low / signal_entry - 1) * 100, 4) if min_low > 0 else 0.0,
        "signal_hit_3pct": high / signal_entry - 1 >= 0.03 if high > 0 else False,
        "signal_hit_5pct": high / signal_entry - 1 >= 0.05 if high > 0 else False,
    }


def _row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("reasons_json", "raw_json"):
        try:
            item[key.replace("_json", "")] = json.loads(item.get(key) or "[]")
        except Exception:
            item[key.replace("_json", "")] = [] if key == "reasons_json" else {}
    return item


def _avg(values) -> float:
    clean = [coerce_number(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else 0.0


def _rate(values) -> float:
    clean = list(values)
    return round(sum(1 for value in clean if value) / len(clean) * 100, 2) if clean else 0.0


def _daily_metrics(rows: List[sqlite3.Row]) -> List[Dict[str, object]]:
    grouped: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["signal_date"], []).append(row)
    daily = []
    for date, items in grouped.items():
        daily.append(
            {
                "signal_date": date,
                "sample_count": len(items),
                "avg_next_close_return": _avg(row["signal_next_close_return"] for row in items),
                "win_rate_next_close": _rate(row["signal_next_close_return"] > 0 for row in items),
                "hit_3pct_rate": _rate(bool(row["signal_hit_3pct"]) for row in items),
                "hit_5pct_rate": _rate(bool(row["signal_hit_5pct"]) for row in items),
                "avg_hold_3d_return": _avg(row["signal_hold_3d_return"] for row in items),
            }
        )
    return sorted(daily, key=lambda item: item["signal_date"], reverse=True)

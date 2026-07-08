from __future__ import annotations

from datetime import datetime
import json
import os
import sqlite3
from typing import Dict, Iterable, List

import pandas as pd

from .daily_data import list_market_data_codes, load_history_frames
from .factors import ALPHALITE_COLUMNS, ALPHALITE_META_COLUMNS, compute_alphalite_for_stock
from .normalization import coerce_number, normalize_code


FACTOR_SET = "alphalite_v1"
FACTOR_COLUMNS = tuple(ALPHALITE_COLUMNS) + tuple(ALPHALITE_META_COLUMNS)


class FactorSnapshotStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path or "").strip() or ".runtime/factor_snapshots.sqlite3"
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def upsert(self, rows: Iterable[Dict[str, object]], factor_set: str = FACTOR_SET) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        payload = []
        for row in rows:
            code = normalize_code(row.get("code"))
            trade_date = str(row.get("trade_date") or "").strip()
            if not code or not trade_date:
                continue
            values = [coerce_number(row.get(column), 0.0) for column in FACTOR_COLUMNS]
            payload.append(
                (
                    trade_date,
                    code,
                    factor_set,
                    *values,
                    json.dumps({column: values[index] for index, column in enumerate(FACTOR_COLUMNS)}, ensure_ascii=False),
                    now,
                )
            )
        if not payload:
            return 0
        columns_sql = ", ".join(FACTOR_COLUMNS)
        placeholders = ", ".join(["?"] * (3 + len(FACTOR_COLUMNS) + 2))
        update_sql = ", ".join("{} = excluded.{}".format(column, column) for column in FACTOR_COLUMNS)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.executemany(
                """
                INSERT INTO factor_snapshots
                (trade_date, code, factor_set, {columns_sql}, factors_json, updated_at)
                VALUES ({placeholders})
                ON CONFLICT(trade_date, code, factor_set) DO UPDATE SET
                    {update_sql},
                    factors_json = excluded.factors_json,
                    updated_at = excluded.updated_at
                """.format(
                    columns_sql=columns_sql,
                    placeholders=placeholders,
                    update_sql=update_sql,
                ),
                payload,
            )
        return len(payload)

    def latest(self, factor_set: str = FACTOR_SET, limit: int = 50) -> List[Dict[str, object]]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM factor_snapshots
                WHERE factor_set = ?
                ORDER BY trade_date DESC, code ASC
                LIMIT ?
                """,
                (factor_set, max(1, int(limit))),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def lookup(self, cases: Iterable[Dict[str, object]], factor_set: str = FACTOR_SET) -> Dict[tuple, Dict[str, object]]:
        keys = []
        for case in cases or []:
            trade_date = _normalize_trade_date(case.get("trade_date") or case.get("signal_date") or case.get("date"))
            code = normalize_code(case.get("code"))
            if trade_date and code and (trade_date, code) not in keys:
                keys.append((trade_date, code))
        if not keys:
            return {}
        dates = sorted({key[0] for key in keys})
        codes = sorted({key[1] for key in keys})
        date_placeholders = ", ".join(["?"] * len(dates))
        code_placeholders = ", ".join(["?"] * len(codes))
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM factor_snapshots
                WHERE factor_set = ?
                  AND trade_date IN ({date_placeholders})
                  AND code IN ({code_placeholders})
                """.format(
                    date_placeholders=date_placeholders,
                    code_placeholders=code_placeholders,
                ),
                [factor_set, *dates, *codes],
            ).fetchall()
        wanted = set(keys)
        result = {}
        for row in rows:
            key = (_normalize_trade_date(row["trade_date"]), normalize_code(row["code"]))
            if key in wanted:
                result[key] = _row_to_dict(row).get("factors", {})
        return result

    def summary(self) -> Dict[str, object]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            total = conn.execute("SELECT COUNT(*) FROM factor_snapshots").fetchone()[0]
            codes = conn.execute("SELECT COUNT(DISTINCT code) FROM factor_snapshots").fetchone()[0]
            date_range = conn.execute("SELECT MIN(trade_date), MAX(trade_date) FROM factor_snapshots").fetchone()
            factor_sets = conn.execute(
                """
                SELECT factor_set, COUNT(*)
                FROM factor_snapshots
                GROUP BY factor_set
                ORDER BY factor_set
                """
            ).fetchall()
        return {
            "db_path": self.db_path,
            "row_count": int(total or 0),
            "stock_count": int(codes or 0),
            "date_start": str(date_range[0] or "") if date_range else "",
            "date_end": str(date_range[1] or "") if date_range else "",
            "factor_sets": {str(row[0]): int(row[1]) for row in factor_sets},
        }

    def _init_db(self) -> None:
        column_defs = ",\n                    ".join("{} REAL NOT NULL DEFAULT 0".format(column) for column in FACTOR_COLUMNS)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factor_snapshots (
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    factor_set TEXT NOT NULL,
                    {column_defs},
                    factors_json TEXT NOT NULL DEFAULT '{{}}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, code, factor_set)
                )
                """.format(column_defs=column_defs)
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_snapshots_code ON factor_snapshots(code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_snapshots_trade_date ON factor_snapshots(trade_date)")


def build_factor_snapshots(
    market_data_db_path: str,
    snapshot_db_path: str,
    codes: Iterable[str] = None,
    limit: int = 0,
    days: int = 120,
    batch_size: int = 200,
) -> Dict[str, object]:
    code_list = _normalize_codes(codes) if codes else list_market_data_codes(market_data_db_path)
    if limit and int(limit) > 0:
        code_list = code_list[: int(limit)]
    store = FactorSnapshotStore(snapshot_db_path)
    requested = len(code_list)
    saved = 0
    skipped = 0
    last_trade_date = ""
    for batch in _batches(code_list, batch_size):
        frames = load_history_frames(market_data_db_path, batch, days=days)
        rows = []
        for code, history in frames.items():
            row = _snapshot_row(code, history)
            if row:
                rows.append(row)
                trade_date = str(row.get("trade_date") or "")
                if trade_date > last_trade_date:
                    last_trade_date = trade_date
            else:
                skipped += 1
        saved += store.upsert(rows)
        skipped += max(0, len(batch) - len(frames))
    return {
        "ok": True,
        "factor_set": FACTOR_SET,
        "requested": requested,
        "saved": saved,
        "skipped": skipped,
        "last_trade_date": last_trade_date,
        "summary": store.summary(),
    }


def _snapshot_row(code: str, history: pd.DataFrame) -> Dict[str, object]:
    if history is None or history.empty:
        return {}
    ordered = history.sort_values("trade_date").reset_index(drop=True)
    factors = compute_alphalite_for_stock(code, ordered)
    if not factors:
        return {}
    factors["trade_date"] = str(ordered["trade_date"].iloc[-1])
    return factors


def _normalize_codes(codes: Iterable[str]) -> List[str]:
    result = []
    for code in codes or []:
        normalized = normalize_code(code)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _normalize_trade_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10].replace("-", "")


def _batches(values: List[str], batch_size: int) -> Iterable[List[str]]:
    size = max(1, int(batch_size or 1))
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    result = {key: row[key] for key in row.keys()}
    try:
        result["factors"] = json.loads(result.get("factors_json") or "{}")
    except Exception:
        result["factors"] = {}
    return result

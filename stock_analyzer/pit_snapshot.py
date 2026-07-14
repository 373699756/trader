from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import zlib
from datetime import datetime
from typing import Dict, Iterable, List

from .normalization import coerce_number, market_type, normalize_code
from .sqlite_support import sqlite_transaction


REAL_FORWARD = "real_forward"
CLOSE_FORWARD = "close_forward"
INTRADAY_PIT_REPLAY = "intraday_pit_replay"
DAILY_PROXY_REPLAY = "daily_proxy_replay"
LEGACY_BASELINE = "legacy_baseline"
UNKNOWN_SAMPLE = "unknown"
SAMPLE_TYPES = {
    REAL_FORWARD,
    CLOSE_FORWARD,
    INTRADAY_PIT_REPLAY,
    DAILY_PROXY_REPLAY,
    LEGACY_BASELINE,
    UNKNOWN_SAMPLE,
}


class PointInTimeSnapshotStore:
    """Persists one immutable raw quote universe for each point-in-time snapshot."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path or "").strip()
        if not self.db_path:
            raise ValueError("db_path is required")
        self._init_db()

    def save(
        self,
        snapshot_id: str,
        quotes,
        *,
        captured_at: str,
        data_source_timestamp: str,
        source: str = "",
        sample_type: str = UNKNOWN_SAMPLE,
        sample_source: str = "intraday_pit_14_30",
    ) -> Dict[str, object]:
        snapshot_id = str(snapshot_id or "").strip()
        if not snapshot_id:
            raise ValueError("snapshot_id is required")
        captured_at = str(captured_at or "").strip()
        if not captured_at:
            raise ValueError("captured_at is required")
        sample_type = normalize_sample_type(sample_type)
        observed_timestamp = str(data_source_timestamp or "").strip()
        if sample_type == REAL_FORWARD and not observed_timestamp:
            # A label alone cannot turn an untimed/replayed quote into a live sample.
            sample_type = UNKNOWN_SAMPLE
        records = _records_from_quotes(quotes)
        if not records:
            raise ValueError("raw quote universe is empty")
        serialized = json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        checksum = hashlib.sha256(serialized).hexdigest()
        compressed = zlib.compress(serialized, level=9)
        coverage = field_coverage(records)
        valid_quote_count = sum(1 for row in records if coerce_number(row.get("price"), 0.0) > 0)
        signal_date = captured_at[:10]
        now = datetime.now().isoformat(timespec="seconds")
        universe_rows = _universe_rows(
            records,
            signal_date=signal_date,
            snapshot_id=snapshot_id,
            observed_at=str(data_source_timestamp or captured_at),
        )
        with sqlite_transaction(self.db_path) as conn:
            existing = conn.execute(
                "SELECT quote_checksum FROM market_quote_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if existing and str(existing[0] or "") != checksum:
                raise ValueError("snapshot_id already exists with different quote content")
            conn.execute(
                """
                INSERT OR IGNORE INTO market_quote_snapshots
                (snapshot_id, signal_date, captured_at, data_source_timestamp, source,
                 sample_type, sample_source, quote_count, valid_quote_count,
                 field_coverage_json, quote_checksum, raw_quotes_blob, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    signal_date,
                    captured_at,
                    observed_timestamp,
                    str(source or ""),
                    sample_type,
                    str(sample_source or ""),
                    len(records),
                    valid_quote_count,
                    json.dumps(coverage, ensure_ascii=False, sort_keys=True),
                    checksum,
                    compressed,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO security_universe_history
                (signal_date, snapshot_id, code, name, market, observed_at,
                 tradability_status, is_st, is_suspended, is_delisted,
                 price_limit_pct, industry, market_cap, float_market_cap,
                 index_memberships_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                universe_rows,
            )
        return {
            "status": "saved" if not existing else "already_saved",
            "snapshot_id": snapshot_id,
            "signal_date": signal_date,
            "captured_at": captured_at,
            "data_source_timestamp": observed_timestamp,
            "quote_count": len(records),
            "valid_quote_count": valid_quote_count,
            "field_coverage": coverage,
            "quote_checksum": checksum,
            "compressed_bytes": len(compressed),
            "universe_count": len(universe_rows),
            "sample_type": sample_type,
            "sample_source": str(sample_source or ""),
        }

    def get(self, snapshot_id: str, include_quotes: bool = False) -> Dict[str, object]:
        with sqlite_transaction(self.db_path, row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                "SELECT * FROM market_quote_snapshots WHERE snapshot_id = ?",
                (str(snapshot_id or ""),),
            ).fetchone()
        if not row:
            return {}
        item = dict(row)
        item["field_coverage"] = _load_json(item.pop("field_coverage_json", "{}"), {})
        raw_blob = item.pop("raw_quotes_blob", None)
        if include_quotes and raw_blob:
            item["quotes"] = json.loads(zlib.decompress(bytes(raw_blob)).decode("utf-8"))
        return item

    def latest(self, include_quotes: bool = False) -> Dict[str, object]:
        with sqlite_transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT snapshot_id FROM market_quote_snapshots ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        return self.get(row[0], include_quotes=include_quotes) if row else {}

    def summary(self) -> Dict[str, object]:
        with sqlite_transaction(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT signal_date), MIN(signal_date), MAX(signal_date),
                       COALESCE(MAX(quote_count), 0), COALESCE(MAX(valid_quote_count), 0)
                FROM market_quote_snapshots
                """
            ).fetchone()
            universe = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT signal_date), COUNT(DISTINCT code)
                FROM security_universe_history
                """
            ).fetchone()
        latest = self.latest(include_quotes=False)
        return {
            "snapshot_count": int(row[0] or 0),
            "trade_day_count": int(row[1] or 0),
            "date_start": str(row[2] or ""),
            "date_end": str(row[3] or ""),
            "max_quote_count": int(row[4] or 0),
            "max_valid_quote_count": int(row[5] or 0),
            "universe_row_count": int(universe[0] or 0),
            "universe_day_count": int(universe[1] or 0),
            "universe_stock_count": int(universe[2] or 0),
            "latest": latest,
        }

    def _init_db(self) -> None:
        with sqlite_transaction(self.db_path) as conn:
            for statement in PIT_SNAPSHOT_TABLES + PIT_SNAPSHOT_INDEXES:
                conn.execute(statement)


def normalize_sample_type(value: object) -> str:
    sample_type = str(value or "").strip().lower()
    sample_type = {
        "real": REAL_FORWARD,
        "live": REAL_FORWARD,
        "forward": REAL_FORWARD,
        "production": REAL_FORWARD,
        "close": CLOSE_FORWARD,
        "close_final": CLOSE_FORWARD,
        "replay": DAILY_PROXY_REPLAY,
        "daily_proxy": DAILY_PROXY_REPLAY,
        "daily_bar_proxy": DAILY_PROXY_REPLAY,
        "intraday": INTRADAY_PIT_REPLAY,
        "pit_replay": INTRADAY_PIT_REPLAY,
        "legacy": LEGACY_BASELINE,
    }.get(sample_type, sample_type)
    return sample_type if sample_type in SAMPLE_TYPES else UNKNOWN_SAMPLE


def field_coverage(records: Iterable[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    rows = [row for row in records or [] if isinstance(row, dict)]
    columns = sorted({str(key) for row in rows for key in row})
    total = len(rows)
    return {
        column: {
            "valid_count": sum(1 for row in rows if not _missing(row.get(column))),
            "total_count": total,
            "coverage_pct": round(
                sum(1 for row in rows if not _missing(row.get(column))) * 100.0 / max(1, total),
                4,
            ),
        }
        for column in columns
    }


PIT_SNAPSHOT_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS market_quote_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        signal_date TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        data_source_timestamp TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        sample_type TEXT NOT NULL DEFAULT 'unknown',
        sample_source TEXT NOT NULL DEFAULT '',
        quote_count INTEGER NOT NULL DEFAULT 0,
        valid_quote_count INTEGER NOT NULL DEFAULT 0,
        field_coverage_json TEXT NOT NULL DEFAULT '{}',
        quote_checksum TEXT NOT NULL,
        raw_quotes_blob BLOB NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS security_universe_history (
        signal_date TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        market TEXT NOT NULL DEFAULT '',
        observed_at TEXT NOT NULL DEFAULT '',
        tradability_status TEXT NOT NULL DEFAULT 'unknown',
        is_st INTEGER,
        is_suspended INTEGER,
        is_delisted INTEGER,
        price_limit_pct REAL,
        industry TEXT NOT NULL DEFAULT '',
        market_cap REAL,
        float_market_cap REAL,
        index_memberships_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        PRIMARY KEY (signal_date, snapshot_id, code),
        FOREIGN KEY(snapshot_id) REFERENCES market_quote_snapshots(snapshot_id)
    )
    """,
)

PIT_SNAPSHOT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_market_quote_snapshots_date ON market_quote_snapshots(signal_date, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_security_universe_date_code ON security_universe_history(signal_date, code)",
    "CREATE INDEX IF NOT EXISTS idx_security_universe_code_date ON security_universe_history(code, signal_date)",
)


def _records_from_quotes(quotes) -> List[Dict[str, object]]:
    if quotes is None:
        return []
    if hasattr(quotes, "to_dict"):
        raw_rows = quotes.to_dict(orient="records")
    elif isinstance(quotes, list):
        raw_rows = quotes
    else:
        raw_rows = list(quotes or [])
    return [_json_safe(dict(row)) for row in raw_rows if isinstance(row, dict)]


def _universe_rows(
    records: Iterable[Dict[str, object]],
    *,
    signal_date: str,
    snapshot_id: str,
    observed_at: str,
) -> List[tuple]:
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    seen = set()
    for item in records:
        code = normalize_code(item.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        name = str(item.get("name") or "")
        price = coerce_number(item.get("price"), None)
        explicit_status = str(item.get("trading_status") or item.get("security_status") or "").strip()
        tradability_status = explicit_status or ("observed_price" if price is not None and price > 0 else "unknown")
        rows.append(
            (
                signal_date,
                snapshot_id,
                code,
                name,
                str(item.get("market") or market_type(code)),
                observed_at,
                tradability_status,
                _nullable_bool(item.get("is_st"), fallback=("ST" in name.upper())),
                _nullable_bool(item.get("is_suspended")),
                _nullable_bool(item.get("is_delisted"), fallback=("退" in name)),
                _nullable_number(item.get("price_limit_pct")),
                str(item.get("industry") or ""),
                _nullable_number(item.get("market_cap")),
                _nullable_number(item.get("float_market_cap")),
                json.dumps(item.get("index_memberships") or [], ensure_ascii=False, sort_keys=True),
                now,
            )
        )
    return rows


def _nullable_bool(value: object, fallback=None):
    if value is None or value == "":
        return None if fallback is None else int(bool(fallback))
    return int(bool(value))


def _nullable_number(value: object):
    return coerce_number(value, None)


def _missing(value: object) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    return False


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _load_json(value: object, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code
from .risk_rules import simulate_exit


PRIMARY_RETURN_BY_STRATEGY = {
    "short_term": ("signal_next_close_return", 1, "次日"),
    "tomorrow_picks": ("signal_next_close_return", 1, "尾盘买入次日"),
    "reversal_picks": ("signal_hold_5d_return", 5, "5日"),
    "swing_picks": ("signal_hold_10d_return", 10, "10日"),
    "breakout_picks": ("signal_hold_10d_return", 10, "10日"),
    "long_term": ("signal_hold_20d_return", 20, "20日"),
    "position_picks": ("signal_hold_20d_return", 20, "20日"),
    "tech_potential": ("signal_hold_20d_return", 20, "20日"),
    "chokepoint_picks": ("signal_hold_20d_return", 20, "20日"),
    "smallcap_value_picks": ("signal_hold_20d_return", 20, "20日"),
}

EXECUTABLE_PRIMARY_RETURN_BY_STRATEGY = {
    "short_term": ("next_close_return", 1, "次日开盘入场"),
    "tomorrow_picks": ("next_close_return", 1, "次日开盘入场"),
    "reversal_picks": ("hold_5d_return", 5, "5日开盘入场"),
    "swing_picks": ("hold_10d_return", 10, "10日开盘入场"),
    "breakout_picks": ("hold_10d_return", 10, "10日开盘入场"),
    "long_term": ("hold_20d_return", 20, "20日开盘入场"),
    "position_picks": ("hold_20d_return", 20, "20日开盘入场"),
    "tech_potential": ("hold_20d_return", 20, "20日开盘入场"),
    "chokepoint_picks": ("hold_20d_return", 20, "20日开盘入场"),
    "smallcap_value_picks": ("hold_20d_return", 20, "20日开盘入场"),
}


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
        with sqlite3.connect(self.db_path, timeout=30) as conn:
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
        with sqlite3.connect(self.db_path, timeout=30) as conn:
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
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.*, o.next_trade_date, o.next_open, o.next_high, o.next_low, o.next_close,
                       o.next_open_return, o.next_close_return,
                       o.intraday_high_return, o.hold_3d_return, o.hold_5d_return,
                       o.hold_10d_return, o.hold_20d_return, o.max_gain_3d,
                       o.max_drawdown_3d, o.hit_3pct, o.hit_5pct,
                       o.signal_next_close_return, o.signal_intraday_high_return,
                       o.signal_hold_3d_return, o.signal_max_gain_3d,
                       o.signal_max_drawdown_3d, o.signal_hit_3pct, o.signal_hit_5pct,
                       o.signal_hold_5d_return, o.signal_hold_10d_return, o.signal_hold_20d_return,
                       o.exit_return, o.signal_exit_return, o.exit_reason, o.exit_days, o.exit_date,
                       o.future_days,
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
        with sqlite3.connect(self.db_path, timeout=30) as conn:
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

    def live_weight_samples(self, strategy_name: str, days: int = 120) -> List[Dict[str, object]]:
        primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.strategy_version, s.rank, s.code,
                       s.name, s.score, s.turnover, s.market, s.raw_json,
                       COALESCE(o.{primary_column}, 0) AS primary_return,
                       COALESCE(o.signal_exit_return, o.exit_return, o.{primary_column}, 0) AS exit_return,
                       COALESCE(o.future_days, 1) AS future_days
                FROM strategy_signals s
                JOIN strategy_outcomes o ON o.signal_id = s.id
                WHERE s.strategy_name = ?
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(primary_column=primary_column),
                (strategy_name,),
            ).fetchall()
        if not rows:
            return []
        dates: List[str] = []
        selected: List[sqlite3.Row] = []
        for row in rows:
            if _is_replay_version(row["strategy_version"]):
                continue
            if int(row["future_days"] or 1) < primary_days:
                continue
            if row["signal_date"] not in dates:
                if len(dates) >= max(1, int(days)):
                    continue
                dates.append(row["signal_date"])
            if row["signal_date"] in dates:
                selected.append(row)
        samples: List[Dict[str, object]] = []
        for row in selected:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except Exception:
                raw = {}
            primary_return = coerce_number(row["primary_return"])
            trade_cost = _execution_cost_pct(row)
            samples.append(
                {
                    "signal_date": row["signal_date"],
                    "strategy_name": row["strategy_name"],
                    "strategy_version": row["strategy_version"],
                    "rank": int(row["rank"] or 0),
                    "code": normalize_code(row["code"]),
                    "name": row["name"],
                    "stored_score": coerce_number(row["score"]),
                    "raw": raw if isinstance(raw, dict) else {},
                    "primary_return": primary_return,
                    "primary_return_net": round(primary_return - trade_cost, 4),
                    "trade_cost_pct": trade_cost,
                    "exit_return": coerce_number(row["exit_return"]),
                    "future_days": int(row["future_days"] or 0),
                    "primary_holding_days": primary_days,
                    "primary_horizon_label": primary_label,
                }
            )
        return samples

    def signal_codes(
        self,
        signal_date: str = "",
        strategy_name: str = "",
        limit: int = 500,
    ) -> List[Dict[str, object]]:
        where = "WHERE 1=1"
        params: List[object] = []
        if signal_date:
            where += " AND signal_date = ?"
            params.append(signal_date)
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        params.append(max(1, int(limit)))
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT code,
                       MAX(name) AS name,
                       COUNT(*) AS signal_count,
                       MAX(signal_date) AS latest_signal_date,
                       MIN(rank) AS best_rank
                FROM strategy_signals
                {}
                GROUP BY code
                ORDER BY latest_signal_date DESC, best_rank ASC
                LIMIT ?
                """.format(where),
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def update_outcomes(
        self,
        provider,
        signal_date: str = "",
        strategy_name: str = "",
        codes: Optional[Iterable[str]] = None,
    ) -> Dict[str, object]:
        where = "WHERE 1=1"
        params = []
        if signal_date:
            where += " AND signal_date = ?"
            params.append(signal_date)
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        normalized_codes: List[str] = []
        for code in codes or []:
            normalized = normalize_code(code)
            if normalized:
                normalized_codes.append(normalized)
        if normalized_codes:
            placeholders = ",".join("?" for _ in normalized_codes)
            where += " AND code IN ({})".format(placeholders)
            params.extend(normalized_codes)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            signals = conn.execute(
                "SELECT * FROM strategy_signals {} ORDER BY signal_date DESC, rank ASC".format(where),
                params,
            ).fetchall()

        updated = 0
        skipped = 0
        for signal in signals:
            outcome = _compute_outcome(provider, signal)
            if outcome and outcome.get("excluded"):
                with sqlite3.connect(self.db_path, timeout=30) as conn:
                    conn.execute("DELETE FROM strategy_outcomes WHERE signal_id = ?", (signal["id"],))
                skipped += 1
                continue
            if not outcome:
                skipped += 1
                continue
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategy_outcomes
                    (signal_id, code, next_trade_date, future_days, next_open, next_high, next_low, next_close,
                     next_open_return, next_close_return, intraday_high_return, hold_3d_return,
                     hold_5d_return, hold_10d_return, hold_20d_return,
                     max_gain_3d, max_drawdown_3d, hit_3pct, hit_5pct,
                     signal_next_close_return, signal_intraday_high_return, signal_hold_3d_return,
                     signal_hold_5d_return, signal_hold_10d_return, signal_hold_20d_return,
                     signal_max_gain_3d, signal_max_drawdown_3d, signal_hit_3pct, signal_hit_5pct,
                     exit_return, signal_exit_return, exit_reason, exit_days, exit_date,
                     updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal["id"],
                        signal["code"],
                        outcome["next_trade_date"],
                        outcome["future_days"],
                        outcome["next_open"],
                        outcome["next_high"],
                        outcome["next_low"],
                        outcome["next_close"],
                        outcome["next_open_return"],
                        outcome["next_close_return"],
                        outcome["intraday_high_return"],
                        outcome["hold_3d_return"],
                        outcome["hold_5d_return"],
                        outcome["hold_10d_return"],
                        outcome["hold_20d_return"],
                        outcome["max_gain_3d"],
                        outcome["max_drawdown_3d"],
                        int(outcome["hit_3pct"]),
                        int(outcome["hit_5pct"]),
                        outcome["signal_next_close_return"],
                        outcome["signal_intraday_high_return"],
                        outcome["signal_hold_3d_return"],
                        outcome["signal_hold_5d_return"],
                        outcome["signal_hold_10d_return"],
                        outcome["signal_hold_20d_return"],
                        outcome["signal_max_gain_3d"],
                        outcome["signal_max_drawdown_3d"],
                        int(outcome["signal_hit_3pct"]),
                        int(outcome["signal_hit_5pct"]),
                        outcome["exit_return"],
                        outcome["signal_exit_return"],
                        outcome["exit_reason"],
                        outcome["exit_days"],
                        outcome["exit_date"],
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
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.rank,
                       s.strategy_version, s.turnover, s.market,
                       COALESCE(o.signal_next_close_return, o.next_close_return) AS signal_next_close_return,
                       o.next_open_return,
                       o.next_close_return,
                       o.next_low,
	                       COALESCE(o.signal_intraday_high_return, o.intraday_high_return) AS signal_intraday_high_return,
	                       o.hold_3d_return,
	                       o.hold_5d_return,
	                       o.hold_10d_return,
	                       o.hold_20d_return,
	                       COALESCE(o.signal_hold_3d_return, o.hold_3d_return) AS signal_hold_3d_return,
                       COALESCE(o.signal_hold_5d_return, o.signal_hold_3d_return, o.hold_3d_return) AS signal_hold_5d_return,
                       COALESCE(o.signal_hold_10d_return, o.signal_hold_5d_return, o.signal_hold_3d_return, o.hold_3d_return) AS signal_hold_10d_return,
                       COALESCE(o.signal_hold_20d_return, o.signal_hold_10d_return, o.signal_hold_5d_return, o.signal_hold_3d_return, o.hold_3d_return) AS signal_hold_20d_return,
                       COALESCE(o.signal_exit_return, o.exit_return, o.signal_hold_3d_return, o.hold_3d_return) AS signal_exit_return,
                       COALESCE(o.signal_max_drawdown_3d, o.max_drawdown_3d) AS signal_max_drawdown_3d,
                       COALESCE(o.signal_hit_3pct, o.hit_3pct) AS signal_hit_3pct,
                       COALESCE(o.signal_hit_5pct, o.hit_5pct) AS signal_hit_5pct,
                       COALESCE(o.future_days, 1) AS future_days
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
        rows = [dict(row) for row in rows]
        selected_all = [row for row in rows if row["signal_date"] in dates]
        base_cost = coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25))
        if strategy_name:
            primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        else:
            primary_column, primary_days, primary_label = "strategy_primary_return", 0, "混合主周期"
        for row in selected_all:
            row_primary_column, row_primary_days, row_primary_label = _primary_return_config(
                strategy_name or row["strategy_name"]
            )
            row["_trade_cost_pct"] = _execution_cost_pct(row)
            row["_primary_return"] = coerce_number(row[row_primary_column])
            row["_primary_return_net"] = round(row["_primary_return"] - row["_trade_cost_pct"], 4)
            row["_exit_return"] = coerce_number(row.get("signal_exit_return"))
            row["_exit_return_net"] = round(row["_exit_return"] - row["_trade_cost_pct"], 4)
            row["_is_replay"] = _is_replay_version(row["strategy_version"])
            row["_primary_ready"] = int(row.get("future_days") or 1) >= row_primary_days
            row["_primary_holding_days"] = row_primary_days
            row["_primary_horizon_label"] = row_primary_label
        selected = [row for row in selected_all if row["_primary_ready"]]
        real_selected_all = [row for row in selected_all if not row["_is_replay"]]
        replay_selected_all = [row for row in selected_all if row["_is_replay"]]
        real_rows = [row for row in selected if not row["_is_replay"]]
        replay_rows = [row for row in selected if row["_is_replay"]]
        primary_rows = real_rows if strategy_name == "tomorrow_picks" else selected
        primary_outcome_rows = real_selected_all if strategy_name == "tomorrow_picks" else selected_all
        primary_dates = []
        for row in primary_rows:
            if row["signal_date"] not in primary_dates:
                primary_dates.append(row["signal_date"])
        metrics = {
            "sample_count": len(primary_rows),
            "outcome_sample_count": len(primary_outcome_rows),
            "total_sample_count": len(selected),
            "total_outcome_sample_count": len(selected_all),
            "real_sample_count": len(real_rows),
            "replay_sample_count": len(replay_rows),
            "real_outcome_sample_count": len(real_selected_all),
            "replay_outcome_sample_count": len(replay_selected_all),
            "day_count": len(primary_dates),
            "outcome_day_count": len(dates),
            "primary_sample_scope": "real_only" if strategy_name == "tomorrow_picks" else "all",
            "primary_return_field": primary_column,
            "primary_holding_days": primary_days,
            "primary_horizon_label": primary_label,
            "trade_cost_pct": base_cost,
            "avg_trade_cost_pct": _avg(row["_trade_cost_pct"] for row in primary_outcome_rows),
            "avg_next_close_return": _avg(row["signal_next_close_return"] for row in primary_outcome_rows),
            "win_rate_next_close": _rate(row["signal_next_close_return"] > 0 for row in primary_outcome_rows),
            "hit_3pct_rate": _rate(bool(row["signal_hit_3pct"]) for row in primary_outcome_rows),
            "hit_5pct_rate": _rate(bool(row["signal_hit_5pct"]) for row in primary_outcome_rows),
            "avg_intraday_high_return": _avg(row["signal_intraday_high_return"] for row in primary_outcome_rows),
            "avg_hold_3d_return": _avg(row["signal_hold_3d_return"] for row in primary_rows),
            "avg_hold_5d_return": _avg(row["signal_hold_5d_return"] for row in primary_rows),
            "avg_hold_10d_return": _avg(row["signal_hold_10d_return"] for row in primary_rows),
            "avg_hold_20d_return": _avg(row["signal_hold_20d_return"] for row in primary_rows),
            "avg_primary_return": _avg(row["_primary_return"] for row in primary_rows),
            "avg_primary_return_net": _avg(row["_primary_return_net"] for row in primary_rows),
            "win_rate_primary": _rate(row["_primary_return"] > 0 for row in primary_rows),
            "win_rate_primary_net": _rate(row["_primary_return_net"] > 0 for row in primary_rows),
            "avg_exit_return": _avg(row["_exit_return"] for row in primary_rows),
            "avg_exit_return_net": _avg(row["_exit_return_net"] for row in primary_rows),
            "win_rate_exit_net": _rate(row["_exit_return_net"] > 0 for row in primary_rows),
            "real_avg_primary_return_net": _avg(row["_primary_return_net"] for row in real_rows),
            "real_win_rate_primary_net": _rate(row["_primary_return_net"] > 0 for row in real_rows),
            "replay_avg_primary_return_net": _avg(row["_primary_return_net"] for row in replay_rows),
            "replay_win_rate_primary_net": _rate(row["_primary_return_net"] > 0 for row in replay_rows),
            "avg_max_drawdown_3d": _avg(row["signal_max_drawdown_3d"] for row in primary_rows),
            "top10_avg_next_close_return": _avg(
                row["signal_next_close_return"] for row in primary_outcome_rows if row["rank"] <= 10
            ),
            "avg_open_to_close_return": _avg(row["next_close_return"] for row in primary_outcome_rows),
            "next_day_compare": _next_day_compare(primary_outcome_rows),
            "replay_next_day_compare": _next_day_compare(replay_selected_all),
            "daily": _daily_metrics(primary_rows),
            "replay_daily": _daily_metrics(replay_rows),
        }
        return metrics

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
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
                    future_days INTEGER NOT NULL DEFAULT 1,
                    hold_3d_return REAL NOT NULL DEFAULT 0,
                    hold_5d_return REAL NOT NULL DEFAULT 0,
                    hold_10d_return REAL NOT NULL DEFAULT 0,
                    hold_20d_return REAL NOT NULL DEFAULT 0,
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
                "future_days": "INTEGER",
                "hold_5d_return": "REAL",
                "hold_10d_return": "REAL",
                "hold_20d_return": "REAL",
                "signal_hold_5d_return": "REAL",
                "signal_hold_10d_return": "REAL",
                "signal_hold_20d_return": "REAL",
                "signal_max_gain_3d": "REAL",
                "signal_max_drawdown_3d": "REAL",
                "signal_hit_3pct": "INTEGER",
                "signal_hit_5pct": "INTEGER",
                "exit_return": "REAL",
                "signal_exit_return": "REAL",
                "exit_reason": "TEXT",
                "exit_days": "INTEGER",
                "exit_date": "TEXT",
            }
            for column, column_type in outcome_columns.items():
                if column not in existing_columns:
                    conn.execute("ALTER TABLE strategy_outcomes ADD COLUMN {} {}".format(column, column_type))


def _compute_outcome(provider, signal: sqlite3.Row) -> Optional[Dict[str, object]]:
    history = provider.get_history(signal["code"], days=180)
    if history is None or history.empty or "trade_date" not in history.columns:
        return None
    df = history.sort_values("trade_date").reset_index(drop=True)
    df["prev_close"] = df["price"].shift(1)
    signal_date = str(signal["signal_date"]).replace("-", "")
    future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(drop=True)
    if future.empty:
        return None
    first = future.iloc[0]
    previous_rows = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) <= signal_date]
    previous_close = coerce_number(previous_rows.iloc[-1].get("price")) if not previous_rows.empty else coerce_number(first.get("prev_close"))
    limit_pct = _daily_limit_pct(str(signal["code"]), str(_mapping_get(signal, "market", "")))
    if _is_unbuyable_limit_up(first, previous_close, limit_pct):
        return {"excluded": True, "skip_reason": "unbuyable_limit_up"}
    open_entry = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    signal_entry = coerce_number(signal["price_at_signal"])
    close = coerce_number(first.get("price"))
    high = coerce_number(first.get("high"))
    low = coerce_number(first.get("low"))
    if open_entry <= 0:
        return None
    if signal_entry <= 0:
        signal_entry = open_entry
    future_days = len(future)
    window = future.head(3)
    last = window.iloc[-1]
    hold_3d_close = coerce_number(last.get("price"))
    hold_5d_close = _window_close(future, 5, close)
    hold_10d_close = _window_close(future, 10, hold_5d_close)
    hold_20d_close = _window_close(future, 20, hold_10d_close)
    max_high = max(coerce_number(value) for value in window.get("high", pd.Series([high])).tolist())
    min_low = min(coerce_number(value) for value in window.get("low", pd.Series([low])).tolist())
    _, primary_days, _ = _primary_return_config(str(signal["strategy_name"]))
    exit_policy = {"limit_down_pct": limit_pct}
    open_exit = simulate_exit(future, open_entry, holding_days=primary_days, policy=exit_policy)
    signal_exit = simulate_exit(future, signal_entry, holding_days=primary_days, policy=exit_policy)
    return {
        "next_trade_date": str(first.get("trade_date")),
        "future_days": future_days,
        "next_open": round(open_entry, 4),
        "next_high": round(high, 4),
        "next_low": round(low, 4),
        "next_close": round(close, 4),
        "next_open_return": round((open_entry / signal_entry - 1) * 100, 4),
        "next_close_return": round((close / open_entry - 1) * 100, 4) if close > 0 else 0.0,
        "intraday_high_return": round((high / open_entry - 1) * 100, 4) if high > 0 else 0.0,
        "hold_3d_return": round((hold_3d_close / open_entry - 1) * 100, 4) if hold_3d_close > 0 else 0.0,
        "hold_5d_return": round((hold_5d_close / open_entry - 1) * 100, 4) if hold_5d_close > 0 else 0.0,
        "hold_10d_return": round((hold_10d_close / open_entry - 1) * 100, 4) if hold_10d_close > 0 else 0.0,
        "hold_20d_return": round((hold_20d_close / open_entry - 1) * 100, 4) if hold_20d_close > 0 else 0.0,
        "max_gain_3d": round((max_high / open_entry - 1) * 100, 4) if max_high > 0 else 0.0,
        "max_drawdown_3d": round((min_low / open_entry - 1) * 100, 4) if min_low > 0 else 0.0,
        "hit_3pct": high / open_entry - 1 >= 0.03 if high > 0 else False,
        "hit_5pct": high / open_entry - 1 >= 0.05 if high > 0 else False,
        "signal_next_close_return": round((close / signal_entry - 1) * 100, 4) if close > 0 else 0.0,
        "signal_intraday_high_return": round((high / signal_entry - 1) * 100, 4) if high > 0 else 0.0,
        "signal_hold_3d_return": round((hold_3d_close / signal_entry - 1) * 100, 4) if hold_3d_close > 0 else 0.0,
        "signal_hold_5d_return": round((hold_5d_close / signal_entry - 1) * 100, 4) if hold_5d_close > 0 else 0.0,
        "signal_hold_10d_return": round((hold_10d_close / signal_entry - 1) * 100, 4) if hold_10d_close > 0 else 0.0,
        "signal_hold_20d_return": round((hold_20d_close / signal_entry - 1) * 100, 4) if hold_20d_close > 0 else 0.0,
        "signal_max_gain_3d": round((max_high / signal_entry - 1) * 100, 4) if max_high > 0 else 0.0,
        "signal_max_drawdown_3d": round((min_low / signal_entry - 1) * 100, 4) if min_low > 0 else 0.0,
        "signal_hit_3pct": high / signal_entry - 1 >= 0.03 if high > 0 else False,
        "signal_hit_5pct": high / signal_entry - 1 >= 0.05 if high > 0 else False,
        "exit_return": open_exit.get("exit_return", 0.0),
        "signal_exit_return": signal_exit.get("exit_return", open_exit.get("exit_return", 0.0)),
        "exit_reason": signal_exit.get("exit_reason", open_exit.get("exit_reason", "hold_to_term")),
        "exit_days": signal_exit.get("exit_days", open_exit.get("exit_days", 0)),
        "exit_date": signal_exit.get("exit_date", open_exit.get("exit_date", "")),
    }


def _window_close(future: pd.DataFrame, days: int, fallback: float) -> float:
    window = future.head(days)
    if window.empty:
        return coerce_number(fallback)
    return coerce_number(window.iloc[-1].get("price")) or coerce_number(fallback)


def _primary_return_config(strategy_name: str):
    if strategy_name == "tomorrow_picks":
        return PRIMARY_RETURN_BY_STRATEGY["tomorrow_picks"]
    if str(getattr(config, "VALIDATION_PRIMARY_ENTRY_MODE", "open")).lower() in ("open", "executable"):
        return EXECUTABLE_PRIMARY_RETURN_BY_STRATEGY.get(
            strategy_name,
            ("next_close_return", 1, "次日开盘入场"),
        )
    return PRIMARY_RETURN_BY_STRATEGY.get(
        strategy_name,
        ("signal_next_close_return", 1, "次日"),
    )


def _is_replay_version(strategy_version: str) -> bool:
    return "replay" in str(strategy_version or "").lower()


def _mapping_get(row, key: str, default=None):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _daily_limit_pct(code: str, market: str = "") -> float:
    normalized = normalize_code(code)
    market_text = str(market or "").lower()
    if normalized.startswith(("300", "301", "688")) or "创业" in market_text or "科创" in market_text:
        return 20.0
    return 10.0


def _is_unbuyable_limit_up(row, previous_close: float, limit_pct: float) -> bool:
    prev = coerce_number(previous_close)
    if prev <= 0:
        return False
    open_price = coerce_number(row.get("open")) or coerce_number(row.get("price"))
    high = coerce_number(row.get("high")) or coerce_number(row.get("price"))
    low = coerce_number(row.get("low")) or coerce_number(row.get("price"))
    close = coerce_number(row.get("price")) or coerce_number(row.get("close"))
    if min(open_price, high, low, close) <= 0:
        return False
    limit_price = prev * (1 + max(1.0, coerce_number(limit_pct, 10.0)) / 100.0)
    # 近似一字涨停/封板：全天最低价仍贴近涨停价，认为真实买单无法成交。
    return (
        open_price >= limit_price * 0.995
        and low >= limit_price * 0.995
        and high <= limit_price * 1.01
        and close >= limit_price * 0.995
    )


def _liquidity_slippage_pct(turnover: float) -> float:
    amount = coerce_number(turnover)
    if amount >= 1_000_000_000:
        return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.05), 0.05)
    if amount >= 300_000_000:
        return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.12), 0.12)
    if amount >= 100_000_000:
        return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.25), 0.25)
    return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.45), 0.45)


def _execution_cost_pct(row) -> float:
    base = coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25), 0.25)
    return round(base + _liquidity_slippage_pct(coerce_number(_mapping_get(row, "turnover"))), 4)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("reasons_json", "raw_json"):
        try:
            item[key.replace("_json", "")] = json.loads(item.get(key) or "[]")
        except Exception:
            item[key.replace("_json", "")] = [] if key == "reasons_json" else {}
    item["trade_cost_pct"] = _execution_cost_pct(item)
    return item


def _avg(values) -> float:
    clean = [coerce_number(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else 0.0


def _rate(values) -> float:
    clean = list(values)
    return round(sum(1 for value in clean if value) / len(clean) * 100, 2) if clean else 0.0


def _next_day_compare(rows: List[sqlite3.Row]) -> Dict[str, object]:
    return {
        "sample_count": len(rows),
        "avg_signal_to_next_open": _avg(row["next_open_return"] for row in rows),
        "avg_signal_to_next_close": _avg(row["signal_next_close_return"] for row in rows),
        "win_rate_signal_to_next_close": _rate(row["signal_next_close_return"] > 0 for row in rows),
        "avg_next_open_to_close": _avg(row["next_close_return"] for row in rows),
        "win_rate_next_open_to_close": _rate(row["next_close_return"] > 0 for row in rows),
        "avg_next_intraday_high_from_signal": _avg(row["signal_intraday_high_return"] for row in rows),
        "avg_next_intraday_low_from_signal": _avg(_next_low_return_from_signal(row) for row in rows),
        "hit_3pct_rate_from_signal": _rate(bool(row["signal_hit_3pct"]) for row in rows),
        "hit_5pct_rate_from_signal": _rate(bool(row["signal_hit_5pct"]) for row in rows),
        "avg_trade_cost_pct": _avg(row["_trade_cost_pct"] for row in rows),
        "avg_signal_to_next_close_net": _avg(row["_primary_return_net"] for row in rows),
    }


def _next_low_return_from_signal(row) -> float:
    entry = coerce_number(_mapping_get(row, "price_at_signal"))
    low = coerce_number(_mapping_get(row, "next_low"))
    if entry <= 0 or low <= 0:
        return 0.0
    return round((low / entry - 1.0) * 100.0, 4)


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
                "avg_primary_return": _avg(row["_primary_return"] for row in items),
                "avg_primary_return_net": _avg(row["_primary_return_net"] for row in items),
                "win_rate_primary": _rate(row["_primary_return"] > 0 for row in items),
                "win_rate_primary_net": _rate(row["_primary_return_net"] > 0 for row in items),
                "avg_exit_return": _avg(row["_exit_return"] for row in items),
                "avg_exit_return_net": _avg(row["_exit_return_net"] for row in items),
                "win_rate_exit_net": _rate(row["_exit_return_net"] > 0 for row in items),
                "real_sample_count": sum(1 for row in items if not row["_is_replay"]),
                "replay_sample_count": sum(1 for row in items if row["_is_replay"]),
            }
        )
    return sorted(daily, key=lambda item: item["signal_date"], reverse=True)

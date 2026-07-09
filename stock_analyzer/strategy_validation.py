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
    "tomorrow_picks": ("next_close_return", 1, "次日开盘入场"),
    "swing_picks": ("signal_hold_5d_return", 5, "5日"),
}

EXECUTABLE_PRIMARY_RETURN_BY_STRATEGY = {
    "short_term": ("next_close_return", 1, "次日开盘入场"),
    "tomorrow_picks": ("next_close_return", 1, "次日开盘入场"),
    "swing_picks": ("hold_5d_return", 5, "5日开盘入场"),
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
        deepseek_shadow_rows: Optional[Iterable[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        signal_date = signal_time[:10]
        rows = list(rows)
        deepseek_shadow_rows = list(deepseek_shadow_rows or [])
        saved = 0
        shadow_saved = 0
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_signal_batches
                (strategy_name, strategy_version, signal_date, signal_time, saved_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_name,
                    strategy_version,
                    signal_date,
                    signal_time,
                    len(rows),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            if "replay" in str(strategy_version or "").lower():
                old_ids = conn.execute(
                    """
                    SELECT id
                    FROM strategy_signals
                    WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                    """,
                    (strategy_name, strategy_version, signal_date),
                ).fetchall()
                delete_sql = """
                    DELETE FROM strategy_signals
                    WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                    """
                delete_params = (strategy_name, strategy_version, signal_date)
                old_shadow_ids = conn.execute(
                    """
                    SELECT id
                    FROM strategy_deepseek_shadow_signals
                    WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                    """,
                    (strategy_name, strategy_version, signal_date),
                ).fetchall()
                shadow_delete_sql = """
                    DELETE FROM strategy_deepseek_shadow_signals
                    WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                    """
                shadow_delete_params = (strategy_name, strategy_version, signal_date)
            else:
                old_ids = conn.execute(
                    """
                    SELECT id
                    FROM strategy_signals
                    WHERE strategy_name = ? AND signal_date = ? AND lower(strategy_version) NOT LIKE '%replay%'
                    """,
                    (strategy_name, signal_date),
                ).fetchall()
                delete_sql = """
                    DELETE FROM strategy_signals
                    WHERE strategy_name = ? AND signal_date = ? AND lower(strategy_version) NOT LIKE '%replay%'
                    """
                delete_params = (strategy_name, signal_date)
                old_shadow_ids = conn.execute(
                    """
                    SELECT id
                    FROM strategy_deepseek_shadow_signals
                    WHERE strategy_name = ? AND signal_date = ? AND lower(strategy_version) NOT LIKE '%replay%'
                    """,
                    (strategy_name, signal_date),
                ).fetchall()
                shadow_delete_sql = """
                    DELETE FROM strategy_deepseek_shadow_signals
                    WHERE strategy_name = ? AND signal_date = ? AND lower(strategy_version) NOT LIKE '%replay%'
                    """
                shadow_delete_params = (strategy_name, signal_date)
            if old_ids:
                conn.executemany(
                    "DELETE FROM strategy_execution_skips WHERE signal_id = ?",
                    [(row[0],) for row in old_ids],
                )
                conn.executemany(
                    "DELETE FROM strategy_outcomes WHERE signal_id = ?",
                    [(row[0],) for row in old_ids],
                )
                conn.execute(delete_sql, delete_params)
            if old_shadow_ids:
                conn.executemany(
                    "DELETE FROM strategy_deepseek_shadow_outcomes WHERE shadow_id = ?",
                    [(row[0],) for row in old_shadow_ids],
                )
                conn.execute(shadow_delete_sql, shadow_delete_params)
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
            for row in deepseek_shadow_rows:
                code = normalize_code(row.get("code"))
                if not code:
                    continue
                rank = int(coerce_number(row.get("rank"), coerce_number(row.get("local_rank"), 0)) or 0)
                local_rank = int(coerce_number(row.get("local_rank"), rank) or 0)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategy_deepseek_shadow_signals
                    (strategy_name, strategy_version, signal_date, signal_time, rank, local_rank, code, name,
                     market, theme, price_at_signal, pct_chg_at_signal, turnover, volume_ratio,
                     turnover_rate, sixty_day_pct, ytd_pct, score, deepseek_rank_score, deepseek_action,
                     deepseek_veto, deepseek_penalty, filter_reason, raw_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_name,
                        strategy_version,
                        signal_date,
                        signal_time,
                        rank,
                        local_rank,
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
                        coerce_number(row.get("deepseek_rank_score")),
                        str(row.get("deepseek_action") or ""),
                        1 if row.get("deepseek_veto") else 0,
                        coerce_number(row.get("deepseek_penalty")),
                        str(row.get("deepseek_filter_reason") or ""),
                        json.dumps(row, ensure_ascii=False),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                shadow_saved += 1
        return {
            "signal_date": signal_date,
            "saved": saved,
            "replaced": len(old_ids),
            "deepseek_shadow_saved": shadow_saved,
            "deepseek_shadow_replaced": len(old_shadow_ids),
        }

    def list_signal_dates(self, strategy_name: str = "") -> List[Dict[str, object]]:
        where = ""
        params = []
        if strategy_name:
            where = "WHERE b.strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            rows = conn.execute(
                """
                SELECT b.signal_date, b.strategy_name, COALESCE(COUNT(s.id), 0) AS count, MAX(b.signal_time) AS signal_time,
                       COALESCE(SUM(CASE WHEN s.id IS NOT NULL AND lower(s.strategy_version) LIKE '%replay%' THEN 0 WHEN s.id IS NOT NULL THEN 1 ELSE 0 END), 0) AS real_count,
                       COALESCE(SUM(CASE WHEN s.id IS NOT NULL AND lower(s.strategy_version) LIKE '%replay%' THEN 1 ELSE 0 END), 0) AS replay_count
                FROM strategy_signal_batches b
                LEFT JOIN strategy_signals s
                  ON s.strategy_name = b.strategy_name
                 AND s.signal_date = b.signal_date
                 AND s.strategy_version = b.strategy_version
                {}
                GROUP BY b.signal_date, b.strategy_name
                ORDER BY b.signal_date DESC, b.strategy_name ASC
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
                "real_count": row[4] or 0,
                "replay_count": row[5] or 0,
                "sample_type": (
                    "empty"
                    if not (row[2] or 0)
                    else "mixed"
                    if (row[4] or 0) and (row[5] or 0)
                    else ("replay" if (row[5] or 0) else "real")
                ),
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
                       o.updated_at AS outcome_updated_at,
                       k.skip_reason, k.updated_at AS skip_updated_at
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_skips k ON k.signal_id = s.id
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
                FROM strategy_signal_batches
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

    def prune_strategies(self, allowed_strategies: Iterable[str]) -> Dict[str, int]:
        allowed = [str(item) for item in allowed_strategies if str(item or "").strip()]
        if not allowed:
            return {"deleted_signals": 0, "deleted_batches": 0}
        placeholders = ",".join("?" for _ in allowed)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            old_ids = conn.execute(
                """
                SELECT id
                FROM strategy_signals
                WHERE strategy_name NOT IN ({})
                """.format(placeholders),
                allowed,
            ).fetchall()
            deleted_signals = len(old_ids)
            if old_ids:
                id_rows = [(row[0],) for row in old_ids]
                conn.executemany("DELETE FROM strategy_execution_skips WHERE signal_id = ?", id_rows)
                conn.executemany("DELETE FROM strategy_outcomes WHERE signal_id = ?", id_rows)
                conn.executemany("DELETE FROM strategy_signals WHERE id = ?", id_rows)
            batch_result = conn.execute(
                """
                DELETE FROM strategy_signal_batches
                WHERE strategy_name NOT IN ({})
                """.format(placeholders),
                allowed,
            )
            shadow_ids = conn.execute(
                """
                SELECT id
                FROM strategy_deepseek_shadow_signals
                WHERE strategy_name NOT IN ({})
                """.format(placeholders),
                allowed,
            ).fetchall()
            deleted_shadow = len(shadow_ids)
            if shadow_ids:
                id_rows = [(row[0],) for row in shadow_ids]
                conn.executemany("DELETE FROM strategy_deepseek_shadow_outcomes WHERE shadow_id = ?", id_rows)
                conn.executemany("DELETE FROM strategy_deepseek_shadow_signals WHERE id = ?", id_rows)
        return {
            "deleted_signals": deleted_signals,
            "deleted_batches": int(batch_result.rowcount or 0),
            "deleted_deepseek_shadow_signals": deleted_shadow,
        }

    def save_tuning_run(
        self,
        strategy_name: str,
        days: int,
        plan: Dict[str, object],
        metrics: Dict[str, object],
        deepseek_review: Dict[str, object],
    ) -> Dict[str, object]:
        now = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategy_tuning_runs
                (strategy_name, run_time, days, status, can_apply, shadow_mode,
                 plan_json, metrics_json, deepseek_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_name,
                    now,
                    int(days),
                    str(plan.get("status", "")),
                    1 if plan.get("can_apply") else 0,
                    1 if plan.get("shadow_mode") else 0,
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(metrics or {}, ensure_ascii=False),
                    json.dumps(deepseek_review or {}, ensure_ascii=False),
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
        return {"id": run_id, "run_time": now}

    def latest_tuning_run(self, strategy_name: str) -> Dict[str, object]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT *
                FROM strategy_tuning_runs
                WHERE strategy_name = ?
                ORDER BY run_time DESC, id DESC
                LIMIT 1
                """,
                (strategy_name,),
            ).fetchone()
        return _tuning_row_to_dict(row) if row else {}

    def list_tuning_runs(self, strategy_name: str, limit: int = 10) -> List[Dict[str, object]]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM strategy_tuning_runs
                WHERE strategy_name = ?
                ORDER BY run_time DESC, id DESC
                LIMIT ?
                """,
                (strategy_name, max(1, int(limit))),
            ).fetchall()
        return [_tuning_row_to_dict(row) for row in rows]

    def live_weight_samples(self, strategy_name: str, days: int = 120) -> List[Dict[str, object]]:
        primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.strategy_version, s.rank, s.code,
                       s.name, s.score, s.turnover, s.market, s.raw_json,
                       COALESCE(o.{primary_column}, 0) AS primary_return,
                       COALESCE(o.next_open_return, 0) AS next_open_return,
                       COALESCE(o.signal_max_drawdown_3d, o.max_drawdown_3d, 0) AS max_drawdown,
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
            if strategy_name == "tomorrow_picks" and not _is_primary_tomorrow_signal(row["rank"], raw):
                continue
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
                    "next_open_return": coerce_number(row["next_open_return"]),
                    "max_drawdown": coerce_number(row["max_drawdown"]),
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
        execution_skipped = 0
        for signal in signals:
            outcome = _compute_outcome(provider, signal)
            if outcome and outcome.get("excluded"):
                with sqlite3.connect(self.db_path, timeout=30) as conn:
                    conn.execute("DELETE FROM strategy_outcomes WHERE signal_id = ?", (signal["id"],))
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO strategy_execution_skips
                        (signal_id, code, skip_reason, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            signal["id"],
                            signal["code"],
                            str(outcome.get("skip_reason") or "excluded"),
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                skipped += 1
                execution_skipped += 1
                continue
            if not outcome:
                skipped += 1
                continue
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute("DELETE FROM strategy_execution_skips WHERE signal_id = ?", (signal["id"],))
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
        shadow = self.update_deepseek_shadow_outcomes(
            provider,
            signal_date=signal_date,
            strategy_name=strategy_name,
            codes=normalized_codes,
        )
        return {
            "updated": updated,
            "skipped": skipped,
            "execution_skipped": execution_skipped,
            "deepseek_shadow_updated": shadow["updated"],
            "deepseek_shadow_skipped": shadow["skipped"],
        }

    def update_deepseek_shadow_outcomes(
        self,
        provider,
        signal_date: str = "",
        strategy_name: str = "",
        codes: Optional[Iterable[str]] = None,
    ) -> Dict[str, int]:
        where = "WHERE 1=1"
        params: List[object] = []
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
            shadow_rows = conn.execute(
                "SELECT * FROM strategy_deepseek_shadow_signals {} ORDER BY signal_date DESC, local_rank ASC".format(where),
                params,
            ).fetchall()

        updated = 0
        skipped = 0
        for shadow in shadow_rows:
            outcome = _compute_outcome(provider, shadow)
            if not outcome or outcome.get("excluded"):
                skipped += 1
                continue
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategy_deepseek_shadow_outcomes
                    (shadow_id, code, next_trade_date, future_days, next_open, next_close,
                     next_close_return, hold_3d_return, hold_5d_return, hold_10d_return, hold_20d_return,
                     signal_next_close_return, signal_hold_3d_return, signal_hold_5d_return,
                     signal_hold_10d_return, signal_hold_20d_return, exit_return, signal_exit_return, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shadow["id"],
                        shadow["code"],
                        outcome["next_trade_date"],
                        outcome["future_days"],
                        outcome["next_open"],
                        outcome["next_close"],
                        outcome["next_close_return"],
                        outcome["hold_3d_return"],
                        outcome["hold_5d_return"],
                        outcome["hold_10d_return"],
                        outcome["hold_20d_return"],
                        outcome["signal_next_close_return"],
                        outcome["signal_hold_3d_return"],
                        outcome["signal_hold_5d_return"],
                        outcome["signal_hold_10d_return"],
                        outcome["signal_hold_20d_return"],
                        outcome["exit_return"],
                        outcome["signal_exit_return"],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            updated += 1
        return {"updated": updated, "skipped": skipped}

    def metrics(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        signal_status = self.signal_status_counts(strategy_name=strategy_name, days=days)
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
                       s.strategy_version, s.turnover, s.market, s.raw_json,
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
        execution_skipped_count = self.execution_skip_count(strategy_name=strategy_name, days=days)
        if not rows:
            return {
                "sample_count": 0,
                "execution_skipped_count": execution_skipped_count,
                **signal_status,
                "daily": [],
            }
        rows = [dict(row) for row in rows]
        window_rows = rows
        window_scope = "all"
        if strategy_name == "tomorrow_picks":
            window_scope = "mixed"
        dates = []
        for row in window_rows:
            if row["signal_date"] not in dates:
                dates.append(row["signal_date"])
            if len(dates) >= days:
                break
        selected_all = [row for row in rows if row["signal_date"] in dates]
        base_cost = coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25))
        if strategy_name:
            primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        else:
            primary_column, primary_days, primary_label = "strategy_primary_return", 0, "混合主周期"
        for row in selected_all:
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except Exception:
                raw = {}
            row["_raw"] = raw if isinstance(raw, dict) else {}
            row["_is_primary_tomorrow"] = _is_primary_tomorrow_signal(row.get("rank"), row["_raw"])
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
        if strategy_name == "tomorrow_picks":
            primary_rows = [row for row in selected if row["_is_primary_tomorrow"]]
            primary_outcome_rows = [row for row in selected_all if row["_is_primary_tomorrow"]]
        else:
            primary_rows = selected
            primary_outcome_rows = selected_all
        primary_dates = []
        for row in primary_rows:
            if row["signal_date"] not in primary_dates:
                primary_dates.append(row["signal_date"])
        metrics = {
            "sample_count": len(primary_rows),
            "outcome_sample_count": len(primary_outcome_rows),
            "total_sample_count": len(selected),
            "total_outcome_sample_count": len(selected_all),
            "backup_sample_count": len(selected) - len(primary_rows),
            "backup_outcome_sample_count": len(selected_all) - len(primary_outcome_rows),
            "real_sample_count": len(real_rows),
            "replay_sample_count": len(replay_rows),
            "real_outcome_sample_count": len(real_selected_all),
            "replay_outcome_sample_count": len(replay_selected_all),
            "day_count": len(primary_dates),
            "outcome_day_count": len(dates),
            "primary_sample_scope": "real_only" if strategy_name == "tomorrow_picks" and real_rows else "replay_only" if strategy_name == "tomorrow_picks" and replay_rows else "all",
            "window_scope": window_scope,
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
            "execution_skipped_count": execution_skipped_count,
            **signal_status,
        }
        return metrics

    def deepseek_attribution(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        strategy_name = str(strategy_name or "").strip()
        if not strategy_name:
            return {"status": "missing_strategy", "sample_count": 0, "days": int(days or 0)}
        primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            signal_rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.rank, s.strategy_version,
                       s.turnover, s.market, s.raw_json,
                       0 AS deepseek_shadow_signal,
                       COALESCE(o.{primary_column}, 0) AS primary_return,
                       COALESCE(o.future_days, 1) AS future_days
                FROM strategy_signals s
                JOIN strategy_outcomes o ON o.signal_id = s.id
                WHERE s.strategy_name = ?
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(primary_column=primary_column),
                (strategy_name,),
            ).fetchall()
            shadow_rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.rank, s.strategy_version,
                       s.turnover, s.market, s.raw_json,
                       1 AS deepseek_shadow_signal,
                       COALESCE(o.{primary_column}, 0) AS primary_return,
                       COALESCE(o.future_days, 1) AS future_days
                FROM strategy_deepseek_shadow_signals s
                JOIN strategy_deepseek_shadow_outcomes o ON o.shadow_id = s.id
                WHERE s.strategy_name = ?
                ORDER BY s.signal_date DESC, s.local_rank ASC
                """.format(primary_column=primary_column),
                (strategy_name,),
            ).fetchall()
        rows = list(signal_rows) + list(shadow_rows)
        rows.sort(
            key=lambda row: (
                str(row["signal_date"] or ""),
                -int(coerce_number(row["rank"], 0) or 0),
            ),
            reverse=True,
        )
        if not rows:
            result = {
                "status": "empty",
                "strategy": strategy_name,
                "days": int(days or 0),
                "sample_count": 0,
                "primary_horizon_label": primary_label,
            }
            _write_deepseek_attribution_snapshot(strategy_name, result)
            return result

        selected_dates: List[str] = []
        for row in rows:
            date = row["signal_date"]
            if date not in selected_dates:
                selected_dates.append(date)
            if len(selected_dates) >= max(1, int(days)):
                break

        selected: List[Dict[str, object]] = []
        for row in rows:
            if row["signal_date"] not in selected_dates:
                continue
            item = dict(row)
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except Exception:
                raw = {}
            item["_raw"] = raw if isinstance(raw, dict) else {}
            item["_is_replay"] = _is_replay_version(item["strategy_version"])
            item["_primary_ready"] = int(item.get("future_days") or 1) >= primary_days
            item["_is_primary_tomorrow"] = _is_primary_tomorrow_signal(item.get("rank"), item["_raw"])
            item["_trade_cost_pct"] = _execution_cost_pct(item)
            item["_primary_return"] = coerce_number(item.get("primary_return"))
            item["_primary_return_net"] = round(item["_primary_return"] - item["_trade_cost_pct"], 4)
            item["_deepseek_shadow_signal"] = bool(item.get("deepseek_shadow_signal"))
            selected.append(item)

        primary_rows = [row for row in selected if row["_primary_ready"]]
        if strategy_name == "tomorrow_picks":
            primary_rows = [row for row in primary_rows if row["_is_primary_tomorrow"]]
        attribution_rows = [row for row in primary_rows if _has_deepseek_review(row.get("_raw"))]
        real_rows = [row for row in attribution_rows if not row["_is_replay"]]
        replay_rows = [row for row in attribution_rows if row["_is_replay"]]
        covered_rows = [row for row in attribution_rows if _deepseek_covered(row.get("_raw"))]
        shadow_rows = [row for row in attribution_rows if row.get("_deepseek_shadow_signal")]
        selected_rows = [row for row in attribution_rows if not row.get("_deepseek_shadow_signal")]
        avoid_veto_rows = [row for row in attribution_rows if _deepseek_avoid_or_veto(row.get("_raw"))]
        priority_rows = [row for row in attribution_rows if _deepseek_action(row.get("_raw")) == "priority"]
        watch_rows = [row for row in attribution_rows if _deepseek_action(row.get("_raw")) == "watch"]
        min_real_samples = 10
        status = "ok"
        if not attribution_rows:
            status = "no_deepseek_samples"
        elif len(real_rows) < min_real_samples:
            status = "insufficient_real_samples"
        counterfactual = _deepseek_counterfactual_topn(strategy_name, attribution_rows)
        priority_vs_watch = _deepseek_group_delta(priority_rows, watch_rows)
        result = {
            "status": status,
            "strategy": strategy_name,
            "days": int(days or 0),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "primary_horizon_label": primary_label,
            "min_real_samples": min_real_samples,
            "sample_count": len(attribution_rows),
            "real_sample_count": len(real_rows),
            "replay_sample_count": len(replay_rows),
            "covered_sample_count": len(covered_rows),
            "selected_sample_count": len(selected_rows),
            "shadow_sample_count": len(shadow_rows),
            "covered_ratio_pct": round(len(covered_rows) / len(attribution_rows) * 100, 2) if attribution_rows else 0.0,
            "local_rank_sample_count": sum(1 for row in attribution_rows if _deepseek_local_rank(row) > 0),
            "reordered_sample_count": sum(1 for row in attribution_rows if _deepseek_local_rank(row) > 0 and _deepseek_local_rank(row) != int(row.get("rank") or 0)),
            "blend_alpha_avg": _avg(_deepseek_blend_alpha(row.get("_raw")) for row in attribution_rows if _deepseek_blend_alpha(row.get("_raw")) is not None),
            "avoid_veto": _return_summary(avoid_veto_rows),
            "shadow_avoid_veto": _return_summary([row for row in avoid_veto_rows if row.get("_deepseek_shadow_signal")]),
            "priority": _return_summary(priority_rows),
            "watch": _return_summary(watch_rows),
            "priority_vs_watch": priority_vs_watch,
            "counterfactual_topn": counterfactual,
            "notes": [
                "avoid/veto 包含正式入选与 DeepSeek gate 剔除后的 shadow 候选；正式策略胜率仍只按入选信号计算。",
            ],
        }
        _write_deepseek_attribution_snapshot(strategy_name, result)
        return result

    def signal_status_counts(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        where = "WHERE 1=1"
        params: List[object] = []
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            dates = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT DISTINCT signal_date
                    FROM strategy_signals
                    {}
                    ORDER BY signal_date DESC
                    LIMIT ?
                    """.format(where),
                    [*params, max(1, int(days))],
                ).fetchall()
            ]
            if not dates:
                return {
                    "signal_sample_count": 0,
                    "pending_outcome_count": 0,
                    "outcome_coverage_pct": None,
                }
            placeholders = ",".join("?" for _ in dates)
            count_where = "WHERE s.signal_date IN ({})".format(placeholders)
            count_params: List[object] = list(dates)
            if strategy_name:
                count_where += " AND s.strategy_name = ?"
                count_params.append(strategy_name)
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS signal_count,
                  SUM(CASE WHEN o.signal_id IS NULL AND k.signal_id IS NULL THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN o.signal_id IS NOT NULL THEN 1 ELSE 0 END) AS outcome_count
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_skips k ON k.signal_id = s.id
                {}
                """.format(count_where),
                count_params,
            ).fetchone()
        signal_count = int((row[0] if row else 0) or 0)
        pending_count = int((row[1] if row else 0) or 0)
        outcome_count = int((row[2] if row else 0) or 0)
        coverage = round(outcome_count / signal_count * 100.0, 2) if signal_count > 0 else None
        return {
            "signal_sample_count": signal_count,
            "pending_outcome_count": pending_count,
            "outcome_coverage_pct": coverage,
        }

    def execution_skip_count(self, strategy_name: str = "", days: int = 20) -> int:
        where = "WHERE 1=1"
        params: List[object] = []
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT s.signal_date
                FROM strategy_signals s
                JOIN strategy_execution_skips k ON k.signal_id = s.id
                {}
                ORDER BY s.signal_date DESC
                LIMIT ?
                """.format(where),
                [*params, max(1, int(days))],
            ).fetchall()
            dates = [row[0] for row in rows]
            if not dates:
                return 0
            placeholders = ",".join("?" for _ in dates)
            count_where = "WHERE s.signal_date IN ({})".format(placeholders)
            count_params: List[object] = list(dates)
            if strategy_name:
                count_where += " AND s.strategy_name = ?"
                count_params.append(strategy_name)
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM strategy_signals s
                JOIN strategy_execution_skips k ON k.signal_id = s.id
                {}
                """.format(count_where),
                count_params,
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def save_market_gate_review(self, market_gate: Dict[str, object], market_filter: str = "all") -> Dict[str, object]:
        if not isinstance(market_gate, dict) or not market_gate.get("enabled"):
            return {"saved": 0, "status": "disabled"}
        now = str(market_gate.get("generated_at") or datetime.now().isoformat(timespec="seconds"))
        review_date = now[:10]
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                """
                INSERT INTO deepseek_market_gate_reviews
                (review_date, review_time, market_filter, regime, size_factor, confidence, status, source, reason,
                 context_json, result_json, counts_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_date, market_filter) DO UPDATE SET
                  review_time=excluded.review_time,
                  regime=excluded.regime,
                  size_factor=excluded.size_factor,
                  confidence=excluded.confidence,
                  status=excluded.status,
                  source=excluded.source,
                  reason=excluded.reason,
                  context_json=excluded.context_json,
                  result_json=excluded.result_json,
                  counts_json=excluded.counts_json,
                  created_at=excluded.created_at
                """,
                (
                    review_date,
                    now,
                    str(market_filter or "all"),
                    str(market_gate.get("regime") or ""),
                    coerce_number(market_gate.get("size_factor"), 1.0),
                    coerce_number(market_gate.get("confidence"), 0.0),
                    str(market_gate.get("status") or ""),
                    str(market_gate.get("source") or ""),
                    str(market_gate.get("reason") or "")[:500],
                    json.dumps(market_gate.get("context") or {}, ensure_ascii=False),
                    json.dumps(market_gate, ensure_ascii=False),
                    json.dumps(market_gate.get("counts") or {}, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        return {"saved": 1, "status": "saved", "review_date": review_date}

    def market_gate_metrics(self, days: int = 120) -> Dict[str, object]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            reviews = conn.execute(
                """
                SELECT *
                FROM deepseek_market_gate_reviews
                ORDER BY review_date DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(days)),),
            ).fetchall()
            if not reviews:
                return {"sample_count": 0, "outcome_sample_count": 0, "hit_rate": 0.0, "by_regime": {}}
            review_dates = [row["review_date"] for row in reviews]
            placeholders = ",".join("?" for _ in review_dates)
            outcome_rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.strategy_version, s.rank, s.turnover, s.market, s.raw_json,
                       COALESCE(o.next_close_return, 0) AS next_close_return,
                       COALESCE(o.signal_next_close_return, o.next_close_return, 0) AS signal_next_close_return,
                       COALESCE(o.hold_3d_return, 0) AS hold_3d_return,
                       COALESCE(o.hold_5d_return, o.hold_3d_return, 0) AS hold_5d_return,
                       COALESCE(o.hold_10d_return, o.hold_5d_return, o.hold_3d_return, 0) AS hold_10d_return,
                       COALESCE(o.hold_20d_return, o.hold_10d_return, o.hold_5d_return, o.hold_3d_return, 0) AS hold_20d_return,
                       COALESCE(o.signal_hold_3d_return, o.hold_3d_return, 0) AS signal_hold_3d_return,
                       COALESCE(o.signal_hold_5d_return, o.signal_hold_3d_return, o.hold_3d_return, 0) AS signal_hold_5d_return,
                       COALESCE(o.signal_hold_10d_return, o.signal_hold_5d_return, o.signal_hold_3d_return, o.hold_3d_return, 0) AS signal_hold_10d_return,
                       COALESCE(o.signal_hold_20d_return, o.signal_hold_10d_return, o.signal_hold_5d_return, o.signal_hold_3d_return, 0) AS signal_hold_20d_return,
                       COALESCE(o.future_days, 1) AS future_days
                FROM strategy_signals s
                JOIN strategy_outcomes o ON o.signal_id = s.id
                WHERE s.signal_date IN ({})
                """.format(placeholders),
                review_dates,
            ).fetchall()

        outcome_by_date: Dict[str, List[float]] = {}
        for row in outcome_rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except Exception:
                raw = {}
            if row["strategy_name"] == "tomorrow_picks" and not _is_primary_tomorrow_signal(row["rank"], raw):
                continue
            primary_column, primary_days, _ = _primary_return_config(row["strategy_name"])
            if int(row["future_days"] or 1) < primary_days:
                continue
            outcome_by_date.setdefault(str(row["signal_date"]), []).append(
                round(coerce_number(row[primary_column]) - _execution_cost_pct(row), 4)
            )

        review_items = []
        by_regime: Dict[str, List[Dict[str, object]]] = {}
        for review in reviews:
            returns = outcome_by_date.get(str(review["review_date"]), [])
            outcome = _market_gate_outcome_summary(returns)
            item = {
                "review_date": review["review_date"],
                "market_filter": review["market_filter"],
                "regime": review["regime"],
                "size_factor": coerce_number(review["size_factor"], 1.0),
                "confidence": coerce_number(review["confidence"], 0.0),
                "status": review["status"],
                "source": review["source"],
                "reason": review["reason"],
                **outcome,
            }
            item["hit"] = _market_gate_hit(str(review["regime"] or ""), outcome.get("actual_regime", "unknown"))
            review_items.append(item)
            by_regime.setdefault(str(review["regime"] or "unknown"), []).append(item)
        outcome_items = [item for item in review_items if item["outcome_sample_count"] > 0 and item["hit"] is not None]
        return {
            "sample_count": len(review_items),
            "outcome_sample_count": len(outcome_items),
            "hit_rate": _rate(item["hit"] for item in outcome_items),
            "by_regime": {
                regime: {
                    "sample_count": len(items),
                    "outcome_sample_count": sum(1 for item in items if item["outcome_sample_count"] > 0),
                    "avg_primary_return_net": _avg(
                        item.get("avg_primary_return_net") for item in items if item["outcome_sample_count"] > 0
                    ),
                    "hit_rate": _rate(item["hit"] for item in items if item["hit"] is not None),
                }
                for regime, items in by_regime.items()
            },
            "recent": review_items[:20],
        }

    def save_stock_prediction_snapshot(self, payload: Dict[str, object]) -> Dict[str, object]:
        optimization = payload.get("optimization") or {}
        if not isinstance(optimization, dict) or not optimization:
            return {"saved": 0, "status": "missing_optimization"}
        code = normalize_code(payload.get("code"))
        if not code:
            return {"saved": 0, "status": "missing_code"}
        now = datetime.now().isoformat(timespec="seconds")
        prediction_date = now[:10]
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                """
                INSERT INTO stock_prediction_snapshots
                (prediction_date, prediction_time, code, name, price_at_signal, stance, bias, timing,
                 optimization_json, prediction_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prediction_date, code) DO UPDATE SET
                  prediction_time=excluded.prediction_time,
                  name=excluded.name,
                  price_at_signal=excluded.price_at_signal,
                  stance=excluded.stance,
                  bias=excluded.bias,
                  timing=excluded.timing,
                  optimization_json=excluded.optimization_json,
                  prediction_json=excluded.prediction_json,
                  created_at=excluded.created_at
                """,
                (
                    prediction_date,
                    now,
                    code,
                    str(payload.get("name") or ""),
                    coerce_number(payload.get("price")),
                    str(optimization.get("stance") or ""),
                    str(optimization.get("bias") or ""),
                    str(optimization.get("timing") or ""),
                    json.dumps(optimization, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
        return {"saved": 1, "status": "saved", "prediction_date": prediction_date, "code": code}

    def update_stock_prediction_outcomes(self, provider, days: int = 120) -> Dict[str, object]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.*
                FROM stock_prediction_snapshots s
                LEFT JOIN stock_prediction_outcomes o ON o.snapshot_id = s.id
                WHERE o.snapshot_id IS NULL
                ORDER BY s.prediction_date DESC, s.id DESC
                LIMIT ?
                """,
                (max(1, int(days)),),
            ).fetchall()
        updated = 0
        skipped = 0
        for row in rows:
            outcome = _compute_stance_outcome(provider, row)
            if not outcome:
                skipped += 1
                continue
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stock_prediction_outcomes
                    (snapshot_id, code, next_trade_date, future_days, next_open, next_close,
                     next_close_return, exit_return, exit_reason, exit_days, exit_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["code"],
                        outcome["next_trade_date"],
                        outcome["future_days"],
                        outcome["next_open"],
                        outcome["next_close"],
                        outcome["next_close_return"],
                        outcome["exit_return"],
                        outcome["exit_reason"],
                        outcome["exit_days"],
                        outcome["exit_date"],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            updated += 1
        return {"updated": updated, "skipped": skipped}

    def stance_metrics(self, days: int = 120) -> Dict[str, object]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.prediction_date, s.code, s.name, s.stance, s.bias, s.timing,
                       o.next_close_return, o.exit_return, o.exit_reason, o.future_days
                FROM stock_prediction_snapshots s
                JOIN stock_prediction_outcomes o ON o.snapshot_id = s.id
                ORDER BY s.prediction_date DESC, s.id DESC
                LIMIT ?
                """,
                (max(1, int(days)) * 20,),
            ).fetchall()
        groups: Dict[str, List[Dict[str, object]]] = {}
        for row in rows:
            item = dict(row)
            groups.setdefault(str(item.get("stance") or "unknown"), []).append(item)
        return {
            "sample_count": len(rows),
            "by_stance": {
                stance: {
                    "sample_count": len(items),
                    "avg_next_close_return": _avg(item.get("next_close_return") for item in items),
                    "win_rate_next_close": _rate(coerce_number(item.get("next_close_return")) > 0 for item in items),
                    "avg_exit_return": _avg(item.get("exit_return") for item in items),
                    "win_rate_exit": _rate(coerce_number(item.get("exit_return")) > 0 for item in items),
                }
                for stance, items in groups.items()
            },
        }

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_signal_batches (
                    strategy_name TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    saved_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_name, signal_date, strategy_version)
                )
                """
            )
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_execution_skips (
                    signal_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    skip_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES strategy_signals(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_deepseek_shadow_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    rank INTEGER NOT NULL DEFAULT 0,
                    local_rank INTEGER NOT NULL DEFAULT 0,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    theme TEXT NOT NULL DEFAULT '',
                    price_at_signal REAL NOT NULL DEFAULT 0,
                    pct_chg_at_signal REAL NOT NULL DEFAULT 0,
                    turnover REAL NOT NULL DEFAULT 0,
                    volume_ratio REAL NOT NULL DEFAULT 0,
                    turnover_rate REAL NOT NULL DEFAULT 0,
                    sixty_day_pct REAL NOT NULL DEFAULT 0,
                    ytd_pct REAL NOT NULL DEFAULT 0,
                    score REAL NOT NULL DEFAULT 0,
                    deepseek_rank_score REAL NOT NULL DEFAULT 0,
                    deepseek_action TEXT NOT NULL DEFAULT '',
                    deepseek_veto INTEGER NOT NULL DEFAULT 0,
                    deepseek_penalty REAL NOT NULL DEFAULT 0,
                    filter_reason TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_name, strategy_version, signal_date, code)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_deepseek_shadow_outcomes (
                    shadow_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    next_trade_date TEXT NOT NULL,
                    future_days INTEGER NOT NULL DEFAULT 1,
                    next_open REAL NOT NULL DEFAULT 0,
                    next_close REAL NOT NULL DEFAULT 0,
                    next_close_return REAL NOT NULL DEFAULT 0,
                    hold_3d_return REAL NOT NULL DEFAULT 0,
                    hold_5d_return REAL NOT NULL DEFAULT 0,
                    hold_10d_return REAL NOT NULL DEFAULT 0,
                    hold_20d_return REAL NOT NULL DEFAULT 0,
                    signal_next_close_return REAL NOT NULL DEFAULT 0,
                    signal_hold_3d_return REAL NOT NULL DEFAULT 0,
                    signal_hold_5d_return REAL NOT NULL DEFAULT 0,
                    signal_hold_10d_return REAL NOT NULL DEFAULT 0,
                    signal_hold_20d_return REAL NOT NULL DEFAULT 0,
                    exit_return REAL NOT NULL DEFAULT 0,
                    signal_exit_return REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(shadow_id) REFERENCES strategy_deepseek_shadow_signals(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deepseek_market_gate_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_date TEXT NOT NULL,
                    review_time TEXT NOT NULL,
                    market_filter TEXT NOT NULL DEFAULT 'all',
                    regime TEXT NOT NULL DEFAULT '',
                    size_factor REAL NOT NULL DEFAULT 1,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    counts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(review_date, market_filter)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_signals_date ON strategy_signals(signal_date)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_date ON strategy_signals(strategy_name, signal_date DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_date_rank ON strategy_signals(strategy_name, signal_date DESC, rank)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy_version_date ON strategy_signals(strategy_name, strategy_version, signal_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_signal_batches_strategy_date ON strategy_signal_batches(strategy_name, signal_date DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_outcomes_code ON strategy_outcomes(code)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_execution_skips_code ON strategy_execution_skips(code)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deepseek_shadow_strategy_date ON strategy_deepseek_shadow_signals(strategy_name, signal_date DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deepseek_shadow_code ON strategy_deepseek_shadow_signals(code)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deepseek_market_gate_date ON deepseek_market_gate_reviews(review_date DESC)"
            )
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
            conn.execute(
                """
                INSERT OR IGNORE INTO strategy_signal_batches
                (strategy_name, strategy_version, signal_date, signal_time, saved_count, created_at)
                SELECT strategy_name, strategy_version, signal_date, MAX(signal_time), COUNT(*), MIN(created_at)
                FROM strategy_signals
                GROUP BY strategy_name, strategy_version, signal_date
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_tuning_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    run_time TEXT NOT NULL,
                    days INTEGER NOT NULL DEFAULT 20,
                    status TEXT NOT NULL DEFAULT '',
                    can_apply INTEGER NOT NULL DEFAULT 0,
                    shadow_mode INTEGER NOT NULL DEFAULT 1,
                    plan_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    deepseek_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_tuning_runs_strategy_time ON strategy_tuning_runs(strategy_name, run_time DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_prediction_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_date TEXT NOT NULL,
                    prediction_time TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    price_at_signal REAL NOT NULL DEFAULT 0,
                    stance TEXT NOT NULL DEFAULT '',
                    bias TEXT NOT NULL DEFAULT '',
                    timing TEXT NOT NULL DEFAULT '',
                    optimization_json TEXT NOT NULL,
                    prediction_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(prediction_date, code)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_prediction_outcomes (
                    snapshot_id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    next_trade_date TEXT NOT NULL,
                    future_days INTEGER NOT NULL DEFAULT 1,
                    next_open REAL NOT NULL DEFAULT 0,
                    next_close REAL NOT NULL DEFAULT 0,
                    next_close_return REAL NOT NULL DEFAULT 0,
                    exit_return REAL NOT NULL DEFAULT 0,
                    exit_reason TEXT NOT NULL DEFAULT '',
                    exit_days INTEGER NOT NULL DEFAULT 0,
                    exit_date TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES stock_prediction_snapshots(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stock_prediction_snapshots_date ON stock_prediction_snapshots(prediction_date DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stock_prediction_snapshots_code ON stock_prediction_snapshots(code)"
            )


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
    if str(_mapping_get(signal, "strategy_name", "")) == "tomorrow_picks":
        high_open_pct = (open_entry / signal_entry - 1.0) * 100.0 if signal_entry > 0 else 0.0
        high_open_skip_pct = coerce_number(getattr(config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 3.0), 3.0)
        if high_open_pct > high_open_skip_pct:
            return {
                "excluded": True,
                "skip_reason": "tomorrow_high_open_chase",
                "next_open_return": round(high_open_pct, 4),
                "threshold_pct": round(high_open_skip_pct, 4),
            }
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


def _compute_stance_outcome(provider, snapshot: sqlite3.Row) -> Optional[Dict[str, object]]:
    try:
        history = provider.get_history(snapshot["code"], days=180)
    except Exception:
        return None
    if history is None or history.empty or "trade_date" not in history.columns:
        return None
    df = history.sort_values("trade_date").reset_index(drop=True)
    signal_date = str(snapshot["prediction_date"]).replace("-", "")
    future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(drop=True)
    if future.empty:
        return None
    first = future.iloc[0]
    entry = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    if entry <= 0:
        entry = coerce_number(snapshot["price_at_signal"])
    close = coerce_number(first.get("price")) or coerce_number(first.get("close"))
    if entry <= 0 or close <= 0:
        return None
    try:
        optimization = json.loads(snapshot["optimization_json"] or "{}")
    except Exception:
        optimization = {}
    holding_days = max(1, int(getattr(config, "STANCE_TRACKING_HOLDING_DAYS", 5)))
    policy = _stance_exit_policy(optimization, holding_days)
    exit_result = simulate_exit(future, entry, holding_days=holding_days, policy=policy)
    return {
        "next_trade_date": str(first.get("trade_date")),
        "future_days": len(future),
        "next_open": round(entry, 4),
        "next_close": round(close, 4),
        "next_close_return": round((close / entry - 1) * 100, 4),
        "exit_return": coerce_number(exit_result.get("exit_return")),
        "exit_reason": str(exit_result.get("exit_reason") or ""),
        "exit_days": int(exit_result.get("exit_days") or 0),
        "exit_date": str(exit_result.get("exit_date") or ""),
    }


def _stance_exit_policy(optimization: Dict[str, object], holding_days: int) -> Dict[str, object]:
    policy = {"holding_days": holding_days}
    for source_key, target_key in (
        ("stop_loss_pct", "stop_loss_pct"),
        ("take_profit_pct", "take_profit_pct"),
        ("trailing_stop_pct", "trailing_stop_pct"),
    ):
        value = optimization.get(source_key) if isinstance(optimization, dict) else None
        number = coerce_number(value, 0.0)
        if number > 0:
            policy[target_key] = number
    return policy


def _window_close(future: pd.DataFrame, days: int, fallback: float) -> float:
    window = future.head(days)
    if window.empty:
        return coerce_number(fallback)
    return coerce_number(window.iloc[-1].get("price")) or coerce_number(fallback)


def _primary_return_config(strategy_name: str):
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


def _is_primary_tomorrow_signal(rank, raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        raw = {}
    tier = str(raw.get("tier") or "").strip()
    if tier:
        return tier == "primary_watch"
    return int(coerce_number(rank)) <= int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 10))


def _row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("reasons_json", "raw_json"):
        try:
            item[key.replace("_json", "")] = json.loads(item.get(key) or "[]")
        except Exception:
            item[key.replace("_json", "")] = [] if key == "reasons_json" else {}
    item["trade_cost_pct"] = _execution_cost_pct(item)
    return item


def _tuning_row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("plan_json", "metrics_json", "deepseek_json"):
        target = key.replace("_json", "")
        try:
            item[target] = json.loads(item.get(key) or "{}")
        except Exception:
            item[target] = {}
        item.pop(key, None)
    item["can_apply"] = bool(item.get("can_apply"))
    item["shadow_mode"] = bool(item.get("shadow_mode"))
    return item


def _avg(values) -> float:
    clean = [coerce_number(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else 0.0


def _rate(values) -> float:
    clean = list(values)
    return round(sum(1 for value in clean if value) / len(clean) * 100, 2) if clean else 0.0


def _deepseek_action(raw: Dict[str, object]) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("deepseek_action") or "").strip().lower()


def _has_deepseek_review(raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        return False
    return any(
        key in raw
        for key in (
            "deepseek_action",
            "deepseek_veto",
            "deepseek_penalty",
            "deepseek_rank_score",
            "deepseek_score",
            "rerank_source",
        )
    )


def _deepseek_covered(raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        return False
    if "deepseek_covered" in raw:
        return bool(raw.get("deepseek_covered"))
    return raw.get("deepseek_score") is not None or str(raw.get("rerank_source") or "") == "deepseek"


def _deepseek_avoid_or_veto(raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("deepseek_veto")) or _deepseek_action(raw) == "avoid"


def _deepseek_local_rank(row: Dict[str, object]) -> int:
    raw = row.get("_raw") if isinstance(row, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return int(coerce_number(raw.get("local_rank"), 0) or 0)


def _deepseek_blend_alpha(raw: Dict[str, object]):
    if not isinstance(raw, dict):
        return None
    if "deepseek_blend_alpha" in raw:
        return coerce_number(raw.get("deepseek_blend_alpha"))
    if "blend_alpha" in raw:
        return coerce_number(raw.get("blend_alpha"))
    return None


def _return_summary(rows: List[Dict[str, object]]) -> Dict[str, object]:
    return {
        "sample_count": len(rows),
        "avg_primary_return_net": _avg(row.get("_primary_return_net") for row in rows),
        "win_rate_primary_net": _rate(coerce_number(row.get("_primary_return_net")) > 0 for row in rows),
    }


def _market_gate_outcome_summary(returns: List[float]) -> Dict[str, object]:
    clean = [coerce_number(value) for value in returns]
    avg_return = _avg(clean)
    win_rate = _rate(value > 0 for value in clean)
    if not clean:
        actual_regime = "unknown"
    elif avg_return < 0 or win_rate < 45:
        actual_regime = "risk_off"
    elif avg_return > 0.3 and win_rate >= 55:
        actual_regime = "risk_on"
    else:
        actual_regime = "balanced"
    return {
        "outcome_sample_count": len(clean),
        "avg_primary_return_net": avg_return,
        "win_rate_primary_net": win_rate,
        "actual_regime": actual_regime,
    }


def _market_gate_hit(expected_regime: str, actual_regime: str):
    expected = str(expected_regime or "").strip().lower()
    actual = str(actual_regime or "").strip().lower()
    if actual == "unknown" or expected not in {"risk_on", "balanced", "risk_off"}:
        return None
    if expected == "balanced":
        return actual == "balanced"
    return expected == actual


def _deepseek_group_delta(
    priority_rows: List[Dict[str, object]],
    watch_rows: List[Dict[str, object]],
) -> Dict[str, object]:
    priority = _return_summary(priority_rows)
    watch = _return_summary(watch_rows)
    return {
        "priority_sample_count": priority["sample_count"],
        "watch_sample_count": watch["sample_count"],
        "priority_win_rate_primary_net": priority["win_rate_primary_net"],
        "watch_win_rate_primary_net": watch["win_rate_primary_net"],
        "win_rate_delta_pct": round(priority["win_rate_primary_net"] - watch["win_rate_primary_net"], 2),
        "avg_return_delta_pct": round(priority["avg_primary_return_net"] - watch["avg_primary_return_net"], 4),
    }


def _deepseek_counterfactual_topn(strategy_name: str, rows: List[Dict[str, object]]) -> Dict[str, object]:
    rows_with_local_rank = [row for row in rows if _deepseek_local_rank(row) > 0]
    if not rows_with_local_rank:
        return {
            "sample_count": 0,
            "day_count": 0,
            "top_n": 0,
            "local_avg_primary_return_net": 0.0,
            "deepseek_avg_primary_return_net": 0.0,
            "avg_return_delta_pct": 0.0,
            "local_win_rate_primary_net": 0.0,
            "deepseek_win_rate_primary_net": 0.0,
            "win_rate_delta_pct": 0.0,
            "status": "missing_local_rank",
        }
    top_n = _deepseek_counterfactual_n(strategy_name)
    by_date: Dict[str, List[Dict[str, object]]] = {}
    for row in rows_with_local_rank:
        by_date.setdefault(str(row.get("signal_date") or ""), []).append(row)
    local_selected: List[Dict[str, object]] = []
    deepseek_selected: List[Dict[str, object]] = []
    for date_rows in by_date.values():
        selected_rows = [row for row in date_rows if not row.get("_deepseek_shadow_signal")]
        count = min(top_n, len(date_rows))
        selected_count = min(top_n, len(selected_rows))
        if count <= 0:
            continue
        local_selected.extend(
            sorted(date_rows, key=lambda item: (_deepseek_local_rank(item), int(item.get("rank") or 9999)))[:count]
        )
        deepseek_selected.extend(sorted(selected_rows, key=lambda item: int(item.get("rank") or 9999))[:selected_count])
    local_summary = _return_summary(local_selected)
    deepseek_summary = _return_summary(deepseek_selected)
    return {
        "sample_count": len(deepseek_selected),
        "day_count": len(by_date),
        "top_n": top_n,
        "local_avg_primary_return_net": local_summary["avg_primary_return_net"],
        "deepseek_avg_primary_return_net": deepseek_summary["avg_primary_return_net"],
        "avg_return_delta_pct": round(
            deepseek_summary["avg_primary_return_net"] - local_summary["avg_primary_return_net"],
            4,
        ),
        "local_win_rate_primary_net": local_summary["win_rate_primary_net"],
        "deepseek_win_rate_primary_net": deepseek_summary["win_rate_primary_net"],
        "win_rate_delta_pct": round(
            deepseek_summary["win_rate_primary_net"] - local_summary["win_rate_primary_net"],
            2,
        ),
        "status": "ok" if deepseek_selected else "empty",
    }


def _deepseek_counterfactual_n(strategy_name: str) -> int:
    if strategy_name == "tomorrow_picks":
        return max(1, int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 5)))
    return max(1, min(10, int(getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18))))


def _write_deepseek_attribution_snapshot(strategy_name: str, result: Dict[str, object]) -> None:
    path = str(getattr(config, "DEEPSEEK_ATTRIBUTION_PATH", ".runtime/deepseek_attribution.json") or "").strip()
    if not path:
        return
    try:
        existing = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing = loaded
        existing[strategy_name] = result
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        return


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

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from . import config
from .normalization import coerce_number, normalize_code
from .performance import json_loads_cached, validation_metrics_cache_key as _validation_metrics_cache_key
from .validation_policy import (
    current_replay_strategy_version,
    current_strategy_version,
    execution_cost_pct as _execution_cost_pct,
    exit_holding_days as _exit_holding_days,
    is_primary_tomorrow_signal as _is_primary_tomorrow_signal,
    is_primary_validation_signal as _is_primary_validation_signal,
    is_replay_version as _is_replay_version,
    matches_current_validation_baseline as _matches_current_validation_baseline,
    outcome_ready as _outcome_ready,
    primary_return_config as _primary_return_config,
    stored_or_current_trade_cost_pct as _stored_or_current_trade_cost_pct,
    stored_validation_baseline_id as _stored_validation_baseline_id,
    validation_baseline_config,
)
from .validation_serialization import (
    oos_report_row_to_dict as _oos_report_row_to_dict,
    signal_row_to_dict as _row_to_dict,
    tuning_row_to_dict as _tuning_row_to_dict,
)
from .validation_stance import compute_stance_outcome as _compute_stance_outcome
from .validation_statistics import (
    average as _avg,
    market_gate_hit as _market_gate_hit,
    market_gate_outcome_summary as _market_gate_outcome_summary,
    rate as _rate,
)


__all__ = [
    "ValidationRepository",
    "SignalRepository",
    "OutcomeRepository",
    "TuningRepository",
    "MarketGateRepository",
    "ResearchRepository",
    "OOSReportRepository",
    "PredictionRepository",
]

def _json_value(value, fallback):
    if isinstance(value, type(fallback)):
        return value
    try:
        loaded = json.loads(value or ("[]" if isinstance(fallback, list) else "{}"))
    except Exception:
        return fallback
    return loaded if isinstance(loaded, type(fallback)) else fallback


def _candidate_snapshot_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for source, target, fallback in (
        ("eligibility_reasons_json", "eligibility_reasons", []),
        ("feature_values_json", "feature_values", {}),
        ("missing_mask_json", "missing_mask", {}),
        ("source_timestamps_json", "source_timestamps", {}),
        ("point_in_time_violations_json", "point_in_time_violations", []),
        ("raw_json", "raw", {}),
    ):
        item[target] = _json_value(item.pop(source, None), fallback)
    item["eligible"] = bool(item.get("eligible"))
    item["selected"] = bool(item.get("selected"))
    item["point_in_time_valid"] = bool(item.get("point_in_time_valid"))
    return item


def _execution_record_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for source, target, fallback in (
        ("execution_policy_json", "execution_policy", {}),
        ("cost_scenarios_json", "cost_scenarios", {}),
        ("raw_prices_json", "raw_prices", []),
        ("benchmark_json", "benchmark", {}),
    ):
        item[target] = _json_value(item.pop(source, None), fallback)
    item["promotion_eligible"] = bool(item.get("promotion_eligible"))
    return item


def _fold_prediction_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    item["selected"] = bool(item.get("selected"))
    item["prediction"] = _json_value(item.pop("prediction_json", None), {})
    return item


class _RepositoryBase:
    """Shared SQLite connection boundary for validation repositories."""

    def __init__(self, connect_fn, db_path: str) -> None:
        self._connect = connect_fn
        self.db_path = db_path

    def connect(self):
        return self._connect(self.db_path)

    def metrics_cache_key(self, strategy_name: str, days: int):
        baseline_id = str(validation_baseline_config(strategy_name).get("baseline_id") or "")
        return _validation_metrics_cache_key(strategy_name, baseline_id, days)


class SignalRepository(_RepositoryBase):
    """Persists and queries strategy signal snapshots."""

    def save_signals(
        self,
        strategy_name: str,
        strategy_version: str,
        signal_time: str,
        rows: Iterable[Dict[str, object]],
        deepseek_shadow_rows: Optional[Iterable[Dict[str, object]]] = None,
        candidate_rows: Optional[Iterable[Dict[str, object]]] = None,
        batch_metadata: Optional[Dict[str, object]] = None,
        execution_policy: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        signal_date = signal_time[:10]
        rows = list(rows)
        deepseek_shadow_rows = list(deepseek_shadow_rows or [])
        candidate_rows = list(candidate_rows or [])
        batch_metadata = dict(batch_metadata or {})
        execution_policy = dict(execution_policy or {})
        execution_policy_json = json.dumps(execution_policy, ensure_ascii=False, sort_keys=True, default=str)
        execution_policy_version = str(execution_policy.get("policy_version") or "")
        portfolio_capital = coerce_number((execution_policy.get("portfolio") or {}).get("capital"))
        saved = 0
        shadow_saved = 0
        candidate_saved = 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_signal_batches
                (strategy_name, strategy_version, signal_date, signal_time, saved_count,
                 candidate_count, selected_count, data_source_timestamp, market_data_cutoff,
                 execution_policy_version, execution_policy_json, generation_json,
                 portfolio_capital, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_name,
                    strategy_version,
                    signal_date,
                    signal_time,
                    len(rows),
                    len(candidate_rows),
                    len(rows),
                    str(batch_metadata.get("data_source_timestamp") or ""),
                    str(batch_metadata.get("market_data_cutoff") or signal_time),
                    execution_policy_version,
                    execution_policy_json,
                    json.dumps(batch_metadata.get("generation") or {}, ensure_ascii=False, sort_keys=True, default=str),
                    portfolio_capital,
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
                candidate_delete_sql = """
                    DELETE FROM strategy_candidate_snapshots
                    WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                    """
                candidate_delete_params = (strategy_name, strategy_version, signal_date)
            else:
                conn.execute(
                    """
                    DELETE FROM strategy_signal_batches
                    WHERE strategy_name = ? AND signal_date = ? AND strategy_version != ?
                      AND lower(strategy_version) NOT LIKE '%replay%'
                    """,
                    (strategy_name, signal_date, strategy_version),
                )
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
                candidate_delete_sql = """
                    DELETE FROM strategy_candidate_snapshots
                    WHERE strategy_name = ? AND signal_date = ? AND lower(strategy_version) NOT LIKE '%replay%'
                    """
                candidate_delete_params = (strategy_name, signal_date)
            if old_ids:
                conn.executemany(
                    "DELETE FROM strategy_execution_skips WHERE signal_id = ?",
                    [(row[0],) for row in old_ids],
                )
                conn.executemany(
                    "DELETE FROM strategy_outcomes WHERE signal_id = ?",
                    [(row[0],) for row in old_ids],
                )
                conn.executemany(
                    "DELETE FROM strategy_execution_records WHERE signal_id = ?",
                    [(row[0],) for row in old_ids],
                )
                conn.execute(delete_sql, delete_params)
            if old_shadow_ids:
                conn.executemany(
                    "DELETE FROM strategy_deepseek_shadow_outcomes WHERE shadow_id = ?",
                    [(row[0],) for row in old_shadow_ids],
                )
                conn.execute(shadow_delete_sql, shadow_delete_params)
            conn.execute(candidate_delete_sql, candidate_delete_params)
            for row in candidate_rows:
                code = normalize_code(row.get("code"))
                if not code:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategy_candidate_snapshots
                    (strategy_name, strategy_version, signal_date, signal_time, code, name, market,
                     industry, style_bucket, eligible, selected, rank, score, point_in_time_valid,
                     eligibility_reasons_json, feature_values_json, missing_mask_json,
                     source_timestamps_json, announcement_time, market_data_cutoff,
                     point_in_time_violations_json, raw_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_name,
                        strategy_version,
                        signal_date,
                        signal_time,
                        code,
                        str(row.get("name") or ""),
                        str(row.get("market") or ""),
                        str(row.get("industry") or ""),
                        str(row.get("style_bucket") or "unknown"),
                        1 if row.get("eligible") else 0,
                        1 if row.get("selected") else 0,
                        int(coerce_number(row.get("rank")) or 0),
                        coerce_number(row.get("score")),
                        1 if row.get("point_in_time_valid") else 0,
                        json.dumps(row.get("eligibility_reasons") or [], ensure_ascii=False, default=str),
                        json.dumps(row.get("feature_values") or {}, ensure_ascii=False, default=str),
                        json.dumps(row.get("missing_mask") or {}, ensure_ascii=False, default=str),
                        json.dumps(row.get("source_timestamps") or {}, ensure_ascii=False, default=str),
                        str(row.get("announcement_time") or ""),
                        str(row.get("market_data_cutoff") or signal_time),
                        json.dumps(row.get("point_in_time_violations") or [], ensure_ascii=False, default=str),
                        json.dumps(row.get("raw") or {}, ensure_ascii=False, default=str),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                candidate_saved += 1
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
            "candidate_saved": candidate_saved,
            "deepseek_shadow_saved": shadow_saved,
            "deepseek_shadow_replaced": len(old_shadow_ids),
        }


    def list_signal_dates(self, strategy_name: str = "") -> List[Dict[str, object]]:
        where = ""
        params = []
        if strategy_name:
            where = "WHERE b.strategy_name = ?"
            params.append(strategy_name)
        with self.connect() as conn:
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


    def existing_validation_dates(self, strategy_name: str, replay_version: str = "") -> List[str]:
        where = "strategy_name = ? AND lower(strategy_version) NOT LIKE '%replay%'"
        params = [strategy_name]
        if replay_version:
            where = "strategy_name = ? AND (lower(strategy_version) NOT LIKE '%replay%' OR strategy_version = ?)"
            params.append(replay_version)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT signal_date FROM strategy_signal_batches WHERE {}".format(where),
                params,
            ).fetchall()
        return [str(row[0]) for row in rows if row and row[0]]


    def signals_for_date(self, signal_date: str, strategy_name: str = "") -> List[Dict[str, object]]:
        where = "WHERE s.signal_date = ?"
        params = [signal_date]
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        with self.connect() as conn:
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
                       COALESCE(o.survivorship_corrected, 0) AS survivorship_corrected,
                       COALESCE(o.correction_reason, '') AS correction_reason,
                       o.trade_cost_pct AS stored_trade_cost_pct,
                       COALESCE(o.primary_return_field, '') AS stored_primary_return_field,
                       COALESCE(o.primary_return, 0) AS stored_primary_return,
                       COALESCE(o.primary_return_net, 0) AS stored_primary_return_net,
                       COALESCE(o.primary_holding_days, 0) AS stored_primary_holding_days,
                       COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
                       COALESCE(o.validation_baseline_json, '') AS validation_baseline_json,
                       COALESCE(e.label_status, CASE WHEN o.signal_id IS NOT NULL THEN 'settled' ELSE 'pending' END) AS label_status,
                       COALESCE(e.reason, k.skip_reason, '') AS execution_reason,
                       COALESCE(e.entry_status, '') AS entry_status,
                       COALESCE(e.exit_status, '') AS exit_status,
                       COALESCE(e.delisting_status, o.delisting_status, 'not_applicable') AS delisting_status,
                       COALESCE(e.promotion_eligible, CASE WHEN o.signal_id IS NOT NULL THEN 1 ELSE 0 END) AS promotion_eligible,
                       e.portfolio_capital, e.target_weight_pct, e.target_notional, e.order_quantity,
                       e.actual_filled_quantity, e.actual_entry_price, e.actual_exit_quantity,
                       e.actual_exit_price, e.unfilled_quantity, e.unfilled_entry_quantity,
                       e.unfilled_exit_quantity, e.fill_source,
                       e.fee_pct, e.slippage_pct, e.impact_pct,
                       e.gross_return_pct, e.net_return_pct, e.return_formula,
                       COALESCE(e.execution_policy_version, o.execution_policy_version, '') AS execution_policy_version,
                       COALESCE(e.execution_policy_json, o.execution_policy_json, '') AS execution_policy_json,
                       COALESCE(e.cost_scenarios_json, o.cost_scenarios_json, '{{}}') AS cost_scenarios_json,
                       COALESCE(e.raw_prices_json, o.raw_prices_json, '[]') AS raw_prices_json,
                       COALESCE(e.benchmark_json, o.benchmark_json, '{{}}') AS benchmark_json,
                       COALESCE(o.return_reproducible, 0) AS return_reproducible,
                       o.updated_at AS outcome_updated_at,
                       k.skip_reason, k.updated_at AS skip_updated_at
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_skips k ON k.signal_id = s.id
                LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
                {}
                ORDER BY s.strategy_name ASC, s.rank ASC
                """.format(where),
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]


    def candidate_snapshots_for_date(
        self,
        signal_date: str,
        strategy_name: str = "",
        strategy_version: str = "",
    ) -> List[Dict[str, object]]:
        where = "WHERE signal_date = ?"
        params: List[object] = [signal_date]
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        if strategy_version:
            where += " AND strategy_version = ?"
            params.append(strategy_version)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM strategy_candidate_snapshots
                {}
                ORDER BY selected DESC, rank ASC, code ASC
                """.format(where),
                params,
            ).fetchall()
        return [_candidate_snapshot_to_dict(row) for row in rows]


    def latest_candidate_snapshots(self, strategy_name: str) -> List[Dict[str, object]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT signal_date, strategy_version
                FROM strategy_signal_batches
                WHERE strategy_name = ? AND candidate_count > 0
                  AND lower(strategy_version) NOT LIKE '%replay%'
                ORDER BY signal_date DESC, signal_time DESC
                LIMIT 1
                """,
                (strategy_name,),
            ).fetchone()
        if not row:
            return []
        return self.candidate_snapshots_for_date(row[0], strategy_name, row[1])


    def latest_signal_rows(self, strategy_name: str) -> List[Dict[str, object]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT signal_date, strategy_version
                FROM strategy_signal_batches
                WHERE strategy_name = ? AND lower(strategy_version) NOT LIKE '%replay%'
                ORDER BY signal_date DESC, signal_time DESC
                LIMIT 1
                """,
                (strategy_name,),
            ).fetchone()
        if not row:
            return []
        signal_date, strategy_version = row
        signals = [
            signal
            for signal in self.signals_for_date(signal_date, strategy_name)
            if signal.get("strategy_version") == strategy_version
        ]
        rows = []
        for signal in signals:
            raw = signal.get("raw") or {}
            if isinstance(raw, dict):
                item = raw.copy()
                item["rank"] = signal.get("rank")
                item["strategy_version"] = signal.get("strategy_version")
                item["signal_date"] = signal.get("signal_date")
                rows.append(item)
        rows.sort(key=lambda item: int(item.get("rank") or 9999))
        return rows


    def prune_strategies(self, allowed_strategies: Iterable[str]) -> Dict[str, int]:
        allowed = [str(item) for item in allowed_strategies if str(item or "").strip()]
        if not allowed:
            return {"deleted_signals": 0, "deleted_batches": 0}
        placeholders = ",".join("?" for _ in allowed)
        with self.connect() as conn:
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
                conn.executemany("DELETE FROM strategy_execution_records WHERE signal_id = ?", id_rows)
                conn.executemany("DELETE FROM strategy_signals WHERE id = ?", id_rows)
            conn.execute(
                "DELETE FROM strategy_candidate_snapshots WHERE strategy_name NOT IN ({})".format(placeholders),
                allowed,
            )
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
        with self.connect() as conn:
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


    def signal_status_counts(
        self,
        strategy_name: str = "",
        days: int = 20,
        strategy_version: str = "",
    ) -> Dict[str, object]:
        where = "WHERE 1=1"
        params: List[object] = []
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        if strategy_version:
            where += " AND strategy_version = ?"
            params.append(strategy_version)
        with self.connect() as conn:
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
                    "unknown_outcome_count": 0,
                    "unfilled_outcome_count": 0,
                    "outcome_coverage_pct": None,
                    "baseline_mismatch_outcome_count": 0,
                }
            placeholders = ",".join("?" for _ in dates)
            count_where = "WHERE s.signal_date IN ({})".format(placeholders)
            count_params: List[object] = list(dates)
            if strategy_name:
                count_where += " AND s.strategy_name = ?"
                count_params.append(strategy_name)
            if strategy_version:
                count_where += " AND s.strategy_version = ?"
                count_params.append(strategy_version)
            rows = conn.execute(
                """
                SELECT
                  s.strategy_name,
                  o.signal_id AS outcome_signal_id,
                  COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
                  k.signal_id AS skip_signal_id,
                  COALESCE(e.label_status, '') AS label_status
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_skips k ON k.signal_id = s.id
                LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
                {}
                """.format(count_where),
                count_params,
            ).fetchall()
        signal_count = len(rows)
        pending_count = 0
        outcome_count = 0
        mismatch_count = 0
        unknown_count = 0
        unfilled_count = 0
        baseline_cache: Dict[str, str] = {}
        for row in rows:
            row_strategy = strategy_name or str(row[0] or "")
            if row_strategy not in baseline_cache:
                baseline_cache[row_strategy] = str(validation_baseline_config(row_strategy).get("baseline_id") or "")
            has_current_outcome = bool(row[1]) and _matches_current_validation_baseline(
                row[2],
                row_strategy,
                baseline_cache[row_strategy],
            )
            has_skip = bool(row[3])
            label_status = str(row[4] or "")
            if has_current_outcome:
                outcome_count += 1
            elif bool(row[1]):
                mismatch_count += 1
            if label_status == "unknown":
                unknown_count += 1
            elif has_skip or label_status == "unfilled":
                unfilled_count += 1
            elif not has_current_outcome:
                pending_count += 1
        coverage = round(outcome_count / signal_count * 100.0, 2) if signal_count > 0 else None
        return {
            "signal_sample_count": signal_count,
            "pending_outcome_count": pending_count,
            "unknown_outcome_count": unknown_count,
            "unfilled_outcome_count": unfilled_count,
            "outcome_coverage_pct": coverage,
            "baseline_mismatch_outcome_count": mismatch_count,
        }


    def execution_skip_count(
        self,
        strategy_name: str = "",
        days: int = 20,
        strategy_version: str = "",
    ) -> int:
        where = "WHERE 1=1"
        params: List[object] = []
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        if strategy_version:
            where += " AND s.strategy_version = ?"
            params.append(strategy_version)
        with self.connect() as conn:
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
            if strategy_version:
                count_where += " AND s.strategy_version = ?"
                count_params.append(strategy_version)
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


    def fetch_recent_signal_dates(
        self,
        strategy_name: str = "",
        current_version: str = "",
        replay_version: str = "",
        days: int = 120,
    ) -> List[str]:
        where = "WHERE 1=1"
        params: List[object] = []
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        if current_version:
            where += " AND (strategy_version = ? OR strategy_version = ?)"
            params.extend((current_version, replay_version))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT signal_date
                FROM strategy_signals
                {}
                ORDER BY signal_date DESC
                LIMIT ?
                """.format(where),
                [*params, max(1, int(days))],
            ).fetchall()
        return [str(row["signal_date"]) for row in rows if row["signal_date"]]


    def fetch_baseline_status_rows(
        self,
        dates: List[str],
        strategy_name: str = "",
        current_version: str = "",
        replay_version: str = "",
    ) -> List[sqlite3.Row]:
        if not dates:
            return []
        placeholders = ",".join("?" for _ in dates)
        row_where = "WHERE s.signal_date IN ({})".format(placeholders)
        row_params: List[object] = list(dates)
        if strategy_name:
            row_where += " AND s.strategy_name = ?"
            row_params.append(strategy_name)
        if current_version:
            row_where += " AND (s.strategy_version = ? OR s.strategy_version = ?)"
            row_params.extend((current_version, replay_version))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.strategy_version, s.rank, s.raw_json,
                       o.signal_id AS outcome_signal_id,
                       COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
                       COALESCE(o.future_days, 1) AS future_days,
                       k.signal_id AS skip_signal_id,
                       CASE WHEN COALESCE(b.candidate_count, 0) > 0
                            THEN COALESCE(e.promotion_eligible, 0) ELSE 1 END AS promotion_eligible
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_skips k ON k.signal_id = s.id
                LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
                LEFT JOIN strategy_signal_batches b
                  ON b.strategy_name = s.strategy_name
                 AND b.strategy_version = s.strategy_version
                 AND b.signal_date = s.signal_date
                {}
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(row_where),
                row_params,
            ).fetchall()


    def fetch_baseline_backfill_rows(
        self,
        dates: List[str],
        strategy_name: str,
        current_version: str = "",
        replay_version: str = "",
        limit: int = 500,
    ) -> List[sqlite3.Row]:
        if not dates:
            return []
        placeholders = ",".join("?" for _ in dates)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT s.signal_date, s.code, MAX(s.name) AS name, MIN(s.rank) AS best_rank,
                       COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
                       o.signal_id AS outcome_signal_id,
                       k.signal_id AS skip_signal_id
                FROM strategy_signals s
                LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_skips k ON k.signal_id = s.id
                WHERE s.signal_date IN ({})
                  AND s.strategy_name = ?
                  {}
                GROUP BY s.signal_date, s.code, o.validation_baseline_id, o.signal_id, k.signal_id
                ORDER BY s.signal_date DESC, best_rank ASC
                LIMIT ?
                """.format(
                    placeholders,
                    "AND (s.strategy_version = ? OR s.strategy_version = ?)" if current_version else "",
                ),
                [
                    *dates,
                    strategy_name,
                    *((current_version, replay_version) if current_version else ()),
                    max(1, int(limit)),
                ],
            ).fetchall()


class OutcomeRepository(_RepositoryBase):
    """Persists strategy outcomes and provides outcome query rows."""

    def fetch_validation_metric_rows(
        self,
        strategy_name: str = "",
        current_version: str = "",
        replay_version: str = "",
    ) -> List[sqlite3.Row]:
        where = "WHERE o.signal_id IS NOT NULL"
        params: List[object] = []
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        if current_version:
            where += " AND (s.strategy_version = ? OR s.strategy_version = ?)"
            params.extend((current_version, replay_version))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.rank,
                       s.strategy_version, s.turnover, s.market, s.raw_json,
                       COALESCE(o.signal_next_close_return, o.next_close_return) AS signal_next_close_return,
                       o.next_open_return,
                       o.next_close_return,
                       o.next_low,
                       o.intraday_high_return AS open_intraday_high_return,
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
                       o.exit_return AS exit_return,
                       COALESCE(o.exit_reason, '') AS exit_reason,
                       COALESCE(o.exit_days, 0) AS exit_days,
                       COALESCE(o.signal_max_drawdown_3d, o.max_drawdown_3d) AS signal_max_drawdown_3d,
                       o.max_drawdown_3d AS open_max_drawdown_primary,
                       o.hit_3pct AS open_hit_3pct,
                       o.hit_5pct AS open_hit_5pct,
                       COALESCE(o.signal_hit_3pct, o.hit_3pct) AS signal_hit_3pct,
                       COALESCE(o.signal_hit_5pct, o.hit_5pct) AS signal_hit_5pct,
                       COALESCE(o.future_days, 1) AS future_days,
                       COALESCE(o.survivorship_corrected, 0) AS survivorship_corrected,
                       COALESCE(o.correction_reason, '') AS correction_reason,
                       o.trade_cost_pct AS stored_trade_cost_pct,
                       COALESCE(o.primary_return_field, '') AS stored_primary_return_field,
                       COALESCE(o.primary_return, 0) AS stored_primary_return,
                       COALESCE(o.primary_return_net, 0) AS stored_primary_return_net,
                       COALESCE(o.primary_holding_days, 0) AS stored_primary_holding_days,
                       COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
                       COALESCE(o.validation_baseline_json, '') AS validation_baseline_json,
                       CASE WHEN COALESCE(b.candidate_count, 0) > 0
                            THEN COALESCE(e.promotion_eligible, 0) ELSE 1 END AS promotion_eligible
                FROM strategy_signals s
                JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
                LEFT JOIN strategy_signal_batches b
                  ON b.strategy_name = s.strategy_name
                 AND b.strategy_version = s.strategy_version
                 AND b.signal_date = s.signal_date
                {}
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(where),
                params,
            ).fetchall()


    def fetch_deepseek_attribution_rows(
        self,
        strategy_name: str,
        primary_column: str,
        current_version: str = "",
        replay_version: str = "",
    ) -> List[sqlite3.Row]:
        version_filter = ""
        query_params: List[object] = [strategy_name]
        if current_version:
            version_filter = " AND (s.strategy_version = ? OR s.strategy_version = ?)"
            query_params.extend((current_version, replay_version))
        with self.connect() as conn:
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
                WHERE s.strategy_name = ? {version_filter}
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(primary_column=primary_column, version_filter=version_filter),
                query_params,
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
                WHERE s.strategy_name = ? {version_filter}
                ORDER BY s.signal_date DESC, s.local_rank ASC
                """.format(primary_column=primary_column, version_filter=version_filter),
                query_params,
            ).fetchall()
        return list(signal_rows) + list(shadow_rows)


    def fetch_signals_for_outcome_update(self, where: str, params: List[object]) -> List[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT strategy_signals.*,
                       existing_outcome.signal_id AS existing_outcome_signal_id,
                       COALESCE(existing_outcome.validation_baseline_id, '') AS existing_validation_baseline_id,
                       COALESCE(existing_outcome.future_days, 0) AS existing_future_days,
                       COALESCE(existing_outcome.exit_reason, '') AS existing_exit_reason,
                       COALESCE((
                           SELECT b.execution_policy_version
                           FROM strategy_signal_batches b
                           WHERE b.strategy_name = strategy_signals.strategy_name
                             AND b.strategy_version = strategy_signals.strategy_version
                             AND b.signal_date = strategy_signals.signal_date
                       ), '') AS execution_policy_version,
                       COALESCE((
                           SELECT b.execution_policy_json
                           FROM strategy_signal_batches b
                           WHERE b.strategy_name = strategy_signals.strategy_name
                             AND b.strategy_version = strategy_signals.strategy_version
                             AND b.signal_date = strategy_signals.signal_date
                       ), '') AS execution_policy_json
                FROM strategy_signals
                LEFT JOIN strategy_outcomes existing_outcome
                  ON existing_outcome.signal_id = strategy_signals.id
                {}
                ORDER BY signal_date DESC, rank ASC
                """.format(where),
                params,
            ).fetchall()


    def delete_strategy_outcome(self, signal_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM strategy_outcomes WHERE signal_id = ?", (signal_id,))


    def save_execution_skip(self, signal_id: int, code: str, reason: str, updated_at: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM strategy_outcomes WHERE signal_id = ?", (signal_id,))
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_execution_skips
                (signal_id, code, skip_reason, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (signal_id, code, reason, updated_at),
            )


    def save_execution_record(self, record: Dict[str, object]) -> None:
        columns = (
            "signal_id",
            "code",
            "label_status",
            "reason",
            "entry_status",
            "exit_status",
            "delisting_status",
            "promotion_eligible",
            "portfolio_capital",
            "target_weight_pct",
            "target_notional",
            "order_quantity",
            "actual_filled_quantity",
            "actual_entry_price",
            "actual_exit_quantity",
            "actual_exit_price",
            "unfilled_quantity",
            "unfilled_entry_quantity",
            "unfilled_exit_quantity",
            "fill_source",
            "fee_pct",
            "slippage_pct",
            "impact_pct",
            "gross_return_pct",
            "net_return_pct",
            "return_formula",
            "execution_policy_version",
            "execution_policy_json",
            "cost_scenarios_json",
            "raw_prices_json",
            "benchmark_json",
            "updated_at",
        )
        json_fields = {
            "execution_policy_json": record.get("execution_policy") or {},
            "cost_scenarios_json": record.get("cost_scenarios") or {},
            "raw_prices_json": record.get("raw_prices") or [],
            "benchmark_json": record.get("benchmark") or {},
        }
        values = []
        for column in columns:
            if column in json_fields:
                values.append(json.dumps(json_fields[column], ensure_ascii=False, sort_keys=True, default=str))
            elif column == "promotion_eligible":
                values.append(1 if record.get(column) else 0)
            else:
                values.append(record.get(column))
        with self.connect() as conn:
            if str(record.get("label_status") or "") != "unfilled":
                conn.execute(
                    "DELETE FROM strategy_execution_skips WHERE signal_id = ?",
                    (record.get("signal_id"),),
                )
            conn.execute(
                "INSERT OR REPLACE INTO strategy_execution_records ({}) VALUES ({})".format(
                    ", ".join(columns),
                    ", ".join("?" for _ in columns),
                ),
                values,
            )


    def execution_records_for_date(
        self,
        signal_date: str,
        strategy_name: str = "",
    ) -> List[Dict[str, object]]:
        where = "WHERE s.signal_date = ?"
        params: List[object] = [signal_date]
        if strategy_name:
            where += " AND s.strategy_name = ?"
            params.append(strategy_name)
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT e.*, s.strategy_name, s.strategy_version, s.signal_date, s.signal_time, s.rank
                FROM strategy_execution_records e
                JOIN strategy_signals s ON s.id = e.signal_id
                {}
                ORDER BY s.strategy_name, s.rank
                """.format(where),
                params,
            ).fetchall()
        return [_execution_record_to_dict(row) for row in rows]


    def save_strategy_outcome(self, signal_id: int, columns, values) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM strategy_execution_skips WHERE signal_id = ?", (signal_id,))
            conn.execute(
                "INSERT OR REPLACE INTO strategy_outcomes ({}) VALUES ({})".format(
                    ", ".join(columns),
                    ", ".join("?" for _ in values),
                ),
                values,
            )


    def fetch_deepseek_shadow_signals(self, where: str, params: List[object]) -> List[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM strategy_deepseek_shadow_signals {} ORDER BY signal_date DESC, local_rank ASC".format(where),
                params,
            ).fetchall()


    def save_deepseek_shadow_outcome(self, values) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_deepseek_shadow_outcomes
                (shadow_id, code, next_trade_date, future_days, next_open, next_close,
                 next_close_return, hold_3d_return, hold_5d_return, hold_10d_return, hold_20d_return,
                 signal_next_close_return, signal_hold_3d_return, signal_hold_5d_return,
                 signal_hold_10d_return, signal_hold_20d_return, exit_return, signal_exit_return, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

class TuningRepository(_RepositoryBase):
    """Persists tuning runs and live-weight training samples."""

    def save_tuning_run(
        self,
        strategy_name: str,
        days: int,
        plan: Dict[str, object],
        metrics: Dict[str, object],
        deepseek_review: Dict[str, object],
    ) -> Dict[str, object]:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
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
        with self.connect() as conn:
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
        with self.connect() as conn:
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
        drawdown_column = "signal_max_drawdown_3d" if strategy_name == "short_term" else "max_drawdown_3d"
        exit_column = "signal_exit_return" if strategy_name == "short_term" else "exit_return"
        version_filter = ""
        params = [strategy_name]
        current_version = current_strategy_version(strategy_name)
        if current_version:
            version_filter = " AND s.strategy_version = ?"
            params.append(current_version)
        current_baseline_id = str(validation_baseline_config(strategy_name).get("baseline_id") or "")
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT s.signal_date, s.strategy_name, s.strategy_version, s.rank, s.code,
                       s.name, s.score, s.turnover, s.market, s.raw_json,
                       COALESCE(o.{primary_column}, 0) AS primary_return,
                       COALESCE(o.next_open_return, 0) AS next_open_return,
                       COALESCE(o.{drawdown_column}, 0) AS max_drawdown,
                       COALESCE(o.{exit_column}, o.{primary_column}, 0) AS exit_return,
                       COALESCE(o.exit_reason, '') AS exit_reason,
                       COALESCE(o.exit_days, 0) AS exit_days,
                       COALESCE(o.future_days, 1) AS future_days,
                       o.trade_cost_pct AS stored_trade_cost_pct,
                       COALESCE(o.primary_return_field, '') AS stored_primary_return_field,
                       COALESCE(o.primary_return, 0) AS stored_primary_return,
                       COALESCE(o.primary_return_net, 0) AS stored_primary_return_net,
                       COALESCE(o.primary_holding_days, 0) AS stored_primary_holding_days,
                       COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
                       CASE WHEN COALESCE(b.candidate_count, 0) > 0
                            THEN COALESCE(e.promotion_eligible, 0) ELSE 1 END AS promotion_eligible
                FROM strategy_signals s
                JOIN strategy_outcomes o ON o.signal_id = s.id
                LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
                LEFT JOIN strategy_signal_batches b
                  ON b.strategy_name = s.strategy_name
                 AND b.strategy_version = s.strategy_version
                 AND b.signal_date = s.signal_date
                WHERE s.strategy_name = ? {version_filter}
                ORDER BY s.signal_date DESC, s.rank ASC
                """.format(
                    primary_column=primary_column,
                    drawdown_column=drawdown_column,
                    exit_column=exit_column,
                    version_filter=version_filter,
                ),
                params,
            ).fetchall()
        if not rows:
            return []
        rows = [
            row
            for row in rows
            if bool(row["promotion_eligible"])
            and _matches_current_validation_baseline(row["validation_baseline_id"], strategy_name, current_baseline_id)
        ]
        if not rows:
            return []
        dates: List[str] = []
        selected: List[sqlite3.Row] = []
        for row in rows:
            if _is_replay_version(row["strategy_version"]):
                continue
            if not _outcome_ready(row, primary_days):
                continue
            if row["signal_date"] not in dates:
                if len(dates) >= max(1, int(days)):
                    continue
                dates.append(row["signal_date"])
            if row["signal_date"] in dates:
                selected.append(row)
        samples: List[Dict[str, object]] = []
        raw_json_cache: Dict[str, object] = {}
        for row in selected:
            raw = json_loads_cached(row["raw_json"], cache=raw_json_cache)
            if not isinstance(raw, dict):
                raw = {}
            if not _is_primary_validation_signal(strategy_name, row["rank"], raw):
                continue
            primary_return = coerce_number(row["primary_return"])
            stored_primary_field = str(row["stored_primary_return_field"] or "")
            has_stored_primary = stored_primary_field == primary_column
            if has_stored_primary:
                primary_return = coerce_number(row["stored_primary_return"])
            trade_cost = _stored_or_current_trade_cost_pct(row)
            primary_return_net = (
                coerce_number(row["stored_primary_return_net"])
                if has_stored_primary
                else round(primary_return - trade_cost, 4)
            )
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
                    "primary_return_net": primary_return_net,
                    "next_open_return": coerce_number(row["next_open_return"]),
                    "max_drawdown": coerce_number(row["max_drawdown"]),
                    "trade_cost_pct": trade_cost,
                    "exit_return": coerce_number(row["exit_return"]),
                    "future_days": int(row["future_days"] or 0),
                    "primary_holding_days": primary_days,
                    "primary_horizon_label": primary_label,
                    "validation_baseline_id": _stored_validation_baseline_id(
                        row["validation_baseline_id"],
                        strategy_name,
                    ),
                }
            )
        return samples


class MarketGateRepository(_RepositoryBase):
    """Persists DeepSeek market-gate reviews and metrics."""

    def save_market_gate_review(self, market_gate: Dict[str, object], market_filter: str = "all") -> Dict[str, object]:
        if not isinstance(market_gate, dict) or not market_gate.get("enabled"):
            return {"saved": 0, "status": "disabled"}
        now = str(market_gate.get("generated_at") or datetime.now().isoformat(timespec="seconds"))
        review_date = now[:10]
        with self.connect() as conn:
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
        with self.connect() as conn:
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
                       COALESCE(o.signal_exit_return, o.exit_return, o.signal_hold_5d_return, o.hold_5d_return, 0) AS signal_exit_return,
                       COALESCE(o.exit_reason, '') AS exit_reason,
                       COALESCE(o.exit_days, 0) AS exit_days,
                       COALESCE(o.future_days, 1) AS future_days,
                       o.trade_cost_pct AS stored_trade_cost_pct
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
            if not _outcome_ready(row, primary_days):
                continue
            outcome_by_date.setdefault(str(row["signal_date"]), []).append(
                round(coerce_number(row[primary_column]) - _stored_or_current_trade_cost_pct(row), 4)
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


class ResearchRepository(_RepositoryBase):
    """Persists research fold predictions for OOS audit and replay."""

    def save_fold_predictions(
        self,
        experiment_id: str,
        fold_id: str,
        strategy_name: str,
        rows: Iterable[Dict[str, object]],
        *,
        baseline_id: str = "",
        model_id: str = "",
        model_version: str = "",
        train_end_date: str = "",
        feature_schema_hash: str = "",
    ) -> Dict[str, object]:
        experiment_id = str(experiment_id or "").strip()
        fold_id = str(fold_id or "").strip()
        strategy_name = str(strategy_name or "").strip()
        if not experiment_id or not fold_id or not strategy_name:
            return {"saved": 0, "status": "missing_identity"}
        rows = [dict(row) for row in rows or [] if isinstance(row, dict)]
        now = datetime.now().isoformat(timespec="seconds")
        saved = 0
        with self.connect() as conn:
            for row in rows:
                code = normalize_code(row.get("code"))
                test_date = str(row.get("test_date") or row.get("signal_date") or "").strip()
                if not code or not test_date:
                    continue
                row_baseline_id = str(row.get("baseline_id") or baseline_id or "")
                row_model_id = str(row.get("model_id") or model_id or "")
                row_model_version = str(row.get("model_version") or model_version or "")
                row_train_end = str(row.get("train_end_date") or train_end_date or "")
                row_schema_hash = str(row.get("feature_schema_hash") or feature_schema_hash or "")
                conn.execute(
                    """
                    INSERT INTO strategy_fold_predictions
                    (experiment_id, fold_id, strategy_name, baseline_id, model_id, model_version,
                     train_end_date, test_date, code, baseline_score, predicted_net_return,
                     predicted_probability, selected, actual_net_return, feature_schema_hash,
                     prediction_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(experiment_id, fold_id, test_date, code) DO UPDATE SET
                      strategy_name=excluded.strategy_name,
                      baseline_id=excluded.baseline_id,
                      model_id=excluded.model_id,
                      model_version=excluded.model_version,
                      train_end_date=excluded.train_end_date,
                      baseline_score=excluded.baseline_score,
                      predicted_net_return=excluded.predicted_net_return,
                      predicted_probability=excluded.predicted_probability,
                      selected=excluded.selected,
                      actual_net_return=excluded.actual_net_return,
                      feature_schema_hash=excluded.feature_schema_hash,
                      prediction_json=excluded.prediction_json,
                      created_at=excluded.created_at
                    """,
                    (
                        experiment_id,
                        fold_id,
                        strategy_name,
                        row_baseline_id,
                        row_model_id,
                        row_model_version,
                        row_train_end,
                        test_date,
                        code,
                        coerce_number(row.get("baseline_score"), None),
                        coerce_number(row.get("predicted_net_return"), None),
                        coerce_number(row.get("predicted_probability"), None),
                        1 if row.get("selected") else 0,
                        coerce_number(row.get("actual_net_return"), None),
                        row_schema_hash,
                        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
                        now,
                    ),
                )
                saved += 1
        return {
            "saved": saved,
            "status": "saved" if saved else "empty",
            "experiment_id": experiment_id,
            "fold_id": fold_id,
            "strategy": strategy_name,
        }


    def list_fold_predictions(
        self,
        experiment_id: str,
        *,
        strategy_name: str = "",
        fold_id: str = "",
        limit: int = 500,
    ) -> List[Dict[str, object]]:
        where = "WHERE experiment_id = ?"
        params: List[object] = [str(experiment_id or "")]
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(str(strategy_name))
        if fold_id:
            where += " AND fold_id = ?"
            params.append(str(fold_id))
        params.append(max(1, int(limit)))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM strategy_fold_predictions
                {}
                ORDER BY test_date ASC, fold_id ASC, selected DESC, code ASC
                LIMIT ?
                """.format(where),
                params,
            ).fetchall()
        return [_fold_prediction_to_dict(row) for row in rows]


class OOSReportRepository(_RepositoryBase):
    """Persists out-of-sample report snapshots."""

    def save_oos_report(
        self,
        report: Dict[str, object],
        trigger: str = "manual",
    ) -> Dict[str, object]:
        if not isinstance(report, dict):
            return {"saved": 0, "status": "invalid_report"}
        strategy_name = str(report.get("strategy") or report.get("strategy_name") or "").strip()
        if not strategy_name:
            return {"saved": 0, "status": "missing_strategy"}
        generated_at = str(report.get("generated_at") or datetime.now().isoformat(timespec="seconds"))
        generated_date = generated_at[:10]
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        validation_gate = report.get("validation_gate") if isinstance(report.get("validation_gate"), dict) else {}
        baseline_status = report.get("baseline_status") if isinstance(report.get("baseline_status"), dict) else {}
        requirements = report.get("requirements") if isinstance(report.get("requirements"), dict) else {}
        baseline_id = str(
            report.get("validation_baseline_id")
            or baseline_status.get("validation_baseline_id")
            or ""
        )
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategy_oos_reports
                (strategy_name, generated_date, generated_at, trigger, days, oos_status, baseline_id,
                 sample_count, real_day_count, avg_primary_return_net, real_avg_primary_return_net,
                 real_avg_primary_return_net_ci95_low, real_avg_primary_return_net_ci95_high,
                 real_portfolio_max_drawdown_pct, gate_blocked, gate_reason,
                 report_json, baseline_status_json, validation_gate_json, requirements_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_name,
                    generated_date,
                    generated_at,
                    str(trigger or "manual"),
                    int(report.get("days") or 0),
                    str(report.get("oos_status") or ""),
                    baseline_id,
                    int(summary.get("sample_count") or 0),
                    int(summary.get("real_day_count") or 0),
                    coerce_number(summary.get("avg_primary_return_net")),
                    coerce_number(summary.get("real_avg_primary_return_net")),
                    (
                        coerce_number(summary.get("real_avg_primary_return_net_ci95_low"))
                        if summary.get("real_avg_primary_return_net_ci95_low") is not None
                        else None
                    ),
                    (
                        coerce_number(summary.get("real_avg_primary_return_net_ci95_high"))
                        if summary.get("real_avg_primary_return_net_ci95_high") is not None
                        else None
                    ),
                    coerce_number(summary.get("real_portfolio_max_drawdown_pct")),
                    1 if validation_gate.get("blocked") else 0,
                    str(validation_gate.get("reason") or "")[:500],
                    json.dumps(report, ensure_ascii=False),
                    json.dumps(baseline_status, ensure_ascii=False),
                    json.dumps(validation_gate, ensure_ascii=False),
                    json.dumps(requirements, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            report_id = int(cursor.lastrowid)
        return {
            "saved": 1,
            "status": "saved",
            "id": report_id,
            "strategy": strategy_name,
            "oos_status": str(report.get("oos_status") or ""),
            "generated_at": generated_at,
        }


    def list_oos_reports(
        self,
        strategy_name: str = "",
        limit: int = 50,
    ) -> List[Dict[str, object]]:
        where = "WHERE 1=1"
        params: List[object] = []
        if strategy_name:
            where += " AND strategy_name = ?"
            params.append(strategy_name)
        params.append(max(1, int(limit)))
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM strategy_oos_reports
                {}
                ORDER BY generated_at DESC, id DESC
                LIMIT ?
                """.format(where),
                params,
            ).fetchall()
        return [_oos_report_row_to_dict(row) for row in rows]


class PredictionRepository(_RepositoryBase):
    """Persists stock prediction snapshots and stance outcomes."""

    def save_stock_prediction_snapshot(self, payload: Dict[str, object]) -> Dict[str, object]:
        optimization = payload.get("optimization") or {}
        if not isinstance(optimization, dict) or not optimization:
            return {"saved": 0, "status": "missing_optimization"}
        code = normalize_code(payload.get("code"))
        if not code:
            return {"saved": 0, "status": "missing_code"}
        now = datetime.now().isoformat(timespec="seconds")
        prediction_date = now[:10]
        with self.connect() as conn:
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
        with self.connect() as conn:
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
            with self.connect() as conn:
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
        with self.connect() as conn:
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


class ValidationRepository(_RepositoryBase):
    """Facade preserving the legacy validation repository API."""

    _FACADE_METHODS = {
        "save_signals": "signals",
        "list_signal_dates": "signals",
        "existing_validation_dates": "signals",
        "signals_for_date": "signals",
        "candidate_snapshots_for_date": "signals",
        "latest_candidate_snapshots": "signals",
        "latest_signal_rows": "signals",
        "prune_strategies": "signals",
        "signal_codes": "signals",
        "signal_status_counts": "signals",
        "execution_skip_count": "signals",
        "fetch_recent_signal_dates": "signals",
        "fetch_baseline_status_rows": "signals",
        "fetch_baseline_backfill_rows": "signals",
        "fetch_validation_metric_rows": "outcomes",
        "fetch_deepseek_attribution_rows": "outcomes",
        "fetch_signals_for_outcome_update": "outcomes",
        "delete_strategy_outcome": "outcomes",
        "save_execution_skip": "outcomes",
        "save_execution_record": "outcomes",
        "execution_records_for_date": "outcomes",
        "save_strategy_outcome": "outcomes",
        "fetch_deepseek_shadow_signals": "outcomes",
        "save_deepseek_shadow_outcome": "outcomes",
        "save_tuning_run": "tuning",
        "latest_tuning_run": "tuning",
        "list_tuning_runs": "tuning",
        "live_weight_samples": "tuning",
        "save_market_gate_review": "market_gates",
        "market_gate_metrics": "market_gates",
        "save_fold_predictions": "research",
        "list_fold_predictions": "research",
        "save_oos_report": "oos_reports",
        "list_oos_reports": "oos_reports",
        "save_stock_prediction_snapshot": "predictions",
        "update_stock_prediction_outcomes": "predictions",
        "stance_metrics": "predictions",
    }

    def __init__(self, connect_fn, db_path: str) -> None:
        super().__init__(connect_fn, db_path)
        self.signals = SignalRepository(connect_fn, db_path)
        self.outcomes = OutcomeRepository(connect_fn, db_path)
        self.tuning = TuningRepository(connect_fn, db_path)
        self.market_gates = MarketGateRepository(connect_fn, db_path)
        self.research = ResearchRepository(connect_fn, db_path)
        self.oos_reports = OOSReportRepository(connect_fn, db_path)
        self.predictions = PredictionRepository(connect_fn, db_path)
        self._repositories = (
            self.signals,
            self.outcomes,
            self.tuning,
            self.market_gates,
            self.research,
            self.oos_reports,
            self.predictions,
        )

    def _repository_for_method(self, name: str):
        repository_name = self._FACADE_METHODS.get(name)
        if not repository_name:
            raise AttributeError(f"{type(self).__name__!s} object has no attribute {name!r}")
        return getattr(self, repository_name)

    def __getattr__(self, name: str):
        return getattr(self._repository_for_method(name), name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(self._FACADE_METHODS))


def _make_facade_method(name: str):
    def _method(self, *args, **kwargs):
        return getattr(self._repository_for_method(name), name)(*args, **kwargs)

    _method.__name__ = name
    _method.__qualname__ = f"ValidationRepository.{name}"
    return _method


for _method_name in ValidationRepository._FACADE_METHODS:
    setattr(ValidationRepository, _method_name, _make_facade_method(_method_name))
del _method_name
del _make_facade_method

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from .normalization import coerce_number, normalize_code
from .execution_policy import (
    cost_scenarios,
    execution_cost_components,
    order_quantities,
    policy_from_signal,
)
from .validation_benchmarks import CandidateBenchmarkCalculator
from .validation_policy import (
    execution_cost_pct as _execution_cost_pct,
    increment_reason as _increment_reason,
    legacy_validation_baseline_id,
    matches_current_validation_baseline as _matches_current_validation_baseline,
    primary_return_config as _primary_return_config,
    validation_baseline_outcome_fingerprint,
    validation_baseline_config,
)


def _mapping_get(row, key: str, default=None):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _execution_input(signal) -> Dict[str, object]:
    raw = _mapping_get(signal, "raw_json", "")
    if isinstance(raw, dict):
        result = dict(raw)
    else:
        try:
            result = json.loads(str(raw or "{}"))
        except Exception:
            result = {}
    if not isinstance(result, dict):
        result = {}
    for key in ("code", "market", "strategy_name", "turnover"):
        value = _mapping_get(signal, key, None)
        if value is not None:
            result[key] = value
    return result


def _needs_outcome_refresh(signal, strategy_name: str, current_baseline_id: str) -> bool:
    if not signal["existing_outcome_signal_id"]:
        return True
    if not _matches_current_validation_baseline(
        signal["existing_validation_baseline_id"],
        strategy_name,
        current_baseline_id,
    ):
        return True
    return (
        strategy_name in {"tomorrow_picks", "swing_picks"}
        and int(signal["existing_future_days"] or 0) < 5
        and str(signal["existing_exit_reason"] or "") in {"", "hold_to_term"}
    )


def _build_execution_record(
    signal,
    outcome: Dict[str, object],
    policy: Dict[str, object],
    scenarios: Dict[str, object],
    benchmark: Dict[str, object],
    primary_return=None,
) -> Dict[str, object]:
    label_status = str(outcome.get("label_status") or "unknown")
    settled = label_status == "settled"
    entry_price = coerce_number(outcome.get("primary_entry_price"), None)
    if entry_price is None or entry_price <= 0:
        entry_price = coerce_number(_mapping_get(signal, "price_at_signal"), 0.0)
    quantities = order_quantities(_execution_input(signal), entry_price, policy)
    order_quantity = coerce_number(quantities.get("order_quantity"))
    entry_was_filled = bool(settled or outcome.get("entry_filled"))
    filled_quantity = order_quantity if entry_was_filled else 0.0
    unfilled_entry_quantity = order_quantity if label_status == "unfilled" and not entry_was_filled else 0.0
    unfilled_exit_quantity = order_quantity if label_status == "unfilled" and entry_was_filled else 0.0
    base_cost = (scenarios.get("base") or {}) if isinstance(scenarios, dict) else {}
    gross_return = coerce_number(primary_return, None) if settled else None
    net_return = (
        round(gross_return - coerce_number(base_cost.get("total_pct")), 4)
        if gross_return is not None
        else None
    )
    if settled:
        entry_status = "filled"
        exit_status = "filled"
        fill_source = str(outcome.get("fill_source") or "simulated_daily_bar")
    elif label_status == "unfilled" and entry_was_filled:
        entry_status = "filled"
        exit_status = "unfilled"
        fill_source = "simulated_daily_bar"
    elif label_status == "unfilled":
        entry_status = "unfilled"
        exit_status = "not_entered"
        fill_source = "daily_bar_execution_rule"
    elif entry_was_filled:
        entry_status = "filled"
        exit_status = "pending" if str(outcome.get("position_status") or "") in {"open_position", "exit_pending"} else label_status
        fill_source = str(outcome.get("fill_source") or "simulated_daily_bar")
    else:
        entry_status = label_status
        exit_status = label_status
        fill_source = ""
    benchmark_ready = bool(
        settled
        and benchmark
        and all(
            isinstance(benchmark.get(key), dict)
            and benchmark[key].get("status") in {"ok", "partial"}
            and benchmark[key].get("return_pct") is not None
            for key in ("market", "industry", "style")
        )
    )
    return {
        "signal_id": int(_mapping_get(signal, "id", 0) or 0),
        "code": str(_mapping_get(signal, "code", "")),
        "label_status": label_status,
        "reason": str(outcome.get("status_reason") or outcome.get("skip_reason") or label_status),
        "entry_status": entry_status,
        "exit_status": exit_status,
        "delisting_status": str(outcome.get("delisting_status") or "not_applicable"),
        "promotion_eligible": bool(
            settled
            and outcome.get("promotion_eligible", True)
            and outcome.get("return_reproducible")
            and benchmark_ready
        ),
        **quantities,
        "actual_filled_quantity": filled_quantity,
        "actual_entry_price": round(entry_price, 4) if entry_was_filled and entry_price > 0 else None,
        "actual_exit_quantity": order_quantity if settled else 0.0,
        "actual_exit_price": (
            round(coerce_number(outcome.get("primary_exit_price")), 4) if settled else None
        ),
        "unfilled_quantity": unfilled_entry_quantity + unfilled_exit_quantity,
        "unfilled_entry_quantity": unfilled_entry_quantity,
        "unfilled_exit_quantity": unfilled_exit_quantity,
        "fill_source": fill_source,
        "fee_pct": coerce_number(base_cost.get("fee_pct")),
        "slippage_pct": coerce_number(base_cost.get("slippage_pct")),
        "impact_pct": coerce_number(base_cost.get("impact_pct")),
        "gross_return_pct": gross_return,
        "net_return_pct": net_return,
        "return_formula": "(actual_exit_price / actual_entry_price - 1) * 100 - scenario_cost_pct",
        "execution_policy_version": str(policy.get("policy_version") or ""),
        "execution_policy": policy,
        "cost_scenarios": scenarios,
        "raw_prices": outcome.get("raw_prices") or [],
        "benchmark": benchmark or {},
        "position_status": str(
            outcome.get("position_status")
            or ("closed" if settled else "open_position" if entry_was_filled else "not_entered")
        ),
        "entry_trade_date": str(outcome.get("entry_trade_date") or ""),
        "earliest_exit_date": str(outcome.get("earliest_exit_date") or ""),
        "exit_trade_date": str(outcome.get("exit_trade_date") or outcome.get("exit_date") or ""),
        "mark_price": coerce_number((outcome.get("raw_prices") or [{}])[-1].get("close"), None),
        "price_adjustment_mode": str(outcome.get("price_adjustment_mode") or ""),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


class StrategyOutcomeService:
    """Coordinates outcome backfill while the store remains the facade."""

    def __init__(self, store, compute_outcome_fn, diagnose_pending_outcome_fn) -> None:
        self.store = store
        self.repository = store.repository
        self.compute_outcome = compute_outcome_fn
        self.diagnose_pending_outcome = diagnose_pending_outcome_fn

    def update_outcomes(
        self,
        provider,
        signal_date: str = "",
        strategy_name: str = "",
        codes: Optional[Iterable[str]] = None,
        only_incomplete: bool = False,
    ) -> Dict[str, object]:
        where = "WHERE 1=1"
        params = []
        if signal_date:
            where += " AND strategy_signals.signal_date = ?"
            params.append(signal_date)
        if strategy_name:
            where += " AND strategy_signals.strategy_name = ?"
            params.append(strategy_name)
        normalized_codes: List[str] = []
        for code in codes or []:
            normalized = normalize_code(code)
            if normalized:
                normalized_codes.append(normalized)
        if normalized_codes:
            placeholders = ",".join("?" for _ in normalized_codes)
            where += " AND strategy_signals.code IN ({})".format(placeholders)
            params.extend(normalized_codes)
        if only_incomplete:
            if strategy_name:
                current_baseline_id = str(validation_baseline_config(strategy_name).get("baseline_id") or "")
                legacy_baseline_id = legacy_validation_baseline_id(strategy_name)
                outcome_fingerprint = validation_baseline_outcome_fingerprint(current_baseline_id)
                compatible_filter = "COALESCE(NULLIF(existing_outcome.validation_baseline_id, ''), ?) = ?"
                compatible_params = [legacy_baseline_id, current_baseline_id]
                if outcome_fingerprint:
                    compatible_filter += " OR COALESCE(existing_outcome.validation_baseline_id, '') LIKE ?"
                    compatible_params.append("%__outcome_{}".format(outcome_fingerprint))
                where += """
                    AND strategy_signals.signal_date < ?
                    AND NOT EXISTS (
                        SELECT 1 FROM strategy_execution_skips k WHERE k.signal_id = strategy_signals.id
                    )
                    AND (
                        existing_outcome.signal_id IS NULL
                        OR NOT ({compatible_filter})
                        OR (
                            strategy_signals.strategy_name IN ('tomorrow_picks', 'swing_picks')
                            AND COALESCE(existing_outcome.future_days, 0) < 5
                            AND COALESCE(existing_outcome.exit_reason, '') IN ('', 'hold_to_term')
                        )
                    )
                """.format(compatible_filter=compatible_filter)
                params.extend([datetime.now().date().isoformat(), *compatible_params])
            else:
                where += """
                AND strategy_signals.signal_date < ?
                AND NOT EXISTS (
                    SELECT 1 FROM strategy_execution_skips k WHERE k.signal_id = strategy_signals.id
                )
                AND (
                    NOT EXISTS (
                        SELECT 1 FROM strategy_outcomes o WHERE o.signal_id = strategy_signals.id
                    )
                    OR (
                        strategy_signals.strategy_name IN ('tomorrow_picks', 'swing_picks')
                        AND COALESCE((
                            SELECT o.future_days FROM strategy_outcomes o
                            WHERE o.signal_id = strategy_signals.id
                        ), 0) < 5
                        AND COALESCE((
                            SELECT o.exit_reason FROM strategy_outcomes o
                            WHERE o.signal_id = strategy_signals.id
                        ), '') IN ('', 'hold_to_term')
                    )
                )
                """
                params.append(datetime.now().date().isoformat())
        signals = self.repository.fetch_signals_for_outcome_update(where, params)
        if only_incomplete and strategy_name:
            signals = [
                signal
                for signal in signals
                if _needs_outcome_refresh(signal, strategy_name, current_baseline_id)
            ]

        updated = 0
        skipped = 0
        execution_skipped = 0
        pending_count = 0
        unknown_count = 0
        skipped_reasons: Dict[str, int] = {}
        benchmark_calculators: Dict[tuple, CandidateBenchmarkCalculator] = {}
        candidate_batches: Dict[tuple, List[Dict[str, object]]] = {}
        if signals:
            grouped_signals: Dict[tuple, List[dict]] = {}
            for signal in signals:
                key = (
                    str(signal["strategy_name"] or ""),
                    str(signal["strategy_version"] or ""),
                    str(signal["signal_date"] or ""),
                )
                grouped_signals.setdefault(key, []).append(dict(signal))
            outcome_columns = (
                "signal_id",
                "code",
                "next_trade_date",
                "future_days",
                "next_open",
                "next_high",
                "next_low",
                "next_close",
                "next_open_return",
                "next_close_return",
                "overnight_return",
                "intraday_high_return",
                "hold_3d_return",
                "hold_5d_return",
                "hold_10d_return",
                "hold_20d_return",
                "max_gain_3d",
                "max_drawdown_3d",
                "hit_3pct",
                "hit_5pct",
                "signal_next_close_return",
                "signal_intraday_high_return",
                "signal_hold_3d_return",
                "signal_hold_5d_return",
                "signal_hold_10d_return",
                "signal_hold_20d_return",
                "signal_max_gain_3d",
                "signal_max_drawdown_3d",
                "signal_hit_3pct",
                "signal_hit_5pct",
                "exit_return",
                "signal_exit_return",
                "exit_reason",
                "exit_days",
                "exit_date",
                "survivorship_corrected",
                "correction_reason",
                "trade_cost_pct",
                "primary_return_field",
                "primary_return",
                "primary_return_net",
                "primary_holding_days",
                "validation_baseline_id",
                "validation_baseline_json",
                "label_status",
                "delisting_status",
                "execution_policy_version",
                "execution_policy_json",
                "cost_scenarios_json",
                "raw_prices_json",
                "benchmark_json",
                "entry_price",
                "exit_price",
                "return_reproducible",
                "position_status",
                "entry_trade_date",
                "earliest_exit_date",
                "exit_trade_date",
                "price_adjustment_mode",
                "updated_at",
            )
            for _key, batch in grouped_signals.items():
                execution_records: List[Dict[str, object]] = []
                outcome_rows: List[tuple] = []
                skip_rows: List[tuple] = []
                signal_ids: List[int] = []
                for signal in batch:
                    outcome = self.compute_outcome(provider, signal)
                    if not outcome:
                        reason = self.diagnose_pending_outcome(provider, signal)
                        outcome = {
                            "label_status": "pending",
                            "status_reason": reason,
                            "delisting_status": "not_applicable",
                            "promotion_eligible": False,
                            "raw_prices": [],
                        }
                    signal_id = int(signal["id"])
                    signal_ids.append(signal_id)
                    policy = policy_from_signal(signal, str(signal["strategy_name"] or ""))
                    scenarios = cost_scenarios(_execution_input(signal), policy)
                    label_status = str(
                        outcome.get("label_status")
                        or ("unfilled" if outcome.get("excluded") else "settled")
                    )
                    outcome["label_status"] = label_status
                    if label_status != "settled":
                        reason = str(outcome.get("skip_reason") or outcome.get("status_reason") or label_status)
                        execution_record = _build_execution_record(signal, outcome, policy, scenarios, {})
                        execution_records.append(execution_record)
                        if label_status == "unfilled":
                            skip_rows.append(
                                (
                                    signal_id,
                                    signal["code"],
                                    reason,
                                    datetime.now().isoformat(timespec="seconds"),
                                )
                            )
                            execution_skipped += 1
                        elif label_status == "unknown":
                            unknown_count += 1
                        else:
                            pending_count += 1
                        skipped += 1
                        _increment_reason(skipped_reasons, reason)
                        continue

                    validation_baseline = validation_baseline_config(
                        str(signal["strategy_name"] or ""),
                        execution_policy=policy,
                    )
                    validation_baseline_id = str(validation_baseline.get("baseline_id") or "")
                    validation_baseline_json = json.dumps(validation_baseline, ensure_ascii=False, sort_keys=True)
                    primary_return_field, primary_holding_days, _ = _primary_return_config(str(signal["strategy_name"] or ""))
                    primary_holding_days = int(
                        coerce_number(outcome.get("primary_holding_days"), primary_holding_days)
                    )
                    primary_return = coerce_number(outcome.get(primary_return_field), None)
                    if primary_return is None:
                        outcome.update(
                            {
                                "label_status": "unknown",
                                "status_reason": "primary_return_missing",
                                "promotion_eligible": False,
                            }
                        )
                        execution_record = _build_execution_record(signal, outcome, policy, scenarios, {})
                        execution_records.append(execution_record)
                        skipped += 1
                        unknown_count += 1
                        _increment_reason(skipped_reasons, "primary_return_missing")
                        continue

                    batch_key = (
                        str(signal["strategy_name"] or ""),
                        str(signal["strategy_version"] or ""),
                        str(signal["signal_date"] or ""),
                    )
                    if batch_key not in benchmark_calculators:
                        candidates = self.repository.candidate_snapshots_for_date(
                            batch_key[2],
                            strategy_name=batch_key[0],
                            strategy_version=batch_key[1],
                        )
                        candidate_batches[batch_key] = candidates
                        benchmark_calculators[batch_key] = CandidateBenchmarkCalculator(provider, candidates)
                    candidate_rows = candidate_batches[batch_key]
                    selected_candidate = next(
                        (
                            item
                            for item in candidate_rows
                            if normalize_code(item.get("code")) == normalize_code(signal["code"])
                        ),
                        None,
                    )
                    if candidate_rows and (
                        not selected_candidate
                        or not selected_candidate.get("eligible")
                        or not selected_candidate.get("selected")
                        or not selected_candidate.get("point_in_time_valid")
                    ):
                        outcome["promotion_eligible"] = False
                    benchmark = benchmark_calculators[batch_key].calculate(
                        signal, outcome, primary_return_field
                    )
                    trade_cost_pct = coerce_number((scenarios.get("base") or {}).get("total_pct"))
                    primary_return_net = round(primary_return - trade_cost_pct, 4)
                    execution_record = _build_execution_record(
                        signal,
                        outcome,
                        policy,
                        scenarios,
                        benchmark,
                        primary_return=primary_return,
                    )
                    execution_records.append(execution_record)
                    if not execution_record.get("promotion_eligible"):
                        outcome["promotion_eligible"] = False
                    outcome_rows.append(
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
                            coerce_number(outcome.get("overnight_return")),
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
                            int(bool(outcome.get("survivorship_corrected"))),
                            str(outcome.get("correction_reason") or ""),
                            trade_cost_pct,
                            primary_return_field,
                            primary_return,
                            primary_return_net,
                            primary_holding_days,
                            validation_baseline_id,
                            validation_baseline_json,
                            "settled",
                            str(outcome.get("delisting_status") or "not_applicable"),
                            str(policy.get("policy_version") or ""),
                            json.dumps(policy, ensure_ascii=False, sort_keys=True),
                            json.dumps(scenarios, ensure_ascii=False, sort_keys=True),
                            json.dumps(outcome.get("raw_prices") or [], ensure_ascii=False, sort_keys=True),
                            json.dumps(benchmark, ensure_ascii=False, sort_keys=True),
                            outcome.get("primary_entry_price"),
                            outcome.get("primary_exit_price"),
                            int(bool(outcome.get("return_reproducible"))),
                            str(outcome.get("position_status") or "closed"),
                            str(outcome.get("entry_trade_date") or ""),
                            str(outcome.get("earliest_exit_date") or ""),
                            str(outcome.get("exit_trade_date") or outcome.get("exit_date") or ""),
                            str(outcome.get("price_adjustment_mode") or ""),
                            datetime.now().isoformat(timespec="seconds"),
                        )
                    )
                    updated += 1
                with self.repository.connect() as conn:
                    if signal_ids:
                        self.repository.delete_execution_skips(signal_ids, connection=conn)
                        self.repository.delete_strategy_outcomes(signal_ids, connection=conn)
                    if skip_rows:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO strategy_execution_skips
                            (signal_id, code, skip_reason, updated_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            skip_rows,
                        )
                    if execution_records:
                        self.repository.save_execution_records(execution_records, connection=conn)
                    if outcome_rows:
                        self.repository.save_strategy_outcomes(outcome_columns, outcome_rows, connection=conn)
        shadow = self.update_deepseek_shadow_outcomes(
            provider,
            signal_date=signal_date,
            strategy_name=strategy_name,
            codes=normalized_codes,
        )
        return {
            "requested": len(signals),
            "updated": updated,
            "skipped": skipped,
            "pending": pending_count,
            "unknown": unknown_count,
            "skipped_reasons": skipped_reasons,
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
        shadow_rows = self.repository.fetch_deepseek_shadow_signals(where, params)

        shadow_rows_by_batch: Dict[tuple, List[dict]] = {}
        for row in shadow_rows:
            key = (str(row["strategy_name"] or ""), str(row["strategy_version"] or ""), str(row["signal_date"] or ""))
            shadow_rows_by_batch.setdefault(key, []).append(dict(row))

        updated = 0
        skipped = 0
        for _batch_key, batch in shadow_rows_by_batch.items():
            to_save: List[tuple] = []
            shadow_ids: List[int] = []
            for shadow in batch:
                outcome = self.compute_outcome(provider, shadow)
                if not outcome or outcome.get("excluded") or outcome.get("label_status") != "settled":
                    skipped += 1
                    continue
                shadow_ids.append(int(shadow["id"]))
                to_save.append(
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
                        outcome.get("overnight_return", outcome["signal_next_close_return"]),
                        datetime.now().isoformat(timespec="seconds"),
                    )
                )
            if not (shadow_ids or to_save):
                continue
            with self.repository.connect() as conn:
                if shadow_ids:
                    self.repository.delete_deepseek_shadow_outcomes(shadow_ids, connection=conn)
                if to_save:
                    self.repository.save_deepseek_shadow_outcomes(to_save, connection=conn)
                    updated += len(to_save)
        return {"updated": updated, "skipped": skipped}

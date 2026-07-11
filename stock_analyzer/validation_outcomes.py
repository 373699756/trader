from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from .normalization import coerce_number, normalize_code


def _sv():
    from . import strategy_validation

    return strategy_validation


def _compute_outcome(provider, signal):
    return _sv()._compute_outcome(provider, signal)


def validation_baseline_config(strategy_name: str = "") -> Dict[str, object]:
    return _sv().validation_baseline_config(strategy_name)


def legacy_validation_baseline_id(strategy_name: str = "") -> str:
    return _sv().legacy_validation_baseline_id(strategy_name)


def _primary_return_config(strategy_name: str):
    return _sv()._primary_return_config(strategy_name)


def _execution_cost_pct(row) -> float:
    return _sv()._execution_cost_pct(row)


def _increment_reason(counter: Dict[str, int], reason: str) -> None:
    return _sv()._increment_reason(counter, reason)


def _diagnose_pending_outcome(provider, signal) -> str:
    return _sv()._diagnose_pending_outcome(provider, signal)


class StrategyOutcomeService:
    """Coordinates outcome backfill while the store remains the facade."""

    def __init__(self, store) -> None:
        self.store = store
        self.repository = store.repository

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
        if only_incomplete:
            if strategy_name:
                current_baseline_id = str(validation_baseline_config(strategy_name).get("baseline_id") or "")
                legacy_baseline_id = legacy_validation_baseline_id(strategy_name)
                outcome_baseline_filter = "COALESCE(NULLIF(o.validation_baseline_id, ''), ?) = ?"
                where += """
                    AND signal_date < ?
                    AND NOT EXISTS (
                        SELECT 1 FROM strategy_execution_skips k WHERE k.signal_id = strategy_signals.id
                    )
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM strategy_outcomes o
                            WHERE o.signal_id = strategy_signals.id AND {outcome_baseline_filter}
                        )
                        OR (
                            strategy_name IN ('tomorrow_picks', 'swing_picks')
                            AND COALESCE((
                                SELECT o.future_days FROM strategy_outcomes o
                                WHERE o.signal_id = strategy_signals.id AND {outcome_baseline_filter}
                            ), 0) < 5
                            AND COALESCE((
                                SELECT o.exit_reason FROM strategy_outcomes o
                                WHERE o.signal_id = strategy_signals.id AND {outcome_baseline_filter}
                            ), '') IN ('', 'hold_to_term')
                        )
                    )
                """.format(outcome_baseline_filter=outcome_baseline_filter)
                params.extend(
                    [
                        datetime.now().date().isoformat(),
                        legacy_baseline_id,
                        current_baseline_id,
                        legacy_baseline_id,
                        current_baseline_id,
                        legacy_baseline_id,
                        current_baseline_id,
                    ]
                )
            else:
                where += """
                AND signal_date < ?
                AND NOT EXISTS (
                    SELECT 1 FROM strategy_execution_skips k WHERE k.signal_id = strategy_signals.id
                )
                AND (
                    NOT EXISTS (
                        SELECT 1 FROM strategy_outcomes o WHERE o.signal_id = strategy_signals.id
                    )
                    OR (
                        strategy_name IN ('tomorrow_picks', 'swing_picks')
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

        updated = 0
        skipped = 0
        execution_skipped = 0
        skipped_reasons: Dict[str, int] = {}
        for signal in signals:
            outcome = _compute_outcome(provider, signal)
            if outcome and outcome.get("excluded"):
                reason = str(outcome.get("skip_reason") or "excluded")
                self.repository.save_execution_skip(
                    signal["id"],
                    signal["code"],
                    reason,
                    datetime.now().isoformat(timespec="seconds"),
                )
                skipped += 1
                execution_skipped += 1
                _increment_reason(skipped_reasons, reason)
                continue
            if not outcome:
                skipped += 1
                _increment_reason(skipped_reasons, _diagnose_pending_outcome(provider, signal))
                continue
            validation_baseline = validation_baseline_config(str(signal["strategy_name"] or ""))
            validation_baseline_id = str(validation_baseline.get("baseline_id") or "")
            validation_baseline_json = json.dumps(validation_baseline, ensure_ascii=False, sort_keys=True)
            primary_return_field, primary_holding_days, _ = _primary_return_config(str(signal["strategy_name"] or ""))
            trade_cost_pct = _execution_cost_pct(signal)
            primary_return = coerce_number(outcome.get(primary_return_field))
            primary_return_net = round(primary_return - trade_cost_pct, 4)
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
                "updated_at",
            )
            outcome_values = (
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
                int(bool(outcome.get("survivorship_corrected"))),
                str(outcome.get("correction_reason") or ""),
                trade_cost_pct,
                primary_return_field,
                primary_return,
                primary_return_net,
                primary_holding_days,
                validation_baseline_id,
                validation_baseline_json,
                datetime.now().isoformat(timespec="seconds"),
            )
            self.repository.save_strategy_outcome(signal["id"], outcome_columns, outcome_values)
            updated += 1
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
            "pending": max(0, skipped - execution_skipped),
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

        updated = 0
        skipped = 0
        for shadow in shadow_rows:
            outcome = _compute_outcome(provider, shadow)
            if not outcome or outcome.get("excluded"):
                skipped += 1
                continue
            self.repository.save_deepseek_shadow_outcome(
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
                )
            )
            updated += 1
        return {"updated": updated, "skipped": skipped}

import os
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code, rename_known_columns
from .production_baseline import attach_generation_provenance
from .risk_rules import _is_sealed_limit_down, simulate_exit
from .sqlite_support import sqlite_transaction
from .execution_policy import policy_from_signal
from .validation_policy import (
    EXECUTABLE_PRIMARY_RETURN_BY_STRATEGY,
    PRIMARY_RETURN_BY_STRATEGY,
    build_validation_baseline_id as _build_validation_baseline_id,
    current_replay_strategy_version,
    current_strategy_version,
    daily_limit_pct as _daily_limit_pct,
    estimated_order_amount as _estimated_order_amount,
    execution_cost_pct as _execution_cost_pct,
    exit_holding_days as _exit_holding_days,
    increment_reason as _increment_reason,
    is_primary_tomorrow_signal as _is_primary_tomorrow_signal,
    is_primary_validation_signal as _is_primary_validation_signal,
    is_replay_version as _is_replay_version,
    is_unbuyable_limit_up as _is_unbuyable_limit_up,
    legacy_validation_baseline_id,
    liquidity_slippage_pct as _liquidity_slippage_pct,
    mapping_get as _mapping_get,
    market_impact_cost_pct,
    matches_current_validation_baseline as _matches_current_validation_baseline,
    outcome_ready as _outcome_ready,
    primary_return_config as _primary_return_config,
    stored_or_current_trade_cost_pct as _stored_or_current_trade_cost_pct,
    stored_validation_baseline_id as _stored_validation_baseline_id,
    strategy_exit_policy as _strategy_exit_policy,
    tail_auction_slippage_pct,
    validation_baseline_config,
)
from .validation_serialization import (
    oos_report_row_to_dict as _oos_report_row_to_dict,
    signal_row_to_dict as _row_to_dict,
    tuning_row_to_dict as _tuning_row_to_dict,
)
from .validation_stance import (
    compute_stance_outcome as _compute_stance_outcome,
    stance_exit_policy as _stance_exit_policy,
)
from .validation_statistics import (
    average as _avg,
    daily_metrics as _daily_metrics,
    deepseek_action as _deepseek_action,
    deepseek_avoid_or_veto as _deepseek_avoid_or_veto,
    deepseek_blend_alpha as _deepseek_blend_alpha,
    deepseek_budget_recommendation as _deepseek_budget_recommendation,
    deepseek_counterfactual_n as _deepseek_counterfactual_n,
    deepseek_counterfactual_topn as _deepseek_counterfactual_topn,
    deepseek_covered as _deepseek_covered,
    deepseek_group_delta as _deepseek_group_delta,
    deepseek_local_rank as _deepseek_local_rank,
    deepseek_shadow_rank as _deepseek_shadow_rank,
    deepseek_token_cost_summary as _deepseek_token_cost_summary,
    deepseek_token_value_metrics as _deepseek_token_value_metrics,
    has_deepseek_review as _has_deepseek_review,
    market_gate_hit as _market_gate_hit,
    market_gate_outcome_summary as _market_gate_outcome_summary,
    mean_confidence_interval as _mean_confidence_interval,
    next_day_compare as _next_day_compare,
    next_low_return_from_signal as _next_low_return_from_signal,
    portfolio_max_drawdown as _portfolio_max_drawdown,
    rate as _rate,
    return_summary as _return_summary,
    top_k_sensitivity as _top_k_sensitivity,
    wilson_lower_bound as _wilson_lower_bound,
    write_deepseek_attribution_snapshot as _write_deepseek_attribution_snapshot,
)
from .validation_outcomes import StrategyOutcomeService
from .validation_repository import ValidationRepository
from .validation_schema import ValidationSchemaManager
from .validation_metrics import ValidationBaselineService, ValidationMetricsService

_connect_validation_db = sqlite_transaction


class StrategyValidationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.schema = ValidationSchemaManager(_connect_validation_db, self.db_path)
        self._init_db()
        self.repository = ValidationRepository(_connect_validation_db, self.db_path)
        self.outcomes = StrategyOutcomeService(
            self,
            compute_outcome_fn=_compute_outcome,
            diagnose_pending_outcome_fn=_diagnose_pending_outcome,
        )
        self.metrics_service = ValidationMetricsService(self)
        self.baseline_service = ValidationBaselineService(self)

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
        rows = [dict(row) for row in rows or [] if isinstance(row, dict)]
        candidate_rows = [dict(row) for row in candidate_rows or [] if isinstance(row, dict)]
        shadow_rows = [dict(row) for row in deepseek_shadow_rows or [] if isinstance(row, dict)]
        metadata = dict(batch_metadata or {})
        generation_meta = {
            "generated_at": signal_time,
            "market_filter": metadata.get("market_filter") or "all",
            "top_n": len(rows),
            "strategy_version": strategy_version,
        }
        provenance = metadata.get("generation") if isinstance(metadata.get("generation"), dict) else None
        if provenance is None:
            provenance = attach_generation_provenance(
                generation_meta,
                strategy_name,
                rows,
                candidate_rows,
            )
        for row in rows:
            row.setdefault("generation", provenance)
        metadata["generation"] = provenance
        for row in shadow_rows:
            row.setdefault("generation", provenance)
        policy = execution_policy or {}
        return self.repository.save_signals(
            strategy_name,
            strategy_version,
            signal_time,
            rows,
            deepseek_shadow_rows=shadow_rows,
            candidate_rows=candidate_rows,
            batch_metadata=metadata,
            execution_policy=policy,
        )

    def list_signal_dates(self, strategy_name: str = "") -> List[Dict[str, object]]:
        return self.repository.list_signal_dates(strategy_name=strategy_name)

    def existing_validation_dates(self, strategy_name: str, replay_version: str = "") -> List[str]:
        return self.repository.existing_validation_dates(strategy_name, replay_version=replay_version)

    def signals_for_date(self, signal_date: str, strategy_name: str = "") -> List[Dict[str, object]]:
        return self.repository.signals_for_date(signal_date, strategy_name=strategy_name)

    def latest_signal_rows(self, strategy_name: str) -> List[Dict[str, object]]:
        return self.repository.latest_signal_rows(strategy_name)

    def candidate_snapshots_for_date(
        self,
        signal_date: str,
        strategy_name: str = "",
        strategy_version: str = "",
    ) -> List[Dict[str, object]]:
        return self.repository.candidate_snapshots_for_date(
            signal_date,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
        )

    def latest_candidate_snapshots(self, strategy_name: str) -> List[Dict[str, object]]:
        return self.repository.latest_candidate_snapshots(strategy_name)

    def execution_records_for_date(
        self,
        signal_date: str,
        strategy_name: str = "",
    ) -> List[Dict[str, object]]:
        return self.repository.execution_records_for_date(signal_date, strategy_name=strategy_name)

    def audit_point_in_time(self, strategy_name: str = "", sample_size: int = 30) -> Dict[str, object]:
        from .validation_audit import audit_point_in_time

        return audit_point_in_time(self, strategy_name=strategy_name, sample_size=sample_size)

    def prune_strategies(self, allowed_strategies: Iterable[str]) -> Dict[str, int]:
        return self.repository.prune_strategies(allowed_strategies)

    def save_tuning_run(
        self,
        strategy_name: str,
        days: int,
        plan: Dict[str, object],
        metrics: Dict[str, object],
        deepseek_review: Dict[str, object],
    ) -> Dict[str, object]:
        return self.repository.save_tuning_run(strategy_name, days, plan, metrics, deepseek_review)

    def latest_tuning_run(self, strategy_name: str) -> Dict[str, object]:
        return self.repository.latest_tuning_run(strategy_name)

    def list_tuning_runs(self, strategy_name: str, limit: int = 10) -> List[Dict[str, object]]:
        return self.repository.list_tuning_runs(strategy_name, limit=limit)

    def live_weight_samples(self, strategy_name: str, days: int = 120) -> List[Dict[str, object]]:
        return self.repository.live_weight_samples(strategy_name, days=days)

    def signal_codes(
        self,
        signal_date: str = "",
        strategy_name: str = "",
        limit: int = 500,
    ) -> List[Dict[str, object]]:
        return self.repository.signal_codes(signal_date=signal_date, strategy_name=strategy_name, limit=limit)

    def update_outcomes(
        self,
        provider,
        signal_date: str = "",
        strategy_name: str = "",
        codes: Optional[Iterable[str]] = None,
        only_incomplete: bool = False,
    ) -> Dict[str, object]:
        return self.outcomes.update_outcomes(
            provider,
            signal_date=signal_date,
            strategy_name=strategy_name,
            codes=codes,
            only_incomplete=only_incomplete,
        )

    def update_deepseek_shadow_outcomes(
        self,
        provider,
        signal_date: str = "",
        strategy_name: str = "",
        codes: Optional[Iterable[str]] = None,
    ) -> Dict[str, int]:
        return self.outcomes.update_deepseek_shadow_outcomes(
            provider,
            signal_date=signal_date,
            strategy_name=strategy_name,
            codes=codes,
        )

    def metrics(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        return self.metrics_service.metrics(strategy_name=strategy_name, days=days)

    def deepseek_attribution(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        return self.metrics_service.deepseek_attribution(strategy_name=strategy_name, days=days)

    def signal_status_counts(
        self,
        strategy_name: str = "",
        days: int = 20,
        strategy_version: str = "",
    ) -> Dict[str, object]:
        return self.repository.signal_status_counts(
            strategy_name=strategy_name,
            days=days,
            strategy_version=strategy_version,
        )

    def validation_baseline_status(self, strategy_name: str = "", days: int = 120) -> Dict[str, object]:
        return self.baseline_service.status(strategy_name=strategy_name, days=days)

    def validation_baseline_backfill_candidates(
        self,
        strategy_name: str,
        days: int = 120,
        limit: int = 500,
    ) -> Dict[str, object]:
        return self.baseline_service.backfill_candidates(
            strategy_name=strategy_name,
            days=days,
            limit=limit,
        )

    def execution_skip_count(
        self,
        strategy_name: str = "",
        days: int = 20,
        strategy_version: str = "",
    ) -> int:
        return self.repository.execution_skip_count(
            strategy_name=strategy_name,
            days=days,
            strategy_version=strategy_version,
        )

    def save_market_gate_review(self, market_gate: Dict[str, object], market_filter: str = "all") -> Dict[str, object]:
        return self.repository.save_market_gate_review(market_gate, market_filter=market_filter)

    def market_gate_metrics(self, days: int = 120) -> Dict[str, object]:
        return self.repository.market_gate_metrics(days=days)

    def save_oos_report(
        self,
        report: Dict[str, object],
        trigger: str = "manual",
    ) -> Dict[str, object]:
        return self.repository.save_oos_report(report, trigger=trigger)

    def list_oos_reports(
        self,
        strategy_name: str = "",
        limit: int = 50,
    ) -> List[Dict[str, object]]:
        return self.repository.list_oos_reports(strategy_name=strategy_name, limit=limit)

    def save_stock_prediction_snapshot(self, payload: Dict[str, object]) -> Dict[str, object]:
        return self.repository.save_stock_prediction_snapshot(payload)

    def update_stock_prediction_outcomes(self, provider, days: int = 120) -> Dict[str, object]:
        return self.repository.update_stock_prediction_outcomes(provider, days=days)

    def stance_metrics(self, days: int = 120) -> Dict[str, object]:
        return self.repository.stance_metrics(days=days)

    def _init_db(self) -> None:
        self.schema.init_db()

def _compute_outcome_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    compact = text[:10].replace("-", "")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(compact if fmt == "%Y%m%d" else text[:10], fmt).date()
        except Exception:
            continue
    return None


def _survivorship_correction_enabled() -> bool:
    return bool(getattr(config, "ENABLE_SURVIVORSHIP_CORRECTION", False))


def _survivorship_signal_is_stale(signal) -> bool:
    if not _survivorship_correction_enabled():
        return False
    signal_dt = _compute_outcome_date(_mapping_get(signal, "signal_date"))
    if signal_dt is None:
        return False
    stale_days = max(0, int(coerce_number(getattr(config, "SURVIVORSHIP_CORRECTION_STALE_DAYS", 30), 30)))
    return (datetime.now().date() - signal_dt).days >= stale_days


def _survivorship_default_outcome(signal, latest_row, reason: str) -> Optional[Dict[str, object]]:
    return {
        "label_status": "unknown",
        "status_reason": str(reason or "missing_history"),
        "delisting_status": "unknown",
        "promotion_eligible": False,
        "raw_prices": _raw_price_rows(pd.DataFrame([latest_row])) if latest_row is not None else [],
    }


def _should_apply_truncated_survivorship_correction(signal, future: pd.DataFrame, primary_days: int, exit_days: int) -> bool:
    if future is None or future.empty:
        return False
    if not _survivorship_signal_is_stale(signal):
        return False
    required_days = max(1, int(primary_days or 1), int(exit_days or 1))
    return len(future) < required_days


def _signal_is_stale(signal) -> bool:
    signal_dt = _compute_outcome_date(_mapping_get(signal, "signal_date"))
    if signal_dt is None:
        return False
    stale_days = max(
        0,
        int(coerce_number(getattr(config, "SURVIVORSHIP_CORRECTION_STALE_DAYS", 30), 30)),
    )
    return (datetime.now().date() - signal_dt).days >= stale_days


def _provider_security_state(provider, code: str) -> Dict[str, str]:
    for method_name in ("get_security_status", "get_stock_status", "security_status"):
        method = getattr(provider, method_name, None)
        if not callable(method):
            continue
        try:
            raw = method(code)
        except Exception:
            continue
        if isinstance(raw, dict):
            if raw.get("is_delisted"):
                return {"status": "delisted", "source": method_name}
            if raw.get("is_suspended"):
                return {"status": "suspended", "source": method_name}
            text = str(raw.get("status") or raw.get("state") or "")
        else:
            text = str(raw or "")
        normalized = text.strip().lower()
        if any(token in normalized for token in ("delist", "退市", "摘牌")):
            return {"status": "delisted", "source": method_name}
        if any(token in normalized for token in ("suspend", "停牌")):
            return {"status": "suspended", "source": method_name}
        if any(token in normalized for token in ("active", "trading", "正常", "交易中")):
            return {"status": "active", "source": method_name}
    return {"status": "unknown", "source": "unavailable"}


def _stateful_unresolved_outcome(
    label_status: str,
    reason: str,
    security_state: Dict[str, str],
    raw_prices: List[Dict[str, object]] = None,
    delisting_status: str = "",
    error: str = "",
    entry_filled: bool = False,
    primary_entry_price: float = None,
) -> Dict[str, object]:
    result = {
        "label_status": str(label_status or "unknown"),
        "status_reason": str(reason or "unknown"),
        "delisting_status": delisting_status
        or ("unpriced_delisting" if security_state.get("status") == "delisted" else "not_applicable"),
        "security_state": dict(security_state or {}),
        "promotion_eligible": False,
        "raw_prices": raw_prices or [],
    }
    if error:
        result["error"] = error
    if entry_filled:
        result["entry_filled"] = True
        result["primary_entry_price"] = primary_entry_price
    if label_status == "unfilled":
        result["excluded"] = True
        result["skip_reason"] = str(reason or "unfilled")
    return result


def _raw_price_rows(frame: pd.DataFrame) -> List[Dict[str, object]]:
    if frame is None or frame.empty:
        return []
    rows: List[Dict[str, object]] = []
    for _, row in frame.iterrows():
        close = coerce_number(row.get("price")) or coerce_number(row.get("close"))
        rows.append(
            {
                "trade_date": str(row.get("trade_date") or ""),
                "open": coerce_number(row.get("open")),
                "high": coerce_number(row.get("high")),
                "low": coerce_number(row.get("low")),
                "close": close,
                "prev_close": coerce_number(row.get("prev_close")),
                "volume": coerce_number(row.get("volume")),
                "turnover": coerce_number(row.get("turnover")),
            }
        )
    return rows


def _compute_outcome(provider, signal: sqlite3.Row) -> Optional[Dict[str, object]]:
    code = str(_mapping_get(signal, "code", ""))
    strategy_name = str(_mapping_get(signal, "strategy_name", ""))
    frozen_policy = policy_from_signal(signal, strategy_name)
    security_state = _provider_security_state(provider, code)
    try:
        history = provider.get_history(code, days=180)
    except Exception as exc:
        return _stateful_unresolved_outcome(
            "unknown",
            "history_fetch_failed",
            security_state,
            error=str(exc),
        )
    if history is None or history.empty or "trade_date" not in history.columns:
        reason = "delisted_last_price_unavailable" if security_state["status"] == "delisted" else "missing_history"
        return _stateful_unresolved_outcome("unknown", reason, security_state)
    df = rename_known_columns(history.copy()).sort_values("trade_date").reset_index(drop=True)
    if "price" not in df.columns:
        return _stateful_unresolved_outcome("unknown", "missing_close_price", security_state)
    df["prev_close"] = df["price"].shift(1)
    signal_date = str(_mapping_get(signal, "signal_date", "")).replace("-", "")
    future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(drop=True)
    if future.empty:
        raw_prices = _raw_price_rows(df.tail(1))
        if security_state["status"] == "delisted":
            return _stateful_unresolved_outcome(
                "unfilled",
                "delisted_before_entry",
                security_state,
                raw_prices=raw_prices,
                delisting_status="delisted_before_entry",
            )
        if security_state["status"] == "suspended":
            return _stateful_unresolved_outcome(
                "unfilled",
                "suspended_at_entry",
                security_state,
                raw_prices=raw_prices,
            )
        status = "unknown" if _signal_is_stale(signal) else "pending"
        reason = "no_future_trade_unclassified" if status == "unknown" else "not_mature_no_future_trade"
        return _stateful_unresolved_outcome(status, reason, security_state, raw_prices=raw_prices)
    first = future.iloc[0]
    previous_rows = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) <= signal_date]
    previous_close = coerce_number(previous_rows.iloc[-1].get("price")) if not previous_rows.empty else coerce_number(first.get("prev_close"))
    configured_limits = frozen_policy.get("price_limits") or {}
    default_limit_pct = _daily_limit_pct(str(signal["code"]), str(_mapping_get(signal, "market", "")))
    limit_key = "chinext_star_pct" if default_limit_pct >= 20.0 else "main_board_pct"
    limit_pct = max(1.0, coerce_number(configured_limits.get(limit_key), default_limit_pct))
    primary_return_field, primary_days, _ = _primary_return_config(strategy_name)
    if strategy_name in {"tomorrow_picks", "swing_picks"} and _is_unbuyable_limit_up(
        first,
        previous_close,
        limit_pct,
    ):
        return {
            "excluded": True,
            "label_status": "unfilled",
            "skip_reason": "unbuyable_limit_up",
            "status_reason": "unbuyable_limit_up",
            "delisting_status": "not_applicable",
            "raw_prices": _raw_price_rows(pd.concat([previous_rows.tail(1), future.head(1)])),
        }
    open_entry = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    signal_entry = coerce_number(signal["price_at_signal"])
    close = coerce_number(first.get("price"))
    high = coerce_number(first.get("high"))
    low = coerce_number(first.get("low"))
    if open_entry <= 0:
        reason = "suspended_at_entry" if security_state["status"] == "suspended" else "invalid_entry_price"
        status = "unfilled" if security_state["status"] == "suspended" else "unknown"
        return _stateful_unresolved_outcome(
            status,
            reason,
            security_state,
            raw_prices=_raw_price_rows(pd.concat([previous_rows.tail(1), future.head(1)])),
        )
    if signal_entry <= 0:
        signal_entry = open_entry
    if strategy_name == "tomorrow_picks":
        high_open_pct = (open_entry / signal_entry - 1.0) * 100.0 if signal_entry > 0 else 0.0
        high_open_skip_pct = coerce_number(
            (frozen_policy.get("entry") or {}).get("high_open_skip_pct"),
            getattr(config, "TOMORROW_HIGH_OPEN_SKIP_PCT", 3.0),
        )
        if high_open_pct > high_open_skip_pct:
            return {
                "excluded": True,
                "label_status": "unfilled",
                "skip_reason": "tomorrow_high_open_chase",
                "status_reason": "tomorrow_high_open_chase",
                "delisting_status": "not_applicable",
                "next_open_return": round(high_open_pct, 4),
                "threshold_pct": round(high_open_skip_pct, 4),
                "raw_prices": _raw_price_rows(pd.concat([previous_rows.tail(1), future.head(1)])),
            }
    future_days = len(future)
    exit_days = max(
        1,
        int(
            coerce_number(
                (frozen_policy.get("exit") or {}).get("holding_days"),
                _exit_holding_days(strategy_name),
            )
        ),
    )
    required_days = max(1, primary_days if primary_return_field not in {"exit_return", "signal_exit_return"} else exit_days)
    if strategy_name == "tomorrow_picks" and _is_sealed_limit_down(first, previous_close, limit_pct):
        return {
            "label_status": "unfilled",
            "excluded": True,
            "skip_reason": "next_close_limit_down_unfilled",
            "status_reason": "next_close_limit_down_unfilled",
            "delisting_status": "not_applicable",
            "promotion_eligible": False,
            "entry_filled": True,
            "primary_entry_price": round(open_entry, 4),
            "raw_prices": _raw_price_rows(pd.concat([previous_rows.tail(1), future.head(1)])),
        }
    window = future.head(max(1, primary_days))
    last = window.iloc[-1]
    hold_3d_close = _window_close(future, 3, coerce_number(last.get("price")))
    hold_5d_close = _window_close(future, 5, close)
    hold_10d_close = _window_close(future, 10, hold_5d_close)
    hold_20d_close = _window_close(future, 20, hold_10d_close)
    three_day_window = future.head(3)
    max_high = max(coerce_number(value) for value in three_day_window.get("high", pd.Series([high])).tolist())
    min_low = min(coerce_number(value) for value in three_day_window.get("low", pd.Series([low])).tolist())
    exit_policy = dict(frozen_policy.get("exit") or _strategy_exit_policy(strategy_name, exit_days, limit_pct))
    exit_policy["holding_days"] = exit_days
    exit_policy["limit_down_pct"] = limit_pct
    open_exit = simulate_exit(future, open_entry, holding_days=exit_days, policy=exit_policy)
    signal_exit = simulate_exit(future, signal_entry, holding_days=exit_days, policy=exit_policy)
    primary_exit_result = open_exit if primary_return_field == "exit_return" else signal_exit
    if (
        future_days < required_days
        and security_state["status"] != "delisted"
        and str(primary_exit_result.get("exit_reason") or "") == "hold_to_term"
    ):
        status = "unknown" if _signal_is_stale(signal) else "pending"
        reason = "truncated_future_unclassified" if status == "unknown" else "insufficient_future_data"
        return _stateful_unresolved_outcome(
            status,
            reason,
            security_state,
            raw_prices=_raw_price_rows(pd.concat([previous_rows.tail(1), future])),
            entry_filled=True,
            primary_entry_price=round(open_entry, 4),
        )
    if primary_return_field == "exit_return" and str(primary_exit_result.get("exit_reason") or "").endswith(
        "_unfilled"
    ):
        return {
            "label_status": "unfilled",
            "excluded": True,
            "skip_reason": str(primary_exit_result.get("exit_reason") or "exit_unfilled"),
            "status_reason": str(primary_exit_result.get("exit_reason") or "exit_unfilled"),
            "delisting_status": (
                "delisted_exit_unfilled" if security_state["status"] == "delisted" else "not_applicable"
            ),
            "promotion_eligible": False,
            "entry_filled": True,
            "primary_entry_price": round(open_entry, 4),
            "raw_prices": _raw_price_rows(
                pd.concat([previous_rows.tail(1), future.head(max(20, exit_days))]).drop_duplicates(
                    subset=["trade_date"]
                )
            ),
        }
    outcome = {
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
        "exit_reason": primary_exit_result.get("exit_reason", "hold_to_term"),
        "exit_days": primary_exit_result.get("exit_days", 0),
        "exit_date": primary_exit_result.get("exit_date", ""),
        "label_status": "settled",
        "status_reason": "settled",
        "delisting_status": "not_applicable",
        "promotion_eligible": True,
        "primary_entry_price": round(signal_entry if primary_return_field.startswith("signal_") else open_entry, 4),
        "primary_exit_price": round(
            open_exit.get("exit_price", close) if primary_return_field == "exit_return" else close,
            4,
        ),
        "raw_prices": _raw_price_rows(
            pd.concat([previous_rows.tail(1), future.head(max(20, exit_days))]).drop_duplicates(subset=["trade_date"])
        ),
        "return_reproducible": True,
    }
    if security_state["status"] == "delisted" and future_days < max(1, exit_days):
        if str(outcome.get("exit_reason") or "") == "hold_to_term":
            outcome["survivorship_corrected"] = True
            outcome["correction_reason"] = "delisted_last_tradable_liquidation"
            outcome["delisting_status"] = "liquidated_last_tradable"
            outcome["exit_reason"] = "delisted_last_tradable_liquidation"
            if not outcome.get("exit_date"):
                outcome["exit_date"] = str(future.iloc[-1].get("trade_date", ""))
            outcome["exit_days"] = max(int(coerce_number(outcome.get("exit_days"))), min(exit_days, len(future)))
        else:
            outcome["delisting_status"] = "exited_before_delisting"
    return outcome


def _window_close(future: pd.DataFrame, days: int, fallback: float) -> float:
    window = future.head(days)
    if window.empty:
        return coerce_number(fallback)
    return coerce_number(window.iloc[-1].get("price")) or coerce_number(fallback)


def _diagnose_pending_outcome(provider, signal) -> str:
    try:
        history = provider.get_history(signal["code"], days=180)
    except Exception:
        return "history_fetch_failed"
    if history is None or history.empty:
        return "missing_history"
    if "trade_date" not in history.columns:
        return "missing_trade_date"
    try:
        df = history.sort_values("trade_date").reset_index(drop=True)
        signal_date = str(_mapping_get(signal, "signal_date")).replace("-", "")
        future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(drop=True)
    except Exception:
        return "history_parse_failed"
    if future.empty:
        if _survivorship_correction_enabled() and not _survivorship_signal_is_stale(signal):
            return "not_stale_for_survivorship_correction"
        return "not_mature_no_future_trade"
    first = future.iloc[0]
    open_entry = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    if open_entry <= 0:
        return "invalid_entry_price"
    return "insufficient_future_data"

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
from .execution_policy import build_execution_policy, policy_from_signal
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
    mean_confidence_interval as _mean_confidence_interval,
    next_day_compare as _next_day_compare,
    next_low_return_from_signal as _next_low_return_from_signal,
    portfolio_max_drawdown as _portfolio_max_drawdown,
    rate as _rate,
    top_k_sensitivity as _top_k_sensitivity,
    wilson_lower_bound as _wilson_lower_bound,
)
from .validation_outcomes import (
    StrategyOutcomeService,
    register_compute_outcome_fn,
)
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

    def save_shadow_analysis_signals(
        self,
        strategy_name: str,
        strategy_version: str,
        signal_time: str,
        rows: Iterable[Dict[str, object]],
    ) -> Dict[str, int]:
        return self.repository.save_shadow_analysis_signals(
            strategy_name,
            strategy_version,
            signal_time,
            rows,
        )

    def save_deepseek_analysis_batch(self, batch: Dict[str, object]) -> Dict[str, object]:
        return self.repository.save_deepseek_analysis_batch(batch)

    def save_deepseek_candidate_features(
        self,
        batch: Dict[str, object],
        rows: Iterable[Dict[str, object]],
    ) -> Dict[str, int]:
        return self.repository.save_deepseek_candidate_features(batch, rows)

    def latest_deepseek_candidate_features(
        self,
        strategy_name: str,
        codes: Iterable[str],
        cutoff_at: str,
        prompt_version: str = "",
        model_name: str = "",
        feature_schema_version: str = "",
    ) -> Dict[str, Dict[str, object]]:
        return self.repository.latest_deepseek_candidate_features(
            strategy_name,
            codes,
            cutoff_at,
            prompt_version=prompt_version,
            model_name=model_name,
            feature_schema_version=feature_schema_version,
        )

    def save_deepseek_counterfactual_outcome(self, row: Dict[str, object]) -> Dict[str, object]:
        return self.repository.save_deepseek_counterfactual_outcome(row)

    def save_deepseek_counterfactual_outcomes(
        self,
        rows: Iterable[Dict[str, object]],
    ) -> List[Dict[str, object]]:
        return self.repository.save_deepseek_counterfactual_outcomes(rows)

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
    ) -> Dict[str, object]:
        return self.repository.save_tuning_run(strategy_name, days, plan, metrics, {})

    def latest_tuning_run(self, strategy_name: str) -> Dict[str, object]:
        return self.repository.latest_tuning_run(strategy_name)

    def list_tuning_runs(self, strategy_name: str, limit: int = 10) -> List[Dict[str, object]]:
        return self.repository.list_tuning_runs(strategy_name, limit=limit)

    def save_fold_predictions(
        self,
        experiment_id: str,
        fold_id: str,
        strategy_name: str,
        rows: Iterable[Dict[str, object]],
        **metadata,
    ) -> Dict[str, object]:
        return self.repository.save_fold_predictions(
            experiment_id,
            fold_id,
            strategy_name,
            rows,
            **metadata,
        )

    def list_fold_predictions(
        self,
        experiment_id: str,
        strategy_name: str = "",
        fold_id: str = "",
        limit: int = 500,
    ) -> List[Dict[str, object]]:
        return self.repository.list_fold_predictions(
            experiment_id,
            strategy_name=strategy_name,
            fold_id=fold_id,
            limit=limit,
        )

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
    position_status: str = "",
    entry_trade_date: str = "",
    earliest_exit_date: str = "",
) -> Dict[str, object]:
    result = {
        "label_status": str(label_status or "unknown"),
        "status_reason": str(reason or "unknown"),
        "delisting_status": delisting_status
        or ("unpriced_delisting" if security_state.get("status") == "delisted" else "not_applicable"),
        "security_state": dict(security_state or {}),
        "promotion_eligible": False,
        "raw_prices": raw_prices or [],
        "position_status": position_status or ("open_position" if entry_filled else "not_entered"),
        "entry_trade_date": str(entry_trade_date or ""),
        "earliest_exit_date": str(earliest_exit_date or ""),
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


def _load_execution_history(provider, code: str, days: int = 180):
    method = getattr(provider, "get_execution_bars_raw", None)
    if callable(method):
        history = method(code, days=days)
        source = "provider_raw_execution_history"
    else:
        # Compatibility path for old providers and fixtures. Such rows remain
        # non-promotable unless the frame explicitly declares raw prices.
        history = provider.get_history(code, days=days)
        source = "legacy_history_fallback"
    mode = str((getattr(history, "attrs", {}) or {}).get("price_adjustment_mode") or "")
    return history, mode, source


def _is_close_auction_entry_policy(policy: Dict[str, object]) -> bool:
    return str((policy.get("entry") or {}).get("timing") or "") == "same_trade_day_close_auction"


def _is_post_1430_entry_policy(policy: Dict[str, object]) -> bool:
    return str((policy.get("entry") or {}).get("timing") or "") == "same_trade_day_after_1430"


def _first_tradable_exit_after_limit_down(
    future: pd.DataFrame,
    limit_pct: float,
    previous_close: float,
) -> Optional[Dict[str, object]]:
    prior_close = coerce_number(previous_close)
    for idx, row in future.iterrows():
        if _is_sealed_limit_down(row, coerce_number(row.get("prev_close")) or prior_close, limit_pct):
            prior_close = coerce_number(row.get("price")) or prior_close
            continue
        exit_price = coerce_number(row.get("open")) or coerce_number(row.get("price"))
        if exit_price > 0:
            return {"row": row, "index": int(idx), "exit_price": exit_price}
    return None


def _compute_close_auction_outcome(
    provider,
    signal,
    frozen_policy: Dict[str, object],
    security_state: Dict[str, str],
) -> Dict[str, object]:
    code = str(_mapping_get(signal, "code", ""))
    strategy_name = str(_mapping_get(signal, "strategy_name", ""))
    post_1430 = _is_post_1430_entry_policy(frozen_policy)
    try:
        history, price_mode, fill_source = _load_execution_history(provider, code, days=180)
    except Exception as exc:
        return _stateful_unresolved_outcome(
            "unknown", "execution_history_fetch_failed", security_state, error=str(exc)
        )
    if history is None or history.empty or "trade_date" not in history.columns:
        return _stateful_unresolved_outcome("unknown", "missing_execution_history", security_state)
    df = rename_known_columns(history.copy()).sort_values("trade_date").reset_index(drop=True)
    if "price" not in df.columns:
        return _stateful_unresolved_outcome("unknown", "missing_raw_close_price", security_state)
    df["prev_close"] = df["price"].shift(1)
    signal_date = str(_mapping_get(signal, "signal_date", "")).replace("-", "")
    date_keys = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    entry_rows = df[date_keys == signal_date]
    prior_rows = df[date_keys < signal_date]
    future = df[date_keys > signal_date].reset_index(drop=True)
    raw_context = pd.concat([prior_rows.tail(1), entry_rows.tail(1), future.head(20)])
    if entry_rows.empty:
        return _stateful_unresolved_outcome(
            "unknown",
            "signal_day_raw_bar_missing",
            security_state,
            raw_prices=_raw_price_rows(raw_context),
        )
    entry_row = entry_rows.iloc[-1]
    entry_price = (
        coerce_number(_mapping_get(signal, "price_at_signal", 0.0))
        if post_1430
        else coerce_number(entry_row.get("price"))
    )
    prior_close = (
        coerce_number(prior_rows.iloc[-1].get("price"))
        if not prior_rows.empty
        else coerce_number(entry_row.get("prev_close"))
    )
    configured_limits = frozen_policy.get("price_limits") or {}
    default_limit_pct = _daily_limit_pct(code, str(_mapping_get(signal, "market", "")))
    limit_key = "chinext_star_pct" if default_limit_pct >= 20.0 else "main_board_pct"
    limit_pct = max(1.0, coerce_number(configured_limits.get(limit_key), default_limit_pct))
    if entry_price <= 0:
        return _stateful_unresolved_outcome(
            "unfilled",
            "invalid_post_1430_reference_price" if post_1430 else "invalid_close_auction_entry_price",
            security_state,
            raw_prices=_raw_price_rows(raw_context),
        )
    # 14:30 后参考入场不能使用收盘后的完整日线形态判断是否可买，
    # 否则会把 14:30 之后才封板的股票错误标成当时无法成交。
    signal_limit_price = prior_close * (1.0 + limit_pct / 100.0) if prior_close > 0 else 0.0
    entry_unbuyable = (
        entry_price >= signal_limit_price * 0.995
        if post_1430 and signal_limit_price > 0
        else _is_unbuyable_limit_up(entry_row, prior_close, limit_pct)
    )
    if entry_unbuyable:
        return _stateful_unresolved_outcome(
            "unfilled",
            "post_1430_limit_up_unfilled" if post_1430 else "close_auction_limit_up_unfilled",
            security_state,
            raw_prices=_raw_price_rows(raw_context),
            position_status="not_entered",
        )
    entry_date = str(entry_row.get("trade_date") or _mapping_get(signal, "signal_date", ""))
    earliest_exit_date = str(future.iloc[0].get("trade_date")) if not future.empty else ""
    raw_prices = _raw_price_rows(raw_context.drop_duplicates(subset=["trade_date"]))
    if future.empty:
        status = "unknown" if _signal_is_stale(signal) else "pending"
        return _stateful_unresolved_outcome(
            status,
            "post_1430_entry_waiting_future_trade" if post_1430 else "close_auction_entry_filled_waiting_t1",
            security_state,
            raw_prices=raw_prices,
            entry_filled=True,
            primary_entry_price=round(entry_price, 4),
            position_status="open_position",
            entry_trade_date=entry_date,
        )

    first = future.iloc[0]
    next_open = coerce_number(first.get("open")) or coerce_number(first.get("price"))
    next_close = coerce_number(first.get("price"))
    next_high = coerce_number(first.get("high"))
    next_low = coerce_number(first.get("low"))
    exit_row = first
    exit_price = next_close
    exit_reason = "next_trade_day_close_auction"
    exit_index = 0
    if post_1430:
        exit_policy = dict(frozen_policy.get("exit") or {})
        holding_days = 1 if strategy_name == "tomorrow_picks" else max(2, int(coerce_number(exit_policy.get("holding_days"), 5)))
        exit_policy["holding_days"] = holding_days
        exit_policy["limit_down_pct"] = limit_pct
        if strategy_name == "swing_picks":
            exit_policy["take_profit_earliest_offset_days"] = max(1, int(coerce_number(exit_policy.get("take_profit_earliest_offset_days"), 1)))
        exit_result = simulate_exit(future, entry_price, holding_days=holding_days, policy=exit_policy)
        if not exit_result.get("ok"):
            return _stateful_unresolved_outcome("pending", str(exit_result.get("reason") or "exit_not_ready"), security_state, raw_prices=raw_prices, entry_filled=True, primary_entry_price=round(entry_price, 4), position_status="open_position", entry_trade_date=entry_date, earliest_exit_date=earliest_exit_date)
        exit_reason = str(exit_result.get("exit_reason") or "hold_to_term")
        if len(future) < holding_days and exit_reason == "hold_to_term":
            if security_state.get("status") == "delisted":
                exit_index = len(future) - 1
                exit_row = future.iloc[exit_index]
                exit_price = coerce_number(exit_row.get("price")) or coerce_number(exit_row.get("open"))
                exit_reason = "delisted_last_tradable_liquidation"
            else:
                return _stateful_unresolved_outcome("pending", "insufficient_future_data", security_state, raw_prices=raw_prices, entry_filled=True, primary_entry_price=round(entry_price, 4), position_status="open_position", entry_trade_date=entry_date, earliest_exit_date=earliest_exit_date)
        if exit_reason.endswith("_unfilled"):
            return _stateful_unresolved_outcome("pending", exit_reason, security_state, raw_prices=raw_prices, entry_filled=True, primary_entry_price=round(entry_price, 4), position_status="exit_pending", entry_trade_date=entry_date, earliest_exit_date=earliest_exit_date)
        if exit_reason != "delisted_last_tradable_liquidation":
            exit_price = coerce_number(exit_result.get("exit_price"))
            exit_index = max(0, int(coerce_number(exit_result.get("exit_days"), 1)) - 1)
            exit_row = future.iloc[min(len(future) - 1, exit_index)]
    elif _is_sealed_limit_down(first, entry_price, limit_pct):
        delayed = _first_tradable_exit_after_limit_down(future.iloc[1:], limit_pct, next_close)
        if delayed is None:
            return _stateful_unresolved_outcome("pending", "t1_limit_down_exit_pending", security_state, raw_prices=raw_prices, entry_filled=True, primary_entry_price=round(entry_price, 4), position_status="exit_pending", entry_trade_date=entry_date, earliest_exit_date=earliest_exit_date)
        exit_row = delayed["row"]
        exit_index = delayed["index"]
        exit_price = coerce_number(delayed["exit_price"])
        exit_reason = "next_close_limit_down_delayed_to_tradable_open"
    if exit_price <= 0:
        return _stateful_unresolved_outcome(
            "pending",
            "exit_price_unavailable",
            security_state,
            raw_prices=raw_prices,
            entry_filled=True,
            primary_entry_price=round(entry_price, 4),
            position_status="exit_pending",
            entry_trade_date=entry_date,
            earliest_exit_date=earliest_exit_date,
        )

    settled_context = pd.concat(
        [prior_rows.tail(1), entry_rows.tail(1), future.iloc[: exit_index + 1]]
    ).drop_duplicates(subset=["trade_date"])
    raw_prices = _raw_price_rows(settled_context)

    hold_3d_close = _window_close(future, 3, next_close)
    hold_5d_close = _window_close(future, 5, hold_3d_close)
    hold_10d_close = _window_close(future, 10, hold_5d_close)
    hold_20d_close = _window_close(future, 20, hold_10d_close)
    three_day_window = future.head(3)
    max_high = max(coerce_number(value) for value in three_day_window.get("high", pd.Series([next_high])).tolist())
    min_low = min(coerce_number(value) for value in three_day_window.get("low", pd.Series([next_low])).tolist())
    realized_exit_return = round((exit_price / entry_price - 1) * 100, 4)
    overnight_open_return = (
        round((next_open / entry_price - 1) * 100, 4) if next_open > 0 else 0.0
    )
    raw_price_verified = price_mode == "raw"
    return {
        "next_trade_date": str(first.get("trade_date")),
        "future_days": len(future),
        "next_open": round(next_open, 4),
        "next_high": round(next_high, 4),
        "next_low": round(next_low, 4),
        "next_close": round(next_close, 4),
        "next_open_return": round((next_open / entry_price - 1) * 100, 4) if next_open > 0 else 0.0,
        "next_close_return": round((next_close / next_open - 1) * 100, 4) if next_open > 0 else 0.0,
        "overnight_return": (
            overnight_open_return if post_1430 and strategy_name == "tomorrow_picks"
            else realized_exit_return if strategy_name == "tomorrow_picks"
            else 0.0
        ),
        "intraday_high_return": round((next_high / next_open - 1) * 100, 4) if next_open > 0 else 0.0,
        "hold_3d_return": round((hold_3d_close / entry_price - 1) * 100, 4),
        "hold_5d_return": round((hold_5d_close / entry_price - 1) * 100, 4),
        "hold_10d_return": round((hold_10d_close / entry_price - 1) * 100, 4),
        "hold_20d_return": round((hold_20d_close / entry_price - 1) * 100, 4),
        "max_gain_3d": round((max_high / entry_price - 1) * 100, 4),
        "max_drawdown_3d": round((min_low / entry_price - 1) * 100, 4),
        "hit_3pct": max_high / entry_price - 1 >= 0.03,
        "hit_5pct": max_high / entry_price - 1 >= 0.05,
        "signal_next_close_return": round((next_close / entry_price - 1) * 100, 4),
        "signal_intraday_high_return": round((next_high / entry_price - 1) * 100, 4),
        "signal_hold_3d_return": round((hold_3d_close / entry_price - 1) * 100, 4),
        "signal_hold_5d_return": round((hold_5d_close / entry_price - 1) * 100, 4),
        "signal_hold_10d_return": round((hold_10d_close / entry_price - 1) * 100, 4),
        "signal_hold_20d_return": round((hold_20d_close / entry_price - 1) * 100, 4),
        "signal_max_gain_3d": round((max_high / entry_price - 1) * 100, 4),
        "signal_max_drawdown_3d": round((min_low / entry_price - 1) * 100, 4),
        "signal_hit_3pct": max_high / entry_price - 1 >= 0.03,
        "signal_hit_5pct": max_high / entry_price - 1 >= 0.05,
        "exit_return": realized_exit_return,
        "signal_exit_return": realized_exit_return,
        "exit_reason": exit_reason,
        "exit_days": int(exit_index) + 1,
        "exit_date": str(exit_row.get("trade_date")),
        "label_status": "settled",
        "status_reason": "settled",
        "position_status": "closed",
        "entry_trade_date": entry_date,
        "earliest_exit_date": earliest_exit_date,
        "exit_trade_date": str(exit_row.get("trade_date")),
        "delisting_status": (
            "liquidated_last_tradable"
            if exit_reason == "delisted_last_tradable_liquidation"
            else "not_applicable"
        ),
        "survivorship_corrected": exit_reason == "delisted_last_tradable_liquidation",
        "correction_reason": (
            "delisted_last_tradable_liquidation"
            if exit_reason == "delisted_last_tradable_liquidation"
            else ""
        ),
        "promotion_eligible": raw_price_verified,
        "primary_entry_price": round(entry_price, 4),
        "primary_exit_price": round(exit_price, 4),
        "primary_holding_days": int(exit_index) + 1,
        "price_adjustment_mode": price_mode or "unknown",
        "fill_source": "{}_signal_reference".format(fill_source) if post_1430 else fill_source,
        "raw_prices": raw_prices,
        "return_reproducible": raw_price_verified,
    }


def _compute_today_continuation_outcome(
    provider,
    signal,
    security_state: Dict[str, str],
) -> Dict[str, object]:
    """Label the 14:30-to-close observation without inventing a trade fill."""
    code = str(_mapping_get(signal, "code", ""))
    try:
        history, price_mode, source = _load_execution_history(provider, code, days=180)
    except Exception as exc:
        return _stateful_unresolved_outcome(
            "unknown",
            "today_execution_history_fetch_failed",
            security_state,
            error=str(exc),
        )
    if history is None or history.empty or "trade_date" not in history.columns:
        return _stateful_unresolved_outcome("unknown", "today_signal_day_bar_missing", security_state)
    df = rename_known_columns(history.copy()).sort_values("trade_date").reset_index(drop=True)
    if "price" not in df.columns:
        return _stateful_unresolved_outcome("unknown", "today_raw_close_missing", security_state)
    df["prev_close"] = df["price"].shift(1)
    signal_date = str(_mapping_get(signal, "signal_date", "")).replace("-", "")
    date_keys = df["trade_date"].astype(str).str.replace("-", "", regex=False)
    signal_rows = df[date_keys == signal_date]
    prior_rows = df[date_keys < signal_date]
    raw_context = pd.concat([prior_rows.tail(1), signal_rows.tail(1)])
    if signal_rows.empty:
        status = "unknown" if _signal_is_stale(signal) else "pending"
        return _stateful_unresolved_outcome(
            status,
            "today_signal_day_bar_missing",
            security_state,
            raw_prices=_raw_price_rows(raw_context),
        )
    reference_price = coerce_number(_mapping_get(signal, "price_at_signal", 0.0))
    close_row = signal_rows.iloc[-1]
    close_price = coerce_number(close_row.get("price"))
    if reference_price <= 0 or close_price <= 0:
        return _stateful_unresolved_outcome(
            "unknown",
            "today_reference_or_close_invalid",
            security_state,
            raw_prices=_raw_price_rows(raw_context),
        )
    observed_return = round((close_price / reference_price - 1.0) * 100.0, 4)
    trade_date = str(close_row.get("trade_date") or _mapping_get(signal, "signal_date", ""))
    raw_verified = price_mode == "raw"
    return {
        "next_trade_date": trade_date,
        "future_days": 0,
        "next_open": None,
        "next_high": None,
        "next_low": None,
        "next_close": None,
        "next_open_return": None,
        "next_close_return": None,
        "overnight_return": None,
        "intraday_high_return": None,
        "hold_3d_return": None,
        "hold_5d_return": None,
        "hold_10d_return": None,
        "hold_20d_return": None,
        "max_gain_3d": None,
        "max_drawdown_3d": None,
        "hit_3pct": None,
        "hit_5pct": None,
        "signal_next_close_return": None,
        "signal_intraday_high_return": None,
        "signal_hold_3d_return": None,
        "signal_hold_5d_return": None,
        "signal_hold_10d_return": None,
        "signal_hold_20d_return": None,
        "signal_max_gain_3d": None,
        "signal_max_drawdown_3d": None,
        "signal_hit_3pct": None,
        "signal_hit_5pct": None,
        "today_continuation_return": observed_return,
        "exit_return": observed_return,
        "signal_exit_return": observed_return,
        "exit_reason": "same_day_close_observation",
        "exit_days": 0,
        "exit_date": trade_date,
        "label_status": "settled",
        "status_reason": "settled",
        "position_status": "observation_closed",
        "entry_trade_date": trade_date,
        "earliest_exit_date": trade_date,
        "exit_trade_date": trade_date,
        "delisting_status": "not_applicable",
        "promotion_eligible": raw_verified,
        "primary_entry_price": round(reference_price, 4),
        "primary_exit_price": round(close_price, 4),
        "primary_holding_days": 0,
        "price_adjustment_mode": price_mode or "unknown",
        "fill_source": "{}_same_day_observation".format(source),
        "raw_prices": _raw_price_rows(raw_context.drop_duplicates(subset=["trade_date"])),
        "return_reproducible": raw_verified,
    }


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
    if strategy_name == "short_term":
        return _compute_today_continuation_outcome(provider, signal, security_state)
    if strategy_name in {"tomorrow_picks", "swing_picks"} and _is_post_1430_entry_policy(frozen_policy):
        return _compute_close_auction_outcome(provider, signal, frozen_policy, security_state)
    if strategy_name == "tomorrow_picks" and _is_close_auction_entry_policy(frozen_policy):
        return _compute_close_auction_outcome(provider, signal, frozen_policy, security_state)
    try:
        method = getattr(provider, "get_factor_bars_adjusted", None)
        if callable(method):
            history = method(code, days=180)
        else:
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
        return _stateful_unresolved_outcome(
            "pending",
            "next_close_limit_down_exit_pending",
            security_state,
            raw_prices=_raw_price_rows(pd.concat([previous_rows.tail(1), future.head(1)])),
            entry_filled=True,
            primary_entry_price=round(open_entry, 4),
            position_status="exit_pending",
            entry_trade_date=str(first.get("trade_date") or ""),
        )
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
        return _stateful_unresolved_outcome(
            "pending",
            str(primary_exit_result.get("exit_reason") or "exit_pending"),
            security_state,
            raw_prices=_raw_price_rows(
                pd.concat([previous_rows.tail(1), future.head(max(20, exit_days))]).drop_duplicates(
                    subset=["trade_date"]
                )
            ),
            entry_filled=True,
            primary_entry_price=round(open_entry, 4),
            position_status="exit_pending",
            entry_trade_date=str(first.get("trade_date") or ""),
        )
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


compute_outcome = _compute_outcome
register_compute_outcome_fn(compute_outcome)

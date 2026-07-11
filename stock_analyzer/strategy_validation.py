import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code
from .risk_rules import simulate_exit
from .runtime_json import atomic_write_json
from .validation_outcomes import StrategyOutcomeService
from .validation_repository import ValidationRepository
from .validation_schema import ValidationSchemaManager
from .validation_services import ValidationBaselineService, ValidationMetricsService


PRIMARY_RETURN_BY_STRATEGY = {
    "short_term": ("signal_next_close_return", 1, "盘中观察至次日收盘（辅助）"),
    "tomorrow_picks": ("next_close_return", 1, "次日开盘至收盘"),
    "swing_picks": ("exit_return", 5, "次日开盘后2-5日可执行退出"),
}

EXECUTABLE_PRIMARY_RETURN_BY_STRATEGY = {
    **PRIMARY_RETURN_BY_STRATEGY,
}


def current_strategy_version(strategy_name: str) -> str:
    return {
        "short_term": str(getattr(config, "SHORT_TERM_STRATEGY_VERSION", "short_term_v2_observation")),
        "tomorrow_picks": str(getattr(config, "TOMORROW_STRATEGY_VERSION", "tomorrow_picks_v9_next_open")),
        "swing_picks": str(getattr(config, "SWING_STRATEGY_VERSION", "swing_2_5d_v3_next_open_exit")),
    }.get(str(strategy_name or ""), "")


def current_replay_strategy_version(strategy_name: str) -> str:
    suffix = str(getattr(config, "VALIDATION_REPLAY_VERSION_SUFFIX", "replay_v2_production"))
    return "{}_{}".format(str(strategy_name or ""), suffix) if strategy_name else ""


@contextmanager
def _connect_validation_db(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class StrategyValidationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.schema = ValidationSchemaManager(_connect_validation_db, self.db_path)
        self._init_db()
        self.repository = ValidationRepository(_connect_validation_db, self.db_path)
        self.outcomes = StrategyOutcomeService(self)
        self.metrics_service = ValidationMetricsService(self)
        self.baseline_service = ValidationBaselineService(self)

    def save_signals(
        self,
        strategy_name: str,
        strategy_version: str,
        signal_time: str,
        rows: Iterable[Dict[str, object]],
        deepseek_shadow_rows: Optional[Iterable[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        return self.repository.save_signals(
            strategy_name,
            strategy_version,
            signal_time,
            rows,
            deepseek_shadow_rows=deepseek_shadow_rows,
        )

    def list_signal_dates(self, strategy_name: str = "") -> List[Dict[str, object]]:
        return self.repository.list_signal_dates(strategy_name=strategy_name)

    def existing_validation_dates(self, strategy_name: str, replay_version: str = "") -> List[str]:
        return self.repository.existing_validation_dates(strategy_name, replay_version=replay_version)

    def signals_for_date(self, signal_date: str, strategy_name: str = "") -> List[Dict[str, object]]:
        return self.repository.signals_for_date(signal_date, strategy_name=strategy_name)

    def latest_signal_rows(self, strategy_name: str) -> List[Dict[str, object]]:
        return self.repository.latest_signal_rows(strategy_name)

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
    if not _survivorship_signal_is_stale(signal):
        return None
    strategy_name = str(_mapping_get(signal, "strategy_name", ""))
    primary_days = _primary_return_config(strategy_name)[1]
    exit_days = _exit_holding_days(strategy_name)
    future_days = max(1, primary_days, exit_days)
    signal_entry = coerce_number(_mapping_get(signal, "price_at_signal"))
    latest_price = coerce_number(latest_row.get("price") if latest_row is not None else None) or coerce_number(
        latest_row.get("close") if latest_row is not None else None
    )
    entry = signal_entry if signal_entry > 0 else latest_price
    if entry <= 0:
        return None
    loss_pct = min(0.0, coerce_number(getattr(config, "DELISTED_DEFAULT_LOSS_PCT", -30.0), -30.0))
    exit_price = max(0.0, entry * (1.0 + loss_pct / 100.0))
    trade_date = str(latest_row.get("trade_date") if latest_row is not None else _mapping_get(signal, "signal_date", ""))
    high = max(entry, exit_price)
    low = min(entry, exit_price)
    return {
        "next_trade_date": trade_date,
        "future_days": future_days,
        "next_open": round(entry, 4),
        "next_high": round(high, 4),
        "next_low": round(low, 4),
        "next_close": round(exit_price, 4),
        "next_open_return": 0.0,
        "next_close_return": round(loss_pct, 4),
        "intraday_high_return": 0.0,
        "hold_3d_return": round(loss_pct, 4),
        "hold_5d_return": round(loss_pct, 4),
        "hold_10d_return": round(loss_pct, 4),
        "hold_20d_return": round(loss_pct, 4),
        "max_gain_3d": 0.0,
        "max_drawdown_3d": round(loss_pct, 4),
        "hit_3pct": False,
        "hit_5pct": False,
        "signal_next_close_return": round(loss_pct, 4),
        "signal_intraday_high_return": 0.0,
        "signal_hold_3d_return": round(loss_pct, 4),
        "signal_hold_5d_return": round(loss_pct, 4),
        "signal_hold_10d_return": round(loss_pct, 4),
        "signal_hold_20d_return": round(loss_pct, 4),
        "signal_max_gain_3d": 0.0,
        "signal_max_drawdown_3d": round(loss_pct, 4),
        "signal_hit_3pct": False,
        "signal_hit_5pct": False,
        "exit_return": round(loss_pct, 4),
        "signal_exit_return": round(loss_pct, 4),
        "exit_reason": reason,
        "exit_days": future_days,
        "exit_date": trade_date,
        "survivorship_corrected": True,
        "correction_reason": reason,
    }


def _should_apply_truncated_survivorship_correction(signal, future: pd.DataFrame, primary_days: int, exit_days: int) -> bool:
    if future is None or future.empty:
        return False
    if not _survivorship_signal_is_stale(signal):
        return False
    required_days = max(1, int(primary_days or 1), int(exit_days or 1))
    return len(future) < required_days


def _compute_outcome(provider, signal: sqlite3.Row) -> Optional[Dict[str, object]]:
    history = provider.get_history(signal["code"], days=180)
    if history is None or history.empty or "trade_date" not in history.columns:
        return _survivorship_default_outcome(signal, None, "survivorship_no_history_default_loss")
    df = history.sort_values("trade_date").reset_index(drop=True)
    df["prev_close"] = df["price"].shift(1)
    signal_date = str(signal["signal_date"]).replace("-", "")
    future = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) > signal_date].reset_index(drop=True)
    if future.empty:
        return _survivorship_default_outcome(signal, df.iloc[-1], "survivorship_no_future_default_loss")
    first = future.iloc[0]
    previous_rows = df[df["trade_date"].astype(str).str.replace("-", "", regex=False) <= signal_date]
    previous_close = coerce_number(previous_rows.iloc[-1].get("price")) if not previous_rows.empty else coerce_number(first.get("prev_close"))
    limit_pct = _daily_limit_pct(str(signal["code"]), str(_mapping_get(signal, "market", "")))
    strategy_name = str(_mapping_get(signal, "strategy_name", ""))
    primary_return_field, primary_days, _ = _primary_return_config(strategy_name)
    if strategy_name in {"tomorrow_picks", "swing_picks"} and _is_unbuyable_limit_up(
        first,
        previous_close,
        limit_pct,
    ):
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
    if strategy_name == "tomorrow_picks":
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
    window = future.head(max(1, primary_days))
    last = window.iloc[-1]
    hold_3d_close = coerce_number(last.get("price"))
    hold_5d_close = _window_close(future, 5, close)
    hold_10d_close = _window_close(future, 10, hold_5d_close)
    hold_20d_close = _window_close(future, 20, hold_10d_close)
    max_high = max(coerce_number(value) for value in window.get("high", pd.Series([high])).tolist())
    min_low = min(coerce_number(value) for value in window.get("low", pd.Series([low])).tolist())
    exit_days = _exit_holding_days(strategy_name)
    exit_policy = _strategy_exit_policy(strategy_name, exit_days, limit_pct)
    open_exit = simulate_exit(future, open_entry, holding_days=exit_days, policy=exit_policy)
    signal_exit = simulate_exit(future, signal_entry, holding_days=exit_days, policy=exit_policy)
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
        "exit_reason": signal_exit.get("exit_reason", open_exit.get("exit_reason", "hold_to_term")),
        "exit_days": signal_exit.get("exit_days", open_exit.get("exit_days", 0)),
        "exit_date": signal_exit.get("exit_date", open_exit.get("exit_date", "")),
    }
    if _should_apply_truncated_survivorship_correction(signal, future, primary_days, exit_days):
        outcome["survivorship_corrected"] = True
        outcome["correction_reason"] = "survivorship_truncated_future_liquidation"
        if str(outcome.get("exit_reason") or "") == "hold_to_term":
            outcome["exit_reason"] = "survivorship_liquidation_exit"
        if not outcome.get("exit_date"):
            outcome["exit_date"] = str(future.iloc[-1].get("trade_date", ""))
        outcome["exit_days"] = max(int(coerce_number(outcome.get("exit_days"))), min(exit_days, len(future)))
    return outcome


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
    return PRIMARY_RETURN_BY_STRATEGY.get(
        strategy_name,
        ("signal_next_close_return", 1, "次日"),
    )


def _build_validation_baseline_id(
    primary_field: str,
    tail_enabled: bool,
    impact_enabled: bool,
    survivorship_enabled: bool,
) -> str:
    parts = [
        "primary_{}".format(primary_field),
        "liquidity_cost",
        "tail" if tail_enabled else "no_tail",
        "impact" if impact_enabled else "no_impact",
        "survivorship" if survivorship_enabled else "no_survivorship",
    ]
    return "validation_{}".format("__".join(parts))


def legacy_validation_baseline_id(strategy_name: str = "") -> str:
    primary_field, _, _ = _primary_return_config(strategy_name)
    return _build_validation_baseline_id(
        primary_field,
        tail_enabled=False,
        impact_enabled=False,
        survivorship_enabled=False,
    )


def _stored_validation_baseline_id(stored_baseline_id: object, strategy_name: str = "") -> str:
    baseline_id = str(stored_baseline_id or "").strip()
    if baseline_id:
        return baseline_id
    return legacy_validation_baseline_id(strategy_name)


def _matches_current_validation_baseline(
    stored_baseline_id: object,
    strategy_name: str = "",
    current_baseline_id: str = "",
) -> bool:
    expected = current_baseline_id or str(validation_baseline_config(strategy_name).get("baseline_id") or "")
    return _stored_validation_baseline_id(stored_baseline_id, strategy_name) == expected


def validation_baseline_config(strategy_name: str = "") -> Dict[str, object]:
    primary_field, primary_days, primary_label = _primary_return_config(strategy_name)
    tail_enabled = bool(getattr(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False))
    impact_enabled = bool(getattr(config, "ENABLE_MARKET_IMPACT", False))
    survivorship_enabled = bool(getattr(config, "ENABLE_SURVIVORSHIP_CORRECTION", False))
    return {
        "baseline_id": _build_validation_baseline_id(
            primary_field,
            tail_enabled=tail_enabled,
            impact_enabled=impact_enabled,
            survivorship_enabled=survivorship_enabled,
        ),
        "strategy_name": str(strategy_name or ""),
        "primary_return_field": primary_field,
        "primary_holding_days": primary_days,
        "primary_horizon_label": primary_label,
        "net_return_formula": "{} - trade_cost_pct".format(primary_field),
        "cost_model": {
            "base_trade_cost_pct": coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25), 0.25),
            "liquidity_slippage_enabled": True,
            "tail_auction_slippage_enabled": tail_enabled,
            "market_impact_enabled": impact_enabled,
            "portfolio_capital": coerce_number(getattr(config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000), 1_000_000),
            "default_position_pct": coerce_number(getattr(config, "VALIDATION_DEFAULT_POSITION_PCT", 10.0), 10.0),
        },
        "survivorship": {
            "enabled": survivorship_enabled,
            "stale_days": int(coerce_number(getattr(config, "SURVIVORSHIP_CORRECTION_STALE_DAYS", 30), 30)),
            "default_loss_pct": coerce_number(getattr(config, "DELISTED_DEFAULT_LOSS_PCT", -30.0), -30.0),
        },
        "separate_legacy_baseline_required": bool(tail_enabled or impact_enabled or survivorship_enabled),
    }


def _strategy_exit_policy(strategy_name: str, holding_days: int, limit_down_pct: float) -> Dict[str, object]:
    policy = {"holding_days": holding_days, "limit_down_pct": limit_down_pct}
    if strategy_name not in {"tomorrow_picks", "swing_picks"}:
        return policy
    prefix = "TOMORROW_AUXILIARY" if strategy_name == "tomorrow_picks" else "SWING_VALIDATION"
    policy.update(
        {
            "take_profit_pct": max(
                0.0,
                coerce_number(getattr(config, "{}_TAKE_PROFIT_PCT".format(prefix), 8.0), 8.0),
            ),
            "stop_loss_pct": max(
                0.0,
                coerce_number(getattr(config, "{}_STOP_LOSS_PCT".format(prefix), 5.0), 5.0),
            ),
            "trailing_stop_pct": max(
                0.0,
                coerce_number(getattr(config, "{}_TRAILING_STOP_PCT".format(prefix), 4.0), 4.0),
            ),
        }
    )
    return policy


def _exit_holding_days(strategy_name: str) -> int:
    if strategy_name in {"tomorrow_picks", "swing_picks"}:
        return 5
    return _primary_return_config(strategy_name)[1]


def _outcome_ready(row, holding_days: int) -> bool:
    if bool(_mapping_get(row, "survivorship_corrected", False)):
        return True
    future_days = int(_mapping_get(row, "future_days", 1) or 1)
    if future_days >= max(1, int(holding_days or 1)):
        return True
    exit_reason = str(_mapping_get(row, "exit_reason", "") or "")
    exit_days = int(_mapping_get(row, "exit_days", 0) or 0)
    return exit_reason not in {"", "hold_to_term"} and 0 < exit_days <= future_days


def _increment_reason(counter: Dict[str, int], reason: str) -> None:
    key = str(reason or "unknown").strip() or "unknown"
    counter[key] = counter.get(key, 0) + 1


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


def _estimated_order_amount(row) -> float:
    capital = max(0.0, coerce_number(getattr(config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000.0), 1_000_000.0))
    if capital <= 0:
        return 0.0
    weight_pct = coerce_number(_mapping_get(row, "suggested_weight"), None)
    if weight_pct is None:
        trade_action = _mapping_get(row, "trade_action", {})
        if isinstance(trade_action, dict):
            position = coerce_number(trade_action.get("position_size"), None)
            if position is not None:
                weight_pct = position * 100.0 if position <= 1.0 else position
    if weight_pct is None or weight_pct <= 0:
        weight_pct = coerce_number(getattr(config, "VALIDATION_DEFAULT_POSITION_PCT", 10.0), 10.0)
    return capital * max(0.0, weight_pct) / 100.0


def tail_auction_slippage_pct(row, base_slippage: float = 0.0) -> float:
    if not bool(getattr(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False)):
        return 0.0
    base = max(0.0, coerce_number(base_slippage))
    daily_turnover = coerce_number(_mapping_get(row, "turnover"))
    order_amount = _estimated_order_amount(row)
    if daily_turnover <= 0 or order_amount <= 0:
        return round(base + 0.5, 4)
    liquidity_ratio = max(0.0001, coerce_number(getattr(config, "TAIL_AUCTION_LIQUIDITY_RATIO", 0.05), 0.05))
    effective_liquidity = daily_turnover * liquidity_ratio
    if effective_liquidity <= 0:
        return round(base + 0.5, 4)
    participation = order_amount / effective_liquidity
    max_extra = max(0.0, coerce_number(getattr(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.8), 0.8))
    extra = min(max_extra, participation * 100.0 * 0.02)
    return round(base + extra, 4)


def market_impact_cost_pct(row) -> float:
    if not bool(getattr(config, "ENABLE_MARKET_IMPACT", False)):
        return 0.0
    adv = coerce_number(_mapping_get(row, "adv_20d")) or coerce_number(_mapping_get(row, "turnover"))
    order_amount = _estimated_order_amount(row)
    if adv <= 0 or order_amount <= 0:
        return 0.0
    coefficient = max(0.0, coerce_number(getattr(config, "MARKET_IMPACT_COEFFICIENT", 0.1), 0.1))
    max_cost = max(0.0, coerce_number(getattr(config, "MARKET_IMPACT_MAX_COST_PCT", 5.0), 5.0))
    participation = max(0.0, order_amount / adv)
    impact = coefficient * math.sqrt(participation) * 100.0
    return round(min(max_cost, impact), 4)


def _execution_cost_pct(row) -> float:
    base = coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25), 0.25)
    liquidity = _liquidity_slippage_pct(coerce_number(_mapping_get(row, "turnover")))
    tail_slippage = tail_auction_slippage_pct(row)
    impact = market_impact_cost_pct(row)
    return round(base + liquidity + tail_slippage + impact, 4)


def _stored_or_current_trade_cost_pct(row) -> float:
    stored = coerce_number(_mapping_get(row, "stored_trade_cost_pct"), None)
    if stored is None:
        stored = coerce_number(_mapping_get(row, "trade_cost_pct"), None)
    if stored is not None and stored > 0:
        return round(stored, 4)
    return _execution_cost_pct(row)


def _is_primary_tomorrow_signal(rank, raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        raw = {}
    tier = str(raw.get("tier") or "").strip()
    if tier:
        return tier == "primary_watch"
    return int(coerce_number(rank)) <= int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 10))


def _is_primary_validation_signal(strategy_name: str, rank, raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("execution_allowed") is False:
        return False
    tier = str(raw.get("tier") or "").strip()
    if tier:
        return tier == "primary_watch"
    if strategy_name == "tomorrow_picks":
        return _is_primary_tomorrow_signal(rank, raw)
    return True


def _row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("reasons_json", "raw_json"):
        try:
            item[key.replace("_json", "")] = json.loads(item.get(key) or "[]")
        except Exception:
            item[key.replace("_json", "")] = [] if key == "reasons_json" else {}
    item["trade_cost_pct"] = _stored_or_current_trade_cost_pct(item)
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


def _oos_report_row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("report_json", "baseline_status_json", "validation_gate_json", "requirements_json"):
        target = key.replace("_json", "")
        try:
            item[target] = json.loads(item.get(key) or "{}")
        except Exception:
            item[target] = {}
        item.pop(key, None)
    item["gate_blocked"] = bool(item.get("gate_blocked"))
    report = item.get("report") if isinstance(item.get("report"), dict) else {}
    if report:
        item.setdefault("summary", report.get("summary") or {})
        item.setdefault("validation_baseline", report.get("validation_baseline") or {})
        item.setdefault("validation_baseline_id", report.get("validation_baseline_id") or item.get("baseline_id"))
    return item


def _avg(values) -> float:
    clean = [coerce_number(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else 0.0


def _mean_confidence_interval(values, z_score: float = 1.96):
    clean = [coerce_number(value) for value in values if value is not None]
    if len(clean) < 2:
        return None, None
    mean = sum(clean) / len(clean)
    variance = sum((value - mean) ** 2 for value in clean) / (len(clean) - 1)
    margin = max(0.0, coerce_number(z_score, 1.96)) * math.sqrt(variance / len(clean))
    return round(mean - margin, 4), round(mean + margin, 4)


def _wilson_lower_bound(values, z_score: float = 1.96):
    clean = [bool(value) for value in values]
    if not clean:
        return None
    count = len(clean)
    successes = sum(1 for value in clean if value)
    proportion = successes / count
    z = max(0.0, coerce_number(z_score, 1.96))
    denominator = 1 + z * z / count
    center = proportion + z * z / (2 * count)
    spread = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * count)) / count)
    return round(max(0.0, (center - spread) / denominator) * 100.0, 2)


def _portfolio_max_drawdown(daily_rows: List[Dict[str, object]]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for row in sorted(daily_rows or [], key=lambda item: str(item.get("signal_date") or "")):
        daily_return = coerce_number(row.get("avg_primary_return_net")) / 100.0
        equity *= max(0.0, 1.0 + daily_return)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity / peak - 1.0) * 100.0)
    return round(max_drawdown, 4)


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


def _deepseek_token_cost_summary(rows: List[Dict[str, object]]) -> Dict[str, object]:
    calls: Dict[str, Dict[str, object]] = {}
    missing_usage = 0
    for row in rows or []:
        raw = row.get("_raw") if isinstance(row, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        cost_hint = raw.get("deepseek_cost_hint") if isinstance(raw.get("deepseek_cost_hint"), dict) else {}
        usage = raw.get("deepseek_usage") if isinstance(raw.get("deepseek_usage"), dict) else {}
        usage_total_tokens = coerce_number(usage.get("total_tokens"), 0.0)
        total_tokens = coerce_number(
            cost_hint.get("total_tokens"),
            coerce_number(raw.get("deepseek_total_tokens"), usage_total_tokens),
        )
        prompt_tokens = coerce_number(
            cost_hint.get("prompt_tokens"),
            coerce_number(usage.get("prompt_tokens"), 0.0),
        )
        completion_tokens = coerce_number(
            cost_hint.get("completion_tokens"),
            coerce_number(usage.get("completion_tokens"), 0.0),
        )
        billable_tokens = coerce_number(
            cost_hint.get("billable_total_tokens"),
            coerce_number(raw.get("deepseek_billable_tokens"), total_tokens),
        )
        estimated_cost = coerce_number(cost_hint.get("estimated_cost"), 0.0)
        if total_tokens <= 0:
            missing_usage += 1
            continue
        call_id = str(raw.get("deepseek_call_id") or "").strip()
        if not call_id:
            call_id = "{}:{}:{}".format(row.get("signal_date", ""), row.get("strategy_name", ""), row.get("rank", ""))
        existing = calls.get(call_id)
        if existing and coerce_number(existing.get("total_tokens")) >= total_tokens:
            continue
        calls[call_id] = {
            "call_id": call_id,
            "source": str(raw.get("deepseek_call_source") or ""),
            "model": str(cost_hint.get("model") or ""),
            "model_tier": str(cost_hint.get("model_tier") or ""),
            "cached": bool(cost_hint.get("cached")),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "billable_total_tokens": billable_tokens,
            "estimated_cost": estimated_cost,
        }
    call_rows = list(calls.values())
    total_tokens = sum(coerce_number(item.get("total_tokens")) for item in call_rows)
    billable_tokens = sum(coerce_number(item.get("billable_total_tokens")) for item in call_rows)
    return {
        "call_count": len(call_rows),
        "total_tokens": round(total_tokens, 4),
        "billable_total_tokens": round(billable_tokens, 4),
        "estimated_cost": round(sum(coerce_number(item.get("estimated_cost")) for item in call_rows), 6),
        "missing_usage_count": missing_usage,
        "has_usage": total_tokens > 0,
        "calls": call_rows[:20],
    }


def _deepseek_token_value_metrics(
    avoid_veto_rows: List[Dict[str, object]],
    counterfactual: Dict[str, object],
    token_cost: Dict[str, object],
) -> Dict[str, object]:
    skipped_loss_saved = sum(max(0.0, -coerce_number(row.get("_primary_return_net"))) for row in avoid_veto_rows)
    false_positive_loss = sum(max(0.0, coerce_number(row.get("_primary_return_net"))) for row in avoid_veto_rows)
    rerank_delta = coerce_number(counterfactual.get("avg_return_delta_pct")) * int(
        counterfactual.get("sample_count") or 0
    )
    net_value = round(skipped_loss_saved - false_positive_loss + rerank_delta, 4)
    total_tokens = coerce_number(token_cost.get("total_tokens"))
    value_per_1k = round(net_value / total_tokens * 1000.0, 4) if total_tokens > 0 else None
    return {
        "skipped_loss_saved_pct": round(skipped_loss_saved, 4),
        "false_positive_profit_loss_pct": round(false_positive_loss, 4),
        "rerank_net_return_delta_pct": coerce_number(counterfactual.get("avg_return_delta_pct")),
        "rerank_net_return_delta_points": round(rerank_delta, 4),
        "net_value_pct_points": net_value,
        "total_tokens": total_tokens,
        "billable_total_tokens": coerce_number(token_cost.get("billable_total_tokens")),
        "value_per_1k_tokens": value_per_1k,
    }


def _deepseek_budget_recommendation(
    status: str,
    real_sample_count: int,
    min_real_samples: int,
    token_value: Dict[str, object],
) -> Dict[str, object]:
    value = token_value.get("value_per_1k_tokens") if isinstance(token_value, dict) else None
    if status != "ok" or int(real_sample_count or 0) < int(min_real_samples or 0):
        return {
            "action": "observe",
            "worth_expanding_budget": False,
            "reason": "样本不足，只观察，不建议扩大 DeepSeek 调用预算。",
        }
    if value is None:
        return {
            "action": "observe",
            "worth_expanding_budget": False,
            "reason": "缺少 token usage，暂不判断是否扩大预算。",
        }
    if coerce_number(value) <= 0:
        return {
            "action": "shrink",
            "worth_expanding_budget": False,
            "reason": "value_per_1k_tokens <= 0，建议收缩 DeepSeek 调用范围。",
        }
    return {
        "action": "expand",
        "worth_expanding_budget": True,
        "reason": "DeepSeek OOS 净收益/token 为正，可评估扩大预算。",
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
        atomic_write_json(path, existing, ensure_ascii=False, separators=(",", ":"))
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
                "avg_next_close_return": _avg(row["_next_day_return"] for row in items),
                "win_rate_next_close": _rate(row["_next_day_return"] > 0 for row in items),
                "hit_3pct_rate": _rate(row["_hit_3pct"] for row in items),
                "hit_5pct_rate": _rate(row["_hit_5pct"] for row in items),
                "avg_hold_3d_return": _avg(row["_hold_3d_return"] for row in items),
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

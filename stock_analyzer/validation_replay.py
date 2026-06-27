from typing import Dict, Iterable, List, Tuple

import pandas as pd

from .factors import compute_alphalite_for_stock
from .normalization import coerce_number, normalize_code, rename_known_columns


REPLAY_VERSION_SUFFIX = "replay_v1"


def backfill_strategy_validation_samples(
    provider,
    validation_store,
    strategy_name: str,
    codes: Iterable[str],
    code_names: Dict[str, str] = None,
    days: int = 220,
    replay_days: int = 20,
    top_n: int = 30,
    holding_days: int = 3,
    min_lookback: int = 30,
) -> Dict[str, object]:
    """用历史K线回放生成策略验证样本。

    该回放只使用量价因子，目的是快速补足验证样本；它不能替代每日真实保存的
    前瞻预测记录。
    """
    strategy_name = strategy_name or "tomorrow_picks"
    version = "{}_{}".format(strategy_name, REPLAY_VERSION_SUFFIX)
    names = {normalize_code(key): value for key, value in (code_names or {}).items()}
    unique_codes = _unique_codes(codes)
    histories = _load_histories(provider, unique_codes, days)
    if not histories:
        return {
            "ok": False,
            "error": "没有可用于历史回放的K线数据",
            "strategy": strategy_name,
            "version": version,
            "requested_codes": len(unique_codes),
            "usable_codes": 0,
            "saved": 0,
            "replaced": 0,
            "date_count": 0,
            "outcome": {"updated": 0, "skipped": 0},
        }

    existing_dates = _existing_signal_keys(validation_store, strategy_name)
    eligible_dates = _eligible_signal_dates(histories, min_lookback, holding_days)
    eligible_dates = [value for value in eligible_dates if value not in existing_dates]
    selected_dates = eligible_dates[-max(1, int(replay_days)) :]
    if not selected_dates:
        return {
            "ok": False,
            "error": "没有新的可回放历史交易日；可增加历史天数、更换股票池或扩大回放窗口",
            "strategy": strategy_name,
            "version": version,
            "requested_codes": len(unique_codes),
            "usable_codes": len(histories),
            "saved": 0,
            "replaced": 0,
            "date_count": 0,
            "existing_date_count": len(existing_dates),
            "outcome": {"updated": 0, "skipped": 0},
        }

    saved = 0
    replaced = 0
    skipped_dates = 0
    saved_dates = []
    for trade_date_key in selected_dates:
        rows = _rank_replay_rows(
            strategy_name,
            histories,
            trade_date_key,
            names,
            top_n=top_n,
            holding_days=holding_days,
            min_lookback=min_lookback,
        )
        if not rows:
            skipped_dates += 1
            continue
        signal_time = "{}T14:30:00".format(_display_date(trade_date_key))
        result = validation_store.save_signals(strategy_name, version, signal_time, rows)
        saved += int(result.get("saved") or 0)
        replaced += int(result.get("replaced") or 0)
        saved_dates.append(result.get("signal_date") or _display_date(trade_date_key))

    outcome = validation_store.update_outcomes(provider, strategy_name=strategy_name)
    return {
        "ok": saved > 0,
        "strategy": strategy_name,
        "version": version,
        "requested_codes": len(unique_codes),
        "usable_codes": len(histories),
        "saved": saved,
        "replaced": replaced,
        "date_count": len(saved_dates),
        "skipped_dates": skipped_dates,
        "dates": saved_dates,
        "existing_date_count": len(existing_dates),
        "outcome": outcome,
        "note": "历史回放样本仅使用量价因子，用于快速补样本，不等同于真实前瞻预测。",
    }


def _unique_codes(codes: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for value in codes:
        code = normalize_code(value)
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _load_histories(provider, codes: List[str], days: int) -> Dict[str, pd.DataFrame]:
    histories: Dict[str, pd.DataFrame] = {}
    for code in codes:
        try:
            history = provider.get_history(code, days=days)
        except Exception:
            continue
        prepared = _prepare_history(code, history)
        if not prepared.empty:
            histories[code] = prepared
    return histories


def _prepare_history(code: str, history: pd.DataFrame) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    df = rename_known_columns(history.copy())
    if "trade_date" not in df.columns:
        return pd.DataFrame()
    if "code" not in df.columns:
        df["code"] = code
    if "price" not in df.columns and "close" in df.columns:
        df["price"] = df["close"]
    if "price" not in df.columns:
        return pd.DataFrame()
    for column in ("open", "high", "low", "price", "turnover", "volume"):
        if column not in df.columns:
            df[column] = df["price"] if column in ("open", "high", "low") else 0.0
        df[column] = df[column].map(coerce_number)
    df["code"] = df["code"].map(normalize_code)
    df["_trade_date_key"] = df["trade_date"].map(_date_key)
    df = df[df["_trade_date_key"] != ""]
    df = df.sort_values("_trade_date_key").reset_index(drop=True)
    return df


def _eligible_signal_dates(
    histories: Dict[str, pd.DataFrame],
    min_lookback: int,
    holding_days: int,
) -> List[str]:
    dates = set()
    lookback_index = max(1, int(min_lookback)) - 1
    future_days = max(1, int(holding_days))
    for history in histories.values():
        max_signal_index = len(history) - future_days - 1
        if max_signal_index < lookback_index:
            continue
        for index in range(lookback_index, max_signal_index + 1):
            dates.add(str(history["_trade_date_key"].iloc[index]))
    return sorted(dates)


def _rank_replay_rows(
    strategy_name: str,
    histories: Dict[str, pd.DataFrame],
    trade_date_key: str,
    names: Dict[str, str],
    top_n: int,
    holding_days: int,
    min_lookback: int,
) -> List[Dict[str, object]]:
    rows = []
    for code, history in histories.items():
        past = history[history["_trade_date_key"] <= trade_date_key].reset_index(drop=True)
        future = history[history["_trade_date_key"] > trade_date_key]
        if len(past) < min_lookback or len(future) < holding_days:
            continue
        factor = compute_alphalite_for_stock(code, past.tail(max(min_lookback, 30)))
        if not factor:
            continue
        latest = past.iloc[-1]
        price = coerce_number(latest.get("price"))
        if price <= 0:
            continue
        raw_signal, score = _strategy_replay_score(strategy_name, factor)
        rows.append(
            {
                "rank": 0,
                "code": code,
                "name": names.get(code) or code,
                "market_label": _market_label(code),
                "theme": "历史量价回放",
                "price": round(price, 4),
                "pct_chg": _one_day_return(past),
                "turnover": coerce_number(latest.get("turnover")),
                "volume_ratio": coerce_number(factor.get("vol_amount_5d")),
                "turnover_rate": 0.0,
                "sixty_day_pct": _period_return(past["price"], 60),
                "ytd_pct": _period_return(past["price"], min(len(past) - 1, 120)),
                "score": score,
                "replay_signal": round(raw_signal, 4),
                "strategy_version": "{}_{}".format(strategy_name, REPLAY_VERSION_SUFFIX),
                "replay": True,
                "reasons": _replay_reasons(strategy_name, factor),
                "alphalite_factor": factor,
            }
        )
    rows.sort(key=lambda item: (item["score"], item["replay_signal"]), reverse=True)
    selected = rows[: max(1, int(top_n))]
    for index, row in enumerate(selected, start=1):
        row["rank"] = index
    return selected


def _strategy_replay_score(strategy_name: str, factor: Dict[str, float]) -> Tuple[float, float]:
    ret_3d = coerce_number(factor.get("ret_3d"))
    ret_5d = coerce_number(factor.get("ret_5d"))
    ret_10d = coerce_number(factor.get("ret_10d"))
    ret_20d = coerce_number(factor.get("ret_20d"))
    ma5_gap = coerce_number(factor.get("ma5_gap"))
    ma20_gap = coerce_number(factor.get("ma20_gap"))
    volume = coerce_number(factor.get("vol_amount_5d"))
    breakout = coerce_number(factor.get("breakout_20d"))
    volatility = coerce_number(factor.get("volatility_20d"))
    ma_bull = coerce_number(factor.get("ma_bull_aligned"))
    vol_ma5 = coerce_number(factor.get("vol_ma5_ratio"))
    if strategy_name == "tomorrow_picks":
        raw = ret_3d * 0.22 + ret_5d * 0.26 + ma5_gap * 0.12 + volume * 1.8 + breakout * 4.5 - volatility * 0.32
    elif strategy_name == "swing_picks":
        raw = ret_5d * 0.18 + ret_10d * 0.24 + ret_20d * 0.12 + ma20_gap * 0.12 + volume * 1.4 + breakout * 3.5 - volatility * 0.28
    elif strategy_name == "position_picks":
        raw = ret_10d * 0.10 + ret_20d * 0.20 + ma20_gap * 0.18 + volume * 1.0 + breakout * 2.8 - volatility * 0.38
    elif strategy_name == "reversal_picks":
        # 反转：近期跌得多(-ret_20d) + 低波动 占优。
        raw = -ret_20d * 0.26 - ret_5d * 0.10 - volatility * 0.55
    elif strategy_name == "smallcap_value_picks":
        # 历史回放看不到市值/估值，用低波动+温和反转作代理（标注于 reasons）。
        raw = -ret_20d * 0.14 - volatility * 0.50 + ret_10d * 0.05
    elif strategy_name == "breakout_picks":
        # 突破：多头排列 + 创新高 + 量能突破。
        raw = ma_bull * 6.0 + breakout * 5.0 + max(0.0, vol_ma5 - 1.0) * 4.0 + ret_10d * 0.16 + ma20_gap * 0.14 - volatility * 0.20
    else:
        raw = ret_5d * 0.16 + ret_10d * 0.20 + ret_20d * 0.16 + ma20_gap * 0.10 + volume * 1.3 + breakout * 3.2 - volatility * 0.30
    return raw, round(max(0.0, min(100.0, 50.0 + raw)), 4)


def _replay_reasons(strategy_name: str, factor: Dict[str, float]) -> List[str]:
    reasons = ["历史回放样本：仅量价因子"]
    if coerce_number(factor.get("breakout_20d")) > 0:
        reasons.append("接近20日突破")
    if coerce_number(factor.get("ret_5d")) > 0:
        reasons.append("5日动量为正")
    if coerce_number(factor.get("ret_20d")) > 0:
        reasons.append("20日趋势为正")
    if coerce_number(factor.get("vol_amount_5d")) >= 1.2:
        reasons.append("近5日成交额放大")
    if coerce_number(factor.get("volatility_20d")) >= 4:
        reasons.append("波动偏高，需降仓位")
    if strategy_name in ("tech_potential", "chokepoint_picks"):
        reasons.append("主题/新闻因子无法历史还原，按量价代理")
    if strategy_name == "smallcap_value_picks":
        reasons.append("市值/估值无法历史还原，按低波动+反转代理")
    if strategy_name == "reversal_picks" and coerce_number(factor.get("ret_20d")) < 0:
        reasons.append("近20日超跌，反转候选")
    return reasons[:5]


def _existing_signal_keys(validation_store, strategy_name: str) -> set:
    try:
        dates = validation_store.list_signal_dates(strategy_name)
    except Exception:
        return set()
    keys = [_date_key(row.get("signal_date")) for row in dates]
    return {value for value in keys if value}


def _one_day_return(history: pd.DataFrame) -> float:
    if len(history) < 2:
        return 0.0
    prev = coerce_number(history["price"].iloc[-2])
    latest = coerce_number(history["price"].iloc[-1])
    if prev <= 0:
        return 0.0
    return round((latest / prev - 1) * 100, 4)


def _period_return(close: pd.Series, days: int) -> float:
    if days <= 0 or len(close) <= days:
        return 0.0
    base = coerce_number(close.iloc[-days - 1])
    latest = coerce_number(close.iloc[-1])
    if base <= 0:
        return 0.0
    return round((latest / base - 1) * 100, 4)


def _market_label(code: str) -> str:
    if code.startswith("300"):
        return "创业板"
    if code.startswith("688"):
        return "科创板"
    return "主板"


def _date_key(value) -> str:
    text = str(value or "").strip()
    return "".join(ch for ch in text if ch.isdigit())[:8]


def _display_date(date_key: str) -> str:
    value = _date_key(date_key)
    if len(value) != 8:
        return value
    return "{}-{}-{}".format(value[:4], value[4:6], value[6:8])

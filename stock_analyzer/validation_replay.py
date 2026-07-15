from typing import Dict, Iterable, List

import pandas as pd

from . import config
from .execution_policy import build_execution_policy
from .factors import compute_alphalite_for_stock
from .normalization import coerce_number, normalize_code, rename_known_columns
from .scoring_core.candidate_filters import prepare_candidates
from .scoring_core.market_regime import build_market_regime
from .strategies import score_swing_2_5d_picks, score_tomorrow_picks


REPLAY_VERSION_SUFFIX = str(getattr(config, "VALIDATION_REPLAY_VERSION_SUFFIX", "replay_v2_production"))
SUPPORTED_REPLAY_STRATEGIES = {"tomorrow_picks", "swing_picks"}
REPLAY_SAMPLE_TYPE = "daily_proxy_replay"
REPLAY_SAMPLE_SOURCE = "daily_bar_proxy"


def backfill_strategy_validation_samples(
    provider,
    validation_store,
    strategy_name: str,
    codes: Iterable[str],
    code_names: Dict[str, str] = None,
    days: int = 220,
    replay_days: int = 20,
    top_n: int = None,
    holding_days: int = 3,
    min_lookback: int = 30,
) -> Dict[str, object]:
    """用历史K线回放生成策略验证样本。

    该回放只使用量价因子，目的是快速补足验证样本；它不能替代每日真实保存的
    前瞻预测记录。
    """
    strategy_name = strategy_name or "tomorrow_picks"
    unique_codes = _unique_codes(codes)
    if strategy_name not in SUPPORTED_REPLAY_STRATEGIES:
        return {
            "ok": False,
            "error": "unsupported_strategy",
            "strategy": strategy_name,
            "version": REPLAY_VERSION_SUFFIX,
            "requested_codes": len(unique_codes),
            "usable_codes": 0,
            "saved": 0,
            "replaced": 0,
            "date_count": 0,
            "outcome": {"updated": 0, "skipped": 0},
        }
    if top_n is None:
        top_n = config.TOMORROW_TOP_N if strategy_name == "tomorrow_picks" else getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18)
    version = "{}_{}".format(strategy_name, REPLAY_VERSION_SUFFIX)
    names = {normalize_code(key): value for key, value in (code_names or {}).items()}
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

    existing_dates = _existing_signal_keys(validation_store, strategy_name, version)
    required_future_days = max(int(holding_days or 0), _required_future_days(strategy_name))
    eligible_dates = _eligible_signal_dates(histories, min_lookback, required_future_days)
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
    replay_batch_metadata = {
        "sample_type": REPLAY_SAMPLE_TYPE,
        "sample_source": REPLAY_SAMPLE_SOURCE,
    }
    for trade_date_key in selected_dates:
        rows = _rank_replay_rows(
            strategy_name,
            histories,
            trade_date_key,
            names,
            top_n=top_n,
            holding_days=required_future_days,
            min_lookback=min_lookback,
        )
        if not rows:
            skipped_dates += 1
            continue
        signal_time = "{}T{}".format(_display_date(trade_date_key), _replay_signal_time_tail())
        result = validation_store.save_signals(
            strategy_name,
            version,
            signal_time,
            rows,
            execution_policy=build_execution_policy(strategy_name),
            batch_metadata=replay_batch_metadata,
        )
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
        "note": "历史回放复用当前生产评分与分层，但仅有历史量价字段，不能替代真实前瞻预测。",
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
    candidate_rows = []
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
        previous_close = coerce_number(past.iloc[-2].get("price")) if len(past) > 1 else price
        high = coerce_number(latest.get("high")) or price
        low = coerce_number(latest.get("low")) or price
        amplitude = ((high - low) / previous_close * 100.0) if previous_close > 0 else 0.0
        candidate_rows.append(
            {
                "code": code,
                "name": names.get(code) or code,
                "price": round(price, 4),
                "open": coerce_number(latest.get("open")) or price,
                "high": high,
                "low": low,
                "prev_close": previous_close,
                "pct_chg": _one_day_return(past),
                "turnover": coerce_number(latest.get("turnover")),
                "volume": coerce_number(latest.get("volume")),
                "volume_ratio": (
                    coerce_number(factor.get("vol_ma5_ratio"))
                    or coerce_number(factor.get("vol_amount_5d"))
                    or 1.0
                ),
                "turnover_rate": 0.0,
                "amplitude": round(amplitude, 4),
                "sixty_day_pct": _period_return(past["price"], 60),
                "ytd_pct": _period_return(past["price"], min(len(past) - 1, 120)),
                "trade_date": "{}T15:00:00".format(_display_date(trade_date_key)),
                # Historical bars do not carry a trustworthy industry label. A
                # code-specific unknown bucket avoids inventing one shared theme
                # that would incorrectly collapse Top-K replay samples.
                "industry": "历史行业未知-{}".format(code),
                **factor,
            }
        )
    if not candidate_rows:
        return []
    candidates = prepare_candidates(pd.DataFrame(candidate_rows))
    if candidates.empty:
        return []
    market_regime = build_market_regime(candidates, breadth_source=candidates)
    if strategy_name == "tomorrow_picks":
        selected, meta = score_tomorrow_picks(
            candidates,
            top_n=max(1, int(top_n)),
            market_regime=market_regime,
            display_cap=0,
            capture_candidate_pool=True,
        )
        research_pool = meta.get("_candidate_pool_rows") if isinstance(meta, dict) else None
        if isinstance(research_pool, list) and research_pool:
            selected = research_pool[: max(1, int(top_n))]
    else:
        selected, _ = score_swing_2_5d_picks(
            candidates,
            top_n=max(1, int(top_n)),
            market_regime=market_regime,
        )
    result = []
    for row in selected:
        item = dict(row)
        item["strategy_version"] = "{}_{}".format(strategy_name, REPLAY_VERSION_SUFFIX)
        item["replay"] = True
        item["replay_source"] = "production_scorer"
        item["sample_type"] = REPLAY_SAMPLE_TYPE
        item["sample_source"] = REPLAY_SAMPLE_SOURCE
        # The full ranked Top-K is the replay research portfolio. Its explicit
        # sample type keeps it out of production promotion.
        item["tier"] = "primary_watch"
        item["execution_allowed"] = True
        item["reasons"] = _unique_reasons(
            list(item.get("reasons") or []) + _replay_reasons(strategy_name, item)
        )
        result.append(item)
    return result


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
    if strategy_name == "today_term":
        reasons.append("今日盘中策略使用量价反应打分")
    if strategy_name == "swing_picks":
        reasons.append("2-5天策略强调趋势和量能延续")
    if strategy_name == "tomorrow_picks":
        reasons.append("次日候选强调收盘回看与次日延续")
    return reasons[:5]


def _existing_signal_keys(validation_store, strategy_name: str, replay_version: str = "") -> set:
    try:
        if hasattr(validation_store, "existing_validation_dates"):
            dates = validation_store.existing_validation_dates(strategy_name, replay_version)
            return {_date_key(value) for value in dates if _date_key(value)}
        dates = validation_store.list_signal_dates(strategy_name)
    except Exception:
        return set()
    keys = [_date_key(row.get("signal_date")) for row in dates]
    return {value for value in keys if value}


def _required_future_days(strategy_name: str) -> int:
    return 5 if strategy_name in {"tomorrow_picks", "swing_picks"} else 1


def _unique_reasons(values: Iterable[str]) -> List[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result[:8]


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


def _replay_signal_time_tail() -> str:
    raw = str(getattr(config, "VALIDATION_AUTO_SNAPSHOT_TIME", "14:30")).strip() or "14:30"
    if ":" not in raw:
        return "15:00:00"
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return "{:02d}:{:02d}:00".format(hour, minute)
    except Exception:
        return "15:00:00"

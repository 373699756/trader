from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number
from . import expected_return
from . import market_regime as market_regime_core
from . import scoring_math


__all__ = [
    "_market_regime_with_history",
    "_parse_datetime_value",
    "_time_parts",
    "_tomorrow_analysis_window",
    "_tomorrow_display_gate",
    "_tomorrow_hard_reject",
    "_tomorrow_intraday_relaxed_mode",
    "_tomorrow_policy",
    "_tomorrow_primary_eligibility",
    "_tomorrow_primary_watch_limit",
    "_tomorrow_quote_time",
]


def _tomorrow_policy() -> Dict[str, object]:
    return {
        "main_max_gain": config.MAX_BUYABLE_GAIN_MAIN,
        "growth_max_gain": config.MAX_BUYABLE_GAIN_GROWTH,
        "min_turnover": config.MIN_TURNOVER,
        "avoid_limit_up": True,
        "entry_style": "收盘后筛选，次日承接优先",
        "intraday_relax_start": getattr(config, "TOMORROW_INTRADAY_RELAX_START", "09:30"),
        "intraday_relax_until": getattr(config, "TOMORROW_INTRADAY_RELAX_UNTIL", "14:30"),
        "risk_controls": ("高涨幅", "高量比", "高换手", "高振幅", "收盘回落", "高开透支", "超涨damp硬门控"),
    }


def _tomorrow_hard_reject(row: pd.Series, intraday_relaxed: bool = False) -> bool:
    pct = coerce_number(row.get("pct_chg"))
    market = row.get("market")
    upper = config.MAX_BUYABLE_GAIN_GROWTH if market in ("chinext", "star") else config.MAX_BUYABLE_GAIN_MAIN
    volume_ratio = coerce_number(row.get("volume_ratio"))
    amplitude = coerce_number(row.get("amplitude"))
    turnover_rate = coerce_number(row.get("turnover_rate"))
    turnover = coerce_number(row.get("turnover"))
    speed = scoring_math._row_speed(row)
    close_location = scoring_math._close_location(
        coerce_number(row.get("price")),
        coerce_number(row.get("high")),
        coerce_number(row.get("low")),
    )
    pct_floor = 0.3 if intraday_relaxed else 0.6
    pct_ceiling = upper * 0.88
    volume_ratio_floor = 0.75 if intraday_relaxed else 0.9
    volume_ratio_ceiling = 5.0
    turnover_rate_floor = 0.8 if intraday_relaxed else 1.5
    if pct <= pct_floor or pct >= pct_ceiling:
        return True
    if volume_ratio < volume_ratio_floor or volume_ratio >= volume_ratio_ceiling:
        return True
    if turnover_rate > 0 and turnover_rate < turnover_rate_floor:
        return True
    if turnover_rate >= 20.0:
        return True
    if amplitude >= 12.0:
        return True
    if not intraday_relaxed and close_location < 0.25:
        return True
    if scoring_math._near_limit_up_risk(row) and turnover_rate < 8.0:
        return True
    if speed > 4.2 or speed < -2.2:
        return True
    if config.MIN_TURNOVER > 0 and turnover < config.MIN_TURNOVER:
        return True
    if coerce_number(row.get("alphalite_factor_ready")) > 0:
        ret_20d = coerce_number(row.get("ret_20d"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        if ret_20d < -18 or ma20_gap < -10 or volatility_20d > 10:
            return True
    return False


def _tomorrow_display_gate(
    top_n: int,
    market_regime: Dict[str, object] = None,
    intraday_relaxed: bool = False,
) -> Tuple[int, float, str]:
    top_n = max(0, int(top_n or 0))
    if not market_regime:
        if intraday_relaxed:
            return top_n, 56.0, "14:30 前早盘模式，默认展示线放宽到 56 分。"
        return top_n, 60.0, "未提供市场环境，只展示达到默认分数门槛的候选。"
    level = market_regime.get("level") or "unknown"
    history_breadth = coerce_number(market_regime.get("history_breadth20_pct"))
    history_coverage = coerce_number(market_regime.get("history_factor_coverage_pct"))
    if intraday_relaxed:
        if history_coverage >= 25:
            if history_breadth > 55:
                return top_n, 56.0, "14:30 前早盘模式，历史宽度强，展示线放宽到 56 分。"
            if history_breadth > 45:
                return top_n, 64.0, "14:30 前早盘模式，历史宽度中性，展示线放宽到 64 分。"
            return top_n, 72.0, "14:30 前早盘模式，历史宽度偏弱，仍需较高分数。"
        if level == "risk_on":
            return top_n, 56.0, "14:30 前早盘模式，偏进攻盘面展示线放宽到 56 分。"
        if level == "balanced":
            return top_n, 60.0, "14:30 前早盘模式，均衡盘面展示线放宽到 60 分。"
        if level == "risk_off":
            return top_n, 66.0, "14:30 前早盘模式，偏防守盘面展示线放宽到 66 分。"
        return top_n, 62.0, "14:30 前早盘模式，盘面不明确时展示线放宽到 62 分。"
    if history_coverage >= 25:
        if history_breadth > 55:
            return top_n, 60.0, "历史20日均线宽度强于55%，只展示达到分数门槛的候选。"
        if history_breadth > 45:
            return top_n, 68.0, "历史20日均线宽度处于45%-55%，只展示较高分候选。"
        return top_n, 78.0, "历史20日均线宽度低于45%，弱市只展示高分候选；不足则不推荐。"
    if level == "risk_on":
        return top_n, 60.0, "偏进攻盘面，只展示达到分数门槛的候选。"
    if level == "balanced":
        return top_n, 66.0, "均衡震荡盘面，只展示达到分数门槛的候选。"
    if level == "risk_off":
        return top_n, 72.0, "偏防守盘面，只展示达到分数门槛的候选；不足则不推荐。"
    return top_n, 70.0, "盘面状态不明确，只展示达到分数门槛的候选。"


def _market_regime_with_history(market_regime: Dict[str, object], df: pd.DataFrame) -> Dict[str, object]:
    return market_regime_core._market_regime_with_history(market_regime, df)


def _tomorrow_primary_watch_limit(strict_count: int, market_regime: Dict[str, object] = None) -> int:
    if strict_count <= 0:
        return 0
    max_primary = max(0, int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 5)))
    if max_primary <= 0:
        return 0
    regime = market_regime or {}
    history_breadth = coerce_number(regime.get("history_breadth20_pct"))
    history_coverage = coerce_number(regime.get("history_factor_coverage_pct"))
    if history_coverage >= 25:
        if history_breadth <= 45:
            return 0
        if history_breadth <= 55:
            return min(strict_count, max_primary, 3)
        return min(strict_count, max_primary)
    level = regime.get("level") or "unknown"
    if level == "risk_off":
        return 0
    if level == "balanced":
        return min(strict_count, max_primary, 3)
    return min(strict_count, max_primary)


def _tomorrow_primary_eligibility(row: Dict[str, object], gate_min_score: float) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    score = expected_return._ranking_gate_score(row)
    primary_min_score = max(
        coerce_number(gate_min_score),
        coerce_number(getattr(config, "TOMORROW_PRIMARY_MIN_SCORE", 68.0), 68.0),
    )
    if score < primary_min_score:
        reasons.append("未达重点排序线")
    risk_penalty = coerce_number(row.get("risk_penalty"))
    max_risk_penalty = coerce_number(getattr(config, "TOMORROW_PRIMARY_MAX_RISK_PENALTY", 12.0), 12.0)
    if risk_penalty > max_risk_penalty:
        reasons.append("风险扣分超主推阈值")
    overheat_damp = coerce_number(row.get("overheat_damp"), 1.0)
    min_overheat_damp = coerce_number(getattr(config, "TOMORROW_PRIMARY_MIN_OVERHEAT_DAMP", 0.72), 0.72)
    if overheat_damp < min_overheat_damp:
        reasons.append("过热抑制过强仅备选")
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
    ytd_pct = coerce_number(row.get("ytd_pct"))
    max_sixty = coerce_number(getattr(config, "TOMORROW_PRIMARY_MAX_SIXTY_DAY_PCT", 90.0), 90.0)
    max_ytd = coerce_number(getattr(config, "TOMORROW_PRIMARY_MAX_YTD_PCT", 130.0), 130.0)
    historical_edge = coerce_number(row.get("historical_edge_score"))
    tail_setup = coerce_number(row.get("tail_setup_score"))
    strong_edge = historical_edge >= 78 and tail_setup >= 72 and risk_penalty <= max_risk_penalty * 0.75
    if sixty_day_pct > max_sixty and not strong_edge:
        reasons.append("60日涨幅过高仅备选")
    if ytd_pct > max_ytd and not strong_edge:
        reasons.append("年内涨幅过高仅备选")
    return not reasons, reasons


def _tomorrow_analysis_window() -> str:
    raw = str(getattr(config, "VALIDATION_AUTO_SNAPSHOT_TIME", "14:50")).strip() or "14:50"
    if ":" not in raw:
        return "15:00"
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return "{:02d}:{:02d}".format(hour, minute)
    except Exception:
        return "15:00"


def _tomorrow_intraday_relaxed_mode(now: datetime = None, quote_time: datetime = None) -> bool:
    if now is None and quote_time is None:
        return False
    current = now or quote_time or datetime.now()
    if current.weekday() >= 5:
        return False
    if now is not None and quote_time is not None and quote_time.date() != current.date():
        return False
    start = _time_parts(getattr(config, "TOMORROW_INTRADAY_RELAX_START", "09:30"), (9, 30))
    cutoff = _time_parts(getattr(config, "TOMORROW_INTRADAY_RELAX_UNTIL", "14:30"), (14, 30))
    current_time = (current.hour, current.minute)
    return start <= current_time < cutoff


def _tomorrow_quote_time(df: pd.DataFrame) -> datetime:
    if df is None:
        return None
    if "trade_date" in df.columns:
        for value in df["trade_date"].dropna().tolist():
            parsed = _parse_datetime_value(value)
            if parsed is not None:
                return parsed
    for key in ("quote_timestamp", "snapshot_mtime"):
        parsed = _parse_datetime_value((df.attrs or {}).get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime_value(value) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 8 and raw.isdigit():
            return datetime.strptime(raw, "%Y%m%d")
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _time_parts(value: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    raw = str(value or "").strip()
    if ":" not in raw:
        return fallback
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return hour, minute
    except Exception:
        return fallback

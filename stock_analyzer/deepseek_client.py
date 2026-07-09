from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
import re
import time
from typing import Dict, List, Tuple

import requests

from .deepseek.event_score import (
    coerce_already_priced_in as _coerce_already_priced_in,
    coerce_catalyst_strength as _coerce_catalyst_strength,
    coerce_sentiment as _coerce_sentiment,
    coerce_time_sensitivity as _coerce_time_sensitivity,
    deepseek_event_adjustment as _deepseek_event_adjustment,
)
from .normalization import coerce_number, normalize_code
from .strategies.types import storage_strategy_name
from . import config


_DOTENV_LOADED = False
_DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_CACHE_SCHEMA_VERSION = 1
_SUPPORTED_DEEPSEEK_BASE_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)
_SUPPORTED_STRATEGIES = set(config.SNAPSHOT_STRATEGIES)
_STRATEGY_CONTEXT = {
    "short_term": {
        "horizon": "盘中到次日",
        "focus": "今日短线动量与量价能否延续到次日，重点识别追高、兑现和冲高回落风险",
    },
    "tomorrow_picks": {
        "horizon": "次日",
        "focus": "次日开盘后到收盘能否承接，重点识别尾盘假拉升、近涨停不可买和次日兑现风险",
    },
    "swing_picks": {
        "horizon": "2-5日",
        "focus": "2-5天短周期趋势延续和量价配合，重点识别假突破、高位横盘和短周期过热",
    },
}

def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_project_path(path: str) -> str:
    path = os.path.expanduser(path.strip())
    if os.path.isabs(path):
        return path
    return os.path.join(_project_root(), path)


def _load_dotenv_if_needed() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    env_path = os.path.expanduser(os.getenv("DEEPSEEK_ENV_FILE", ".env").strip())
    if not env_path:
        env_path = ".env"
    candidates = [env_path] if os.path.isabs(env_path) else [
        os.path.abspath(env_path),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env_path),
    ]
    env_path = ""
    for path in candidates:
        if os.path.exists(path):
            env_path = path
            break
    if not env_path:
        return
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        return


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _coerce_strategies(env_name: str) -> List[str]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return []
    return [storage_strategy_name(item) for item in raw.replace("，", ",").split(",") if item.strip()]


def _coerce_env_config() -> Dict[str, object]:
    _load_dotenv_if_needed()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    return {
        "enabled": _env_bool("DEEPSEEK_ENABLED", bool(api_key)),
        "base_url": _coerce_base_url(os.getenv("DEEPSEEK_BASE_URL", _DEFAULT_DEEPSEEK_BASE_URL)),
        "api_key": api_key,
        "model": _coerce_model(os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"), "deepseek-v4-flash"),
        "pro_model": _coerce_model(os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro"), "deepseek-v4-pro"),
        "review_limit": max(5, _env_int("DEEPSEEK_REVIEW_LIMIT", 20)),
        "max_tokens": max(80, _env_int("DEEPSEEK_MAX_TOKENS", 800)),
        "timeout_seconds": max(2.0, _env_float("DEEPSEEK_TIMEOUT_SECONDS", 12.0)),
        "retry_count": max(0, _env_int("DEEPSEEK_RETRY_COUNT", 2)),
        "validation_timeout_seconds": max(3.0, _env_float("DEEPSEEK_VALIDATION_TIMEOUT_SECONDS", 10.0)),
        "validation_retry_count": max(0, _env_int("DEEPSEEK_VALIDATION_RETRY_COUNT", 0)),
        "retry_base_delay": max(0.2, _env_float("DEEPSEEK_RETRY_BASE_DELAY", 0.8)),
        "blend_alpha": max(0.0, min(1.0, _env_float("DEEPSEEK_BLEND_ALPHA", 0.30))),
        "strategies": _coerce_strategies("DEEPSEEK_STRATEGIES"),
        "pro_strategies": _coerce_strategies("DEEPSEEK_PRO_STRATEGIES"),
        "cache_enabled": _env_bool("DEEPSEEK_CACHE_ENABLED", True),
        "cache_path": _resolve_project_path(os.getenv("DEEPSEEK_CACHE_PATH", ".runtime/deepseek_cache.json")),
        "cache_ttl_seconds": max(0, _env_int("DEEPSEEK_CACHE_TTL_SECONDS", 86400)),
    }


def _safe_parse_json(text: str):
    text = str(text or "").strip().lstrip("\ufeff")
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    candidates = [text]
    decoder = json.JSONDecoder()
    for start in [index for index, char in enumerate(text) if char in "[{"]:
        try:
            parsed, _ = decoder.raw_decode(text[start:])
            candidates.append(text[start:])
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        for value in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                return json.loads(value)
            except Exception:
                pass
    return None


def _extract_results(parsed) -> List[Dict[str, object]]:
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if not isinstance(parsed, dict):
        return []
    records = parsed.get("results")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    return []


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "对"}


def _coerce_action(value: object, score: float, penalty: float, veto: bool) -> str:
    action = str(value or "").strip().lower()
    if action in {"priority", "watch", "avoid"}:
        return action
    if veto or penalty >= 18 or score < 45:
        return "avoid"
    if penalty >= 8 or score < 70:
        return "watch"
    return "priority"


def _coerce_model(name: str, default: str) -> str:
    text = str(name or "").strip()
    if not text:
        return default
    text = text.lower()
    if text in _SUPPORTED_DEEPSEEK_BASE_MODELS:
        return text
    return default


def _coerce_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return _DEFAULT_DEEPSEEK_BASE_URL.rstrip("/")
    lower = url.lower()
    if lower.endswith("/chat/completions"):
        return url[: -len("/chat/completions")].rstrip("/") or _DEFAULT_DEEPSEEK_BASE_URL.rstrip("/")
    if lower.endswith("/v1"):
        return url[: -len("/v1")].rstrip("/") or _DEFAULT_DEEPSEEK_BASE_URL.rstrip("/")
    return url


def _deepseek_chat_url(base_url: str) -> str:
    return f"{_coerce_base_url(base_url).rstrip('/')}/chat/completions"


def _strategy_context(strategy_name: str) -> Dict[str, str]:
    return _STRATEGY_CONTEXT.get(
        str(strategy_name),
        {
            "horizon": "策略主周期",
            "focus": "结合策略目标周期做风险复核，重点识别过热、流动性和趋势失效风险",
        },
    )


def _unique_strings(values: List[object]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


_NEXT_DAY_LOSS_FACTORS = (
    "今日涨幅过大或接近涨停但未形成稳定承接",
    "高位放量、量比过热、换手过热，可能是资金分歧或兑现",
    "振幅过大、尾盘回落、收盘位置偏弱，次日容易低开或冲高回落",
    "连续多日上涨、20日/60日/YTD涨幅透支、偏离20日线过远",
    "短线速度过快或尾盘急拉，容易形成次日兑现压力",
    "量能不足、成交额不足、历史同类信号偏弱或风险扣分偏高",
    "趋势破位、20日趋势弱、波动率过高、事件/黑名单/舆情风险",
)


_NEXT_DAY_PROFIT_FACTORS = (
    "今日涨幅适中，强但不过热，仍有次日溢价空间",
    "量能温和放大，量比和换手处于健康区间",
    "成交额充足，流动性足够支持次日继续承接",
    "5/10/20日动量转强，60日趋势向上但未明显透支",
    "收盘结构较强，尾盘没有明显回落，买入安全分较好",
    "历史量价结构占优，同类验证表现不差",
    "市场环境顺风，风险扣分低，多个本地因子同时确认",
)


def _request_payload(strategy_name: str, candidates: List[Dict[str, object]], market_filter: str) -> List[Dict[str, object]]:
    return [
        {
            "code": row.get("code", ""),
            "name": row.get("name", ""),
            "score": coerce_number(row.get("score"), 0.0),
            "base_score": coerce_number(row.get("base_score"), 0.0),
            "raw_score": coerce_number(row.get("raw_score"), 0.0),
            "pct_chg": coerce_number(row.get("pct_chg"), 0.0),
            "speed": coerce_number(row.get("speed"), 0.0),
            "five_min_pct": coerce_number(row.get("five_min_pct"), 0.0),
            "volume_ratio": coerce_number(row.get("volume_ratio"), 0.0),
            "turnover_rate": coerce_number(row.get("turnover_rate"), 0.0),
            "turnover": coerce_number(row.get("turnover"), 0.0),
            "amplitude": coerce_number(row.get("amplitude"), 0.0),
            "sixty_day_pct": coerce_number(row.get("sixty_day_pct"), 0.0),
            "ytd_pct": coerce_number(row.get("ytd_pct"), 0.0),
            "ret_5d": coerce_number(row.get("ret_5d"), 0.0),
            "ret_10d": coerce_number(row.get("ret_10d"), 0.0),
            "ret_20d": coerce_number(row.get("ret_20d"), 0.0),
            "ma20_gap": coerce_number(row.get("ma20_gap"), 0.0),
            "vol_amount_5d": coerce_number(row.get("vol_amount_5d"), 0.0),
            "breakout_20d": bool(row.get("breakout_20d")),
            "volatility_20d": coerce_number(row.get("volatility_20d"), 0.0),
            "liquidity_score": coerce_number(row.get("liquidity_score"), 0.0),
            "momentum_score": coerce_number(row.get("momentum_score"), 0.0),
            "trend_score": coerce_number(row.get("trend_score"), 0.0),
            "historical_edge_score": coerce_number(row.get("historical_edge_score"), 0.0),
            "execution_score": coerce_number(row.get("execution_score"), 0.0),
            "tail_setup_score": coerce_number(row.get("tail_setup_score"), 0.0),
            "risk_penalty": coerce_number(row.get("risk_penalty"), 0.0),
            "risk_penalty_parts": row.get("risk_penalty_parts", {}),
            "overheat_damp": coerce_number(row.get("overheat_damp"), 1.0),
            "failure_reasons": row.get("failure_reasons", []),
            "market": str(row.get("market", "")),
            "market_label": str(row.get("market_label", "")),
            "industry": str(row.get("industry", "")),
            "theme": str(row.get("theme", "")),
            "reasons": row.get("reasons", []),
        }
        for row in candidates
    ]


def _next_day_factor_review(row: Dict[str, object]) -> Dict[str, object]:
    pct = coerce_number(row.get("pct_chg"), 0.0)
    speed = coerce_number(row.get("speed"), 0.0)
    volume_ratio = coerce_number(row.get("volume_ratio"), 0.0)
    turnover_rate = coerce_number(row.get("turnover_rate"), 0.0)
    amplitude = coerce_number(row.get("amplitude"), 0.0)
    sixty_day_pct = coerce_number(row.get("sixty_day_pct"), 0.0)
    ytd_pct = coerce_number(row.get("ytd_pct"), 0.0)
    ret_20d = coerce_number(row.get("ret_20d"), 0.0)
    ma20_gap = coerce_number(row.get("ma20_gap"), 0.0)
    volatility_20d = coerce_number(row.get("volatility_20d"), 0.0)
    historical_edge = coerce_number(row.get("historical_edge_score"), 50.0)
    execution = coerce_number(row.get("execution_score"), 50.0)
    tail_setup = coerce_number(row.get("tail_setup_score"), 50.0)
    liquidity = coerce_number(row.get("liquidity_score"), 50.0)
    momentum = coerce_number(row.get("momentum_score"), 50.0)
    trend = coerce_number(row.get("trend_score"), 50.0)
    risk_penalty = coerce_number(row.get("risk_penalty"), 0.0)
    overheat_damp = coerce_number(row.get("overheat_damp"), 1.0)

    penalty = 0.0
    bonus = 0.0
    risk_flags: List[str] = []
    profit_flags: List[str] = []

    def risk(label: str, value: float) -> None:
        nonlocal penalty
        penalty += value
        if label not in risk_flags:
            risk_flags.append(label)

    def profit(label: str, value: float) -> None:
        nonlocal bonus
        bonus += value
        if label not in profit_flags:
            profit_flags.append(label)

    if pct >= 8.0:
        risk("当日涨幅过高", 8)
    elif pct >= 6.5:
        risk("当日涨幅偏高", 4)
    elif 1.0 <= pct <= 5.5:
        profit("涨幅强但未过热", 4)
    elif pct < 0:
        risk("当日走势偏弱", 6)

    if volume_ratio >= 5.5:
        risk("量比过热", 9)
    elif volume_ratio >= 4.0:
        risk("量比偏高", 5)
    elif 1.2 <= volume_ratio <= 3.5:
        profit("量能温和放大", 5)
    elif 0 < volume_ratio < 1.0:
        risk("量能不足", 4)

    if turnover_rate >= 18:
        risk("换手过热", 8)
    elif turnover_rate >= 14:
        risk("换手偏高", 4)
    elif 2 <= turnover_rate <= 12:
        profit("换手健康", 3)

    if amplitude >= 11:
        risk("振幅过大", 8)
    elif amplitude >= 9:
        risk("振幅偏大", 4)

    if speed > 3:
        risk("尾盘急拉追高", 6)
    elif speed < -1.2:
        risk("尾盘回落", 7)

    if sixty_day_pct > 70:
        risk("60日涨幅过大", 9)
    elif sixty_day_pct > 45:
        risk("60日涨幅偏大", 5)
    elif 3 <= sixty_day_pct <= 35:
        profit("60日趋势未透支", 4)

    if ytd_pct > 120:
        risk("年内涨幅过大", 9)
    elif ytd_pct > 80:
        risk("年内涨幅偏大", 5)

    if ret_20d > 45:
        risk("20日涨幅过快", 8)
    elif ret_20d > 25:
        risk("20日涨幅偏快", 4)
    elif 0 <= ret_20d <= 20:
        profit("20日动量可延续", 3)
    elif ret_20d < -8:
        risk("短期趋势偏弱", 5)

    if ma20_gap > 35:
        risk("偏离20日线过远", 8)
    elif ma20_gap > 22:
        risk("偏离20日线偏远", 4)
    elif 0 <= ma20_gap <= 12:
        profit("贴近20日趋势上方", 3)
    elif ma20_gap < -6:
        risk("跌破20日趋势", 6)

    if volatility_20d > 8:
        risk("20日波动过高", 6)
    elif 0 < volatility_20d <= 5.5:
        profit("波动可控", 2)

    if risk_penalty >= 18:
        risk("本地风险扣分高", 8)
    elif risk_penalty >= 10:
        risk("本地风险扣分偏高", 4)
    elif risk_penalty <= 5:
        profit("本地风险低", 3)

    if overheat_damp < 0.72:
        risk("过热抑制明显", 6)
    if historical_edge >= 58:
        profit("历史结构占优", 4)
    elif historical_edge < 45:
        risk("历史结构偏弱", 5)
    if execution >= 72:
        profit("买入安全较好", 4)
    elif execution < 55:
        risk("买入安全偏低", 6)
    if tail_setup >= 70:
        profit("收盘结构较强", 4)
    elif tail_setup < 52:
        risk("收盘结构偏弱", 5)
    if liquidity >= 65:
        profit("流动性较好", 3)
    if momentum >= 70 and trend >= 60:
        profit("动量趋势共振", 4)
    elif momentum < 45 or trend < 45:
        risk("动量趋势不足", 5)

    for text in row.get("failure_reasons") or []:
        if str(text).strip():
            risk(str(text).strip(), 3)

    return {
        "penalty": round(_clamp(penalty - min(bonus, 10.0) * 0.35, 0.0, 35.0), 2),
        "bonus": round(_clamp(bonus, 0.0, 20.0), 2),
        "risk_flags": risk_flags[:6],
        "profit_flags": profit_flags[:6],
        "veto": penalty >= 28 and bonus < 8,
    }


def _build_messages(strategy_name: str, candidates: List[Dict[str, object]], market_filter: str) -> List[Dict[str, object]]:
    context = _strategy_context(strategy_name)
    return [
        {
            "role": "system",
            "content": "你是A股研究助手。请只输出 JSON，不要解释、不要 Markdown。"
            "输出必须包含 results 数组，每个元素包含 code、llm_score(0-100)、horizon_up_score(0-100)、action、veto、penalty、reason、risk_flags。"
            "可选字段 event_type、sentiment(-2~2)、catalyst_strength(0-100)、time_sensitivity、already_priced_in、catalyst_score、"
            "theme_truth_score、event_risk_score（都是0-100，except sentiment/flag）。"
            "action 只能是 priority、watch、avoid；penalty 是0-30扣分；risk_flags 是字符串数组，最多3项。",
        },
        {
            "role": "user",
            "content": (
                "策略: {strategy}\n"
                "策略周期: {horizon}\n"
                "复核重点: {focus}\n"
                "市场: {market}，仅聚焦A股（主板/创业板/科创板）\n"
                "请重点做风险复核和反推荐，不要直接替代本地量化分数。\n"
                "先输出结构化事件字段，再输出复核判断：\n"
                "event_type（业绩/订单/政策/并购/涨价/监管/传闻/未知）、sentiment(-2~2)、"
                "catalyst_strength(0-100)、time_sensitivity(今天/明天/2-5天/长期)、already_priced_in(true/false)。\n"
                "horizon_up_score 表示策略主周期内上涨/跑赢倾向；如果看起来强但容易回落，请提高 penalty 或 action=avoid。\n"
                "短周期亏钱因素必须逐项考虑: {loss_factors}\n"
                "短周期赚钱因素必须逐项考虑: {profit_factors}\n"
                "主题类策略必须判断 theme_truth_score，事件/公告/财报风险较高时提高 event_risk_score 和 penalty。\n"
                "如果亏钱因素明显多于赚钱因素，必须 action=avoid 或提高 penalty；如果赚钱因素多但存在追高风险，action=watch。\n"
                "输出 JSON 示例: {{\"results\":[{{\"code\":\"600519\",\"llm_score\":87.4,\"horizon_up_score\":74,\"action\":\"watch\","
                "\"veto\":false,\"penalty\":8,\"reason\":\"...\",\"risk_flags\":[\"涨幅透支\"],\"event_type\":\"业绩\","
                "\"sentiment\":1,\"catalyst_strength\":78,\"time_sensitivity\":\"明天\",\"already_priced_in\":false,"
                "\"catalyst_score\":55,\"theme_truth_score\":50,\"event_risk_score\":35}}]}}\n"
                "候选池: {pool}".format(
                    strategy=strategy_name,
                    horizon=context["horizon"],
                    focus=context["focus"],
                    market=market_filter,
                    loss_factors="；".join(_NEXT_DAY_LOSS_FACTORS),
                    profit_factors="；".join(_NEXT_DAY_PROFIT_FACTORS),
                    pool=json.dumps(_request_payload(strategy_name, candidates, market_filter), ensure_ascii=False),
                )
            ),
        },
    ]


def _cache_key(
    strategy_name: str,
    market_filter: str,
    model_name: str,
    pool: List[Dict[str, object]],
    blend_alpha: float,
) -> str:
    payload = {
        "schema": _CACHE_SCHEMA_VERSION,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy": strategy_name,
        "market": market_filter,
        "model": model_name,
        "blend_alpha": blend_alpha,
        "pool": _request_payload(strategy_name, pool, market_filter),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cache(path: str) -> Dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cache = json.load(handle)
        return cache if isinstance(cache, dict) else {}
    except Exception:
        return {}


def _write_cache(path: str, cache: Dict[str, object]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        return


def _cache_entry_valid(entry: Dict[str, object], ttl_seconds: int) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("schema") != _CACHE_SCHEMA_VERSION:
        return False
    if entry.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return False
    if ttl_seconds <= 0:
        return True
    cached_at = coerce_number(entry.get("cached_at"), 0.0)
    return cached_at > 0 and (time.time() - cached_at) <= ttl_seconds


def _merge_ranking_rows(
    rows: List[Dict[str, object]],
    llm_records: List[Dict[str, object]],
    blend_alpha: float,
    strategy_name: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    llm_by_code = {}
    for item in llm_records:
        code = normalize_code(str(item.get("code", "")).strip())
        if not code:
            continue
        llm_score = coerce_number(item.get("llm_score"), float("nan"))
        if not math.isnan(llm_score):
            raw_flags = item.get("risk_flags", [])
            if isinstance(raw_flags, str):
                raw_flags = [raw_flags]
            risk_flags = [str(flag).strip() for flag in raw_flags if str(flag).strip()][:3] if isinstance(raw_flags, list) else []
            up_score = coerce_number(item.get("tomorrow_up_score"), llm_score)
            up_score = coerce_number(item.get("horizon_up_score"), up_score)
            penalty = _clamp(coerce_number(item.get("penalty"), 0.0), 0.0, 30.0)
            penalty = _clamp(
                penalty + (3.0 if _coerce_already_priced_in(item.get("already_priced_in")) else 0.0) + max(0.0, -_coerce_sentiment(item.get("sentiment"))),
                0.0,
                30.0,
            )
            veto = _coerce_bool(item.get("veto"))
            action = _coerce_action(item.get("action"), up_score, penalty, veto)
            if action == "avoid":
                penalty = max(penalty, 15.0)
            if veto:
                penalty = max(penalty, 30.0)
            llm_by_code[code] = {
                "llm_score": round(_clamp(llm_score, 0.0, 100.0), 2),
                "tomorrow_up_score": round(_clamp(up_score, 0.0, 100.0), 2),
                "action": action,
                "veto": veto,
                "penalty": round(penalty, 2),
                "reason": str(item.get("reason", "")).strip(),
                "risk_flags": risk_flags,
                "event_type": str(item.get("event_type", "")).strip(),
                "sentiment": _coerce_sentiment(item.get("sentiment")),
                "catalyst_strength": _coerce_catalyst_strength(item.get("catalyst_strength")),
                "time_sensitivity": _coerce_time_sensitivity(item.get("time_sensitivity")),
                "already_priced_in": _coerce_bool(item.get("already_priced_in")),
                "catalyst_score": round(_clamp(coerce_number(item.get("catalyst_score"), 50.0), 0.0, 100.0), 2),
                "theme_truth_score": round(_clamp(coerce_number(item.get("theme_truth_score"), 50.0), 0.0, 100.0), 2),
                "event_risk_score": round(_clamp(coerce_number(item.get("event_risk_score"), 50.0), 0.0, 100.0), 2),
            }

    merged: List[Dict[str, object]] = []
    for row in rows:
        next_row = dict(row)
        factor_review = _next_day_factor_review(next_row)
        code = normalize_code(str(next_row.get("code", "")).strip())
        base_score = coerce_number(next_row.get("score"), 0.0)
        llm_item = llm_by_code.get(code)
        if llm_item:
            llm_score = coerce_number(llm_item.get("llm_score"), base_score)
            up_score = coerce_number(llm_item.get("tomorrow_up_score"), llm_score)
            event_adjustment = _deepseek_event_adjustment(strategy_name, llm_item)
            up_score = _clamp(
                up_score
                + min(coerce_number(factor_review.get("bonus"), 0.0), 8.0) * 0.35
                + coerce_number(event_adjustment.get("bonus"), 0.0),
                0.0,
                100.0,
            )
            penalty = _clamp(
                coerce_number(llm_item.get("penalty"), 0.0)
                + coerce_number(event_adjustment.get("penalty"), 0.0)
                + coerce_number(factor_review.get("penalty"), 0.0),
                0.0,
                45.0,
            )
            combined = round((1.0 - blend_alpha) * base_score + blend_alpha * up_score - penalty, 2)
            if llm_item.get("veto") or factor_review.get("veto"):
                combined -= 50.0
            next_row["deepseek_score"] = llm_score
            next_row["tomorrow_up_score"] = up_score
            next_row["deepseek_horizon_score"] = up_score
            next_row["deepseek_action"] = llm_item.get("action") or "watch"
            if factor_review.get("veto"):
                next_row["deepseek_action"] = "avoid"
            next_row["deepseek_veto"] = bool(llm_item.get("veto") or factor_review.get("veto"))
            next_row["deepseek_penalty"] = penalty
            next_row["deepseek_reason"] = llm_item.get("reason") or ""
            next_row["deepseek_risk_flags"] = _unique_strings(
                list(llm_item.get("risk_flags") or []) + list(factor_review.get("risk_flags") or [])
            )[:6]
            next_row["deepseek_profit_flags"] = factor_review.get("profit_flags") or []
            next_row["deepseek_catalyst_score"] = llm_item.get("catalyst_score")
            next_row["deepseek_theme_truth_score"] = llm_item.get("theme_truth_score")
            next_row["deepseek_event_risk_score"] = llm_item.get("event_risk_score")
            next_row["deepseek_event_score"] = event_adjustment.get("event_score")
            next_row["deepseek_event_bonus"] = event_adjustment.get("bonus")
            next_row["deepseek_event_penalty"] = event_adjustment.get("penalty")
            next_row["deepseek_event_type"] = llm_item.get("event_type") or ""
            next_row["deepseek_sentiment"] = llm_item.get("sentiment")
            next_row["deepseek_catalyst_strength"] = llm_item.get("catalyst_strength")
            next_row["deepseek_time_sensitivity"] = llm_item.get("time_sensitivity")
            next_row["deepseek_already_priced_in"] = bool(llm_item.get("already_priced_in"))
            next_row["deepseek_rank_score"] = combined
            next_row["rerank_source"] = "deepseek"
        else:
            next_row["deepseek_score"] = None
            next_row["tomorrow_up_score"] = _clamp(base_score + min(coerce_number(factor_review.get("bonus"), 0.0), 8.0) * 0.35, 0.0, 100.0)
            next_row["deepseek_horizon_score"] = next_row["tomorrow_up_score"]
            next_row["deepseek_action"] = "avoid" if factor_review.get("veto") else "unknown"
            next_row["deepseek_veto"] = bool(factor_review.get("veto"))
            next_row["deepseek_penalty"] = coerce_number(factor_review.get("penalty"), 0.0)
            next_row["deepseek_reason"] = "未返回该票 LLM 打分，回退原始排序"
            next_row["deepseek_risk_flags"] = factor_review.get("risk_flags") or []
            next_row["deepseek_profit_flags"] = factor_review.get("profit_flags") or []
            next_row["deepseek_catalyst_score"] = None
            next_row["deepseek_theme_truth_score"] = None
            next_row["deepseek_event_risk_score"] = None
            next_row["deepseek_event_score"] = None
            next_row["deepseek_event_bonus"] = 0.0
            next_row["deepseek_event_penalty"] = 0.0
            next_row["deepseek_event_type"] = ""
            next_row["deepseek_sentiment"] = 0.0
            next_row["deepseek_catalyst_strength"] = None
            next_row["deepseek_time_sensitivity"] = "长期"
            next_row["deepseek_already_priced_in"] = False
            next_row["deepseek_rank_score"] = round(base_score - coerce_number(factor_review.get("penalty"), 0.0), 2)
        merged.append(next_row)

    gated = []
    filtered = []
    for row in merged:
        keep, reason = _deepseek_gate_decision(row)
        if keep:
            gated.append(row)
        else:
            next_row = dict(row)
            next_row["deepseek_filter_reason"] = reason
            filtered.append(next_row)

    gated.sort(key=lambda item: coerce_number(item.get("deepseek_rank_score"), 0.0), reverse=True)
    for rank, row in enumerate(gated, start=1):
        row["rank"] = rank
    return gated, {
        "covered": len(llm_by_code),
        "total": len(rows),
        "filtered": len(filtered),
        "filtered_codes": [row.get("code") for row in filtered[:8]],
        "filter_reasons": _filter_reason_counts(filtered),
    }


def _deepseek_gate_decision(row: Dict[str, object]) -> Tuple[bool, str]:
    rank_score = coerce_number(row.get("deepseek_rank_score"), coerce_number(row.get("score"), 0.0))
    penalty = coerce_number(row.get("deepseek_penalty"), 0.0)
    action = str(row.get("deepseek_action") or "").lower()
    if row.get("deepseek_veto"):
        return False, "deepseek_veto"
    if penalty >= 30:
        return False, "deepseek_penalty_high"
    if action == "avoid" and penalty >= 15:
        return False, "deepseek_avoid"
    if rank_score < 45:
        return False, "deepseek_rank_score_low"
    return True, ""


def _filter_reason_counts(rows: List[Dict[str, object]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        reason = str(row.get("deepseek_filter_reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def rerank_candidates(
    rows: List[Dict[str, object]],
    strategy_name: str,
    market_filter: str = "all",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    strategy_name = storage_strategy_name(strategy_name)
    config = _coerce_env_config()
    if not config.get("enabled", False):
        return rows, {"enabled": False, "status": "disabled"}
    if config["api_key"] == "":
        return rows, {"enabled": False, "status": "missing_api_key"}
    if strategy_name not in _SUPPORTED_STRATEGIES:
        return rows, {"enabled": False, "status": "strategy_not_supported", "strategy": strategy_name}
    if config["strategies"] and strategy_name not in config["strategies"]:
        return rows, {"enabled": False, "status": "strategy_not_enabled", "strategy": strategy_name}

    total = len(rows)
    review_limit = min(int(config["review_limit"]), total)
    if total < 3:
        return rows, {"enabled": True, "status": "insufficient_rows", "requested": total}

    pool = [dict(row) for row in rows[:review_limit] if isinstance(row, dict)]
    if not pool:
        return rows, {"enabled": True, "status": "no_valid_pool", "requested": total}

    use_pro = str(strategy_name) in config["pro_strategies"]
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
    cache_key = _cache_key(
        strategy_name,
        market_filter,
        selected_model,
        pool,
        float(config["blend_alpha"]),
    )
    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        entry = cache.get(cache_key)
        if _cache_entry_valid(entry, int(config["cache_ttl_seconds"])):
            results = _extract_results(entry.get("parsed") if isinstance(entry, dict) else {})
            if results:
                merged_rows, coverage = _merge_ranking_rows(rows, results, float(config["blend_alpha"]), strategy_name)
                return merged_rows, {
                    "enabled": True,
                    "status": "cache_hit",
                    "strategy": strategy_name,
                    "requested": total,
                    "review_limit": review_limit,
                    "covered": coverage.get("covered", 0),
                    "filtered": coverage.get("filtered", 0),
                    "filtered_codes": coverage.get("filtered_codes", []),
                    "filter_reasons": coverage.get("filter_reasons", {}),
                    "source": "deepseek_cache",
                    "model": selected_model,
                    "model_tier": model_tier,
                    "base_model": config["model"],
                    "cache_key": cache_key[:12],
                    "cached_at": entry.get("cached_at"),
                    "usage": entry.get("usage", {}),
                    "cost_hint": {"cached": True},
                }

    messages = _build_messages(strategy_name, pool, market_filter)
    url = _deepseek_chat_url(config["base_url"])
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": selected_model,
        "messages": messages,
        "temperature": 0.15,
        "max_tokens": config["max_tokens"],
        "response_format": {"type": "json_object"},
    }

    parsed = None
    usage = {}
    last_error = ""
    attempt = 0
    while attempt <= int(config["retry_count"]):
        attempt += 1
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=float(config["timeout_seconds"]),
            )
            if response.status_code in (429, 500, 502, 503, 504) and attempt <= int(config["retry_count"]):
                last_error = "可重试响应码: {}".format(response.status_code)
                time.sleep((2**(attempt - 1)) * float(config["retry_base_delay"]))
                continue
            response.raise_for_status()
            raw = response.json()
            usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
            content = (
                (raw.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            parsed = _safe_parse_json(str(content))
            if parsed is not None:
                break
            last_error = "响应无法解析 JSON"
        except Exception as exc:
            last_error = str(exc)
            if attempt <= int(config["retry_count"]):
                time.sleep((2**(attempt - 1)) * float(config["retry_base_delay"]))
            else:
                break

    if parsed is None:
        return rows, {
            "enabled": True,
            "status": "fallback",
            "error": last_error,
            "requested": total,
            "review_limit": review_limit,
        }

    results = _extract_results(parsed)
    merged_rows, coverage = _merge_ranking_rows(rows, results, float(config["blend_alpha"]), strategy_name)
    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        cache[cache_key] = {
            "schema": _CACHE_SCHEMA_VERSION,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "cached_at": time.time(),
            "strategy": strategy_name,
            "market": market_filter,
            "model": selected_model,
            "model_tier": model_tier,
            "parsed": parsed,
            "usage": usage,
        }
        _write_cache(str(config["cache_path"]), cache)
    meta = {
        "enabled": True,
        "status": "ok",
        "strategy": strategy_name,
        "requested": total,
        "review_limit": review_limit,
        "covered": coverage.get("covered", 0),
        "filtered": coverage.get("filtered", 0),
        "filtered_codes": coverage.get("filtered_codes", []),
        "filter_reasons": coverage.get("filter_reasons", {}),
        "source": "deepseek_chat",
        "model": payload["model"],
        "model_tier": model_tier,
        "base_model": config["model"],
        "cache_key": cache_key[:12],
        "attempts": attempt,
        "usage": usage,
        "cost_hint": usage,
    }
    return merged_rows, meta


def _validation_sample_payload(samples: List[Dict[str, object]], limit: int = 8) -> Dict[str, object]:
    failed = sorted(samples, key=lambda item: coerce_number(item.get("primary_return_net"), 0.0))[:limit]
    success = sorted(samples, key=lambda item: coerce_number(item.get("primary_return_net"), 0.0), reverse=True)[:max(3, limit // 2)]

    factor_fields = (
        "ret_3d",
        "ret_5d",
        "ret_10d",
        "ret_20d",
        "ma5_gap",
        "ma20_gap",
        "ma60_gap",
        "vol_amount_5d",
        "vol_ma5_ratio",
        "turnover_20d",
        "breakout_20d",
        "volatility_20d",
        "alphalite_coverage",
    )

    def _factor_snapshot(row: Dict[str, object], raw: Dict[str, object]) -> Dict[str, object]:
        factors = row.get("factor_snapshot") if isinstance(row.get("factor_snapshot"), dict) else {}
        if not factors:
            factors = raw.get("factor_snapshot") if isinstance(raw.get("factor_snapshot"), dict) else {}
        if not factors:
            factors = raw.get("alphalite_factor") if isinstance(raw.get("alphalite_factor"), dict) else {}
        compact = {}
        for field in factor_fields:
            if field in factors:
                compact[field] = round(coerce_number(factors.get(field), 0.0), 4)
        return compact

    def _case(row: Dict[str, object]) -> Dict[str, object]:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        item = {
            "date": row.get("signal_date", ""),
            "code": row.get("code", ""),
            "name": row.get("name", ""),
            "rank": row.get("rank", 0),
            "score": coerce_number(row.get("stored_score"), coerce_number(raw.get("score"), 0.0)),
            "pct_chg": coerce_number(raw.get("pct_chg"), 0.0),
            "turnover_rate": coerce_number(raw.get("turnover_rate"), 0.0),
            "volume_ratio": coerce_number(raw.get("volume_ratio"), 0.0),
            "sixty_day_pct": coerce_number(raw.get("sixty_day_pct"), 0.0),
            "primary_return_net": coerce_number(row.get("primary_return_net"), 0.0),
            "max_drawdown": coerce_number(row.get("max_drawdown"), 0.0),
            "reasons": raw.get("reasons", [])[:4] if isinstance(raw.get("reasons"), list) else [],
        }
        factors = _factor_snapshot(row, raw)
        if factors:
            item["factor_snapshot"] = factors
        return item

    return {
        "failed_cases": [_case(row) for row in failed],
        "success_cases": [_case(row) for row in success],
    }


def review_strategy_validation(
    strategy_name: str,
    metrics: Dict[str, object],
    samples: List[Dict[str, object]],
    days: int = 20,
) -> Dict[str, object]:
    strategy_name = storage_strategy_name(strategy_name)
    config = _coerce_env_config()
    if not config.get("enabled", False):
        return {"enabled": False, "status": "disabled"}
    if config["api_key"] == "":
        return {"enabled": False, "status": "missing_api_key"}
    if strategy_name not in _SUPPORTED_STRATEGIES:
        return {"enabled": False, "status": "strategy_not_supported", "strategy": strategy_name}
    if config["strategies"] and strategy_name not in config["strategies"]:
        return {"enabled": False, "status": "strategy_not_enabled", "strategy": strategy_name}

    use_pro = str(strategy_name) in config["pro_strategies"]
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
    context = _strategy_context(strategy_name)
    cases = _validation_sample_payload(samples)
    review_input = {
        "schema": _CACHE_SCHEMA_VERSION,
        "kind": "validation_review",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy": strategy_name,
        "horizon": context["horizon"],
        "focus": context["focus"],
        "days": int(days),
        "model": selected_model,
        "metrics": {
            "sample_count": metrics.get("sample_count", 0),
            "real_sample_count": metrics.get("real_sample_count", 0),
            "replay_sample_count": metrics.get("replay_sample_count", 0),
            "win_rate_primary_net": metrics.get("win_rate_primary_net"),
            "avg_primary_return_net": metrics.get("avg_primary_return_net"),
            "real_win_rate_primary_net": metrics.get("real_win_rate_primary_net"),
            "real_avg_primary_return_net": metrics.get("real_avg_primary_return_net"),
            "avg_max_drawdown_3d": metrics.get("avg_max_drawdown_3d"),
            "execution_skipped_count": metrics.get("execution_skipped_count", 0),
            "primary_horizon_label": metrics.get("primary_horizon_label", ""),
        },
        **cases,
    }
    raw_key = json.dumps(review_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        entry = cache.get(cache_key)
        if _cache_entry_valid(entry, int(config["cache_ttl_seconds"])):
            parsed = entry.get("parsed") if isinstance(entry, dict) else {}
            if isinstance(parsed, dict):
                return {
                    "enabled": True,
                    "status": "cache_hit",
                    "strategy": strategy_name,
                    "source": "deepseek_cache",
                    "model": selected_model,
                    "model_tier": model_tier,
                    "cache_key": cache_key[:12],
                    "cached_at": entry.get("cached_at"),
                    **parsed,
                }

    messages = [
        {
            "role": "system",
            "content": (
                "你是A股策略复盘助手。请只输出 JSON，不要 Markdown。"
                "输出字段: decision、avoid_conditions、suggested_filters、suggested_penalties、summary、rule_candidates。"
                "rule_candidates 是可验证规则数组，每项包含 field、operator、threshold、penalty、reason。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请根据最近策略验证结果，给出反推荐条件和降权建议。"
                "不要建议加仓，不要承诺收益，只总结哪些情况应避免或降权。"
                "策略周期: {horizon}。复核重点: {focus}。"
                "复盘时必须对照亏钱因素: {loss_factors}。"
                "同时对照赚钱因素: {profit_factors}。"
                "suggested_penalties 每项包含 condition 和 penalty(0-30)。"
                "rule_candidates 只给能用本地字段验证的规则，例如 pct_chg、volume_ratio、turnover_rate、amplitude、sixty_day_pct、risk_penalty，"
                "或 factor_snapshot.ret_20d、factor_snapshot.ma20_gap、factor_snapshot.vol_ma5_ratio、factor_snapshot.breakout_20d。"
                "输入: {payload}"
            ).format(
                horizon=context["horizon"],
                focus=context["focus"],
                loss_factors="；".join(_NEXT_DAY_LOSS_FACTORS),
                profit_factors="；".join(_NEXT_DAY_PROFIT_FACTORS),
                payload=json.dumps(review_input, ensure_ascii=False),
            ),
        },
    ]
    payload = {
        "model": selected_model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max(700, int(config["max_tokens"])),
        "response_format": {"type": "json_object"},
    }
    url = _deepseek_chat_url(config["base_url"])
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    parsed = None
    usage = {}
    last_error = ""
    attempt = 0
    retry_count = int(config.get("validation_retry_count", 0))
    timeout_seconds = min(6.0, float(config.get("validation_timeout_seconds", config["timeout_seconds"])))
    timed_out = False
    while attempt <= retry_count:
        attempt += 1
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
            if response.status_code in (429, 500, 502, 503, 504) and attempt <= retry_count:
                last_error = "可重试响应码: {}".format(response.status_code)
                time.sleep((2**(attempt - 1)) * float(config["retry_base_delay"]))
                continue
            response.raise_for_status()
            raw = response.json()
            usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
            content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
            parsed = _safe_parse_json(str(content))
            if isinstance(parsed, dict):
                break
            last_error = "响应无法解析 JSON"
        except Exception as exc:
            last_error = str(exc)
            timed_out = isinstance(exc, requests.exceptions.Timeout) or "timed out" in last_error.lower()
            if attempt <= retry_count:
                time.sleep((2**(attempt - 1)) * float(config["retry_base_delay"]))

    if not isinstance(parsed, dict):
        return {
            "enabled": True,
            "status": "timeout" if timed_out else "fallback",
            "strategy": strategy_name,
            "error": last_error,
        }

    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        cache[cache_key] = {
            "schema": _CACHE_SCHEMA_VERSION,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "cached_at": time.time(),
            "strategy": strategy_name,
            "model": selected_model,
            "model_tier": model_tier,
            "parsed": parsed,
            "usage": usage,
        }
        _write_cache(str(config["cache_path"]), cache)
    return {
        "enabled": True,
        "status": "ok",
        "strategy": strategy_name,
        "source": "deepseek_chat",
        "model": selected_model,
        "model_tier": model_tier,
        "cache_key": cache_key[:12],
        "attempts": attempt,
        "usage": usage,
        **parsed,
    }


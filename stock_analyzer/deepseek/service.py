from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import re
import time
from typing import Dict, List, Tuple

import requests

from .budget_policy import BudgetPolicy
from .batch_rerank_service import BatchRerankService
from .event_score import (
    coerce_already_priced_in as _coerce_already_priced_in,
    coerce_catalyst_strength as _coerce_catalyst_strength,
    coerce_sentiment as _coerce_sentiment,
    coerce_time_sensitivity as _coerce_time_sensitivity,
    deepseek_event_adjustment as _deepseek_event_adjustment,
)
from .http_client import DeepSeekHttpClient
from .market_gate_service import MarketGateReviewService
from .news_context import NewsContextProvider
from .payload_builder import PayloadBuilder
from .rerank_service import RerankService
from .result_merger import ResultMerger
from .configuration import DeepSeekRuntimeConfig
from .cache import DeepSeekCache
from .usage_accounting import UsageAccounting
from .validation_review_service import ValidationReviewService
from ..normalization import coerce_number, normalize_code
from ..strategies.types import storage_strategy_name
from ..deepseek_rules import rule_penalty_for_row
from .. import config


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
        "blend_alpha": max(0.0, min(1.0, _env_float("DEEPSEEK_BLEND_ALPHA", 0.15))),
        "batch_review_limit": max(5, min(15, _env_int("DEEPSEEK_BATCH_REVIEW_LIMIT", 15))),
        "cascade_filter_enabled": bool(getattr(config, "DEEPSEEK_CASCADE_FILTER_ENABLED", True)),
        "cascade_max_review": max(3, min(15, int(getattr(config, "DEEPSEEK_CASCADE_MAX_REVIEW", 8)))),
        "strategies": _coerce_strategies("DEEPSEEK_STRATEGIES"),
        "pro_strategies": _coerce_strategies("DEEPSEEK_PRO_STRATEGIES"),
        "cache_enabled": _env_bool("DEEPSEEK_CACHE_ENABLED", True),
        "cache_path": _resolve_project_path(os.getenv("DEEPSEEK_CACHE_PATH", ".runtime/deepseek_cache.json")),
        "cache_ttl_seconds": max(0, _env_int("DEEPSEEK_CACHE_TTL_SECONDS", 86400)),
    }


_RUNTIME_CONFIG = DeepSeekRuntimeConfig(_coerce_env_config)
_DEEPSEEK_CACHE = DeepSeekCache()
_DEEPSEEK_HTTP_CLIENT = DeepSeekHttpClient(requests_module=requests, sleep_func=time.sleep)
_BUDGET_POLICY = BudgetPolicy()
_USAGE_ACCOUNTING = UsageAccounting()


def _market_data_provider_factory():
    from ..providers import MarketDataProvider

    return MarketDataProvider()


def _score_news_items(items):
    from ..sentiment import score_news_items

    return score_news_items(items)


_NEWS_CONTEXT_PROVIDER = NewsContextProvider(
    config_module=config,
    time_module=time,
    provider_factory=_market_data_provider_factory,
    score_news_items=_score_news_items,
)


def _runtime_config() -> Dict[str, object]:
    return _RUNTIME_CONFIG.load()


def _strategy_blend_alpha(strategy_name: str, default: float) -> float:
    path = str(getattr(config, "WEIGHTS_OVERRIDE_PATH", ".runtime/weights.json") or "")
    if not path or not os.path.exists(path):
        return max(0.0, min(1.0, float(default)))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        alpha_map = payload.get("deepseek_blend_alpha") if isinstance(payload, dict) else {}
        value = alpha_map.get(strategy_name) if isinstance(alpha_map, dict) else None
        if value is None:
            return max(0.0, min(1.0, float(default)))
        return max(0.0, min(1.0, coerce_number(value, default)))
    except Exception:
        return max(0.0, min(1.0, float(default)))


def _batch_max_tokens(candidate_count: int, configured_max_tokens: int) -> int:
    needed = 300 + max(0, int(candidate_count)) * 150
    return max(900, int(configured_max_tokens), needed)


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


def _usage_cost_hint(
    usage: Dict[str, object],
    model: str = "",
    model_tier: str = "",
    cached: bool = False,
    allocation_ratio: float = 1.0,
) -> Dict[str, object]:
    return _USAGE_ACCOUNTING.cost_hint(
        usage,
        model=model,
        model_tier=model_tier,
        cached=cached,
        allocation_ratio=allocation_ratio,
    )


def _deepseek_efficiency_meta(
    requested_count: int,
    reviewed_count: int,
    usage_or_cost_hint: Dict[str, object],
    review_policy: Dict[str, object] = None,
) -> Dict[str, object]:
    return _USAGE_ACCOUNTING.efficiency_meta(
        requested_count,
        reviewed_count,
        usage_or_cost_hint,
        review_policy=review_policy,
    )


def _attach_deepseek_cost_metadata(
    rows: List[Dict[str, object]],
    cost_hint: Dict[str, object],
    call_id: str,
    source: str,
) -> None:
    _USAGE_ACCOUNTING.attach_row_metadata(rows, cost_hint, call_id, source)


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


def _news_context_enabled() -> bool:
    return _NEWS_CONTEXT_PROVIDER.enabled()


def _attach_news_context(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    return _NEWS_CONTEXT_PROVIDER.attach(rows)


def _cached_news_context(entry: object, limit: int):
    return _NEWS_CONTEXT_PROVIDER.cached_context(entry, limit)


def _prune_news_cache(cache: Dict[str, object]) -> Tuple[Dict[str, object], bool]:
    return _NEWS_CONTEXT_PROVIDER.prune_cache(cache)


def _compact_news_items(items: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
    return _NEWS_CONTEXT_PROVIDER.compact_news_items(items, limit)


def _compact_news_sentiment(scored: Dict[str, object]) -> Dict[str, object]:
    return _NEWS_CONTEXT_PROVIDER.compact_news_sentiment(scored)


def _announcement_flags(row: Dict[str, object], news_items: List[Dict[str, object]], sentiment: Dict[str, object]) -> List[str]:
    return _NEWS_CONTEXT_PROVIDER.announcement_flags(row, news_items, sentiment)


def _read_news_cache() -> Dict[str, object]:
    return _NEWS_CONTEXT_PROVIDER.read_cache()


def _write_news_cache(cache: Dict[str, object]) -> None:
    _NEWS_CONTEXT_PROVIDER.write_cache(cache)


def _merge_review_context(rows: List[Dict[str, object]], pool: List[Dict[str, object]]) -> List[Dict[str, object]]:
    if not rows or not pool:
        return rows
    context_by_code = {}
    for item in pool:
        code = normalize_code(item.get("code"))
        if not code:
            continue
        context_by_code[code] = {
            key: item.get(key)
            for key in ("recent_news", "announcement_flags", "news_sentiment", "news_context_status", "news_context_error")
            if key in item
        }
    if not context_by_code:
        return rows
    merged = []
    for row in rows:
        item = dict(row)
        context = context_by_code.get(normalize_code(item.get("code")))
        if context:
            item.update(context)
        merged.append(item)
    return merged


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


_PAYLOAD_BUILDER = PayloadBuilder(_strategy_context, _NEXT_DAY_LOSS_FACTORS, _NEXT_DAY_PROFIT_FACTORS)


def _payload_number(value, default: float = 0.0, digits: int = 1):
    return _PAYLOAD_BUILDER.payload_number(value, default, digits)


def _payload_strings(values, limit: int = 4) -> List[str]:
    return _PAYLOAD_BUILDER.payload_strings(values, limit)


def _payload_news(items) -> List[Dict[str, object]]:
    return _PAYLOAD_BUILDER.payload_news(items)


def _payload_news_sentiment(payload) -> Dict[str, object]:
    return _PAYLOAD_BUILDER.payload_news_sentiment(payload)


def _request_payload(strategy_name: str, candidates: List[Dict[str, object]], market_filter: str) -> List[Dict[str, object]]:
    return _PAYLOAD_BUILDER.request_payload(strategy_name, candidates, market_filter)


def _row_has_llm_edge_context(row: Dict[str, object]) -> bool:
    return _BUDGET_POLICY.row_has_llm_edge_context(row)


def _row_is_llm_ambiguous(row: Dict[str, object]) -> bool:
    return _BUDGET_POLICY.row_is_llm_ambiguous(row)


def _select_single_review_pool(
    rows: List[Dict[str, object]],
    review_limit: int,
    config: Dict[str, object],
    model_tier: str = "base",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    return _BUDGET_POLICY.select_single_review_pool(rows, review_limit, config, model_tier)


def _select_batch_review_pool(
    rows: List[Dict[str, object]],
    batch_limit: int,
    config: Dict[str, object],
    model_tier: str = "base",
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    return _BUDGET_POLICY.select_batch_review_pool(rows, batch_limit, config, model_tier)


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
    return _PAYLOAD_BUILDER.build_messages(strategy_name, candidates, market_filter)


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
    return _DEEPSEEK_CACHE.read(path)


def _write_cache(path: str, cache: Dict[str, object]) -> None:
    _DEEPSEEK_CACHE.write(path, cache)


def _cache_entry_valid(entry: Dict[str, object], ttl_seconds: int) -> bool:
    return _DEEPSEEK_CACHE.entry_valid(entry, ttl_seconds, schema_version=_CACHE_SCHEMA_VERSION)


def _merge_ranking_rows(
    rows: List[Dict[str, object]],
    llm_records: List[Dict[str, object]],
    blend_alpha: float,
    strategy_name: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    return _RESULT_MERGER.merge_ranking_rows(rows, llm_records, blend_alpha, strategy_name)


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


def _compact_filtered_rows(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    keys = (
        "rank",
        "local_rank",
        "code",
        "name",
        "market",
        "market_label",
        "theme",
        "price",
        "pct_chg",
        "turnover",
        "volume_ratio",
        "turnover_rate",
        "sixty_day_pct",
        "ytd_pct",
        "score",
        "reasons",
        "tier",
        "deepseek_covered",
        "deepseek_blend_alpha",
        "blend_alpha",
        "deepseek_score",
        "deepseek_horizon_score",
        "tomorrow_up_score",
        "deepseek_action",
        "deepseek_veto",
        "deepseek_penalty",
        "deepseek_rule_penalty",
        "deepseek_rules_matched",
        "deepseek_rank_score",
        "deepseek_reason",
        "deepseek_filter_reason",
        "deepseek_risk_flags",
        "deepseek_profit_flags",
        "deepseek_catalyst_score",
        "deepseek_theme_truth_score",
        "deepseek_event_risk_score",
        "deepseek_event_score",
        "deepseek_event_bonus",
        "deepseek_event_penalty",
        "deepseek_event_type",
        "deepseek_sentiment",
        "deepseek_catalyst_strength",
        "deepseek_time_sensitivity",
        "deepseek_already_priced_in",
        "deepseek_call_id",
        "deepseek_call_source",
        "deepseek_usage",
        "deepseek_cost_hint",
        "deepseek_total_tokens",
        "deepseek_billable_tokens",
        "rerank_source",
    )
    result = []
    for row in rows:
        item = {key: row.get(key) for key in keys if key in row}
        item["deepseek_shadow_signal"] = True
        result.append(item)
    return result


_RESULT_MERGER = ResultMerger(
    normalize_code=normalize_code,
    coerce_number=coerce_number,
    clamp=_clamp,
    coerce_bool=_coerce_bool,
    coerce_action=_coerce_action,
    coerce_already_priced_in=_coerce_already_priced_in,
    coerce_sentiment=_coerce_sentiment,
    coerce_catalyst_strength=_coerce_catalyst_strength,
    coerce_time_sensitivity=_coerce_time_sensitivity,
    deepseek_event_adjustment=_deepseek_event_adjustment,
    next_day_factor_review=_next_day_factor_review,
    rule_penalty_for_row=rule_penalty_for_row,
    unique_strings=_unique_strings,
    gate_decision=_deepseek_gate_decision,
    compact_filtered_rows=_compact_filtered_rows,
    filter_reason_counts=_filter_reason_counts,
)


def _rerank_candidates_impl(
    rows: List[Dict[str, object]],
    strategy_name: str,
    market_filter: str = "all",
    model_tier_override: str = "",
    review_limit_override: int = 0,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    strategy_name = storage_strategy_name(strategy_name)
    config = _runtime_config()
    if not config.get("enabled", False):
        return rows, {"enabled": False, "status": "disabled"}
    if config["api_key"] == "":
        return rows, {"enabled": False, "status": "missing_api_key"}
    if strategy_name not in _SUPPORTED_STRATEGIES:
        return rows, {"enabled": False, "status": "strategy_not_supported", "strategy": strategy_name}
    if config["strategies"] and strategy_name not in config["strategies"]:
        return rows, {"enabled": False, "status": "strategy_not_enabled", "strategy": strategy_name}

    total = len(rows)
    configured_review_limit = int(review_limit_override or config["review_limit"])
    review_limit = min(configured_review_limit, total)
    if total < 3:
        return rows, {"enabled": True, "status": "insufficient_rows", "requested": total}

    use_pro = model_tier_override == "pro" or (
        model_tier_override not in {"base", "pro"} and str(strategy_name) in config["pro_strategies"]
    )
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
    scan_pool = [dict(row) for row in rows[:review_limit] if isinstance(row, dict)]
    if not scan_pool:
        return rows, {"enabled": True, "status": "no_valid_pool", "requested": total}
    scan_pool = _attach_news_context(scan_pool)
    pool, review_policy = _select_single_review_pool(scan_pool, review_limit, config, model_tier)
    if not pool:
        if int(review_policy.get("input_count") or 0) <= 0:
            status = "no_valid_pool"
            reason = "候选池没有可发送 DeepSeek 的有效记录。"
        elif int(review_policy.get("executable_count") or 0) <= 0:
            status = "no_executable_review_candidates"
            reason = "候选均为备选/观察/零仓位，未发送 DeepSeek。"
        elif model_tier == "pro":
            status = "no_pro_boundary_samples"
            reason = "Pro 档仅复核边界样本，本轮未发现需要 Pro 复核的边界候选。"
        else:
            status = "no_review_candidates"
            reason = "本轮没有需要 DeepSeek 复核的候选。"
        return rows, {
            "enabled": True,
            "status": status,
            "strategy": strategy_name,
            "requested": total,
            "review_limit": 0,
            "model": selected_model,
            "model_tier": model_tier,
            "base_model": config["model"],
            "review_policy": review_policy,
            "reason": reason,
            **_deepseek_efficiency_meta(total, 0, {}, review_policy),
        }
    rows = _merge_review_context(rows, pool)
    blend_alpha = _strategy_blend_alpha(strategy_name, float(config["blend_alpha"]))
    cache_key = _cache_key(
        strategy_name,
        market_filter,
        selected_model,
        pool,
        blend_alpha,
    )
    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        entry = cache.get(cache_key)
        if _cache_entry_valid(entry, int(config["cache_ttl_seconds"])):
            results = _extract_results(entry.get("parsed") if isinstance(entry, dict) else {})
            if results:
                merged_rows, coverage = _merge_ranking_rows(rows, results, blend_alpha, strategy_name)
                cost_hint = _usage_cost_hint(entry.get("usage", {}), selected_model, model_tier, cached=True)
                _attach_deepseek_cost_metadata(merged_rows, cost_hint, cache_key[:12], "deepseek_cache")
                _attach_deepseek_cost_metadata(
                    coverage.get("filtered_rows", []),
                    cost_hint,
                    cache_key[:12],
                    "deepseek_cache",
                )
                return merged_rows, {
                    "enabled": True,
                    "status": "cache_hit",
                    "strategy": strategy_name,
                    "requested": total,
                    "review_limit": len(pool),
                    "covered": coverage.get("covered", 0),
                    "filtered": coverage.get("filtered", 0),
                    "filtered_codes": coverage.get("filtered_codes", []),
                    "filtered_rows": coverage.get("filtered_rows", []),
                    "filter_reasons": coverage.get("filter_reasons", {}),
                    "source": "deepseek_cache",
                    "model": selected_model,
                    "model_tier": model_tier,
                    "base_model": config["model"],
                    "blend_alpha": blend_alpha,
                    "cache_key": cache_key[:12],
                    "cached_at": entry.get("cached_at"),
                    "usage": entry.get("usage", {}),
                    "cost_hint": cost_hint,
                    "review_policy": review_policy,
                    **_deepseek_efficiency_meta(total, len(pool), cost_hint, review_policy),
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

    http_result = _DEEPSEEK_HTTP_CLIENT.post_json(
        url,
        headers=headers,
        payload=payload,
        timeout=float(config["timeout_seconds"]),
        retry_count=int(config["retry_count"]),
        retry_base_delay=float(config["retry_base_delay"]),
        parse_content=_safe_parse_json,
    )
    parsed = http_result.parsed
    usage = http_result.usage
    last_error = http_result.error
    attempt = http_result.attempts

    if parsed is None:
        return rows, {
            "enabled": True,
            "status": "fallback",
            "error": last_error,
            "requested": total,
            "review_limit": len(pool),
            "review_policy": review_policy,
            **_deepseek_efficiency_meta(total, len(pool), {}, review_policy),
        }

    results = _extract_results(parsed)
    merged_rows, coverage = _merge_ranking_rows(rows, results, blend_alpha, strategy_name)
    cost_hint = _usage_cost_hint(usage, selected_model, model_tier, cached=False)
    _attach_deepseek_cost_metadata(merged_rows, cost_hint, cache_key[:12], "deepseek_chat")
    _attach_deepseek_cost_metadata(coverage.get("filtered_rows", []), cost_hint, cache_key[:12], "deepseek_chat")
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
            "cost_hint": cost_hint,
        }
        _write_cache(str(config["cache_path"]), cache)
    meta = {
        "enabled": True,
        "status": "ok",
        "strategy": strategy_name,
        "requested": total,
        "review_limit": len(pool),
        "covered": coverage.get("covered", 0),
        "filtered": coverage.get("filtered", 0),
        "filtered_codes": coverage.get("filtered_codes", []),
        "filtered_rows": coverage.get("filtered_rows", []),
        "filter_reasons": coverage.get("filter_reasons", {}),
        "source": "deepseek_chat",
        "model": payload["model"],
        "model_tier": model_tier,
        "base_model": config["model"],
        "blend_alpha": blend_alpha,
        "cache_key": cache_key[:12],
        "attempts": attempt,
        "usage": usage,
        "cost_hint": cost_hint,
        "review_policy": review_policy,
    }
    meta.update(_deepseek_efficiency_meta(total, len(pool), cost_hint, review_policy))
    return merged_rows, meta


def _rerank_candidates_batch_impl(
    rows_by_strategy: Dict[str, List[Dict[str, object]]],
    market_filter: str = "all",
    model_tier_override: str = "",
    review_limit_override: int = 0,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, object]]]:
    config = _runtime_config()
    normalized_rows = {
        storage_strategy_name(strategy): list(rows or [])
        for strategy, rows in (rows_by_strategy or {}).items()
    }
    result_rows = {strategy: list(rows or []) for strategy, rows in normalized_rows.items()}
    meta_by_strategy: Dict[str, Dict[str, object]] = {}

    if not config.get("enabled", False):
        return result_rows, {
            strategy: {"enabled": False, "status": "disabled", "strategy": strategy}
            for strategy in result_rows
        }
    if config["api_key"] == "":
        return result_rows, {
            strategy: {"enabled": False, "status": "missing_api_key", "strategy": strategy}
            for strategy in result_rows
        }

    active_payloads = []
    active_rows: Dict[str, List[Dict[str, object]]] = {}
    active_alphas: Dict[str, float] = {}
    active_cascade: Dict[str, Dict[str, object]] = {}
    active_review_limits: Dict[str, int] = {}
    configured_review_limit = int(review_limit_override or config["review_limit"])
    batch_limit = min(configured_review_limit, int(config.get("batch_review_limit") or 15))
    use_pro = model_tier_override == "pro"
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
    for strategy_name, rows in normalized_rows.items():
        total = len(rows)
        if strategy_name not in _SUPPORTED_STRATEGIES:
            meta_by_strategy[strategy_name] = {"enabled": False, "status": "strategy_not_supported", "strategy": strategy_name}
            continue
        if config["strategies"] and strategy_name not in config["strategies"]:
            meta_by_strategy[strategy_name] = {"enabled": False, "status": "strategy_not_enabled", "strategy": strategy_name}
            continue
        if total < 3:
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "insufficient_rows",
                "strategy": strategy_name,
                "requested": total,
            }
            continue
        blend_alpha = _strategy_blend_alpha(strategy_name, float(config["blend_alpha"]))
        if blend_alpha <= 0:
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "alpha_zero_skipped",
                "strategy": strategy_name,
                "requested": total,
                "review_limit": 0,
                "blend_alpha": 0.0,
                "reason": "OOS归因判定 DeepSeek 无增益，跳过批量 payload 以节省输入 token。",
            }
            continue
        scan_pool = [dict(row) for row in rows[: min(batch_limit, total)] if isinstance(row, dict)]
        if not scan_pool:
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "no_valid_pool",
                "strategy": strategy_name,
                "requested": total,
            }
            continue
        scan_pool = _attach_news_context(scan_pool)
        pool, cascade_meta = _select_batch_review_pool(scan_pool, batch_limit, config, model_tier)
        if not pool:
            if int(cascade_meta.get("executable_count") or 0) <= 0 and int(cascade_meta.get("input_count") or 0) > 0:
                status = "no_executable_review_candidates"
                reason = "候选均为备选/观察/零仓位，未发送 DeepSeek。"
            elif model_tier == "pro":
                status = "no_pro_boundary_samples"
                reason = "Pro 档仅复核边界样本，本策略未发现需要 Pro 复核的边界候选。"
            else:
                status = "local_confident_skipped"
                reason = "候选缺少事件/新闻模糊点，本地高置信直接放行，未发送 DeepSeek。"
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": status,
                "strategy": strategy_name,
                "requested": total,
                "review_limit": 0,
                "blend_alpha": blend_alpha,
                "cascade_filter": cascade_meta,
                "review_policy": cascade_meta,
                "reason": reason,
                **_deepseek_efficiency_meta(total, 0, {}, cascade_meta),
            }
            continue
        result_rows[strategy_name] = _merge_review_context(rows, pool)
        active_rows[strategy_name] = result_rows[strategy_name]
        active_alphas[strategy_name] = blend_alpha
        active_cascade[strategy_name] = cascade_meta
        active_review_limits[strategy_name] = len(pool)
        candidates_payload = _request_payload(strategy_name, pool, market_filter)
        active_payloads.append(
            {
                "strategy": strategy_name,
                "horizon": _strategy_context(strategy_name)["horizon"],
                "focus": _strategy_context(strategy_name)["focus"],
                "review_limit": len(pool),
                "cascade_filter": cascade_meta,
                "candidates": candidates_payload,
            }
        )

    if not active_payloads:
        return result_rows, meta_by_strategy

    request_input = {
        "schema": _CACHE_SCHEMA_VERSION,
        "kind": "batch_rerank",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "market": market_filter,
        "model": selected_model,
        "strategies": active_payloads,
        "blend_alpha": active_alphas,
    }
    cache_key = _batch_cache_key(request_input)
    total_review_candidates = sum(int(payload.get("review_limit") or 0) for payload in active_payloads)
    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        entry = cache.get(cache_key)
        if _cache_entry_valid(entry, int(config["cache_ttl_seconds"])):
            parsed = entry.get("parsed") if isinstance(entry, dict) else {}
            results_by_strategy = _extract_batch_results(parsed, active_rows.keys())
            if results_by_strategy:
                for strategy_name, rows in active_rows.items():
                    results = results_by_strategy.get(strategy_name)
                    if results is None:
                        meta_by_strategy[strategy_name] = {
                            "enabled": True,
                            "status": "no_strategy_result",
                            "strategy": strategy_name,
                            "source": "deepseek_cache",
                        }
                        continue
                    merged, coverage = _merge_ranking_rows(rows, results, active_alphas[strategy_name], strategy_name)
                    review_count = active_review_limits.get(strategy_name, min(batch_limit, len(rows)))
                    allocation_ratio = review_count / max(1, total_review_candidates)
                    cost_hint = _usage_cost_hint(
                        entry.get("usage", {}),
                        selected_model,
                        model_tier,
                        cached=True,
                        allocation_ratio=allocation_ratio,
                    )
                    call_id = "{}:{}".format(cache_key[:12], strategy_name)
                    _attach_deepseek_cost_metadata(merged, cost_hint, call_id, "deepseek_batch_cache")
                    _attach_deepseek_cost_metadata(
                        coverage.get("filtered_rows", []),
                        cost_hint,
                        call_id,
                        "deepseek_batch_cache",
                    )
                    result_rows[strategy_name] = merged
                    meta_by_strategy[strategy_name] = _rerank_meta_from_coverage(
                        strategy_name,
                        len(rows),
                        review_count,
                        coverage,
                        source="deepseek_batch_cache",
                        model=selected_model,
                        model_tier=model_tier,
                        blend_alpha=active_alphas[strategy_name],
                        cache_key=cache_key[:12],
                        cached_at=entry.get("cached_at"),
                        usage=entry.get("usage", {}),
                        cost_hint=cost_hint,
                        cascade_filter=active_cascade.get(strategy_name),
                    )
                return result_rows, meta_by_strategy

    total_review_candidates = sum(int(payload.get("review_limit") or 0) for payload in active_payloads)
    messages = _build_batch_messages(request_input)
    payload = {
        "model": selected_model,
        "messages": messages,
        "temperature": 0.12,
        "max_tokens": _batch_max_tokens(total_review_candidates, int(config["max_tokens"])),
        "response_format": {"type": "json_object"},
    }
    url = _deepseek_chat_url(config["base_url"])
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    http_result = _DEEPSEEK_HTTP_CLIENT.post_json(
        url,
        headers=headers,
        payload=payload,
        timeout=float(config["timeout_seconds"]),
        retry_count=int(config["retry_count"]),
        retry_base_delay=float(config["retry_base_delay"]),
        parse_content=_safe_parse_json,
    )
    parsed = http_result.parsed
    usage = http_result.usage
    last_error = http_result.error
    attempt = http_result.attempts

    if parsed is None:
        for strategy_name, rows in active_rows.items():
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "fallback",
                "strategy": strategy_name,
                "source": "deepseek_batch",
                "error": last_error,
                "requested": len(rows),
                "review_limit": active_review_limits.get(strategy_name, min(batch_limit, len(rows))),
                "cascade_filter": active_cascade.get(strategy_name),
                "review_policy": active_cascade.get(strategy_name),
                **_deepseek_efficiency_meta(
                    len(rows),
                    active_review_limits.get(strategy_name, min(batch_limit, len(rows))),
                    {},
                    active_cascade.get(strategy_name),
                ),
            }
        return result_rows, meta_by_strategy

    if config.get("cache_enabled", True):
        cache = _read_cache(str(config["cache_path"]))
        cache[cache_key] = {
            "schema": _CACHE_SCHEMA_VERSION,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "cached_at": time.time(),
            "kind": "batch_rerank",
            "market": market_filter,
            "model": selected_model,
            "model_tier": model_tier,
            "parsed": parsed,
            "usage": usage,
            "cost_hint": _usage_cost_hint(usage, selected_model, model_tier, cached=False),
        }
        _write_cache(str(config["cache_path"]), cache)

    results_by_strategy = _extract_batch_results(parsed, active_rows.keys())
    for strategy_name, rows in active_rows.items():
        results = results_by_strategy.get(strategy_name)
        if results is None:
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "no_strategy_result",
                "strategy": strategy_name,
                "source": "deepseek_batch",
                "requested": len(rows),
                "review_limit": active_review_limits.get(strategy_name, min(batch_limit, len(rows))),
                "cascade_filter": active_cascade.get(strategy_name),
                "review_policy": active_cascade.get(strategy_name),
                **_deepseek_efficiency_meta(
                    len(rows),
                    active_review_limits.get(strategy_name, min(batch_limit, len(rows))),
                    {},
                    active_cascade.get(strategy_name),
                ),
            }
            continue
        merged, coverage = _merge_ranking_rows(rows, results, active_alphas[strategy_name], strategy_name)
        review_count = active_review_limits.get(strategy_name, min(batch_limit, len(rows)))
        allocation_ratio = review_count / max(1, total_review_candidates)
        cost_hint = _usage_cost_hint(
            usage,
            selected_model,
            model_tier,
            cached=False,
            allocation_ratio=allocation_ratio,
        )
        call_id = "{}:{}".format(cache_key[:12], strategy_name)
        _attach_deepseek_cost_metadata(merged, cost_hint, call_id, "deepseek_batch")
        _attach_deepseek_cost_metadata(coverage.get("filtered_rows", []), cost_hint, call_id, "deepseek_batch")
        result_rows[strategy_name] = merged
        meta_by_strategy[strategy_name] = _rerank_meta_from_coverage(
            strategy_name,
            len(rows),
            review_count,
            coverage,
            source="deepseek_batch",
            model=selected_model,
            model_tier=model_tier,
            blend_alpha=active_alphas[strategy_name],
            cache_key=cache_key[:12],
            usage=usage,
            cost_hint=cost_hint,
            attempts=attempt,
            cascade_filter=active_cascade.get(strategy_name),
        )
    return result_rows, meta_by_strategy


def _rerank_meta_from_coverage(
    strategy_name: str,
    total: int,
    review_limit: int,
    coverage: Dict[str, object],
    *,
    source: str,
    model: str,
    model_tier: str,
    blend_alpha: float,
    cache_key: str,
    cached_at=None,
    usage=None,
    cost_hint=None,
    attempts=None,
    cascade_filter=None,
) -> Dict[str, object]:
    meta = {
        "enabled": True,
        "status": "cache_hit" if "cache" in source else "ok",
        "strategy": strategy_name,
        "requested": total,
        "review_limit": review_limit,
        "covered": coverage.get("covered", 0),
        "filtered": coverage.get("filtered", 0),
        "filtered_codes": coverage.get("filtered_codes", []),
        "filtered_rows": coverage.get("filtered_rows", []),
        "filter_reasons": coverage.get("filter_reasons", {}),
        "source": source,
        "model": model,
        "model_tier": model_tier,
        "base_model": model,
        "blend_alpha": blend_alpha,
        "cache_key": cache_key,
        "usage": usage or {},
        "cost_hint": cost_hint or _usage_cost_hint(usage or {}, model, model_tier, cached="cache" in source),
    }
    if cached_at is not None:
        meta["cached_at"] = cached_at
    if attempts is not None:
        meta["attempts"] = attempts
    if cascade_filter is not None:
        meta["cascade_filter"] = cascade_filter
    meta.update(_deepseek_efficiency_meta(total, review_limit, meta["cost_hint"], cascade_filter))
    return meta


def _batch_cache_key(request_input: Dict[str, object]) -> str:
    raw = json.dumps(request_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_batch_messages(request_input: Dict[str, object]) -> List[Dict[str, str]]:
    return _PAYLOAD_BUILDER.build_batch_messages(request_input)


def _extract_batch_results(parsed: object, strategies) -> Dict[str, List[Dict[str, object]]]:
    strategy_set = {storage_strategy_name(strategy) for strategy in strategies}
    data = parsed if isinstance(parsed, dict) else {}
    result: Dict[str, List[Dict[str, object]]] = {}
    grouped = data.get("strategies") if isinstance(data, dict) else {}
    if isinstance(grouped, dict):
        for strategy, payload in grouped.items():
            normalized = storage_strategy_name(strategy)
            if normalized not in strategy_set:
                continue
            if isinstance(payload, dict):
                result[normalized] = _extract_results(payload)
            elif isinstance(payload, list):
                result[normalized] = [item for item in payload if isinstance(item, dict)]
    flat = data.get("results") if isinstance(data, dict) else []
    if isinstance(flat, list):
        for item in flat:
            if not isinstance(item, dict):
                continue
            normalized = storage_strategy_name(str(item.get("strategy") or item.get("strategy_name") or ""))
            if normalized not in strategy_set:
                continue
            next_item = dict(item)
            next_item.pop("strategy", None)
            next_item.pop("strategy_name", None)
            result.setdefault(normalized, []).append(next_item)
    return result


def _coerce_market_gate_result(parsed: object) -> Dict[str, object]:
    return _MARKET_GATE_REVIEW_SERVICE.coerce_result(parsed)


def _local_market_gate(context: Dict[str, object]) -> Dict[str, object]:
    return _MARKET_GATE_REVIEW_SERVICE.local_gate(context)


def _local_market_gate_decisive(result: Dict[str, object]) -> bool:
    return _MARKET_GATE_REVIEW_SERVICE.local_gate_decisive(result)


def _validation_sample_payload(samples: List[Dict[str, object]], limit: int = 8) -> Dict[str, object]:
    return _VALIDATION_REVIEW_SERVICE.sample_payload(samples, limit)


_RERANK_SERVICE = RerankService(_rerank_candidates_impl)
_BATCH_RERANK_SERVICE = BatchRerankService(_rerank_candidates_batch_impl)
_MARKET_GATE_REVIEW_SERVICE = MarketGateReviewService(
    config_module=config,
    runtime_config=_runtime_config,
    http_client=_DEEPSEEK_HTTP_CLIENT,
    cache_schema_version=_CACHE_SCHEMA_VERSION,
    read_cache=_read_cache,
    write_cache=_write_cache,
    chat_url=_deepseek_chat_url,
    parse_json=_safe_parse_json,
    cost_hint=_usage_cost_hint,
    clamp=_clamp,
)
_VALIDATION_REVIEW_SERVICE = ValidationReviewService(
    runtime_config=_runtime_config,
    supported_strategies=_SUPPORTED_STRATEGIES,
    strategy_context=_strategy_context,
    cache_schema_version=_CACHE_SCHEMA_VERSION,
    read_cache=_read_cache,
    write_cache=_write_cache,
    cache_entry_valid=_cache_entry_valid,
    chat_url=_deepseek_chat_url,
    parse_json=_safe_parse_json,
    http_client=_DEEPSEEK_HTTP_CLIENT,
    cost_hint=_usage_cost_hint,
    loss_factors=_NEXT_DAY_LOSS_FACTORS,
    profit_factors=_NEXT_DAY_PROFIT_FACTORS,
)


def rerank_candidates(*args, **kwargs):
    return _RERANK_SERVICE.rerank_candidates(*args, **kwargs)


def rerank_candidates_batch(*args, **kwargs):
    return _BATCH_RERANK_SERVICE.rerank_candidates_batch(*args, **kwargs)


def review_market_regime(*args, **kwargs):
    return _MARKET_GATE_REVIEW_SERVICE.review_market_regime(*args, **kwargs)


def review_strategy_validation(*args, **kwargs):
    return _VALIDATION_REVIEW_SERVICE.review_strategy_validation(*args, **kwargs)

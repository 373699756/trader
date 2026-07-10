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
from .deepseek_rules import rule_penalty_for_row
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


_ANNOUNCEMENT_KEYWORDS = (
    "业绩预告",
    "业绩预增",
    "业绩预亏",
    "减持",
    "增持",
    "解禁",
    "问询函",
    "监管函",
    "质押",
    "立案",
    "处罚",
    "诉讼",
    "并购",
    "重组",
    "中标",
    "订单",
)


def _news_context_enabled() -> bool:
    return bool(getattr(config, "ENABLE_DEEPSEEK_NEWS_CONTEXT", False))


def _attach_news_context(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    if not rows or not _news_context_enabled():
        return rows
    try:
        from .providers import MarketDataProvider
        from .sentiment import score_news_items

        provider = MarketDataProvider()
        cache = _read_news_cache()
        limit = max(1, int(getattr(config, "DEEPSEEK_NEWS_CONTEXT_LIMIT", 6)))
        enriched: List[Dict[str, object]] = []
        cache, changed = _prune_news_cache(cache)
        for row in rows:
            item = dict(row)
            code = normalize_code(item.get("code"))
            if not code:
                enriched.append(item)
                continue
            cached = _cached_news_context(cache.get(code), limit)
            if cached is None:
                try:
                    news_items = provider.get_stock_news(code, name=str(item.get("name") or ""), limit=limit)
                except Exception as exc:
                    news_items = []
                    item["news_context_status"] = "error"
                    item["news_context_error"] = str(exc)[:120]
                scored = score_news_items(news_items)
                cached = {
                    "fetched_at": time.time(),
                    "recent_news": _compact_news_items(scored.get("items") or news_items, limit),
                    "news_sentiment": _compact_news_sentiment(scored),
                }
                cache[code] = cached
                changed = True
            item["recent_news"] = cached.get("recent_news", [])
            item["news_sentiment"] = cached.get("news_sentiment", {})
            item["announcement_flags"] = _announcement_flags(item, item.get("recent_news") or [], item.get("news_sentiment") or {})
            item["news_context_status"] = item.get("news_context_status") or "ok"
            enriched.append(item)
        if changed:
            _write_news_cache(cache)
        return enriched
    except Exception:
        return rows


def _cached_news_context(entry: object, limit: int):
    if not isinstance(entry, dict):
        return None
    fetched_at = coerce_number(entry.get("fetched_at"), 0.0)
    max_age = max(0, int(getattr(config, "NEWS_CACHE_HOURS", 6))) * 3600
    if max_age > 0 and (time.time() - fetched_at) > max_age:
        return None
    return {
        "fetched_at": fetched_at,
        "recent_news": list(entry.get("recent_news") or [])[:limit],
        "news_sentiment": dict(entry.get("news_sentiment") or {}),
    }


def _prune_news_cache(cache: Dict[str, object]) -> Tuple[Dict[str, object], bool]:
    if not isinstance(cache, dict) or not cache:
        return {}, False
    now = time.time()
    max_age = max(0, int(getattr(config, "NEWS_CACHE_HOURS", 6))) * 3600
    max_entries = max(0, int(getattr(config, "DEEPSEEK_NEWS_CACHE_MAX_ENTRIES", 1000)))
    items = []
    changed = False
    for code, entry in cache.items():
        if not isinstance(entry, dict):
            changed = True
            continue
        fetched_at = coerce_number(entry.get("fetched_at"), 0.0)
        if max_age > 0 and fetched_at > 0 and now - fetched_at > max_age:
            changed = True
            continue
        items.append((str(code), entry, fetched_at))
    if max_entries > 0 and len(items) > max_entries:
        items.sort(key=lambda item: item[2], reverse=True)
        items = items[:max_entries]
        changed = True
    return {code: entry for code, entry, _ in items}, changed


def _compact_news_items(items: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
    compact = []
    for item in items[:limit]:
        title = str(item.get("title") or item.get("content") or "").strip()
        if not title:
            continue
        compact.append(
            {
                "title": title[:60],
                "source": str(item.get("source") or "")[:20],
                "publish_time": str(item.get("publish_time") or item.get("time") or "")[:32],
                "trigger_words": list(item.get("trigger_words") or [])[:6],
            }
        )
    return compact


def _compact_news_sentiment(scored: Dict[str, object]) -> Dict[str, object]:
    return {
        "score": coerce_number(scored.get("score"), 50.0),
        "summary": str(scored.get("summary") or "")[:120],
        "trigger_words": list(scored.get("trigger_words") or [])[:8],
        "risk_words": list(scored.get("risk_words") or [])[:8],
    }


def _announcement_flags(row: Dict[str, object], news_items: List[Dict[str, object]], sentiment: Dict[str, object]) -> List[str]:
    flags: List[object] = []
    raw_event_flags = row.get("event_risk_flags") or []
    if isinstance(raw_event_flags, list):
        for flag in raw_event_flags:
            if isinstance(flag, dict):
                flags.append(flag.get("label"))
            else:
                flags.append(flag)
    flags.extend(sentiment.get("risk_words") or [])
    for news in news_items or []:
        title = str(news.get("title") or "")
        for keyword in _ANNOUNCEMENT_KEYWORDS:
            if keyword in title:
                flags.append(keyword)
    return _unique_strings(flags)[:10]


def _read_news_cache() -> Dict[str, object]:
    path = str(getattr(config, "DEEPSEEK_NEWS_CACHE_PATH", ".runtime/deepseek_news_context.json") or "")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_news_cache(cache: Dict[str, object]) -> None:
    path = str(getattr(config, "DEEPSEEK_NEWS_CACHE_PATH", ".runtime/deepseek_news_context.json") or "")
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        return


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


def _payload_number(value, default: float = 0.0, digits: int = 1):
    number = coerce_number(value, default)
    if abs(number) >= 1000000:
        return int(round(number))
    if digits <= 0:
        return int(round(number))
    return round(number, digits)


def _payload_strings(values, limit: int = 4) -> List[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text[:40])
        if len(result) >= limit:
            break
    return result


def _payload_news(items) -> List[Dict[str, object]]:
    compact = []
    for item in (items or [])[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("content") or "").strip()[:60]
        if not title:
            continue
        compact.append(
            {
                "title": title,
                "source": str(item.get("source") or "")[:20],
                "time": str(item.get("publish_time") or item.get("time") or "")[:16],
            }
        )
    return compact


def _payload_news_sentiment(payload) -> Dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    return {
        "score": _payload_number(payload.get("score"), 50.0, 0),
        "risk_words": _payload_strings(payload.get("risk_words"), 4),
        "trigger_words": _payload_strings(payload.get("trigger_words"), 4),
    }


def _request_payload(strategy_name: str, candidates: List[Dict[str, object]], market_filter: str) -> List[Dict[str, object]]:
    return [
        {
            "code": row.get("code", ""),
            "name": row.get("name", ""),
            "score": _payload_number(row.get("score"), 0.0, 0),
            "pct_chg": _payload_number(row.get("pct_chg"), 0.0, 1),
            "speed": _payload_number(row.get("speed"), 0.0, 1),
            "volume_ratio": _payload_number(row.get("volume_ratio"), 0.0, 1),
            "turnover_rate": _payload_number(row.get("turnover_rate"), 0.0, 1),
            "turnover": _payload_number(row.get("turnover"), 0.0, 0),
            "amplitude": _payload_number(row.get("amplitude"), 0.0, 1),
            "sixty_day_pct": _payload_number(row.get("sixty_day_pct"), 0.0, 1),
            "ret_5d": _payload_number(row.get("ret_5d"), 0.0, 1),
            "ret_10d": _payload_number(row.get("ret_10d"), 0.0, 1),
            "ret_20d": _payload_number(row.get("ret_20d"), 0.0, 1),
            "ma20_gap": _payload_number(row.get("ma20_gap"), 0.0, 1),
            "vol_amount_5d": _payload_number(row.get("vol_amount_5d"), 0.0, 1),
            "breakout_20d": bool(row.get("breakout_20d")),
            "volatility_20d": _payload_number(row.get("volatility_20d"), 0.0, 1),
            "liquidity_score": _payload_number(row.get("liquidity_score"), 0.0, 0),
            "momentum_score": _payload_number(row.get("momentum_score"), 0.0, 0),
            "trend_score": _payload_number(row.get("trend_score"), 0.0, 0),
            "historical_edge_score": _payload_number(row.get("historical_edge_score"), 0.0, 0),
            "execution_score": _payload_number(row.get("execution_score"), 0.0, 0),
            "tail_setup_score": _payload_number(row.get("tail_setup_score"), 0.0, 0),
            "risk_penalty": _payload_number(row.get("risk_penalty"), 0.0, 0),
            "risk_penalty_parts": row.get("risk_penalty_parts", {}),
            "overheat_damp": _payload_number(row.get("overheat_damp"), 1.0, 2),
            "failure_reasons": _payload_strings(row.get("failure_reasons"), 3),
            "market": str(row.get("market", "")),
            "theme": str(row.get("theme", "")),
            "reasons": _payload_strings(row.get("reasons"), 4),
            "recent_news": _payload_news(row.get("recent_news")),
            "announcement_flags": _payload_strings(row.get("announcement_flags"), 5),
            "news_sentiment": _payload_news_sentiment(row.get("news_sentiment")),
        }
        for row in candidates
    ]


def _row_has_llm_edge_context(row: Dict[str, object]) -> bool:
    if row.get("recent_news") or row.get("announcement_flags"):
        return True
    news_sentiment = row.get("news_sentiment") if isinstance(row.get("news_sentiment"), dict) else {}
    if news_sentiment.get("risk_words") or news_sentiment.get("trigger_words"):
        return True
    if coerce_number(news_sentiment.get("score"), 50.0) <= 45 or coerce_number(news_sentiment.get("score"), 50.0) >= 62:
        return True
    event_risk = row.get("event_risk") if isinstance(row.get("event_risk"), dict) else {}
    blacklist_risk = row.get("blacklist_risk") if isinstance(row.get("blacklist_risk"), dict) else {}
    if event_risk.get("flags") or blacklist_risk.get("flags"):
        return True
    if row.get("event_risk_flags") or row.get("risk_words"):
        return True
    return False


def _row_is_llm_ambiguous(row: Dict[str, object]) -> bool:
    score = coerce_number(row.get("score"), 0.0)
    risk_penalty = coerce_number(row.get("risk_penalty"), 0.0)
    overheat_damp = coerce_number(row.get("overheat_damp"), 1.0)
    if score < 45:
        return False
    if 55 <= score <= 88:
        return True
    if risk_penalty >= 8 or overheat_damp < 0.88:
        return score <= 94
    return False


def _select_batch_review_pool(rows: List[Dict[str, object]], batch_limit: int, config: Dict[str, object]) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    rows = [dict(row) for row in rows[: min(batch_limit, len(rows))] if isinstance(row, dict)]
    if not rows or not config.get("cascade_filter_enabled", True):
        return rows, {"enabled": False, "input_count": len(rows), "selected_count": len(rows)}
    max_review = min(len(rows), int(config.get("cascade_max_review") or 8), int(batch_limit))
    selected = [row for row in rows if _row_has_llm_edge_context(row) and _row_is_llm_ambiguous(row)]
    selected = selected[:max_review]
    skipped = max(0, len(rows) - len(selected))
    return selected, {
        "enabled": True,
        "input_count": len(rows),
        "selected_count": len(selected),
        "skipped_local_confident": skipped,
        "max_review": max_review,
    }


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
                "主题类策略必须判断 theme_truth_score；如果 recent_news 没有具体标题依据，必须视为题材待证实并降低 theme_truth_score。\n"
                "announcement_flags/news_sentiment 是真实新闻与事件输入，减持、解禁、质押、问询函、监管函等风险命中时提高 event_risk_score 和 penalty。\n"
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
                    pool=json.dumps(
                        _request_payload(strategy_name, candidates, market_filter),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
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

    local_order = sorted(
        enumerate(rows),
        key=lambda pair: (
            -coerce_number(pair[1].get("score"), 0.0),
            int(coerce_number(pair[1].get("rank"), pair[0] + 1) or pair[0] + 1),
            pair[0],
        ),
    )
    local_rank_by_index = {original_index: rank for rank, (original_index, _) in enumerate(local_order, start=1)}

    merged: List[Dict[str, object]] = []
    for local_index, row in enumerate(rows, start=1):
        next_row = dict(row)
        factor_review = _next_day_factor_review(next_row)
        code = normalize_code(str(next_row.get("code", "")).strip())
        base_score = coerce_number(next_row.get("score"), 0.0)
        if next_row.get("deepseek_rule_penalty") is not None and next_row.get("deepseek_rules_matched") is not None:
            rule_penalty = 0.0
            matched_rules = list(next_row.get("deepseek_rules_matched") or [])
        else:
            rule_penalty, matched_rules = rule_penalty_for_row(strategy_name, next_row)
        llm_item = llm_by_code.get(code)
        # P0 归因：记录 rerank 前的本地名次与本次混合参数，随 raw_json 落库，
        # 供 strategy_validation.deepseek_attribution 做反事实排序增益与 alpha 自适应。
        local_rank = local_rank_by_index.get(local_index - 1, local_index)
        next_row["local_rank"] = local_rank
        next_row["deepseek_covered"] = bool(llm_item)
        next_row["deepseek_blend_alpha"] = round(float(blend_alpha), 4)
        next_row["blend_alpha"] = round(float(blend_alpha), 4)
        next_row["deepseek_rule_penalty"] = coerce_number(next_row.get("deepseek_rule_penalty"), 0.0) + rule_penalty
        next_row["deepseek_rules_matched"] = matched_rules
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
            combined = round((1.0 - blend_alpha) * base_score + blend_alpha * up_score - penalty - rule_penalty, 2)
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
            next_row["deepseek_rank_score"] = round(
                base_score - coerce_number(factor_review.get("penalty"), 0.0) - rule_penalty,
                2,
            )
        is_observation = (
            next_row.get("tier") == "backup_pool"
            or next_row.get("observation_mode") == "intraday_provisional"
        )
        if is_observation and next_row.get("deepseek_action") != "avoid":
            next_row["deepseek_action"] = "watch"
            observation_label = "盘中候选" if next_row.get("observation_mode") == "intraday_provisional" else "备选候选"
            next_row["deepseek_reason"] = "{}仅观察；{}".format(
                observation_label,
                str(next_row.get("deepseek_reason") or "等待14:30后确认")
            )
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
        "filtered_rows": _compact_filtered_rows(filtered),
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
        "rerank_source",
    )
    result = []
    for row in rows:
        item = {key: row.get(key) for key in keys if key in row}
        item["deepseek_shadow_signal"] = True
        result.append(item)
    return result


def rerank_candidates(
    rows: List[Dict[str, object]],
    strategy_name: str,
    market_filter: str = "all",
    model_tier_override: str = "",
    review_limit_override: int = 0,
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
    configured_review_limit = int(review_limit_override or config["review_limit"])
    review_limit = min(configured_review_limit, total)
    if total < 3:
        return rows, {"enabled": True, "status": "insufficient_rows", "requested": total}

    pool = [dict(row) for row in rows[:review_limit] if isinstance(row, dict)]
    if not pool:
        return rows, {"enabled": True, "status": "no_valid_pool", "requested": total}
    pool = _attach_news_context(pool)
    rows = _merge_review_context(rows, pool)

    use_pro = model_tier_override == "pro" or (
        model_tier_override not in {"base", "pro"} and str(strategy_name) in config["pro_strategies"]
    )
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
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
                return merged_rows, {
                    "enabled": True,
                    "status": "cache_hit",
                    "strategy": strategy_name,
                    "requested": total,
                    "review_limit": review_limit,
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
    merged_rows, coverage = _merge_ranking_rows(rows, results, blend_alpha, strategy_name)
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
        "cost_hint": usage,
    }
    return merged_rows, meta


def rerank_candidates_batch(
    rows_by_strategy: Dict[str, List[Dict[str, object]]],
    market_filter: str = "all",
    model_tier_override: str = "",
    review_limit_override: int = 0,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, object]]]:
    config = _coerce_env_config()
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
    configured_review_limit = int(review_limit_override or config["review_limit"])
    batch_limit = min(configured_review_limit, int(config.get("batch_review_limit") or 15))
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
        pool, cascade_meta = _select_batch_review_pool(scan_pool, batch_limit, config)
        if not pool:
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "local_confident_skipped",
                "strategy": strategy_name,
                "requested": total,
                "review_limit": 0,
                "blend_alpha": blend_alpha,
                "cascade_filter": cascade_meta,
                "reason": "候选缺少事件/新闻模糊点，本地高置信直接放行，未发送 DeepSeek。",
            }
            continue
        result_rows[strategy_name] = _merge_review_context(rows, pool)
        active_rows[strategy_name] = result_rows[strategy_name]
        active_alphas[strategy_name] = blend_alpha
        active_cascade[strategy_name] = cascade_meta
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

    use_pro = model_tier_override == "pro"
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
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
                    result_rows[strategy_name] = merged
                    meta_by_strategy[strategy_name] = _rerank_meta_from_coverage(
                        strategy_name,
                        len(rows),
                        min(batch_limit, len(rows)),
                        coverage,
                        source="deepseek_batch_cache",
                        model=selected_model,
                        model_tier=model_tier,
                        blend_alpha=active_alphas[strategy_name],
                        cache_key=cache_key[:12],
                        cached_at=entry.get("cached_at"),
                        usage=entry.get("usage", {}),
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
    parsed = None
    usage = {}
    last_error = ""
    attempt = 0
    while attempt <= int(config["retry_count"]):
        attempt += 1
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=float(config["timeout_seconds"]))
            if response.status_code in (429, 500, 502, 503, 504) and attempt <= int(config["retry_count"]):
                last_error = "可重试响应码: {}".format(response.status_code)
                time.sleep((2**(attempt - 1)) * float(config["retry_base_delay"]))
                continue
            response.raise_for_status()
            raw = response.json()
            usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
            content = ((raw.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
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
        for strategy_name, rows in active_rows.items():
            meta_by_strategy[strategy_name] = {
                "enabled": True,
                "status": "fallback",
                "strategy": strategy_name,
                "source": "deepseek_batch",
                "error": last_error,
                "requested": len(rows),
                "review_limit": min(batch_limit, len(rows)),
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
                "review_limit": min(batch_limit, len(rows)),
            }
            continue
        merged, coverage = _merge_ranking_rows(rows, results, active_alphas[strategy_name], strategy_name)
        result_rows[strategy_name] = merged
        meta_by_strategy[strategy_name] = _rerank_meta_from_coverage(
            strategy_name,
            len(rows),
            min(batch_limit, len(rows)),
            coverage,
            source="deepseek_batch",
            model=selected_model,
            model_tier=model_tier,
            blend_alpha=active_alphas[strategy_name],
            cache_key=cache_key[:12],
            usage=usage,
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
        "cost_hint": {"cached": True} if "cache" in source else (usage or {}),
    }
    if cached_at is not None:
        meta["cached_at"] = cached_at
    if attempts is not None:
        meta["attempts"] = attempts
    if cascade_filter is not None:
        meta["cascade_filter"] = cascade_filter
    return meta


def _batch_cache_key(request_input: Dict[str, object]) -> str:
    raw = json.dumps(request_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_batch_messages(request_input: Dict[str, object]) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是A股短线多策略复核器。请只输出 JSON，不要 Markdown。"
                "一次处理多个策略，必须按策略分别返回 results。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请对输入中的每个策略候选池做复核。输出格式必须是 "
                "{\"strategies\":{\"short_term\":{\"results\":[...]},\"tomorrow_picks\":{\"results\":[...]},\"swing_picks\":{\"results\":[...]}}}。"
                "每个 result 字段同单策略复核: code、llm_score、horizon_up_score、action、veto、penalty、reason、risk_flags、"
                "event_type、sentiment、catalyst_strength、time_sensitivity、already_priced_in、catalyst_score、theme_truth_score、event_risk_score。"
                "只评价输入候选，不新增股票；优先识别追高、流动性、事件风险和催化剂真实性。"
                "输入: "
                + json.dumps(request_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ),
        },
    ]


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


def review_market_regime(context: Dict[str, object]) -> Dict[str, object]:
    if not getattr(config, "ENABLE_DEEPSEEK_MARKET_GATE", False):
        return {"enabled": False, "status": "disabled"}
    local_result = _local_market_gate(context or {})
    if _local_market_gate_decisive(local_result):
        return {
            "enabled": True,
            "status": "ok",
            "source": "local_market_gate",
            "decision_path": "local_decisive",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            **local_result,
        }
    ds_config = _coerce_env_config()
    if not ds_config.get("enabled", False):
        return {"enabled": False, "status": "deepseek_disabled"}
    if ds_config["api_key"] == "":
        return {"enabled": False, "status": "missing_api_key"}
    cache_path = str(getattr(config, "DEEPSEEK_MARKET_GATE_CACHE_PATH", ".runtime/deepseek_market_gate.json"))
    cache_key = datetime.now().strftime("%Y-%m-%d")
    cache = _read_cache(cache_path)
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("schema") == _CACHE_SCHEMA_VERSION:
        return {**cached.get("result", {}), "source": "deepseek_market_gate_cache", "cache_key": cache_key}

    messages = [
        {
            "role": "system",
            "content": (
                "你是A股短线交易的大盘风控复核器。只做当天是否适合出手的风险判断，"
                "不要推荐个股。必须输出JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请基于以下市场上下文判断今日短线推荐是否需要收缩。"
                "输出字段: regime(risk_on/balanced/risk_off), size_factor(0-1), confidence(0-100), reason。"
                "risk_off 表示建议明显减少推荐数量；balanced 表示轻微收缩或正常；risk_on 表示正常展示。"
                "上下文: {context}"
            ).format(context=json.dumps(context or {}, ensure_ascii=False, sort_keys=True)),
        },
    ]
    payload = {
        "model": ds_config["model"],
        "messages": messages,
        "temperature": 0.05,
        "max_tokens": min(max(120, int(ds_config.get("max_tokens") or 800)), 500),
        "response_format": {"type": "json_object"},
    }
    try:
        response = requests.post(
            _deepseek_chat_url(str(ds_config["base_url"])),
            headers={
                "Authorization": f"Bearer {ds_config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=float(ds_config["timeout_seconds"]),
        )
        response.raise_for_status()
        raw = response.json()
        content = ((raw.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
        parsed = _safe_parse_json(str(content)) or {}
        result = _coerce_market_gate_result(parsed)
        result.update(
            {
                "enabled": True,
                "status": "ok",
                "source": "deepseek_market_gate",
                "model": payload["model"],
                "usage": raw.get("usage", {}) if isinstance(raw, dict) else {},
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        cache[cache_key] = {"schema": _CACHE_SCHEMA_VERSION, "date": cache_key, "result": result}
        _write_cache(cache_path, cache)
        return result
    except Exception as exc:
        return {"enabled": True, "status": "fallback", "error": str(exc), **local_result}


def _coerce_market_gate_result(parsed: object) -> Dict[str, object]:
    data = parsed if isinstance(parsed, dict) else {}
    regime = str(data.get("regime") or data.get("market_regime") or "balanced").strip().lower()
    if regime not in {"risk_on", "balanced", "risk_off"}:
        regime = "balanced"
    default_factor = 1.0 if regime == "risk_on" else 0.7 if regime == "balanced" else 0.4
    min_factor = max(0.0, min(1.0, coerce_number(getattr(config, "DEEPSEEK_MARKET_GATE_MIN_SIZE_FACTOR", 0.25), 0.25)))
    size_factor = _clamp(coerce_number(data.get("size_factor"), default_factor), min_factor, 1.0)
    return {
        "regime": regime,
        "size_factor": round(size_factor, 3),
        "confidence": round(_clamp(coerce_number(data.get("confidence"), 50.0), 0.0, 100.0), 2),
        "reason": str(data.get("reason") or data.get("summary") or "")[:240],
    }


def _local_market_gate(context: Dict[str, object]) -> Dict[str, object]:
    up_ratio = coerce_number(context.get("up_ratio_pct"), 50.0)
    limit_up_count = coerce_number(context.get("limit_up_count"), 0.0)
    avg_pct = coerce_number(context.get("avg_pct_chg"), 0.0)
    if up_ratio < 35 or avg_pct < -1.2:
        regime = "risk_off"
        factor = coerce_number(getattr(config, "PORTFOLIO_GROSS_RISK_OFF", 0.4), 0.4)
        reason = "本地大盘宽度偏弱，自动收缩推荐数量。"
    elif up_ratio > 58 and limit_up_count >= 20 and avg_pct > 0.5:
        regime = "risk_on"
        factor = coerce_number(getattr(config, "PORTFOLIO_GROSS_RISK_ON", 1.0), 1.0)
        reason = "本地大盘宽度较强，维持推荐数量。"
    else:
        regime = "balanced"
        factor = coerce_number(getattr(config, "PORTFOLIO_GROSS_BALANCED", 0.7), 0.7)
        reason = "本地大盘中性，轻微收缩推荐数量。"
    min_factor = max(0.0, min(1.0, coerce_number(getattr(config, "DEEPSEEK_MARKET_GATE_MIN_SIZE_FACTOR", 0.25), 0.25)))
    return {
        "regime": regime,
        "size_factor": round(_clamp(factor, min_factor, 1.0), 3),
        "confidence": 45.0,
        "reason": reason,
        "source": "local_market_gate",
    }


def _local_market_gate_decisive(result: Dict[str, object]) -> bool:
    return str((result or {}).get("regime") or "").strip().lower() in {"risk_on", "risk_off"}


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

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import time
from typing import Dict

import requests

from .deepseek_client import (
    _CACHE_SCHEMA_VERSION,
    _NEXT_DAY_LOSS_FACTORS,
    _NEXT_DAY_PROFIT_FACTORS,
    _cache_entry_valid,
    _coerce_env_config,
    _deepseek_chat_url,
    _read_cache,
    _safe_parse_json,
    _strategy_context,
    _unique_strings,
    _write_cache,
)
from .strategies.types import storage_strategy_name


def review_stock_prediction(
    prediction_payload: Dict[str, object],
    strategy_name: str = "short_term",
) -> Dict[str, object]:
    strategy_name = storage_strategy_name(strategy_name or "short_term")
    config = _coerce_env_config()
    if not config.get("enabled", False):
        return {"enabled": False, "status": "disabled"}
    if config["api_key"] == "":
        return {"enabled": False, "status": "missing_api_key"}
    if config["strategies"] and strategy_name not in config["strategies"]:
        return {"enabled": False, "status": "strategy_not_enabled", "strategy": strategy_name}

    use_pro = str(strategy_name) in config["pro_strategies"]
    selected_model = str(config["pro_model"] if use_pro else config["model"])
    model_tier = "pro" if use_pro else "base"
    context = _strategy_context(strategy_name)
    compact_payload = _stock_prediction_review_payload(prediction_payload, strategy_name, context)
    raw_key = json.dumps(compact_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
                "你是A股个股交易策略优化助手。请只输出 JSON，不要 Markdown。"
                "输出字段: summary、stance、bias、timing、reasoning、entry_plan、risk_controls、strategy_adjustments、avoid_conditions。"
                "entry_plan、risk_controls、strategy_adjustments、avoid_conditions 都必须是字符串数组。"
                "stance 只能是 buy_trial、watch_only、hold_or_wait、avoid_chase。"
                "bias 只能是 bullish、neutral、bearish。timing 只能是 now、pullback、breakout_confirm、observe。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请基于本地量化预测结果，对这只股票给出更实用的策略优化建议。"
                "重点不是重复涨跌判断，而是回答现在该怎么做、怎么试单、什么情况下不能追。"
                "策略周期: {horizon}。复核重点: {focus}。"
                "必须结合以下亏钱因素: {loss_factors}。"
                "同时参考赚钱因素: {profit_factors}。"
                "不要建议重仓，不要承诺收益。若风险偏高，优先给 watch_only 或 avoid_chase。"
                "strategy_adjustments 请写成具体执行建议，例如“小仓试单”“回踩再看”“突破确认再跟”“跌破某类结构撤退”。"
                "输入: {payload}"
            ).format(
                horizon=context["horizon"],
                focus=context["focus"],
                loss_factors="；".join(_NEXT_DAY_LOSS_FACTORS),
                profit_factors="；".join(_NEXT_DAY_PROFIT_FACTORS),
                payload=json.dumps(compact_payload, ensure_ascii=False),
            ),
        },
    ]
    payload = {
        "model": selected_model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max(500, int(config["max_tokens"])),
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
                time.sleep((2 ** (attempt - 1)) * float(config["retry_base_delay"]))
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
                time.sleep((2 ** (attempt - 1)) * float(config["retry_base_delay"]))

    if not isinstance(parsed, dict):
        return {
            "enabled": True,
            "status": "timeout" if timed_out else "fallback",
            "strategy": strategy_name,
            "error": last_error,
        }

    normalized = {
        "summary": str(parsed.get("summary", "")).strip(),
        "stance": str(parsed.get("stance", "")).strip().lower(),
        "bias": str(parsed.get("bias", "")).strip().lower(),
        "timing": str(parsed.get("timing", "")).strip().lower(),
        "reasoning": _unique_strings(parsed.get("reasoning", []))[:4] if isinstance(parsed.get("reasoning"), list) else [],
        "entry_plan": _unique_strings(parsed.get("entry_plan", []))[:4] if isinstance(parsed.get("entry_plan"), list) else [],
        "risk_controls": _unique_strings(parsed.get("risk_controls", []))[:4] if isinstance(parsed.get("risk_controls"), list) else [],
        "strategy_adjustments": _unique_strings(parsed.get("strategy_adjustments", []))[:5] if isinstance(parsed.get("strategy_adjustments"), list) else [],
        "avoid_conditions": _unique_strings(parsed.get("avoid_conditions", []))[:4] if isinstance(parsed.get("avoid_conditions"), list) else [],
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
            "parsed": normalized,
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
        **normalized,
    }


def _stock_prediction_review_payload(
    prediction_payload: Dict[str, object],
    strategy_name: str,
    context: Dict[str, str],
) -> Dict[str, object]:
    prediction = prediction_payload.get("prediction") or {}
    short_horizon = (prediction_payload.get("horizons") or {}).get("short") or {}
    market_regime = prediction_payload.get("market_regime") or {}
    hits = prediction_payload.get("strategy_hits") or []
    missed = prediction_payload.get("missed_strategies") or []
    return {
        "schema": _CACHE_SCHEMA_VERSION,
        "kind": "stock_prediction_review",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strategy": strategy_name,
        "horizon": context["horizon"],
        "focus": context["focus"],
        "code": prediction_payload.get("code", ""),
        "name": prediction_payload.get("name", ""),
        "market": prediction_payload.get("market", ""),
        "market_label": prediction_payload.get("market_label", ""),
        "price": prediction_payload.get("price"),
        "pct_chg": prediction_payload.get("pct_chg"),
        "turnover": prediction_payload.get("turnover"),
        "volume_ratio": prediction_payload.get("volume_ratio"),
        "sixty_day_pct": prediction_payload.get("sixty_day_pct"),
        "ytd_pct": prediction_payload.get("ytd_pct"),
        "data_source": prediction_payload.get("data_source", ""),
        "prediction": {
            "label": prediction.get("label", ""),
            "direction": prediction.get("direction", ""),
            "score": prediction.get("score"),
            "confidence": prediction.get("confidence"),
            "risk_level": prediction.get("risk_level", ""),
            "advice": prediction.get("advice", ""),
        },
        "short_horizon": {
            "label": short_horizon.get("label", ""),
            "prediction": (short_horizon.get("prediction") or {}).get("label", ""),
            "score": (short_horizon.get("prediction") or {}).get("score"),
            "confidence": (short_horizon.get("prediction") or {}).get("confidence"),
        },
        "market_regime": {
            "label": market_regime.get("label", ""),
            "score": market_regime.get("score"),
            "advice": market_regime.get("advice", ""),
        },
        "risk_flags": prediction_payload.get("risk_flags") or [],
        "strategy_hits": [
            {
                "strategy_name": item.get("strategy_name", ""),
                "strategy_label": item.get("strategy_label", ""),
                "horizon_label": item.get("horizon_label", ""),
                "rank": item.get("rank"),
                "score": item.get("score"),
                "direction_score": item.get("direction_score"),
                "risk_score": item.get("risk_score"),
                "action": item.get("action", ""),
                "reasons": (item.get("reasons") or [])[:3],
                "failure_reasons": (item.get("failure_reasons") or [])[:2],
            }
            for item in hits[:4]
            if isinstance(item, dict)
        ],
        "missed_strategies": [
            {
                "strategy_label": item.get("strategy_label", ""),
                "horizon_label": item.get("horizon_label", ""),
                "reason": item.get("reason", ""),
            }
            for item in missed[:3]
            if isinstance(item, dict)
        ],
    }

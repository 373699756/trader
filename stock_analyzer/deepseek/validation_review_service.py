from __future__ import annotations

from datetime import datetime
import hashlib
import json
import time
from typing import Callable, Dict, Iterable, List

from ..normalization import coerce_number
from ..strategies.types import storage_strategy_name


class ValidationReviewService:
    """DeepSeek strategy-validation review orchestration."""

    def __init__(
        self,
        *,
        runtime_config: Callable[[], Dict[str, object]],
        supported_strategies: Iterable[str],
        strategy_context: Callable[[str], Dict[str, str]],
        cache_schema_version: int,
        read_cache: Callable[[str], Dict[str, object]],
        write_cache: Callable[[str, Dict[str, object]], None],
        cache_entry_valid: Callable[[Dict[str, object], int], bool],
        chat_url: Callable[[str], str],
        parse_json: Callable[[str], object],
        http_client,
        cost_hint: Callable[..., Dict[str, object]],
        loss_factors: Iterable[str],
        profit_factors: Iterable[str],
    ) -> None:
        self.runtime_config = runtime_config
        self.supported_strategies = {storage_strategy_name(strategy) for strategy in supported_strategies}
        self.strategy_context = strategy_context
        self.cache_schema_version = cache_schema_version
        self.read_cache = read_cache
        self.write_cache = write_cache
        self.cache_entry_valid = cache_entry_valid
        self.chat_url = chat_url
        self.parse_json = parse_json
        self.http_client = http_client
        self.cost_hint = cost_hint
        self.loss_factors = tuple(loss_factors)
        self.profit_factors = tuple(profit_factors)

    def sample_payload(self, samples: List[Dict[str, object]], limit: int = 8) -> Dict[str, object]:
        failed = sorted(samples, key=lambda item: coerce_number(item.get("primary_return_net"), 0.0))[:limit]
        success = sorted(
            samples,
            key=lambda item: coerce_number(item.get("primary_return_net"), 0.0),
            reverse=True,
        )[: max(3, limit // 2)]

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

        def factor_snapshot(row: Dict[str, object], raw: Dict[str, object]) -> Dict[str, object]:
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

        def case(row: Dict[str, object]) -> Dict[str, object]:
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
            factors = factor_snapshot(row, raw)
            if factors:
                item["factor_snapshot"] = factors
            return item

        return {
            "failed_cases": [case(row) for row in failed],
            "success_cases": [case(row) for row in success],
        }

    def review(
        self,
        strategy_name: str,
        metrics: Dict[str, object],
        samples: List[Dict[str, object]],
        days: int = 20,
    ) -> Dict[str, object]:
        strategy_name = storage_strategy_name(strategy_name)
        config = self.runtime_config()
        if not config.get("enabled", False):
            return {"enabled": False, "status": "disabled"}
        if config["api_key"] == "":
            return {"enabled": False, "status": "missing_api_key"}
        if strategy_name not in self.supported_strategies:
            return {"enabled": False, "status": "strategy_not_supported", "strategy": strategy_name}
        if config["strategies"] and strategy_name not in config["strategies"]:
            return {"enabled": False, "status": "strategy_not_enabled", "strategy": strategy_name}

        use_pro = str(strategy_name) in config["pro_strategies"]
        selected_model = str(config["pro_model"] if use_pro else config["model"])
        model_tier = "pro" if use_pro else "base"
        context = self.strategy_context(strategy_name)
        cases = self.sample_payload(samples)
        review_input = {
            "schema": self.cache_schema_version,
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
            cache = self.read_cache(str(config["cache_path"]))
            entry = cache.get(cache_key)
            if self.cache_entry_valid(entry, int(config["cache_ttl_seconds"])):
                parsed = entry.get("parsed") if isinstance(entry, dict) else {}
                if isinstance(parsed, dict):
                    usage = entry.get("usage", {}) if isinstance(entry, dict) else {}
                    return {
                        "enabled": True,
                        "status": "cache_hit",
                        "strategy": strategy_name,
                        "source": "deepseek_cache",
                        "model": selected_model,
                        "model_tier": model_tier,
                        "cache_key": cache_key[:12],
                        "cached_at": entry.get("cached_at"),
                        "usage": usage,
                        "cost_hint": self.cost_hint(usage, selected_model, model_tier, cached=True),
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
                    loss_factors="；".join(self.loss_factors),
                    profit_factors="；".join(self.profit_factors),
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
        retry_count = int(config.get("validation_retry_count", 0))
        timeout_seconds = min(6.0, float(config.get("validation_timeout_seconds", config["timeout_seconds"])))
        http_result = self.http_client.post_json(
            self.chat_url(config["base_url"]),
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=timeout_seconds,
            retry_count=retry_count,
            retry_base_delay=float(config["retry_base_delay"]),
            parse_content=self.parse_json,
        )
        parsed = http_result.parsed
        usage = http_result.usage
        last_error = http_result.error
        attempt = http_result.attempts
        timed_out = http_result.timed_out

        if not isinstance(parsed, dict):
            return {
                "enabled": True,
                "status": "timeout" if timed_out else "fallback",
                "strategy": strategy_name,
                "error": last_error,
            }

        if config.get("cache_enabled", True):
            cost_hint = self.cost_hint(usage, selected_model, model_tier, cached=False)
            cache = self.read_cache(str(config["cache_path"]))
            cache[cache_key] = {
                "schema": self.cache_schema_version,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "cached_at": time.time(),
                "strategy": strategy_name,
                "model": selected_model,
                "model_tier": model_tier,
                "parsed": parsed,
                "usage": usage,
                "cost_hint": cost_hint,
            }
            self.write_cache(str(config["cache_path"]), cache)
        else:
            cost_hint = self.cost_hint(usage, selected_model, model_tier, cached=False)
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
            "cost_hint": cost_hint,
            **parsed,
        }

    def review_strategy_validation(self, *args, **kwargs):
        return self.review(*args, **kwargs)

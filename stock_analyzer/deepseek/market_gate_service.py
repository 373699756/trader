from __future__ import annotations

import json
from datetime import datetime
from typing import Callable, Dict

from ..normalization import coerce_number


class MarketGateReviewService:
    """DeepSeek market-gate review orchestration."""

    def __init__(
        self,
        *,
        config_module,
        runtime_config: Callable[[], Dict[str, object]],
        http_client,
        cache_schema_version: int,
        read_cache: Callable[[str], Dict[str, object]],
        write_cache: Callable[[str, Dict[str, object]], None],
        chat_url: Callable[[str], str],
        parse_json: Callable[[str], object],
        cost_hint: Callable[..., Dict[str, object]],
        clamp: Callable[[float, float, float], float],
    ) -> None:
        self.config = config_module
        self.runtime_config = runtime_config
        self.http_client = http_client
        self.cache_schema_version = cache_schema_version
        self.read_cache = read_cache
        self.write_cache = write_cache
        self.chat_url = chat_url
        self.parse_json = parse_json
        self.cost_hint = cost_hint
        self.clamp = clamp

    def review(self, context: Dict[str, object]) -> Dict[str, object]:
        if not getattr(self.config, "ENABLE_DEEPSEEK_MARKET_GATE", False):
            return {"enabled": False, "status": "disabled"}
        local_result = self.local_gate(context or {})
        if self.local_gate_decisive(local_result):
            return {
                "enabled": True,
                "status": "ok",
                "source": "local_market_gate",
                "decision_path": "local_decisive",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                **local_result,
            }
        ds_config = self.runtime_config()
        if not ds_config.get("enabled", False):
            return {"enabled": False, "status": "deepseek_disabled"}
        if ds_config["api_key"] == "":
            return {"enabled": False, "status": "missing_api_key"}
        cache_path = str(getattr(self.config, "DEEPSEEK_MARKET_GATE_CACHE_PATH", ".runtime/deepseek_market_gate.json"))
        cache_key = datetime.now().strftime("%Y-%m-%d")
        cache = self.read_cache(cache_path)
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("schema") == self.cache_schema_version:
            result = dict(cached.get("result", {}) or {})
            usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}
            result["cost_hint"] = self.cost_hint(
                usage,
                str(result.get("model") or ds_config["model"]),
                str(result.get("model_tier") or "base"),
                cached=True,
            )
            return {**result, "source": "deepseek_market_gate_cache", "cache_key": cache_key}

        payload = {
            "model": ds_config["model"],
            "messages": self._messages(context or {}),
            "temperature": 0.05,
            "max_tokens": min(max(120, int(ds_config.get("max_tokens") or 800)), 500),
            "response_format": {"type": "json_object"},
        }
        try:
            http_result = self.http_client.post_json(
                self.chat_url(str(ds_config["base_url"])),
                headers={
                    "Authorization": f"Bearer {ds_config['api_key']}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout=float(ds_config["timeout_seconds"]),
                retry_count=0,
                retry_base_delay=0.0,
                parse_content=self.parse_json,
            )
            if http_result.parsed is None and http_result.error:
                raise RuntimeError(http_result.error)
            usage = http_result.usage
            parsed = http_result.parsed or {}
            result = self.coerce_result(parsed)
            result.update(
                {
                    "enabled": True,
                    "status": "ok",
                    "source": "deepseek_market_gate",
                    "model": payload["model"],
                    "model_tier": "base",
                    "usage": usage,
                    "cost_hint": self.cost_hint(usage, str(payload["model"]), "base", cached=False),
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            cache[cache_key] = {"schema": self.cache_schema_version, "date": cache_key, "result": result}
            self.write_cache(cache_path, cache)
            return result
        except Exception as exc:
            return {"enabled": True, "status": "fallback", "error": str(exc), **local_result}

    def review_market_regime(self, *args, **kwargs):
        return self.review(*args, **kwargs)

    def coerce_result(self, parsed: object) -> Dict[str, object]:
        data = parsed if isinstance(parsed, dict) else {}
        regime = str(data.get("regime") or data.get("market_regime") or "balanced").strip().lower()
        if regime not in {"risk_on", "balanced", "risk_off"}:
            regime = "balanced"
        default_factor = 1.0 if regime == "risk_on" else 0.7 if regime == "balanced" else 0.4
        min_factor = max(
            0.0,
            min(1.0, coerce_number(getattr(self.config, "DEEPSEEK_MARKET_GATE_MIN_SIZE_FACTOR", 0.25), 0.25)),
        )
        size_factor = self.clamp(coerce_number(data.get("size_factor"), default_factor), min_factor, 1.0)
        return {
            "regime": regime,
            "size_factor": round(size_factor, 3),
            "confidence": round(self.clamp(coerce_number(data.get("confidence"), 50.0), 0.0, 100.0), 2),
            "reason": str(data.get("reason") or data.get("summary") or "")[:240],
        }

    def local_gate(self, context: Dict[str, object]) -> Dict[str, object]:
        up_ratio = coerce_number(context.get("up_ratio_pct"), 50.0)
        limit_up_count = coerce_number(context.get("limit_up_count"), 0.0)
        avg_pct = coerce_number(context.get("avg_pct_chg"), 0.0)
        if up_ratio < 35 or avg_pct < -1.2:
            regime = "risk_off"
            factor = coerce_number(getattr(self.config, "PORTFOLIO_GROSS_RISK_OFF", 0.4), 0.4)
            reason = "本地大盘宽度偏弱，自动收缩推荐数量。"
        elif up_ratio > 58 and limit_up_count >= 20 and avg_pct > 0.5:
            regime = "risk_on"
            factor = coerce_number(getattr(self.config, "PORTFOLIO_GROSS_RISK_ON", 1.0), 1.0)
            reason = "本地大盘宽度较强，维持推荐数量。"
        else:
            regime = "balanced"
            factor = coerce_number(getattr(self.config, "PORTFOLIO_GROSS_BALANCED", 0.7), 0.7)
            reason = "本地大盘中性，轻微收缩推荐数量。"
        min_factor = max(
            0.0,
            min(1.0, coerce_number(getattr(self.config, "DEEPSEEK_MARKET_GATE_MIN_SIZE_FACTOR", 0.25), 0.25)),
        )
        return {
            "regime": regime,
            "size_factor": round(self.clamp(factor, min_factor, 1.0), 3),
            "confidence": 45.0,
            "reason": reason,
            "source": "local_market_gate",
        }

    @staticmethod
    def local_gate_decisive(result: Dict[str, object]) -> bool:
        return str((result or {}).get("regime") or "").strip().lower() in {"risk_on", "risk_off"}

    @staticmethod
    def _messages(context: Dict[str, object]):
        return [
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

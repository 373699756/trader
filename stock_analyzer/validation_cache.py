import time
from typing import Dict

from . import config
from .app_support import strategy_validation_gate_decision
from .performance import validation_metrics_cache_key


class ValidationMetricsCache:
    """Small TTL cache around validation metrics and summary lookups."""

    def __init__(self, validation_store, ttl_seconds: float = None) -> None:
        self.validation_store = validation_store
        self.ttl_seconds = float(config.REFRESH_SECONDS if ttl_seconds is None else ttl_seconds)
        self._metrics_cache: Dict[tuple, tuple] = {}
        self._summary_cache: Dict[tuple, tuple] = {}

    def clear(self) -> None:
        self._metrics_cache.clear()
        self._summary_cache.clear()

    def _metrics_key(self, strategy_name: str, days: int):
        store_key = getattr(self.validation_store, "metrics_cache_key", None)
        if callable(store_key):
            return store_key(strategy_name, days)
        repository = getattr(self.validation_store, "repository", None)
        repository_key = getattr(repository, "metrics_cache_key", None)
        if callable(repository_key):
            return repository_key(strategy_name, days)
        return validation_metrics_cache_key(strategy_name, "", days)

    def metrics(self, strategy_name: str, days: int):
        key = self._metrics_key(strategy_name, days)
        hit = self._metrics_cache.get(key)
        now = time.time()
        if hit is not None and now < float(hit[1]):
            return hit[0]
        value = self.validation_store.metrics(strategy_name, days=days)
        self._metrics_cache[key] = (value, now + self.ttl_seconds)
        return value

    def summary(self, strategy_name: str, days: int):
        key = self._metrics_key(strategy_name, days)
        hit = self._summary_cache.get(key)
        now = time.time()
        if hit is not None and now < float(hit[1]):
            return hit[0]
        metrics = self.metrics(strategy_name, days)
        deepseek_attribution_by_strategy = {
            item: self.validation_store.deepseek_attribution(item, days=days)
            for item in config.SNAPSHOT_STRATEGIES
        }
        latest_tuning = self.validation_store.latest_tuning_run(strategy_name)
        saved_deepseek_review = (latest_tuning.get("deepseek") or {}) if latest_tuning else {}
        value = {
            "metrics": metrics,
            "validation_gate": strategy_validation_gate_decision(metrics, strategy_name),
            "deepseek_attribution": deepseek_attribution_by_strategy.get(strategy_name, {}),
            "deepseek_attribution_by_strategy": deepseek_attribution_by_strategy,
            "deepseek_market_gate": self.validation_store.market_gate_metrics(days=days),
            "deepseek_review": saved_deepseek_review
            or {
                "enabled": False,
                "status": "not_requested",
                "strategy": strategy_name,
                "reason": "DeepSeek validation review only runs from tuning POST or scheduled end-of-day jobs.",
            },
        }
        self._summary_cache[key] = (value, now + min(self.ttl_seconds, 30.0))
        return value

"""Runtime cache-policy helpers."""

from __future__ import annotations

from trader.infra.settings import RuntimeSettings


def fixed_cache_ttl(settings: RuntimeSettings, dataset: str) -> float:
    value = settings.market_data.cache_policy.datasets[dataset].refresh_ttl_seconds
    if value is None:
        raise ValueError(f"cache dataset {dataset} does not define a fixed TTL")
    return value


__all__ = ["fixed_cache_ttl"]

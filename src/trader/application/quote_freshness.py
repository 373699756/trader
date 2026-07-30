"""Read-only quote freshness summaries for runtime status."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.application.status import RuntimeState
from trader.domain.recommendation.models import LiveOverlay, Strategy


def topk_quote_age(
    state: RuntimeState,
    overlays: Mapping[tuple[Strategy, str], LiveOverlay],
    now: datetime,
    *,
    target_seconds: float = 10.0,
) -> Mapping[str, object]:
    per_strategy: dict[str, object] = {}
    active_ages: list[float] = []
    excluded_frozen: list[str] = []
    for strategy in Strategy:
        if strategy is Strategy.LONG:
            continue
        snapshot = state.latest(strategy)
        if snapshot is None:
            continue
        overlay = overlays.get((strategy, snapshot.trade_date))
        if overlay is not None and overlay.snapshot_id == snapshot.snapshot_id:
            ages = [quote.age_seconds(now) for quote in overlay.quotes.values()]
        elif snapshot.frozen:
            excluded_frozen.append(strategy.value)
            continue
        else:
            ages = [item.features.quote.age_seconds(now) for item in snapshot.recommendations]
        active_ages.extend(ages)
        per_strategy[strategy.value] = _age_summary(ages)
    return {
        "target_seconds": target_seconds,
        **_age_summary(active_ages, target_seconds=target_seconds),
        "per_strategy": per_strategy,
        "excluded_frozen_strategies": sorted(excluded_frozen),
        "measured_at": now.isoformat(),
    }


def long_quote_age(
    state: RuntimeState,
    now: datetime,
    *,
    target_seconds: float = 10.0,
) -> Mapping[str, object]:
    snapshot = state.latest(Strategy.LONG)
    ages = (
        [
            item.features.quote.age_seconds(now)
            for item in snapshot.recommendations
            if item.features.quote.price is not None
        ]
        if snapshot is not None
        else []
    )
    return {
        "target_seconds": target_seconds,
        **_age_summary(ages, target_seconds=target_seconds),
        "measured_at": now.isoformat(),
    }


def _age_summary(ages: Sequence[float], *, target_seconds: float = 10.0) -> dict[str, object]:
    if not ages:
        return {
            "sample_count": 0,
            "p50_seconds": None,
            "p95_seconds": None,
            "maximum_seconds": None,
            "meets_target": None,
        }
    ordered = sorted(max(0.0, float(age)) for age in ages)
    p50_index = max(0, math.ceil(len(ordered) * 0.50) - 1)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    p50 = round(ordered[p50_index], 3)
    p95 = round(ordered[p95_index], 3)
    return {
        "sample_count": len(ordered),
        "p50_seconds": p50,
        "p95_seconds": p95,
        "maximum_seconds": round(ordered[-1], 3),
        "meets_target": p95 <= target_seconds,
    }


__all__ = ["long_quote_age", "topk_quote_age"]

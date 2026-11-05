"""Route plans for the live market-factor comparison."""

from __future__ import annotations

from dataclasses import dataclass

_QUOTE_SOURCES = frozenset({"tencent", "eastmoney", "sina"})


@dataclass(frozen=True)
class RoutePlan:
    name: str
    sources: tuple[str, ...]
    scheduling: str


def RoutePlans(sources: tuple[str, ...], mode: str) -> tuple[RoutePlan, ...]:
    plans: list[RoutePlan] = []
    quote_sources = tuple(source for source in sources if source in _QUOTE_SOURCES)
    if mode != "combined":
        plans.extend(RoutePlan(source, (source,), "single") for source in quote_sources)
    if mode == "single" or "tencent" not in sources:
        return tuple(plans)
    if not any(plan.sources == ("tencent",) for plan in plans):
        plans.append(RoutePlan("tencent", ("tencent",), "single"))
    if "sina" in sources:
        plans.append(RoutePlan("tencent_then_sina", ("tencent", "sina"), "sequential_failure_backup"))
    return tuple(plans)


__all__ = ["RoutePlan", "RoutePlans"]

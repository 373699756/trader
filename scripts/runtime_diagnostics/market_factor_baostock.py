"""BaoStock current-industry capability for market-factor comparisons."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from trader.recommendation.domain.market.models import ModelIndustryReference

from .reporting import summarize_latency_ms

BAOSTOCK_REFERENCE_FIELDS = (
    "model_industry",
    "model_industry_classification",
    "model_industry_effective_date",
    "model_industry_source",
    "model_industry_data_version",
)


@dataclass(frozen=True)
class BaostockCapability:
    active_industries: Mapping[str, ModelIndustryReference]
    elapsed_ms: float

    @property
    def mode(self) -> str:
        if self.active_industries:
            return "validated_local_industry_query"
        return "unavailable"


def ProbeBaoStockIndustry(
    existing: Mapping[str, ModelIndustryReference],
    elapsed_ms: float,
) -> BaostockCapability:
    return BaostockCapability(MappingProxyType(dict(existing)), elapsed_ms)


def BaoStockSourceResults(
    capability: BaostockCapability,
    scopes: Sequence[tuple[object, tuple[str, ...]]],
    rounds: int,
    instant_quote_fields: Sequence[str],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for scope, codes in scopes:
        covered = sum(code in capability.active_industries for code in codes)
        total = len(codes)
        coverage = covered / total if total else 0.0
        results.append(
            {
                "source": "baostock",
                "kind": "local_reference",
                "scope": scope,
                "rounds": rounds,
                "network_fetch_rounds": 0,
                "cache_reuse_rounds": rounds,
                "network_status": "not_requested",
                "mode": capability.mode,
                "error": None,
                "requested_rows": total,
                "returned_rows": covered,
                "physical_rows": len(capability.active_industries),
                "coverage": round(coverage, 4),
                "request_timings_ms": summarize_latency_ms([capability.elapsed_ms]),
                "supported_reference_fields": list(BAOSTOCK_REFERENCE_FIELDS),
                "unsupported_instant_quote_fields": list(instant_quote_fields),
                "invalid_rows": 0,
            }
        )
    return results


__all__ = [
    "BAOSTOCK_REFERENCE_FIELDS",
    "BaostockCapability",
    "BaoStockSourceResults",
    "ProbeBaoStockIndustry",
]

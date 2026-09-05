"""Capability-only contract for historical facts required by Tomorrow V3."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.domain.research.h1_point_in_time import canonical_hash

HistoricalEffectiveFactsStatus = Literal["historical_effective_facts_ready", "historical_data_insufficient"]


@dataclass(frozen=True)
class HistoricalEffectiveFactsProbe:
    source: str
    earliest_available: date | None
    industry_effective_at: bool
    eligibility_effective_at: bool
    hard_filter_effective_at: bool
    risk_facts_effective_at: bool
    schema_version: str = "historical_effective_facts_probe"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.source.strip() or self.schema_version != "historical_effective_facts_probe":
            raise ValueError("historical effective-facts probe identity is invalid")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalEffectiveFactsAudit:
    probes: tuple[HistoricalEffectiveFactsProbe, ...]
    status: HistoricalEffectiveFactsStatus
    failure_reasons: tuple[str, ...]
    point_in_time_parity: bool = False
    v3_training_authority: bool = False
    production_authority: bool = False
    schema_version: str = "historical_effective_facts_capability"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        probes = tuple(sorted(self.probes, key=lambda item: item.source))
        if not probes or len({item.source for item in probes}) != len(probes):
            raise ValueError("historical effective-facts audit requires unique probes")
        if self.status not in ("historical_effective_facts_ready", "historical_data_insufficient"):
            raise ValueError("historical effective-facts status is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if (self.status == "historical_effective_facts_ready") == bool(reasons):
            raise ValueError("historical effective-facts status and reasons are inconsistent")
        if self.v3_training_authority != (self.status == "historical_effective_facts_ready"):
            raise ValueError("effective-facts training eligibility must match capability status")
        if self.point_in_time_parity or self.production_authority:
            raise ValueError("effective-facts capability audit cannot authorize parity or production")
        if self.schema_version != "historical_effective_facts_capability":
            raise ValueError("historical effective-facts audit schema is invalid")
        object.__setattr__(self, "probes", probes)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def build_historical_effective_facts_audit(
    probes: tuple[HistoricalEffectiveFactsProbe, ...],
) -> HistoricalEffectiveFactsAudit:
    if not probes:
        raise ValueError("historical effective-facts audit requires at least one source")
    capabilities = (
        ("historical_industry_effective_at_unavailable", any(probe.industry_effective_at for probe in probes)),
        ("historical_eligibility_effective_at_unavailable", any(probe.eligibility_effective_at for probe in probes)),
        ("historical_hard_filter_effective_at_unavailable", any(probe.hard_filter_effective_at for probe in probes)),
        ("historical_risk_facts_effective_at_unavailable", any(probe.risk_facts_effective_at for probe in probes)),
    )
    reasons = tuple(reason for reason, available in capabilities if not available)
    return HistoricalEffectiveFactsAudit(
        probes,
        "historical_effective_facts_ready" if not reasons else "historical_data_insufficient",
        reasons,
        v3_training_authority=not reasons,
    )


def baostock_effective_facts_probe() -> HistoricalEffectiveFactsProbe:
    return HistoricalEffectiveFactsProbe("baostock", None, False, False, False, False)


__all__ = [
    "HistoricalEffectiveFactsAudit",
    "HistoricalEffectiveFactsProbe",
    "HistoricalEffectiveFactsStatus",
    "baostock_effective_facts_probe",
    "build_historical_effective_facts_audit",
]

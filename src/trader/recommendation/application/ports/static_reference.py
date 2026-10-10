"""Accepted official population read boundary."""

from dataclasses import dataclass

from trader.recommendation.domain.market.static import StaticMarketReference


@dataclass(frozen=True, slots=True)
class StaticReferenceRead:
    reference: StaticMarketReference | None
    reference_epoch: str
    refresh_failed: bool
    refresh_ttl_seconds: float

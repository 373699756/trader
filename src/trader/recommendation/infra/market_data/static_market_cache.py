"""Resident issuer projection of accepted references; no supplier or history I/O."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime

from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.evidence.pipeline import SourceHealth, SourceHealthState
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.market.static import StaticIssuer


@dataclass(frozen=True, slots=True)
class StaticIssuerBaseline:
    records: tuple[StaticIssuer, ...]
    reference_epoch: str
    identity: str
    latest_success_at: datetime | None
    source_count: int

    def health(self, observed_at: datetime) -> SourceHealth:
        return SourceHealth(
            SourceHealthState.READY if self.source_count else SourceHealthState.UNAVAILABLE,
            self.source_count,
            self.source_count,
            self.latest_success_at,
            max(0.0, (observed_at - self.latest_success_at).total_seconds())
            if self.latest_success_at is not None
            else None,
        )


@dataclass(frozen=True, slots=True)
class StaticBaselineRead:
    baseline: StaticIssuerBaseline
    cache_hit: bool


class StaticMarketCache:
    """Reuse a lightweight baseline until accepted issuer fields or references change.

    Population is the accepted market read, not an invented complete exchange
    universe. Quote prices/timestamps never invalidate this static projection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._baseline: StaticIssuerBaseline | None = None

    def read(self, quotes: tuple[MarketQuote, ...], reference_epoch: str) -> StaticBaselineRead:
        records = tuple(
            StaticIssuer(
                quote.code,
                quote.name,
                quote.board,
                quote.exchange,
                quote.listing_date,
                quote.is_st,
                quote.is_blacklisted,
            )
            for quote in sorted(quotes, key=lambda item: item.code)
        )
        if len({item.code for item in records}) != len(records):
            raise ValueError("static market input codes must be unique")
        with self._lock:
            previous = self._baseline
            if previous is not None and previous.reference_epoch == reference_epoch and previous.records == records:
                return StaticBaselineRead(previous, True)
            identity = hashlib.sha256(repr((reference_epoch, records)).encode("utf-8")).hexdigest()
            baseline = StaticIssuerBaseline(
                records,
                reference_epoch,
                identity,
                max((quote.received_time.astimezone(SHANGHAI) for quote in quotes), default=None),
                len({quote.source for quote in quotes}),
            )
            self._baseline = baseline
            return StaticBaselineRead(baseline, False)


__all__ = ["StaticMarketCache", "StaticBaselineRead", "StaticIssuerBaseline"]

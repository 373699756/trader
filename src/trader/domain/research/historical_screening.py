"""Immutable contract for downloadable retrospective score screening."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class HistoricalScreeningSpec:
    research_identity: str
    registered_on: date
    source_cutoff: date
    download_sessions: int
    training_start: date
    training_end: date
    validation_start: date
    validation_end: date
    minimum_history_sessions: int
    label_horizon_sessions: int
    round_trip_cost_bps: int
    promotion_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if _IDENTITY.fullmatch(self.research_identity) is None:
            raise ValueError("historical screening identity is invalid")
        if not self.training_start <= self.training_end < self.validation_start <= self.validation_end:
            raise ValueError("historical screening training window must precede validation")
        if self.source_cutoff < self.validation_end + timedelta(days=self.label_horizon_sessions):
            raise ValueError("historical screening source cutoff cannot cover the labels")
        if self.registered_on <= self.source_cutoff:
            raise ValueError("historical screening registration must follow its retrospective source cutoff")
        if self.download_sessions < self.minimum_history_sessions + self.label_horizon_sessions:
            raise ValueError("historical screening download window is too short")
        if self.minimum_history_sessions < 20 or not 1 <= self.label_horizon_sessions <= 20:
            raise ValueError("historical screening history or label horizon is invalid")
        if self.round_trip_cost_bps < 0 or self.round_trip_cost_bps > 1000:
            raise ValueError("historical screening cost is invalid")
        if self.promotion_authority:
            raise ValueError("retrospective historical screening cannot have promotion authority")
        payload = {
            field.name: _canonical(getattr(self, field.name)) for field in dataclasses.fields(self) if field.init
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "content_hash", hashlib.sha256(encoded.encode()).hexdigest())


@dataclass(frozen=True)
class HistoricalPriceBar:
    trade_date: date
    open_price: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    pct_change: float
    turnover_rate: float | None
    adjustment: str
    source: str

    def __post_init__(self) -> None:
        values = (self.open_price, self.close, self.high, self.low, self.volume, self.amount, self.pct_change)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("historical screening bar values must be finite")
        if min(self.open_price, self.close, self.high, self.low) <= 0.0 or min(self.volume, self.amount) < 0.0:
            raise ValueError("historical screening bar prices or flows are invalid")
        if self.low > min(self.open_price, self.close) or self.high < max(self.open_price, self.close):
            raise ValueError("historical screening bar OHLC is inconsistent")
        if self.turnover_rate is not None and not math.isfinite(self.turnover_rate):
            raise ValueError("historical screening turnover must be finite")
        if self.adjustment != "qfq" or not self.source:
            raise ValueError("historical screening bars require a qfq source")


def _canonical(value: object) -> object:
    return value.isoformat() if isinstance(value, date) else value


SCORE_H0_V1_SPEC = HistoricalScreeningSpec(
    research_identity="score_h0_v1",
    registered_on=date(2026, 8, 20),
    source_cutoff=date(2026, 8, 19),
    download_sessions=640,
    training_start=date(2024, 7, 1),
    training_end=date(2025, 12, 31),
    validation_start=date(2026, 1, 1),
    validation_end=date(2026, 7, 31),
    minimum_history_sessions=61,
    label_horizon_sessions=5,
    round_trip_cost_bps=20,
)


__all__ = ["HistoricalPriceBar", "HistoricalScreeningSpec", "SCORE_H0_V1_SPEC"]

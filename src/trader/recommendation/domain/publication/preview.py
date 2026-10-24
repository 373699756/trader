"""Immutable identity for an ephemeral run using a completed market session."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class PreviewContext:
    run_id: str
    market_trade_date: date
    started_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or _RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("preview run identity is invalid")
        if type(self.market_trade_date) is not date:
            raise ValueError("preview market date is invalid")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("preview start time must be timezone-aware")
        started_at = self.started_at.astimezone(_SHANGHAI)
        if self.market_trade_date > started_at.date():
            raise ValueError("preview cannot refer to a future market session")
        object.__setattr__(self, "started_at", started_at)

    @property
    def market_as_of(self) -> datetime:
        return datetime.combine(self.market_trade_date, time(15, 0), tzinfo=_SHANGHAI)

    def accepts_quote(self, source_time: datetime, observed_at: datetime) -> bool:
        if (
            source_time.tzinfo is None
            or source_time.utcoffset() is None
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            return False
        return (
            source_time.astimezone(_SHANGHAI).date() == self.market_trade_date
            and self.market_as_of <= source_time <= observed_at
        )


__all__ = ["PreviewContext"]

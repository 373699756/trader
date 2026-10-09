"""Typed refresh boundary for the current-only Long projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class LongRefreshRequest:
    observed_at: datetime
    phase: str
    deadline: datetime | None = None
    force: bool = False

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at, "long refresh observed_at")
        if not self.phase:
            raise ValueError("long refresh phase must not be empty")
        if self.deadline is not None:
            _require_shanghai(self.deadline, "long refresh deadline")


class LongRefreshPort(Protocol):
    def offer_refresh(self, request: LongRefreshRequest) -> bool: ...


def _require_shanghai(value: datetime, label: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or not (isinstance(value.tzinfo, ZoneInfo) and value.tzinfo.key == "Asia/Shanghai")
    ):
        raise ValueError(f"{label} must use Asia/Shanghai")


__all__ = ["LongRefreshPort", "LongRefreshRequest"]

"""Timezone-aware clock adapter for Shanghai business time."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ShanghaiClock:
    """Normalize an injected wall clock to the product business timezone."""

    value: Callable[[], datetime]

    def now(self) -> datetime:
        current = self.value()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("business clock must be timezone-aware")
        return current.astimezone(_SHANGHAI)


__all__ = ["ShanghaiClock"]

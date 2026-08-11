"""Read ports for the offline Score-R2 evidence boundary."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from trader.application.ports.market import DataPlaneReadPort
from trader.application.research.models import (
    HistoricalDaySummary,
    HistoricalFullFieldBundle,
)


class HistoricalDataPlaneReadPort(DataPlaneReadPort, Protocol):
    """Offline extension of the canonical E1 read port for Score-R2 adapters.

    Implementations retain the canonical immutable snapshot boundary and must
    discard hard-reject identities when projecting historical research data.
    """

    def is_trading_day(self, trade_date: date) -> bool: ...

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary: ...

    def load_full_fields(
        self,
        trade_date: date,
        codes: tuple[str, ...],
    ) -> HistoricalFullFieldBundle: ...


__all__ = ["HistoricalDataPlaneReadPort"]

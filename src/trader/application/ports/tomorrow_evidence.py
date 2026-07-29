"""Persistence boundary for tomorrow shadow cutover observations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from trader.application.tomorrow_shadow import TomorrowShadowObservation


class TomorrowShadowEvidencePort(Protocol):
    def record(self, observation: TomorrowShadowObservation) -> None: ...


__all__ = ["TomorrowShadowEvidencePort"]

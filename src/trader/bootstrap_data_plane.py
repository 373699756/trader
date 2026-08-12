"""Startup recovery for the V2 data plane only."""

from __future__ import annotations

import logging

from trader.infra.market_data.service import MarketFeatureService
from trader.infra.persistence.data_plane import DataPlaneRepository

_LOGGER = logging.getLogger(__name__)


def _initialize_reference_data_plane(
    market_data: MarketFeatureService,
    data_plane: DataPlaneRepository,
) -> None:
    try:
        data_plane.initialize()
        market_data.references.recover()
        market_data.history.recover_from_data_plane()
        market_data.research.recover_from_data_plane()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOGGER.warning("V2 data plane initialization degraded: %s", type(exc).__name__)


def initialize_reference_data_plane(
    market_data: MarketFeatureService,
    data_plane: DataPlaneRepository,
) -> None:
    _initialize_reference_data_plane(market_data, data_plane)


__all__ = ["initialize_reference_data_plane", "_initialize_reference_data_plane"]

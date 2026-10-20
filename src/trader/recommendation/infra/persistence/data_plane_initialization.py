"""Startup recovery for the current data plane only."""

from __future__ import annotations

import logging

from trader.recommendation.infra.market_data.market_feature_service import MarketFeatureService
from trader.recommendation.infra.persistence.data_plane import SQLiteDataPlane

_LOGGER = logging.getLogger(__name__)


def InitializeReferenceDataPlane(
    market_data: MarketFeatureService,
    data_plane: SQLiteDataPlane,
) -> None:
    try:
        data_plane.initialize()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOGGER.warning("data plane initialization degraded: %s", type(exc).__name__)
    try:
        market_data.references.initialize()
        market_data.quotes.gateway.update_reference_observations(market_data.references.security_observations())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOGGER.warning("reference initialization degraded: %s", type(exc).__name__)


__all__ = ["InitializeReferenceDataPlane"]

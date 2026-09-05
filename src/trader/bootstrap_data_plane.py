"""Startup recovery for the current data plane only."""

from __future__ import annotations

import logging
from datetime import datetime

from trader.infra.market_data.service.facade import MarketFeatureService
from trader.infra.persistence.data_plane import DataPlaneRepository

_LOGGER = logging.getLogger(__name__)


def _initialize_reference_data_plane(
    market_data: MarketFeatureService,
    data_plane: DataPlaneRepository,
    observed_at: datetime | None = None,
) -> None:
    try:
        data_plane.initialize()
        market_data.references.recover()
        market_data.history.recover_from_data_plane()
        market_data.research.recover_from_data_plane()
        if observed_at is not None:
            market_data.references.schedule_security_master_refresh(observed_at)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOGGER.warning("data plane initialization degraded: %s", type(exc).__name__)


def initialize_reference_data_plane(
    market_data: MarketFeatureService,
    data_plane: DataPlaneRepository,
    observed_at: datetime | None = None,
) -> None:
    _initialize_reference_data_plane(market_data, data_plane, observed_at)


__all__ = ["initialize_reference_data_plane", "_initialize_reference_data_plane"]

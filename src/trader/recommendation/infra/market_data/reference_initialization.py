"""Load the published download reference into the shared quote input."""

import logging

from trader.recommendation.infra.market_data.market_feature_service import MarketFeatureService

_LOGGER = logging.getLogger(__name__)


def InitializeMarketReferences(market_data: MarketFeatureService) -> None:
    try:
        market_data.references.initialize()
        market_data.quotes.gateway.update_reference_observations(market_data.references.security_observations())
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _LOGGER.warning("reference initialization degraded: %s", type(exc).__name__)


__all__ = ["InitializeMarketReferences"]

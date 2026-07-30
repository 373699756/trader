from __future__ import annotations

import logging

from trader.application.tomorrow_shadow import TomorrowCutoverGate
from trader.infra.market_data.service import MarketFeatureService
from trader.infra.persistence.data_plane import DataPlaneRepository
from trader.infra.persistence.tomorrow_shadow_evidence import (
    TomorrowShadowEvidenceRepository,
    TomorrowShadowEvidenceUnavailableError,
)

_LOGGER = logging.getLogger(__name__)


def _initialize_reference_data_plane(
    market_data: MarketFeatureService,
    data_plane: DataPlaneRepository,
) -> None:
    try:
        data_plane.initialize()
        market_data.references.recover()
        if (history := getattr(market_data, "history", None)) is not None:
            history.recover_from_data_plane()
        if (research := getattr(market_data, "research", None)) is not None:
            research.recover_from_data_plane()
    except Exception as exc:
        _LOGGER.warning("reference data plane initialization failed: %s", type(exc).__name__)


def initialize_reference_data_plane(
    market_data: MarketFeatureService,
    data_plane: DataPlaneRepository,
) -> None:
    """Backward-compatible public name for bootstrap wiring."""

    _initialize_reference_data_plane(market_data, data_plane)


def initialize_tomorrow_evidence(
    tomorrow_evidence: TomorrowShadowEvidenceRepository,
    tomorrow_gate: TomorrowCutoverGate,
) -> None:
    """Initialize tomorrow shadow evidence and restore gate state."""

    try:
        tomorrow_evidence.initialize()
        tomorrow_gate.restore(tomorrow_evidence.load_recent())
    except TomorrowShadowEvidenceUnavailableError:
        tomorrow_gate.mark_evidence_failure()


__all__ = [
    "initialize_reference_data_plane",
    "initialize_tomorrow_evidence",
    "_initialize_reference_data_plane",
]

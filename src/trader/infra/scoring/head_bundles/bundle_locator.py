"""Locate all configured shared head bundles without parsing model payloads."""

from __future__ import annotations

from pathlib import Path

from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.head_bundles.bundle_repository import locate_active_head_bundle
from trader.infra.scoring.profiles.v3.contracts import V3_TRAINING_PROFILE


def locate_head_bundles(training_root: Path) -> tuple[tuple[Strategy, Path], ...]:
    strategies = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    return tuple(
        (
            strategy,
            locate_active_head_bundle(training_root / f"{strategy.value}-v3", strategy, V3_TRAINING_PROFILE),
        )
        for strategy in strategies
    )


__all__ = ["locate_head_bundles"]

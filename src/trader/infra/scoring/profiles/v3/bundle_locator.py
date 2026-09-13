"""Locate all configured V3 head bundles without parsing model payloads."""

from __future__ import annotations

from pathlib import Path

from trader.domain.recommendation.models import Strategy


def locate_head_bundles(training_root: Path) -> tuple[tuple[Strategy, Path], ...]:
    from trader.infra.scoring.profiles.v3.training_bundle_repository import locate_active_head_bundle

    strategies = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    return tuple(
        (strategy, locate_active_head_bundle(training_root / f"{strategy.value}-v3", strategy))
        for strategy in strategies
    )


__all__ = ["locate_head_bundles"]

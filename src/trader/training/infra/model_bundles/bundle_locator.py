"""Locate all configured shared head bundles without parsing model payloads."""

from __future__ import annotations

from pathlib import Path

from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_repository import LocateActiveHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract


def LocateHeadBundles(
    training_root: Path,
    profile: TrainedProfileContract,
) -> tuple[tuple[Strategy, Path], ...]:
    """Locate all heads below the selected profile's owned training directory."""

    return tuple(
        (
            contract.strategy,
            LocateActiveHeadBundle(
                training_root / profile.output_directory / contract.directory_name,
                contract.strategy,
                profile,
            ),
        )
        for contract in profile.heads
    )


__all__ = ["LocateHeadBundles"]

"""Locate all configured shared head bundles without parsing model payloads."""

from __future__ import annotations

from pathlib import Path

from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.head_bundles.bundle_repository import locate_active_head_bundle
from trader.infra.scoring.head_bundles.contracts import TrainedProfileContract


def locate_head_bundles(
    training_root: Path,
    profile: TrainedProfileContract,
) -> tuple[tuple[Strategy, Path], ...]:
    """Locate all heads below the selected profile's owned training directory."""

    return tuple(
        (
            contract.strategy,
            locate_active_head_bundle(
                training_root / profile.output_directory / contract.directory_name,
                contract.strategy,
                profile,
            ),
        )
        for contract in profile.heads
    )


__all__ = ["locate_head_bundles"]

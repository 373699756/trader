"""Read-only use case for the Tomorrow V3 frozen daily input contract."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from trader.domain.research.tomorrow_training_input import (
    FrozenDailyInputDescriptor,
    TomorrowInputCompatibility,
    evaluate_tomorrow_training_input,
)


class TomorrowFrozenDailyInputPort(Protocol):
    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...


def verify_tomorrow_training_input_port(
    port: TomorrowFrozenDailyInputPort,
    *,
    expected_manifest_hash: str,
    expected_source_cutoff: date,
) -> TomorrowInputCompatibility:
    """Bind B's compatibility result to one frozen A manifest."""

    return evaluate_tomorrow_training_input(
        port.describe_frozen_daily_input(),
        expected_manifest_hash=expected_manifest_hash,
        expected_source_cutoff=expected_source_cutoff,
    )


__all__ = ["TomorrowFrozenDailyInputPort", "verify_tomorrow_training_input_port"]

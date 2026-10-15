"""Read-only use case for the Tomorrow V3 frozen daily input contract."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from trader.training.domain.tomorrow_training_input import (
    EvaluateTomorrowTrainingInput,
    FrozenDailyInputDescriptor,
    TomorrowTrainingInputCompatibility,
)


class TomorrowFrozenDailyInputPort(Protocol):
    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...


def VerifyTomorrowTrainingInputPort(
    port: TomorrowFrozenDailyInputPort,
    *,
    expected_manifest_hash: str,
    expected_source_cutoff: date,
) -> TomorrowTrainingInputCompatibility:
    """Bind B's compatibility result to one frozen A manifest."""

    return EvaluateTomorrowTrainingInput(
        port.describe_frozen_daily_input(),
        expected_manifest_hash=expected_manifest_hash,
        expected_source_cutoff=expected_source_cutoff,
    )


__all__ = ["TomorrowFrozenDailyInputPort", "VerifyTomorrowTrainingInputPort"]

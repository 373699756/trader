"""Read-only use case for the Tomorrow V3 frozen daily input contract."""

from __future__ import annotations

from typing import Protocol

from trader.domain.research.tomorrow_v3_input_compatibility import (
    FrozenDailyInputDescriptor,
    TomorrowV3InputCompatibility,
    evaluate_tomorrow_v3_input_compatibility,
)


class TomorrowV3FrozenDailyInputPort(Protocol):
    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor: ...


def verify_tomorrow_v3_input_port(
    port: TomorrowV3FrozenDailyInputPort,
    *,
    expected_manifest_hash: str,
) -> TomorrowV3InputCompatibility:
    """Bind B's compatibility result to one frozen A manifest."""

    return evaluate_tomorrow_v3_input_compatibility(
        port.describe_frozen_daily_input(),
        expected_manifest_hash=expected_manifest_hash,
    )


__all__ = ["TomorrowV3FrozenDailyInputPort", "verify_tomorrow_v3_input_port"]
